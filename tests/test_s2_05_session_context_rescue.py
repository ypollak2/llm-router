"""S2-5 — the gate should ask whether the reference is RESOLVABLE, not just refuse.

`_is_context_dependent` blocks ~48% of real prompts (measured on 376 from this
machine's transcripts), and reading them confirmed the refusal is correct: they
genuinely point at local state. Stage 1 then added an OKF rescue for the ones that
NAME code, worth 1.3%. The rest do not name anything — they point:

    "commit this and show me the demo again"
    "show me the score when it's done"
    "Seems like I see the old demo, can you please check it"

No document retrieval resolves "this". The conversation does. Measured directly,
same prompt and same model, from a real transcript:

    without session context (10s):
        "I need more specific details... Could you clarify?"
    with session context (3s):
        "'This' refers to the technical assessment submission for the Head of AI
         role in the arena-demo repo... using agenticgraphs and llm-router to
         implement a V2 UI plan, addressing 104 out of 116 cases showing no
         document"

So the gate's question is wrong. It asks "does this prompt reference local state?"
when what decides routability is "can the reference be resolved from what we have?"

The rescue is deliberately conservative, because the failure mode inverts. Without
context the model refuses, which is safe and merely wasteful. With thin or wrong
context it answers confidently about the wrong thing, which is the fabrication this
whole effort exists to stop. So a rescue requires an actual exchange to resolve
against — at minimum one prior assistant turn, since "did that work?" and "commit
this" are unanswerable from the user's own prompts alone.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router import session_store

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("s2_05_auto_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["s2_05_auto_route"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "s2-05-scope")
    yield


def _exchange(sid: str) -> None:
    session_store.record_event(
        sid, "user_prompt",
        "the arena-demo review page shows no document for 104 of 116 cases",
        role="user", task_type="code",
    )
    session_store.record_event(
        sid, "claude_answer",
        "Fixed: persona documents were never attached in demo/host/identity.py.",
        role="assistant", task_type="code",
    )


POINTING = "commit this and show me the demo again"


def test_a_pointing_prompt_is_still_gated_with_no_history(hook):
    """Nothing to resolve against — refusing is correct."""
    assert hook._is_context_dependent(POINTING)
    assert hook._session_context_rescue(POINTING, "s2-05-empty") is None


def test_a_pointing_prompt_is_rescued_once_there_is_an_exchange(hook):
    sid = "s2-05-has-history"
    _exchange(sid)
    ctx = hook._session_context_rescue(POINTING, sid)
    assert ctx, "an exchange exists but the prompt was not rescued"
    assert "arena-demo" in ctx


def test_user_turns_alone_are_not_enough(hook):
    """"did that work?" cannot be answered from the questions alone.

    A store holding only the user's own prompts describes what was asked and never
    what was concluded, which is precisely the half a pointing prompt refers to.
    """
    sid = "s2-05-user-only"
    for i in range(4):
        session_store.record_event(
            sid, "user_prompt", f"please look at thing {i}", role="user", task_type="code"
        )
    assert hook._session_context_rescue(POINTING, sid) is None


def test_a_trivial_exchange_is_not_enough(hook):
    """Thin context is worse than none: it invites a confident wrong resolution."""
    sid = "s2-05-thin"
    session_store.record_event(sid, "user_prompt", "hi", role="user")
    session_store.record_event(sid, "claude_answer", "ok", role="assistant")
    assert hook._session_context_rescue(POINTING, sid) is None


def test_a_self_contained_prompt_is_never_gated_so_never_rescued(hook):
    sid = "s2-05-selfcontained"
    _exchange(sid)
    assert not hook._is_context_dependent("explain CRDTs to me")


def test_no_session_id_cannot_rescue(hook):
    assert hook._session_context_rescue(POINTING, "") is None


def test_rescue_failure_is_never_fatal(hook, monkeypatch):
    """Context is best-effort; a broken store must not break the prompt."""
    def _boom(*_a, **_k):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(session_store, "build_session_context", _boom)
    assert hook._session_context_rescue(POINTING, "s2-05-broken") is None


def test_the_rescue_can_be_disabled(hook, monkeypatch):
    """An operator who does not want conversation relayed to a local model."""
    sid = "s2-05-optout"
    _exchange(sid)
    monkeypatch.setenv("LLM_ROUTER_SESSION_RESCUE", "off")
    assert hook._session_context_rescue(POINTING, sid) is None


# ── the privacy gate the rescue depends on ──────────────────────────────────

def test_local_mode_does_not_silently_disable_draft_context(monkeypatch):
    """`local` is the setting most likely chosen by someone who wants context to
    stay on the machine — and it was the one that switched context off entirely.

    The hook passes `target_provider="local"`, a category meaning "the free-local
    draft chain" (already guaranteed by the free-tier-drafts filter). It was absent
    from the allowlist, so `build_session_context` returned "" for every draft with
    no error. Masked until now because the default mode is `all`; a rescue that
    fires and then ships nothing is worse than no rescue.
    """
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    sid = "s2-05-localmode"
    _exchange(sid)
    ctx = session_store.build_session_context(
        sid, max_tokens=3000, task_type="code",
        query="arena-demo", target_provider="local",
    )
    assert ctx, "local mode returned no context for a local draft"
    assert "arena-demo" in ctx


def test_local_mode_still_blocks_a_paid_external_provider(monkeypatch):
    """The guarantee that matters must survive the fix."""
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    sid = "s2-05-localmode-block"
    _exchange(sid)
    for provider in ("openai", "gemini", "perplexity", "anthropic"):
        assert session_store.build_session_context(
            sid, max_tokens=3000, task_type="code",
            query="arena-demo", target_provider=provider,
        ) == "", f"context leaked to {provider} under local mode"
