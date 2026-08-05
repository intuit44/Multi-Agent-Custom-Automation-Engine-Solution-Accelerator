"""Unit tests for the streamed model-router decision parser.

Pins the behaviors that used to be invisible in the router.py inline version:
fragment accumulation across deltas, PARALLEL tool calls (the silent-fallback
bug: '{"task":"A"}{"task":"B"}' concatenation), and loud parse failures.
"""

import importlib.util
import json
import os
from types import SimpleNamespace

# Direct file-location import: suite-order sys.modules stubs of 'v4' (from
# other test modules' header mocks) break package imports; the module under
# test is pure stdlib so loading it standalone is always safe.
_mod_path = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "..", "backend", "v4", "api", "router_decision.py",
    )
)
_spec = importlib.util.spec_from_file_location("router_decision_under_test", _mod_path)
_rd = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_rd)
RouterDecision = _rd.RouterDecision
RouterDecisionAccumulator = _rd.RouterDecisionAccumulator


def _tc(index=0, name=None, arguments=None):
    return SimpleNamespace(
        index=index, function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_empty_stream_yields_empty_decision():
    acc = RouterDecisionAccumulator()
    d = acc.finalize()
    assert d == RouterDecision()
    assert not acc.has_function


def test_single_call_fragmented_arguments():
    acc = RouterDecisionAccumulator()
    acc.add_delta(_tc(0, name="run_macae_mcp_server"))
    acc.add_delta(_tc(0, arguments='{"task": "Use Git'))
    acc.add_delta(_tc(0, arguments='Hub tools"}'))
    d = acc.finalize()
    assert d.fn_name == "run_macae_mcp_server"
    assert d.args == {"task": "Use GitHub tools"}
    assert d.parse_error is None


def _parallel_call_deltas():
    """Minimal repro scenario: two parallel tool_calls, fragmented args."""
    return [
        _tc(0, name="run_macae_mcp_server", arguments='{"task": "A"'),
        _tc(1, name="run_knowledge_base", arguments='{"task": "B"'),
        _tc(0, arguments="}"),
        _tc(1, arguments="}"),
    ]


def test_bug_reproduces_under_old_shared_buffer_algorithm():
    # Executable bug report — the pre-fix inline algorithm from router.py:
    # ONE shared buffer for every tool_call index. On the minimal scenario it
    # yields '{"task": "A"{"task": "B"}}'-style garbage -> json error -> the
    # old `except: _args = {}` -> dispatch silently ran on the RAW user prompt.
    _fn_name, _fn_args = "", ""
    for tc in _parallel_call_deltas():
        fn = tc.function
        if fn.name:
            _fn_name = fn.name
        if fn.arguments:
            _fn_args += fn.arguments
    try:
        _args = json.loads(_fn_args or "{}")
    except Exception:
        _args = {}
    # The router DID choose a function with a task — and the old code lost it:
    assert _fn_name == "run_knowledge_base"  # last-writer-wins, also wrong
    assert _args == {}  # task gone -> `_args.get("task") or prompt` -> raw


def test_parallel_tool_calls_do_not_concatenate():
    # Same minimal scenario against the fix: per-index buffers, first call wins.
    acc = RouterDecisionAccumulator()
    for tc in _parallel_call_deltas():
        acc.add_delta(tc)
    d = acc.finalize()
    assert d.fn_name == "run_macae_mcp_server"
    assert d.args == {"task": "A"}
    assert d.parse_error is None


def test_first_call_unparseable_falls_through_to_second():
    acc = RouterDecisionAccumulator()
    acc.add_delta(_tc(0, name="run_macae_mcp_server", arguments='{"task": bro'))
    acc.add_delta(_tc(1, name="run_knowledge_base", arguments='{"task": "ok"}'))
    d = acc.finalize()
    assert d.fn_name == "run_knowledge_base"
    assert d.args == {"task": "ok"}
    assert d.parse_error is None


def test_all_unparseable_reports_loudly():
    acc = RouterDecisionAccumulator()
    acc.add_delta(_tc(0, name="run_macae_mcp_server", arguments='{"task": "A"}{"x"'))
    d = acc.finalize()
    assert d.fn_name == "run_macae_mcp_server"
    assert d.args == {}
    assert d.parse_error is not None
    assert '{"task": "A"}{"x"' in d.parse_error


def test_non_dict_json_is_a_parse_error():
    acc = RouterDecisionAccumulator()
    acc.add_delta(_tc(0, name="run_web_search", arguments='["not", "a", "dict"]'))
    d = acc.finalize()
    assert d.args == {}
    assert d.parse_error is not None


def test_no_arguments_yields_empty_args():
    acc = RouterDecisionAccumulator()
    acc.add_delta(_tc(0, name="run_web_search"))
    d = acc.finalize()
    assert d.fn_name == "run_web_search"
    assert d.args == {}
    assert d.parse_error is None


def test_chunked_function_name_is_appended():
    acc = RouterDecisionAccumulator()
    acc.add_delta(_tc(0, name="run_"))
    acc.add_delta(_tc(0, name="python_execution", arguments='{"task": "t"}'))
    assert acc.finalize().fn_name == "run_python_execution"


def test_missing_index_defaults_to_zero():
    acc = RouterDecisionAccumulator()
    acc.add_delta(
        SimpleNamespace(function=SimpleNamespace(name="run_web_search", arguments="{}"))
    )
    d = acc.finalize()
    assert d.fn_name == "run_web_search"
    assert d.args == {}


def test_has_function_guards_content_streaming():
    # router.py streams router text only while no function has been named
    acc = RouterDecisionAccumulator()
    assert not acc.has_function
    acc.add_delta(_tc(0, arguments='{"ta'))  # fragment without a name yet
    assert not acc.has_function
    acc.add_delta(_tc(0, name="run_macae_mcp_server"))
    assert acc.has_function


def test_delta_without_function_attr_is_ignored():
    acc = RouterDecisionAccumulator()
    acc.add_delta(SimpleNamespace(index=0, function=None))
    assert acc.finalize() == RouterDecision()
