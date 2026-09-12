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
    # REAL porcelain, not filler. The old payload was 60 lines of prose that no
    # git filter recognises; it "compressed" 91% only because the unrecognised
    # branch returned output[:200] — a blind cut that silently dropped 50 lines.
    # That fallback is gone (it became data loss once output was substituted
    # rather than appended), so a payload has to be the shape a filter handles.
    out = "\n".join(f"?? generated/file_{i}.txt" for i in range(lines))
    return json.dumps({
        "session_id": "t", "cwd": "/tmp", "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_response": {"stdout": out, "stderr": "", "interrupted": False},
    })


def _run(hook: Path, payload: str, *, isolated: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LLM_ROUTER_BASH_COMPRESS", None)
    # The hook is SILENT by default now: a PostToolUse hook cannot replace what
    # the model sees (verified live — updatedOutput ignored, additionalContext
    # appends), so emitting anything would enlarge the turn rather than shrink
    # it. These tests are about the hook running and compressing correctly, so
    # they turn the emission on explicitly.
    env["LLM_ROUTER_COMPRESS_EMIT"] = "1"
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
    assert "reduction" in body["updatedOutput"]


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


def test_the_hook_is_silent_by_default():
    """A PostToolUse hook cannot replace a tool result.

    Verified live in-session against a real `git status --porcelain`:
    `contextForAgent` is not a Claude Code field and was dropped;
    `updatedOutput` was ignored and all 56 lines arrived in full;
    `additionalContext` was honoured but APPENDS.

    So compression here saves nothing — the uncompressed output reaches the
    model regardless — and emitting a summary alongside it makes the turn
    LARGER than doing nothing. Silence is the correct default until a
    mechanism exists that can actually substitute (PreToolUse deny, where the
    command never runs at all).
    """
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    env.pop("LLM_ROUTER_COMPRESS_EMIT", None)
    env.pop("LLM_ROUTER_BASH_COMPRESS", None)
    r = subprocess.run([sys.executable, str(SRC_HOOK)], input=_payload(),
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0
    assert r.stdout.strip() == "", "the hook added tokens it cannot save"


def test_the_stat_is_still_recorded_while_silent(tmp_path):
    """The compressor works (9.6% over 994 real outputs); only the DELIVERY is
    impossible. Keeping the measurement means the number is ready the day a
    substitution mechanism exists."""
    import sqlite3

    db = tmp_path / "usage.db"
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), LLM_ROUTER_DB_PATH=str(db))
    env.pop("LLM_ROUTER_COMPRESS_EMIT", None)
    env.pop("LLM_ROUTER_BASH_COMPRESS", None)
    subprocess.run([sys.executable, str(SRC_HOOK)], input=_payload(),
                   capture_output=True, text=True, env=env, timeout=60)
    rows = sqlite3.connect(db).execute(
        "SELECT tokens_saved FROM compression_stats").fetchall()
    assert rows and rows[0][0] > 0
