"""P1.2 rolling summary: restart, compaction, cadence, spawn p95, opt-in inertness."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import context_summary as cs

GOOD = "\n".join(f"## {s}\nx" for s in cs.SECTIONS)


def _write(path: Path, msgs: list[dict]) -> Path:
    with open(path, "w") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")
    return path


def user(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, **kw}


def asst(text="ok", tool=None):
    blocks = [{"type": "text", "text": text}]
    if tool:
        blocks.append({"type": "tool_use", "name": tool[0], "input": {"file_path": tool[1]}})
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def convo(n_turns):
    out = []
    for i in range(n_turns):
        out += [user(f"request {i}"), asst(f"reply {i}", ("Edit", f"/r/f{i}.py"))]
    return out


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(tmp_path / "sessions.db"))
    return tmp_path / "sessions.db"


def fake_gen(model, prompt):
    return GOOD


def test_extractive_when_no_model_and_all_sections(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(8))  # 16 messages
    assert cs.maybe_update("s1", t, model=None, db=db) == "extractive"
    row = cs.load_summary("s1", db)
    assert row["source"] == "extractive" and row["upto_msg_idx"] == 16
    assert cs.has_all_sections(row["text"])
    assert "/r/f0.py" in row["text"] and "request 0" in row["text"]


def test_model_path_and_bad_model_output_falls_back(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(8))
    assert cs.maybe_update("s1", t, model="m", db=db, generate=fake_gen, free_pct=90) == "model"
    assert cs.load_summary("s1", db)["model"] == "m"
    assert cs.maybe_update("s2", t, model="m", db=db, generate=lambda m, p: "no sections", free_pct=90) == "extractive"
    boom = lambda m, p: (_ for _ in ()).throw(RuntimeError("ollama down"))  # noqa: E731
    assert cs.maybe_update("s3", t, model="m", db=db, generate=boom, free_pct=90) == "extractive"


def test_low_free_memory_never_calls_model(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(8))
    called = []
    src = cs.maybe_update("s1", t, model="m", db=db, free_pct=10.0,
                          generate=lambda m, p: called.append(1) or GOOD)
    assert src == "extractive" and not called


def test_survives_process_restart(tmp_path, db):
    """Process A summarises and exits; process B (fresh interpreter) reads it back."""
    t = _write(tmp_path / "t.jsonl", convo(8))
    env = dict(os.environ, HOME=str(tmp_path), LLM_ROUTER_SESSIONS_PATH=str(db),
               LLM_ROUTER_HOME=str(tmp_path / "home"))
    w = ("from llm_router import context_summary as c; "
         f"print(c.maybe_update('s1', {str(t)!r}, model=None))")
    r = ("from llm_router import context_summary as c; "
         "r=c.load_summary('s1'); print(r['upto_msg_idx'], r['source'], c.has_all_sections(r['text']))")
    outs = [subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True,
                           cwd=str(tmp_path)) for code in (w, r)]
    assert outs[0].stdout.split() == ["extractive"], outs[0].stderr
    assert outs[1].stdout.split() == ["16", "extractive", "True"], outs[1].stderr


def test_compaction_stores_host_summary_and_continues(tmp_path, db):
    msgs = convo(6) + [user("HOST SUMMARY TEXT", isCompactSummary=True, isMeta=True)] + convo(6)
    t = _write(tmp_path / "t.jsonl", msgs)
    ci = 12
    # first pass sees the compaction marker and adopts it
    assert cs.maybe_update("s1", t, model=None, db=db) == "host_compact"
    row = cs.load_summary("s1", db)
    assert row["text"] == "HOST SUMMARY TEXT" and row["source"] == "host_compact"
    assert row["upto_msg_idx"] == ci + 1
    # second pass continues FROM it: 12 post-compaction messages, due
    assert cs.maybe_update("s1", t, model="m", db=db, generate=fake_gen, free_pct=90) == "model"
    assert cs.load_summary("s1", db)["upto_msg_idx"] == len(cs.read_messages(t))


def test_prompt_carries_previous_summary(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(8))
    cs._store("s1", 2, "PREV-SUMMARY-MARK", "m", "model", db)
    seen = []
    cs.maybe_update("s1", t, model="m", db=db, free_pct=90, generate=lambda m, p: seen.append(p) or GOOD)
    new = seen[0].split("NEW MESSAGES:")[1]
    assert "PREV-SUMMARY-MARK" in seen[0].split("NEW MESSAGES:")[0]
    assert "request 1" in new and "request 0" not in new  # only messages after prev_upto


def _msgs(n_human, n_total):
    base = [{"human": False} for _ in range(n_total)]
    for i in range(n_human):
        base[i] = {"human": True}
    return base


def test_cadence():
    assert cs.plan(_msgs(0, 8), 0) == 8            # 8 unsummarised > 7 -> due
    assert cs.plan(_msgs(3, 7), 0) is None         # 7 unsummarised, 3 human turns
    assert cs.plan(_msgs(4, 7), 0) == 7            # 4 human turns -> due
    assert cs.plan(_msgs(3, 10), 3) is None        # 7 since prev, 0 human turns
    assert cs.plan(_msgs(0, 8), 8) is None         # nothing new


def test_cadence_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_EVERY", "2")
    assert cs.summary_every() == 2
    ms = [{"human": True}, {"human": True}] + [{"human": False}] * 3
    assert cs.plan(ms, 0) == 5
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_EVERY", "junk")
    assert cs.summary_every() == cs.DEFAULT_EVERY


def test_skipped_when_not_due(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(3))  # 6 messages
    assert cs.maybe_update("s1", t, model=None, db=db) == "skipped"
    assert cs.load_summary("s1", db) is None


def test_torn_trailing_line_ignored(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(8))
    with open(t, "a") as f:
        f.write('{"type": "user", "mess')
    assert cs.maybe_update("s1", t, model=None, db=db) == "extractive"


def test_pick_model_uses_resource_map(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_MODEL", raising=False)
    row = lambda n, caps, st="connected": {"kind": "local", "status": st, "resource": f"ollama/{n}",  # noqa: E731
                                           "capabilities": caps}
    m = {"resources": [row("llama3:8b", ["generate"]), row("qwen3.5:9b", ["generate"], "absent"),
                       row("qwen3.6:x", ["embedding"]), {"kind": "claude", "status": "connected", "resource": "claude"}]}
    assert cs.pick_model(["qwen3.5:9b"], m) == "llama3:8b"   # map wins over /api/tags
    assert cs.pick_model(["qwen3.5:9b"], {"resources": []}) is None
    assert cs.pick_model(["qwen3.5:9b"], None) == "qwen3.5:9b"  # no map: tags


def test_pick_model(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_MODEL", raising=False)
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_MODEL", raising=False)
    # D-205: no family preference; excluded internals skipped, then discovery order.
    assert cs.pick_model(["llmr-edit:latest", "nomic-embed-text"]) is None
    assert cs.pick_model([]) is None
    assert cs.pick_model(["llmr-edit:latest", "zeta:9b", "alpha:8b"]) == "zeta:9b"
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "alpha:8b")        # the user's own setting wins
    assert cs.pick_model(["llmr-edit:latest", "zeta:9b", "alpha:8b"]) == "alpha:8b"
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_MODEL")
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_MODEL", "x:1")
    assert cs.pick_model([]) == "x:1"


def test_spawn_is_detached_nonblocking_and_cooled_down(tmp_path, monkeypatch):
    """Deterministic: spawn hands argv to the fork+execv helper once per session
    per cooldown and never waits on the child. No wall clock."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    calls = []
    import llm_router.statusline_tick as st
    monkeypatch.setattr(st, "_spawn_detached", lambda argv: calls.append(argv))
    argv = ["worker", "--x"]
    for i in range(100):
        assert cs.spawn(f"sess-{i}", "/x/t.jsonl", argv=argv) is True
    assert len(calls) == 100 and all(c == argv for c in calls)
    assert cs.spawn("sess-0", "/x/t.jsonl", argv=argv) is False  # cooldown
    assert cs.spawn("", "/x/t.jsonl", argv=argv) is False
    assert len(calls) == 100
    default = []
    monkeypatch.setattr(st, "_spawn_detached", lambda a: default.append(a))
    assert cs.spawn("fresh", "/t/x.jsonl") is True
    assert default[0][1:3] == ["-m", "llm_router.context_summary"]


@pytest.mark.timing
def test_spawn_p95_harness_under_50ms():
    """P1.2-a wall clock, in the serial timing lane, measured in a small separate process."""
    root = Path(cs.__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-I", str(root / "scripts/bench/context_summary_spawn_bench.py"), "100"],
                         capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(root / "src")},
                         cwd=str(root), timeout=120)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    print(r)
    assert r["n"] == 100 and r["p95_ms"] <= 50.0, r


def test_coverage_reports_n_and_informative(tmp_path, db):
    cs._store("a", 5, GOOD, "none", "extractive", db)
    r = cs.coverage({"a": 20, "b": 20, "c": 3}, db)
    assert r == {"n_sessions": 2, "covered": 1, "pct": 50.0, "informative": False}
    assert cs.coverage({}, db)["pct"] is None


def test_off_by_default_hook_does_not_import_module():
    src = (Path(cs.__file__).parent / "hooks" / "session-end.py").read_text()
    gate = src.index('os.environ.get("LLM_ROUTER_ROLLING_SUMMARY"')
    imp = src.index("import context_summary")
    assert gate < imp and "context_summary" not in src[:gate]
    assert not cs.enabled()


def test_hooks_copies_identical():
    root = Path(cs.__file__).resolve().parents[2]
    assert (root / "hooks/session-end.py").read_bytes() == (root / "src/llm_router/hooks/session-end.py").read_bytes()
