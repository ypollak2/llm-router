"""NS4 — the quality breaker un-routes a class whose routed answers keep failing.

Each test targets one behaviour named in the NS4 brief, asserting the REASON a
decision was made (not only ``allowed``/``state`` booleans) per CLAUDE.md K7
("assert the reason, not just the boolean").
"""
from __future__ import annotations

import json

import pytest

from llm_router import quality_breaker as qb

LEVER = "mcp_llm"
TASK = "query"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))


def _unit(outcome: str, ts: float = 1_000_000.0, lever: str = LEVER, task_type: str = TASK,
          model: str = "ollama/qwen") -> dict:
    from datetime import datetime, timezone
    return {
        "session_id": "s1",
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "kind": "routed_mcp",
        "lever": lever,
        "task_type": task_type,
        "model": model,
        "outcome": outcome,
        "signal": "test",
    }


def _units_fn(units: list[dict]):
    return lambda **_kwargs: list(units)


# ── opens at threshold with n >= min_n, and NOT below ───────────────────────

def test_opens_when_failure_rate_and_n_clear_the_defaults():
    # 20 classified units (default min_n), all failing (rate=1.0 >= 0.5 default).
    units = [_unit("redo", ts=1000.0 + i) for i in range(20)]
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.allowed is False
    assert d.state == qb.OPEN
    assert d.n == 20
    assert d.failure_rate == 1.0
    assert "OPENED" in d.reason and "n=20" in d.reason


def test_does_not_open_below_min_n_even_at_100pct_failure():
    # 19 classified units — one below the default min_n of 20.
    units = [_unit("redo", ts=1000.0 + i) for i in range(19)]
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.allowed is True
    assert d.state == qb.CLOSED
    assert d.n == 19
    assert "closed" in d.reason


def test_does_not_open_below_threshold_even_with_plenty_of_n():
    # 40 classified units, only 10 failing => failure_rate 0.25 < 0.5 default.
    units = ([_unit("redo", ts=1000.0 + i) for i in range(10)]
             + [_unit("used", ts=1100.0 + i) for i in range(30)])
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.allowed is True
    assert d.state == qb.CLOSED
    assert d.failure_rate == pytest.approx(0.25)


def test_opens_exactly_at_the_threshold_boundary(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_MIN_N", "20")
    # Exactly 20 classified, exactly 10 failing => rate == 0.5 == threshold (>=, not >).
    units = ([_unit("redo", ts=1000.0 + i) for i in range(10)]
             + [_unit("used", ts=1100.0 + i) for i in range(10)])
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.state == qb.OPEN
    assert d.failure_rate == 0.5


# ── unknown excluded from the rate, reported separately ─────────────────────

def test_unknown_units_are_excluded_from_the_rate_and_reported():
    # 25 "unknown" units plus 20 classified units at exactly the 0.5 boundary.
    # If unknown polluted the denominator the rate would drop well under 0.5
    # and the class would stay closed; it must not.
    units = ([_unit("unknown", ts=900.0 + i) for i in range(25)]
             + [_unit("redo", ts=1000.0 + i) for i in range(10)]
             + [_unit("used", ts=1100.0 + i) for i in range(10)])
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.n == 20  # classified count, NOT 45
    assert d.failure_rate == 0.5
    assert d.unknown == 25
    assert d.state == qb.OPEN


def test_all_unknown_window_never_opens_and_never_reads_as_favourable():
    # window_size() default is 50; use exactly that so the assertion isn't
    # coupled to a constant this test doesn't own.
    units = [_unit("unknown", ts=1000.0 + i) for i in range(qb.window_size())]
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.allowed is True
    assert d.n == 0
    assert d.failure_rate is None
    assert d.unknown == qb.window_size()


# ── half-open probe: closes on a good probe, reopens on a bad one ──────────

def _open_state(now: float, opened_at: float) -> dict:
    return {"classes": {qb.class_key(LEVER, TASK): {
        "state": qb.OPEN, "opened_at": opened_at, "n": 20, "failure_rate": 1.0,
    }}}


def test_half_open_probe_closes_on_a_good_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S", "10")
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_PROBE_SIZE", "5")
    opened_at = 1_000_000.0
    qb._write_state(_open_state(opened_at, opened_at))

    # Step 1: cooldown elapsed -> half_open, no probe units yet.
    now_half_open = opened_at + 11
    d1 = qb.should_route(LEVER, TASK, now=now_half_open, units_fn=_units_fn([]))
    assert d1.state == qb.HALF_OPEN
    assert d1.allowed is True
    assert "half_open" in d1.reason

    # Step 2: 5 good probe units land after entering half-open.
    probe = [_unit("used", ts=now_half_open + 1 + i) for i in range(5)]
    d2 = qb.should_route(LEVER, TASK, now=now_half_open + 20, units_fn=_units_fn(probe))
    assert d2.state == qb.CLOSED
    assert d2.allowed is True
    assert "CLOSED" in d2.reason and "probe passed" in d2.reason


def test_half_open_probe_reopens_on_a_bad_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S", "10")
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_PROBE_SIZE", "5")
    opened_at = 1_000_000.0
    qb._write_state(_open_state(opened_at, opened_at))

    now_half_open = opened_at + 11
    qb.should_route(LEVER, TASK, now=now_half_open, units_fn=_units_fn([]))

    probe = [_unit("redo", ts=now_half_open + 1 + i) for i in range(5)]
    d2 = qb.should_route(LEVER, TASK, now=now_half_open + 20, units_fn=_units_fn(probe))
    assert d2.state == qb.OPEN
    assert d2.allowed is False
    assert "REOPENED" in d2.reason and "probe failed" in d2.reason


def test_half_open_stays_open_pending_enough_probe_units():
    opened_at = 1_000_000.0
    qb._write_state(_open_state(opened_at, opened_at))
    with_env = {"LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S": "10"}
    import os
    for k, v in with_env.items():
        os.environ[k] = v
    try:
        qb.should_route(LEVER, TASK, now=opened_at + 11, units_fn=_units_fn([]))
        # Only 2 of the required 5 probe units have landed.
        probe = [_unit("redo", ts=opened_at + 12 + i) for i in range(2)]
        d = qb.should_route(LEVER, TASK, now=opened_at + 20, units_fn=_units_fn(probe))
        assert d.state == qb.HALF_OPEN
        assert d.allowed is True
        assert "pending" in d.reason and "2/5" in d.reason
    finally:
        for k in with_env:
            os.environ.pop(k, None)


def test_half_open_all_unknown_probe_stays_open_and_pending():
    """S9: an inconclusive probe must not read as a pass."""
    opened_at = 1_000_000.0
    qb._write_state(_open_state(opened_at, opened_at))
    import os
    os.environ["LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S"] = "10"
    os.environ["LLM_ROUTER_QUALITY_BREAKER_PROBE_SIZE"] = "5"
    try:
        now_half_open = opened_at + 11
        qb.should_route(LEVER, TASK, now=now_half_open, units_fn=_units_fn([]))
        probe = [_unit("unknown", ts=now_half_open + 1 + i) for i in range(5)]
        d = qb.should_route(LEVER, TASK, now=now_half_open + 20, units_fn=_units_fn(probe))
        assert d.state == qb.HALF_OPEN
        assert d.allowed is True
        assert "inconclusive" in d.reason
    finally:
        os.environ.pop("LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S", None)
        os.environ.pop("LLM_ROUTER_QUALITY_BREAKER_PROBE_SIZE", None)


# ── corrupt state file fails open ───────────────────────────────────────────

def test_corrupt_state_file_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    p = tmp_path / qb.STATE_FILE
    p.write_text("{not json", encoding="utf-8")
    # Even with a batch that would otherwise open the class, a corrupt state
    # file must not crash — it starts every class from "closed, no history"
    # for THIS call, and still records the fresh evaluation honestly.
    units = [_unit("redo", ts=1000.0 + i) for i in range(20)]
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn(units))
    assert d.allowed is False  # the FRESH evaluation still opens it — no crash either way
    assert d.state == qb.OPEN


def test_corrupt_state_file_records_a_failopen(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    p = tmp_path / qb.STATE_FILE
    p.write_text("not even json {{{", encoding="utf-8")
    from llm_router import failopen
    failopen.reset_cache()
    before = failopen.snapshot().total or 0
    qb.should_route(LEVER, TASK, units_fn=_units_fn([]))
    after = failopen.snapshot().total or 0
    assert after > before


def test_wrong_shaped_state_file_fails_open_too(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    p = tmp_path / qb.STATE_FILE
    p.write_text(json.dumps({"classes": "not-a-dict"}), encoding="utf-8")
    d = qb.should_route(LEVER, TASK, units_fn=_units_fn([]))
    assert d.allowed is True
    assert d.state == qb.CLOSED


# ── the draft auto-revert (I5) behaviour is preserved ───────────────────────

def test_drafts_lever_delegates_to_i5_unused_streak(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.hooks import draft_usage
    monkeypatch.setenv("LLM_ROUTER_DRAFT_REVERT_AFTER", "3")
    # Two unused verdicts in a row: below the (lowered) revert threshold.
    draft_usage.record_draft("s1", 1.0, "ollama/qwen")
    draft_usage.audit("s1", "no relay marker here")
    draft_usage.record_draft("s1", 2.0, "ollama/qwen")
    draft_usage.audit("s1", "still no relay marker")
    d = qb.should_route("drafts", "query")
    assert d.allowed is True
    assert d.state == qb.CLOSED
    assert "streak 2" in d.reason

    # Third unused verdict trips I5's own streak-of-3 revert.
    draft_usage.record_draft("s1", 3.0, "ollama/qwen")
    draft_usage.audit("s1", "still nothing")
    d2 = qb.should_route("drafts", "query")
    assert d2.allowed is False
    assert d2.state == qb.OPEN
    assert "I5" in d2.reason and "3 drafts unused" in d2.reason

    # A relayed draft resets I5's streak — quality_breaker sees that too.
    draft_usage.record_draft("s1", 4.0, "ollama/qwen")
    draft_usage.audit("s1", "🎯 LLM Router routed → ollama/qwen · query/simple · 1s\n\nok")
    d3 = qb.should_route("drafts", "query")
    assert d3.allowed is True
    assert "streak 0" in d3.reason


def test_open_classes_folds_in_the_draft_lever(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.hooks import draft_usage
    monkeypatch.setenv("LLM_ROUTER_DRAFT_REVERT_AFTER", "1")
    draft_usage.record_draft("s1", 1.0, "ollama/qwen")
    draft_usage.audit("s1", "no marker")
    rows = qb.open_classes()
    keys = {r["key"] for r in rows}
    assert qb.class_key("drafts", None) in keys


def test_stop_line_summary_none_when_nothing_open(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert qb.stop_line_summary() is None


def test_stop_line_summary_counts_open_classes(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    qb._write_state({"classes": {
        "mcp_llm:query": {"state": qb.OPEN, "n": 20, "failure_rate": 0.9},
        "direct:code": {"state": qb.HALF_OPEN, "n": 5, "failure_rate": 0.4},
    }})
    line = qb.stop_line_summary()
    assert line == "breaker: 2 classes off"


# ── per-model refinement ─────────────────────────────────────────────────────

def test_per_model_key_used_only_when_it_alone_clears_min_n():
    # Coarse class has 20 failing units split across two models, 10 each —
    # neither model alone clears the default min_n of 20, so the decision
    # must be keyed on the COARSE class, not silently drop to a thin model.
    units = ([_unit("redo", ts=1000.0 + i, model="model-a") for i in range(10)]
             + [_unit("redo", ts=1100.0 + i, model="model-b") for i in range(10)])
    d = qb.should_route(LEVER, TASK, model="model-a", units_fn=_units_fn(units))
    assert d.key == qb.class_key(LEVER, TASK, None)


def test_per_model_key_used_when_that_model_alone_clears_min_n():
    units = [_unit("redo", ts=1000.0 + i, model="model-a") for i in range(20)]
    d = qb.should_route(LEVER, TASK, model="model-a", units_fn=_units_fn(units))
    assert d.key == qb.class_key(LEVER, TASK, "model-a")


# ── dry_run: read-only, no persistence ──────────────────────────────────────

def test_dry_run_does_not_touch_the_real_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    units = [_unit("redo", ts=1000.0 + i) for i in range(20)]
    rows = qb.dry_run(days=30, units_fn=_units_fn(units))
    assert any(r["lever"] == LEVER and r["task_type"] == TASK and r["would_be"] == qb.OPEN
               for r in rows)
    assert not (tmp_path / qb.STATE_FILE).exists()


def test_dry_run_evaluates_drafts_generically_not_via_the_live_i5_streak(tmp_path, monkeypatch):
    """dry_run answers "what would the rate-based breaker say", for every
    lever alike — including drafts, where PRODUCTION gating instead delegates
    to I5's live streak (test_drafts_lever_delegates_to_i5_unused_streak).
    A real streak of 0 (freshly reset) must not mask a historically bad
    draft class in the dry run the way it would in should_route()."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.hooks import draft_usage
    draft_usage.record_draft("s1", 1.0, "m")
    draft_usage.audit("s1", draft_usage.RELAY_MARKER + " → m\n\nok")  # streak resets to 0

    units = ([_unit("discarded", ts=1000.0 + i, lever="drafts", task_type=None)
              for i in range(30)])
    rows = qb.dry_run(days=30, units_fn=_units_fn(units))
    draft_rows = [r for r in rows if r["lever"] == "drafts"]
    assert draft_rows and draft_rows[0]["would_be"] == qb.OPEN
    assert draft_rows[0]["n"] == 30
