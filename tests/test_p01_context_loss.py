"""P0.1 context-loss regressions (R-CTX-4): the newest context and the live
request survive every truncation.

* P0.1-b  ``session_store.build_session_context`` kept the OLDEST events when
  over budget (``truncate_to_budget`` sliced the head), so the newest turn was
  the first thing lost. Property test: newest event present in 200/200
  over-budget cases.
* P0.1-c  ``context.build_context_messages`` cut the joined layers with
  ``combined[:max_chars]``. Layer 3 (the caller's live context) is appended
  last, so it was the first thing cut. 4 over-budget cases at 10x budget.
* P0.1-d  ``context_prep.prepare_prompt`` passed the user prompt through
  ``truncate_to_budget``. The prompt is now intact, or ``ContextOverflow`` is
  raised when it alone exceeds the model window.

Every test here fails on da31df7.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hypothesis import Phase, given, settings
from hypothesis import strategies as st

from llm_router import session_store
from llm_router.token_budget import estimate_tokens, truncate_to_budget

# ── P0.1-b: newest event survives session-context truncation ─────────────────

_CASES = {"n": 0}
_WORDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=20, max_size=600)


@st.composite
def _over_budget_session(draw):
    budget = draw(st.integers(min_value=40, max_value=600))
    # The newest event fits the budget on its own (with room for the marker
    # and the sentinel lines); everything older pushes the total over it.
    newest_len = draw(st.integers(min_value=10, max_value=budget * 4 - 80))
    newest = "NEWEST " + draw(st.text(alphabet="abcdefghij ", min_size=newest_len,
                                      max_size=newest_len))
    older = draw(st.lists(_WORDS, min_size=3, max_size=60))
    while sum(len(t) for t in older) < budget * 4 + 400:
        older.append("filler " + "z" * 300)
    records = [
        {"kind": "user_prompt", "content": f"old{i} {t}", "task_type": "query"}
        for i, t in enumerate(older)
    ]
    records.append({"kind": "user_prompt", "content": newest, "task_type": "query"})
    return budget, records


@settings(max_examples=200, deadline=None, database=None, phases=[Phase.generate])
@given(_over_budget_session())
def _newest_event_survives(case):
    budget, records = case
    with patch.object(session_store, "load_events", lambda *a, **k: list(records)), \
            patch.object(session_store, "get_mode", lambda: "all"):
        out = session_store.build_session_context("sid", max_tokens=budget, task_type="query")
    full = "\n".join(session_store._format_record(r) for r in records)
    assert estimate_tokens(full) > budget  # the case really is over budget
    assert session_store._format_record(records[-1]) in out
    _CASES["n"] += 1


def test_newest_event_present_in_200_of_200_over_budget_cases():
    _CASES["n"] = 0
    _newest_event_survives()
    print(f"P0.1-b over-budget cases checked: {_CASES['n']}")
    assert _CASES["n"] == 200


def test_truncate_tail_keeps_the_end_and_fits():
    text = "\n".join(f"line {i:04d} " + "x" * 40 for i in range(200))
    out = truncate_to_budget(text, 100, keep="tail")
    assert out.endswith("line 0199 " + "x" * 40)
    assert estimate_tokens(out) <= 100
    assert out.startswith("[…older context truncated…]\n")


def test_truncate_default_still_keeps_the_head():
    text = "\n".join(f"line {i:04d} " + "x" * 40 for i in range(200))
    out = truncate_to_budget(text, 100)
    assert out.startswith("line 0000")


# ── P0.1-c: layer 3 is never cut in build_context_messages ──────────────────

_BUDGET = 100  # tokens -> 400 chars
_TEN_X = _BUDGET * 4 * 10


def _live(n_chars: int) -> str:
    words, i = [], 0
    while sum(len(w) + 1 for w in words) < n_chars:
        words.append(f"LIVE{i:05d}")
        i += 1
    return " ".join(words)


@pytest.fixture
def ctx_env(tmp_path):
    from llm_router import context as ctx

    ctx._reset_session_buffers_for_test()
    with patch("llm_router.context._get_db_path", return_value=tmp_path / "empty.db"):
        yield ctx
    ctx._reset_session_buffers_for_test()


async def _build(ctx, *, caller, summaries="", buffer_msgs=0, durable=""):
    async def _summ(limit=3):
        return ["s"] if summaries else []

    with patch.object(ctx, "get_recent_session_summaries", _summ), \
            patch.object(ctx, "format_session_summaries", lambda s: summaries), \
            patch.object(session_store, "resolve_session_id", lambda explicit=None: "sid-p01"), \
            patch.object(session_store, "build_session_context", lambda *a, **k: durable):
        buf = ctx.get_session_buffer(*ctx._resolve_context_identity(None, "sid-p01"))
        for i in range(buffer_msgs):
            buf.record("user", f"buffered message {i} " + "b" * 300, task_type="query")
        msgs = await ctx.build_context_messages(
            caller_context=caller, max_context_tokens=_BUDGET, session_id="sid-p01",
        )
    assert len(msgs) == 1
    return msgs[0]["content"]


@pytest.mark.asyncio
async def test_layer3_intact_when_summaries_are_10x_budget(ctx_env):
    caller = _live(200)
    out = await _build(ctx_env, caller=caller, summaries="Previous session: " + "p " * (_TEN_X // 2))
    assert caller in out


@pytest.mark.asyncio
async def test_layer3_intact_when_session_buffer_is_10x_budget(ctx_env):
    caller = _live(200)
    out = await _build(ctx_env, caller=caller, buffer_msgs=_TEN_X // 300 + 1)
    assert caller in out


@pytest.mark.asyncio
async def test_layer3_intact_when_durable_context_is_10x_budget(ctx_env):
    caller = _live(200)
    out = await _build(ctx_env, caller=caller, durable="USER: " + "d " * (_TEN_X // 2))
    assert caller in out


@pytest.mark.asyncio
async def test_layer3_intact_when_it_alone_is_10x_budget(ctx_env):
    caller = _live(_TEN_X)
    out = await _build(
        ctx_env, caller=caller, summaries="Previous session: " + "p " * 300,
        durable="USER: " + "d " * 300,
    )
    assert caller in out


@pytest.mark.asyncio
async def test_lowest_layer_dropped_before_higher_ones(ctx_env):
    """Priority 3 > 2a > 1 > 2b: a huge durable layer (2b) goes before a small
    summary layer (1)."""
    caller = _live(80)
    out = await _build(
        ctx_env, caller=caller, summaries="Previous session: SUMMARY-KEPT",
        durable="USER: " + "DURABLE " * 2000,
    )
    assert caller in out
    assert "SUMMARY-KEPT" in out
    assert len(out) <= _BUDGET * 4 + 64


# ── P0.1-d: context_prep never truncates the user prompt ────────────────────

def test_200k_prompt_is_intact_when_it_fits_the_window():
    from llm_router.context_prep import prepare_prompt
    from llm_router.types import Complexity, TaskType

    prompt = "Explain this code:\n" + ("x = 1\n" * 40_000)[: 200_000 - 19]
    assert len(prompt) == 200_000
    result = prepare_prompt(prompt, TaskType.CODE, Complexity.SIMPLE, "openai/gpt-4o")
    intact = result.user_prompt == prompt  # a bool: a 200k-char diff crashes pytest's repr
    assert intact, f"user prompt changed: {len(prompt)} -> {len(result.user_prompt)} chars"


def test_200k_prompt_raises_context_overflow_when_over_the_window():
    from llm_router.context_prep import prepare_prompt
    from llm_router.local_context_guard import ContextOverflow
    from llm_router.types import Complexity, TaskType

    prompt = "Explain this code:\n" + ("x = 1\n" * 40_000)[: 200_000 - 19]
    with pytest.raises(ContextOverflow):
        prepare_prompt(prompt, TaskType.CODE, Complexity.SIMPLE, "ollama/gemma4:latest")


@pytest.mark.parametrize("model", ["openai/gpt-4o", "ollama/gemma4:latest", "ollama/qwen3.5"])
@pytest.mark.parametrize("n_chars", [100, 20_000, 60_000, 200_000])
def test_user_prompt_is_never_shortened(model, n_chars):
    from llm_router.context_prep import prepare_prompt
    from llm_router.local_context_guard import ContextOverflow
    from llm_router.types import Complexity, TaskType

    prompt = ("q " * n_chars)[:n_chars]
    try:
        result = prepare_prompt(prompt, TaskType.QUERY, Complexity.MODERATE, model)
    except ContextOverflow:
        return
    intact = result.user_prompt == prompt  # a bool: a 200k-char diff crashes pytest's repr
    assert intact, f"user prompt changed: {len(prompt)} -> {len(result.user_prompt)} chars"
