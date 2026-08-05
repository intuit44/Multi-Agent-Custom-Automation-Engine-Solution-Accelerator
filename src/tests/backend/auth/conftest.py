"""Reset global mutable state in auth_utils between tests."""
import pytest


@pytest.fixture(autouse=True)
def _reset_dev_token_cache():
    """Reset _DEV_TOKEN_CACHE before each test to prevent contamination."""
    from backend.auth.auth_utils import _DEV_TOKEN_CACHE

    original = dict(_DEV_TOKEN_CACHE)
    _DEV_TOKEN_CACHE["token"] = None
    _DEV_TOKEN_CACHE["expires_on"] = 0
    yield
    _DEV_TOKEN_CACHE.update(original)


@pytest.fixture(autouse=True)
def _disable_dev_obo(monkeypatch):
    """Disable _dev_acquire_user_token in tests to prevent real auth calls."""
    monkeypatch.setattr(
        "backend.auth.auth_utils._dev_acquire_user_token", lambda: None
    )
