"""GH#53: "routed" must mean an execution, not a hint.

The reporter chased what looked like a reporting bug: snapshot/status/doctor/
verify/last/replay all read usage.db and correctly showed zero on a session
with no llm() call yet, while the Stop-line badge and model_tracking.jsonl
showed non-zero — because those are populated by the UserPromptSubmit hook's
classification step, i.e. "a hint was emitted", not "a model was called". Both
numbers were right; they answered different questions under one word.

Maintainer's decision: "routed" is reserved for real executions.

Investigating this, the distinction was already made in session-end.py under
"D6" — the classifier-log count is labelled "classified" with its source and
window, and only routing_decisions-derived counts say "routed". This file does
not change that; it pins it, because the labelling is a convention that a
future edit can silently undo and only a reader comparing two dashboards would
notice.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"
_SESSION_END = _SRC / "hooks" / "session-end.py"


def test_classifier_log_count_is_not_called_routed():
    """model_tracking.jsonl counts classifications, not executions."""
    body = _SESSION_END.read_text()
    start = body.index("} classified")
    window = body[start - 600:start + 300]
    assert "classified" in window
    assert not re.search(r"total_hits\}[^\"]*routed", window), (
        "the classifier-log count is labelled 'routed'; it counts hints emitted"
    )


def test_classified_count_names_its_source_and_window():
    """Two numbers under one word is the defect; attribution is the fix."""
    body = _SESSION_END.read_text()
    i = body.index("} classified")
    window = body[i:i + 400]
    assert "classifier log" in window, "the classified count does not name its source"
    assert "today" in window, "the classified count does not name its window"


def test_routed_labels_are_backed_by_execution_data():
    """Every 'routed' label must sit with real cost/token figures."""
    body = _SESSION_END.read_text()
    for m in re.finditer(r"^.*\}\s*routed.*$", body, re.M):
        line = m.group(0)
        if "classified" in line:
            continue
        # A compact badge carries no figures; it must instead be provably
        # sourced from an executions count (see the `routes` scrape).
        ok = any(t in line for t in ("tok", "$", "fallback", "="))
        ok = ok or "routes.group" in line
        assert ok, f"a 'routed' label with no execution figures beside it: {line.strip()}"


def test_hud_counter_records_after_execution_not_at_classification():
    """statusline_hud's 'Total routed' must be fed post-response.

    tools/routing.py calls record_routing_decision with resp.model and
    resp.cost_usd — fields that only exist once a model has answered. If that
    call ever moves ahead of the response, the HUD starts counting hints under
    the word 'routed' and this issue returns.
    """
    routing = (_SRC / "tools" / "routing.py").read_text()
    call = routing[routing.index("record_routing_decision("):][:400]
    assert "resp.model" in call and "resp.cost_usd" in call, (
        "the HUD counter no longer reads from the response — it may now be "
        "counting classifications rather than executions"
    )
