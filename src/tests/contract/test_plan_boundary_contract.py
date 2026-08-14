"""The Plan boundary: what the code hands to the Magentic framework.

Schemathesis cannot see this defect class: POST /process_request returns 200
whether the task it forwards is the bare objective or carries kilobytes of
welded session preamble — the HTTP contract is identical either way. The
assertion here is on the OBJECT at the framework boundary instead:
``MagenticContext.task`` must be the current objective and
``MagenticContext.chat_history`` must carry the prior conversation as real
Messages, because that is how the framework models them (separate fields).

Lives in this tree (not ``src/tests/backend``) because it needs the REAL
``agent_framework`` — 25 of the 33 backend test files replace it in
``sys.modules`` with Mock() at import time.

No cassettes: nothing here reaches a socket. The manager is constructed with
an inert agent object and the LLM-calling half of plan() is cut off by a
monkeypatch that captures the context it would have received.
"""

import pytest
from agent_framework import Message
from agent_framework_orchestrations._magentic import (
    MagenticContext,
    StandardMagenticManager,
)

from v4.orchestration.human_approval_manager import HumanApprovalMagenticManager

OBJECTIVE = "Crea una App para medir la temperatura del aire"

HISTORY = [
    {"role": "user", "content": "Necesito comparar sensores de temperatura"},
    {"role": "assistant", "content": "Comparativa previa: DHT22 vs BME280 ..."},
    {"role": "user", "content": "El DHT22 me convence por precio"},
    {"role": "user", "content": "   "},  # blank turn — must be dropped
]


class _InertAgent:
    """Satisfies the constructor (create_session); no test reaches an LLM call."""

    def create_session(self):
        return None


def _manager() -> HumanApprovalMagenticManager:
    return HumanApprovalMagenticManager(
        user_id="contract-user",
        agent=_InertAgent(),
        max_round_count=3,
        max_stall_count=3,
        max_reset_count=2,
    )


def _context() -> MagenticContext:
    # Exactly how the orchestrator builds it in _handle_messages: the task
    # message is the only element of chat_history at plan() time.
    return MagenticContext(
        task=OBJECTIVE,
        participant_descriptions={"AgentA": "does A"},
        chat_history=[Message(role="user", text=OBJECTIVE)],
    )


def _role(message: Message) -> str:
    return getattr(message.role, "value", message.role)


def test_task_stays_bare_and_history_becomes_messages():
    mgr = _manager()
    mgr.seed_chat_history(HISTORY)

    ctx = _context()
    mgr._apply_pending_history(ctx)

    # The objective is the task — no preamble, no separator, no welded history.
    assert ctx.task == OBJECTIVE

    # The conversation precedes the task message, as user-role Messages.
    # The assistant turn (a recovered plan draft) and the blank turn are gone.
    assert [m.text for m in ctx.chat_history] == [
        "Necesito comparar sensores de temperatura",
        "El DHT22 me convence por precio",
        OBJECTIVE,
    ]
    assert {_role(m) for m in ctx.chat_history} == {"user"}


def test_seed_is_consumed_once():
    mgr = _manager()
    mgr.seed_chat_history(HISTORY)

    ctx = _context()
    mgr._apply_pending_history(ctx)
    once = list(ctx.chat_history)

    # A replan or a second run on the same (reused) manager must not re-seed.
    mgr._apply_pending_history(ctx)
    assert ctx.chat_history == once


def test_empty_seed_clears_stale_context():
    mgr = _manager()
    mgr.seed_chat_history(HISTORY)
    # A new run with no recovered history overwrites the stale seed
    # (run_orchestration calls seed_chat_history unconditionally).
    mgr.seed_chat_history([])

    ctx = _context()
    mgr._apply_pending_history(ctx)
    assert [m.text for m in ctx.chat_history] == [OBJECTIVE]


@pytest.mark.asyncio
async def test_plan_seeds_before_delegating(monkeypatch):
    """plan() must apply the seed BEFORE the framework's planning LLM calls.

    StandardMagenticManager.plan grounds its facts/plan completions on
    ``[*chat_history, ...]`` — seeding after it would be a silent no-op. The
    monkeypatch captures the context super().plan receives and cuts execution
    there (the real method would go on to LLM calls and the approval gate).
    """
    captured: dict = {}

    class _Cut(Exception):
        pass

    async def _capture(self, magentic_context):
        captured["ctx"] = magentic_context
        raise _Cut()

    monkeypatch.setattr(StandardMagenticManager, "plan", _capture)

    mgr = _manager()
    mgr.seed_chat_history(HISTORY)

    with pytest.raises(_Cut):
        await mgr.plan(_context())

    ctx = captured["ctx"]
    assert ctx.task == OBJECTIVE
    assert [m.text for m in ctx.chat_history] == [
        "Necesito comparar sensores de temperatura",
        "El DHT22 me convence por precio",
        OBJECTIVE,
    ]
