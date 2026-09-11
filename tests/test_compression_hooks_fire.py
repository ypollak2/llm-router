"""The compression hooks must actually fire on the host they are installed on.

Before the shared reader, feeding bash-compress a verbatim Claude Code
PostToolUse payload produced empty stdout and exit 0 — the hook had shipped
dead. These tests run the real scripts as subprocesses, because every bug here
was invisible to an import-level test: the module imported fine, `main()` just
returned before doing anything.

The bundled-plugin case runs with no importable `llm_router` package, which is
how a plugin is actually distributed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SRC_HOOK = REPO / "src" / "llm_router" / "hooks" / "bash-compress.py"
BUNDLED_HOOK = REPO / "hooks" / "bash-compress.py"


def _payload(lines: int = 60) -> str:
    out = "\n".join(f"note: line {i} of verbose git output" for i in range(lines))
    return json.dumps({
        "session_id": "t", "cwd": "/tmp", "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_response": {"stdout": out, "stderr": "", "interrupted": False},
    })


def _run(hook: Path, payload: str, *, isolated: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LLM_ROUTER_BASH_COMPRESS", None)
    if isolated:
        env["PYTHONPATH"] = ""          # no llm_router package, as in a shipped plugin
    else:
        env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run([sys.executable, str(hook)], input=payload,
                          capture_output=True, text=True, env=env, timeout=60,
                          cwd=str(REPO))


def test_the_hook_fires_on_a_real_claude_code_payload():
    """The regression that matters: this exact shape produced nothing at all."""
    r = _run(SRC_HOOK, _payload())
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "hook produced no output on a Claude Code payload"
    body = json.loads(r.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "PostToolUse"
    assert "reduction" in body["contextForAgent"]


@pytest.mark.skipif(not BUNDLED_HOOK.exists(), reason="bundle not built")
def test_the_bundled_copy_fires_without_the_llm_router_package():
    """A plugin ships as hook scripts at the repo root with no package to import.
    An unguarded `from llm_router...` import would crash every tool call."""
    r = _run(BUNDLED_HOOK, _payload(), isolated=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "bundled hook produced no output"


def test_small_output_is_left_alone():
    r = _run(SRC_HOOK, _payload(lines=2))
    assert r.stdout.strip() == ""


def test_a_non_shell_tool_is_ignored():
    payload = json.loads(_payload())
    payload["tool_name"] = "Read"
    r = _run(SRC_HOOK, json.dumps(payload))
    assert r.stdout.strip() == ""


def test_the_opt_out_still_works():
    env_payload = _payload()
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), LLM_ROUTER_BASH_COMPRESS="off")
    r = subprocess.run([sys.executable, str(SRC_HOOK)], input=env_payload,
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.stdout.strip() == ""


def test_malformed_stdin_never_crashes_a_tool_call():
    r = _run(SRC_HOOK, "{not json")
    assert r.returncode == 0
    assert not r.stderr.strip()


def test_the_compression_stat_reaches_the_database(tmp_path):
    """Third defect in the same chain: MIGRATE_ADD_COMPRESSION_STATS was defined
    in v6.2 and never added to all_migrations, so `compression_stats` did not
    exist, every write raised OperationalError, and the hook's bare
    `except (ImportError, Exception): pass` swallowed it. A hook that fired
    would still have measured nothing."""
    import sqlite3

    db = tmp_path / "usage.db"
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"),
               LLM_ROUTER_DB_PATH=str(db))
    env.pop("LLM_ROUTER_BASH_COMPRESS", None)
    r = subprocess.run([sys.executable, str(SRC_HOOK)], input=_payload(),
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr

    rows = sqlite3.connect(db).execute(
        "SELECT layer, original_tokens, compressed_tokens, tokens_saved FROM compression_stats"
    ).fetchall()
    assert len(rows) == 1, "compression happened but nothing was recorded"
    layer, original, compressed, saved = rows[0]
    assert layer == "rtk"
    assert original > compressed
    assert saved == original - compressed


def test_the_migration_is_actually_applied():
    """Guards the specific mistake: declared, exported, never added to the list."""
    from llm_router.cost import MIGRATE_ADD_COMPRESSION_STATS
    import inspect
    import llm_router.cost as cost

    src = inspect.getsource(cost)
    assert "+ MIGRATE_ADD_COMPRESSION_STATS" in src, \
        "MIGRATE_ADD_COMPRESSION_STATS is defined but not in all_migrations"
    assert MIGRATE_ADD_COMPRESSION_STATS
