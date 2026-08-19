"""DASH-4 (D6) — the two routing counts come from different stores, so each
must name its source rather than imply they reconcile.

The session summary shows two "decision" counts in the routing area:

* ``_format_routing_logic`` — the classification-method mix, counted from
  ``model_tracking.jsonl`` (the classifier log).
* ``_query_router_efficiency`` — the fallback rate, counted from the
  ``routing_decisions`` table.

Both are "today", but they are *independent logs* with independent write
coverage, so their totals legitimately differ. Presenting both as bare
"decisions" invites a reader to expect them to match. The fix attributes each
count to its source ("classified … classifier log" vs "… routed") so the
divergence is explained, not hidden.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_HOOK_DIR = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
sys.path.insert(0, str(_HOOK_DIR))
_spec = importlib.util.spec_from_file_location("session_end", _HOOK_DIR / "session-end.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

_SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"


def _strip(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    # A dummy DB so _query_routing_logic's os.path.exists(DB_PATH) gate passes.
    db = tmp_path / "usage.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(se, "DB_PATH", str(db))
    monkeypatch.setattr(se, "STATE_DIR", str(tmp_path))
    # Seed the classifier log with a few of today's decisions.
    now = time.time()
    lines = [
        {"timestamp": now, "classification_method": "heuristic", "classification_confidence": 0.9},
        {"timestamp": now, "classification_method": "heuristic", "classification_confidence": 0.8},
        {"timestamp": now, "classification_method": "ollama", "classification_confidence": 0.6},
    ]
    (tmp_path / "model_tracking.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n"
    )
    return tmp_path


def test_routing_logic_header_attributes_its_source_and_window(routing_env):
    """Fail-before: the header says bare '{n} decisions' with no source. The
    classifier-log count must be attributed ('classified' + 'classifier log')
    and carry its window ('today') so it is not read as the routing-decisions
    total."""
    text = _strip("\n".join(se._format_routing_logic(None)))
    assert text, "expected a rendered routing-logic panel for seeded data"
    assert "classifier log" in text, "count must name its source (the classifier log)"
    assert "today" in text, "count must carry its window"
    assert "classified" in text, "the classifier-log count must be labelled 'classified'"
    # The bare word 'decisions' (identical to the routing-decisions panel) must
    # no longer stand in for the classifier-log count.
    assert "decisions" not in text, "must not label the classifier-log count 'decisions'"


def test_efficiency_total_is_attributed_as_routed():
    """Source guard: the router-efficiency fallbacks denominator (from the
    routing_decisions table) is labelled 'routed', distinguishing it from the
    classifier-log 'classified' count."""
    txt = (_SRC / "hooks" / "session-end.py").read_text()
    # Both the zero-fallback and the with-fallback branches must attribute the
    # total to the routing log.
    assert "fallbacks ({efficiency['total']} routed)" in txt or \
           "{efficiency['total']} routed" in txt, \
           "efficiency total must be attributed as 'routed'"
