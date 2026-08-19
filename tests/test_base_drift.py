"""G-METRIC-1 — durable base-model-drift ledger.

session_spend.json is reset each session, so the deduped override/routed counts
that reveal "drift back to base subscription models" (routable turns the main
model handled itself instead of routing) used to vanish. A durable INSERT OR
REPLACE row per session in routing_outcomes.db makes base drift measurable over
time; read_base_drift aggregates it. The DB derives from SESSION_SPEND_FILE.parent
so isolating SESSION_SPEND_FILE (as existing tests do) isolates it too.
"""
from __future__ import annotations

from pathlib import Path

from llm_router import session_spend as ss


def _isolate(tmp_path: Path, monkeypatch, session_key: str):
    # Isolating SESSION_SPEND_FILE also relocates routing_outcomes.db (derived).
    monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_key)


def test_empty_ledger_reports_no_drift(tmp_path):
    d = ss.read_base_drift("all", db_path=tmp_path / "absent.db")
    assert d["routed_turns"] == 0
    assert d["overridden_turns"] == 0
    assert d["base_drift_share"] == 0.0
    assert d["capture_rate"] == 1.0
    assert d["sessions"] == 0


def test_override_recorded_and_drift_computed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "sess-A")
    s = ss.SessionSpend()
    s.call_count = 4
    s.mark_overridden(1)  # marks + persists → durable upsert

    d = ss.read_base_drift("all")
    assert d["sessions"] == 1
    assert d["routed_turns"] == 4
    assert d["overridden_turns"] == 1
    assert d["base_drift_share"] == 0.25
    assert d["capture_rate"] == 0.75


def test_one_row_per_session_no_duplication(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "sess-A")
    s = ss.SessionSpend()
    s.call_count = 4
    s.mark_overridden(1)
    s.call_count = 6
    s.mark_overridden(2)  # same session → INSERT OR REPLACE, not a new row

    d = ss.read_base_drift("all")
    assert d["sessions"] == 1  # still one row
    assert d["routed_turns"] == 6
    assert d["overridden_turns"] == 2


def test_drift_aggregates_across_sessions(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "A")
    a = ss.SessionSpend()
    a.call_count = 10
    a.mark_overridden(1)                                                    # 1/10
    monkeypatch.setenv("CLAUDE_SESSION_ID", "B")
    b = ss.SessionSpend()
    b.call_count = 10
    b.mark_overridden(1)
    b.mark_overridden(2)                                                    # 2/10

    d = ss.read_base_drift("all")
    assert d["sessions"] == 2
    assert d["routed_turns"] == 20
    assert d["overridden_turns"] == 3
    assert d["base_drift_share"] == 0.15
    assert d["capture_rate"] == 0.85


def test_persist_never_raises_on_durable_error(monkeypatch):
    # Un-creatable parent → both the json write and the durable upsert must be
    # swallowed; spend tracking never crashes on disk issues.
    monkeypatch.setattr(ss, "SESSION_SPEND_FILE", Path("/proc/nonexistent/session_spend.json"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-A")
    s = ss.SessionSpend()
    s.call_count = 1
    s.mark_overridden(1)  # must not raise


# ── Version-scoped drift (routing-audit-agent alert uses only the last version) ──

def test_version_filter_scopes_to_matching_version(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "sess-old")
    monkeypatch.setattr(ss, "_current_llm_router_version", lambda: "0.8.5")
    a = ss.SessionSpend()
    a.call_count = 10
    a.mark_overridden(1)  # 1/10 drift, recorded as 0.8.5

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-new")
    monkeypatch.setattr(ss, "_current_llm_router_version", lambda: "0.8.6")
    b = ss.SessionSpend()
    b.call_count = 10
    b.mark_overridden(1)
    b.mark_overridden(2)
    b.mark_overridden(3)  # 3/10 drift, recorded as 0.8.6

    # Unversioned read aggregates both releases (back-compat).
    allv = ss.read_base_drift("all")
    assert allv["sessions"] == 2
    assert allv["overridden_turns"] == 4
    assert allv["version"] is None

    # Scoped to the latest version only.
    v86 = ss.read_base_drift("all", version="0.8.6")
    assert v86["sessions"] == 1
    assert v86["routed_turns"] == 10
    assert v86["overridden_turns"] == 3
    assert v86["base_drift_share"] == 0.3
    assert v86["capture_rate"] == 0.7
    assert v86["version"] == "0.8.6"

    # The prior version's rows are excluded from that scope.
    v85 = ss.read_base_drift("all", version="0.8.5")
    assert v85["sessions"] == 1
    assert v85["overridden_turns"] == 1
    assert v85["base_drift_share"] == 0.1


def test_version_current_resolves_running_version(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, "sess-cur")
    monkeypatch.setattr(ss, "_current_llm_router_version", lambda: "9.9.9")
    s = ss.SessionSpend()
    s.call_count = 4
    s.mark_overridden(1)

    d = ss.read_base_drift("all", version="current")
    assert d["version"] == "9.9.9"
    assert d["sessions"] == 1
    assert d["overridden_turns"] == 1

    # A different running version sees none of the older-version rows.
    monkeypatch.setattr(ss, "_current_llm_router_version", lambda: "0.0.1")
    other = ss.read_base_drift("all", version="current")
    assert other["sessions"] == 0
    assert other["version"] == "0.0.1"


def test_empty_ledger_reports_version_in_payload(tmp_path):
    d = ss.read_base_drift("all", version="current", db_path=tmp_path / "absent.db")
    assert d["sessions"] == 0
    assert "version" in d
