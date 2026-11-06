"""BIND W1 / F1 audit: no hook's stdout carries anything but its own JSON, whatever the libraries log.

``agent-route.py`` lost 317 of 317 Codex handoffs because a library log line (structlog's default sink is
STDOUT) came out in front of its decision. ``auto-route.py`` already guarded against that; the other hooks
did not. This test runs every hook script in ``hooks/`` as a subprocess (``enforce-route.py`` is audited by
another pull request: see ``OWNED_ELSEWHERE``) under a canary: a ``sitecustomize``
that makes library code log one ``error`` line through ``llm_router.logging`` at three moments --

* ``stdin``    the first read of the payload inside ``main()`` (a log line before the hook has done anything),
* ``dumps``    every ``json.dumps`` / ``json.dump`` inside ``main()`` (a log line just before the JSON),
* ``atexit``   interpreter exit (a log line after the JSON).

The hook passes when its stdout is empty or exactly one JSON object, the canary line is NOT on it, and the
canary line IS on stderr (a canary that never fired proves nothing: an empty set passes everything).

A second canary does the same through LiteLLM's OWN logger (S4 of the independent review): LiteLLM keeps a handler that
writes every record below WARNING to ``sys.stdout``, at a level it reads from ``LITELLM_LOG`` (default DEBUG) when it is
imported, and the structlog canary above never touches it. It runs with LiteLLM imported late (after the hook's guard) and
early (before it), and a control shows the canary does reach stdout when the level is DEBUG.

N4 (re-review of PR #818): an explicit ``LITELLM_LOG=DEBUG`` in the user's environment used to win over the hooks' guard
and put LiteLLM's records in front of the JSON of all ten hooks that carry it. The same LiteLLM canary now also runs with
that value set, in both import orders: the guard forces the level (docs/bugs/BIND-10.md).
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
LITELLM_MARK = "CANARY-LITELLM-LINE"

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


import os as _os

if _os.environ.get("CANARY_LITELLM") == "early":
    import litellm  # noqa: F401  imported BEFORE the hook's own guard runs


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
    if _os.environ.get("CANARY_LITELLM"):
        try:
            import logging
            import litellm  # noqa: F401  (late mode: first imported here, after the hook's guard)
            logging.getLogger("LiteLLM").info("@LMARK@:" + trigger)  # below WARNING: its handler writes it to stdout
            with open(_os.environ["CANARY_FIRED"], "a") as fh:
                fh.write(trigger + "\n")
        except Exception as exc:
            sys.stderr.write("CANARY-BROKEN-LITELLM %s: %s\n" % (type(exc).__name__, exc))
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
'''.replace("@MARK@", MARK).replace("@LMARK@", "CANARY-LITELLM-LINE")

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
    "playwright-compress.py": {"hook_event_name": "PostToolUse", "tool_name": "mcp__playwright__browser_snapshot",
                               "tool_input": {}, "session_id": "s1",
                               "tool_response": {"content": [{"type": "text", "text": "- generic: x\n" * 400}]}},
    "session-end.py": {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": "/nonexistent"},
    "session-start.py": {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup", "cwd": "/tmp"},
    "status-bar.py": {"hook_event_name": "UserPromptSubmit", "prompt": "hello there", "session_id": "s1"},
    "subagent-start.py": {"hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "general-purpose",
                          "session_id": "s1"},
    # BIND W7 (text-job-bind): a placeholder command with a refused shape (a `git add` before the commit), so main()
    # runs to its one JSON decision (a deny) through _Channel.send's json.dumps, with no model and no git needed.
    "text-job-bind.py": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "toolu_3",
                         "tool_input": {"command": 'git add a.py && git commit -m "${LLM_TEXT:?}"'},
                         "session_id": "s1", "cwd": "/tmp"},
    "usage-refresh.py": {"hook_event_name": "PostToolUse", "tool_name": "mcp__llm_router__llm", "tool_input": {},
                         "tool_response": {}, "session_id": "s1"},
}
#: Stdlib only: import nothing from llm_router, so no library can log through structlog from them
#: (codex-stop.py by design: tests/test_codex_stop.py pins "no llm_router import on the hot path").
STDLIB_ONLY = {"plugin-install.py", "codex-stop.py"}
#: Hooks whose stdout guard and subprocess tests live in ANOTHER pull request. BIND W2 (PR #817) owns
#: ``enforce-route.py`` (and its ``src/llm_router/hooks/`` mirror): it carries its own stdout guard and tests the
#: three emit paths as subprocesses in ``tests/test_w2_enforce_route_holds.py`` (the block, the local-interception
#: deny, a probe and a release that print nothing, and the in-process call that must hand stdout back). This PR
#: does not touch that hook, so two PRs cannot edit one file, and the canary does not run it here. After both
#: merge, the same canary can take it back by moving the name to ``PAYLOADS``.
OWNED_ELSEWHERE = {"enforce-route.py"}
#: ``auto-route.py`` is audited by the structlog canary here but is PR #817's file too (BIND W2 edits it), so its LiteLLM
#: guard, if it needs one, goes with that PR; this branch does not touch it.
LITELLM_NOT_HERE = {"auto-route.py"}


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
    missing = [n for n in found if n not in PAYLOADS and n not in STDLIB_ONLY and n not in OWNED_ELSEWHERE]
    assert not missing, f"hooks with no canary payload (add one to PAYLOADS): {missing}"
    # An exclusion must name a script that exists and must not also be audited here, or it hides nothing real.
    assert set(OWNED_ELSEWHERE) <= set(found), f"OWNED_ELSEWHERE names a script that is not shipped: {OWNED_ELSEWHERE}"
    assert not set(OWNED_ELSEWHERE) & (set(PAYLOADS) | STDLIB_ONLY), "a hook is both audited here and excluded"


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


def _canary_env(tmp_path, canary_dir, mode, explicit=None):
    env = hook_env(tmp_path, LLM_ROUTER_AGENT_ROUTE_CODEX="off", CANARY_LITELLM=mode,
                   CANARY_FIRED=str(tmp_path / "litellm-fired.txt"))
    env.pop("LITELLM_LOG", None)  # nothing the user set ...
    if explicit:
        env["LITELLM_LOG"] = explicit  # ... or the user's own value, which must not win over the hook's guard (N4)
    env["PYTHONPATH"] = f"{canary_dir}:{env['PYTHONPATH']}"
    return env


@pytest.mark.parametrize("explicit", [None, "DEBUG"], ids=["env-unset", "explicit-DEBUG"])
@pytest.mark.parametrize("mode", ["late", "early"])
@pytest.mark.parametrize("name", sorted(set(PAYLOADS) - LITELLM_NOT_HERE))
def test_hook_stdout_is_only_its_json_even_when_litellm_logs(name, mode, explicit, tmp_path, canary_dir):
    """S4: LiteLLM's own handler writes records below WARNING to stdout. The hook must keep them off, whether LiteLLM is
    imported after its guard (``late``: the level is forced first) or before it (``early``: its loggers are set), and
    whether the user's environment says nothing or says ``LITELLM_LOG=DEBUG`` (N4)."""
    env = _canary_env(tmp_path, canary_dir, mode, explicit)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / name)], input=json.dumps(PAYLOADS[name]), capture_output=True, text=True,
        env=env, cwd=str(tmp_path), timeout=120)
    assert "CANARY-BROKEN" not in proc.stderr, proc.stderr[-400:]
    decision_of(proc)  # empty or exactly one JSON object
    assert LITELLM_MARK not in proc.stdout, (
        f"{name} ({mode}, LITELLM_LOG={explicit}): LiteLLM's handler reached stdout:\n{proc.stdout[:300]}")
    fired = (tmp_path / "litellm-fired.txt").read_text().split() if (tmp_path / "litellm-fired.txt").exists() else []
    assert "atexit" in fired and ({"stdin", "dumps"} & set(fired)), (  # the canary ran after the work and inside main()
        f"{name} ({mode}): the LiteLLM canary never fired inside main(): {fired}\n{proc.stderr[-300:]}")


@pytest.mark.parametrize("level, leaks", [("DEBUG", True), ("ERROR", False)])
def test_the_litellm_canary_reaches_stdout_exactly_when_the_level_lets_it(level, leaks, tmp_path, canary_dir):
    """The control: with no hook, but the root logger at DEBUG as a hook leaves it (``configure_logging``), LiteLLM's handler
    does print the canary line to stdout at ``LITELLM_LOG=DEBUG`` and not at ERROR, so the canary can fail and the hooks'
    pass above means something."""
    env = _canary_env(tmp_path, canary_dir, "early")  # LiteLLM cannot be first imported at interpreter shutdown
    env["LITELLM_LOG"] = level
    proc = subprocess.run([sys.executable, "-c", "import logging; logging.getLogger().setLevel(logging.DEBUG)"],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    assert "CANARY-BROKEN" not in proc.stderr, proc.stderr[-400:]
    assert (LITELLM_MARK in proc.stdout) is leaks, (level, proc.stdout[:300], proc.stderr[-300:])
