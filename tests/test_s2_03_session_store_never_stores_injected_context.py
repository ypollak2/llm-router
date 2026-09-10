"""S2-3 — the session store must never swallow its own injected context.

`okf._KNOWLEDGE_CTX_RE` exists because re-capturing an injected
`<knowledge_context>` block is a feedback loop: the block is retrieved, injected,
echoed, stored, and then retrieved again forever. OKF closes that loop on its own
write path. The session store never had the equivalent guard, and it has six write
paths.

The live proof, from this machine's own session shard:

    [user] <knowledge_context> ## [ModelCapability] gemini-2.5-pro
           Higher-quality Gemini model. Use for architecture and ...

That is not something the user typed. `router.py:4152` rebinds
``prompt = _okf.inject_context(prompt, concepts)`` before dispatch, and
`router.py:1977` then records that rebound value as the user's turn. So every
routed call with retrieval active wrote the retrieved documents into the
conversation history as though the user had said them — and `build_session_context`
serves that history back on later turns.

S2-2 stripped blocks on the one path it added. That was the narrow fix. This is the
general one: the guard belongs in `record_event`, so no future caller has to know
about it. Defence in depth — the six existing callers get it for free and a seventh
cannot reintroduce the loop by forgetting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router import session_store

BLOCK = (
    "<knowledge_context>\n"
    "## [ModelCapability] gemini-2.5-pro\n"
    "Higher-quality Gemini model. Use for architecture and deep reasoning.\n"
    "</knowledge_context>"
)
REAL = "so what actually broke the reconciler?"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "s2-03-scope")
    yield


def _bodies(sid: str) -> str:
    return " ".join(e.get("content", "") for e in session_store.load_events(sid, limit=50))


def test_injected_block_is_stripped_from_a_recorded_prompt():
    """The exact live failure: router.py records the OKF-rebound prompt."""
    sid = "s2-03-a"
    session_store.record_event(sid, "user_prompt", f"{BLOCK}\n\n{REAL}", role="user")
    body = _bodies(sid)
    assert "ModelCapability" not in body, "an injected block became conversation history"
    assert "knowledge_context" not in body
    assert "reconciler" in body, "stripping the block also destroyed the real prompt"


def test_stripping_applies_to_assistant_turns_too():
    """A model that echoes its context back must not have that stored either."""
    sid = "s2-03-b"
    session_store.record_event(
        sid, "routed_qa", f"As shown:\n{BLOCK}\nthe answer is FOO_MARKER.",
        role="assistant",
    )
    body = _bodies(sid)
    assert "ModelCapability" not in body
    assert "FOO_MARKER" in body


def test_a_turn_that_is_only_an_injected_block_is_not_recorded():
    """Nothing of the user's remains, so there is no turn worth keeping."""
    sid = "s2-03-c"
    session_store.record_event(sid, "user_prompt", BLOCK, role="user")
    assert session_store.load_events(sid) == []


def test_multiple_blocks_in_one_turn_are_all_removed():
    sid = "s2-03-d"
    session_store.record_event(
        sid, "user_prompt", f"{BLOCK}\nmiddle KEEP_ME text\n{BLOCK}", role="user"
    )
    body = _bodies(sid)
    assert "ModelCapability" not in body
    assert "KEEP_ME" in body


def test_ordinary_content_is_untouched():
    """The guard must not corrupt normal turns — most turns are normal."""
    sid = "s2-03-e"
    text = "the gate skips ~48% of prompts; see src/llm_router/okf.py find_relevant"
    session_store.record_event(sid, "user_prompt", text, role="user")
    assert text in _bodies(sid)


def test_the_block_never_reaches_the_context_a_model_is_given():
    """What this is ultimately protecting: the block must not be served back."""
    sid = "s2-03-f"
    session_store.record_event(sid, "user_prompt", f"{BLOCK}\n\n{REAL}", role="user")
    ctx = session_store.build_session_context(
        sid, max_tokens=1500, task_type="code",
        query="reconciler", target_provider="ollama",
    )
    assert "ModelCapability" not in ctx
    assert "gemini-2.5-pro" not in ctx


def test_case_and_whitespace_variants_are_caught():
    sid = "s2-03-g"
    session_store.record_event(
        sid,
        "user_prompt",
        "<KNOWLEDGE_CONTEXT>\n## [ModelCapability] x\n</KNOWLEDGE_CONTEXT>\n\nKEEP_G",
        role="user",
    )
    body = _bodies(sid)
    assert "ModelCapability" not in body
    assert "KEEP_G" in body
