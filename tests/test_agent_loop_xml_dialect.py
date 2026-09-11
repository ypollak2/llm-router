"""The repair shim must speak Qwen's XML tool-call dialect.

qwen3-coder:30b emits every tool call as `<function=NAME><parameter=K>V</parameter>`
and leaves Ollama's structured `tool_calls` field empty. Before this, the shim
matched only the JSON dialect, `_repair_toolcalls` returned [], `tools_used`
stayed 0, and run_agent_loop's "the model only chatted" guard discarded a
CORRECT tool call. Measured on a 5-task repo harness: 2/5 before, and the three
failures returned None rather than a wrong answer — the signature of a parser
that cannot hear the model, not of a model that cannot drive the loop.
"""
from __future__ import annotations

import pytest

from llm_router.hooks.agent_loop import _repair_toolcalls, _repair_xml_toolcalls

QWEN_REAL = """I need to find the default value of OLLAMA_TIMEOUT.

<function=read_file>
<parameter=path>
src/llm_router/hooks/auto-route.py
</parameter>
</function>
</tool_call>"""


def test_the_exact_payload_qwen3_coder_emits():
    """Captured verbatim from a live qwen3-coder:30b turn, stray </tool_call> and all."""
    assert _repair_toolcalls(QWEN_REAL) == [
        {"function": {"name": "read_file",
                      "arguments": {"path": "src/llm_router/hooks/auto-route.py"}}}
    ]


def test_a_missing_closing_function_tag_still_parses():
    """The model routinely omits </function>; the body runs to end-of-string."""
    calls = _repair_xml_toolcalls(
        "<function=search_files>\n<parameter=pattern>\n_MAX_ITERATIONS\n</parameter>"
    )
    assert calls[0]["function"]["arguments"]["pattern"] == "_MAX_ITERATIONS"


def test_two_calls_in_one_turn_do_not_bleed_into_each_other():
    calls = _repair_xml_toolcalls(
        "<function=read_file><parameter=path>a.py</parameter></function>"
        "<function=read_file><parameter=path>b.py</parameter></function>"
    )
    assert [c["function"]["arguments"]["path"] for c in calls] == ["a.py", "b.py"]


def test_multiple_parameters_are_all_recovered():
    calls = _repair_xml_toolcalls(
        "<function=edit_file>\n<parameter=path>\nx.py\n</parameter>\n"
        "<parameter=old_string>\nfoo\n</parameter>\n"
        "<parameter=new_string>\nbar\n</parameter>\n</function>"
    )
    assert calls[0]["function"]["arguments"] == {
        "path": "x.py", "old_string": "foo", "new_string": "bar"
    }


def test_an_unknown_tool_name_is_dropped_not_executed():
    """A hallucinated tool must fall through to the next model, not burn an
    iteration on an "Unknown tool" string fed back as a tool result."""
    assert _repair_xml_toolcalls(
        "<function=grep><parameter=pattern>x</parameter></function>") == []


def test_a_function_tag_with_no_parameters_is_not_a_call():
    assert _repair_xml_toolcalls("<function=read_file></function>") == []


@pytest.mark.parametrize("value", ["2.0.0", "true", "15", "null"])
def test_argument_values_are_never_coerced(value):
    """json-parsing the value would turn the version string 2.0.0's neighbours —
    true/15/null — into a bool/int/None and hand execute_tool the wrong type."""
    calls = _repair_xml_toolcalls(
        f"<function=read_file><parameter=path>{value}</parameter></function>")
    assert calls[0]["function"]["arguments"]["path"] == value


def test_the_json_dialect_still_wins_when_both_could_match():
    """Regression guard: the JSON path is the older, proven one (qwen2.5-coder:7b
    0/3 → 3/3). XML recovery must not shadow it."""
    assert _repair_toolcalls('{"name": "read_file", "arguments": {"path": "a.py"}}') == [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
    ]


def test_plain_prose_is_still_not_a_tool_call():
    assert _repair_toolcalls("I looked at the function and it seems fine.") == []
    assert _repair_toolcalls("") == []
