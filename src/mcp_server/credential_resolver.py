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

Prerequisite: the mcp_server's managed identity (``AZURE_CLIENT_ID``) needs the
**Key Vault Secrets User** role on the vault so it can read the secrets.
"""

import json
import logging
import os
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
            client_id = os.environ.get("AZURE_CLIENT_ID") or None
            credential = (
                DefaultAzureCredential(managed_identity_client_id=client_id)
                if client_id
                else DefaultAzureCredential()
            )
            self._kv_client = SecretClient(vault_url=kv_url, credential=credential)
        return self._kv_client

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

            # Accept full URI (…/secrets/<name>[/<version>]) or bare secret name.
            if secret_ref.startswith("https://"):
                parts = secret_ref.rstrip("/").split("/secrets/")
                secret_name = parts[-1].split("/")[0]
            else:
                secret_name = secret_ref

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


# Module-level singleton — matches ``from credential_resolver import credential_resolver``.
credential_resolver = CredentialResolver()
