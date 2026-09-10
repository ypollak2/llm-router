"""S2-5b — a warm session is not evidence that the conversation answers THIS prompt.

The original S2-5 condition tested a property of the SESSION (at least one prior
assistant turn, at least 200 characters of context) and never whether the context
bore on the prompt in front of it. So once any exchange existed, every gated prompt
was rescued. Replayed over 376 real prompts that put routable at 100%, which is not
a result, it is a bypass.

The quality check is what settled it. With one arena-demo exchange in the store:

    "continue with U7"  ->  "# U7: Document Attachment Fix for Arena Demo ..."
    "continue with U4"  ->  "# U4: Review and Resolution of Document Attachment ..."

U7 and U4 are different items in the user's plan. The model knew neither, anchored
on the only context it had, and wrote confident detailed content for both. Same for
"run codex audit again", which narrated running an audit it cannot run.

S2-6 does not catch these: they invent PROSE, not file paths. That limitation was
documented when the grounding check was written and is now demonstrated.

What makes a rescue legitimate is one of two things, not the mere existence of
history:

  * the prompt shares real subject matter with the conversation — it is asking
    about the thing that was being discussed; or
  * the prompt is a pure continuation ("yes, do it", "go ahead") whose entire
    meaning is "whatever we just agreed", where recency IS the referent.

"continue with U7" looks like the second but is not: it names a specific target the
conversation never mentioned. That is precisely the case that fabricated, so a
continuation must carry no unexplained specifics.
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
    spec = importlib.util.spec_from_file_location("s2_05b_auto_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["s2_05b_auto_route"] = mod
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
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "s2-05b-scope")
    yield


SID = "s2-05b-session"


@pytest.fixture
def warm(hook):
    session_store.record_event(
        SID, "user_prompt",
        "the arena-demo review page shows no document for 104 of 116 cases",
        role="user", task_type="code",
    )
    session_store.record_event(
        SID, "claude_answer",
        "Fixed: persona documents were never attached in demo/host/identity.py.",
        role="assistant", task_type="code",
    )
    return SID


# ── the prompts that actually fabricated ───────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "continue with U7",
    "continue with U4",
    "run codex audit again to check the fixes",
    "fix the stale test count in the plan",
    "Can you bring the last things we've worked on in this repo?",
])
def test_prompts_that_fabricated_are_no_longer_rescued(hook, warm, prompt):
    """Each of these produced confident invented content in the quality check."""
    assert hook._session_context_rescue(prompt, warm) is None, (
        f"{prompt!r} would still be routed against unrelated context"
    )


# ── what a legitimate rescue looks like ────────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "commit this and show me the demo again",
    "did the persona document fix actually work?",
    "show me the review page again",
])
def test_prompts_about_the_conversation_are_still_rescued(hook, warm, prompt):
    """The mechanism must survive its own guard, or S2-5 was pointless."""
    assert hook._session_context_rescue(prompt, warm) is not None, (
        f"{prompt!r} is about the conversation and should still route"
    )


def test_a_bare_continuation_is_rescued(hook, warm):
    """"yes, do it" means "whatever we just agreed" — recency IS the referent."""
    assert hook._session_context_rescue("yes, do it", warm) is not None


def test_a_continuation_naming_an_unknown_target_is_not(hook, warm):
    """The U7 case: shaped like a continuation, but names something unexplained."""
    assert hook._session_context_rescue("continue with U7", warm) is None
    assert hook._session_context_rescue("go ahead with phase 3 of the migration", warm) is None


# ── the guard must not collapse into "never rescue" ────────────────────────

def test_the_guard_is_not_a_blanket_refusal(hook, warm):
    """A guard that rejects everything passes every 'does not fabricate' test."""
    rescued = [
        p for p in (
            "commit this and show me the demo again",
            "did the persona document fix actually work?",
            "yes, do it",
            "show me the review page again",
        )
        if hook._session_context_rescue(p, warm)
    ]
    assert len(rescued) >= 3, f"guard rejected almost everything: {rescued}"


def test_still_nothing_to_rescue_against_when_cold(hook):
    assert hook._session_context_rescue("commit this", "s2-05b-cold") is None
