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

Reviewer-01 (live-reproduced) added two more, both fixed here:

  7. the `n` printed beside the VERIFIED `$` figure is the row count BEHIND
     it (savings_stats rows passing the verified predicate) — never raw
     call volume across the five UNION'd tables. Live output read
     "$0.00 … (n=47260)" with verified_n actually 0.
  8. the North Star primary-metric line renders on its own, independent of
     the per-window money loop — which `continue`s past any window with
     zero activity, "Today" most days on a real install — so it must not
     vanish along with an empty Today.
"""
from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone

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
    m = dashboard_data.PrimaryMetric(window="lifetime", verified_n=3, eligible_n=10, unmeasured_n=0)
    assert m.render() == (
        "Verified share of eligible Claude turns (all-time): too few to tell (n=10)"
    )


def test_primary_metric_render_percentage():
    m = dashboard_data.PrimaryMetric(window="lifetime", verified_n=40, eligible_n=80, unmeasured_n=5)
    text = m.render()
    assert "(all-time)" in text, "the rendered line must state which window it covers"
    assert "40 of 80 (50%)" in text
    assert "unmeasured n=5" in text
    assert "mode not recorded" in text, "the NULL handling must be stated in the line"


def test_primary_metric_render_nothing_to_show():
    m = dashboard_data.PrimaryMetric(window="today", verified_n=0, eligible_n=0, unmeasured_n=0)
    assert m.render() == ""


# ── 7. verified `n` is the verified ROW COUNT, never raw call volume ─────────


def test_window_totals_verified_calls_is_the_verified_row_count(tmp_path):
    """`WindowTotals.verified_calls` must count ONLY savings_stats rows
    passing the verified predicate — not `calls` (raw activity across all
    five UNION'd tables) and not derivable from `saved_usd` alone (a
    genuinely verified row can itself have saved $0.00)."""
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _usage_ddl(conn)
    for _ in range(10):
        _usage_row(conn, is_simulated=0)  # 10 real rows, never verified
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=2.0, mode="block")   # the one verified row
    _stats_row(conn, saved=1.0, mode="echo")    # eligible, not verified
    conn.commit()
    conn.close()

    t = dashboard_data.query_window("today", db_path=db)
    assert t.calls == 12, "premise: raw activity volume across every table"
    assert t.verified_calls == 1, (
        f"verified_calls must count ONLY the mode='block' row, got {t.verified_calls}"
    )


def test_status_verified_line_n_is_verified_rows_not_call_volume(
    tmp_path, importing_a_submodule
):
    """Reviewer-01, live-reproduced: status_premium's money line showed
    "$0.00 … (n=47260)" — raw call volume across five UNION'd tables — while
    the actual row count behind the dollar figure was far smaller.

    2026-09-27: the panel now shows ONE merged estimate
    (`Summary.estimated_n` = `realized_n + unverified_n`), so a fixture
    where every row carries money (the original 10 usage + 1 savings_stats
    rows) can no longer discriminate the fix from the bug — both would
    coincidentally print the same `n`. This fixture adds 5 NON-production
    usage rows (`is_simulated=1`), which `_production_pred` drops from the
    money figure ENTIRELY (see `dashboard_data._production_pred`'s
    docstring) but which still count toward raw `calls` — so the invariant
    ("the n behind the $ figure, never unfiltered activity volume") stays
    exercised: `estimated_n` is 11 (10 unverified usage rows + 1 verified
    savings_stats row), while raw `calls` is 16.
    """
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _usage_ddl(conn)
    for _ in range(10):
        _usage_row(conn, is_simulated=0)
    for _ in range(5):
        _usage_row(conn, is_simulated=1)  # dropped from money entirely
    _savings_stats_ddl(conn)
    _stats_row(conn, saved=2.0, mode="block")
    conn.commit()
    conn.close()

    from llm_router import dashboard_data
    from llm_router.ui import status_premium as sp

    canonical = dashboard_data.summary("today", db_path=db)
    assert canonical.estimated_n == 11, "premise: 10 unverified + 1 verified"
    totals = dashboard_data.query_window("today", db_path=db)
    assert totals.calls == 16, "premise: raw activity volume is wider (16)"

    cmd = sp.PremiumStatusCommand()
    cmd.db_path = db
    group = cmd.render_routing_savings()

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(group)
    text = buf.getvalue()

    money_lines = [ln for ln in text.splitlines() if "est. saved" in ln]
    assert money_lines, f"no money line found: {text!r}"
    for ln in money_lines:
        assert "(n=11)" in ln and "n=16" not in ln and "n=15" not in ln, (
            f"the money line must say n=11 (the row count behind the $ "
            f"figure), not raw call volume: {ln!r}"
        )


# ── 8. the primary metric renders independent of the per-window money loop ──


def test_primary_metric_renders_even_when_today_is_empty(tmp_path, importing_a_submodule):
    """Reviewer-01, live-reproduced: on a real install "Today" is empty most
    of the time, and the per-window money loop `continue`s straight past an
    empty window — the North Star line must not be gated behind it."""
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    _savings_stats_ddl(conn)
    # Dated yesterday, NOT today — the "Today" window must be genuinely empty.
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    _stats_row(conn, saved=2.0, mode="block", ts=yesterday)
    _stats_row(conn, saved=1.0, mode="echo", ts=yesterday)
    conn.commit()
    conn.close()

    # Premise: Today really is empty.
    today_totals = dashboard_data.query_window("today", db_path=db)
    assert today_totals.calls == 0, "premise: today has no activity anywhere"

    from llm_router.ui import status_premium as sp

    cmd = sp.PremiumStatusCommand()
    cmd.db_path = db
    group = cmd.render_routing_savings()

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(group)
    text = buf.getvalue()

    assert "Verified share of eligible Claude turns" in text, (
        f"the North Star line must render even when Today is empty: {text!r}"
    )
    # n=2 is below TOO_FEW_THRESHOLD (50), so it prints "too few to tell" —
    # the point here is that it renders AT ALL, picking up yesterday's rows
    # via the "all-time" window despite Today being empty, not the exact
    # percentage wording (covered by test_primary_metric_render_percentage).
    assert "too few to tell (n=2)" in text, text


# ── 5. `llm-router status`'s savings panel has no bare `$` ───────────────────
#
# 2026-09-27 SCOPE NOTE: these two tests used to assert R7's subscription-
# specific wording (`label_money`'s "real dollars avoided" / "subscription:
# no cash changed hands"). The panel no longer calls `label_money` at all —
# it renders `Summary.display()`, and `dashboard_data.Summary` has no
# subscription field (that framing belongs to `savings.CanonicalSavings`,
# a different, still-unchanged code path used by `doctor`/`explain-dashboard`
# — see those commands' own `canonical_savings()` calls). The invariant these
# tests still protect — a bare, unqualified `$X` must never appear beside a
# quota-style panel — is checked directly below; the subscription-specific
# half of the original assertions is intentionally dropped, not silently
# broken, because `Summary.display()` structurally cannot print one (its
# only qualifier is "est. saved ... vs BASELINE", subscription or not).


def test_status_savings_panel_has_no_bare_dollar(tmp_path, importing_a_submodule):
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

    assert "est. saved" in text and "vs claude-opus" in text, (
        f"the money figure must carry its 'est.'/baseline qualifier: {text!r}"
    )
    # The pre-fix line was literally f"${saved:.2f} saved" — a number
    # immediately followed by the bare word "saved" with no qualifier.
    assert not re.search(r"\$\d[\d,]*\.\d{2}\s+saved\b", text), (
        f"found a bare-dollar 'saved' line with no qualifier: {text!r}"
    )
    # Scoped to the MONEY lines only — the panel's separate North Star line
    # ("Verified share of eligible Claude turns", a different metric, out of
    # this task's scope) legitimately says "verified" and must not trip this.
    money_lines = [ln for ln in text.splitlines() if "est. saved" in ln]
    assert money_lines and not any("verified" in ln.lower() for ln in money_lines), (
        f"'verified'/'unverified' must not reach the money line: {money_lines!r}"
    )


def test_status_savings_panel_labels_money_under_subscription_too(
    tmp_path, monkeypatch, importing_a_submodule
):
    """Control: the qualifier does not depend on subscription state — the
    figure is an estimate either way, and `Summary.display()` takes no
    subscription input at all."""
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

    assert "est. saved" in text and "vs claude-opus" in text, text
    assert not re.search(r"\$\d[\d,]*\.\d{2}\s+saved\b", text), (
        f"found a bare-dollar 'saved' line with no qualifier: {text!r}"
    )
