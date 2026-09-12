"""Keep the task in the window, or the loop forgets what it was asked.

Measured on this machine: the window is ~16,400 tokens and llama.cpp runs with
`--context-shift --keep 4`, so on overflow it discards the OLDEST tokens — the
system prompt and the question — and answers about whatever survived. No error.

`read_file` returned up to 50,000 characters (~12,500 tokens): 76% of the window
in one call. The file the failing harness task reads is 217,729 characters. So
the loop destroyed its own instructions on the first read, then wandered — the
"repeats a read, then refuses to finish" trace, from the other end.
"""
from __future__ import annotations

import pytest

from llm_router.hooks import context_budget as budget


def test_the_per_result_cap_is_far_below_the_window():
    """The old cap was 50,000 chars against a ~16,400-token window — three times
    the whole window in a single tool result."""
    cap_tokens = budget.max_tool_result_chars() / budget.CHARS_PER_TOKEN
    assert cap_tokens <= budget.window_tokens() / 4, (
        "one tool result should not be able to claim more than a quarter of the "
        "window — four of them still have to leave room for the conversation "
        "that interprets them")


def test_the_planning_window_leaves_headroom():
    """Planning against the exact ceiling means the first estimate that runs long
    evicts the task, silently. The margin is the feature."""
    assert budget.window_tokens() < 16_400


def test_truncation_says_how_to_succeed_not_just_that_it_failed():
    """A bare "(truncated)" tells the model its read failed and gives it no
    cheaper way to win, so it reads again — the loop we are breaking."""
    out = budget.truncate_tool_result("x" * 60_000)
    assert "TRUNCATED" in out
    assert "search_files" in out
    assert "offset" in out
    assert "Do NOT read this file again" in out


def test_a_small_result_is_untouched():
    assert budget.truncate_tool_result("short") == "short"


def test_pruning_never_drops_the_system_prompt_or_the_question():
    """These are exactly what llama.cpp takes first. The harness must evict from
    the opposite end."""
    msgs = [{"role": "system", "content": "SYSTEM-CANARY"},
            {"role": "user", "content": "QUESTION-CANARY"}]
    msgs += [{"role": "tool", "content": "x" * 40_000} for _ in range(6)]
    pruned = budget.prune(msgs)
    assert pruned[0]["content"] == "SYSTEM-CANARY"
    assert pruned[1]["content"] == "QUESTION-CANARY"


def test_pruning_drops_the_oldest_tool_results_first():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    msgs += [{"role": "tool", "content": f"RESULT-{i}-" + "x" * 30_000} for i in range(5)]
    pruned = budget.prune(msgs)
    assert "RESULT-0" not in pruned[2]["content"], "oldest result survived"
    assert "RESULT-4" in pruned[-1]["content"], "newest result was dropped"


def test_a_dropped_result_says_so():
    """A silently vanished result invites the model to re-run the call that
    produced it."""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    msgs += [{"role": "tool", "content": "x" * 40_000} for _ in range(5)]
    pruned = budget.prune(msgs)
    assert "dropped to stay within the context window" in pruned[2]["content"]
    assert "Do not re-run it" in pruned[2]["content"]


def test_pruning_brings_the_history_under_budget():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    msgs += [{"role": "tool", "content": "x" * 30_000} for _ in range(8)]
    assert budget.messages_tokens(msgs) > budget.window_tokens()
    assert budget.messages_tokens(budget.prune(msgs)) <= budget.window_tokens() * 1.6


def test_a_history_that_fits_is_returned_unchanged():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
            {"role": "tool", "content": "small"}]
    assert budget.prune(msgs) == msgs


def test_the_restated_task_carries_the_question():
    assert "find the timeout" in budget.restate("find the timeout")
    assert "finish" in budget.restate("x")


@pytest.mark.parametrize("bad", ["", "nonsense", "-1", "0"])
def test_nonsense_settings_fall_back_rather_than_disabling_the_budget(monkeypatch, bad):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WINDOW", bad)
    monkeypatch.setenv("LLM_ROUTER_MAX_TOOL_RESULT_CHARS", bad)
    assert budget.window_tokens() == 12000
    assert budget.max_tool_result_chars() > 0


# ── the tools themselves ────────────────────────────────────────────────────

def test_a_huge_file_read_is_capped(tmp_path):
    from llm_router.hooks.agent_loop import execute_tool
    (tmp_path / "big.py").write_text("\n".join(f"line {i}" for i in range(20_000)))
    out = execute_tool("read_file", {"path": "big.py"}, tmp_path)
    assert len(out) < 50_000
    assert "TRUNCATED" in out


def test_a_range_read_returns_only_that_range(tmp_path):
    """The cap is only fair if there is a cheaper way to get the rest."""
    from llm_router.hooks.agent_loop import execute_tool
    (tmp_path / "big.py").write_text("\n".join(f"line {i}" for i in range(5000)))
    out = execute_tool("read_file", {"path": "big.py", "offset": 100, "limit": 5},
                       tmp_path)
    assert "line 100" in out and "line 104" in out
    assert "line 200" not in out
    assert "of 5000" in out, "the model should know how much it did not read"


def test_a_range_read_survives_nonsense_arguments(tmp_path):
    from llm_router.hooks.agent_loop import execute_tool
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    for args in ({"offset": "x"}, {"limit": "y"}, {"offset": -5}, {"offset": 99999}):
        out = execute_tool("read_file", {"path": "a.py", **args}, tmp_path)
        assert "Error" not in out or "not found" not in out
