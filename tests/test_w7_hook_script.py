"""W7 text-job-bind: the hook script, run as a real process, and what it prints.

A hook's stdout is the host's decision channel: one JSON object or nothing. A line in front of
it, or behind it, makes the host ignore the whole decision (BIND-1, PR #818: every Codex
handoff of the audit was void). So every emit path here is run as a subprocess and its stdout is parsed with
``json.loads`` whole, with writers of every kind trying to put a line on it: ``print``, a logging
handler on ``sys.stdout``, a raw ``os.write(1, ...)``, a child process that inherits file
descriptor 1, and an ``atexit`` handler. A control run with the guard off proves the canary does
reach stdout, so a pass means the guard works.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "src" / "llm_router" / "hooks" / "text-job-bind.py"
BUNDLED = REPO_ROOT / "hooks" / "text-job-bind.py"
T = '"${LLM_TEXT:?}"'
MODEL_TEXT = "app: change a.py\n\nA body line about a.py."


@pytest.fixture()
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    e = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "LLM_ROUTER_", "CLAUDE"))}
    e.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"), GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"),
             GIT_CONFIG_NOSYSTEM="1", OPENAI_API_KEY="sk-test-dummy-key-for-ci-only-no-real-calls-made")
    return e


@pytest.fixture()
def repo(tmp_path, env) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for a in (["init", "-q", "-b", "main", "."], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=r, check=True, env=env)
    (r / "a.py").write_text("print('a')\n")
    subprocess.run(["git", "add", "."], cwd=r, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True, env=env)
    (r / "a.py").write_text("print('a')\nprint('b')\n")
    subprocess.run(["git", "add", "a.py"], cwd=r, check=True, env=env)
    return r


def stdin_for(command: str, repo: Path, **extra) -> str:
    return json.dumps({"session_id": "s", "tool_use_id": "t", "cwd": str(repo), "hook_event_name": "PreToolUse",
                       "tool_name": "Bash", "tool_input": {"command": command, "description": "d", "timeout": 1000}, **extra})


def run_script(script: Path, stdin: str, env: dict, *flags: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *flags, str(script)], input=stdin, env=env, capture_output=True,
                          text=True, timeout=timeout)


def one_object(out: str) -> dict:
    obj = json.loads(out)  # the host's rule: the whole of stdout is ONE JSON object
    assert isinstance(obj, dict)
    return obj


def ledger_rows(env: dict) -> list[dict]:
    p = Path(env["LLM_ROUTER_HOME"]) / "text_job_binds.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


# ---- nothing to say ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("stdin", [
    "", "not json", "[]", "5", json.dumps({"tool_name": "Read", "tool_input": {"file_path": "commit"}}),
    json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}),
])
def test_no_opinion_prints_nothing(stdin, env):
    r = run_script(HOOK, stdin, env)
    assert r.returncode == 0 and r.stdout == ""


def test_an_ordinary_command_prints_nothing_and_does_not_load_the_router(repo, env):
    r = run_script(HOOK, stdin_for("ls -la && echo hi", repo), env, "-X", "importtime")
    assert r.returncode == 0 and r.stdout == ""
    assert "llm_router.tools" not in r.stderr and "litellm" not in r.stderr


def test_a_handwritten_commit_prints_nothing_and_leaves_one_row(repo, env):
    r = run_script(HOOK, stdin_for("git commit -m 'fix: a thing'", repo), env)
    assert r.returncode == 0 and r.stdout == ""
    (row,) = ledger_rows(env)
    assert (row["kind"], row["job"]) == ("handwritten", "commit_message")
    assert "fix: a thing" not in json.dumps(row)


# ---- denials, through the real engine and the real text job (no model is reached) ----------------------------------

def test_a_refused_shape_is_one_deny_object(repo, env):
    r = run_script(HOOK, stdin_for(f"git add a.py && git commit -m {T}", repo), env)
    assert r.returncode == 0
    obj = one_object(r.stdout)
    assert obj["decision"] == "block" and obj["reason"] == obj["hookSpecificOutput"]["permissionDecisionReason"]
    hso = obj["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"
    assert "updatedInput" not in hso and "git add" in obj["reason"]
    (row,) = ledger_rows(env)
    assert (row["kind"], row["outcome"], row["reason"]) == ("denied", "unsupported", "mutating_predecessor")


def test_with_no_passed_local_lane_the_job_is_denied_without_any_model_call(repo, env):
    r = run_script(HOOK, stdin_for(f"git commit -m {T}", repo), env)
    obj = one_object(r.stdout)
    assert obj["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "No local model has passed" in obj["reason"] and "commit message yourself" in obj["reason"]
    (row,) = ledger_rows(env)
    assert (row["outcome"], row["reason"]) == ("fallback", "no_lane") and "out_chars" not in row


def test_the_off_switch_denies(repo, env):
    r = run_script(HOOK, stdin_for(f"git commit -m {T}", repo), {**env, "LLM_ROUTER_LOCAL_TEXT_JOBS": "0"})
    obj = one_object(r.stdout)
    assert "turned off" in obj["reason"] and ledger_rows(env)[0]["reason"] == "flag_off"


def test_a_placeholder_in_a_doc_passes_and_in_an_unreadable_commit_denies(repo, env):
    r = run_script(HOOK, stdin_for(f"echo {T}", repo), env)
    assert r.returncode == 0 and r.stdout == ""
    r = run_script(HOOK, stdin_for(f"git commit -m {T} 'oops", repo), env)
    assert "could not be read" in one_object(r.stdout)["reason"]


def test_without_the_router_package_a_placeholder_is_still_denied_and_nothing_else_is_touched(repo, env):
    """A plugin-only install runs the bundled copy where `import llm_router` fails."""
    for script in (HOOK, BUNDLED):
        r = run_script(script, stdin_for(f"git commit -m {T}", repo), env, "-I", "-S")
        obj = one_object(r.stdout)
        assert obj["hookSpecificOutput"]["permissionDecision"] == "deny" and "could not load the router package" in obj["reason"]
        r = run_script(script, stdin_for("git commit -m 'by hand'", repo), env, "-I", "-S")
        assert r.returncode == 0 and r.stdout == ""
        r = run_script(script, stdin_for("ls", repo), env, "-I", "-S")
        assert r.returncode == 0 and r.stdout == ""


# ---- the rewrite, through the real script with a stub model call ---------------------------------------------------

def run_wrapped(tmp_path: Path, stdin: str, env: dict, *, body: str = "pass", fd_guard: bool = True,
                text: str = MODEL_TEXT, hook: Path = HOOK, timeout: float = 120,
                prelude: str = "") -> subprocess.CompletedProcess:
    """Run the real hook's ``main`` as a real process with the model call replaced by a stub whose
    body is ``body``. Only ``engine.decide``'s ``generate`` argument is changed; everything else
    (the stdout guard, the engine, git, the files) is the production code."""
    n = len(list(tmp_path.glob("wrap_*.py")))
    script = (
        "import importlib.util, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('text_job_bind_hook', {str(hook)!r})\n"
        "hook = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(hook)\n"
        "from llm_router.text_job_bind import engine\n"
        "from llm_router.text_job_bind.engine import GenResult\n"
        "_orig = engine.decide\n"
        "def gen(job, text, hint, timeout_s):\n"
        f"{textwrap.indent(textwrap.dedent(body), '    ')}\n"
        f"    return GenResult('accepted', {text!r}, 'ollama/stub', 1, '')\n"
        "engine.decide = lambda payload, **kw: _orig(payload, generate=gen, **kw)\n"
        f"{prelude}\n"
        + ("hook.entry()\n" if fd_guard else "code = hook.main(fd_guard=False)\nsys.stderr.flush()\nos._exit(code)\n")
    )
    w = tmp_path / f"wrap_{n}.py"
    w.write_text(script)
    return subprocess.run([sys.executable, str(w)], input=stdin, env=env, capture_output=True, text=True, timeout=timeout)


def test_the_rewrite_is_one_object_with_updated_input_and_no_permission_decision(repo, env, tmp_path):
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env)
    assert r.returncode == 0, r.stderr
    obj = one_object(r.stdout)
    assert set(obj) == {"hookSpecificOutput", "systemMessage"}
    hso = obj["hookSpecificOutput"]
    assert set(hso) == {"hookEventName", "updatedInput"} and hso["hookEventName"] == "PreToolUse"
    ui = hso["updatedInput"]
    assert ui["timeout"] == 1000 and set(ui) == {"command", "description", "timeout"}  # every field kept
    assert ui["description"] == "git commit (local text): app: change a.py"  # what the approver sees besides a path
    path = Path(ui["command"].split("-F ", 1)[1])
    # the default body is the deterministic list of changed files: the model's body line never reaches the commit
    published = "app: change a.py\n\nChanged files:\n- a.py (+1/-0)\n"
    assert ui["command"] == f"git commit -F {path}" and path.read_text() == published
    assert "ollama/stub" in obj["systemMessage"]
    out = subprocess.run(["bash", "-c", ui["command"]], cwd=repo, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo, env=env, capture_output=True, text=True).stdout
    assert log.strip() == published.strip() and "A body line" not in log
    (row,) = ledger_rows(env)
    assert (row["kind"], row["outcome"], row["out_chars"]) == ("bound", "accepted", len(published.rstrip("\n")))
    assert row["body_mode"] == "files"


CANARY = '''
    import atexit, logging, os, subprocess, sys, threading
    print("STRAY-PRINT")
    sys.stdout.write("STRAY-WRITE\\n")
    h = logging.StreamHandler(sys.stdout); lg = logging.getLogger("canary"); lg.addHandler(h); lg.warning("STRAY-LOG")
    os.write(1, b"STRAY-RAW-FD1\\n")
    subprocess.run(["echo", "STRAY-CHILD"])
    atexit.register(lambda: print("STRAY-ATEXIT"))
'''

LINGERING_THREAD = '''
    import threading, time
    threading.Thread(target=lambda: time.sleep(60), daemon=False).start()
'''


def test_no_writer_can_put_a_line_in_front_of_or_behind_the_decision(repo, env, tmp_path):
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, body=CANARY)
    assert r.returncode == 0, r.stderr
    obj = one_object(r.stdout)
    assert obj["hookSpecificOutput"]["updatedInput"]["command"].startswith("git commit -F ")
    for stray in ("STRAY-PRINT", "STRAY-WRITE", "STRAY-LOG", "STRAY-RAW-FD1", "STRAY-CHILD", "STRAY-ATEXIT"):
        assert stray not in r.stdout and stray in r.stderr, f"{stray} must have happened, on stderr"


def test_the_canary_does_reach_stdout_without_the_file_descriptor_guard(repo, env, tmp_path):
    """The control: with only `sys.stdout` rebound, a raw write and a child still get through. If this
    stopped failing to parse, the guard test above would prove nothing."""
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, body=CANARY, fd_guard=False)
    assert "STRAY-RAW-FD1" in r.stdout and "STRAY-CHILD" in r.stdout
    with pytest.raises(ValueError):
        json.loads(r.stdout)


def test_a_lingering_library_thread_cannot_keep_the_host_waiting(repo, env, tmp_path):
    """After the decision is out the process exits normally, and a watchdog ends it hard if a non-daemon
    thread (the kind aiosqlite leaves behind) stops the interpreter from finishing."""
    # The thread sleeps 60 s; the subprocess timeout (20 s) is the clock: without the watchdog it raises.
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, body="pass",
                    prelude=textwrap.dedent(LINGERING_THREAD), timeout=20)
    assert r.returncode == 0
    assert one_object(r.stdout)["hookSpecificOutput"]["updatedInput"]["command"].startswith("git commit -F ")


def test_the_deadline_guard_answers_with_a_deny_before_the_host_would_kill_the_hook(repo, env, tmp_path):
    # The model call sleeps 60 s; the guard fires at 4 s. Without it the run outlives the 25 s subprocess timeout.
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, body="import time; time.sleep(60)",
                    prelude="hook._DEADLINE_SEC = 4", timeout=25)
    assert r.returncode == 0
    obj = one_object(r.stdout)
    assert obj["hookSpecificOutput"]["permissionDecision"] == "deny" and "ran out of time" in obj["reason"]
    assert "updatedInput" not in obj["hookSpecificOutput"]


def test_the_deadline_guard_leaves_a_ledger_row_the_cooldown_and_a_latency_row(repo, env, tmp_path):
    """F10: os._exit skips the exit handlers, so the guard writes what an engine timeout writes before it leaves."""
    home = Path(env["LLM_ROUTER_HOME"])
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, body="import time; time.sleep(60)",
                    prelude="hook._DEADLINE_SEC = 4\nfrom llm_router import hook_latency\n"
                            "hook_latency.begin('text-job-bind', 'PreToolUse', hook._HOOK_T0)", timeout=25)
    assert r.returncode == 0 and one_object(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    (row,) = ledger_rows(env)
    assert (row["kind"], row["outcome"], row["reason"]) == ("denied", "timeout", "deadline_guard")
    until = json.loads((home / "text_job_bind" / "cooldown.json").read_text())["until"]
    assert until > time.time() + 200  # the cooldown, as after a timeout inside the engine
    lat = [json.loads(ln) for ln in (home / "hook_latency.jsonl").read_text().splitlines()]
    assert len(lat) == 1 and lat[0]["hook"] == "text-job-bind"
    assert lat[0]["phases_ms"]["text_job"] > 2000  # the wait on the model is booked as the model's, not the router's
    assert lat[0]["router_added_ms"] < 2500
    # the next placeholder command is refused at once, as after any timeout
    r2 = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, body="pass")
    assert "paused" in one_object(r2.stdout)["reason"]


def test_the_deadline_guard_is_armed_only_for_a_placeholder_command(repo, env, tmp_path):
    r = run_wrapped(tmp_path, stdin_for("git commit -m 'by hand'", repo), env, prelude="hook._DEADLINE_SEC = 0.01")
    assert r.returncode == 0 and r.stdout == ""


def test_the_hooks_literal_fallbacks_match_the_limits():
    import importlib.util

    from llm_router.text_job_bind import limits

    spec = importlib.util.spec_from_file_location("tjb_hook_limits", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook._HOOK_TIMEOUT_SEC == limits.HOOK_TIMEOUT_SEC
    assert hook._DEADLINE_SEC == limits.HOOK_TIMEOUT_SEC - 15 and limits.BUDGET_SEC < hook._DEADLINE_SEC < limits.HOOK_TIMEOUT_SEC


def test_a_second_decision_is_never_written(capsys):
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("tjb_hook_unit", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    buf = io.StringIO()
    ch = hook._Channel(buf)
    ch.deny("first")
    ch.deny("second")
    ch.rewrite({"command": "x"}, None)
    assert json.loads(buf.getvalue())["reason"] == "first"


def test_in_process_main_hands_stdout_back_and_writes_one_object(repo, env, monkeypatch, capsys):
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("tjb_hook_unit2", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv("LLM_ROUTER_HOME", env["LLM_ROUTER_HOME"])
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "0")
    before = sys.stdout
    buf = io.StringIO()
    assert hook.main(stdin_for(f"git commit -m {T}", repo), out=buf) == 0
    assert sys.stdout is before
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert capsys.readouterr().out == ""


def test_the_bundled_copy_is_byte_identical_and_stamped():
    assert HOOK.read_bytes() == BUNDLED.read_bytes()
    assert b"llm_router-hook-version:" in HOOK.read_bytes()[:200]


def test_loading_the_router_is_booked_as_router_added_time_not_as_the_models_wait(repo, env, tmp_path):
    """F11: the router import takes seconds and is not the model's wait. A stand-in `prepare` that takes a second
    must show up in router_added_ms; the model phase must stay small."""
    home = Path(env["LLM_ROUTER_HOME"])
    prelude = ("from llm_router import hook_latency\n"
               "hook_latency.begin('text-job-bind', 'PreToolUse', hook._HOOK_T0)\n"
               "_o2 = engine.decide\n"
               "engine.decide = lambda payload, **kw: _o2(payload, prepare=lambda: __import__('time').sleep(1.0), **kw)\n")
    r = run_wrapped(tmp_path, stdin_for(f"git commit -m {T}", repo), env, prelude=prelude)
    assert r.returncode == 0 and '"updatedInput"' in r.stdout
    (lat,) = [json.loads(ln) for ln in (home / "hook_latency.jsonl").read_text().splitlines()]
    assert lat["router_added_ms"] >= 950, lat
    assert lat["phases_ms"]["text_job"] < 500, lat
