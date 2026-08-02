"""Tests for retrospective analysis engine.

Tests cover:
- Session window detection
- Data collection from usage.db
- IAF debrief analysis (facts, gaps, causes, actions)
- File I/O (retrospective.md, directives.md)
- Output formatting
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from llm_router.retrospective import (
    analyze_facts,
    analyze_gaps,
    build_retrospective,
    classify_root_causes,
    format_compact_summary,
    format_full_report,
    generate_actions,
    get_session_window,
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def sample_decisions() -> list[dict]:
    """Sample routing decisions for testing."""
    return [
        {
            "id": 1,
            "timestamp": "2026-04-17T14:30:00+00:00",
            "task_type": "code",
            "classifier_confidence": 0.95,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "cost_usd": 0.0001,
            "saved_usd": 0.001,
            "judge_score": 0.95,
        },
        {
            "id": 2,
            "timestamp": "2026-04-17T14:31:00+00:00",
            "task_type": "security_review",
            "classifier_confidence": 0.23,  # Low!
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "cost_usd": 0.0002,
            "saved_usd": 0.0005,
            "judge_score": None,  # No feedback yet
        },
        {
            "id": 3,
            "timestamp": "2026-04-17T14:32:00+00:00",
            "task_type": "security_review",
            "classifier_confidence": 0.25,  # Low again!
            "recommended_model": "sonnet",
            "final_model": "sonnet",  # Downgraded due to budget
            "success": 1,
            "cost_usd": 0.0005,
            "saved_usd": 0.0008,
            "judge_score": 0.74,  # Low quality
        },
        {
            "id": 4,
            "timestamp": "2026-04-17T14:33:00+00:00",
            "task_type": "research",
            "classifier_confidence": 0.85,
            "recommended_model": "sonnet",
            "final_model": "sonnet",
            "success": 1,
            "cost_usd": 0.0003,
            "saved_usd": 0.001,
            "judge_score": 0.92,
        },
    ]


@pytest.fixture
def sample_corrections() -> list[dict]:
    """Sample manual corrections (user escalations)."""
    return [
        {"decision_id": 2, "reason": "User escalated to opus"},
        {"decision_id": 3, "reason": "User escalated to opus"},
    ]


# ── Session Window Tests ───────────────────────────────────────────────────

def test_get_session_window_with_valid_file(tmp_path, monkeypatch):
    """Test session window detection with valid session_start.txt."""
    session_file = tmp_path / "session_start.txt"
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    session_file.write_text(str(ts))

    monkeypatch.setattr(
        "llm_router.retrospective.SESSION_START_FILE", session_file
    )

    start, end = get_session_window()

    assert start <= end
    assert (end - start).total_seconds() < 60  # Should be very recent


def test_get_session_window_fallback(tmp_path, monkeypatch):
    """Test session window detection falls back to 2 hours if file missing."""
    fake_file = tmp_path / "nonexistent.txt"
    monkeypatch.setattr(
        "llm_router.retrospective.SESSION_START_FILE", fake_file
    )

    start, end = get_session_window()

    assert start < end
    duration = (end - start).total_seconds() / 3600
    assert 1.9 < duration < 2.1  # Close to 2 hours


# ── Facts Analysis Tests ───────────────────────────────────────────────────

def test_analyze_facts_basic(sample_decisions, sample_corrections):
    """Test basic facts collection."""
    facts = analyze_facts(sample_decisions, sample_corrections)

    assert facts["total_calls"] == len(sample_decisions)
    assert facts["correction_count"] == len(sample_corrections)
    assert facts["total_cost"] == pytest.approx(0.0011, abs=0.0001)
    assert facts["total_saved"] == pytest.approx(0.0033, abs=0.0001)
    assert facts["duration_min"] == 3  # 14:30 to 14:33
    assert facts["task_distribution"] == {
        "code": 1,
        "security_review": 2,
        "research": 1,
    }
    assert facts["avg_confidence"] == pytest.approx(0.57, abs=0.01)


def test_analyze_facts_empty_session():
    """Test facts analysis with empty session."""
    facts = analyze_facts([], [])

    assert facts["total_calls"] == 0
    assert facts["total_cost"] == 0.0
    assert facts["total_saved"] == 0.0
    assert facts["classification_accuracy"] == 1.0


def test_analyze_facts_accuracy():
    """Test accuracy calculation based on corrections."""
    decisions = [
        {"timestamp": "2026-04-17T14:30:00+00:00", "task_type": "code"},
        {"timestamp": "2026-04-17T14:31:00+00:00", "task_type": "analysis"},
    ]
    corrections = [
        {"decision_id": 1, "reason": "wrong"},
    ]

    facts = analyze_facts(decisions, corrections)

    # 1 correction / 2 decisions = 50% incorrect = 50% accuracy
    assert facts["classification_accuracy"] == pytest.approx(0.5, abs=0.01)


# ── WS2/WS3 Context Enrichment Tests (WS7) ─────────────────────────────────

def test_analyze_facts_includes_ws2_ws3_context_keys(sample_decisions, sample_corrections):
    """analyze_facts()'s main-case return is additively wrapped with WS2/WS3
    population-level context — both keys must always be present, even if the
    underlying data source is empty/unavailable (fail-open -> None)."""
    facts = analyze_facts(sample_decisions, sample_corrections)

    assert "mis_route_rate_inferred_baseline" in facts
    assert "realized_savings_today_usd" in facts


def test_analyze_facts_empty_session_includes_ws2_ws3_context_keys():
    """The empty-decisions early-return path is also wrapped, not just the
    main case — both WS7 context keys must be present here too."""
    facts = analyze_facts([], [])

    assert "mis_route_rate_inferred_baseline" in facts
    assert "realized_savings_today_usd" in facts


def test_analyze_facts_ws2_context_fails_open(monkeypatch):
    """A broken/missing WS2 routing_quality module must never raise into
    analyze_facts() — the field falls back to None."""
    import llm_router.routing_quality as routing_quality

    def _boom():
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(routing_quality, "summarize", _boom)

    facts = analyze_facts([], [])

    assert facts["mis_route_rate_inferred_baseline"] is None


def test_analyze_facts_ws3_context_fails_open(monkeypatch):
    """A broken/missing WS3 dashboard_data module must never raise into
    analyze_facts() — the field falls back to None."""
    import llm_router.dashboard_data as dashboard_data

    def _boom(window, *, db_path=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(dashboard_data, "query_realized_savings", _boom)

    facts = analyze_facts([], [])

    assert facts["realized_savings_today_usd"] is None


# ── Gaps Analysis Tests ────────────────────────────────────────────────────

def test_analyze_gaps_low_confidence(sample_decisions, sample_corrections):
    """Test gap detection for low confidence."""
    gaps = analyze_gaps(sample_decisions, sample_corrections)

    # Decision 2 and 3 have low confidence (< 0.70)
    security_gaps = [g for g in gaps if g["task_type"] == "security_review"]
    assert len(security_gaps) == 2
    assert all("LOW_CONFIDENCE" in g["flags"] for g in security_gaps)


def test_analyze_gaps_low_quality(sample_decisions, sample_corrections):
    """Test gap detection for low quality score."""
    gaps = analyze_gaps(sample_decisions, sample_corrections)

    # Decision 3 has judge_score 0.74 < 0.85
    quality_gaps = [g for g in gaps if "LOW_QUALITY" in g["flags"]]
    assert len(quality_gaps) >= 1


def test_analyze_gaps_manual_correction(sample_decisions, sample_corrections):
    """Test gap detection for manual corrections."""
    gaps = analyze_gaps(sample_decisions, sample_corrections)

    # 2 corrections in sample
    override_gaps = [g for g in gaps if "MANUAL_OVERRIDE" in g["flags"]]
    assert len(override_gaps) == 2


def test_analyze_gaps_empty_session():
    """Test gap analysis with empty session."""
    gaps = analyze_gaps([], [])
    assert gaps == []


def test_analyze_gaps_audited_misroute():
    """WS6's offline audit_verdict column, when 'likely_misroute', must raise
    the AUDITED_MISROUTE flag — read directly, not re-derived from
    judge_score."""
    decisions = [
        {
            "id": 6,
            "timestamp": "2026-04-17T14:40:00+00:00",
            "task_type": "code",
            "classifier_confidence": 0.95,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "cost_usd": 0.0001,
            "saved_usd": 0.0002,
            "audit_verdict": "likely_misroute",
        },
    ]

    gaps = analyze_gaps(decisions, [])

    audited = [g for g in gaps if "AUDITED_MISROUTE" in g["flags"]]
    assert len(audited) == 1
    assert audited[0]["decision_id"] == 6


def test_analyze_gaps_audit_verdict_other_values_no_flag():
    """Only 'likely_misroute' should raise the flag — 'likely_correct' and
    'insufficient_data' must not."""
    decisions = [
        {
            "id": 7,
            "timestamp": "2026-04-17T14:41:00+00:00",
            "task_type": "code",
            "classifier_confidence": 0.95,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "audit_verdict": "likely_correct",
        },
        {
            "id": 8,
            "timestamp": "2026-04-17T14:42:00+00:00",
            "task_type": "code",
            "classifier_confidence": 0.95,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "audit_verdict": "insufficient_data",
        },
    ]

    gaps = analyze_gaps(decisions, [])

    assert not any("AUDITED_MISROUTE" in g["flags"] for g in gaps)


# ── Root Cause Classification Tests ────────────────────────────────────────

def test_classify_root_causes_classifier_error(sample_decisions, sample_corrections):
    """Test root cause classification for classifier errors."""
    # Create a decision with classifier error but NO manual override
    decisions = [
        {
            "id": 5,
            "timestamp": "2026-04-17T14:35:00+00:00",
            "task_type": "analysis",
            "classifier_confidence": 0.25,  # Low!
            "recommended_model": "sonnet",
            "final_model": "sonnet",
            "success": 1,
            "cost_usd": 0.0005,
            "saved_usd": 0.0008,
            "judge_score": 0.74,  # Low quality
        },
    ]
    corrections = []  # No manual override

    gaps = analyze_gaps(decisions, corrections)
    causes = classify_root_causes(gaps)

    # Decision 5: low confidence + low quality = classifier error
    classifier_errors = [
        c for c in causes if c["root_cause"] == "CLASSIFIER_ERROR"
    ]
    assert len(classifier_errors) >= 1


def test_classify_root_causes_profile_stale(sample_decisions, sample_corrections):
    """Test root cause classification for stale profile."""
    gaps = analyze_gaps(sample_decisions, sample_corrections)
    causes = classify_root_causes(gaps)

    # 2 manual overrides for security_review = stale profile
    stale_causes = [
        c for c in causes
        if c["root_cause"] == "PROFILE_STALE" and c["task_type"] == "security_review"
    ]
    assert len(stale_causes) >= 1


def test_classify_root_causes_audited_misroute():
    """A gap carrying the AUDITED_MISROUTE flag must classify to the
    AUDITED_MISROUTE root cause at confidence=2 (high — independent offline
    audit signal), and must not be shadowed by other root causes."""
    decisions = [
        {
            "id": 6,
            "timestamp": "2026-04-17T14:40:00+00:00",
            "task_type": "code",
            "classifier_confidence": 0.95,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "audit_verdict": "likely_misroute",
        },
    ]

    gaps = analyze_gaps(decisions, [])
    causes = classify_root_causes(gaps)

    audited_causes = [c for c in causes if c["root_cause"] == "AUDITED_MISROUTE"]
    assert len(audited_causes) == 1
    assert audited_causes[0]["confidence"] == 2
    assert audited_causes[0]["gap_id"] == 6


# ── Action Generation Tests ────────────────────────────────────────────────

def test_generate_actions_escalation(sample_decisions, sample_corrections):
    """Test action generation for profile update rules."""
    gaps = analyze_gaps(sample_decisions, sample_corrections)
    causes = classify_root_causes(gaps)
    actions = generate_actions(causes, sample_corrections, sample_decisions)

    # Should generate profile update for security_review (PROFILE_STALE = 2 overrides)
    profile_updates = [
        a for a in actions if a["type"] == "PROFILE_UPDATE"
    ]
    assert len(profile_updates) >= 1


def test_generate_actions_empty():
    """Test action generation with no gaps."""
    actions = generate_actions([], [], [])
    assert actions == []


# ── Full Retrospective Tests ───────────────────────────────────────────────

def test_build_retrospective(sample_decisions, sample_corrections):
    """Test full retrospective assembly."""
    start = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 17, 14, 33, tzinfo=timezone.utc)

    retro = build_retrospective(start, end, sample_decisions, sample_corrections)

    assert retro["session_start"] == start.isoformat()
    assert retro["session_end"] == end.isoformat()
    assert "facts" in retro
    assert "gaps" in retro
    assert "root_causes" in retro
    assert "actions" in retro
    assert "timestamp_generated" in retro

    # Verify facts
    assert retro["facts"]["total_calls"] == 4
    assert retro["facts"]["correction_count"] == 2

    # Verify gaps detected
    assert len(retro["gaps"]) > 0

    # Verify actions generated
    assert len(retro["actions"]) > 0


# ── Output Formatting Tests ────────────────────────────────────────────────

def test_format_compact_summary(sample_decisions, sample_corrections):
    """Test compact summary formatting."""
    start = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 17, 14, 33, tzinfo=timezone.utc)
    retro = build_retrospective(start, end, sample_decisions, sample_corrections)

    output = format_compact_summary(retro)

    assert "Retrospective" in output
    assert "Accuracy" in output
    assert "Gaps" in output
    assert "Actions" in output


def test_format_full_report(sample_decisions, sample_corrections):
    """Test full report formatting."""
    start = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 17, 14, 33, tzinfo=timezone.utc)
    retro = build_retrospective(start, end, sample_decisions, sample_corrections)

    output = format_full_report(retro)

    assert "SESSION RETROSPECTIVE" in output
    assert "【FACTS】" in output
    assert "【GAPS】" in output
    assert "【ROOT CAUSES】" in output
    assert "【ACTIONS】" in output
    assert "47 calls" not in output  # Different from sample
    assert "4 calls" in output


def test_format_full_report_empty():
    """Test full report formatting with empty retrospective."""
    retro = {
        "facts": {},
        "gaps": [],
        "root_causes": [],
        "actions": [],
    }

    output = format_full_report(retro)

    assert "SESSION RETROSPECTIVE" in output
    assert "0 calls" in output


def test_format_full_report_includes_ws2_ws3_context_lines(sample_decisions, sample_corrections):
    """The Facts section of the full report must surface the WS2/WS3
    population-level context fields, defensively formatted."""
    start = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 17, 14, 33, tzinfo=timezone.utc)
    retro = build_retrospective(start, end, sample_decisions, sample_corrections)

    output = format_full_report(retro)

    # Values are population-level and environment-dependent (may be a number
    # or "N/A" in a clean test DB) — assert the labels are present, not a
    # specific figure.
    assert "misroute rate" in output.lower() or "mis_route_rate" in output.lower() or "baseline" in output.lower()


# ── Brand Leak Tests (WS7) ──────────────────────────────────────────────────

def test_retrospective_module_has_no_unallowed_brand_leak():
    """No public/private name in the retrospective module may contain
    'chuzom' — the only allowed occurrence anywhere in the codebase is the
    provenance-header pattern accepted by scripts/ci/check_identity.py, which
    this module does not use (it predates the migration and is native code,
    not a literal port)."""
    import llm_router.retrospective as retrospective

    for name in dir(retrospective):
        assert "chuzom" not in name.lower(), f"brand leak in name: {name}"


def test_retrospective_output_never_leaks_brand(sample_decisions, sample_corrections):
    """Every string value anywhere in a built retrospective, and in both
    rendered report formats, must be free of 'chuzom' — output identity is
    llm-router only."""
    start = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 17, 14, 33, tzinfo=timezone.utc)
    retro = build_retrospective(start, end, sample_decisions, sample_corrections)

    def _walk(obj):
        if isinstance(obj, str):
            assert "chuzom" not in obj.lower(), f"brand leak in value: {obj!r}"
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v)

    _walk(retro)
    assert "chuzom" not in format_full_report(retro).lower()
    assert "chuzom" not in format_compact_summary(retro).lower()
