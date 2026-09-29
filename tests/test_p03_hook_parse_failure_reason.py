"""P0.3 regression: the hook's JSON-parse-failure branch must reach a
deliberate, reasoned no-decision — never the generic UNHANDLED_EXCEPTION
bucket, and never a bare "JSON parse failed" with no diagnostic.

Root cause (2026-09-29 audit of 572/9,152 = 6% "JSON parse failed" runs over
30 days, ~/.llm-router/auto-route-debug.log): dominated by tests that
deliberately exercise this exact branch (tests/test_a04_malformed_hook_stdin.py
and similar) which, before the write-time log split landed in c16c0c8
(2026-09-13), wrote straight into the PRODUCTION log instead of
auto-route-debug.test.log. That split already brought the real-log rate to 0
for the 16 days since (2026-09-14 through 2026-09-29 at audit time). Two gaps
remained in the code itself, which this file pins:

1. A non-UTF-8 byte on stdin was not caught by the old
   ``except (json.JSONDecodeError, EOFError)`` — it fell through to the
   generic top-level fail-open handler and was logged as an "unhandled
   exception in main()", the exact conflation the audit was asked to explain.
2. Even a caught parse failure was recorded under
   ``coverage.Reason.UNHANDLED_EXCEPTION`` — the SAME bucket as a genuine
   crash — and the debug log line carried no diagnostic (no byte count, no
   error detail), so a future occurrence could not be root-caused from the
   log alone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _run(stdin_bytes: bytes, home: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LLM_ROUTER_HOME": str(home),
        "LLM_ROUTER_DB_PATH": str(home / "chz_p03.db"),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin_bytes, capture_output=True, env=env, timeout=30
    )


def _debug_log_text(home: Path) -> str:
    # PYTEST_CURRENT_TEST is inherited from os.environ (pytest sets it on the
    # running process, and _run above copies the parent env), so the hook's
    # own write-time split (auto-route.py's _debug_log_path) routes this run
    # to the *.test.log file, never the production one.
    p = home / "auto-route-debug.test.log"
    return p.read_text() if p.exists() else ""


def test_non_utf8_stdin_is_caught_here_not_by_the_generic_catchall(tmp_path):
    r = _run(b"\xff\xfe not valid utf-8 \x80\x81", tmp_path, {"LLM_ROUTER_ZERO_CLAUDE": "0"})
    assert r.returncode == 0, r.stderr
    assert b"could not parse hook stdin" in r.stderr, r.stderr

    log = _debug_log_text(tmp_path)
    assert "JSON parse failed" in log, log
    assert "non-utf8 byte" in log, log
    # Must NOT have fallen through to the generic fail-open handler — that
    # was the pre-fix behaviour for this exact input.
    assert "fail-open: unhandled exception in main()" not in log, log


def test_parse_failure_has_its_own_coverage_reason(tmp_path):
    from llm_router import coverage

    prior_home = os.environ.get("LLM_ROUTER_HOME")
    try:
        r = _run(b"{not valid json", tmp_path, {"LLM_ROUTER_ZERO_CLAUDE": "0"})
        assert r.returncode == 0, r.stderr

        os.environ["LLM_ROUTER_HOME"] = str(tmp_path)
        coverage.reset_cache()
        snap = coverage.snapshot()
    finally:
        if prior_home is None:
            os.environ.pop("LLM_ROUTER_HOME", None)
        else:
            os.environ["LLM_ROUTER_HOME"] = prior_home
        coverage.reset_cache()

    assert snap.by_reason.get("PARSE_FAILURE", 0) >= 1, snap.by_reason
    assert snap.by_reason.get("UNHANDLED_EXCEPTION", 0) == 0, (
        f"parse failure was recorded as UNHANDLED_EXCEPTION: {snap.by_reason}"
    )


def test_debug_log_carries_byte_count_and_reason_for_empty_stdin(tmp_path):
    r = _run(b"", tmp_path, {"LLM_ROUTER_ZERO_CLAUDE": "0"})
    assert r.returncode == 0, r.stderr

    log = _debug_log_text(tmp_path)
    assert "JSON parse failed" in log, log
    assert "bytes=0" in log, log
    assert "empty stdin" in log, log


def test_malformed_json_still_blocks_under_zero_claude(tmp_path):
    r = _run(b"{not valid json", tmp_path, {"LLM_ROUTER_ZERO_CLAUDE": "1"})
    out = r.stdout.decode().strip()
    assert out, "zero-Claude produced NO output on malformed stdin (silent bypass)"
    dec = json.loads(out)
    assert dec.get("decision") == "block", dec
