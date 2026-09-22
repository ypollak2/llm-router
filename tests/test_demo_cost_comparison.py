"""The demo's savings arithmetic — audit 2026-09-22, T-02.

`commands/demo.py` had no tests at all, and the thing it got wrong was the one
number a demo exists to show. It accumulated a flat ``total_opus += 0.015`` for
every row, so a batch containing a real premium call was charged $0.015 for a
call that cost $0.045, and the resulting NEGATIVE saving was printed with the
word "cheaper" attached:

    Always-Opus: $0.0450 per batch
    Smart Routing: $0.06030 per batch
    Savings:  $-0.0153 (-34% cheaper)

Reproduced verbatim against the pre-fix module with the fixture in
``_seed_usage_db`` below, which is why that fixture's shape is load-bearing:
**it must keep containing a call that ran on the baseline model.** A fixture of
three cheap calls passes these tests against the broken code.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from llm_router import pricing
from llm_router.commands import demo as D

_USAGE_SCHEMA = """
CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    task_type TEXT NOT NULL,
    profile TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    prompt TEXT,
    complexity TEXT DEFAULT 'moderate'
)
"""

_INSERT = (
    "INSERT INTO usage (model, provider, task_type, profile, input_tokens, "
    "output_tokens, cost_usd, latency_ms, success, prompt, complexity) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)


def _seed_usage_db(home, rows) -> None:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(home / "usage.db"))
    conn.execute(_USAGE_SCHEMA)
    conn.executemany(_INSERT, rows)
    conn.commit()
    conn.close()


def _row(model, cost, in_tok, out_tok, prompt="a prompt", task="query", compl="moderate"):
    return (model, "anthropic", task, "balanced", in_tok, out_tok, cost, 100.0, 1, prompt, compl)


@pytest.fixture
def demo_home(tmp_path, monkeypatch):
    home = tmp_path / "llm-router-home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setenv("NO_COLOR", "1")
    return home


def _savings_line(out: str) -> str:
    for line in out.splitlines():
        if "Savings:" in line:
            return line
    raise AssertionError(f"no Savings line in demo output:\n{out}")


# ── the defect ────────────────────────────────────────────────────────────────

def test_a_batch_containing_a_premium_call_never_prints_a_negative_saving_as_cheaper(
    demo_home, capsys
):
    """The exact shape that produced '$-0.0153 (-34% cheaper)'."""
    baseline = pricing.savings_baseline_model()
    _seed_usage_db(
        demo_home,
        [
            _row("claude-haiku-4-5", 0.00030, 300, 150, "what is a mutex?", "query", "simple"),
            _row("claude-sonnet-5", 0.01500, 900, 600, "why is my async slow?", "analyze"),
            # The load-bearing row: it ran on the baseline model itself.
            _row(baseline, 0.04500, 2000, 1400, "implement a rate limiter", "code", "complex"),
        ],
    )
    D._run_demo()
    out = capsys.readouterr().out
    line = _savings_line(out)

    amount = float(re.search(r"\$(-?[\d.]+)", line).group(1))
    assert amount >= 0, f"negative saving reported: {line!r}"
    assert "cheaper" in line, f"a positive saving should say cheaper: {line!r}"
    assert "-" not in line.split("Savings:")[1], f"sign leaked into the saving: {line!r}"


def test_a_call_on_the_baseline_model_is_charged_its_own_real_cost(demo_home, capsys):
    """One row, on the baseline model. Routing saved exactly nothing.

    The old code charged this row $0.015 against a $0.045 actual, inventing a
    $0.030 loss out of a constant.
    """
    baseline = pricing.savings_baseline_model()
    _seed_usage_db(demo_home, [_row(baseline, 0.04500, 2000, 1400, "big task", "code", "complex")])
    D._run_demo()
    out = capsys.readouterr().out

    assert "already ran on" in out, out
    assert "$0.0000 (0%)" in out, out
    assert "cheaper" not in _savings_line(out)


# ── the honest negative ───────────────────────────────────────────────────────

def test_a_genuinely_negative_saving_is_reported_as_more_expensive(demo_home, capsys, monkeypatch):
    """When routing really did cost more, the demo says so — it does not hide the sign.

    Fixing the arithmetic must not turn into clamping the result at zero: a
    demo that can only ever print good news is not a measurement. Forced here
    by pricing a row far above what any baseline estimate could reach.
    """
    _seed_usage_db(demo_home, [_row("claude-haiku-4-5", 5.00, 300, 150, "cheap model, absurd bill")])
    D._run_demo()
    out = capsys.readouterr().out
    line = _savings_line(out)

    assert "more expensive" in line, line
    assert "cheaper" not in line, line
    assert "-$" in line, line


# ── the denominator ───────────────────────────────────────────────────────────

def test_a_row_with_no_tokens_is_excluded_and_disclosed_not_scored_as_zero(demo_home, capsys):
    """An unpriceable row must leave BOTH totals, and the exclusion must be visible.

    Counting it as $0 on the baseline side shrinks the denominator and inflates
    the printed percentage — the 'empty set reads as healthy' shape.
    """
    _seed_usage_db(
        demo_home,
        [
            _row("claude-haiku-4-5", 0.00030, 300, 150, "priced row"),
            _row("claude-haiku-4-5", 0.00100, 0, 0, "no tokens recorded"),
        ],
    )
    D._run_demo()
    out = capsys.readouterr().out

    assert "n=1 of 2 calls" in out, out
    assert "1 call(s) excluded" in out, out


def test_the_comparison_states_its_n(demo_home, capsys):
    """A rate without its denominator is not a measurement (repo CLAUDE.md)."""
    _seed_usage_db(
        demo_home,
        [_row("claude-haiku-4-5", 0.00030, 300, 150, f"prompt {i}") for i in range(4)],
    )
    D._run_demo()
    out = capsys.readouterr().out
    assert re.search(r"n=\d+ of \d+ calls", out), out


# ── the examples path ─────────────────────────────────────────────────────────

def test_example_cases_are_priced_from_the_table_not_from_literals(demo_home):
    """Every example dollar figure must be reproducible from llm_router.pricing.

    The old examples carried hand-written strings ("$0.015" for Sonnet) that no
    longer matched the table. A demo quoting a price the router does not charge
    is a false claim, however small.
    """
    for prompt, task, compl, model, cost_str, baseline in D._example_cases():
        if cost_str == "n/a":
            assert baseline is None
            continue
        shown = float(cost_str.lstrip("$"))
        assert pricing.resolve(model) is not None, f"example model {model!r} is not in the table"
        assert baseline is not None
        assert baseline >= shown - 1e-12, (
            f"example {prompt}: baseline ${baseline} is below its own routed cost ${shown}"
        )


def test_the_examples_batch_reports_a_non_negative_saving(demo_home, capsys):
    """No usage.db at all — the fallback path must survive its own arithmetic."""
    D._run_demo()
    out = capsys.readouterr().out
    line = _savings_line(out)
    assert float(re.search(r"\$(-?[\d.]+)", line).group(1)) >= 0, line


# ── anti-vacuity ──────────────────────────────────────────────────────────────

def test_these_tests_actually_exercise_the_comparison(demo_home, capsys):
    """If the comparison block stops printing, every assertion above goes quiet.

    ``_savings_line`` raises on a missing line, but only for the tests that call
    it — and a filter that drops nothing has not been shown to work. This pins
    the fixture: a seeded DB must reach the comparison at all.
    """
    baseline = pricing.savings_baseline_model()
    _seed_usage_db(demo_home, [_row(baseline, 0.04500, 2000, 1400, "big task", "code", "complex")])
    D._run_demo()
    out = capsys.readouterr().out
    assert "Cost Comparison:" in out, out
    assert "Always-" in out, out
    assert D._baseline_model_label() in out, out
