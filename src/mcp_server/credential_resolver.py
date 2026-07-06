"""Self-contained credential resolution for the MCP server (Key Vault).

This is a decoupled twin of ``src/backend/credential_resolver.py``. The backend
version imports ``common.config.app_config`` (the whole backend config), which is
NOT available in the mcp_server container — so importing the backend module here
fails with ``ModuleNotFoundError`` and Key Vault resolution silently degrades to
``None`` (leaving ``connect_mcp_server`` unable to resolve a registered server's
secret, which forced passing tokens by hand).

This module has NO backend dependency: it reads ``AZURE_KEY_VAULT_URL`` and
``AZURE_CLIENT_ID`` from the environment and uses ``azure-identity`` +
``azure-keyvault-secrets`` (both already in this package's pyproject). It exposes
the same ``resolve_by_secret_ref`` interface and a module-level singleton
``credential_resolver`` so ``from credential_resolver import credential_resolver``
resolves to THIS file inside the mcp_server.

Prerequisites for the mcp_server's managed identity (``AZURE_CLIENT_ID`` / ``CLIENT_ID``):
- **Key Vault Secrets User** — read secrets (static_secret, oauth_refresh read).
- **Key Vault Secrets Officer** — additionally required for ``oauth_refresh``, which
  writes the rotated access/refresh tokens back to the vault.
- A role on the target resource (e.g. **Grafana Viewer**) for ``managed_identity``,
  which mints a fresh AAD token per call and stores nothing.
"""

import json
import logging
import os
import time
from typing import Dict, Optional

from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

logger = logging.getLogger(__name__)

# Fallback to the MACAE project vault. Prefer setting AZURE_KEY_VAULT_URL explicitly;
# the previous hardcoded default pointed at a DIFFERENT project's vault
# (yellowstkeyvault8df0efc3 in boat-rental-app-group), which silently stored/read
# MACAE secrets in the wrong vault.
_DEFAULT_KV_URL = "https://kv-pslc25991vme66zmins.vault.azure.net/"


class CredentialResolver:
    """Resolves credentials from Key Vault at runtime (env-configured, no backend)."""

    def __init__(self) -> None:
        self._kv_client: Optional[SecretClient] = None
        self._cache: Dict[str, Dict[str, str]] = {}

    async def initialize(self) -> None:
        """Pre-warm the Key Vault client during app startup (best-effort)."""
        try:
            _ = self._get_keyvault_client()
            logger.info("CredentialResolver initialized - Key Vault client ready")
        except Exception as exc:  # noqa: BLE001 - startup must not crash
            logger.warning("CredentialResolver initialization skipped: %s", exc)

    def _get_keyvault_client(self) -> SecretClient:
        """Lazy Key Vault client using the mcp_server's own managed identity."""
        if self._kv_client is None:
            kv_url = os.environ.get("AZURE_KEY_VAULT_URL", _DEFAULT_KV_URL)
            if not kv_url:
                raise ValueError("AZURE_KEY_VAULT_URL not configured")

            # DefaultAzureCredential covers both prod (user-assigned MI via
            # AZURE_CLIENT_ID) and local dev (az login), same as the backend.
            client_id = (
                os.environ.get("AZURE_CLIENT_ID") or os.environ.get("CLIENT_ID") or None
            )
            credential = (
                DefaultAzureCredential(managed_identity_client_id=client_id)
                if client_id
                else DefaultAzureCredential()
            )
            self._kv_client = SecretClient(vault_url=kv_url, credential=credential)
        return self._kv_client

    @staticmethod
    def _secret_name_from_ref(secret_ref: str) -> str:
        """Extract the bare secret name from a full KV URI or return it as-is.

        Accepts ``https://<vault>/secrets/<name>[/<version>]`` or a bare name.
        """
        if secret_ref.startswith("https://"):
            parts = secret_ref.rstrip("/").split("/secrets/")
            return parts[-1].split("/")[0]
        return secret_ref

    async def resolve_by_secret_ref(self, secret_ref: str) -> Optional[Dict[str, str]]:
        """Resolve credentials from a Key Vault secret URI or bare secret name.

        Returns a dict of credential key-value pairs, or None if not found. A
        plain-string secret value is wrapped as ``{"token": value}``.
        """
        if not secret_ref:
            return None
        if secret_ref in self._cache:
            return self._cache[secret_ref]
        try:
            kv_client = self._get_keyvault_client()
            secret_name = self._secret_name_from_ref(secret_ref)
            secret = await kv_client.get_secret(secret_name)
            if secret.value is None:
                logger.warning("Secret '%s' has no value", secret_name)
                return None

            try:
                credentials = json.loads(secret.value)
            except json.JSONDecodeError:
                credentials = {"token": secret.value}

            self._cache[secret_ref] = credentials
            logger.info("Resolved credentials via secret_ref")
            return credentials
        except Exception as e:  # noqa: BLE001 - never surface KV internals
            logger.warning("Failed to resolve secret_ref: %s", type(e).__name__)
            return None

    async def resolve_valid_token(
        self,
        *,
        credential_source: str = "static_secret",
        audience: Optional[str] = None,
        secret_ref: Optional[str] = None,
    ) -> Optional[str]:
        """Return a VALID bearer token for a server, per its ``credential_source``.

        This is the single dispatch point that the connect tools call. It hides HOW
        a token is obtained behind the source strategy:

        - ``managed_identity`` — mint a FRESH AAD token for ``audience`` from the
          platform's Managed Identity. Nothing is stored; never expires on us.
        - ``static_secret``    — read the fixed token from Key Vault via ``secret_ref``.
        - ``oauth_refresh``    — return a valid access_token, refreshing it with the
          stored refresh_token and writing the rotation back to Key Vault when it
          nears expiry (see ``_resolve_oauth_refresh``).
        """
        source = (credential_source or "static_secret").lower()

        if source == "managed_identity":
            return await self._mint_managed_identity_token(audience)

        if not secret_ref:
            return None
        creds = await self.resolve_by_secret_ref(secret_ref)
        if not creds:
            return None

        if source == "oauth_refresh":
            return await self._resolve_oauth_refresh(secret_ref, creds)

        # static_secret (and any unknown source) — return the stored token as-is.
        return (
            creds.get("access_token")
            or creds.get("token")
            or creds.get("api_key")
            or next(iter(creds.values()), None)
        )

    async def _mint_managed_identity_token(
        self, audience: Optional[str]
    ) -> Optional[str]:
        """Mint a fresh AAD token for ``audience`` using the platform's Managed Identity.

        Requires the MI (AZURE_CLIENT_ID / CLIENT_ID) to hold a role on the target
        resource (e.g. Grafana Viewer). Returns None if no audience is configured.
        """
        if not audience:
            logger.warning(
                "managed_identity credential_source has no audience configured"
            )
            return None
        client_id = (
            os.environ.get("AZURE_CLIENT_ID") or os.environ.get("CLIENT_ID") or None
        )
        credential = (
            DefaultAzureCredential(managed_identity_client_id=client_id)
            if client_id
            else DefaultAzureCredential()
        )
        try:
            token = await credential.get_token(audience)
            logger.info("Minted managed-identity token (audience=%s)", audience)
            return token.token
        except Exception as e:  # noqa: BLE001 - never surface credential internals
            logger.error("Failed to mint managed-identity token: %s", type(e).__name__)
            return None
        finally:
            await credential.close()

    async def _resolve_oauth_refresh(
        self, secret_ref: str, creds: Dict[str, str]
    ) -> Optional[str]:
        """Return a valid OAuth access_token, refreshing + writing back if expired.

        The Key Vault blob is expected to hold:
          {access_token, refresh_token, expires_at (epoch secs), token_endpoint,
           client_id, [client_secret], [scopes]}

        If the access_token is still valid (>120s of life), it's returned as-is.
        Otherwise we exchange the refresh_token at token_endpoint, persist the
        rotated tokens back to Key Vault (providers rotate refresh_token, so
        WITHOUT write-back the next refresh breaks), and return the new token.
        On any failure we fall back to the stored access_token.
        """
        access_token = creds.get("access_token")
        expires_at = creds.get("expires_at")

        # Still valid? Keep a 120s safety margin.
        try:
            if access_token and expires_at and float(expires_at) - time.time() > 120:
                return access_token
        except (TypeError, ValueError):
            pass

        refresh_token = creds.get("refresh_token")
        token_endpoint = creds.get("token_endpoint")
        client_id = creds.get("client_id")
        if not (refresh_token and token_endpoint and client_id):
            logger.warning(
                "oauth_refresh: missing refresh_token/token_endpoint/client_id — "
                "returning stored access_token (may be expired)"
            )
            return access_token

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        client_secret = creds.get("client_secret")
        if client_secret:
            data["client_secret"] = client_secret
        scopes = creds.get("scopes")
        if scopes:
            data["scope"] = scopes if isinstance(scopes, str) else " ".join(scopes)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    token_endpoint,
                    data=data,
                    headers={"Accept": "application/json"},
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:  # noqa: BLE001 - fall back to stored token on any error
            logger.error("oauth_refresh: token refresh failed: %s", type(e).__name__)
            return access_token

        new_access = payload.get("access_token")
        if not new_access:
            logger.error("oauth_refresh: refresh response had no access_token")
            return access_token

        # Merge the rotation back into the blob and persist it.
        updated = dict(creds)
        updated["access_token"] = new_access
        if payload.get("refresh_token"):
            # Provider rotated the refresh_token — MUST persist or the next refresh fails.
            updated["refresh_token"] = payload["refresh_token"]
        expires_in = payload.get("expires_in")
        if expires_in:
            try:
                updated["expires_at"] = str(int(time.time()) + int(expires_in))
            except (TypeError, ValueError):
                pass

        await self._write_back(secret_ref, updated)
        logger.info("oauth_refresh: refreshed access_token and wrote back to Key Vault")
        return new_access

    async def _write_back(self, secret_ref: str, blob: Dict[str, str]) -> None:
        """Persist an updated credential blob back to Key Vault (needs write access).

        Requires the MI to hold **Key Vault Secrets Officer** (Secrets User is
        read-only). The in-process cache is refreshed regardless, so a failed
        write-back doesn't cause a refresh storm within the process lifetime.
        """
        try:
            kv_client = self._get_keyvault_client()
            secret_name = self._secret_name_from_ref(secret_ref)
            await kv_client.set_secret(secret_name, json.dumps(blob))
            logger.info("oauth_refresh: rotated token persisted to Key Vault")
        except Exception as e:  # noqa: BLE001 - never surface KV internals
            logger.error(
                "oauth_refresh: write-back failed (MI needs Key Vault Secrets "
                "Officer?): %s",
                type(e).__name__,
            )
        finally:
            self._cache[secret_ref] = blob


# Module-level singleton — matches ``from credential_resolver import credential_resolver``.
credential_resolver = CredentialResolver()
