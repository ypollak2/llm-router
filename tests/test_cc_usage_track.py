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
import os
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

FIXTURES = Path(__file__).parent / "fixtures" / "hook_payloads"


def _rows(temp_home: Path) -> list[dict]:
    db = temp_home / ".llm-router" / "usage.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM usage ORDER BY id")]
    conn.close()
    return out


def _one(temp_home: Path) -> dict:
    (row,) = _rows(temp_home)
    return row


def _assert_unknown_row(row: dict, *, model: str = "unknown") -> None:
    """The explicit UNKNOWN outcome: visible, but no money and no tokens for any reader to count."""
    assert row["model"] == model
    assert row["provider"] == "cc"
    assert row["input_tokens"] == 0 and row["output_tokens"] == 0
    assert row["potential_cost_usd"] is None and row["saved_usd"] is None
    assert row["baseline_model"] is None or model != "unknown"
    assert row["cost_usd"] == 0.0
    assert row["reason"].startswith("claude_code_subscription:unknown_model:")


def _transcript(temp_home: Path, *records: object, raw_tail: str = "") -> str:
    path = temp_home / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + raw_tail)
    return str(path)


def _assistant(model: str, **extra) -> dict:
    return {"type": "assistant", "message": {"model": model}, **extra}


def test_recorded_fixture_has_no_model_so_the_unknown_path_is_covered(temp_home):
    """The captured payload carries no model anywhere: an UNKNOWN row, not a guessed Sonnet."""
    payload = json.loads((FIXTURES / "cc-usage-track.json").read_text())
    assert "model" not in payload["tool_input"] and "resolvedModel" not in payload["tool_response"]
    _run_hook_with_payload(payload, temp_home)
    row = _one(temp_home)
    _assert_unknown_row(row)
    assert row["reason"].endswith("no_model_source_transcript_unreadable")


def test_fixture_with_model_prices_the_row(temp_home):
    payload = json.loads((FIXTURES / "cc-usage-track-with-model.json").read_text())
    _run_hook_with_payload(payload, temp_home)
    row = _one(temp_home)
    assert row["model"] == "claude-haiku-5-5" and row["reason"] == "claude_code_subscription"
    assert row["potential_cost_usd"] > 0


@pytest.mark.parametrize(
    ("alias", "model"),
    [("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5")],
)
def test_tool_input_model_alias_maps_to_current_priced_model(temp_home, alias, model):
    """Explore is not Haiku: the Agent call's model parameter decides, at the registry price."""
    _run_hook_with_payload(_agent_payload(subagent_type="Explore", model=alias), temp_home)
    row = _one(temp_home)
    assert row["model"] == model
    from llm_router import pricing

    p = pricing.price_for(model)
    assert row["potential_cost_usd"] == pytest.approx(
        (p.input * row["input_tokens"] + p.output * row["output_tokens"]) / 1e6, rel=1e-6)
    assert row["saved_usd"] == row["potential_cost_usd"]


def test_alias_map_lives_in_the_registry_and_its_targets_are_consistent():
    from llm_router import pricing

    assert pricing.claude_code_model("Opus") == pricing.resolve("opus") == "claude-opus-5-5"
    assert pricing.claude_code_model("nonesuch") is None
    for alias, target in pricing.CLAUDE_CODE_MODEL_ALIASES.items():
        # a current model: priced, or (Fable 5.2) deliberately unpriced -- never priced as another
        assert target.startswith("claude-" + alias)
        priced = pricing.price_for(target)
        assert priced is None or priced.model_id == pricing.resolve(target)
    assert "claude-sonnet-5-5" in pricing.known_models() and "claude-haiku-5-5" in pricing.known_models()


def test_tool_response_resolved_model_wins_over_tool_input(temp_home):
    payload = _agent_payload(model="haiku")
    payload["tool_response"]["resolvedModel"] = "claude-opus-5-5"
    _run_hook_with_payload(payload, temp_home)
    assert _one(temp_home)["model"] == "claude-opus-5-5"


def test_unrecognized_resolved_model_is_unknown_not_a_fallthrough(temp_home):
    payload = _agent_payload(model="haiku")
    payload["tool_response"]["resolvedModel"] = "gpt-9"
    _run_hook_with_payload(payload, temp_home)
    row = _one(temp_home)
    _assert_unknown_row(row)
    assert row["reason"].endswith("unrecognized_model_in_tool_response")


def _definition(temp_home: Path, name: str, model_line: str, base: Path | None = None) -> None:
    agents = (base or temp_home) / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(f"---\nname: {name}\n{model_line}\n---\nbody model: haiku\n")


def test_agent_definition_frontmatter_model(temp_home):
    _definition(temp_home, "reviewer", "model: opus")
    _run_hook_with_payload(_agent_payload(subagent_type="reviewer", model=None), temp_home)
    assert _one(temp_home)["model"] == "claude-opus-5-5"


def test_project_definition_beats_user_definition(temp_home):
    _definition(temp_home, "w", "model: haiku")
    _definition(temp_home, "w", "model: sonnet", base=temp_home / "proj")
    payload = _agent_payload(subagent_type="w", model=None)
    payload["cwd"] = str(temp_home / "proj")
    _run_hook_with_payload(payload, temp_home)
    assert _one(temp_home)["model"] == "claude-sonnet-5-5"


def test_absent_model_consults_definition_but_explicit_inherit_goes_to_parent(temp_home):
    _definition(temp_home, "d", "model: haiku")
    tp = _transcript(temp_home, _assistant("claude-opus-5-5"))
    for tool_model, expected in ((None, "claude-haiku-5-5"), ("inherit", "claude-opus-5-5")):
        (temp_home / ".llm-router" / "usage.db").unlink(missing_ok=True)
        payload = _agent_payload(subagent_type="d", model=tool_model)
        payload["transcript_path"] = tp
        _run_hook_with_payload(payload, temp_home)
        assert _one(temp_home)["model"] == expected, tool_model


def test_definition_inherit_or_no_model_line_goes_to_parent(temp_home):
    _definition(temp_home, "i", "model: inherit")
    _definition(temp_home, "n", "description: x")
    tp = _transcript(temp_home, _assistant("claude-sonnet-5-5"))
    for name in ("i", "n"):
        (temp_home / ".llm-router" / "usage.db").unlink(missing_ok=True)
        payload = _agent_payload(subagent_type=name, model=None)
        payload["transcript_path"] = tp
        _run_hook_with_payload(payload, temp_home)
        assert _one(temp_home)["model"] == "claude-sonnet-5-5", name


def test_unrecognized_definition_model_is_unknown(temp_home):
    _definition(temp_home, "u", "model: banana")
    _run_hook_with_payload(_agent_payload(subagent_type="u", model=None), temp_home)
    row = _one(temp_home)
    _assert_unknown_row(row)
    assert row["reason"].endswith("unrecognized_model_in_definition")


def test_parent_uses_last_main_chain_real_record_skipping_sidechain_and_synthetic(temp_home):
    tp = _transcript(
        temp_home,
        _assistant("claude-haiku-5-5"),
        _assistant("claude-opus-5-5"),                       # the latest REAL main-chain record
        _assistant("claude-sonnet-5-5", isSidechain=True),   # a sub-agent's record: not the parent
        _assistant("<synthetic>"),
        {"type": "user", "message": {"content": "hi"}},
        raw_tail="not json\n",
    )
    payload = _agent_payload(model="inherit")
    payload["transcript_path"] = tp
    _run_hook_with_payload(payload, temp_home)
    assert _one(temp_home)["model"] == "claude-opus-5-5"


def test_unrecognized_latest_parent_model_is_unknown_not_an_older_model(temp_home):
    tp = _transcript(temp_home, _assistant("claude-haiku-5-5"), _assistant("mystery-model"))
    payload = _agent_payload(model="inherit")
    payload["transcript_path"] = tp
    _run_hook_with_payload(payload, temp_home)
    row = _one(temp_home)
    _assert_unknown_row(row)
    assert row["reason"].endswith("unrecognized_parent_model")


def test_truncated_transcript_tail_without_a_complete_record_is_unknown(temp_home, monkeypatch):
    big = temp_home / "big.jsonl"
    big.write_text(json.dumps(_assistant("claude-opus-5-5")) + "\n" + "x" * 5000 + "\n")
    mod = _load_hook()
    monkeypatch.setattr(mod, "_MAX_TRANSCRIPT_TAIL_BYTES", 1000)  # the opus record is outside the tail
    assert mod._parent_model(str(big))[0] == "absent"
    big.write_text(json.dumps(_assistant("claude-opus-5-5"))[:30])  # cut mid-record
    assert mod._parent_model(str(big))[0] == "absent"


def test_reads_are_bounded_and_irregular_paths_never_raise(temp_home, monkeypatch):
    mod = _load_hook()
    monkeypatch.setattr(mod, "_MAX_DEFINITION_BYTES", 20)
    _definition(temp_home, "long", "model: opus")  # the model line sits past byte 20
    assert mod._definition_model("long", None)[0] == "absent"
    assert mod._parent_model("a\x00b")[0] == "absent"                 # NUL in the path
    assert mod._parent_model(str(temp_home))[0] == "absent"           # a directory
    fifo = temp_home / "fifo"
    os.mkfifo(fifo)
    assert mod._parent_model(str(fifo))[0] == "absent"                # not a regular file, and no block
    assert mod._definition_model("x\x00y", "a\x00b")[0] == "absent"
    payload = _agent_payload(model="inherit")
    payload["transcript_path"] = "a\x00b"
    _run_hook_with_payload(payload, temp_home)
    _assert_unknown_row(_one(temp_home))


def test_definition_symlink_cannot_escape_the_agents_dir(temp_home):
    outside = temp_home / "outside.md"
    outside.write_text("---\nmodel: opus\n---\n")
    agents = temp_home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "link.md").symlink_to(outside)
    mod = _load_hook()
    assert mod._definition_model("link", None)[0] == "absent"
    assert mod._definition_model("../../outside", None)[0] == "absent"


def test_unknown_model_writes_a_visible_row_that_is_not_a_saving(temp_home):
    _run_hook_with_payload(_agent_payload(subagent_type="Explore", model=None), temp_home)
    _assert_unknown_row(_one(temp_home))


def test_unpriced_current_model_keeps_its_name_and_counts_no_money(temp_home):
    """Fable 5.2 is current but unpriced in llm_router.pricing: a row naming it, no stand-in price."""
    from llm_router import pricing

    assert pricing.price_for("claude-fable-5-2") is None  # if this starts failing, price it and drop this test
    _run_hook_with_payload(_agent_payload(model="fable"), temp_home)
    row = _one(temp_home)
    _assert_unknown_row(row, model="claude-fable-5-2")
    assert row["reason"].endswith("unpriced_model")
    assert row["baseline_model"] is None


def test_unknown_full_id_is_not_priced_as_something_else(temp_home):
    _run_hook_with_payload(_agent_payload(model="claude-nonesuch-9"), temp_home)
    _assert_unknown_row(_one(temp_home), model="claude-nonesuch-9")


def test_known_full_id_outside_the_aliases_uses_registry_price(temp_home):
    _run_hook_with_payload(_agent_payload(model="claude-opus-4-8"), temp_home)
    row = _one(temp_home)
    assert row["model"] == "claude-opus-4-8" and row["potential_cost_usd"] > 0


def test_unknown_rows_add_nothing_to_any_money_reader(temp_home):
    """Run the ledger's own readers over a known row plus an unknown one: only the known adds money."""
    from llm_router import ledger_view

    _run_hook_with_payload(_agent_payload(model="sonnet"), temp_home)
    known = _rows(temp_home)[0]
    _run_hook_with_payload(_agent_payload(model=None), temp_home)
    unknown = _rows(temp_home)[1]
    db = temp_home / ".llm-router" / "usage.db"
    con = sqlite3.connect(str(db))
    try:
        total = con.execute("SELECT COUNT(*), SUM(saved_usd), SUM(potential_cost_usd), SUM(input_tokens) FROM usage").fetchone()
    finally:
        con.close()
    assert total[0] == 2                                  # the unknown call stays visible
    assert total[1] == pytest.approx(known["saved_usd"])  # NULL adds nothing
    assert total[2] == pytest.approx(known["potential_cost_usd"])
    assert total[3] == known["input_tokens"]              # 0 tokens: nothing for a token-priced reader
    assert ledger_view.provider_class("cc", reason=unknown["reason"]) == "subscription"
    assert ledger_view.subscription_seat("cc", reason=unknown["reason"]) == ledger_view.SEAT_CLAUDE


def test_happy_path_writes_no_fail_open_record(temp_home, monkeypatch):
    """A hook that fails open hides its own bugs (a NameError once looked like 'no row')."""
    from llm_router import failopen

    seen: list = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, *a, **k: seen.append((code, exc)))
    _run_hook_with_payload(_agent_payload(model="opus"), temp_home)
    _run_hook_with_payload(_agent_payload(model=None), temp_home)
    assert seen == []
    assert len(_rows(temp_home)) == 2


def test_definition_name_cannot_escape_the_agents_dir(temp_home):
    (temp_home / "evil.md").write_text("---\nmodel: opus\n---\n")
    _run_hook_with_payload(_agent_payload(subagent_type="../../evil", model=None), temp_home)
    _assert_unknown_row(_one(temp_home))


def test_tests_write_only_to_the_temp_home(temp_home):
    """Isolation check: the DB the hook resolves is under tmp, never the operator's home."""
    mod = _load_hook()
    assert str(mod._db_path()).startswith(str(temp_home))
