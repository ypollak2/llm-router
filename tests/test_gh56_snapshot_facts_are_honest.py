"""GH#56: snapshots asserted zeros they had not measured.

Three snapshot files on the reporter's disk, spanning two days, were
byte-for-byte identical on the facts block:

    "facts": {"total_calls": 0, "total_cost": 0.0, "total_saved": 0.0,
              "accuracy": 1.0, "duration_min": 0}

One was timestamped one second after a real llm_query completed.

This is NOT an independent bug — it is GH#55 one layer down.
`run_session_retrospective` -> `fetch_session_decisions` reads the
`routing_decisions` table; when that table is empty (because the session routed
through one of the other three same-named writers) `analyze_facts` returns its
zero literals, and the snapshot records them as measured fact.

`accuracy: 1.0` is the tell: a perfect score derived from nothing. A snapshot
that means "I could not see your activity" must not be indistinguishable from
"you had no activity" — the same defect fixed in doctor for GH#55.
"""
from __future__ import annotations

from llm_router.retrospective import analyze_facts


def test_no_decisions_is_marked_unmeasured_not_zero():
    facts = analyze_facts([], [])
    assert facts.get("measured") is False, (
        "facts from an empty decision set must be flagged unmeasured; otherwise "
        "'I saw nothing' is written to disk as 'nothing happened'"
    )


def test_real_decisions_are_marked_measured():
    # Shaped like a real routing_decisions row: fetch_session_decisions does
    # SELECT *, so "timestamp" is always present.
    facts = analyze_facts(
        [{"timestamp": "2026-08-28T10:00:00+00:00", "task_type": "query",
          "complexity": "simple", "final_model": "ollama/llama3.1:8b",
          "classifier_confidence": 0.9, "cost_usd": 0.0}],
        [],
    )
    assert facts.get("measured") is True
    assert facts["total_calls"] == 1


def test_accuracy_is_not_asserted_as_perfect_from_nothing():
    """1.0 out of zero samples is the number that made the files look real."""
    facts = analyze_facts([], [])
    acc = facts.get("classification_accuracy", facts.get("accuracy"))
    assert acc is None, (
        f"accuracy={acc!r} was derived from zero decisions; it must be None, "
        f"not a perfect score"
    )


def test_zero_counts_are_still_present_for_existing_readers():
    """The keys must not vanish — snapshot.py and share.py read them directly."""
    facts = analyze_facts([], [])
    for key in ("total_calls", "total_cost", "total_saved", "duration_min"):
        assert key in facts, f"{key} disappeared; existing readers would KeyError"


def test_injected_savings_still_flow_through():
    """total_saved is derived by the caller from the ledger, not from decisions."""
    facts = analyze_facts([], [], total_saved=1.25)
    assert facts["total_saved"] == 1.25


def test_end_to_end_snapshot_round_trip(tmp_path, monkeypatch):
    """A written-then-read snapshot must carry the unmeasured flag through JSON."""
    import json

    facts = analyze_facts([], [])
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"facts": facts}))

    loaded = json.loads(path.read_text())["facts"]
    assert loaded["measured"] is False, "the flag did not survive serialization"
    assert loaded["total_calls"] == 0
