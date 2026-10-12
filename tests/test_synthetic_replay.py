"""scripts/synthetic_replay.py: the $0 synthetic-session replay harness (owner decision D-42).

What is pinned here, and why each matters:

* the stub is never bypassed: a door pointed anywhere but the stub fails the run (exit 1),
  and the guard refuses the connection before a packet leaves the process;
* every ledger row the replay causes, in every writer whose schema has a tag field,
  carries a synthetic tag, and no row claims to be real traffic;
* the deterministic half of the summary is identical for a fixed corpus;
* an empty or unreadable corpus exits non-zero without starting anything;
* the guard is baked into the children's interpreter, so a grandchild with an emptied
  environment is still guarded; network binaries on PATH are shadowed;
* leftover processes are counted with a positive control, and "cannot tell" fails the run;
* every hook hooks/hooks.json registers is driven, or listed with the reason it was not;
* no fixture text reaches the outputs (counts and hashes only).

These tests run real hook subprocesses, a real proxy and a local stub server, so they are
end-to-end, but none of them asserts a wall-clock bound (no ``timing`` mark).
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "synthetic_replay.py"
GUARD = ROOT / "scripts" / "_replay_netguard.py"
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_sessions" / "sessions.jsonl"


def _load():
    spec = importlib.util.spec_from_file_location("synthetic_replay_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sr = _load()

# One xdist worker runs the whole module (CI: --dist loadgroup), so the module-scoped
# replay fixture runs once, not once per worker.
pytestmark = pytest.mark.xdist_group("synthetic_replay")


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── corpus ────────────────────────────────────────────────────────────────────


def test_empty_corpus_exits_nonzero(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    assert sr.main(["--corpus", str(empty), "--out", str(tmp_path / "out"), "--work", str(tmp_path)]) == 2
    assert not (tmp_path / "out").exists()


def test_missing_and_malformed_corpus_exit_nonzero(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"session_id": "x", "step": "nonsense", "messages": []}\n')
    out = tmp_path / "out"
    assert sr.main(["--corpus", str(bad), "--out", str(out)]) == 2
    assert sr.main(["--corpus", str(tmp_path / "absent.jsonl"), "--out", str(out)]) == 2


def test_records_are_rebuilt_from_deltas_per_thread():
    recs = sr.load_corpus(FIXTURE)
    a = [r for r in recs if r["corpus_sid"] == "fx-a"]
    # turn 2's request is every main-thread delta before it plus its own
    t2 = next(r for r in a if r["turn"] == 2 and r["step"] == "turn_first")
    assert len(t2["request"]) == 1 + 2 + 2
    sub = next(r for r in a if r["step"] == "subagent")
    assert sub["thread"] != "main" and len(sub["request"]) == 1  # a sub-agent starts fresh
    assert all(r["sid"].startswith(sr.SESSION_PREFIX) and "fx-" not in r["sid"] for r in recs)


# ── the guard ─────────────────────────────────────────────────────────────────


def test_guard_refuses_everything_but_the_allowlist(tmp_path):
    """The sitecustomize guard, as a child gets it: the allowed port connects, any other
    port and any DNS lookup is refused before a packet is sent, and each refusal is logged."""
    violations = tmp_path / "v.jsonl"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        ok_port = listener.getsockname()[1]
        other = _closed_port()
        guard_dir = tmp_path / "guard"
        guard_dir.mkdir()
        (guard_dir / "sitecustomize.py").write_text(GUARD.read_text())
        code = (
            "import socket\n"
            f"socket.create_connection(('127.0.0.1', {ok_port}), timeout=2).close()\n"
            "for target in [('127.0.0.1', %d), ('example.org', 443)]:\n"
            "    try:\n"
            "        socket.create_connection(target, timeout=2)\n"
            "        print('CONNECTED', target)\n"
            "    except OSError:\n"
            "        print('refused')\n" % other
        )
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(guard_dir),
               "LLM_ROUTER_REPLAY_ALLOW": f"127.0.0.1:{ok_port}", "LLM_ROUTER_REPLAY_VIOLATIONS": str(violations)}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["refused", "refused"]
    rows = [json.loads(line) for line in violations.read_text().splitlines()]
    assert {(r["kind"], str(r["host"])) for r in rows} == {("connect", "127.0.0.1"), ("getaddrinfo", "example.org")}


@pytest.mark.timeout(240)
def test_a_door_pointed_off_the_stub_fails_the_run(tmp_path, monkeypatch):
    """Point the MCP door's Ollama base at a port that is not the stub: the guard refuses
    the connection and the run exits 1 with the target named in the summary."""
    off = f"http://127.0.0.1:{_closed_port()}"
    real_env = sr.Scratch.env

    def env(self, **extra):
        e = real_env(self, **extra)
        e.update({"OLLAMA_HOST": off, "OLLAMA_BASE_URL": off, "OLLAMA_URL": off, "LLM_ROUTER_OLLAMA_URL": off})
        return e

    monkeypatch.setattr(sr.Scratch, "env", env)
    rc = sr.run(FIXTURE, tmp_path / "out", {"mcp"}, sessions=1, work=tmp_path)
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert rc == 1
    net = summary["deterministic"]["network"]
    assert net["violations"] >= 1
    assert any(t.endswith(off.rsplit(":", 1)[1]) for t in net["violation_targets"])


def test_guard_is_baked_into_the_child_interpreter(tmp_path):
    """A child of the scratch interpreter with NO LLM_ROUTER_* variable (what
    safe_subprocess.get_delegated_env or agent_loop's {"PATH": ...} would hand a
    grandchild) is still guarded; curl on the scratch PATH is the shadow script."""
    import shutil

    with sr.StubServer() as stub:
        scratch = sr.Scratch(tmp_path / "root", stub, None)
        other = _closed_port()
        code = (
            "import socket, urllib.request\n"
            f"urllib.request.urlopen('{stub.url}/v1/models', timeout=5).read()\n"
            f"for target in [('127.0.0.1', {other}), ('example.org', 443)]:\n"
            "    try:\n"
            "        socket.create_connection(target, timeout=2)\n"
            "        print('CONNECTED', target)\n"
            "    except OSError:\n"
            "        print('refused')\n"
        )
        out = subprocess.run([scratch.python, "-c", code], env={"PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["refused", "refused"]
        assert stub.hits["models"] == 1  # the allowed target was reached
        assert len(scratch.violation_rows()) == 2
        env = scratch.env()
        for tool in ("curl", "wget", "nc", "security", "llm-router"):
            found = shutil.which(tool, path=env["PATH"])
            assert found and Path(found).parent == scratch.bin, tool
        assert subprocess.run(["curl", "https://example.org"], env=env, timeout=30).returncode == 1


def test_unknown_lingering_children_fail_the_run(tmp_path, monkeypatch):
    """When the process table cannot be read, leftover processes are 'unknown', never 0,
    and the run exits 1."""
    monkeypatch.setattr(sr.Scratch, "descendants", lambda self: None)
    rc = sr.run(FIXTURE, tmp_path / "out", {"gateway"}, sessions=1, work=tmp_path)
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    lingering = summary["timing"]["lingering_children"]
    assert rc == 1
    assert lingering["detectable"] is False and lingering["terminated_after_wait"] == "unknown"
    assert "lingering child processes could not be determined" in summary["deterministic"]["errors"]


def test_an_unrelated_process_naming_the_root_is_never_counted_or_signalled(tmp_path):
    """Ownership is the run marker or an exact scratch interpreter / shim argv, never a
    command-line substring: a process of another interpreter whose argv names the scratch
    root (the review's ``tail -f <root>/net_violations.jsonl``) is not counted and survives
    the reap, while a real scratch-interpreter child is counted."""
    with sr.StubServer() as stub:
        scratch = sr.Scratch(tmp_path / "root", stub, None)
        stranger = subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(60)",
                                     str(scratch.violations)], stdin=subprocess.DEVNULL)
        owned = subprocess.Popen([scratch.python, "-c", "import time; time.sleep(60)"], env={"PATH": "/usr/bin:/bin"},
                                 stdin=subprocess.DEVNULL)
        try:
            pids = None
            for _ in range(30):  # wait until both are visible to the process table
                pids = scratch.descendants()
                if pids is not None and owned.pid in pids:
                    break
                time.sleep(0.1)
            assert pids is not None and owned.pid in pids  # env-less, found by its exact argv[0]
            assert stranger.pid not in pids
            owned.kill()
            owned.wait(timeout=10)
            reaped = sr._reap(scratch, wait_s=1.0)
            assert reaped["detectable"] is True and reaped["terminated_after_wait"] == 0
            assert stranger.poll() is None  # never signalled
        finally:
            for p in (stranger, owned):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=10)


def test_scratch_venv_is_isolated_and_writes_only_inside_the_scratch_root(tmp_path, monkeypatch):
    with sr.StubServer() as stub:
        scratch = sr.Scratch(tmp_path / "ok", stub, None)
        cfg = (scratch.pyenv / "pyvenv.cfg").read_text()
        assert "include-system-site-packages = false" in cfg
        assert scratch.python != sys.executable
        assert Path(scratch.python).parent.parent == scratch.pyenv
        outside = tmp_path / "outside-site-packages"
        outside.mkdir()
        monkeypatch.setattr(sr.Scratch, "_site_packages", staticmethod(lambda python: outside.resolve()))
        with pytest.raises(RuntimeError, match="outside the scratch root"):
            sr.Scratch(tmp_path / "refused", stub, None)
        assert list(outside.iterdir()) == []  # refused before any write


def test_hook_registry_is_derived_from_hooks_json():
    reg = sr.load_hook_registry()
    raw = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    n = sum(len(g["hooks"]) for groups in raw["hooks"].values() for g in groups)
    assert len(reg) == n
    names = {(e["event"], e["hook"]) for e in reg}
    for pair in [("UserPromptSubmit", "status-bar"), ("PreToolUse", "enforce-route"),
                 ("PostToolUse", "context-capture"), ("PostToolUse", "bash-compress"),
                 ("PostToolUse", "playwright-compress"), ("PostToolUse", "usage-refresh")]:
        assert pair in names
    assert sr.matcher_matches(None, "Bash") and sr.matcher_matches("Agent", "Agent")
    assert not sr.matcher_matches("Agent", "Bash")
    assert sr.matcher_matches("llm_|mcp__llm_router__llm", "mcp__llm_router__llm_route")


def test_guard_is_removed_from_the_harness_process_after_a_run(tmp_path):
    assert sr.run(FIXTURE, tmp_path / "out", {"gateway"}, sessions=1, work=tmp_path) == 0
    assert not getattr(socket, "_llm_router_replay_guard", False)


# ── the full replay ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    base = tmp_path_factory.mktemp("replay")
    rcs, summaries = [], []
    for i in (1, 2):
        out = base / f"out{i}"
        rcs.append(sr.run(FIXTURE, out, set(sr.ALL_DOORS), work=base))
        summaries.append((json.loads((out / "summary.json").read_text()), (out / "summary.md").read_text(),
                          (out / "summary.json").read_text()))
    return rcs, summaries


@pytest.mark.timeout(300)
def test_full_replay_runs_every_door_with_no_outbound_connection(replay):
    rcs, summaries = replay
    assert rcs == [0, 0]
    det, timing = summaries[0][0]["deterministic"], summaries[0][0]["timing"]
    assert det["network"]["violations"] == 0
    assert det["errors"] == []
    # the stub really answered (a 0-violation run that never reached the stub proves nothing)
    assert timing["stub_hits"].get("anthropic_messages", 0) > 0
    for door in ("hook", "agent-route", "proxy", "gateway", "sdk", "mcp"):
        assert det["doors_summary"][door]["ok"] > 0, door
    assert det["doors_summary"]["proxy"]["calls"] == det["corpus"]["records"]
    lingering = timing["lingering_children"]
    assert lingering["detectable"] is True and lingering["canary_seen"] is True
    assert lingering["terminated_after_wait"] == 0


@pytest.mark.timeout(300)
def test_every_registered_hook_is_driven_or_explained(replay):
    det = replay[1][0][0]["deterministic"]
    hooks = det["hooks"]
    registered = sr.load_hook_registry()
    assert len(hooks["registered"]) == len(registered)
    driven = set(hooks["driven"])
    not_driven = {f"{h['event']}:{h['hook']}": h["reason"] for h in hooks["not_driven"]}
    for e in registered:
        key = f"{e['event']}:{e['hook']}"
        assert (key in driven) != (key in not_driven), key
    # the fixture has no router MCP tool call, so only the llm_-matcher hook stays undriven
    assert set(not_driven) == {"PostToolUse:usage-refresh"}
    assert "no tool in the corpus matches" in not_driven["PostToolUse:usage-refresh"]


@pytest.mark.timeout(300)
def test_every_taggable_row_is_tagged_synthetic(replay):
    det = replay[1][0][0]["deterministic"]
    tagging = det["synthetic_tagging"]
    covered = tagging["rows_in_writers_with_a_tag_field"]
    assert covered["n"] > 0 and covered["k"] == covered["n"]
    assert tagging["rows_claiming_real"] == 0
    for name, w in det["ledgers"].items():
        if w["tag_fields_in_schema"]:
            assert w["synthetic"]["k"] == w["rows"], name
    # writers that cannot carry a tag are named, never silently counted as tagged
    assert set(tagging["writers_without_any_tag_field"]) == {
        n for n, w in det["ledgers"].items() if not w["tag_fields_in_schema"]}
    assert det["ledgers"]["proxy_calls.jsonl"]["rows"] > 0


@pytest.mark.timeout(300)
def test_archive_mechanics_on_the_fixture(replay):
    checks = replay[1][0][0]["deterministic"]["archive"]["checks"]
    assert {k: v["verdict"] for k, v in checks.items()} == {
        "stop_never_archives": "PASS", "session_end_archives": "PASS", "keeps_ge5_events_when_ge5_turns": "PASS"}


@pytest.mark.timeout(300)
def test_deterministic_section_is_identical_across_runs(replay):
    (a, _, _), (b, _, _) = replay[1]
    assert a["deterministic"] == b["deterministic"]


@pytest.mark.timeout(300)
def test_outputs_carry_no_message_text(replay):
    texts = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        for m in json.loads(line)["messages"]:
            for b in m["content"] if isinstance(m["content"], list) else []:
                t = b.get("text") or (b.get("content") if isinstance(b.get("content"), str) else "")
                if t and len(t) > 12:
                    texts.append(t[:40])
    assert texts
    for _, md, raw in replay[1]:
        for t in texts:
            assert t not in md and t not in raw
