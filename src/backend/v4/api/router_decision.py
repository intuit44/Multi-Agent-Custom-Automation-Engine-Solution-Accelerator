"""Streamed model-router decision parsing.

The model-router answers over chat/completions streaming either with plain
content (it answers the turn itself) or with a function/tool call whose
arguments arrive as string fragments spread across deltas.

The previous inline implementation concatenated ALL tool_call fragments into
one buffer, so parallel tool calls produced '{"task":"A"}{"task":"B"}' —
invalid JSON — and the dispatch silently fell back to the RAW user prompt
instead of the router's task, with no trace in the logs. Fragments are now
accumulated per tool_call index, the first parseable call wins, and a parse
failure is surfaced in the result instead of being swallowed.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RouterDecision:
    """Outcome of a streamed router turn."""

    fn_name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    # Raw argument buffer that failed to parse as a JSON object, when any.
    # The caller must log it — this failure mode used to be invisible.
    parse_error: Optional[str] = None


class RouterDecisionAccumulator:
    """Accumulates streamed tool_call deltas, keyed by tool_call index."""

    def __init__(self) -> None:
        self._names: Dict[int, str] = {}
        self._args: Dict[int, str] = {}
        self._order: List[int] = []

    @property
    def has_function(self) -> bool:
        """True once any tool_call named a function (guards content streaming)."""
        return bool(self._names)

    def add_delta(self, tool_call: Any) -> None:
        """Feed one tool_call delta from the chat/completions stream."""
        fn = getattr(tool_call, "function", None)
        if fn is None:
            return
        idx = getattr(tool_call, "index", None)
        idx = 0 if idx is None else idx
        if idx not in self._order:
            self._order.append(idx)
        name = getattr(fn, "name", None)
        if name:
            self._names[idx] = self._names.get(idx, "") + name
        fragment = getattr(fn, "arguments", None)
        if fragment:
            self._args[idx] = self._args.get(idx, "") + fragment

    def finalize(self) -> RouterDecision:
        """Resolve the accumulated deltas into a decision.

        First tool_call (stream order) whose arguments parse as a JSON object
        wins. If every named call has unparseable arguments, the first one is
        returned with ``parse_error`` set so the caller can log it loudly.
        """
        first_bad: Optional[RouterDecision] = None
        for idx in self._order:
            name = self._names.get(idx, "")
            if not name:
                continue
            raw = self._args.get(idx, "")
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = None
            if isinstance(args, dict):
                return RouterDecision(fn_name=name, args=args)
            if first_bad is None:
                first_bad = RouterDecision(fn_name=name, args={}, parse_error=raw)
        return first_bad or RouterDecision()
