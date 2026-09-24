"""PR6 (North Star 8+12): `llm-router status` conflated "test traffic" with
"verified savings".

The status headline's verified/unverified split for `usage`/`claude_usage`/
`codex_usage`/`gemini_usage` used `dashboard_data._production_pred` — a drop
filter that excludes benchmark/pytest rows — as though it answered "was the
routed draft actually used instead of Claude's turn". It doesn't: only
`savings_stats`'s `mode='block'` predicate (PR #147,
`savings.VERIFIED_SAVED_SQL`) can answer that, and those four tables carry no
"used" column at all, so their money must always be unverified.

This file pins:

  1. a production (real, non-test) `usage` row is STILL never "verified" —
     production only ever meant "not a test fixture", never "was used".
  2. a `savings_stats` row observed to have replaced Claude's turn
     (mode='block') IS verified.
  3. mode='echo' (Claude answered anyway, the draft was discarded) is
     unverified.
  4. mode NULL (nobody recorded an outcome) is neither verified nor
     unverified for the PRIMARY METRIC — it is UNMEASURED, counted apart
     from both the numerator and the denominator (S9: unknown must not be
     the favourable answer, and must not be silently dropped either).
  5. under a Claude subscription, `llm-router status`'s savings panel never
     prints a bare `$` — every money figure carries `label_money`'s
     counterfactual qualifier.
  6. the primary metric's numerator and denominator both come from
     `savings_stats` alone, in the SAME window — seeded alongside a
     differently-sized `usage` table so a cross-table bug would give a
     different (and wrong) answer.
"""
from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timezone

from llm_router import dashboard_data


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _savings_stats_ddl(conn: sqlite3.Connection) -> None:
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


def _stats_row(conn, *, saved=1.0, host="claude_code", model="ollama/qwen2.5:7b",
               mode="block", is_simulated=0, ts=None):
    conn.execute(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, "
        "estimated_claude_cost_saved, external_cost, model_used, host, "
        "input_tokens, output_tokens, is_simulated, mode) "
        "VALUES (?, 's1', 'code', ?, 0.0, ?, ?, 10, 10, ?, ?)",
        (ts or _now_iso(), saved, model, host, is_simulated, mode),
    )


def _usage_ddl(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE usage (
        timestamp TEXT, provider TEXT, success INTEGER,
        input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
        is_simulated INTEGER
    )""")


def _usage_row(conn, *, in_tok=1_000_000, out_tok=0, cost=0.0, is_simulated=0, ts=None):
    conn.execute(
        "INSERT INTO usage VALUES (?, 'ollama', 1, ?, ?, ?, ?)",
        (ts or _now_iso(), in_tok, out_tok, cost, is_simulated),
    )


# ── 1. a production `usage` row never counts as verified ─────────────────────


def test_production_usage_row_never_counts_as_verified(tmp_path):
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _usage_ddl(conn)
    _usage_row(conn, is_simulated=0)  # real, production — but no "used" signal
    conn.commit()
    conn.close()

    t = dashboard_data.query_window("lifetime", db_path=db)
    assert t.saved_usd == 0.0, (
        "a production `usage` row has no 'used' column and must never be "
        "verified — only savings_stats mode='block' can be"
    )
    assert t.unverified_saved_usd > 0.0, (
        "the row's saving must still be counted — as unverified, not dropped"
    )


# ── 2/3. savings_stats mode='block' / mode='echo' ─────────────────────────────


def test_mode_block_is_verified(tmp_path):
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=2.0, mode="block")
    conn.commit()
    conn.close()

    t = dashboard_data.query_window("today", db_path=db)
    assert t.saved_usd == 2.0
    assert t.unverified_saved_usd == 0.0


def test_mode_echo_is_unverified(tmp_path):
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=2.0, mode="echo")
    conn.commit()
    conn.close()

    t = dashboard_data.query_window("today", db_path=db)
    assert t.saved_usd == 0.0
    assert t.unverified_saved_usd == 2.0


# ── 4. mode NULL is UNMEASURED for the primary metric ─────────────────────────


def test_mode_null_is_unverified_money_but_unmeasured_for_the_metric(tmp_path):
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=2.0, mode=None)
    conn.commit()
    conn.close()

    # savings.py's own established rule (unchanged by PR6): mode NULL is
    # unverified MONEY, not dropped.
    t = dashboard_data.query_window("today", db_path=db)
    assert t.saved_usd == 0.0
    assert t.unverified_saved_usd == 2.0

    # The PRIMARY METRIC is a different axis (a count of turns with a known
    # outcome, not a dollar figure): mode NULL means nobody recorded an
    # outcome at all, so it is neither a verified nor an unverified TURN —
    # it must not shrink the "confirmed not used" side of the ratio, and it
    # must not inflate the denominator as though it were measured.
    m = dashboard_data.query_primary_metric("today", db_path=db)
    assert m.verified_n == 0
    assert m.eligible_n == 0
    assert m.unmeasured_n == 1


# ── 6. primary metric: numerator + denominator, one table, same window ───────


def test_primary_metric_ignores_other_tables(tmp_path):
    """A cross-table bug (denominator from `usage`, numerator from
    `savings_stats`) would inflate `eligible_n` by the 500 `usage` rows
    seeded here. It must not — both sides come from savings_stats alone."""
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _usage_ddl(conn)
    for _ in range(500):
        _usage_row(conn, is_simulated=0)
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=1.0, mode="block")   # verified, eligible
    _stats_row(conn, saved=1.0, mode="echo")    # eligible, not verified
    _stats_row(conn, saved=1.0, mode=None)      # unmeasured — neither side
    conn.commit()
    conn.close()

    m = dashboard_data.query_primary_metric("today", db_path=db)
    assert m.eligible_n == 2, (
        f"denominator must come from savings_stats alone, not the 500-row "
        f"usage table — got {m.eligible_n}"
    )
    assert m.verified_n == 1
    assert m.unmeasured_n == 1


def test_primary_metric_render_too_few():
    m = dashboard_data.PrimaryMetric(window="today", verified_n=3, eligible_n=10, unmeasured_n=0)
    assert m.render() == "Verified share of eligible Claude turns: too few to tell (n=10)"


def test_primary_metric_render_percentage():
    m = dashboard_data.PrimaryMetric(window="today", verified_n=40, eligible_n=80, unmeasured_n=5)
    text = m.render()
    assert "40 of 80 (50%)" in text
    assert "unmeasured n=5" in text


def test_primary_metric_render_nothing_to_show():
    m = dashboard_data.PrimaryMetric(window="today", verified_n=0, eligible_n=0, unmeasured_n=0)
    assert m.render() == ""


# ── 5. subscription: `llm-router status`'s savings panel has no bare `$` ─────


def test_status_savings_panel_has_no_bare_dollar_under_subscription(
    tmp_path, monkeypatch, importing_a_submodule
):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "1")
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=3.5, mode="block")
    conn.commit()
    conn.close()

    from llm_router.ui import status_premium as sp

    cmd = sp.PremiumStatusCommand()
    cmd.db_path = db
    group = cmd.render_routing_savings()

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(group)
    text = buf.getvalue()

    assert "subscription" in text.lower(), (
        "under a subscription the money figure must carry the counterfactual "
        f"qualifier (label_money), got: {text!r}"
    )
    # The pre-fix line was literally f"${saved:.2f} saved" — a number
    # immediately followed by the bare word "saved" with no qualifier.
    assert not re.search(r"\$\d[\d,]*\.\d{2}\s+saved\b", text), (
        f"found a bare-dollar 'saved' line with no label_money qualifier: {text!r}"
    )


def test_status_savings_panel_labels_money_without_subscription_too(
    tmp_path, monkeypatch, importing_a_submodule
):
    """Control: the qualifier is R7's label_money, not a subscription-only
    special case — real dollars avoided still names its baseline."""
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=3.5, mode="block")
    conn.commit()
    conn.close()

    from llm_router.ui import status_premium as sp

    cmd = sp.PremiumStatusCommand()
    cmd.db_path = db
    group = cmd.render_routing_savings()

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(group)
    text = buf.getvalue()

    assert "real dollars avoided" in text, text
    assert not re.search(r"\$\d[\d,]*\.\d{2}\s+saved\b", text), (
        f"found a bare-dollar 'saved' line with no label_money qualifier: {text!r}"
    )
