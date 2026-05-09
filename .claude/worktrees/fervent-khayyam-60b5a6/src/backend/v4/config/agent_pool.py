"""Session agent pool. One persistent agent per (tenant_id, user_id, session_id).

Eviction: agents not accessed within TTL_SECONDS are removed from the cache.
This prevents unbounded memory growth without risking agent truncation.

NOTE on MCP lifecycle — why we do NOT call agent.close() during TTL eviction:
  anyio cancel scopes are bound to the asyncio Task that entered them.
  Calling close() from a *different* task (loop.create_task) raises
  "Attempted to exit cancel scope in a different task than it was entered in".
  Worse, if the eviction task runs concurrently with an in-flight request that
  still holds a local reference to the same agent object, calling close() on
  it mid-stream truncates the streaming response.
  Therefore TTL eviction simply drops the cache reference and lets Python GC
  reclaim the resources; the framework logs a benign WARNING if the connection
  was already gone when reused.

NOTE on MCP pre-warm — why we ping before returning a cached agent:
  The agent_framework's call_tool() retries ClosedResourceError by calling
  connect(reset=True).  But when the LLM fires N parallel tool calls and ALL
  of them hit ClosedResourceError simultaneously, each independently calls
  connect(reset=True) — creating a race where call B tears down the session
  that call A just established, exhausting A's retry budget.
  By sending a proactive ping (via _ensure_connected() / session.send_ping())
  from the request task BEFORE agent.invoke(), we detect stale connections
  and reconnect exactly once, in a single task, before the parallel tool
  calls begin.  This eliminates the race entirely.
"""

import logging
import time
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


async def _ensure_mcp_warm(agent: Any) -> None:
    """Proactively verify and reconnect the agent's MCP connection if stale.

    The agent_framework's MCPTool exposes ``_ensure_connected()`` which
    sends a ping and reconnects on failure.  We call it once, serially,
    from the same asyncio Task that will later invoke the agent.  This
    prevents the parallel-ClosedResourceError race (see module docstring).

    Safe to call on agents without MCP — simply returns immediately.
    """
    mcp_tool = getattr(agent, "mcp_tool", None)
    if mcp_tool is None:
        return
    if not getattr(mcp_tool, "is_connected", False):
        return  # Never connected; factory will handle first connect

    ensure_fn = getattr(mcp_tool, "_ensure_connected", None)
    if ensure_fn is None:
        return  # Older framework version without _ensure_connected

    try:
        await ensure_fn()
        logger.debug("MCP pre-warm: connection is alive")
    except Exception as e:
        # _ensure_connected already tried reconnect; if it still fails the
        # invoke will handle it.  Log and move on so we don't block the request.
        logger.warning("MCP pre-warm failed (invoke will retry): %s", e)


# Agents idle longer than this are evicted (reference dropped, GC handles rest).
_TTL_SECONDS: int = 3600  # 1 hour

_cache: Dict[Tuple[str, str, str], Any] = {}
_last_used: Dict[Tuple[str, str, str], float] = {}


def _evict_stale() -> None:
    """Remove cache entries that have exceeded the idle TTL.

    Does NOT call agent.close() — see module docstring for the reason.
    """
    now = time.monotonic()
    stale_keys = [k for k, t in _last_used.items() if now - t > _TTL_SECONDS]
    for key in stale_keys:
        _cache.pop(key, None)
        _last_used.pop(key, None)
        logger.info(
            "Evicted stale agent for tenant=%s session=%s (idle >%ds) — "
            "reference dropped, resources released to GC",
            key[0][:8],
            key[2][:12],
            _TTL_SECONDS,
        )


async def _safe_close(agent: Any, key: Tuple[str, str, str]) -> None:
    """Close an agent — only safe to call from the task that owns its cancel scope.

    Use this exclusively for explicit session teardown where the calling task
    is the same one that created the agent (i.e. evict_session called within
    the same request lifecycle that opened the agent).
    For TTL eviction use _evict_stale() which skips close() entirely.
    """
    try:
        if hasattr(agent, "close") and callable(agent.close):
            await agent.close()
            logger.info(
                "Closed agent for tenant=%s session=%s",
                key[0][:8],
                key[2][:12],
            )
    except RuntimeError as e:
        if "cancel scope" in str(e).lower():
            logger.warning(
                "Cross-task close skipped for session=%s — "
                "cancel scope boundary; resources released to GC.",
                key[2][:12],
            )
        else:
            logger.warning("RuntimeError closing agent session=%s: %s", key[2][:12], e)
    except Exception as e:
        logger.warning("Error closing agent session=%s: %s", key[2][:12], e)


async def get_or_create(tenant_id: str, user_id: str, session_id: str, factory) -> Any:
    """Get cached agent or create via factory (async callable returning opened agent).

    On each call, stale entries are evicted first so the pool size stays bounded.
    """
    _evict_stale()

    key = (tenant_id, user_id, session_id)
    if key in _cache:
        _last_used[key] = time.monotonic()
        agent = _cache[key]
        # Pre-warm: verify MCP connection is alive before handing off to invoke.
        # This runs in the request task, preventing the parallel-reconnect race.
        await _ensure_mcp_warm(agent)
        logger.info(
            "Reusing agent for tenant=%s session=%s (pool size: %d)",
            tenant_id[:8],
            session_id[:12],
            len(_cache),
        )
        return agent

    agent = await factory()
    _cache[key] = agent
    _last_used[key] = time.monotonic()
    logger.info(
        "Created agent for tenant=%s session=%s (pool size: %d)",
        tenant_id[:8],
        session_id[:12],
        len(_cache),
    )
    return agent


async def evict_session(tenant_id: str, user_id: str, session_id: str) -> None:
    """Explicitly evict and close the agent for a specific session.

    Call this when a chat session ends so MCP connections are released
    immediately rather than waiting for the TTL.
    """
    key = (tenant_id, user_id, session_id)
    agent = _cache.pop(key, None)
    _last_used.pop(key, None)
    if agent is not None:
        await _safe_close(agent, key)
        logger.info(
            "Explicitly evicted agent for tenant=%s session=%s",
            tenant_id[:8],
            session_id[:12],
        )
