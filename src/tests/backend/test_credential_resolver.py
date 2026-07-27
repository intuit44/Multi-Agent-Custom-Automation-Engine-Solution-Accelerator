"""Unit tests for the backend CredentialResolver (Key Vault-backed).

Covers the user-connection credential lane used by the MCP connect flows in
v4/api/router.py: secret-ref resolution (URI and bare name), the
project-{id}-{provider} naming convention, JSON vs plain-token secret values,
caching semantics, store/invalidate, and client lifecycle. All Key Vault I/O
is mocked — no live Azure.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

src_backend = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src", "backend"
)
if src_backend not in sys.path:
    sys.path.insert(0, os.path.abspath(src_backend))

import credential_resolver as cr_module
from credential_resolver import CredentialResolver


def _secret(value, sid="https://kv.vault.azure.net/secrets/x/1"):
    return SimpleNamespace(value=value, id=sid)


@pytest.fixture
def resolver():
    r = CredentialResolver()
    r._kv_client = AsyncMock()
    return r


# ---------------------------------------------------------------- secret_ref


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_empty_returns_none(resolver):
    assert await resolver.resolve_by_secret_ref("") is None
    resolver._kv_client.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_cache_hit_skips_kv(resolver):
    resolver._cache["ref-1"] = {"token": "cached"}
    assert await resolver.resolve_by_secret_ref("ref-1") == {"token": "cached"}
    resolver._kv_client.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_full_uri_extracts_secret_name(resolver):
    resolver._kv_client.get_secret.return_value = _secret('{"k": "v"}')
    ref = "https://kv.vault.azure.net/secrets/my-secret/abc123version"
    assert await resolver.resolve_by_secret_ref(ref) == {"k": "v"}
    resolver._kv_client.get_secret.assert_awaited_once_with("my-secret")


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_bare_name_passthrough(resolver):
    resolver._kv_client.get_secret.return_value = _secret('{"k": "v"}')
    await resolver.resolve_by_secret_ref("project-u1-infobip")
    resolver._kv_client.get_secret.assert_awaited_once_with("project-u1-infobip")


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_plain_token_wrapped(resolver):
    resolver._kv_client.get_secret.return_value = _secret("App abc-123")
    creds = await resolver.resolve_by_secret_ref("ref-token")
    assert creds == {"token": "App abc-123"}
    # and it was cached under the original ref
    assert resolver._cache["ref-token"] == {"token": "App abc-123"}


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_none_value_returns_none(resolver):
    resolver._kv_client.get_secret.return_value = _secret(None)
    assert await resolver.resolve_by_secret_ref("ref-x") is None
    assert "ref-x" not in resolver._cache


@pytest.mark.asyncio
async def test_resolve_by_secret_ref_kv_error_returns_none(resolver):
    resolver._kv_client.get_secret.side_effect = RuntimeError("kv down")
    assert await resolver.resolve_by_secret_ref("ref-y") is None


# --------------------------------------------------------- project/provider


@pytest.mark.asyncio
async def test_resolve_credentials_secret_name_convention(resolver):
    resolver._kv_client.get_secret.return_value = _secret('{"a": 1}')
    creds = await resolver.resolve_credentials("user_1", "my_provider")
    assert creds == {"a": 1}
    # underscores become hyphens: project-{project_id}-{provider_id}
    resolver._kv_client.get_secret.assert_awaited_once_with(
        "project-user-1-my-provider"
    )


@pytest.mark.asyncio
async def test_resolve_credentials_second_call_uses_cache(resolver):
    resolver._kv_client.get_secret.return_value = _secret('{"a": 1}')
    await resolver.resolve_credentials("p", "prov")
    await resolver.resolve_credentials("p", "prov")
    assert resolver._kv_client.get_secret.await_count == 1


@pytest.mark.asyncio
async def test_resolve_credentials_none_value_returns_none(resolver):
    resolver._kv_client.get_secret.return_value = _secret(None)
    assert await resolver.resolve_credentials("p", "prov") is None


@pytest.mark.asyncio
async def test_resolve_credentials_non_json_returns_none(resolver):
    # resolve_credentials (unlike secret_ref) requires JSON — plain string fails
    resolver._kv_client.get_secret.return_value = _secret("not-json")
    assert await resolver.resolve_credentials("p", "prov") is None


@pytest.mark.asyncio
async def test_resolve_credentials_kv_error_returns_none(resolver):
    resolver._kv_client.get_secret.side_effect = RuntimeError("boom")
    assert await resolver.resolve_credentials("p", "prov") is None


# ------------------------------------------------------------------- store


@pytest.mark.asyncio
async def test_store_credentials_sets_json_and_invalidates_cache(resolver):
    resolver._cache["p:prov"] = {"stale": "yes"}
    resolver._kv_client.set_secret.return_value = _secret(
        "x", sid="https://kv.vault.azure.net/secrets/project-p-prov/v2"
    )
    uri = await resolver.store_credentials("p", "prov", {"token": "t1"})
    assert uri.endswith("/secrets/project-p-prov/v2")
    resolver._kv_client.set_secret.assert_awaited_once_with(
        "project-p-prov", json.dumps({"token": "t1"})
    )
    assert "p:prov" not in resolver._cache


@pytest.mark.asyncio
async def test_store_credentials_none_id_raises(resolver):
    resolver._kv_client.set_secret.return_value = _secret("x", sid=None)
    with pytest.raises(RuntimeError):
        await resolver.store_credentials("p", "prov", {"token": "t"})


@pytest.mark.asyncio
async def test_store_credentials_kv_error_reraises(resolver):
    resolver._kv_client.set_secret.side_effect = RuntimeError("kv write denied")
    with pytest.raises(RuntimeError):
        await resolver.store_credentials("p", "prov", {"token": "t"})


# --------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_close_closes_client_and_resets(resolver):
    client = resolver._kv_client
    await resolver.close()
    client.close.assert_awaited_once()
    assert resolver._kv_client is None


@pytest.mark.asyncio
async def test_close_swallows_errors_but_resets(resolver):
    resolver._kv_client.close.side_effect = RuntimeError("already closed")
    await resolver.close()
    assert resolver._kv_client is None


@pytest.mark.asyncio
async def test_close_noop_without_client():
    r = CredentialResolver()
    await r.close()  # must not raise
    assert r._kv_client is None


@pytest.mark.asyncio
async def test_initialize_swallows_client_errors(resolver):
    with patch.object(
        CredentialResolver, "_get_keyvault_client", side_effect=ValueError("no url")
    ):
        await resolver.initialize()  # must not raise


def test_lazy_client_built_once_and_reused():
    r = CredentialResolver()
    fake_client = MagicMock()
    with (
        patch.object(cr_module, "SecretClient", return_value=fake_client) as ctor,
        patch.object(
            cr_module.config, "_get_optional", return_value="https://kv.example.net/"
        ),
        patch.object(cr_module.config, "get_azure_credential_async"),
    ):
        assert r._get_keyvault_client() is fake_client
        assert r._get_keyvault_client() is fake_client
        ctor.assert_called_once()


@pytest.mark.asyncio
async def test_missing_kv_url_yields_none_not_crash():
    r = CredentialResolver()
    with patch.object(cr_module.config, "_get_optional", return_value=""):
        # _get_keyvault_client raises ValueError inside -> swallowed -> None
        assert await r.resolve_by_secret_ref("some-ref") is None
