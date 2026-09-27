"""2026-09-27: one savings truth across `status` / `savings-report` / `gain` /
the statusline.

An audit of a real machine found the four found user-facing savings surfaces
disagreeing at the same instant:

  * `llm-router status`      all-time n=9224, baseline claude-opus-5
  * `llm-router savings-report`  all-time n=150,  baseline claude-opus-4
  * `llm-router gain`        7-day n=28 (a DIFFERENT table, routing_decisions),
                              Opus baseline $0.00 for every free/local call
  * the statusline           today's figure, baseline claude-opus-5

Root causes, each pinned by a test below:

  1. `savings._baseline_model()` hardcoded `"claude-opus-4"` independently of
     `pricing.savings_baseline_model()` (`"claude-opus-5"`) — see
     `tests/economics/test_single_baseline_policy.py::
     test_savings_module_delegates_to_the_policy` for the dedicated fix.
  2. `llm-router savings-report` computed its headline from
     `cost.get_realized_savings` (reads `claude_usage`/`codex_usage`/
     `gemini_usage` only) while `llm-router status` and the statusline read
     `dashboard_data.query_window`'s UNION of five tables — different row
     counts by construction, not by arithmetic error.
  3. `llm-router gain` priced its Opus baseline by multiplying the ACTUAL
     dollar cost by a per-model multiplier; for a free/local route that cost
     is correctly `$0.00`, so `0 * anything == 0` — every local call reported
     a $0.00 baseline regardless of how many tokens it used.

This file pins the fix: `dashboard_data.summary(period)` is now the ONE
function `status`, `savings-report`, `gain`'s canonical line, and the
statusline (via `query_window` + `render_money`, which `summary()` composes)
all call, over the SAME fixture database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from llm_router import dashboard_data, pricing, savings


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_db(path) -> None:
    """A `savings_stats`-only fixture (no `usage`/`claude_usage`/etc — see
    `commands/savings_report.py`'s module docstring for the one known
    residual asymmetry those tables introduce, which this fixture sidesteps
    by construction so every surface's totals reconcile exactly).

    Four rows, timestamped "now" (after `savings.REALIZED_GATE_SINCE`,
    inside every surface's "today" window):

      1. host=claude_code, mode='block'  -> VERIFIED   $0.05 (n=1)
      2. host=claude_code, mode=NULL     -> unverified  $0.03 (eligible,
                                            UNMEASURED for the primary metric)
      3. host=router,       mode=NULL    -> unverified  $0.02 (not eligible:
                                            host != claude_code)
      4. host=claude_code, agentic model -> unverified  $0.10 (not eligible:
                                            model_used LIKE 'llm_router-agentic%')

    Expected canonical figure: realized $0.05 (n=1), unverified $0.15 (n=3),
    unmeasured n=1, routed n=4.
    """
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE savings_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        session_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        estimated_claude_cost_saved REAL NOT NULL,
        external_cost REAL NOT NULL DEFAULT 0,
        model_used TEXT NOT NULL,
        host TEXT NOT NULL DEFAULT 'claude_code',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        is_simulated INTEGER,
        mode TEXT
    )""")
    ts = _now_iso()
    rows = [
        (ts, "s1", "code", 0.05, 0.0, "ollama/a", "claude_code", 0, "block"),
        (ts, "s2", "code", 0.03, 0.0, "ollama/a", "claude_code", 0, None),
        (ts, "s3", "code", 0.02, 0.0, "ollama/b", "router", 0, None),
        (ts, "s4", "code", 0.10, 0.0, "llm_router-agentic-x", "claude_code", 0, "block"),
    ]
    conn.executemany(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, "
        "estimated_claude_cost_saved, external_cost, model_used, host, "
        "is_simulated, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fixture_home(tmp_path, monkeypatch):
    """LLM_ROUTER_HOME pointed at a tmp dir holding the seeded usage.db — so
    every surface's DEFAULT db-path resolution (`paths.state_path`,
    `paths.llm_router_home`) lands on the SAME fixture, the way it would on a
    real machine."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    db = tmp_path / "usage.db"
    _seed_db(db)
    return tmp_path, db


# ── The canonical figure itself ───────────────────────────────────────────


def test_summary_matches_the_hand_derived_expectation(fixture_home):
    _, db = fixture_home
    s = dashboard_data.summary("today", db_path=db)
    assert s.realized_usd == pytest.approx(0.05)
    assert s.realized_n == 1
    assert s.unverified_usd == pytest.approx(0.15)
    assert s.unverified_n == 3
    assert s.unmeasured_n == 1
    assert s.routed_n == 4
    assert s.baseline_model == pricing.savings_baseline_model()


def test_by_model_reconciles_with_the_headline_when_nothing_is_simulated(fixture_home):
    """The known residual noted in `commands/savings_report.py`'s docstring
    (query_model_savings drops is_simulated=1 rows; query_window does not)
    does not apply here — this fixture has none — so the parts must sum to
    the whole exactly."""
    _, db = fixture_home
    s = dashboard_data.summary("today", db_path=db)
    by_model = dashboard_data.query_model_savings("today", db_path=db)
    assert by_model["verified_saved"] == pytest.approx(s.realized_usd)
    assert by_model["unverified_saved"] == pytest.approx(s.unverified_usd)
    assert by_model["verified_calls"] == s.realized_n
    assert by_model["unverified_calls"] == s.unverified_n


# ── Cross-surface agreement ────────────────────────────────────────────────


def _status_figures(db_path):
    """(realized_usd, realized_n, unverified_usd, unverified_n, baseline) as
    `llm-router status` renders them for the "Today" row — read straight off
    the `rich.console.Group`'s `Text` renderables (`.plain`), not off
    console-rendered/wrapped text, so a narrow terminal width can't corrupt
    the parse."""
    from llm_router.savings import label_money  # noqa: F401 (documents the format read below)
    from llm_router.ui.status_premium import PremiumStatusCommand

    cmd = PremiumStatusCommand()
    cmd.db_path = db_path
    group = cmd.render_routing_savings()
    lines = [r.plain for r in group.renderables]
    text = "\n".join(lines)

    import re

    m_verified = re.search(
        r"Today.*?\$([\d.]+) (?:real dollars avoided|baseline-equivalent avoided[^v]*) "
        r"vs ([\w.-]+) \(n=(\d+)\)",
        text, re.S,
    )
    assert m_verified, f"could not find Today's verified figure in:\n{text}"
    m_unverified = re.search(r"\+\s*\$([\d.]+) unverified, n=(\d+)", text)
    assert m_unverified, f"could not find Today's unverified figure in:\n{text}"
    return (
        float(m_verified.group(1)), int(m_verified.group(3)),
        float(m_unverified.group(1)), int(m_unverified.group(2)),
        m_verified.group(2),
    )


def _savings_report_figures(period="day"):
    import re

    from llm_router.commands.savings_report import render_savings_report

    text = render_savings_report(period)
    m = re.search(
        r"verified \$([\d.]+) \(n=(\d+)\) . unverified estimate \$([\d.]+) \(n=(\d+)\)"
        r".*?baseline (\S+)",
        text,
    )
    assert m, f"could not find the canonical headline in:\n{text}"
    return (
        float(m.group(1)), int(m.group(2)), float(m.group(3)), int(m.group(4)),
        m.group(5),
    )


def _gain_figures(period="today"):
    import re

    from llm_router.commands.gain import show_gain

    text = show_gain(period)
    m = re.search(
        r"verified \$([\d.]+) \(n=(\d+)\) . unverified estimate \$([\d.]+) \(n=(\d+)\)"
        r".*?baseline (\S+)",
        text,
    )
    assert m, f"could not find the canonical headline in gain's output:\n{text}"
    return (
        float(m.group(1)), int(m.group(2)), float(m.group(3)), int(m.group(4)),
        m.group(5),
    )


def _statusline_figures(db_path):
    """The exact two calls `hooks/statusline-command.sh` makes
    (`query_window("today")` + fields `render_money` reads) — read as data,
    not by shelling out to the bash script, since the script's job is only to
    find a Python and pass this call's result through."""
    totals = dashboard_data.query_window("today", db_path=db_path)
    return (
        totals.saved_usd, totals.verified_calls,
        totals.unverified_saved_usd, totals.unverified_calls,
    )


def test_four_surfaces_report_identical_figures(fixture_home):
    _, db = fixture_home
    canonical = dashboard_data.summary("today", db_path=db)

    status = _status_figures(db)
    report = _savings_report_figures("day")
    gain = _gain_figures("today")
    statusline = _statusline_figures(db)

    for label, (v_usd, v_n, u_usd, u_n, *rest) in (
        ("status", status), ("savings-report", report), ("gain", gain),
    ):
        assert v_usd == pytest.approx(canonical.realized_usd), label
        assert v_n == canonical.realized_n, label
        assert u_usd == pytest.approx(canonical.unverified_usd), label
        assert u_n == canonical.unverified_n, label
        assert rest[0] == canonical.baseline_model, label

    # statusline: same two underlying numbers, read directly off query_window
    # (what render_money actually formats) rather than parsed text.
    assert statusline[0] == pytest.approx(canonical.realized_usd)
    assert statusline[1] == canonical.realized_n
    assert statusline[2] == pytest.approx(canonical.unverified_usd)
    assert statusline[3] == canonical.unverified_n
