"""S2-6 — a draft may not name files that exist neither in its context nor on disk.

Every earlier item in Stage 2 makes the routed model MORE willing to answer. S2-5
in particular flips the failure mode: before it, a model with no context refused
("I need more specific details"), which is safe and merely wasteful. After it, a
model with context answers — and a model with the WRONG context answers just as
fluently about the wrong thing. That is the fabrication this whole effort exists to
stop, arriving through the door built to fix it.

So the last item is the check, not another lever.

What is checkable without a second model call is the same thing the OKF store
already restricts itself to: verified structure. A draft that says
`demo/host/identity.py` is making a claim that can be tested — the path is either
in the material the model was given, or it exists in the repo, or the model
invented it. The third case is exactly the failure reported on 2026-09-06, where
routed reviews "listed tests that do not exist".

Deliberately narrow. It does not judge whether the draft is *right*; it catches the
specific, mechanical way a context-fed draft goes wrong, and it costs nothing.
Prose claims are out of scope — that needs a judge, and a judge that is wrong is
worse than no judge.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("s2_06_auto_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["s2_06_auto_route"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


CONTEXT = (
    "[user] the arena-demo review page shows no document for 104 of 116 cases\n"
    "[assistant] Fixed: persona documents were never attached in demo/host/identity.py."
)


def test_a_draft_naming_only_context_files_is_grounded(hook):
    draft = "The fix is in demo/host/identity.py — persona documents were not attached."
    assert hook._grounding_violations(draft, CONTEXT, prompt="commit this") == []


def test_a_draft_inventing_a_file_is_caught(hook):
    """The 2026-09-06 failure: routed reviews naming tests that do not exist."""
    draft = "See tests/test_persona_attachment_smoke.py for the regression coverage."
    bad = hook._grounding_violations(draft, CONTEXT, prompt="commit this")
    assert "tests/test_persona_attachment_smoke.py" in bad


def test_a_file_named_in_the_prompt_counts_as_grounded(hook):
    """The user can introduce a path the context does not have."""
    draft = "src/llm_router/okf.py defines find_relevant."
    assert hook._grounding_violations(
        draft, CONTEXT, prompt="what does src/llm_router/okf.py do?"
    ) == []


def test_a_file_that_really_exists_in_the_repo_is_grounded(hook, tmp_path, monkeypatch):
    """Existing on disk is evidence too — the model may have read it earlier."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "real_module.py").write_text("x = 1", encoding="utf-8")
    draft = "The change belongs in pkg/real_module.py."
    assert hook._grounding_violations(draft, CONTEXT, prompt="fix it") == []


def test_several_inventions_are_all_reported(hook):
    # Not `a/...` or `b/...`: those are git diff prefixes and are stripped before
    # comparison, so using them here tested prefix handling rather than the
    # several-inventions behaviour this is named for.
    draft = "Touch pkg/one.py, lib/two.py and demo/host/identity.py."
    bad = hook._grounding_violations(draft, CONTEXT, prompt="go")
    assert set(bad) == {"pkg/one.py", "lib/two.py"}


def test_prose_without_paths_is_never_flagged(hook):
    """The check is about structure. It must not become a prose judge."""
    draft = "This refers to the S64 assessment; the next step is to commit and redeploy."
    assert hook._grounding_violations(draft, CONTEXT, prompt="commit this") == []


def test_empty_inputs_are_safe(hook):
    assert hook._grounding_violations("", CONTEXT, prompt="x") == []
    assert hook._grounding_violations("some text", "", prompt="") == []


def test_an_ungrounded_draft_is_rejected(hook):
    """The decision the check exists to drive: fall through to Claude."""
    draft = "See tests/test_invented.py."
    assert hook._draft_is_relayable(draft, CONTEXT, prompt="commit this") is False


def test_a_grounded_draft_is_accepted(hook):
    draft = "Fixed in demo/host/identity.py."
    assert hook._draft_is_relayable(draft, CONTEXT, prompt="commit this") is True


def test_the_check_can_be_disabled(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GROUNDING_CHECK", "off")
    assert hook._draft_is_relayable("See tests/test_invented.py.", CONTEXT, prompt="x") is True


def test_a_broken_check_does_not_reject_everything(hook, monkeypatch):
    """Fail-open: a bug in the guard must not silently stop all routing."""
    monkeypatch.setattr(hook, "_grounding_violations", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert hook._draft_is_relayable("anything", CONTEXT, prompt="x") is True


def test_git_diff_prefixes_are_not_invented_paths(hook):
    """`a/` and `b/` are git's diff prefixes, not directories.

    Found by the S2-5b quality check: a draft quoting a diff of
    `demo/host/identity.py` — a file that IS in context — was reported as citing
    two invented paths, which would have rejected a correct answer. A guard with
    false positives silently costs routing opportunities and is hard to notice,
    because the fallthrough looks exactly like a model that just did not answer.
    """
    ctx = "[assistant] Fixed persona documents in demo/host/identity.py."
    draft = "```diff\n--- a/demo/host/identity.py\n+++ b/demo/host/identity.py\n```"
    assert hook._grounding_violations(draft, ctx, prompt="commit this") == []


def test_a_genuinely_invented_path_under_a_prefix_is_still_caught(hook):
    """Stripping the prefix must not become a way to smuggle anything through."""
    ctx = "[assistant] Fixed persona documents in demo/host/identity.py."
    draft = "--- a/demo/totally_invented.py"
    assert hook._grounding_violations(draft, ctx, prompt="x") == ["demo/totally_invented.py"]
