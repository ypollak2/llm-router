"""`llm-router pi` and the local Pi agent profile (integrations/pi/).

Three layers:
  * the launcher (commands/pi.py): profile lookup, models.json, argv/env, errors;
  * the profile's pure logic (integrations/pi/extensions/lib/*.mjs), run with
    `node --test` whenever Node is on PATH (GitHub's Ubuntu runners have it);
  * end-to-end runs of the real Pi binary against a scripted OpenAI server
    (integrations/pi/tests/mock_openai.py), no model involved; skipped where Pi is
    not installed, which includes CI.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm_router.commands import pi as pic

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "integrations" / "pi"


# ---------------------------------------------------------------- the profile is complete

def test_every_extension_the_launcher_loads_exists():
    assert pic.EXTENSIONS, "premise: the launcher loads at least one extension"
    for name in set(pic.EXTENSIONS) | set(pic.CHILD_EXTENSIONS):
        assert (PROFILE / "extensions" / name).is_file(), name
    assert (PROFILE / pic.SYSTEM_RULES).is_file()


def test_children_cannot_recurse_or_ask():
    assert "subagent.ts" in pic.EXTENSIONS and "question.ts" in pic.EXTENSIONS
    assert "subagent.ts" not in pic.CHILD_EXTENSIONS
    assert "question.ts" not in pic.CHILD_EXTENSIONS
    assert set(pic.CHILD_EXTENSIONS) <= set(pic.EXTENSIONS)


def test_extension_relative_imports_resolve():
    """A typo in an import path only fails inside Pi, at startup. Check them here."""
    checked = 0
    for f in (PROFILE / "extensions").rglob("*.*"):
        if f.suffix not in (".ts", ".mjs"):
            continue
        for spec in re.findall(r'from\s+"(\./[^"]+)"', f.read_text(encoding="utf-8")):
            assert (f.parent / spec).is_file(), f"{f.name}: {spec}"
            checked += 1
    assert checked >= 6, "premise: the extensions import their lib modules"


def test_agent_definitions_have_name_description_and_tools():
    agents = sorted((PROFILE / "agent" / "agents").glob("*.md"))
    assert {a.stem for a in agents} >= {"worker", "scout"}
    for a in agents:
        head = a.read_text(encoding="utf-8").split("---")[1]
        assert re.search(r"^name:\s*\S", head, re.M), a.name
        assert re.search(r"^description:\s*\S", head, re.M), a.name
        assert re.search(r"^tools:\s*\S", head, re.M), a.name


def test_settings_enable_compaction_below_the_window():
    s = json.loads((PROFILE / "agent" / "settings.json").read_text())
    c = s["compaction"]
    assert c["enabled"] is True
    # Pi compacts above contextWindow - reserveTokens; at 32k this keeps one full
    # 50 KB file read (~13-25k tokens) from pushing a prompt past the window.
    assert 8192 <= c["reserveTokens"] < 32768


def test_system_rules_cover_the_measured_failures():
    rules = (PROFILE / pic.SYSTEM_RULES).read_text(encoding="utf-8")
    assert "RELATIVE paths" in rules
    assert "`question` tool" in rules and "Never ask such a question in plain text" in rules
    assert "`subagent` tool" in rules
    assert "never a message from the user" in rules


def test_wheel_config_maps_the_profile_where_the_launcher_looks():
    import tomllib

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    fi = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert fi.get("integrations/pi") == "llm_router/_pi_profile"
    wheel_candidate = Path(pic.__file__).resolve().parents[1] / "_pi_profile"
    assert wheel_candidate.name == fi["integrations/pi"].split("/")[-1]


# ---------------------------------------------------------------- launcher

def test_models_json_declares_images_only_for_vision_models():
    v = pic.build_models_json("http://localhost:11434", "qwen3.6:35b-a3b-coding", 32768, vision=True)
    m = v["providers"]["ollama"]["models"][0]
    assert m["input"] == ["text", "image"]
    assert m["contextWindow"] == 32768
    assert v["providers"]["ollama"]["baseUrl"] == "http://localhost:11434/v1"
    t = pic.build_models_json("http://localhost:11434", "x", 8192, vision=False)
    assert t["providers"]["ollama"]["models"][0]["input"] == ["text"]


def test_max_tokens_uses_the_field_pi_reads_and_fits_small_windows():
    """Pi's models.json field is `maxTokens` (dist/core/model-config.js); an unknown key
    such as `maxOutput` validates, is dropped, and Pi then requests 16384."""
    m = pic.build_models_json("http://h:1", "x", 8192, vision=False)["providers"]["ollama"]["models"][0]
    assert "maxOutput" not in m and "output" not in m
    assert m["maxTokens"] == 2048
    big = pic.build_models_json("http://h:1", "x", 131072, vision=False)["providers"]["ollama"]["models"][0]
    assert big["maxTokens"] == pic.DEFAULT_MAX_OUTPUT


def test_models_json_keys_are_all_known_to_pi():
    """Guard against another silently ignored key: every key must be one Pi's schema lists."""
    pi_schema_keys = {"id", "name", "api", "baseUrl", "reasoning", "thinkingLevelMap", "input", "inputLimits", "cost",
                      "promptCache", "contextWindow", "maxTokens", "samplingParams", "headers", "compat"}
    m = pic.build_models_json("http://h:1", "x", 32768, vision=True)["providers"]["ollama"]["models"][0]
    assert set(m) <= pi_schema_keys, set(m) - pi_schema_keys


def test_profile_dir_override_must_contain_the_extensions(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PI_PROFILE_DIR", str(tmp_path))
    # An empty override is skipped, and the source checkout is found instead.
    assert pic.resolve_profile_dir() == PROFILE
    monkeypatch.setenv("LLM_ROUTER_PI_PROFILE_DIR", str(PROFILE))
    assert pic.resolve_profile_dir() == PROFILE


def _fake_pi(tmp_path: Path) -> Path:
    fake = tmp_path / "pi"
    fake.write_text("#!/bin/sh\necho fake-pi \"$@\"\n")
    fake.chmod(0o755)
    return fake


def _run_launcher(tmp_path, monkeypatch, capsys, *args):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_PI_BIN", str(_fake_pi(tmp_path)))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens: no network effect
    monkeypatch.delenv("LLM_ROUTER_PI_MODEL", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PI_CONTEXT", raising=False)
    rc = pic.cmd_pi(list(args))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_print_command_builds_the_full_invocation(tmp_path, monkeypatch, capsys):
    rc, out, err = _run_launcher(tmp_path, monkeypatch, capsys, "--model", "qwen3.6:35b-a3b-coding",
                                 "--context", "32768", "--vision", "--print-command", "--", "-p", "hi")
    assert rc == 0, err
    cmd = json.loads(out)
    argv, env = cmd["argv"], cmd["env"]
    assert argv[0].endswith("/pi")
    assert argv[1:6] == ["--offline", "--provider", "ollama", "--model", "qwen3.6:35b-a3b-coding"]
    loaded = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert [Path(x).name for x in loaded] == list(pic.EXTENSIONS)
    assert argv[-2:] == ["-p", "hi"]
    assert argv[argv.index("--append-system-prompt") + 1].endswith(pic.SYSTEM_RULES)
    agent_dir = Path(env["PI_CODING_AGENT_DIR"])
    assert agent_dir == tmp_path / "home" / "pi" / "agent"
    models = json.loads((agent_dir / "models.json").read_text())
    assert models["providers"]["ollama"]["models"][0]["input"] == ["text", "image"]
    assert (agent_dir / "settings.json").is_file() and (agent_dir / "agents" / "worker.md").is_file()
    assert [Path(p).name for p in env["LLM_ROUTER_PI_CHILD_EXTENSIONS"].split(os.pathsep)] == list(pic.CHILD_EXTENSIONS)
    assert env["LLM_ROUTER_PI_EVENT_LOG"].endswith("events.jsonl")
    assert "context=32768 (--context)" in err


def test_images_are_off_by_default_even_when_the_server_lists_vision(tmp_path, monkeypatch, capsys):
    """Measured: qwen3.6 with images declared gave a confident wrong code 20/20 times,
    against 10/10 explicit "cannot see" with text only. Opt-in, never automatic."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ShowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("LLM_ROUTER_PI_BIN", str(_fake_pi(tmp_path)))
        monkeypatch.setenv("OLLAMA_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        rc = pic.cmd_pi(["--model", "m", "--context", "8192", "--print-command"])
        cap = capsys.readouterr()
    finally:
        srv.shutdown()
    assert rc == 0, cap.err
    assert "server lists vision: yes" in cap.err, "premise: the server did report vision"
    agent_dir = Path(json.loads(cap.out)["env"]["PI_CODING_AGENT_DIR"])
    assert json.loads((agent_dir / "models.json").read_text())["providers"]["ollama"]["models"][0]["input"] == ["text"]
    assert "images=no" in cap.err


def test_unreachable_server_reports_vision_unknown(tmp_path, monkeypatch, capsys):
    rc, _out, err = _run_launcher(tmp_path, monkeypatch, capsys, "--model", "m", "--context", "8192", "--print-command")
    assert rc == 0, err
    assert "server lists vision: unknown" in err


def test_unknown_context_window_refuses_to_start(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    rc, _out, err = _run_launcher(tmp_path, monkeypatch, capsys, "--model", "m", "--print-command")
    assert rc == 2
    assert "context window is unknown" in err and "--context N" in err
    assert not (tmp_path / "home" / "pi" / "agent" / "models.json").exists()


def test_an_untested_pi_version_is_warned_about(tmp_path, monkeypatch, capsys):
    rc, _out, err = _run_launcher(tmp_path, monkeypatch, capsys, "--model", "m", "--context", "8192", "--print-command")
    assert rc == 0, err
    assert "this profile was built and measured against 0.99.x" in err  # the fake pi is not 0.99


def test_model_is_required(tmp_path, monkeypatch, capsys):
    rc, _out, err = _run_launcher(tmp_path, monkeypatch, capsys, "--print-command")
    assert rc == 2
    assert "--model is required" in err


def test_missing_pi_is_an_explicit_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_ROUTER_PI_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no `pi` here
    rc = pic.cmd_pi(["--model", "m", "--context", "4096", "--print-command"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "npm install -g @earendil-works/pi-coding-agent" in err


def test_unsafe_ollama_url_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_PI_BIN", str(_fake_pi(tmp_path)))
    monkeypatch.setenv("OLLAMA_BASE_URL", "file:///etc/passwd")
    rc = pic.cmd_pi(["--model", "m", "--print-command"])
    assert rc == 2
    assert "refusing the Ollama URL" in capsys.readouterr().err


def test_context_env_must_be_a_number(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PI_CONTEXT", "lots")
    with pytest.raises(pic.ProfileError):
        pic.resolve_context("http://127.0.0.1:9", "m", None)
    monkeypatch.setenv("LLM_ROUTER_PI_CONTEXT", "16384")
    assert pic.resolve_context("http://127.0.0.1:9", "m", None) == (16384, "LLM_ROUTER_PI_CONTEXT")
    assert pic.resolve_context("http://127.0.0.1:9", "m", 4096) == (4096, "--context")


class _ShowHandler(BaseHTTPRequestHandler):
    caps: list | None = ["completion", "tools", "vision"]

    def log_message(self, *_a):
        return

    def do_POST(self):  # noqa: N802
        body = json.dumps({"capabilities": self.caps} if self.caps is not None else {}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_capabilities_come_from_api_show():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ShowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert "vision" in pic.model_capabilities(url, "qwen3.6")
        _ShowHandler.caps = None
        assert pic.model_capabilities(url, "qwen3.6") is None
    finally:
        _ShowHandler.caps = ["completion", "tools", "vision"]
        srv.shutdown()
    assert pic.model_capabilities("http://127.0.0.1:9", "m") is None


def test_main_dispatches_pi_to_cmd_pi():
    tree = ast.parse((ROOT / "src" / "llm_router" / "cli.py").read_text(encoding="utf-8"))
    branches = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and "'pi'" in ast.unparse(n.test) and "args[0]" in ast.unparse(n.test)
    ]
    assert len(branches) == 1
    assert "cmd_pi(args[1:])" in ast.unparse(branches[0].body)


# ---------------------------------------------------------------- pure logic (node --test)

def test_profile_logic_unit_tests_pass_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    proc = subprocess.run([node, "--test", str(PROFILE / "tests" / "lib.test.mjs")],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)
    passed = re.search(r"^# pass (\d+)|^ℹ pass (\d+)", proc.stdout, re.M)
    n_pass = int(next(g for g in passed.groups() if g)) if passed else 0
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert n_pass >= 15, f"premise: the node suite ran its tests (pass={n_pass})\n{proc.stdout[-1500:]}"


# ---------------------------------------------------------------- end to end (real Pi, scripted server)

def _harness():
    sys.path.insert(0, str(PROFILE / "tests"))
    import pi_mock_harness as h

    if not h.find_pi():
        pytest.skip("Pi is not installed (set LLM_ROUTER_PI_BIN to run the end-to-end checks)")
    return h


def _pgrep(pattern: str) -> list[str]:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()


@pytest.mark.timeout(90)
def test_e2e_sigint_kills_the_running_shell_command(tmp_path):
    h = _harness()
    marker = f"sleep 47.{os.getpid() % 1000}"
    ws = tmp_path / "ws"
    ws.mkdir()
    srv = h.MockServer([{"tool_calls": [{"name": "bash", "arguments": {"command": f"{marker} && touch after.txt"}}]},
                        {"text": "finished"}], tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port)
        r = h.run_pi("run it", ws, tmp_path / "agent", ["cancel.ts"], sigint_after_tool="bash")
        time.sleep(1.0)
        assert r["sigint_sent"], "premise: the shell command started and SIGINT was sent"
        assert r["rc"] == 130, r["stderr"]
        assert _pgrep(marker) == [], "the shell command outlived Pi"
    finally:
        subprocess.run(["pkill", "-f", marker])
        srv.close()


@pytest.mark.timeout(90)
def test_e2e_mistyped_write_path_lands_in_the_working_directory(tmp_path):
    h = _harness()
    ws = tmp_path / "ws_54i0b6lf"
    ws.mkdir()
    real = os.path.realpath(ws)
    typo = real.replace("54i0b6lf", "54i0b61f") + "/two.txt"
    srv = h.MockServer([{"tool_calls": [{"name": "write", "arguments": {"path": real.lstrip("/") + "/one.txt", "content": "1"}}]},
                        {"tool_calls": [{"name": "write", "arguments": {"path": typo, "content": "2"}}]},
                        {"text": "ok"}], tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port)
        r = h.run_pi("write two files", ws, tmp_path / "agent", ["paths.ts"])
        assert sorted(os.listdir(ws)) == ["one.txt", "two.txt"], r["stderr"]
        notes = " ".join(x["text"] for x in h.tool_results(r["events"]))
        assert "leading \"/\" restored" in notes and "mistyped working directory corrected" in notes
    finally:
        srv.close()


@pytest.mark.timeout(90)
def test_e2e_oversized_prompt_is_refused_not_sent(tmp_path):
    h = _harness()
    ws = tmp_path / "ws"
    ws.mkdir()
    srv = h.MockServer([{"text": "must not be reached"}], tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port, context_window=8000)
        r = h.run_pi("Summarize: " + "1234567890 " * 800, ws, tmp_path / "agent", ["context-guard.ts"],
                     extra_env={"LLM_ROUTER_PI_EVENT_LOG": str(tmp_path / "ev.jsonl")})
        assert srv.requests() == [], "the oversized request reached the server"
        errors = [e["message"].get("errorMessage", "") for e in r["events"]
                  if e.get("type") == "message_end" and e["message"].get("stopReason") == "error"]
        assert errors and "prompt too long; exceeded max context length" in errors[0]
        ev = [json.loads(x) for x in (tmp_path / "ev.jsonl").read_text().splitlines()]
        assert ev and ev[0]["kind"] == "presend_refused"
    finally:
        srv.close()


@pytest.mark.timeout(120)
def test_e2e_compaction_keeps_the_request_and_shrinks_the_context(tmp_path):
    h = _harness()
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, needle_at, needle in (("a.log", 20, "AAAA1111"), ("b.log", 620, "BBBB2222")):
        lines = [f"2026-10-04T10:{i % 60:02d}:00 INFO worker-{i % 23} processed batch {1000 + i} in {i % 97}ms status=ok"
                 for i in range(640)]
        lines[needle_at] = f"CHECKSUM={needle}"
        (ws / name).write_text("\n".join(lines) + "\n")
    request = ("Read a.log fully with the read tool, then read b.log fully with the read tool. Each contains one line "
               "starting with CHECKSUM=. Report both values, labelled a.log and b.log.")
    # The extraction calls get canned answers; one of them invents a line, which must be dropped.
    script = [{"tool_calls": [{"name": "read", "arguments": {"path": "a.log"}}, {"name": "read", "arguments": {"path": "b.log"}}]}]
    script += [{"text": "CHECKSUM=AAAA1111\nCHECKSUM=INVENTED9"}] + [{"text": "NONE"}] * 7 + [{"text": "final"}]
    srv = h.MockServer(script, tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port)
        r = h.run_pi(request, ws, tmp_path / "agent", ["compaction.ts"], timeout_s=110)
        ends = [e for e in r["events"] if e.get("type") == "compaction_end" and e.get("result")]
        assert ends, f"premise: the session compacted\n{r['stderr'][-1500:]}"
        res = ends[0]["result"]
        assert res["estimatedTokensAfter"] < res["tokensBefore"] / 4
        summary = res["summary"]
        assert request in summary, "the user request is not in the summary verbatim"
        assert "OUTPUT OF TOOLS" in summary
        assert "CHECKSUM=AAAA1111" in summary and "CHECKSUM=BBBB2222" in summary
        assert "INVENTED9" not in summary
    finally:
        srv.close()


@pytest.mark.timeout(120)
def test_e2e_prose_question_guard_nudges_once_per_prompt(tmp_path):
    """Review finding: resetting on agent_start let the guard's own continuation re-arm it."""
    h = _harness()
    ws = tmp_path / "ws"
    ws.mkdir()
    prose = {"text": "The file exists. Which do you prefer?\n1. Delete it\n2. Rename it"}
    srv = h.MockServer([prose] * 6, tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port)
        r = h.run_pi("deal with legacy.cfg", ws, tmp_path / "agent", ["question.ts"], timeout_s=100)
        n = len(srv.requests())
        assert n == 2, f"expected the first answer plus exactly one nudge, got {n} model calls\n{r['stderr'][-800:]}"
        nudge = srv.requests()[1]["request"]["messages"][-1]
        assert "Call the `question` tool now" in json.dumps(nudge)
    finally:
        srv.close()


@pytest.mark.timeout(120)
def test_e2e_interrupting_the_parent_stops_the_sub_agents_shell_command(tmp_path):
    """Review finding: SIGKILL on the child's group skipped the child's cancel.ts, and its
    bash command (in its own group) survived."""
    h = _harness()
    marker = f"sleep 53.{os.getpid() % 1000}"
    ws = tmp_path / "ws"
    ws.mkdir()
    srv = h.MockServer([{"tool_calls": [{"name": "subagent", "arguments": {"task": "run the long job"}}]},
                        {"tool_calls": [{"name": "bash", "arguments": {"command": f"{marker} && touch after.txt"}}]},
                        {"text": "done"}, {"text": "done"}], tmp_path)
    child_exts = os.pathsep.join(str(PROFILE / "extensions" / n) for n in pic.CHILD_EXTENSIONS)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port)
        r = h.run_pi("delegate the long job", ws, tmp_path / "agent", ["cancel.ts", "subagent.ts"],
                     extra_env={"LLM_ROUTER_PI_CHILD_EXTENSIONS": child_exts},
                     sigint_after_tool="subagent", sigint_when_running=marker, timeout_s=100)
        assert r["sigint_sent"], "premise: the sub-agent's command started and SIGINT was sent"
        deadline = time.monotonic() + 10
        while _pgrep(marker) and time.monotonic() < deadline:
            time.sleep(0.3)
        assert _pgrep(marker) == [], "the sub-agent's shell command outlived the parent"
        assert not (ws / "after.txt").exists()
    finally:
        subprocess.run(["pkill", "-f", marker])
        srv.close()


@pytest.mark.timeout(90)
def test_e2e_a_reply_whose_prompt_filled_the_window_becomes_an_explicit_error(tmp_path):
    h = _harness()
    ws = tmp_path / "ws"
    ws.mkdir()
    srv = h.MockServer([{"text": "an answer about a truncated prompt", "prompt_tokens": 32767}], tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port)
        r = h.run_pi("hello", ws, tmp_path / "agent", ["context-guard.ts"],
                     extra_env={"LLM_ROUTER_PI_EVENT_LOG": str(tmp_path / "ev.jsonl")})
        ends = [e["message"] for e in r["events"] if e.get("type") == "message_end" and e["message"].get("role") == "assistant"]
        assert ends and ends[-1]["stopReason"] == "error"
        assert "the server reported 32767 prompt tokens" in ends[-1]["errorMessage"]
        assert "an answer about a truncated prompt" not in json.dumps(ends[-1])
        assert json.loads((tmp_path / "ev.jsonl").read_text().splitlines()[0])["kind"] == "truncation_detected"
    finally:
        srv.close()


@pytest.mark.timeout(120)
def test_e2e_extraction_calls_turn_thinking_off_for_ollama(tmp_path):
    h = _harness()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.log").write_text("\n".join(f"row {i} value {i * 7} status=ok" for i in range(3000)) + "\nCHECKSUM=ZZZ9\n")
    script = [{"tool_calls": [{"name": "read", "arguments": {"path": "a.log"}}]}] + [{"text": "NONE"}] * 8 + [{"text": "final"}]
    srv = h.MockServer(script, tmp_path)
    try:
        h.write_agent_dir(tmp_path / "agent", srv.port, context_window=16384, provider="ollama")
        r = h.run_pi("Read a.log and report CHECKSUM=.", ws, tmp_path / "agent", ["compaction.ts"],
                     provider="ollama", timeout_s=110)
        assert any(e.get("type") == "compaction_end" and e.get("result") for e in r["events"]), \
            f"premise: the session compacted\n{r['stderr'][-800:]}"
        extraction = [x["request"] for x in srv.requests() if not x["request"].get("tools")]
        assert extraction, "premise: extraction calls were made"
        assert all(q.get("reasoning_effort") == "none" for q in extraction)
    finally:
        srv.close()
