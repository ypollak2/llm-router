"""Regression tests for the CC subscription usage tracker hook.

Pre-v9.4.0 behaviour:
    `cc-usage-track.py` (PostToolUse[Agent]) wrote to a separate orphan DB,
    ``~/.llm-router/llm_usage.db``, with a stub schema (no baseline_model /
    potential_cost_usd / saved_usd columns). The dashboard never read that
    DB, so every Agent subagent call was effectively invisible to savings
    metrics — even though it's exactly the kind of call we want to credit
    against the subscription quota.

Fix:
    The hook now writes to the canonical ``~/.llm-router/usage.db`` with the
    full schema, populating baseline_model + potential_cost_usd + saved_usd.
    Since subscription calls have cost_usd = 0, saved_usd equals the full
    counterfactual API cost.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_hook():
    """Load cc-usage-track.py as an importable module (hyphen → underscore)."""
    hook_path = (
        Path(__file__).parent.parent
        / "src"
        / "llm_router"
        / "hooks"
        / "cc-usage-track.py"
    )
    spec = importlib.util.spec_from_file_location("cc_usage_track", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Redirect ~/.llm-router into a temp dir so the hook can't touch real data."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".llm-router").mkdir()
    return tmp_path


def _run_hook_with_payload(payload: dict, temp_home: Path) -> None:
    """Reload the hook (so DB_PATH picks up the patched HOME) and run main()."""
    # Clear any prior import so DB_PATH re-evaluates against the patched HOME
    sys.modules.pop("cc_usage_track", None)
    mod = _load_hook()
    with patch("sys.stdin", StringIO(json.dumps(payload))):
        try:
            mod.main()
        except SystemExit:
            pass  # main() always sys.exit(0) — that's the contract


def _select_row(db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM usage ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def _agent_payload(subagent_type: str = "Explore", prompt_len: int = 400, output_len: int = 200) -> dict:
    return {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": subagent_type,
            "prompt": "x" * prompt_len,
        },
        "tool_response": {"output": "y" * output_len},
        "duration_ms": 1000,
    }


def test_hook_writes_to_canonical_usage_db(temp_home):
    """The hook must write to usage.db, NOT the orphan llm_usage.db."""
    _run_hook_with_payload(_agent_payload(), temp_home)

    canonical = temp_home / ".llm-router" / "usage.db"
    orphan = temp_home / ".llm-router" / "llm_usage.db"

    assert canonical.exists(), "must write to usage.db"
    assert not orphan.exists(), "must NOT write to orphan llm_usage.db"


def test_hook_populates_baseline_and_savings(temp_home):
    """INSERT must populate baseline_model, potential_cost_usd, saved_usd."""
    _run_hook_with_payload(_agent_payload(subagent_type="Explore"), temp_home)

    row = _select_row(temp_home / ".llm-router" / "usage.db")
    assert row is not None
    assert row["baseline_model"], "baseline_model must not be NULL/empty"
    assert row["potential_cost_usd"] > 0.0, "Explore call has tokens — baseline > 0"
    assert row["saved_usd"] == pytest.approx(row["potential_cost_usd"])


def test_subscription_cost_is_zero(temp_home):
    """Claude Code is flat-rate — cost_usd must stay 0.0, savings equals baseline."""
    _run_hook_with_payload(_agent_payload(), temp_home)

    row = _select_row(temp_home / ".llm-router" / "usage.db")
    assert row["cost_usd"] == 0.0
    assert row["saved_usd"] == row["potential_cost_usd"]


def test_explore_subagent_uses_haiku_baseline(temp_home):
    """Explore / general-purpose subagents are credited against Haiku."""
    _run_hook_with_payload(_agent_payload(subagent_type="Explore"), temp_home)
    row = _select_row(temp_home / ".llm-router" / "usage.db")
    assert "haiku" in row["baseline_model"].lower()


def test_default_subagent_uses_sonnet_baseline(temp_home):
    """Code/Plan/architect subagents are credited against Sonnet."""
    _run_hook_with_payload(_agent_payload(subagent_type="code-reviewer"), temp_home)
    row = _select_row(temp_home / ".llm-router" / "usage.db")
    assert "sonnet" in row["baseline_model"].lower()


def test_provider_is_cc(temp_home):
    """All CC-tracker rows must carry provider='cc' so the dashboard can group them."""
    _run_hook_with_payload(_agent_payload(), temp_home)
    row = _select_row(temp_home / ".llm-router" / "usage.db")
    assert row["provider"] == "cc"


def test_non_agent_tool_is_ignored(temp_home):
    """Hook is PostToolUse[Agent] — other tool names must not write any row."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"output": ""},
        "duration_ms": 50,
    }
    _run_hook_with_payload(payload, temp_home)

    canonical = temp_home / ".llm-router" / "usage.db"
    # Either file doesn't exist OR has no usage rows
    if canonical.exists():
        conn = sqlite3.connect(str(canonical))
        n = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        conn.close()
        assert n == 0
