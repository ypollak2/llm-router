"""Phase 0.5 / T6 (Edit 6): advise-mode adoption recording.

``advise``/``advisory`` enforcement NEVER blocks — that invariant predates this
change and must not regress. But before this edit, advise's early-exit at
``enforce-route.py``'s ``if enforce in ("advise", "advisory"): sys.exit(0)``
skipped the rest of ``main()`` entirely, so a session run entirely in advise
mode recorded ZERO adoption evidence even when the host honored every single
routed directive — realized_savings_usd stayed 0 forever in that mode, by
construction rather than by any real bypass.

Edit 6 makes the advise branch duplicate the guarded pending-read + the
IDENTICAL "routing satisfied" 3-branch predicate used by hard/smart
(~1140-1166: llm_* prefix, exact expected-tool match, or expected-MCP-server
match) and, on a match, call the same `_record_realization_used` writer used
downstream — then still unconditionally exits 0. This test proves:

1. Advise never blocks (empty stdout / exit 0) regardless of match.
2. On a MATCHING tool call, the pending file is cleared and a
   route_realized / verified_used / door_call ledger row lands at the
   pending's route_id.
3. On a NON-matching tool call, nothing is recorded and pending survives
   (negative control — proves the predicate actually gates the write,
   not a tautology that always records).
4. No pending state at all: advise still never crashes or blocks
   (fail-open on the newly-added try/except).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run_hook(
    hook_path: Path,
    payload: dict,
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Mirrors tests/test_route_enforcement_hooks.py::_run_hook, minus the
    smart-default injection (irrelevant here — every test sets LLM_ROUTER_ENFORCE
    explicitly to advise)."""
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    env["LLM_ROUTER_ENFORCE"] = "advise"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_pending(home: Path, session_id: str, **overrides) -> Path:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    pending_path = router_dir / f"pending_route_{session_id}.json"
    data = {
        "expected_tool": "llm_query",
        "task_type": "query",
        "complexity": "simple",
        "issued_at": time.time(),
        "session_id": session_id,
    }
    data.update(overrides)
    pending_path.write_text(json.dumps(data), encoding="utf-8")
    return pending_path


@pytest.fixture
def ledger_db(tmp_path):
    """A dedicated ledger DB path, isolated from ~/.llm-router/usage.db, shared
    between the subprocess hook invocation (via LLM_ROUTER_EXECUTION_LEDGER_DB in
    extra_env) and the in-process assertion (via path=... on the accessor)."""
    return tmp_path / "ledger" / "usage.db"


def test_advise_never_blocks_on_matching_tool(tmp_path, ledger_db):
    from llm_router.execution_ledger import get_route_accounting

    session_id = "sess-advise-match"
    route_id = "sess-advise-match:1785000000:llm_query:deadbeef"
    pending_path = _write_pending(
        tmp_path, session_id, route_id=route_id, turn_id=42
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "llm_query"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "advise mode must never emit a block decision, matching tool or not"
    )
    assert not pending_path.exists(), (
        "advise must clear pending once the routed door was actually called"
    )

    acc = get_route_accounting(route_id, path=ledger_db)
    assert acc.realized_routes == 1, (
        "advise-mode adoption must write a verified_used route_realized row "
        "when the host calls the routed door — this is the whole point of T6"
    )
    assert acc.overridden_routes == 0
    assert acc.realization_unknown_routes == 0


def test_advise_never_blocks_on_nonmatching_tool_and_records_nothing(
    tmp_path, ledger_db
):
    """Negative control: proves the predicate actually gates the write. If this
    failed (realized_routes > 0 for an unrelated tool), T6's match logic would
    be a tautology that always records regardless of what was called."""
    from llm_router.execution_ledger import get_route_accounting

    session_id = "sess-advise-nomatch"
    route_id = "sess-advise-nomatch:1785000000:llm_query:cafebabe"
    pending_path = _write_pending(
        tmp_path, session_id, route_id=route_id, turn_id=7
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "advise must never block, even on a miss"
    assert pending_path.exists(), (
        "a non-matching tool call must NOT be treated as adoption; pending "
        "should survive for a later matching call"
    )

    acc = get_route_accounting(route_id, path=ledger_db)
    assert acc.realized_routes == 0
    assert acc.overridden_routes == 0


def test_advise_with_no_pending_state_does_not_crash(tmp_path, ledger_db):
    """Fail-open: no pending file at all (e.g. advise turned on mid-session,
    or the directive already expired/was cleared) must not raise or block."""
    session_id = "sess-advise-empty"

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "llm_query"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_advise_matches_via_expected_tool_exact_name(tmp_path, ledger_db):
    """Covers predicate branch 2 (exact expected_tool match) rather than the
    llm_ prefix shortcut, e.g. a non-llm_-prefixed expected tool called
    verbatim."""
    from llm_router.execution_ledger import get_route_accounting

    session_id = "sess-advise-exact"
    route_id = "sess-advise-exact:1785000000:some_tool:0badf00d"
    _write_pending(
        tmp_path,
        session_id,
        route_id=route_id,
        turn_id=1,
        expected_tool="mcp__llm_router__some_tool",
    )

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "mcp__llm_router__some_tool"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""

    acc = get_route_accounting(route_id, path=ledger_db)
    assert acc.realized_routes == 1


def test_advise_dedups_via_stable_event_id_on_retry(tmp_path, ledger_db):
    """A retried hook invocation for the same (session, route_id) must not
    double-count — _record_realization_used's event_id is content-stable
    (sha256 of session_id|route_id|route_realized), so INSERT OR IGNORE
    collapses repeats. Re-write pending between calls since the first call
    clears it, mirroring the hook being fired twice for the same directive
    (e.g. a duplicate PreToolUse delivery)."""
    from llm_router.execution_ledger import get_route_accounting

    session_id = "sess-advise-dedup"
    route_id = "sess-advise-dedup:1785000000:llm_query:11111111"

    for _ in range(2):
        _write_pending(tmp_path, session_id, route_id=route_id, turn_id=9)
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": "llm_query"},
            home=tmp_path,
            extra_env={"LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    acc = get_route_accounting(route_id, path=ledger_db)
    assert acc.realized_routes == 1, (
        "two honored calls for the identical route_id must dedup to one "
        "realized route, not double-count"
    )
