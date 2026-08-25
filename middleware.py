"""
Security headers and rate limiting middleware.

Implements two related concerns that both live in front of every request:

* **Issue #147 - security headers.** ``SecurityHeadersMiddleware`` adds
  ``X-Content-Type-Options``, ``X-Frame-Options``, ``Referrer-Policy`` and a
  ``Content-Security-Policy`` to every response, including streamed
  ``FileResponse`` downloads and the ``429`` bodies produced below.
* **Issue #146 - rate limiting.** ``RateLimitMiddleware`` applies a tight limit
  to ``/upload`` (one request spawns a job that makes one LibreTranslate call
  per trans-unit, with retries, against an HTTP client capped at
  ``MAX_CONNECTIONS``), a looser default to the read endpoints, and exempts
  ``/health`` entirely so the Docker healthcheck can never throttle itself into
  an unhealthy container.

Both are registered by a single call to :func:`install_middleware`.

CSRF protection is deliberately **not** implemented: the app has no cookies, no
sessions and no authentication, so there is no ambient authority for a
cross-site request to abuse. See issue #147.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional, Sequence, Tuple

from limits import RateLimitItem, parse_many
from limits.strategies import RateLimiter
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Security headers (issue #147)
# ---------------------------------------------------------------------------

#: Headers that are identical on every response.
STATIC_SECURITY_HEADERS: Dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}

#: Strict policy for the application itself.
#:
#: The UI carries no inline script and no inline style: ``templates/index.html``
#: loads ``/static/app.js`` and ``/static/style.css`` and wires its buttons up
#: with ``addEventListener``, so ``script-src``/``style-src`` need no
#: ``'unsafe-inline'``. ``tests/test_middleware.py`` guards that with a
#: regression test over the template, because re-introducing an inline
#: ``onclick=`` would silently break the page under this policy.
#:
#: ``img-src`` allows ``https://bondit.services`` for the BondIT favicon
#: referenced from the template.
APP_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: https://bondit.services",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

#: Relaxed policy for the auto-generated API documentation only.
#:
#: FastAPI serves Swagger UI and ReDoc from jsDelivr and bootstraps them with an
#: inline ``<script>``; under ``APP_CSP`` both pages render blank. The relaxation
#: is scoped to :data:`DOCS_PATHS` so it can never apply to the application's own
#: HTML, JSON or download responses. Serving the docs assets locally would let
#: this be dropped -- worth a follow-up, but it is a documentation UI, not the
#: user-facing app.
DOCS_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "img-src 'self' data: https://cdn.jsdelivr.net https://fastapi.tiangolo.com",
        "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com",
        "worker-src 'self' blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)

#: Paths that render the vendored documentation UIs.
DOCS_PATHS: Tuple[str, ...] = ("/docs", "/redoc", "/docs/oauth2-redirect")


def csp_for_path(path: str) -> str:
    """Return the Content-Security-Policy that applies to ``path``."""
    if path in DOCS_PATHS:
        return DOCS_CSP
    return APP_CSP


class SecurityHeadersMiddleware:
    """
    Pure ASGI middleware that stamps the security headers onto every response.

    Implemented at the ASGI layer rather than as a ``BaseHTTPMiddleware`` so it
    does not buffer or re-wrap streamed responses -- ``/download`` serves files
    through ``FileResponse``, and ``/upload`` hands work to a background task.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        csp = csp_for_path(scope.get("path", ""))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in STATIC_SECURITY_HEADERS.items():
                    headers[name] = value
                headers["content-security-policy"] = csp
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------------------
# Rate limiting (issue #146)
# ---------------------------------------------------------------------------

#: Never rate limited. The compose healthcheck polls this every 30s; limiting it
#: would let the container throttle itself into an unhealthy state.
EXEMPT_PATHS: Tuple[str, ...] = ("/health",)

#: The expensive endpoint, limited separately from everything else.
UPLOAD_PATH = "/upload"


def client_identifier(request: Request) -> str:
    """
    Resolve the rate-limit key for a request.

    **Deployment assumption:** by default the peer address of the socket is
    used, i.e. the app is assumed to be reached directly or through a proxy that
    preserves the peer address. ``X-Forwarded-For`` is client-supplied and
    trivially spoofed, so honouring it on a directly exposed deployment would
    let any caller bypass the limit by varying the header. Set
    ``RATE_LIMIT_TRUST_FORWARDED_FOR=true`` only when a reverse proxy you
    control sets that header -- without it, every request behind such a proxy
    shares one key and the limit becomes global.

    slowapi ships ``get_ipaddr()`` for this, but it looks the header up as
    ``X_FORWARDED_FOR`` (underscores), which never matches the real
    ``X-Forwarded-For``, so the lookup is done here instead.
    """
    if settings.rate_limit_trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first_hop = forwarded.split(",")[0].strip()
            if first_hop:
                return first_hop
    return get_remote_address(request)


def build_rate_limit_response(retry_after: int, limit: str) -> JSONResponse:
    """Build the 429 response, always carrying ``Retry-After`` (seconds)."""
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {limit}"},
        headers={"Retry-After": str(retry_after)},
    )


def retry_after_seconds(
    strategy: RateLimiter, item: RateLimitItem, *identifiers: str
) -> int:
    """Seconds until ``item`` lets ``identifiers`` through again (at least 1)."""
    reset_time = strategy.get_window_stats(item, *identifiers).reset_time
    return max(1, math.ceil(reset_time - time.time()))


async def rate_limit_exceeded_handler(
    _request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """
    Handle ``RateLimitExceeded``.

    :class:`RateLimitMiddleware` returns its 429 directly -- an exception raised
    from middleware never reaches the app's handlers. This exists so that any
    future ``@limiter.limit(...)`` decorator on a route produces the same 429
    shape instead of a 500. Falls back to the full window length, which is the
    conservative answer when the offending key is not in hand.
    """
    item = exc.limit.limit
    return build_rate_limit_response(max(1, item.get_expiry()), str(item))


class RateLimitMiddleware:
    """
    Path-based rate limiting on top of a slowapi :class:`~slowapi.Limiter`.

    slowapi's usual entry point is the ``@limiter.limit(...)`` decorator, which
    requires adding a ``request: Request`` parameter to each route. Matching on
    the path instead keeps the limits declared in one place and leaves the route
    signatures in ``app.py`` untouched, while still using slowapi's configured
    storage and strategy to do the counting.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_limiter: Limiter,
        default_limit: str,
        upload_limit: str,
        exempt_paths: Sequence[str] = EXEMPT_PATHS,
    ) -> None:
        self.app = app
        self.limiter = rate_limiter
        self.exempt_paths = tuple(exempt_paths)
        self.default_items: List[RateLimitItem] = parse_many(default_limit)
        self.upload_items: List[RateLimitItem] = parse_many(upload_limit)

    def rule_for(self, path: str) -> Optional[Tuple[List[RateLimitItem], str]]:
        """Return the limits and bucket name for ``path``, or ``None`` if exempt."""
        if path in self.exempt_paths:
            return None
        if path == UPLOAD_PATH:
            return self.upload_items, "upload"
        return self.default_items, "default"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        rule = self.rule_for(path)
        if rule is None:
            await self.app(scope, receive, send)
            return

        items, bucket = rule
        key = client_identifier(Request(scope))
        strategy = self.limiter.limiter

        for item in items:
            if not strategy.hit(item, key, bucket):
                retry_after = retry_after_seconds(strategy, item, key, bucket)
                logger.warning(
                    "Rate limit %s exceeded for %s on %s (retry after %ss)",
                    item,
                    key,
                    path,
                    retry_after,
                )
                response = build_rate_limit_response(retry_after, str(item))
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def create_limiter() -> Limiter:
    """Build the slowapi limiter from settings."""
    return Limiter(
        key_func=client_identifier,
        default_limits=[settings.rate_limit_default],
        storage_uri=settings.rate_limit_storage_uri,
        strategy="moving-window",
        headers_enabled=True,
    )


#: Shared limiter. Module level so tests (and any future decorator use) address
#: the same storage the middleware counts into.
limiter = create_limiter()


def reset_rate_limits() -> None:
    """Drop every recorded hit. Used by the test suite to isolate tests."""
    limiter.reset()


def install_middleware(app) -> Limiter:
    """
    Register the security headers and rate limiting middleware on ``app``.

    Starlette runs the most recently added middleware outermost, so the rate
    limiter is added first and the header middleware second: a 429 short-circuits
    the app but still passes back out through the header middleware.
    """
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(
        RateLimitMiddleware,
        rate_limiter=limiter,
        default_limit=settings.rate_limit_default,
        upload_limit=settings.rate_limit_upload,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    logger.info(
        "Middleware installed (rate limiting %s: %s default, %s on %s; %s exempt)",
        "enabled" if settings.rate_limit_enabled else "disabled",
        settings.rate_limit_default,
        settings.rate_limit_upload,
        UPLOAD_PATH,
        ", ".join(EXEMPT_PATHS),
    )
    return limiter
