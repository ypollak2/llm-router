"""P1.2 rolling summary: restart, compaction, cadence, spawn p95, opt-in inertness."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


def test_survives_restart(tmp_path, db):
    t = _write(tmp_path / "t.jsonl", convo(8))
    cs.maybe_update("s1", t, model=None, db=db)
    # a fresh process reads the same file-backed db
    code = ("import sys; from llm_router import context_summary as c; "
            "r=c.load_summary('s1'); print(r['upto_msg_idx'], r['source'])")
    env = dict(os.environ, HOME=str(tmp_path), LLM_ROUTER_SESSIONS_PATH=str(db))
    out = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True,
                         cwd=str(tmp_path))
    assert out.stdout.split() == ["16", "extractive"], out.stderr


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


def test_pick_model(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_MODEL", raising=False)
    assert cs.pick_model(["llmr-edit:latest", "nomic-embed-text", "qwen3-coder:30b"]) is None
    assert cs.pick_model(["llmr-edit:latest", "qwen3.5:9b", "llama3:8b"]) == "qwen3.5:9b"
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_MODEL", "x:1")
    assert cs.pick_model([]) == "x:1"


def test_spawn_cooldown_and_p95_over_100_spawns(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    argv = [sys.executable, "-I", "-c", "pass"]
    times = []
    for i in range(100):
        t0 = time.perf_counter()
        assert cs.spawn(f"sess-{i}", "/x/t.jsonl", argv=argv) is True
        times.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.5)
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass
    times.sort()
    p95 = times[94]
    print(f"spawn p95={p95:.2f} ms over n=100")
    assert p95 <= 50.0
    assert cs.spawn("sess-0", "/x/t.jsonl", argv=argv) is False  # cooldown
    assert cs.spawn("", "/x/t.jsonl", argv=argv) is False


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
