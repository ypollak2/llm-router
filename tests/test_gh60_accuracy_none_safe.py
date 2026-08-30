"""GH#60 (part B): every renderer of a snapshot's "accuracy" field must
tolerate the honest GH#56 value (`None`, for "unmeasured") instead of
crashing on `None * 100`.

`retrospective.analyze_facts([])` deliberately returns
`classification_accuracy: None` for an empty decision set (GH#56 — an
unmeasured session must not look identical to a perfect one). But
`monitoring/periodic.py:91` writes that value straight into on-disk session
snapshots as `facts["accuracy"]`, and consumers across `commands/snapshot.py`,
`monitoring/live_tracker.py`, `monitoring/periodic.py`, and `retrospective.py`
itself read it back with `facts.get("accuracy", 1.0)` — a default that never
applies because the key is *present* with value `None`. The result was a
`TypeError` in `snapshot`, and a latent one in every other reader that would
have hit the same shape.

This includes files already written to disk under 13.0.4, before this fix
existed — `periodic.py:91` is left writing `None` (that is the correct,
honest value per GH#56); every reader below is what had to change to
tolerate it, including forever, for pre-existing snapshot files.
"""
from __future__ import annotations

from llm_router.commands.snapshot import (
    format_snapshot_status,
    format_hourly_snapshots,
    print_session_snapshot,
)
from llm_router.monitoring import live_tracker, periodic
from llm_router.retrospective import (
    analyze_facts,
    format_compact_summary,
    format_full_report,
)


# ── retrospective.py: the regression pin ────────────────────────────────────

def test_analyze_facts_empty_decisions_accuracy_stays_none():
    """Pin GH#56's intent: a future change must not quietly resurrect the
    fake-1.0 default for an unmeasured session."""
    facts = analyze_facts([], [])
    assert facts["classification_accuracy"] is None


# ── commands/snapshot.py ────────────────────────────────────────────────────

def _snapshot(accuracy):
    return {
        "hour": 1,
        "facts": {"total_calls": 3, "total_cost": 0.01, "total_saved": 0.05,
                   "accuracy": accuracy},
        "gap_count": 0,
        "action_count": 0,
    }


def test_format_snapshot_status_handles_none_accuracy():
    out = format_snapshot_status(_snapshot(None))
    assert "n/a" in out
    assert "None" not in out


def test_format_snapshot_status_still_shows_real_accuracy():
    out = format_snapshot_status(_snapshot(0.91))
    assert "91%" in out


def test_format_hourly_snapshots_handles_none_accuracy():
    snaps = [_snapshot(0.9), _snapshot(None)]
    out = format_hourly_snapshots(snaps)
    assert "n/a" in out
    assert "90%" in out


def test_print_session_snapshot_compact_handles_none_accuracy(monkeypatch, capsys):
    monkeypatch.setattr(
        "llm_router.commands.snapshot.load_session_snapshots",
        lambda date_str="": [_snapshot(0.95), _snapshot(None)],
    )
    print_session_snapshot(compact=True)
    out = capsys.readouterr().out
    assert "n/a" in out
    # The trend comparison must be skipped (not crash) when either side of
    # the comparison is unmeasured.
    assert "↓" not in out


def test_print_session_snapshot_full_handles_none_accuracy(monkeypatch, capsys):
    monkeypatch.setattr(
        "llm_router.commands.snapshot.load_session_snapshots",
        lambda date_str="": [_snapshot(None), _snapshot(None)],
    )
    print_session_snapshot(compact=False)
    out = capsys.readouterr().out
    assert "n/a" in out


# ── monitoring/live_tracker.py ──────────────────────────────────────────────

def test_get_live_trend_indicator_handles_none_accuracy(monkeypatch):
    monkeypatch.setattr(
        live_tracker, "load_session_snapshots",
        lambda: [_snapshot(0.9), _snapshot(None)],
    )
    indicator = live_tracker.get_live_trend_indicator()
    assert indicator != ""  # must not silently swallow into the except-all
    assert "n/a" in indicator


def test_display_hourly_progress_handles_none_accuracy(monkeypatch):
    monkeypatch.setattr(
        live_tracker, "load_session_snapshots",
        lambda: [_snapshot(None)],
    )
    line = live_tracker.display_hourly_progress()
    assert "n/a" in line


def test_get_trend_pressure_handles_none_accuracy(monkeypatch):
    monkeypatch.setattr(
        live_tracker, "load_session_snapshots",
        lambda: [_snapshot(0.9), _snapshot(None)],
    )
    # Must not raise, and must not treat "unmeasured" as a signal to escalate.
    assert live_tracker.get_trend_pressure() == 0.0


def test_get_trend_pressure_guards_none_explicitly_before_arithmetic():
    """White-box pin, not a tautology: `get_trend_pressure` wraps its whole
    body in `except Exception: return 0.0` (a deliberate fail-open — see the
    docstring), so a `None - float` TypeError from the old, un-guarded code
    was ALSO silently caught and returned 0.0 — the exact same value the
    fixed code returns on purpose. That makes
    `test_get_trend_pressure_handles_none_accuracy` above pass identically
    whether or not the fix is present (verified: it survives reverting the
    fix), so it cannot be the test that proves this consumer was fixed.

    This pins the actual fix — an explicit `is None` check that returns
    early, before the subtraction, so correctness no longer depends on an
    unrelated blanket exception handler staying in place. If a future
    refactor deletes the guard, this fails even though the blanket handler
    would still mask the crash from every caller.
    """
    import inspect

    src = inspect.getsource(live_tracker.get_trend_pressure)
    assert "is None" in src, (
        "no explicit None guard before the accuracy subtraction — behavior "
        "would again depend entirely on the blanket except-Exception "
        "fail-open, which is invisible to a black-box test"
    )


# ── monitoring/periodic.py ───────────────────────────────────────────────────

def test_analyze_session_trends_handles_none_accuracy():
    snaps = [_snapshot(None), _snapshot(0.9)]
    trend = periodic.analyze_session_trends(snaps)
    assert trend["trend_type"] == "unmeasured"
    assert trend["accuracy_change"] is None
    assert trend["first_accuracy"] is None


def test_format_trend_summary_handles_none_accuracy():
    trend = periodic.analyze_session_trends([_snapshot(None), _snapshot(None)])
    out = periodic.format_trend_summary(trend)
    assert "n/a" in out


# ── retrospective.py formatters ─────────────────────────────────────────────

def _facts(accuracy):
    return {
        "total_calls": 0, "total_cost": 0.0, "total_saved": 0.0,
        "duration_min": 0, "correction_count": 0,
        "classification_accuracy": accuracy,
    }


def test_format_compact_summary_handles_none_accuracy():
    out = format_compact_summary({"facts": _facts(None), "gaps": [], "actions": []})
    assert "n/a" in out


def test_format_full_report_handles_none_accuracy():
    out = format_full_report({
        "facts": _facts(None), "gaps": [], "root_causes": [], "actions": [],
    })
    assert "n/a" in out
