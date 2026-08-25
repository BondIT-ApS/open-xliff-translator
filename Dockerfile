# =============================================================================
# Builder — compiles wheels, then is thrown away.
# =============================================================================
FROM python:3.14-slim AS builder

WORKDIR /build

# Toolchain needed to build any package without a prebuilt wheel. None of this
# reaches the runtime image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ make && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
RUN pip install --upgrade pip setuptools && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# =============================================================================
# Runtime — no compiler, no package manager.
# =============================================================================
FROM python:3.14-slim

WORKDIR /app

# curl is required by HEALTHCHECK below.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Dependencies only — the build toolchain stays in the builder stage.
COPY --from=builder /install /usr/local

# Drop pip from the runtime image.
#
# The application never installs anything at runtime, and shipping a package
# manager means shipping something that can fetch and execute arbitrary code.
# It also carried the only two Trivy findings the image had: pip's vendored
# dependency manifest declares msgpack==1.1.2 (GHSA-6v7p-g79w-8964, HIGH) and
# setuptools==70.3.0 (CVE-2025-47273, HIGH), neither of which application code
# can reach. Removing pip removes the manifest and the vendored copies with it.
#
# setuptools is not carried over either. python:3.14-slim does not bundle it,
# nothing in requirements.txt needs it, and the full test suite passes inside
# this image without it (292 passed).
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

COPY . .

# Create the non-root user and every volume-backed directory.
#
# `data` must be created here, BEFORE the chown. Docker seeds a fresh named
# volume from the image, so a directory missing from this list arrives
# root-owned while the app runs as appuser, and the container dies at startup
# with sqlite3.OperationalError: unable to open database file.
RUN groupadd -r appuser && useradd -r -g appuser appuser && \
    mkdir -p uploads processed data && \
    chown -R appuser:appuser /app

USER appuser

# Documentation only; the actual port comes from APP_PORT.
EXPOSE 5003

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT:-5003}/health || exit 1

# Exec form with an explicit `exec` so uvicorn REPLACES the shell and becomes
# PID 1. Plain shell form left /bin/sh as PID 1 with uvicorn as its child, and
# sh does not forward signals -- so `docker stop` never reached uvicorn and the
# FastAPI lifespan shutdown never ran: the SQLite connection was killed rather
# than closed, the httpx client was never released, and the cleanup task was
# never cancelled. The `sh -c` wrapper is still needed to expand APP_PORT.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${APP_PORT:-5003}"]
