"""Function-invocation middleware that lets the model recover from tool failures.

When an agent's tool raises, ``agent_framework`` would normally propagate the
exception out of ``agent.invoke`` / ``workflow.run`` — by which point the model's
turn is already over and the only thing the backend can do is render a UI error.

This middleware sits inside the function-invocation loop (where the tool actually
executes, in-process, for ``MCPStreamableHTTPTool`` and any Python function tool).
It classifies the failure with :func:`classify_tool_error`, and when the error is
*model-recoverable* (bad arguments / wrong path / missing id) it substitutes the
tool result with :pyattr:`ToolError.message_for_model` and returns normally — so
the model sees an actionable tool result and retries with a corrected call **in
the same turn**. Non-recoverable errors (consent, permission, transient,
connectivity, unknown) are re-raised so the router surfaces the right UI/telemetry.

Server-side Foundry tools (published-agent KB, code interpreter) execute remotely
and never enter this pipeline — they are unaffected by design.
"""

from __future__ import annotations

import logging

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from v4.common.tool_errors import classify_tool_error, emit_tool_error

logger = logging.getLogger(__name__)


class SelfHealToolMiddleware(FunctionMiddleware):
    """Convert recoverable tool exceptions into model-readable tool results."""

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        tool_name = getattr(context.function, "name", None) or "tool"
        try:
            await call_next()
        except Exception as exc:  # noqa: BLE001 — classified, then healed or re-raised
            err = classify_tool_error(exc)
            emit_tool_error(f"tool:{tool_name}", err)
            if err.recoverable_by_model:
                logger.warning(
                    "Self-healing tool '%s' (%s, status=%s) — returning guidance "
                    "to the model for in-turn retry.",
                    tool_name,
                    err.category.value,
                    err.status_code,
                )
                # Hand the model an actionable result instead of crashing the turn.
                # Returning normally (no MiddlewareTermination) lets the agent loop
                # continue so the model can re-call the tool with a corrected payload.
                context.result = (
                    f"[tool_error:{err.category.value}] {err.message_for_model}"
                )
                return
            logger.error(
                "Tool '%s' failed unrecoverably (%s, status=%s) — propagating.",
                tool_name,
                err.category.value,
                err.status_code,
            )
            raise
