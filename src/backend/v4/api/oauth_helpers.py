"""OAuth2 flow helpers for MCP connections.

Provides:
- Signed `state` token generation/verification (HMAC-SHA256)
- Authorize URL construction
- Code-for-token exchange
- Redirect URI builder
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import List, Optional, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600


def _get_state_secret() -> str:
    secret = os.environ.get("OAUTH_STATE_SECRET")
    if not secret:
        secret = (
            os.environ.get("AZURE_CLIENT_SECRET")
            or os.environ.get("APP_SECRET_KEY")
            or "macae-oauth-state-fallback"
        )
    return secret


def sign_state(user_id: str, server_name: str) -> str:
    """Sign a short-lived state token binding user_id + server_name."""
    payload = json.dumps(
        {"u": user_id, "s": server_name, "e": int(time.time()) + _STATE_TTL_SECONDS},
        separators=(",", ":"),
    )
    sig = hmac.new(
        _get_state_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    raw = f"{payload}|{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_state(state: str) -> Tuple[str, str]:
    """Verify a state token and return (user_id, server_name).

    Raises ValueError on invalid signature or expiration.
    """
    padded = state + "=" * (-len(state) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception as exc:
        raise ValueError(f"Malformed state token: {exc}") from exc

    if "|" not in decoded:
        raise ValueError("Malformed state token: missing signature")

    payload, sig = decoded.rsplit("|", 1)
    expected = hmac.new(
        _get_state_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid state signature")

    data = json.loads(payload)
    if data.get("e", 0) < int(time.time()):
        raise ValueError("State token expired")

    return data["u"], data["s"]


def build_redirect_uri() -> str:
    """Build the OAuth callback URL from BACKEND_BASE_URL env var."""
    base = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/api/v4/mcp/connections/oauth/callback"


def build_authorize_url(
    authorize_url: str,
    client_id: str,
    scopes: List[str],
    state: str,
    redirect_uri: Optional[str] = None,
) -> str:
    """Construct the provider authorize URL with required query params."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri or build_redirect_uri(),
        "scope": " ".join(scopes) if scopes else "",
        "state": state,
        "response_type": "code",
    }
    return f"{authorize_url}?{urlencode(params)}"


async def exchange_code_for_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: Optional[str] = None,
) -> dict:
    """Exchange an authorization code for an access token."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri or build_redirect_uri(),
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()
