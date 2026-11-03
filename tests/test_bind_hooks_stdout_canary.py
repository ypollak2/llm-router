"""BIND W1 / F1 audit: no hook's stdout carries anything but its own JSON, whatever the libraries log.

``agent-route.py`` lost 317 of 317 Codex handoffs because a library log line (structlog's default sink is
STDOUT) came out in front of its decision. ``auto-route.py`` already guarded against that; the other hooks
did not. This test runs EVERY hook script in ``hooks/`` as a subprocess under a canary: a ``sitecustomize``
that makes library code log one ``error`` line through ``llm_router.logging`` at three moments --

* ``stdin``    the first read of the payload inside ``main()`` (a log line before the hook has done anything),
* ``dumps``    every ``json.dumps`` / ``json.dump`` inside ``main()`` (a log line just before the JSON),
* ``atexit``   interpreter exit (a log line after the JSON).

The hook passes when its stdout is empty or exactly one JSON object, the canary line is NOT on it, and the
canary line IS on stderr (a canary that never fired proves nothing: an empty set passes everything).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys

import pytest

from tests._bind_support import REPO, decision_of, hook_env

# Every test starts the real hook as a subprocess (up to four per test); the repo default of 30 s is for
# in-process tests and a loaded runner can double the 8 s the slowest takes here.
pytestmark = [
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX hook runs"),
    pytest.mark.timeout(180),
]

HOOKS = REPO / "hooks"
MARK = "CANARY-LOG-LINE"

_SITECUSTOMIZE = r'''
import atexit, json, sys

_orig_dumps, _orig_dump = json.dumps, json.dump
_busy = False


def _in_hook_main():
    f = sys._getframe(2)
    while f is not None:
        if f.f_code.co_name == "main" and "/hooks/" in f.f_code.co_filename.replace("\\", "/"):
            return True
        f = f.f_back
    return False


def _log(trigger):
    global _busy
    if _busy:
        return
    _busy = True
    try:
        from llm_router.logging import get_logger
        get_logger("canary").error("@MARK@:" + trigger)
    except Exception as exc:  # the canary must not break the hook
        sys.stderr.write("CANARY-BROKEN %s: %s\n" % (type(exc).__name__, exc))
    finally:
        _busy = False


def dumps(*a, **k):
    if _in_hook_main():
        _log("dumps")
    return _orig_dumps(*a, **k)


def dump(*a, **k):
    if _in_hook_main():
        _log("dumps")
    return _orig_dump(*a, **k)


json.dumps, json.dump = dumps, dump


class _Stdin:
    def __init__(self, real):
        self._real = real
        self._fired = False

    def _fire(self):
        if not self._fired and _in_hook_main():
            self._fired = True
            _log("stdin")

    def read(self, *a, **k):
        self._fire()
        return self._real.read(*a, **k)

    def readline(self, *a, **k):
        self._fire()
        return self._real.readline(*a, **k)

    def __iter__(self):
        self._fire()
        return iter(self._real)

    def __getattr__(self, name):
        return getattr(self._real, name)


sys.stdin = _Stdin(sys.stdin)
atexit.register(lambda: _log("atexit"))
'''.replace("@MARK@", MARK)

# One payload per hook, chosen so main() runs past its early exits. A hook with no entry here fails the test:
# a new hook cannot skip the audit by being absent from it.
_BIG_OUTPUT = "\n".join(f"line {i} of command output text" for i in range(400))
PAYLOADS: dict[str, dict] = {
    "agent-depth-release.py": {"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "toolu_1",
                               "tool_input": {"prompt": "x"}, "session_id": "s1"},
    "agent-route.py": {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "toolu_2",
                       "tool_input": {"prompt": "list all files in src/", "subagent_type": "general-purpose"},
                       "session_id": "s1", "cwd": "/tmp"},
    "auto-route.py": {"hook_event_name": "UserPromptSubmit", "prompt": "What is the capital of France?",
                      "session_id": "s1"},
    "bash-compress.py": {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                         "tool_response": {"stdout": _BIG_OUTPUT}, "session_id": "s1"},
    "cc-usage-track.py": {"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_input": {"prompt": "x"},
                          "tool_response": {"content": [{"type": "text", "text": "done"}]}, "session_id": "s1"},
    "context-capture.py": {"hook_event_name": "PostToolUse", "tool_name": "Read",
                           "tool_input": {"file_path": "/tmp/x"}, "tool_response": {"content": "abc"},
                           "session_id": "s1"},
    "enforce-route.py": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                         "session_id": "s1", "cwd": "/tmp"},
    "playwright-compress.py": {"hook_event_name": "PostToolUse", "tool_name": "mcp__playwright__browser_snapshot",
                               "tool_input": {}, "session_id": "s1",
                               "tool_response": {"content": [{"type": "text", "text": "- generic: x\n" * 400}]}},
    "session-end.py": {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": "/nonexistent"},
    "session-start.py": {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup", "cwd": "/tmp"},
    "status-bar.py": {"hook_event_name": "UserPromptSubmit", "prompt": "hello there", "session_id": "s1"},
    "subagent-start.py": {"hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "general-purpose",
                          "session_id": "s1"},
    "usage-refresh.py": {"hook_event_name": "PostToolUse", "tool_name": "mcp__llm_router__llm", "tool_input": {},
                         "tool_response": {}, "session_id": "s1"},
}
#: Stdlib only: import nothing from llm_router, so no library can log through structlog from them
#: (codex-stop.py by design: tests/test_codex_stop.py pins "no llm_router import on the hot path").
STDLIB_ONLY = {"plugin-install.py", "codex-stop.py"}


def _hook_scripts() -> list[str]:
    out = []
    for path in sorted(HOOKS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "llm_router-hook-version" in text and "def main" in text or path.name in STDLIB_ONLY:
            out.append(path.name)
    return out


def test_the_audit_covers_every_shipped_hook_script():
    found = _hook_scripts()
    assert len(found) >= 15, found  # an empty or shrunken set would pass everything
    missing = [n for n in found if n not in PAYLOADS and n not in STDLIB_ONLY]
    assert not missing, f"hooks with no canary payload (add one to PAYLOADS): {missing}"


def test_the_stdlib_only_hooks_really_import_nothing_from_llm_router():
    for name in STDLIB_ONLY:
        tree = ast.parse((HOOKS / name).read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "llm_router" not in imported, f"{name} imports llm_router: give it a payload and a guard"


@pytest.fixture(scope="module")
def canary_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("canary")
    (d / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    return d


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_hook_stdout_is_only_its_json_even_when_libraries_log(name, tmp_path, canary_dir):
    env = hook_env(tmp_path, LLM_ROUTER_AGENT_ROUTE_CODEX="off")
    env["PYTHONPATH"] = f"{canary_dir}:{env['PYTHONPATH']}"
    proc = subprocess.run(
        [sys.executable, str(HOOKS / name)], input=json.dumps(PAYLOADS[name]), capture_output=True, text=True,
        env=env, cwd=str(tmp_path), timeout=120)
    assert "CANARY-BROKEN" not in proc.stderr, proc.stderr[-400:]
    decision_of(proc)  # raises unless stdout is empty or exactly one JSON object
    assert MARK not in proc.stdout, f"{name}: a library log line reached stdout:\n{proc.stdout[:300]}"
    # Non-vacuous: the canary fired, on stderr, before the hook's work (stdin/dumps) and after it (atexit).
    assert f"{MARK}:atexit" in proc.stderr, f"{name}: the exit canary never fired:\n{proc.stderr[-500:]}"
    assert (f"{MARK}:stdin" in proc.stderr or f"{MARK}:dumps" in proc.stderr), (
        f"{name}: no in-main canary fired (the payload exits before main() reads or writes anything):\n"
        f"{proc.stderr[-500:]}")
