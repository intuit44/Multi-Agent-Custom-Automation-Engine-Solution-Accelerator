import base64
import json
import logging
import os
import time


def _extract_bearer_token(auth_header: str) -> str | None:
    """Extract token from 'Bearer <token>' header value."""
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return None


_DEV_TOKEN_CACHE: dict = {"token": None, "expires_on": 0}
_DEV_TOKEN_SCOPE = (
    "api://ea9ffc3e-8a23-4a7d-836d-234d7c7565c1/McpServers.CopilotMCP.All"
)
_DEV_CLIENT_ID = "ee7ae9f0-67c2-4370-9a9f-1d497a506140"  # macae-v4-auth
_DEV_TENANT_ID = "978d9cc6-784c-4c98-8d90-a4a6344a65ff"


def _dev_acquire_user_token() -> str | None:
    """Dev-only: acquire a delegated AAD token for OBO flow.

    Uses DeviceCodeCredential with persistent token cache.
    First run: prompts device code login once.
    Subsequent runs: uses cached refresh token automatically — no re-auth needed
    until the refresh token is revoked (~90 days).
    """
    if os.environ.get("APP_ENV", "prod").lower() not in ("dev", "development", "local"):
        return None

    env_token = os.environ.get("MACAE_DEV_OBO_TOKEN")
    if env_token:
        logging.info("Dev OBO: using token from MACAE_DEV_OBO_TOKEN env var")
        return env_token

    now = int(time.time())
    if _DEV_TOKEN_CACHE["token"] and _DEV_TOKEN_CACHE["expires_on"] - 300 > now:
        return _DEV_TOKEN_CACHE["token"]

    try:
        from azure.identity import DeviceCodeCredential, TokenCachePersistenceOptions

        credential = DeviceCodeCredential(
            client_id=_DEV_CLIENT_ID,
            tenant_id=_DEV_TENANT_ID,
            cache_persistence_options=TokenCachePersistenceOptions(
                name="macae-dev-obo",
                allow_unencrypted_storage=True,
            ),
        )
        access = credential.get_token(_DEV_TOKEN_SCOPE)
        _DEV_TOKEN_CACHE["token"] = access.token
        _DEV_TOKEN_CACHE["expires_on"] = access.expires_on
        logging.info("Dev OBO: token acquired (cached to disk, refresh ~90 days)")
        return access.token
    except Exception as exc:
        logging.info("Dev OBO auto-acquire skipped (%s)", exc)
        return None


def get_authenticated_user_details(request_headers):
    user_object = {}

    # check the headers for the Principal-Id (the guid of the signed in user)
    if "x-ms-client-principal-id" not in request_headers:
        # In production, missing auth headers means misconfigured EasyAuth
        import os

        app_env = os.environ.get("APP_ENV", "dev").lower()
        if app_env not in ("dev", "development", "local"):
            logging.warning(
                "SECURITY: Request without EasyAuth headers in %s environment",
                app_env,
            )
            raise PermissionError(
                "Authentication required. No EasyAuth principal found in headers."
            )

        logging.info(
            "No user principal found in headers — using sample_user (dev mode)"
        )
        from . import sample_user

        raw_user_object = sample_user.sample_user
    else:
        # if it is, get the user details from the EasyAuth headers
        raw_user_object = {k: v for k, v in request_headers.items()}

    normalized_headers = {k.lower(): v for k, v in raw_user_object.items()}
    user_object["user_principal_id"] = normalized_headers.get(
        "x-ms-client-principal-id"
    )
    user_object["user_name"] = normalized_headers.get("x-ms-client-principal-name")
    # Dev fallback: ensure user_name is never None (prevents Cosmos added_by=None)
    if not user_object["user_name"]:
        user_object["user_name"] = "dev-user@local"
    user_object["auth_provider"] = normalized_headers.get("x-ms-client-principal-idp")
    user_object["auth_token"] = normalized_headers.get("x-ms-token-aad-id-token")
    user_object["access_token"] = normalized_headers.get(
        "x-ms-token-aad-access-token"
    ) or _extract_bearer_token(normalized_headers.get("authorization", ""))
    _tok = user_object["access_token"]
    if not _tok:
        _tok = _dev_acquire_user_token()
        user_object["access_token"] = _tok
    if _tok:
        logging.info(
            "OBO token present: %s...%s (len=%d)", _tok[:20], _tok[-10:], len(_tok)
        )
    else:
        logging.info(
            "OBO token: None (no Bearer header, EasyAuth token, or dev CLI token)"
        )
    user_object["client_principal_b64"] = normalized_headers.get(
        "x-ms-client-principal"
    )
    user_object["aad_id_token"] = normalized_headers.get("x-ms-token-aad-id-token")

    # Extract tenant_id from the base64-encoded client principal
    user_object["tenant_id"] = (
        get_tenantid(user_object.get("client_principal_b64")) or ""
    )

    return user_object


def get_tenantid(client_principal_b64):
    logger = logging.getLogger(__name__)
    tenant_id = ""
    if client_principal_b64:
        try:
            # Decode the base64 header to get the JSON string
            decoded_bytes = base64.b64decode(client_principal_b64)
            decoded_string = decoded_bytes.decode("utf-8")
            # Convert the JSON string1into a Python dictionary
            user_info = json.loads(decoded_string)
            # Extract the tenant ID
            tenant_id = user_info.get("tid")  # 'tid' typically holds the tenant ID
        except Exception as ex:
            logger.exception(ex)
    return tenant_id
