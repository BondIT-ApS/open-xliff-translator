"""
Tests for middleware.py.

Covers issue #147 (security headers on every response type, strict CSP) and
issue #146 (rate limiting, /health exemption, 429 with Retry-After).
"""
import os
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from limits import parse_many
from slowapi.errors import RateLimitExceeded
from slowapi.wrappers import Limit
from starlette.requests import Request

import middleware
from app import app
from middleware import (
    APP_CSP,
    DOCS_CSP,
    DOCS_PATHS,
    STATIC_SECURITY_HEADERS,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    build_rate_limit_response,
    client_identifier,
    create_limiter,
    csp_for_path,
    install_middleware,
    rate_limit_exceeded_handler,
    retry_after_seconds,
)
from settings import settings

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEMPLATE_PATH = os.path.join(PROJECT_ROOT, "templates", "index.html")

APP_JS_PATH = os.path.join(PROJECT_ROOT, "static", "app.js")


@pytest.fixture
def client():
    """Test client for the real application."""
    return TestClient(app)


def upload_limit_amount() -> int:
    """The number of /upload requests allowed before a 429."""
    return parse_many(settings.rate_limit_upload)[0].amount


def default_limit_amount() -> int:
    """The number of requests allowed on non-upload endpoints before a 429."""
    return parse_many(settings.rate_limit_default)[0].amount


def post_rejected_upload(test_client):
    """
    POST an upload the route rejects cheaply.

    A non-.xlf file is refused before any job is created, so the limit can be
    exercised without spawning translation work. Only the status code matters.
    """
    return test_client.post(
        "/upload", files={"file": ("not-xliff.txt", b"nope", "text/plain")}
    )


def assert_security_headers(response):
    """Assert every header from issue #147 is present on a response."""
    for name, value in STATIC_SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, f"missing/incorrect {name}"
    assert response.headers.get("content-security-policy")


# ---------------------------------------------------------------------------
# Security headers (#147)
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    """Headers must be present on every response type the app produces."""

    def test_headers_on_html_response(self, client):
        """The HTML index carries all four headers."""
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert_security_headers(response)
        assert response.headers["content-security-policy"] == APP_CSP

    def test_headers_on_json_response(self, client):
        """A JSON response (404 from /progress) carries all four headers."""
        response = client.get("/progress/no-such-job")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert_security_headers(response)
        assert response.headers["content-security-policy"] == APP_CSP

    def test_headers_on_file_download_response(self, client):
        """A streamed FileResponse download carries all four headers."""
        os.makedirs(settings.processed_folder, exist_ok=True)
        file_path = os.path.join(settings.processed_folder, "headers_probe.xlf")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("<xliff></xliff>")
        try:
            response = client.get("/download/headers_probe.xlf")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/xml")
            assert_security_headers(response)
            assert response.headers["content-security-policy"] == APP_CSP
        finally:
            os.remove(file_path)

    def test_headers_on_static_assets(self, client):
        """The externalised CSS and JS are served and carry the headers."""
        for path, marker in (("/static/app.js", "uploadBtn"), ("/static/style.css", "#progressBar")):
            response = client.get(path)
            assert response.status_code == 200, path
            assert marker in response.text, path
            assert_security_headers(response)

    def test_headers_on_error_response(self, client):
        """A 400 still carries the headers."""
        response = client.get("/download/../../etc/passwd")
        assert response.status_code in (400, 404)
        assert_security_headers(response)


class TestContentSecurityPolicy:
    """The application policy must be strict; docs get a scoped exception."""

    def test_app_policy_has_no_unsafe_directives(self):
        """No 'unsafe-inline' or 'unsafe-eval' anywhere in the app policy."""
        assert "'unsafe-inline'" not in APP_CSP
        assert "'unsafe-eval'" not in APP_CSP

    def test_app_policy_locks_down_scripts_and_framing(self):
        """Scripts and styles come from 'self' only; framing is denied."""
        assert "script-src 'self'" in APP_CSP
        assert "style-src 'self'" in APP_CSP
        assert "object-src 'none'" in APP_CSP
        assert "base-uri 'none'" in APP_CSP
        assert "frame-ancestors 'none'" in APP_CSP
        assert "default-src 'self'" in APP_CSP

    def test_docs_policy_applies_only_to_docs_paths(self):
        """The relaxed policy is scoped to the documentation UIs."""
        for path in DOCS_PATHS:
            assert csp_for_path(path) == DOCS_CSP
        for path in ("/", "/upload", "/health", "/openapi.json", "/download/x.xlf"):
            assert csp_for_path(path) == APP_CSP

    def test_docs_page_served_with_relaxed_policy(self, client):
        """Swagger UI needs its CDN and inline bootstrap, and gets them."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == DOCS_CSP
        assert_security_headers(response)

    def test_openapi_schema_keeps_strict_policy(self, client):
        """The schema is app JSON, not a docs page."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == APP_CSP


class TestTemplateHasNoInlineCode:
    """
    Regression guard for the strict CSP.

    The policy has no 'unsafe-inline'; re-introducing an inline handler or a
    <script>/<style> block in the template would silently break the UI in a
    browser while every server-side test still passed.
    """

    @staticmethod
    def template_source() -> str:
        """The template with HTML comments stripped -- comments are not executed."""
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as handle:
            return re.sub(r"<!--.*?-->", "", handle.read(), flags=re.DOTALL)

    def test_no_inline_event_handlers(self):
        """No onclick=/onchange=/... attributes."""
        handlers = re.findall(r"\son[a-z]+\s*=", self.template_source())
        assert handlers == [], f"inline event handlers found: {handlers}"

    def test_no_inline_script_blocks(self):
        """Every <script> tag has a src; none carries a body."""
        script_tags = re.findall(r"<script\b[^>]*>", self.template_source())
        assert script_tags, "template should still load its behaviour"
        for tag in script_tags:
            assert "src=" in tag, f"inline script block found: {tag}"

    def test_no_inline_style_blocks_or_attributes(self):
        """Styles come from the stylesheet only."""
        source = self.template_source()
        assert "<style" not in source
        assert not re.findall(r"\sstyle\s*=", source)
        assert '<link rel="stylesheet" href="/static/style.css">' in source


class TestPanelsAreStillWired:
    """
    The panels the CSP must not break.

    :class:`TestTemplateHasNoInlineCode` only proves nothing inline is left --
    deleting the vocabulary panel and the history section outright would satisfy
    it just as well. These tests pin the opposite half: the controls are still
    in the markup, and their behaviour lives in ``static/app.js`` where
    ``script-src 'self'`` will actually run it.

    They cannot prove the page works -- only a browser can, because a CSP
    violation still returns 200. They are the cheap guard against the specific
    regression of a future change re-inlining a panel's script.
    """

    @staticmethod
    def app_js_source() -> str:
        with open(APP_JS_PATH, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def template_source() -> str:
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as handle:
            return handle.read()

    @pytest.mark.parametrize(
        "element_id",
        [
            # Vocabulary overrides (issue #142).
            "vocabPanel",
            "vocabRows",
            "addTermBtn",
            "vocabImport",
            "vocabError",
            # Recent Translations (issue #28).
            "historySection",
            "historyList",
            "historyRefresh",
            "historyRetention",
        ],
    )
    def test_control_present_in_template(self, element_id):
        """Each panel control the script binds to is still in the markup."""
        assert f'id="{element_id}"' in self.template_source()

    @pytest.mark.parametrize(
        "element_id",
        ["addTermBtn", "vocabImport", "vocabRows", "historyRefresh"],
    )
    def test_control_bound_with_add_event_listener(self, element_id):
        """Every panel control is wired from app.js, not from an attribute."""
        source = self.app_js_source()
        pattern = rf'getElementById\("{element_id}"\)\.addEventListener\('
        assert re.search(pattern, source), f"{element_id} has no listener in app.js"

    def test_delete_buttons_use_delegation(self):
        """
        Per-row delete buttons are created after load, so they are delegated.

        The original inline version assigned ``remove.onclick = ...`` on a
        freshly created element. That is a DOM property and would survive the
        CSP, but it re-binds on every render; the delegated listener on the
        tbody is bound once and covers rows that do not exist yet.
        """
        source = self.app_js_source()
        assert 'getElementById("vocabRows").addEventListener("click"' in source
        assert 'closest("button.vocab-delete")' in source
        assert ".onclick" not in source

    @pytest.mark.parametrize("selector", ["#vocabPanel", "#historySection"])
    def test_panel_styles_moved_to_stylesheet(self, client, selector):
        """The styles the panels shipped inline are served from /static."""
        response = client.get("/static/style.css")
        assert response.status_code == 200
        assert selector in response.text

    def test_panel_behaviour_served_under_script_src_self(self, client):
        """Both panels' code is reachable at a same-origin URL."""
        response = client.get("/static/app.js")
        assert response.status_code == 200
        assert "/api/glossary" in response.text
        assert "/api/history" in response.text


class TestSecurityHeadersMiddlewareUnit:
    """Direct exercise of the ASGI middleware."""

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        """Lifespan and websocket scopes are forwarded untouched."""
        seen = {}

        async def inner_app(scope, receive, send):
            seen["scope"] = scope

        wrapped = SecurityHeadersMiddleware(inner_app)
        await wrapped({"type": "lifespan"}, None, None)
        assert seen["scope"] == {"type": "lifespan"}


# ---------------------------------------------------------------------------
# Rate limiting (#146)
# ---------------------------------------------------------------------------


class TestUploadRateLimit:
    """The expensive endpoint is limited most tightly."""

    def test_upload_returns_429_with_retry_after_past_its_limit(self, client):
        """Requests up to the limit pass; the next one is a 429."""
        allowed = upload_limit_amount()
        for attempt in range(allowed):
            response = post_rejected_upload(client)
            assert response.status_code != 429, f"limited early on attempt {attempt + 1}"

        response = post_rejected_upload(client)
        assert response.status_code == 429
        assert "Rate limit exceeded" in response.json()["detail"]

        retry_after = response.headers.get("Retry-After")
        assert retry_after is not None, "429 must carry Retry-After"
        assert int(retry_after) >= 1

    def test_rate_limited_response_still_has_security_headers(self, client):
        """A 429 is produced inside the header middleware, so it is stamped too."""
        for _ in range(upload_limit_amount() + 1):
            response = post_rejected_upload(client)
        assert response.status_code == 429
        assert_security_headers(response)

    def test_upload_limit_is_tighter_than_the_default(self):
        """The whole point of a separate bucket for /upload."""
        assert upload_limit_amount() < default_limit_amount()

    def test_upload_uses_its_own_bucket(self, client):
        """Exhausting /upload does not lock out the read endpoints."""
        for _ in range(upload_limit_amount() + 1):
            post_rejected_upload(client)
        response = client.get("/progress/no-such-job")
        assert response.status_code == 404


class TestHealthExemption:
    """The Docker healthcheck polls /health every 30s and must never be throttled."""

    def test_health_is_exempt_from_the_rule_table(self):
        """/health resolves to no rule at all."""
        rate_limit_middleware = RateLimitMiddleware(
            None,
            rate_limiter=create_limiter(),
            default_limit="1/minute",
            upload_limit="1/minute",
        )
        assert rate_limit_middleware.rule_for("/health") is None
        assert rate_limit_middleware.rule_for("/upload") is not None
        assert rate_limit_middleware.rule_for("/progress/x") is not None

    def test_health_still_served_after_the_default_bucket_is_exhausted(self, client):
        """Saturate the shared limiter, then confirm /health is unaffected."""
        allowed = default_limit_amount()
        for _ in range(allowed + 1):
            client.get("/progress/no-such-job")
        assert client.get("/progress/no-such-job").status_code == 429

        for _ in range(5):
            response = client.get("/health")
            assert response.status_code != 429
            assert response.json()["status"] in ("healthy", "degraded")

    def test_health_survives_far_more_requests_than_the_limit(self, client):
        """Many consecutive healthchecks, none of them limited."""
        for _ in range(default_limit_amount() + 10):
            assert client.get("/health").status_code != 429


class TestDefaultRateLimit:
    """Read endpoints share a looser default bucket."""

    def test_progress_is_limited_at_the_default(self, client):
        """The default limit applies and the 429 carries Retry-After."""
        allowed = default_limit_amount()
        for _ in range(allowed):
            assert client.get("/progress/no-such-job").status_code == 404
        response = client.get("/progress/no-such-job")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1

    def test_default_leaves_room_for_one_hz_progress_polling(self):
        """The UI polls /progress once a second while a job runs."""
        assert default_limit_amount() > 60


class TestRateLimitToggle:
    """RATE_LIMIT_ENABLED is honoured per request."""

    def test_disabling_rate_limiting_lets_everything_through(self, client, monkeypatch):
        """No 429 at all when the feature is switched off."""
        monkeypatch.setattr(settings, "rate_limit_enabled", False)
        for _ in range(upload_limit_amount() + 5):
            assert post_rejected_upload(client).status_code != 429


class TestClientIdentifier:
    """Which address the limit is keyed on, and the proxy assumption."""

    @staticmethod
    def make_request(headers=None, client_host="10.0.0.1") -> Request:
        raw_headers = [
            (key.encode(), value.encode()) for key, value in (headers or {}).items()
        ]
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": raw_headers,
                "client": (client_host, 12345) if client_host else None,
            }
        )

    def test_defaults_to_the_peer_address(self, monkeypatch):
        """X-Forwarded-For is ignored unless explicitly trusted."""
        monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", False)
        request = self.make_request({"x-forwarded-for": "1.2.3.4"})
        assert client_identifier(request) == "10.0.0.1"

    def test_uses_first_forwarded_hop_when_trusted(self, monkeypatch):
        """Behind a trusted proxy the original client is used."""
        monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
        request = self.make_request({"x-forwarded-for": "1.2.3.4, 10.0.0.9"})
        assert client_identifier(request) == "1.2.3.4"

    def test_falls_back_when_forwarded_header_is_absent(self, monkeypatch):
        """Trusting the header does not require it to be present."""
        monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
        assert client_identifier(self.make_request()) == "10.0.0.1"

    def test_falls_back_when_forwarded_header_is_blank(self, monkeypatch):
        """An empty header value is not a usable key."""
        monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
        request = self.make_request({"x-forwarded-for": "  ,  "})
        assert client_identifier(request) == "10.0.0.1"

    def test_falls_back_when_there_is_no_client(self, monkeypatch):
        """slowapi's get_remote_address handles a missing peer."""
        monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", False)
        assert client_identifier(self.make_request(client_host=None)) == "127.0.0.1"


class TestRateLimitHelpers:
    """Response shape and the decorator-compatible exception handler."""

    def test_build_rate_limit_response(self):
        """429 with Retry-After and a JSON detail."""
        response = build_rate_limit_response(42, "5 per 1 minute")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "42"

    def test_retry_after_is_at_least_one_second(self):
        """Never advertise a zero or negative Retry-After."""
        limiter = create_limiter()
        item = parse_many("1/minute")[0]
        assert limiter.limiter.hit(item, "probe", "bucket") is True
        assert limiter.limiter.hit(item, "probe", "bucket") is False
        seconds = retry_after_seconds(limiter.limiter, item, "probe", "bucket")
        assert 1 <= seconds <= 60

    @pytest.mark.asyncio
    async def test_exception_handler_returns_429_with_retry_after(self):
        """A RateLimitExceeded from a future @limiter.limit route is handled."""
        item = parse_many("3/minute")[0]
        limit = Limit(item, lambda: "key", None, False, None, None, None, 1, False)
        response = await rate_limit_exceeded_handler(None, RateLimitExceeded(limit))
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        """Lifespan and websocket scopes are not counted."""
        seen = {}

        async def inner_app(scope, receive, send):
            seen["scope"] = scope

        wrapped = RateLimitMiddleware(
            inner_app,
            rate_limiter=create_limiter(),
            default_limit="1/minute",
            upload_limit="1/minute",
        )
        await wrapped({"type": "lifespan"}, None, None)
        assert seen["scope"] == {"type": "lifespan"}


class TestInstallMiddleware:
    """Wiring on a throwaway app, independent of app.py."""

    def test_installs_headers_limiter_and_handler(self, monkeypatch):
        """One call gives a fresh app both middlewares and the 429 handler."""
        monkeypatch.setattr(settings, "rate_limit_upload", "2/minute")
        monkeypatch.setattr(settings, "rate_limit_default", "50/minute")

        test_app = FastAPI()

        @test_app.post("/upload")
        async def _upload():
            return {"ok": True}

        @test_app.get("/health")
        async def _health():
            return {"ok": True}

        returned_limiter = install_middleware(test_app)
        assert returned_limiter is middleware.limiter
        assert test_app.state.limiter is middleware.limiter
        assert RateLimitExceeded in test_app.exception_handlers

        with TestClient(test_app) as test_client:
            assert test_client.post("/upload").status_code == 200
            assert test_client.post("/upload").status_code == 200
            limited = test_client.post("/upload")
            assert limited.status_code == 429
            assert int(limited.headers["Retry-After"]) >= 1
            assert_security_headers(limited)

            for _ in range(10):
                assert test_client.get("/health").status_code == 200
