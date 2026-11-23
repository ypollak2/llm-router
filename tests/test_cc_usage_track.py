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
    # LLM_ROUTER_HOME now takes precedence over HOME/Path.home() (see
    # _router_home() in the hook), and the autouse isolation fixture in
    # conftest.py already exports it pointed at a *different* tmp dir. Patch
    # it here too, or the hook writes to that dir instead of tmp_path/.llm-router.
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
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


def _agent_payload(
    subagent_type: str = "Explore",
    prompt_len: int = 400,
    output_len: int = 200,
    model: str | None = "haiku",
) -> dict:
    """Shape of tests/fixtures/hook_payloads/cc-usage-track.json (a recorded Claude Code
    PostToolUse[Agent] payload), plus the Agent call's `model` alias when ``model`` is set."""
    tool_input = {"subagent_type": subagent_type, "prompt": "x" * prompt_len}
    if model is not None:
        tool_input["model"] = model
    return {
        "session_id": "5f0c9a2e-7b31-4c8d-9e46-1a2b3c4d5e6f",
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "tool_response": {
            "content": [{"type": "text", "text": "y" * output_len}],
            "totalDurationMs": 1000,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
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


def test_baseline_names_the_family_the_row_was_priced_at(temp_home):
    """The baseline label follows the model that ran, not the subagent_type."""
    _run_hook_with_payload(_agent_payload(subagent_type="Explore", model="sonnet"), temp_home)
    row = _select_row(temp_home / ".llm-router" / "usage.db")
    assert row["model"] == "claude-sonnet-5-5"
    assert row["baseline_model"] == "sonnet"


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


# -- model resolution: the model that RAN, not one guessed from subagent_type -----------------------

def _rows(temp_home: Path) -> list[dict]:
    db = temp_home / ".llm-router" / "usage.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM usage ORDER BY id")]
    conn.close()
    return out


def _price_per_token(model: str) -> tuple[float, float]:
    from llm_router import pricing

    p = pricing.price_for(model)
    assert p is not None, model
    return p.input / 1e6, p.output / 1e6


@pytest.mark.parametrize(
    ("alias", "model"),
    [
        ("opus", "claude-opus-5-5"),
        ("sonnet", "claude-sonnet-5-5"),
        ("haiku", "claude-haiku-5-5"),
    ],
)
def test_tool_input_model_alias_maps_to_current_priced_model(temp_home, alias, model):
    """Explore is not Haiku: the Agent call's model parameter decides, at the registry price."""
    payload = _agent_payload(subagent_type="Explore", model=alias)
    _run_hook_with_payload(payload, temp_home)
    (row,) = _rows(temp_home)
    assert row["model"] == model
    in_p, out_p = _price_per_token(model)
    assert row["potential_cost_usd"] == pytest.approx(
        in_p * row["input_tokens"] + out_p * row["output_tokens"], rel=1e-6
    )


def test_tool_response_resolved_model_wins_over_tool_input(temp_home):
    payload = _agent_payload(model="haiku")
    payload["tool_response"]["resolvedModel"] = "claude-opus-5-5"
    _run_hook_with_payload(payload, temp_home)
    (row,) = _rows(temp_home)
    assert row["model"] == "claude-opus-5-5"


def test_agent_definition_frontmatter_model(temp_home):
    agents = temp_home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("---\nname: reviewer\nmodel: opus\n---\nbody model: haiku\n")
    _run_hook_with_payload(_agent_payload(subagent_type="reviewer", model=None), temp_home)
    (row,) = _rows(temp_home)
    assert row["model"] == "claude-opus-5-5"


def test_project_definition_beats_user_definition(temp_home):
    for base, model in ((temp_home / ".claude" / "agents", "haiku"), (temp_home / "proj" / ".claude" / "agents", "sonnet")):
        base.mkdir(parents=True)
        (base / "w.md").write_text(f"---\nmodel: {model}\n---\n")
    payload = _agent_payload(subagent_type="w", model=None)
    payload["cwd"] = str(temp_home / "proj")
    _run_hook_with_payload(payload, temp_home)
    (row,) = _rows(temp_home)
    assert row["model"] == "claude-sonnet-5-5"


def test_inherit_falls_through_to_the_parent_session_model(temp_home):
    agents = temp_home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "inh.md").write_text("---\nmodel: inherit\n---\n")
    transcript = temp_home / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"model": "claude-haiku-5-5"}}) + "\n"
        + json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n"
        + "not json\n"
    )
    payload = _agent_payload(subagent_type="inh", model="inherit")
    payload["transcript_path"] = str(transcript)
    _run_hook_with_payload(payload, temp_home)
    (row,) = _rows(temp_home)
    assert row["model"] == "claude-opus-5-5"  # the LAST assistant message


def test_unknown_model_writes_no_row(temp_home):
    """No tool_response model, no tool_input model, no definition, no transcript: UNKNOWN.

    Unknown is not a measured zero and not a favourable counterfactual, so nothing is
    written -- a row would be counted by every savings figure."""
    _run_hook_with_payload(_agent_payload(subagent_type="Explore", model=None), temp_home)
    assert _rows(temp_home) == []


def test_inherit_with_no_parent_is_unknown_and_not_counted(temp_home):
    _run_hook_with_payload(_agent_payload(model="inherit"), temp_home)
    assert _rows(temp_home) == []


def test_unpriced_current_model_writes_no_row(temp_home):
    """Fable 5.2 is current but has no entry in llm_router.pricing: no row, and no
    stand-in price from another model."""
    from llm_router import pricing

    assert pricing.price_for("claude-fable-5-2") is None  # if this starts failing, price it and drop this test
    _run_hook_with_payload(_agent_payload(model="fable"), temp_home)
    assert _rows(temp_home) == []


def test_unknown_full_id_is_not_priced_as_something_else(temp_home):
    _run_hook_with_payload(_agent_payload(model="claude-nonesuch-9"), temp_home)
    assert _rows(temp_home) == []


def test_known_full_id_outside_the_default_table_uses_registry_price(temp_home):
    _run_hook_with_payload(_agent_payload(model="claude-opus-4-8"), temp_home)
    (row,) = _rows(temp_home)
    assert row["model"] == "claude-opus-4-8"
    assert row["potential_cost_usd"] > 0


def test_definition_name_cannot_escape_the_agents_dir(temp_home):
    (temp_home / "evil.md").write_text("---\nmodel: opus\n---\n")
    _run_hook_with_payload(_agent_payload(subagent_type="../../evil", model=None), temp_home)
    assert _rows(temp_home) == []


def test_tests_write_only_to_the_temp_home(temp_home):
    """Isolation check: the DB the hook resolves is under tmp, never the operator's home."""
    mod = _load_hook()
    assert str(mod._db_path()).startswith(str(temp_home))
