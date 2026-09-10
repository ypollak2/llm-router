"""S2-4 — the draft context budget is configuration, not a literal in the hook.

`auto-route.py` called `build_session_context(..., max_tokens=800)` with 800
written into the call. Meanwhile `RouterConfig.session_context_max_tokens_draft`
exists, documents itself as "budget for hook-level direct/draft call injection",
defaults to 800 — and nothing read it. Dead config: an operator who changed it saw
no effect, and the only way to alter the budget was to edit the hook.

Whether 800 is *enough* is the second question. Measured across this machine's 27
sessions with 5+ events, the largest hold ~40k, ~12k and ~9k tokens of raw content;
800 is 1-8% of the working sessions and 25-32% of the short ones.
`build_session_context` selects before it truncates (3 newest events, plus older
ones matching the task type or sharing keywords with the query), so the cap is not
discarding 99% of what matters — but it was set without reference to what the
receiving model can actually hold.

Measured rather than assumed, since the risk of raising it is that the question
itself gets pushed out of the context window:

    qwen3-coder:30b, default num_ctx, 4656-token prompt
      -> prompt_eval_count=4656, and the marker planted AFTER the filler
         came back correctly ('PELICAN-9931')

So a 4.6k-token prompt survives intact on the model this actually routes to, and a
3000-token context budget leaves room for the prompt, the OKF block and the answer.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


@pytest.fixture(scope="module")
def hook():
    return _load("s2_04_auto_route")


def test_the_budget_is_not_hardcoded_at_the_call_site():
    """The literal is what made the config field dead."""
    src = HOOK.read_text(encoding="utf-8")
    assert not re.search(r"max_tokens\s*=\s*800\b", src), (
        "the draft context budget is still a literal in the hook"
    )


def test_config_field_is_actually_read(hook):
    assert hasattr(hook, "_draft_context_budget")
    assert isinstance(hook._draft_context_budget(), int)


def test_default_leaves_room_for_the_prompt_and_the_answer(hook, monkeypatch):
    """Big enough to carry a real conversation, small enough to fit the window.

    A 4656-token prompt was verified intact on qwen3-coder:30b at default num_ctx,
    with a marker after the filler still recoverable.
    """
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET", raising=False)
    budget = hook._draft_context_budget()
    assert budget > 800, "still the old budget"
    assert budget <= 4000, (
        f"budget {budget} risks crowding out the prompt on a local model"
    )


def test_env_override_wins(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET", "1234")
    assert hook._draft_context_budget() == 1234


def test_a_junk_override_falls_back_rather_than_crashing(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET", "not-a-number")
    assert hook._draft_context_budget() > 0


def test_a_nonsensical_budget_is_clamped(hook, monkeypatch):
    """0 or negative would silently disable context; huge would blow the window."""
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET", "0")
    assert hook._draft_context_budget() > 0
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET", "999999")
    assert hook._draft_context_budget() <= 32000


def test_more_budget_actually_yields_more_context(tmp_path, monkeypatch):
    """The budget must reach build_session_context, not just exist."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "s2-04-scope")
    from llm_router import session_store

    sid = "s2-04-sess"
    for i in range(40):
        session_store.record_event(
            sid, "user_prompt", f"turn {i}: the invoice reconciler drifts on ledger_delta",
            role="user", task_type="code",
        )
    small = session_store.build_session_context(
        sid, max_tokens=200, task_type="code", query="reconciler", target_provider="ollama"
    )
    large = session_store.build_session_context(
        sid, max_tokens=3000, task_type="code", query="reconciler", target_provider="ollama"
    )
    assert len(large) > len(small), "raising the budget changed nothing"
