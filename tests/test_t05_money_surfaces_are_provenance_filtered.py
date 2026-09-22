"""Every money surface must exclude synthetic rows — audit 2026-09-22, T-05.

The 2026-09-21 C-02 fix added write-time provenance to `usage` and a filter to
ONE reader. Five siblings read the same rows with no filter, and the one with no
filter was `get_team_savings` — the surface `team.py` broadcasts to a shared
Slack/Discord channel. Reproduced before the fix: a single synthetic row
produced a **$27.00 team-savings broadcast** while the dashboard, on the same
data, correctly said $0.00.

THIS FILE ASSERTS THE CALL SITE, NOT THE DEFINITION (Rule B).

Testing that `production_only()` returns the right SQL string would have passed
throughout the entire period the bug existed, because the helper was never the
problem — nobody called it. So every test here runs the real public function
against a real SQLite ledger holding exactly one synthetic row, and asserts the
dollars that come back.

`test_the_surface_list_is_complete` is the part that keeps this closed: it
enumerates the money-returning functions in `cost.py` by inspection and fails
when one appears that this file does not cover. A new surface must be added to
`SURFACES` or the suite goes red — it cannot be forgotten the way these five
were.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from llm_router import cost

# One synthetic row, priced high enough that counting it is unmistakable.
SYNTHETIC_COST = 3.00
SYNTHETIC_TOKENS = 1_000_000


def _seed(home, *, is_simulated: int | None = 1) -> None:
    """Write one row into every money table, stamped with the given provenance."""
    db = str(home / "usage.db")
    conn = sqlite3.connect(db)
    # cache_hit=1 is load-bearing: without it `get_cache_savings` returns zero
    # for a reason that has nothing to do with provenance, and its entry in
    # SURFACES passes on air. Caught by the red-check — that surface was the one
    # row of this file that stayed green with every filter removed.
    conn.execute(
        "INSERT INTO usage (model, provider, task_type, profile, input_tokens, "
        "output_tokens, cost_usd, latency_ms, success, is_simulated, "
        "cache_hit, cache_savings_usd) "
        "VALUES ('claude-opus-5','anthropic','query','fast',?,?,?,100,1,?,1,?)",
        (SYNTHETIC_TOKENS, SYNTHETIC_TOKENS, SYNTHETIC_COST, is_simulated,
         SYNTHETIC_COST),
    )
    for table in ("claude_usage", "codex_usage", "gemini_usage"):
        conn.execute(
            f"INSERT INTO {table} (model, tokens_used, complexity, cost_saved_usd, "
            "time_saved_sec, input_tokens, output_tokens, is_simulated) "
            "VALUES ('claude-opus-5', ?, 'complex', ?, 0, ?, ?, ?)",
            (SYNTHETIC_TOKENS, SYNTHETIC_COST, SYNTHETIC_TOKENS, SYNTHETIC_TOKENS,
             is_simulated),
        )
    conn.execute(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, "
        "estimated_claude_cost_saved, external_cost, model_used, host, "
        "input_tokens, output_tokens, is_simulated) "
        "VALUES (datetime('now'), 's1', 'code', ?, ?, 'claude-opus-5', "
        "'claude_code', ?, ?, ?)",
        (SYNTHETIC_COST, SYNTHETIC_COST, SYNTHETIC_TOKENS, SYNTHETIC_TOKENS,
         is_simulated),
    )
    conn.execute(
        "INSERT INTO routing_decisions (timestamp, task_type, complexity, "
        "final_model, final_provider, cost_usd, success, provenance) "
        "VALUES (datetime('now'), 'query', 'simple', 'claude-opus-5', "
        "'anthropic', ?, 1, ?)",
        (SYNTHETIC_COST, "unattributed" if is_simulated else "runtime"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
async def ledger(tmp_path, monkeypatch):
    """A real ledger at a real schema, holding exactly one synthetic row."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    db = await cost._get_db()   # builds the schema + runs every migration
    await db.close()
    _seed(home)
    return home


# ── the surfaces ──────────────────────────────────────────────────────────────
#
# (name, coroutine, extractor) — the extractor pulls the dollars/counts that
# must be zero. Keep this list in sync with `_money_surface_names()` below; the
# completeness test enforces it.

def _team(r):      return (r["saved_usd"], r["actual_usd"], r["total_calls"])
def _period(r):    return (r["all_time"]["saved_usd"], r["all_time"]["calls"])
def _realized(r):  return (r["gross_saved_usd"], r["realized_saved_usd"])
def _lifetime(r):  return (r["total_saved"], r["total_external_cost"], r["tasks_routed"])
def _quality(r):   return (r["total_cost_usd"], r["total_decisions"])
def _sonnet(r):    return (r["actual_cost"], r["saved"], r["total_calls"])
def _efficiency(r):
    return tuple(v for v in r.values() if isinstance(v, (int, float)))
def _summary(r):   return (r["total_calls"], r["total_tokens"], r["cost_saved_usd"])
def _cache(r):     return (r["total_calls_cached"], r["total_savings_usd"])


SURFACES = [
    ("get_team_savings",             lambda: cost.get_team_savings(period="all"),        _team),
    ("get_savings_by_period",        lambda: cost.get_savings_by_period(),               _period),
    ("get_realized_savings",         lambda: cost.get_realized_savings(period="all"),    _realized),
    ("get_lifetime_savings_summary", lambda: cost.get_lifetime_savings_summary(days=0),  _lifetime),
    ("get_quality_report",           lambda: cost.get_quality_report(days=30),           _quality),
    ("get_routing_savings_vs_sonnet", lambda: cost.get_routing_savings_vs_sonnet(days=0), _sonnet),
    ("get_router_efficiency",        lambda: cost.get_router_efficiency(period="all"),   _efficiency),
    # Neither of these appears in the audit's T-05 table. Both were found by
    # `test_the_surface_list_is_complete` below, on its first run.
    ("get_savings_summary",          lambda: cost.get_savings_summary(period="all"),     _summary),
    ("get_cache_savings",            lambda: cost.get_cache_savings(period="all"),             _cache),
]

SPEND_CAPS = [
    ("get_daily_spend", lambda: cost.get_daily_spend()),
    ("get_monthly_spend", lambda: cost.get_monthly_spend()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, call, extract", SURFACES, ids=[s[0] for s in SURFACES])
async def test_one_synthetic_row_reports_zero(ledger, name, call, extract):
    """The whole finding, per surface."""
    values = extract(await call())
    assert all(abs(float(v)) < 1e-9 for v in values), (
        f"{name} counted a synthetic row: {values}. It was stamped "
        f"is_simulated=1 / provenance='unattributed' at write time."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name, call", SPEND_CAPS, ids=[s[0] for s in SPEND_CAPS])
async def test_a_synthetic_row_cannot_trip_a_real_budget_cap(ledger, name, call):
    """T-05's opposite failure mode.

    These two gate real spending caps. A benchmark run's synthetic dollars
    counted here throttles legitimate routing — the damage is a working install
    that stops working, which looks like a bug anywhere but here.
    """
    spend = float(await call())
    assert spend < 1e-9, f"{name} counted ${spend:.4f} of synthetic spend toward a real cap"


# ── the discriminator: the same rows, stamped real ────────────────────────────

@pytest.mark.asyncio
async def test_a_production_row_is_still_counted(tmp_path, monkeypatch):
    """Anti-vacuity for every test above.

    A filter that excludes everything passes all of them and reports a clean
    zero forever. "A filter that drops nothing has not been shown to work" has a
    twin, and this is it: the identical rows stamped `is_simulated=0` must come
    back as money.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    db = await cost._get_db()
    await db.close()
    _seed(home, is_simulated=0)

    team = await cost.get_team_savings(period="all")
    assert team["total_calls"] == 1, "a production row vanished — the filter drops everything"
    assert team["actual_usd"] == pytest.approx(SYNTHETIC_COST)

    assert float(await cost.get_daily_spend()) == pytest.approx(SYNTHETIC_COST)

    period = await cost.get_savings_by_period()
    assert period["all_time"]["calls"] == 1


@pytest.mark.asyncio
async def test_an_unstamped_row_is_excluded_from_money(tmp_path, monkeypatch):
    """NULL provenance is UNKNOWN, and unknown is not money.

    Fail-closed. `IS NOT 1` admits NULL, which is how a row nobody measured
    became a dollar the first time.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    db = await cost._get_db()
    await db.close()
    _seed(home, is_simulated=None)

    team = await cost.get_team_savings(period="all")
    assert team["total_calls"] == 0, "an unstamped row counted as production money"
    assert float(await cost.get_daily_spend()) < 1e-9


# ── the part that keeps this closed ───────────────────────────────────────────

def _money_surface_names() -> set[str]:
    """Public `cost.py` coroutines whose name says they report money.

    Deliberately name-based and deliberately broad. The alternative — a curated
    list — is what T-05 was: five surfaces nobody remembered to add.
    """
    money = ("savings", "spend", "efficiency", "realized", "quality_report")
    out = set()
    for name, obj in vars(cost).items():
        if name.startswith("_") or not name.startswith("get_"):
            continue
        if not inspect.iscoroutinefunction(obj):
            continue
        if any(m in name for m in money):
            out.add(name)
    return out


def test_the_surface_list_is_complete():
    """A new money surface must be covered here, or this fails.

    This is the test that makes the fix durable rather than a one-time sweep.
    """
    covered = {n for n, _, _ in SURFACES} | {n for n, _ in SPEND_CAPS} | KNOWN_NOT_MONEY
    missing = _money_surface_names() - covered
    assert not missing, (
        "money-reporting surfaces with no provenance test: "
        + ", ".join(sorted(missing))
        + "\n\nAdd each to SURFACES (or to KNOWN_NOT_MONEY with a reason). "
        "T-05 happened because five such surfaces were added and nobody "
        "noticed they read unfiltered rows."
    )


#: Surfaces the name-scan catches that do not aggregate ledger dollars. Each
#: entry needs a reason — an unexplained exclusion is how the list rots back
#: into the state this test exists to prevent.
KNOWN_NOT_MONEY: set[str] = {
    # per-task-type slice of get_daily_spend; shares its filter and its cap
    "get_daily_spend_by_task_type",
}


def test_the_completeness_check_is_not_vacuous():
    """If `_money_surface_names()` found nothing, the test above passes on air."""
    found = _money_surface_names()
    assert len(found) >= 7, (
        f"the surface scan found only {len(found)} money functions ({sorted(found)}) "
        "— it has stopped matching cost.py and is no longer protecting anything"
    )
    assert "get_team_savings" in found, "the scan misses the surface that broadcasts to Slack"
