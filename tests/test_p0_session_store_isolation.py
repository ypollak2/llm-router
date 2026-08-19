"""P0 — session state must land inside whatever sandbox the caller established.

WHAT WENT WRONG
---------------
A full-suite run failed on `test_context.py::test_session_store_import_failure_is_fail_open`
with an assertion message containing REAL routed content from the developer's own machine:

    assert 'still works' in '[llm_router-session-context]\\nROUTED(openai/gpt-4o-mini): ...'

That text was not produced by the test. It came from
`~/.llm-router/projects/<id>/session_context_<session>.jsonl` — the live session store of the
Claude Code session that was running the suite. The suite read the developer's actual
prompts and model outputs, injected them into a test's messages, and printed them into
the log.

THE DEFECT: THREE SANDBOX MECHANISMS, AND THIS MODULE HONOURED THE UNUSED ONE

    mechanism                     used by                          honoured here
    LLM_ROUTER_HOME (paths.py)        6 test files, is_isolated()      no
    pathlib.Path.home patch       88 test files, conftest:315,329  no
    HOME env var                  nothing in this suite            yes

`_state_dir()` called `os.path.expanduser("~")`. That reads the HOME environment
variable, so replacing the `pathlib.Path.home` METHOD — what conftest actually does —
leaves it pointing at the real home. Its docstring said "so monkeypatched HOME works",
which is true of a mechanism the suite does not use.

WHY THIS IS WORSE THAN A FLAKY TEST
-----------------------------------
`is_isolated()` exists, in its own words, "so a destructive test can *assert* its sandbox
took effect instead of assuming it did — the assumption is what caused the incident." It
returns True under LLM_ROUTER_HOME while this module writes and reads outside that sandbox.
A guard that reports clean while blind is the recurring defect class of this audit, and
here it guarded the store holding user prompt text.

Beyond tests: any deployment relocating state with LLM_ROUTER_HOME — a sandbox, a CI runner,
a multi-tenant host — had session context escape to the real home regardless.

WHAT THESE TESTS ASSERT
-----------------------
That a sandbox, established by EITHER supported mechanism, actually contains session
state. They assert on resolved paths and on a written file's location — observable
facts — never on the implementation that computes them.
"""

from __future__ import annotations

import pathlib
import uuid
from unittest.mock import patch

import llm_router.session_store as session_store
from llm_router.paths import llm_router_home, is_isolated


class TestLLMRouterHomeIsHonoured:
    """The canonical mechanism. `is_isolated()` must not be able to lie."""

    def test_state_dir_follows_llm_router_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated()
        assert session_store._state_dir() == tmp_path

    def test_project_dir_is_inside_the_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert tmp_path in session_store._project_dir().parents

    def test_session_and_pointer_paths_are_inside_the_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert tmp_path in session_store._session_path("sess-1").parents
        assert tmp_path in session_store._pointer_path().parents

    def test_isolated_state_dir_agrees_with_the_canonical_helper(self, tmp_path, monkeypatch):
        """Two implementations of "where does state live" must not disagree.

        They did: `is_isolated()` returned True while `_state_dir()` pointed at the real
        home. Asserting they AGREE — rather than asserting each separately — is what
        makes a future divergence fail here.
        """
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert session_store._state_dir() == llm_router_home()


class TestPathHomePatchIsHonoured:
    """The mechanism 88 test files actually use, including this repo's conftest."""

    def test_state_dir_follows_a_patched_path_home(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert session_store._state_dir() == tmp_path / ".llm-router"

    def test_a_written_event_lands_in_the_sandbox_not_the_real_home(self, tmp_path):
        """The end-to-end property, and the one that actually failed.

        Reading `_state_dir()` proves where the module INTENDS to write. This proves
        where a byte went — the claim that matters after a suite wrote into the
        developer's real store.

        The leak check is keyed on a UNIQUE SESSION ID, not on searching the real store
        for the canary text. Searching by content was the first version and it was
        wrong: llm_router legitimately records the developer's tool calls, so editing this
        very file wrote the canary string into a real session log, and the test failed
        on llm_router working correctly. A per-run session id can only appear in a file this
        test caused to be created.
        """
        session_id = f"iso-sess-{uuid.uuid4()}"
        real_home_store = pathlib.Path.home() / ".llm-router"

        with patch("pathlib.Path.home", return_value=tmp_path):
            session_store.record_event(session_id, "user_prompt", "sandbox canary")

        written = list((tmp_path / ".llm-router").rglob(f"session_context_{session_id}.jsonl"))
        assert written, f"nothing written under the sandbox {tmp_path}"
        assert "sandbox canary" in written[0].read_text()

        escaped = (
            list(real_home_store.rglob(f"session_context_{session_id}.jsonl"))
            if real_home_store.exists() else []
        )
        assert not escaped, f"session state escaped the sandbox into {escaped}"
