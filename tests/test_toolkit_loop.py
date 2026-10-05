"""Router-owned loop, verifier, patch, ledger, CLI: real workspace, real subprocesses, scripted model."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from llm_router.toolkit import sandbox
from llm_router.toolkit.adapters.ollama import AdapterError, AdapterReply
from llm_router.toolkit.loop import Budgets, run_task, tree_fingerprint
from tests.toolkit_fixtures import PY_DIR, digest, make_source

pytestmark = pytest.mark.timeout(180)
SANDBOX_OK = sandbox.prove_sandbox().proven
needs_sandbox = pytest.mark.skipif(not SANDBOX_OK, reason="sandbox not proven")

VERIFY = "pytest -q tests/test_pkg.py"


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class Scripted:
    """An adapter that replays tool calls. One call per reply, like a real model."""
    model = "scripted"
    num_ctx = 25000

    def __init__(self, calls, final="done"):
        self.calls = list(calls)
        self.final = final
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools, *, timeout_s):
        self.seen.append(list(messages))
        if not self.calls:
            return AdapterReply(content="", tool_calls=[call("finish", summary=self.final)],
                                message={"role": "assistant", "content": "",
                                         "tool_calls": [call("finish", summary=self.final)]},
                                tokens_in=10, tokens_out=5)
        c = self.calls.pop(0)
        return AdapterReply(content="", tool_calls=[c], message={"role": "assistant", "content": "",
                                                                  "tool_calls": [c]}, tokens_in=100, tokens_out=20)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.delenv("LLM_ROUTER_TOOLLAYER", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:1")
    src = make_source(tmp_path / "source")
    return tmp_path, src


FIX = [call("read", path="src/pkg.py"),
       call("edit", path="src/pkg.py", edits=[{"old_string": "return a - b", "new_string": "return a + b"}]),
       call("bash", command=VERIFY)]


@needs_sandbox
def test_fix_task_is_verified_used_and_propose_only(iso):
    tmp, src = iso
    before = digest(src)
    fp = tree_fingerprint(src)
    res = run_task("fix add", adapter=Scripted(FIX), source=src, verify_cmd=VERIFY, python_dir=PY_DIR,
                   workspace_parent=tmp)
    assert res.status == "done" and res.used is True and res.verified_by == "V1", res.verify
    assert res.verify["baseline_rc"] != 0 and res.verify["after_rc"] == 0
    assert res.changed_files == ["src/pkg.py"] and res.source_modified is False
    assert digest(src) == before and tree_fingerprint(src) == fp          # propose-only: tree untouched
    assert "+    return a + b" in res.patch_text
    assert stat.S_IMODE(os.stat(res.patch_path).st_mode) == 0o600
    # the patch applies to the original tree
    p = subprocess.run(["git", "apply", "--check", "-"], input=res.patch_text, text=True, cwd=src,
                       capture_output=True)
    assert p.returncode == 0, p.stderr
    # ledger: verify + used recorded; ledger file lines for every call and every decision
    row = sqlite3.connect(str(tmp / "ledger.db")).execute(
        "SELECT verify, used, provider, model FROM execution_events WHERE route_id=?", (res.run_id,)).fetchone()
    assert row == ("V1:pass", 1, "ollama", "scripted")
    lines = [json.loads(x) for x in Path(res.ledger_path).read_text().splitlines()]
    assert {x["kind"] for x in lines} == {"call", "decision"} and sum(x["kind"] == "call" for x in lines) == 4
    assert stat.S_IMODE(os.stat(res.ledger_path).st_mode) == 0o600


@needs_sandbox
def test_without_a_verifier_used_is_unknown_not_false(iso):
    tmp, src = iso
    res = run_task("fix add", adapter=Scripted(FIX[:2]), source=src, python_dir=PY_DIR, workspace_parent=tmp)
    assert res.used is None and res.verify is None and res.verified_by is None
    row = sqlite3.connect(str(tmp / "ledger.db")).execute(
        "SELECT verify, used FROM execution_events WHERE route_id=?", (res.run_id,)).fetchone()
    assert row == (None, None), "unknown must be NULL in the ledger, never 0"


@needs_sandbox
def test_a_change_that_does_not_fix_is_not_used(iso):
    tmp, src = iso
    calls = [call("edit", path="src/pkg.py", edits=[{"old_string": "return a - b", "new_string": "return a * b"}])]
    res = run_task("fix add", adapter=Scripted(calls), source=src, verify_cmd=VERIFY, python_dir=PY_DIR,
                   workspace_parent=tmp)
    assert res.used is False and "failed" in res.verify["reason"]


@needs_sandbox
def test_models_own_done_never_sets_used(iso):
    tmp, src = iso
    res = run_task("fix add", adapter=Scripted([], final="all fixed, trust me"), source=src, verify_cmd=VERIFY,
                   python_dir=PY_DIR, workspace_parent=tmp)
    assert res.status == "done" and res.used is False        # empty patch, red baseline


@needs_sandbox
def test_editing_the_frozen_test_raises_a_safety_flag_and_blocks_used(iso):
    tmp, src = iso
    calls = [call("edit", path="tests/test_pkg.py", edits=[{"old_string": "== 3", "new_string": "== -1"}]),
             call("edit", path="src/pkg.py", edits=[{"old_string": "return a - b", "new_string": "return a + b"}])]
    res = run_task("fix add", adapter=Scripted(calls), source=src, verify_cmd=VERIFY, python_dir=PY_DIR,
                   workspace_parent=tmp)
    assert "protected" in res.safety_flags
    assert res.used is False, "a run that touched a frozen test must not count as used"


def test_fail_closed_bash_off_and_verifier_does_not_run(iso, monkeypatch):
    tmp, src = iso
    monkeypatch.setattr(sandbox, "prove_sandbox",
                        lambda refresh=False: sandbox.SandboxStatus(False, "forced: no sandbox", {}))
    calls = [call("bash", command="ls")] + FIX[:2]
    res = run_task("fix add", adapter=Scripted(calls), source=src, verify_cmd=VERIFY, python_dir=PY_DIR,
                   workspace_parent=tmp)
    assert res.bash_enabled is False and "forced" in res.bash_reason
    assert res.used is None and res.verify["ran"] is False, "no sandbox: the verifier must not run unsandboxed"
    lines = [json.loads(x) for x in Path(res.ledger_path).read_text().splitlines()]
    assert any(x.get("rule") == "bash_off" and not x.get("allow") for x in lines)


def test_kill_switch_stops_the_loop_before_the_next_call(iso, monkeypatch):
    tmp, src = iso

    class Killer(Scripted):
        def chat(self, messages, tools, *, timeout_s):
            monkeypatch.setenv("LLM_ROUTER_TOOLLAYER", "off")
            return super().chat(messages, tools, timeout_s=timeout_s)

    res = run_task("x", adapter=Killer([call("read", path="README.md")]), source=src, workspace_parent=tmp)
    assert res.status == "kill" or res.denials >= 1
    assert not res.changed_files


def test_kill_switch_before_start_refuses_to_run(iso, monkeypatch):
    tmp, src = iso
    monkeypatch.setenv("LLM_ROUTER_TOOLLAYER", "off")
    res = run_task("x", adapter=Scripted([]), source=src, workspace_parent=tmp)
    assert res.status == "kill" and res.steps == 0


def test_step_budget_ends_a_runaway(iso):
    tmp, src = iso
    calls = [call("list", path=".", depth=i % 3 + 1) for i in range(50)] + \
            [call("read", path="README.md", offset=i) for i in range(50)]
    res = run_task("x", adapter=Scripted(calls), source=src, budgets=Budgets(max_steps=5),
                   workspace_parent=tmp)
    assert res.status == "budget" and res.stop_reason == "max_steps" and res.steps == 5


def test_token_budget_ends_the_loop(iso):
    tmp, src = iso
    calls = [call("read", path="README.md", offset=i) for i in range(20)]
    res = run_task("x", adapter=Scripted(calls), source=src, budgets=Budgets(max_tokens=300),
                   workspace_parent=tmp)
    assert res.stop_reason == "token_budget" and res.tokens_in is not None


def test_repeated_identical_calls_are_stopped_by_the_loop_guard(iso):
    tmp, src = iso
    res = run_task("x", adapter=Scripted([call("read", path="README.md")] * 20), source=src,
                   workspace_parent=tmp)
    assert res.stop_reason == "repeat_loop"


def test_time_budget_ends_the_loop(iso):
    tmp, src = iso
    res = run_task("x", adapter=Scripted([call("list")] * 3), source=src, budgets=Budgets(max_seconds=0.0),
                   workspace_parent=tmp)
    assert res.stop_reason == "time_budget"


def test_adapter_failure_is_reported_not_raised(iso):
    tmp, src = iso

    class Down(Scripted):
        def chat(self, *a, **k):
            raise AdapterError("llm_unreachable: nope")

    res = run_task("x", adapter=Down([]), source=src, workspace_parent=tmp)
    assert res.status == "error" and "llm_unreachable" in res.stop_reason


def test_workspace_is_removed_after_the_run(iso):
    tmp, src = iso
    run_task("x", adapter=Scripted([]), source=src, workspace_parent=tmp)
    assert not [p for p in tmp.iterdir() if p.name.startswith("llmr-toolkit-")]


def test_patch_handles_new_and_deleted_files(iso):
    tmp, src = iso
    from llm_router.toolkit.result import make_patch
    ws = sandbox.create_workspace(src, parent=tmp)
    (ws.root / "new.txt").write_text("hello\n")
    (ws.root / "notes.txt").unlink()
    (ws.root / "README.md").write_text("# changed\n")
    patch, changed = make_patch(ws.baseline, ws.root)
    assert sorted(changed) == ["README.md", "new.txt", "notes.txt"]
    assert "new file mode" in patch and "deleted file mode" in patch
    p = subprocess.run(["git", "apply", "--check", "-"], input=patch, text=True, cwd=src, capture_output=True)
    assert p.returncode == 0, p.stderr


# ── the ledger columns ───────────────────────────────────────────────────────


def test_old_ledger_db_is_migrated_with_null_verify_and_used(tmp_path):
    from llm_router import execution_ledger as L
    db = tmp_path / "usage.db"
    legacy_ddl = L._DDL.replace("adoption_method TEXT,\n    verify TEXT,\n    used INTEGER\n",
                                "adoption_method TEXT\n")
    assert legacy_ddl != L._DDL
    raw = sqlite3.connect(str(db))
    raw.executescript(legacy_ddl)
    raw.execute("INSERT INTO execution_events (schema_version, event_id, ts, event_type) "
                "VALUES (1,'old',1.0,'route_completed')")
    raw.commit()
    cols = {r[1] for r in raw.execute("PRAGMA table_info(execution_events)")}
    raw.close()
    assert "verify" not in cols and "used" not in cols
    _connect = L._connect
    conn = _connect(db)
    row = conn.execute("SELECT verify, used FROM execution_events WHERE event_id='old'").fetchone()
    conn.close()
    assert row == (None, None)


# ── CLI against a fake Ollama ────────────────────────────────────────────────


class _FakeOllama(BaseHTTPRequestHandler):
    script: list[dict] = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        msg = {"role": "assistant", "content": "", "tool_calls": [type(self).script.pop(0)]} \
            if type(self).script else {"role": "assistant", "content": "", "tool_calls": [call("finish", summary="ok")]}
        body = json.dumps({"message": msg, "prompt_eval_count": 50, "eval_count": 7}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


@needs_sandbox
def test_cli_run_end_to_end_against_a_fake_ollama(iso, monkeypatch, capsys):
    tmp, src = iso
    _FakeOllama.script = list(FIX)
    srv = HTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(sandbox, "install_signal_handlers", lambda: None)
    from llm_router.commands.run import cmd_run
    try:
        rc = cmd_run(["--model", "ollama/fake:1b", "--verify", VERIFY, "--workspace", str(src),
                      "--python-dir", PY_DIR, "fix add()"])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "VERIFIED" in out and "propose-only" in out and "patch.diff" in out
    assert (src / "src" / "pkg.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_cli_usage_and_kill_exit_codes(iso, monkeypatch, capsys):
    from llm_router.commands.run import cmd_run
    assert cmd_run(["--model", "x", "   "]) == 2
    monkeypatch.setenv("LLM_ROUTER_TOOLLAYER", "off")
    assert cmd_run(["--model", "x", "do it"]) == 3
    monkeypatch.delenv("LLM_ROUTER_TOOLLAYER")
    kill = sandbox.kill_file()
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text("x")
    assert cmd_run(["--model", "x", "do it"]) == 3


def test_cli_is_dispatched_by_the_main_entry(monkeypatch):
    import llm_router.cli as cli
    seen = {}
    monkeypatch.setattr("llm_router.commands.run.cmd_run", lambda a: seen.setdefault("a", a) and 0)
    monkeypatch.setattr("sys.argv", ["llm-router", "run", "--model", "m", "task"])
    with pytest.raises(SystemExit):
        cli.main()
    assert seen["a"] == ["--model", "m", "task"]


# ── agent_loop is a thin caller of the toolkit ───────────────────────────────


def test_agent_loop_delegates_to_the_toolkit_executor(tmp_path):
    from llm_router.hooks import agent_loop
    from llm_router.toolkit import tools
    assert agent_loop._parse_command_line is tools.parse_command_line
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    assert "hello" in agent_loop.execute_tool("read_file", {"path": "a.txt"}, tmp_path)
    assert "not available" in agent_loop.execute_tool("read_file", {"path": ".env"}, tmp_path)
    assert ".env" not in agent_loop.execute_tool("list_files", {"path": ".", "pattern": ".*"}, tmp_path)


def test_one_executor_no_second_subprocess_runner_in_agent_loop():
    import ast
    src = Path(__file__).resolve().parents[1] / "src/llm_router/hooks/agent_loop.py"
    tree = ast.parse(src.read_text())
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not [c for c in calls if c.startswith("subprocess.")], "agent_loop spawns its own processes again"
    tools_src = (src.parent.parent / "toolkit" / "tools.py").read_text()
    assert "shell=True" not in tools_src


def test_repair_shim_is_shared_and_still_recovers_xml_and_json_calls():
    from llm_router.hooks import agent_loop
    xml = "<function=read_file>\n<parameter=path>\na.py\n</parameter>\n</function>"
    assert agent_loop._repair_toolcalls(xml)[0]["function"]["name"] == "read_file"
    js = '{"name": "write_file", "arguments": {"path": "x", "content": "y"}}'
    assert agent_loop._repair_toolcalls(js)[0]["function"]["arguments"]["path"] == "x"


def test_normalize_call_repairs_mechanically_and_never_invents():
    from llm_router.toolkit.tools import normalize_call
    n, a, r = normalize_call("Read_File", '{"path": "a.py", "offset": "3", "bogus": 1}')
    assert (n, a) == ("read", {"path": "a.py", "offset": 3}) and r
    n, a, _ = normalize_call("edit", {"path": "a.py", "old_string": "x", "new_string": "y"})
    assert a["edits"] == [{"old_string": "x", "new_string": "y"}]
    n, a, _ = normalize_call("read", {})
    assert a == {}


# ── a busy Ollama must not look like a model that gave up ───────────────────


def test_empty_server_replies_are_retried_not_treated_as_a_model_decision(iso, monkeypatch):
    tmp, src = iso
    import llm_router.toolkit.loop as loop_mod
    monkeypatch.setattr(loop_mod, "_TRANSIENT_SLEEP_S", 0.0)
    state = {"n": 0}

    class Flaky(Scripted):
        def chat(self, messages, tools, *, timeout_s):
            state["n"] += 1
            if state["n"] <= 2:
                raise AdapterError("empty_reply: ollama returned no message and done=false", retryable=True)
            return super().chat(messages, tools, timeout_s=timeout_s)

    res = run_task("x", adapter=Flaky([call("write", path="a.txt", content="hi")]), source=src, workspace_parent=tmp)
    assert res.status == "done" and res.changed_files == ["a.txt"] and res.steps == 2


def test_persistent_server_faults_end_the_run_as_an_error_not_a_blocked_task(iso, monkeypatch):
    tmp, src = iso
    import llm_router.toolkit.loop as loop_mod
    monkeypatch.setattr(loop_mod, "_TRANSIENT_SLEEP_S", 0.0)

    class Dead(Scripted):
        def chat(self, *a, **k):
            raise AdapterError("empty_reply", retryable=True)

    res = run_task("x", adapter=Dead([]), source=src, workspace_parent=tmp)
    assert res.status == "error" and "empty_reply" in res.stop_reason


def test_ollama_adapter_turns_an_empty_done_false_reply_into_a_retryable_error(monkeypatch):
    import io
    from llm_router.toolkit.adapters import ollama as O
    body = json.dumps({"model": "", "message": {"role": "", "content": ""}, "done": False}).encode()
    monkeypatch.setattr(O.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    monkeypatch.setattr(O, "check_overflow", lambda *a, **k: None)
    with pytest.raises(AdapterError) as ei:
        O.OllamaAdapter("m").chat([{"role": "user", "content": "x"}], [], timeout_s=5)
    assert ei.value.retryable
