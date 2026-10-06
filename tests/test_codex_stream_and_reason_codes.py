"""Codex delegation failures: stream overflow, silent hang, reasonless rows.

Evidence: 143 ``codex_failed`` rows in ~/.llm-router/north_star_units.jsonl for
the 7 days to 2026-10-06 -- 73 "Separator is not found, and chunk exceed the
limit" (asyncio's 64 KiB StreamReader line limit), 37 "timed out after 120s",
28 "Reading additional input from stdin...".

A fake ``codex`` executable stands in for the CLI; no real Codex is started.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import textwrap
import time
from pathlib import Path

import pytest

from llm_router import codex_agent
from llm_router.codex_agent import CodexResult, run_codex

HOOK_PATH = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "agent-route.py"


def _fake_codex(tmp_path: Path, body: str) -> str:
    path = tmp_path / "codex"
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _event(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))


def _run(monkeypatch, binary, **kw):
    import asyncio
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: binary)
    return asyncio.run(run_codex("q", model="m", working_dir=str(Path(binary).parent), **kw))


# -- class 1: long JSONL line over asyncio's 64 KiB default -------------------

def test_line_over_64k_is_read_not_an_overflow_error(monkeypatch, tmp_path):
    big = "A" * 200_000
    b = _fake_codex(tmp_path, f"""
        import sys
        sys.stdout.write({_event(big)!r} + "\\n")
    """)
    res = _run(monkeypatch, b, timeout=20)
    assert res.success, res.content[:200]
    assert res.content == big
    assert "Separator" not in res.content


def test_line_over_cap_is_dropped_flagged_and_run_survives(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_agent, "_STDOUT_LINE_LIMIT", 100_000, raising=False)
    b = _fake_codex(tmp_path, f"""
        import sys
        sys.stdout.write("x" * 500_000 + "\\n")
        sys.stdout.write({_event("answer")!r} + "\\n")
    """)
    res = _run(monkeypatch, b, timeout=20)
    assert res.success and res.content == "answer"
    assert res.truncated is True


def test_total_bytes_cap_stops_the_process_and_records_truncation(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_agent, "_STDOUT_TOTAL_CAP", 200_000, raising=False)
    b = _fake_codex(tmp_path, f"""
        import sys
        sys.stdout.write({_event("partial")!r} + "\\n")
        sys.stdout.flush()
        while True:
            sys.stdout.write("noise " * 1000 + "\\n"); sys.stdout.flush()
    """)
    t = time.monotonic()
    res = _run(monkeypatch, b, timeout=30)
    assert time.monotonic() - t < 15
    assert res.truncated is True and res.content == "partial"


# -- class 2: stdin ------------------------------------------------------------

def test_stdin_is_closed_so_codex_never_waits_on_it(monkeypatch, tmp_path):
    # Blocks forever if stdin is an open pipe/tty; returns at once on EOF.
    b = _fake_codex(tmp_path, f"""
        import sys
        sys.stdin.read()
        sys.stdout.write({_event("ok")!r} + "\\n")
    """)
    res = _run(monkeypatch, b, timeout=10)
    assert res.success and res.content == "ok"


def test_banner_only_is_a_coded_failure(monkeypatch, tmp_path):
    b = _fake_codex(tmp_path, """
        print("Reading additional input from stdin...")
    """)
    res = _run(monkeypatch, b, timeout=10)
    assert not res.success
    assert res.reason_code == "empty_completion"


# -- class 3: slow / silent output -------------------------------------------

def test_silent_hang_is_killed_at_the_timeout(monkeypatch, tmp_path):
    b = _fake_codex(tmp_path, """
        import time
        time.sleep(12)
    """)
    t = time.monotonic()
    res = _run(monkeypatch, b, timeout=1)
    assert time.monotonic() - t < 6, "no output ever arrives; the deadline must still fire"
    assert res.exit_code == 124 and res.reason_code == "timeout"


def test_drip_feed_past_the_deadline_times_out(monkeypatch, tmp_path):
    b = _fake_codex(tmp_path, """
        import sys, time
        for _ in range(100):
            print("{}"); sys.stdout.flush(); time.sleep(0.2)
    """)
    res = _run(monkeypatch, b, timeout=1)
    assert res.exit_code == 124 and res.reason_code == "timeout"


# -- every failure path carries a reason_code ----------------------------------

def test_every_runner_failure_has_a_reason_code(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: None)
    import asyncio
    assert asyncio.run(run_codex("q", model="m")).reason_code == "binary_missing"

    b = _fake_codex(tmp_path, """
        import sys
        print('{"type":"error","message":"model not found"}')
        sys.exit(3)
    """)
    assert _run(monkeypatch, b, timeout=10).reason_code == "cli_error"

    b = _fake_codex(tmp_path, "import sys; sys.exit(2)")
    assert _run(monkeypatch, b, timeout=10).reason_code == "nonzero_exit"

    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: str(tmp_path / "nope"))
    assert asyncio.run(run_codex("q", model="m", working_dir=str(tmp_path))).reason_code == "spawn_error"


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_reason_codes", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.mark.parametrize("res,status,expect", [
    ({"content": "Codex timed out after 120s", "exit_code": 124, "reason_code": "timeout"}, "failed", "timeout"),
    ({"content": "x", "exit_code": 1}, "failed", "unclassified"),   # legacy result, no code
    (None, "failed", "no_result"),
    (None, "no_time", "no_time"),
])
def test_ledger_row_for_every_failure_has_reason_and_code(monkeypatch, tmp_path, res, status, expect):
    hook = _load_hook()
    if res is not None:
        res = CodexResult(model="m", duration_sec=1.0, **res)
    monkeypatch.setattr(hook, "_router_home", lambda: tmp_path)
    hook._note_codex_failure("ns3", status, res, "general-purpose", "query", "complex", "s")
    rows = [json.loads(line) for line in (tmp_path / "north_star_units.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "codex_failed"
    assert rows[0]["reason_code"] == expect
    assert rows[0]["reason"].strip()
