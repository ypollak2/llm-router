"""W: a draft Claude only saw as a hint must never become "what Claude said".

Found 2026-09-25: a local draft that was DISCARDED (echo mode — Claude answered
itself) was appended to the per-session transcript shard as an assistant turn,
then persisted into the session store as `claude_answer`, and fed to later
drafts as the conversation. Two later drafts repeated its fabricated claim
("prepare_prompt was called without project_dir … fixed") as fact.

The shard exists for turns the hook ANSWERED (block/zero-Claude mode: the
prompt never reached Claude, so the draft is the only record of the reply). In
echo mode the draft is not the answer and must not be recorded as one.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
DRAFT = "The fix was to pass project_dir into prepare_prompt; routing now works for 80% of prompts."


def _load():
    cached = sys.modules.get("auto_route_w")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_w", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_w"] = module
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, tmp_path, env):
    ar = _load()
    home = tmp_path / ".llm-router"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setattr(ar, "_router_dir", lambda: home, raising=False)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "1",
                 "LLM_ROUTER_ENFORCE": "suggest", **env}.items():
        monkeypatch.setenv(k, v)
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de
    model = de.ModelSpec(provider="ollama", model="fake-model")
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(cb, "build_chain", lambda c, z, t: [model])
    monkeypatch.setattr(cb, "needs_claude_tools", lambda p, t: False)
    res = de.DirectResult(text=DRAFT, model=model, latency_ms=5)
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: res)
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: res)
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": "What does os.path.join do?", "session_id": "sess-w1x2y3"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit:
        pass
    assert any("DIRECT SUCCESS" in line for line in log), "premise: a draft was produced"
    shard = home / "transcript_sess-w1x2y3.jsonl"
    return shard.read_text() if shard.exists() else ""


def test_a_draft_offered_as_a_hint_is_not_recorded_as_the_answer(monkeypatch, tmp_path):
    shard = _run(monkeypatch, tmp_path, {"LLM_ROUTER_ZERO_CLAUDE": "off"})
    assert DRAFT not in shard, "an unused hint was written into the conversation history"


def test_a_draft_that_answered_the_turn_is_still_recorded(monkeypatch, tmp_path):
    shard = _run(monkeypatch, tmp_path, {"LLM_ROUTER_ZERO_CLAUDE": "on"})
    assert DRAFT in shard, "a blocked turn's only record is the draft"
