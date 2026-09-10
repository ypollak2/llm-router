"""S2-2 — Claude's own answers must reach the durable session store.

`record_event(role="assistant")` fires in exactly two places, and neither is
Claude: `auto-route.py` records the DIRECT draft when a local model answers, and
`router.py` records a routed MCP reply. So the store accumulated the user's prompts
and the tool calls, and never what was concluded.

Measured on this machine's live session logs before the fix:

    session shard A   18 events  {'user': 9,  'assistant': 9}     <- routed turns
    session shard B   65 events  {'user': 1,  'tool': 64}         <- Claude turns
    session shard C    8 events  {'tool': 8}

The shards where Claude did the work carry tool calls and no conclusions. A model
reading that context sees a user ask for something, sees files being read, and
never learns what the answer was — which is the half of the conversation that
resolves "did that work?" or "commit this".

The hook already reads Claude's turns out of the Claude Code transcript
(`_load_conversation_history`) to feed the immediate DIRECT draft. They were simply
never persisted, so anything reading the durable store later — a routed MCP call, a
subsequent session — could not see them.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from llm_router import session_store

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("s2_02_auto_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["s2_02_auto_route"] = mod
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
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "s2-02-scope")
    yield


def _transcript(tmp_path: Path, turns: list[tuple[str, str]]) -> str:
    p = tmp_path / "transcript.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for role, text in turns:
            fh.write(json.dumps({
                "type": role,
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            }) + "\n")
    return str(p)


CONCLUSION = "The timeout was the defect: OLLAMA_TIMEOUT was 4s and every local p50 exceeds it."


def test_claudes_answer_reaches_the_durable_store(hook, tmp_path):
    """The core gap: Claude concluded something and the store never learned it."""
    sid = "s2-02-sess-a"
    t = _transcript(tmp_path, [
        ("user", "why did routing collapse?"),
        ("assistant", CONCLUSION),
    ])
    hook._persist_assistant_turns(t, sid, current_prompt="what next?")

    events = session_store.load_events(sid, limit=50)
    assistant = [e for e in events if e.get("role") == "assistant"]
    assert assistant, "Claude's turn was not recorded at all"
    assert any(CONCLUSION[:40] in (e.get("content") or "") for e in assistant)


def test_it_reaches_the_context_block_a_routed_model_sees(hook, tmp_path):
    """Persisting is only useful if build_session_context surfaces it."""
    sid = "s2-02-sess-b"
    t = _transcript(tmp_path, [
        ("user", "why did routing collapse?"),
        ("assistant", CONCLUSION),
    ])
    hook._persist_assistant_turns(t, sid, current_prompt="what next?")

    ctx = session_store.build_session_context(
        sid, max_tokens=1500, task_type="code",
        query="timeout routing", target_provider="ollama",
    )
    assert "OLLAMA_TIMEOUT" in ctx, "the conclusion never reached the routed model"


def test_repeated_calls_do_not_duplicate(hook, tmp_path):
    """The hook fires once per prompt over a transcript that keeps growing."""
    sid = "s2-02-sess-c"
    t = _transcript(tmp_path, [
        ("user", "why did routing collapse?"),
        ("assistant", CONCLUSION),
    ])
    for _ in range(3):
        hook._persist_assistant_turns(t, sid, current_prompt="what next?")

    matches = [
        e for e in session_store.load_events(sid, limit=50)
        if e.get("role") == "assistant" and CONCLUSION[:40] in (e.get("content") or "")
    ]
    assert len(matches) == 1, f"recorded {len(matches)} copies of one turn"


def test_only_the_newest_unseen_turns_are_written(hook, tmp_path):
    """A long transcript must not be replayed into the store on every prompt."""
    sid = "s2-02-sess-d"
    turns = []
    for i in range(20):
        turns.append(("user", f"question {i}"))
        turns.append(("assistant", f"answer {i}"))
    t = _transcript(tmp_path, turns)
    hook._persist_assistant_turns(t, sid, current_prompt="next")

    recorded = [e for e in session_store.load_events(sid, limit=100) if e.get("role") == "assistant"]
    assert 0 < len(recorded) <= 6, f"replayed {len(recorded)} assistant turns"
    joined = " ".join(e.get("content", "") for e in recorded)
    assert "answer 19" in joined, "the most recent conclusion was not among them"


def test_injected_knowledge_blocks_are_never_persisted(hook, tmp_path):
    """S2-3 overlaps here: an injected block must not become session history.

    A `<knowledge_context>` block was found recorded as a real turn in this
    machine's live data. Re-persisting injected material is the self-poisoning
    loop `okf._KNOWLEDGE_CTX_RE` exists to close.
    """
    sid = "s2-02-sess-e"
    poisoned = (
        "<knowledge_context>\n## [ModelCapability] gemini-2.5-pro\nHigher-quality "
        "Gemini model.\n</knowledge_context>\n\nThe real conclusion is that FOO_MARKER holds."
    )
    t = _transcript(tmp_path, [("user", "q"), ("assistant", poisoned)])
    hook._persist_assistant_turns(t, sid, current_prompt="next")

    bodies = " ".join(
        e.get("content", "") for e in session_store.load_events(sid, limit=50)
    )
    assert "ModelCapability" not in bodies, "an injected block became session history"
    assert "FOO_MARKER" in bodies, "stripping the block also lost the real content"


def test_a_missing_transcript_is_not_an_error(hook, tmp_path):
    """Zero-Claude sessions have no transcript; persistence is best-effort."""
    hook._persist_assistant_turns(str(tmp_path / "nope.jsonl"), "s2-02-sess-f", current_prompt="x")
    assert session_store.load_events("s2-02-sess-f") == []


def test_no_session_id_is_a_noop(hook, tmp_path):
    t = _transcript(tmp_path, [("assistant", CONCLUSION)])
    hook._persist_assistant_turns(t, "", current_prompt="x")  # must not raise
