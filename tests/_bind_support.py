"""Support for the BIND W1 hook tests: a stand-in ``codex`` executable and a subprocess runner.

Why this exists. The Codex agent-route handoff was void for 317 of 317 delegations
(docs/BUGS.md BIND-1) because ``hooks/agent-route.py`` printed a log line to stdout in front of its
decision, and Claude Code ignores a decision that is not the only thing on stdout. Every earlier test
replaced the call that logs (``_try_codex_subagent_delegation`` or ``_log_cli_savings``), so none could
see it. Tests built on this module run the REAL hook script as a subprocess, with a real executable
standing in for ``codex`` (found through ``CODEX_PATH``, the override ``find_codex_binary`` honours;
it never searches ``PATH``), so ``run_codex``, ``_log_cli_savings`` and the structlog default sink all run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOKS_SRC = REPO / "src" / "llm_router" / "hooks"
AGENT_ROUTE = HOOKS_SRC / "agent-route.py"

#: A PreToolUse[Agent] prompt the hook classifies as task type ``code``, which Codex delegation accepts.
SUITABLE_PROMPT = "implement a fix for the off-by-one bug in the pager and explain the root cause"
#: Reaches NS3's ``unsuitable`` branch (the multi-file-write phrase), so Codex is never started.
UNSUITABLE_PROMPT = "refactor the entire codebase across multiple files to rename Foo to Bar"


def quota_text(hours_ahead: float = 2.0) -> str:
    """The captured Codex usage-limit wording, with a reset time safely in the future."""
    import datetime as _dt

    when = (_dt.datetime.now() + _dt.timedelta(hours=hours_ahead)).strftime("%I:%M %p")
    return (
        "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
        f"https://chatgpt.com/codex/settings/usage to purchase more credits or try again at {when}."
    )


def write_codex_stub(
    directory: Path,
    *,
    mode: str = "answer",
    answer: str = "STUB-CODEX-ANSWER",
    argv_log: Path | None = None,
    sleep_s: float = 0.0,
) -> Path:
    """Write an executable named ``codex`` that behaves like ``codex exec --json`` and return its path.

    ``mode``: ``answer`` (one agent message, exit 0), ``quota`` (the usage-limit error events, exit 1) or
    ``fail`` (no events, exit 2). Each call appends ``{"argv": [...], "cwd": ...}`` to ``argv_log`` so a test
    can assert what the hook really launched. The script embeds its configuration, so it does not depend on
    which environment variables the hook passes to the child.
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(
        f"""\
        import json, os, sys, time
        MODE = {mode!r}
        ANSWER = {answer!r}
        QUOTA = {quota_text()!r}
        ARGV_LOG = {str(argv_log) if argv_log else None!r}
        if ARGV_LOG:
            with open(ARGV_LOG, "a") as fh:
                fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                                      "env": {{"LITELLM_LOG": os.environ.get("LITELLM_LOG")}}}}) + "\\n")
        time.sleep({sleep_s!r})
        def ev(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()
        ev({{"type": "thread.started", "thread_id": "stub"}})
        ev({{"type": "turn.started"}})
        if MODE == "answer":
            ev({{"type": "item.completed", "item": {{"type": "agent_message", "text": ANSWER}}}})
            ev({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}})
            sys.exit(0)
        if MODE == "quota":
            ev({{"type": "error", "message": QUOTA}})
            ev({{"type": "turn.failed", "error": {{"message": QUOTA}}}})
            sys.exit(1)
        sys.stderr.write("stub codex: simulated failure\\n")
        sys.exit(2)
        """
    )
    script = directory / "codex_stub.py"
    script.write_text(body)
    launcher = directory / "codex"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)
    return launcher


def hook_env(home: Path, **extra: str) -> dict[str, str]:
    """A clean environment for running a hook: no inherited router or session settings, state under ``home``."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LLM_ROUTER_", "CLAUDE_CODE_", "CODEX_")) and k != "CLAUDECODE"
    }
    state = home / ".llm-router"
    state.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "LLM_ROUTER_HOME": str(state),
            "PYTHONPATH": str(REPO / "src"),
            # DEBUG so the log lines that polluted stdout are really emitted; with the default INFO level the
            # debug line is dropped and a leak could not show.
            "LLM_ROUTER_LOG_LEVEL": "DEBUG",
            "LLM_ROUTER_NO_DETACHED_SPAWN": "1",
            "LLM_ROUTER_MAP_REDISCOVERY": "off",
            "LLM_ROUTER_ROUTE_BANNER": "off",
            "LLM_ROUTER_PROVIDER_RESET_PATH": str(state / "provider_reset.json"),
            "CLAUDE_CODE_SESSION_ID": "bind-test-session",
        }
    )
    env.update(extra)
    return env


def agent_payload(prompt: str, *, subagent_type: str = "general-purpose", tool_use_id: str = "toolu_bind_1",
                  cwd: str | None = None, session_id: str = "bind-test-session", **tool_input) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "cwd": cwd or str(REPO),
        "tool_input": {"prompt": prompt, "subagent_type": subagent_type, **tool_input},
    }


def run_hook(script: Path, payload: dict, env: dict[str, str], cwd: Path | None = None,
             timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        cwd=str(cwd) if cwd else None, timeout=timeout,
    )


def decision_of(proc: subprocess.CompletedProcess) -> dict | None:
    """The one JSON object a hook printed, or None when it printed nothing.

    A host treats stdout that is not exactly one JSON object as plain text and applies no decision, so
    anything else (a log line first, two objects, trailing text) is a failure, not a value to skip.
    """
    out = proc.stdout
    if not out.strip():
        return None
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"hook stdout is not one JSON object ({exc}); first 300 chars: {out[:300]!r}\n"
            f"stderr head: {proc.stderr[:300]!r}"
        ) from None
    assert isinstance(obj, dict), f"hook stdout is JSON but not an object: {out[:200]!r}"
    assert out.lstrip().startswith("{"), f"stdout does not start with '{{': {out[:80]!r}"
    return obj


def stub_calls(argv_log: Path) -> list[dict]:
    """The launches the stub recorded (empty when Codex was never started)."""
    if not argv_log.exists():
        return []
    return [json.loads(line) for line in argv_log.read_text().splitlines() if line.strip()]


def ledger_rows(home: Path) -> list[dict]:
    path = home / ".llm-router" / "north_star_units.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
