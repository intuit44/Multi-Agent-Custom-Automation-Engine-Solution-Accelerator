"""Typed classification, retry and user-messaging for tool/agent failures.

Replaces the generic "I'm having trouble connecting to my tools" fallback with
actionable, category-specific handling:

  * TRANSIENT (429/500/503/timeouts)  -> retried with exponential back-off + jitter
  * AUTH_CONSENT (AADSTS65001, expired)-> surface the consent flow / ask to re-auth
  * PERMISSION (insufficient/admin)    -> clear "admin must approve" message
  * CONNECTIVITY (network/DNS/TLS)     -> "couldn't reach the service" message
  * UNKNOWN                            -> safe generic, still non-offensive

Every failure is emitted to telemetry (``Tool_Error``) so latency/error-rate and
HTTP/AADSTS codes can be monitored and alerted on in Application Insights.

This module is deliberately provider-agnostic: it reads status codes, AADSTS
codes and exception type names from whatever the underlying SDK raises
(azure.core ``HttpResponseError``/``ClientAuthenticationError``, Graph
``ODataError``/``ServiceException``, aiohttp/httpx connection errors, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, TypeVar

from common.utils.event_utils import track_event_if_configured

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ToolErrorCategory(str, Enum):
    """Coarse buckets that map 1:1 to a recovery strategy and a user message."""

    TRANSIENT = "transient"  # retry with back-off
    AUTH_CONSENT = "auth_consent"  # missing consent / expired token -> consent flow
    PERMISSION = "permission"  # insufficient privileges -> admin must grant
    CONNECTIVITY = "connectivity"  # network / DNS / TLS / firewall
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolError:
    category: ToolErrorCategory
    status_code: Optional[int]
    retryable: bool
    consent_required: bool
    aadsts: Optional[str]
    detail: str  # internal only — for logs/telemetry, never shown to the user


# Status codes worth an automatic retry (transient by definition).
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
# AADSTS buckets (https://learn.microsoft.com/azure/active-directory/develop/reference-error-codes)
_AADSTS_CONSENT = {"65001", "70011"}  # user/app has not consented
_AADSTS_EXPIRED = {"500133", "700082", "50173", "50196"}  # token expired/stale
_AADSTS_PERMISSION = {"50105", "65004", "90094", "650053"}  # insufficient/admin

# Exception TYPE names (across SDKs) that map directly to a category — matched on
# the class, never on the message text.
_TRANSIENT_TYPES = {
    "RateLimitError", "InternalServerError", "APITimeoutError",
    "APIConnectionError", "ServiceResponseError", "TimeoutError",
    "ServerError",
}
_CONNECTIVITY_TYPES = {
    "ServiceRequestError", "ServerTimeoutError", "ClientConnectionError",
    "ClientConnectorError", "ClientOSError", "ConnectTimeout", "ReadTimeout",
    "ConnectError", "ConnectionError", "ConnectionResetError",
}
_AUTH_TYPES = {"ClientAuthenticationError", "AuthenticationError"}
_PERMISSION_TYPES = {"PermissionDeniedError"}
# Structured error codes (from exc.code / exc.type / exc.error.code) — attribute
# values returned by the SDK, not free-text parsed from the message.
_TRANSIENT_CODES = {
    "rate_limit_exceeded", "server_error", "service_unavailable",
    "timeout", "503", "500", "429",
}


def _chain(exc: BaseException):
    """Yield exc and every linked cause/context, so a typed root exception is
    found even when an outer layer wrapped it in a plain Exception."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _obj_status(exc: BaseException) -> Optional[int]:
    """HTTP status read from the EXCEPTION OBJECT (or its .response), not a regex."""
    for obj in (exc, getattr(exc, "response", None)):
        if obj is None:
            continue
        for attr in ("status_code", "status"):
            v = getattr(obj, attr, None)
            if isinstance(v, int) and 100 <= v <= 599:
                return v
    return None


def _obj_retryable(exc: BaseException) -> Optional[bool]:
    """Honor an explicit SDK retry signal if the exception exposes one."""
    for attr in ("retryable", "should_retry", "is_retryable"):
        v = getattr(exc, attr, None)
        if isinstance(v, bool):
            return v
    return None


def _obj_error_code(exc: BaseException) -> Optional[str]:
    """Structured error code from attributes: exc.error.code / exc.code / exc.type."""
    err = getattr(exc, "error", None)
    for src in (
        getattr(err, "code", None),
        getattr(exc, "code", None),
        getattr(exc, "type", None),
    ):
        if isinstance(src, str) and src:
            return src.lower()
    return None


def _aad_codes(exc: BaseException) -> list[str]:
    """AAD numeric codes from structured attributes (MSAL/azure-identity expose
    ``error_codes``); no regex over the message."""
    codes = getattr(exc, "error_codes", None)
    if isinstance(codes, (list, tuple)):
        return [str(c) for c in codes]
    err = getattr(exc, "error", None)
    if isinstance(err, dict) and isinstance(err.get("error_codes"), (list, tuple)):
        return [str(c) for c in err["error_codes"]]
    return []


def classify_tool_error(exc: BaseException) -> ToolError:
    """Classify a tool/agent failure by INSPECTING THE EXCEPTION OBJECT — HTTP
    status, an explicit ``retryable`` flag, the structured error code, the
    exception type, and AAD ``error_codes`` — walking the cause chain so a wrapped
    typed exception is still found. The human-readable message is never parsed to
    decide the category (it is kept only as ``detail`` for display/telemetry)."""
    status: Optional[int] = None
    retryable_flag: Optional[bool] = None
    error_code: Optional[str] = None
    aad: list[str] = []
    types: set[str] = set()
    for e in _chain(exc):
        types.add(type(e).__name__)
        status = status or _obj_status(e)
        if retryable_flag is None:
            retryable_flag = _obj_retryable(e)
        error_code = error_code or _obj_error_code(e)
        aad = aad or _aad_codes(e)

    detail = str(exc) or type(exc).__name__
    aadsts = aad[0] if aad else None

    # 1) Structured AAD codes decide consent vs expiry vs permission.
    if any(c in _AADSTS_CONSENT for c in aad):
        return ToolError(ToolErrorCategory.AUTH_CONSENT, status, False, True, aadsts, detail)
    if any(c in _AADSTS_EXPIRED for c in aad):
        return ToolError(ToolErrorCategory.AUTH_CONSENT, status, True, False, aadsts, detail)
    if any(c in _AADSTS_PERMISSION for c in aad):
        return ToolError(ToolErrorCategory.PERMISSION, status, False, False, aadsts, detail)

    # 2) An explicit SDK retry flag is the strongest non-auth signal.
    if retryable_flag is True:
        return ToolError(ToolErrorCategory.TRANSIENT, status, True, False, aadsts, detail)

    # 3) Exception TYPE → category.
    if types & _AUTH_TYPES:
        cat = ToolErrorCategory.PERMISSION if status == 403 else ToolErrorCategory.AUTH_CONSENT
        return ToolError(cat, status, False, False, aadsts, detail)
    if types & _PERMISSION_TYPES:
        return ToolError(ToolErrorCategory.PERMISSION, status, False, False, aadsts, detail)
    if types & _CONNECTIVITY_TYPES:
        return ToolError(ToolErrorCategory.CONNECTIVITY, status, True, False, aadsts, detail)
    if types & _TRANSIENT_TYPES:
        return ToolError(ToolErrorCategory.TRANSIENT, status, True, False, aadsts, detail)

    # 4) HTTP status code (from the object).
    if status == 401:
        return ToolError(ToolErrorCategory.AUTH_CONSENT, status, False, False, aadsts, detail)
    if status == 403:
        return ToolError(ToolErrorCategory.PERMISSION, status, False, False, aadsts, detail)
    if status in _TRANSIENT_STATUS:
        return ToolError(ToolErrorCategory.TRANSIENT, status, True, False, aadsts, detail)

    # 5) Structured error-code attribute.
    if error_code in _TRANSIENT_CODES:
        return ToolError(ToolErrorCategory.TRANSIENT, status, True, False, aadsts, detail)

    # 6) Explicit non-retryable signal, else genuinely unknown.
    return ToolError(ToolErrorCategory.UNKNOWN, status, False, False, aadsts, detail)


# Redact bearer tokens, JWTs and long opaque secrets before exposing a raw reason.
_SECRET_RE = re.compile(r"(eyJ[A-Za-z0-9._-]{12,}|Bearer\s+\S+|[A-Za-z0-9_\-]{40,})")


def safe_reason(detail: str, limit: int = 500) -> str:
    """The real error text, with secrets stripped and whitespace collapsed.

    No canned/hardcoded copy — this is the actual reason returned by the failing
    tool/service, so the agent (and the user) sees the truth.
    """
    if not detail:
        return ""
    d = _SECRET_RE.sub("<redacted>", detail)
    d = " ".join(d.split())
    return d[:limit] + ("…" if len(d) > limit else "")


def user_message_for(err: ToolError, tool: Optional[str] = None) -> str:
    """Surface the real failure (tool, codes, actual reason) — not a fixed string."""
    bits = []
    if tool:
        bits.append(tool)
    if err.status_code:
        bits.append(f"HTTP {err.status_code}")
    if err.aadsts:
        bits.append(f"AADSTS{err.aadsts}")
    head = " · ".join(bits)
    reason = safe_reason(err.detail) or err.category.value
    return f"{head} — {reason}" if head else reason


def emit_tool_error(op: str, err: ToolError, attempt: int = 1) -> None:
    """Send a structured failure event to Application Insights telemetry."""
    track_event_if_configured(
        "Tool_Error",
        {
            "op": op,
            "attempt": attempt,
            "category": err.category.value,
            "status_code": err.status_code or 0,
            "aadsts": err.aadsts or "",
            "retryable": err.retryable,
            "consent_required": err.consent_required,
        },
    )


async def run_with_backoff(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    op: str = "tool_call",
) -> T:
    """Await ``factory()``, retrying ONLY retryable (transient / connectivity /
    expired-token) errors with exponential back-off + jitter.

    Non-retryable errors (consent, permission, unknown) raise immediately so the
    caller can route them to the right user message / consent flow without
    wasting time on doomed retries.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            err = classify_tool_error(exc)
            emit_tool_error(op, err, attempt)
            last = exc
            if not err.retryable or attempt == attempts:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, base_delay)  # jitter to avoid thundering herd
            logger.warning(
                "%s failed (%s status=%s, attempt %d/%d) — retrying in %.2fs",
                op,
                err.category.value,
                err.status_code,
                attempt,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last
