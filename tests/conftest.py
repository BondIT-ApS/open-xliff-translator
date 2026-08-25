"""
Shared pytest fixtures.

Rate limiting (issue #146) is enabled by default, and the limiter counts hits in
a process-wide store keyed by client address. Every test uses the same
TestClient address, so without this fixture the limits would leak between tests
and the suite would start returning 429 as more endpoint tests are added.
"""
import pytest

import middleware


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear recorded rate-limit hits before and after every test."""
    middleware.reset_rate_limits()
    yield
    middleware.reset_rate_limits()
