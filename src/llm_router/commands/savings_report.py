"""Detailed savings report command.

R6/2026-09-27. This docstring used to claim ``savings_stats`` was "the SINGLE
source of truth, so the report can never disagree with the stored stats."
Both halves were wrong in a way worth recording, because the sentence read as
a guarantee:

* It is not the single source. The 2026-09-22 audit found TWENTY user-facing
  savings surfaces across FIVE data sources; this one read `savings_stats`,
  `llm-router gain` read `routing_decisions`, the statuslines read `usage`,
  and `llm-router status` read a union of four. A free DIRECT route recorded
  in one was invisible to the others.
* "Can never disagree with the stored stats" was true and beside the point. It
  agreed with its own table and with nothing else, and it applied no
  provenance filter — so a benchmark run inflated this report and not the
  eight surfaces that went through `cost.py`.

The 2026-09-27 fix went one step further: this file used to hold TWO
disagreeing computations of its own — `canonical_savings()` (reading
`claude_usage`/`codex_usage`/`gemini_usage`, n=20-ish) for the headline, and a
separately hand-rolled query over `savings_stats` (n=150) for the "ledger"
line beneath it, with a stale hardcoded baseline (`claude-opus-4`) that had
drifted from `pricing.SAVINGS_BASELINE_MODEL` (`claude-opus-5`). Both are gone.
The headline now comes from ``dashboard_data.summary()`` — the SAME function
`llm-router status`, `llm-router gain`, and the statusline call — so this
report can no longer print a different verified/unverified figure or a
different baseline model than any other surface reading the same database.

The per-model free/paid breakdown below still comes from `savings_stats` (the
only table carrying it), via ``dashboard_data.query_model_savings`` — the
single implementation shared with `summary()`'s own `by_model`, filtered the
same way (provenance, verified-vs-unverified) as everything else. Known
residual: ``query_model_savings`` drops non-production (``is_simulated=1``)
``savings_stats`` rows; the HEADLINE above (``query_window``, via
``summary()``) intentionally does not apply that same drop to
``savings_stats`` — see ``tests/test_acc01_status_headline_provenance.py``'s
`test_unverified_accumulates_across_every_table`, which pins the headline
side of this on purpose. On real data this is a single-digit row count; the
per-model total can therefore differ from the headline by that much and no
more.

Usage:
    llm-router savings-report              — full report (all time)
    llm-router savings-report --period week — weekly report
    llm-router savings-report --period day  — today only
"""

from __future__ import annotations

from pathlib import Path

from llm_router import paths


def _get_db_path() -> Path:
    return paths.state_path("usage.db")


def _provider_of(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[0]
    if model.startswith("claude") or model == "cc":
        return "anthropic"
    return model or "unknown"


def _query(db_path: Path, period: str, *, paid: bool) -> dict:
    """Per-model free/paid breakdown for `period`, in this report's own field
    names. Delegates to ``dashboard_data.query_model_savings`` — the ONE
    implementation of this query — rather than re-running it here.
    """
    from llm_router.dashboard_data import _PERIOD_ALIASES, query_model_savings

    window = _PERIOD_ALIASES.get(period, period)
    raw = query_model_savings(window, paid=paid, db_path=db_path)
    stats = {
        "calls": raw["calls"], "saved": raw["verified_saved"],
        "cost": raw["cost"], "by_model": {},
        "unverified": raw["unverified_saved"],
        "unverified_calls": raw["unverified_calls"],
    }
    for model, d in raw["by_model"].items():
        stats["by_model"][model] = {
            "calls": d["calls"], "saved": d["verified_saved"], "cost": d["cost"],
            "provider": _provider_of(model),
        }
    return stats


def _canonical_headline(period: str, db_path: Path) -> str:
    """The one savings figure, labelled. R6/R7.

    Takes the ALREADY-RESOLVED ``db_path`` rather than calling
    ``_get_db_path()`` again — ``render_savings_report`` resolves it once and
    every helper reuses that value, the same convention the original file
    used. A second, independent ``_get_db_path()`` call here previously
    re-ran a test's monkeypatched fixture builder a second time (re-creating
    an already-existing sqlite table), which is exactly the kind of "two
    computations of the same thing" this rewrite exists to stop.

    Degrades to a stated UNAVAILABLE rather than to a number. A report that
    silently falls back to its own arithmetic when the canonical accessor is
    unreachable is the twenty-first surface.
    """
    try:
        from llm_router.dashboard_data import summary

        return summary(period, db_path=db_path).headline()
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen
        failopen.record("CHZ-FO-SAVINGS-REPORT-CANONICAL", exc)
        return "canonical savings figure UNAVAILABLE (see llm-router doctor)"


def render_savings_report(period: str = "all") -> str:
    db_path = _get_db_path()
    if not db_path.exists():
        return "No usage data found. Start routing prompts to generate data."

    free = _query(db_path, period, paid=False)
    paid = _query(db_path, period, paid=True)
    if not free["calls"] and not paid["calls"]:
        return "No routing data available for this period."

    label = {"day": "Last 24 Hours", "week": "Last 7 Days",
             "month": "Last 30 Days", "all": "All Time"}.get(period, "All Time")

    out = [f"\n╭─ SAVINGS REPORT ─ {label} " + "─" * 34 + "╮", "│"]
    # ONE headline, from dashboard_data.summary() — the same function
    # `llm-router status`, `llm-router gain` and the statusline call. No
    # second, disagreeing total is computed here anymore.
    out.append(f"│  {_canonical_headline(period, db_path)}")
    out.append("│")

    def section(title: str, s: dict, free_section: bool) -> None:
        if not s["calls"]:
            return
        spent = "$0.0000" if free_section else f"${s['cost']:.4f}"
        out.append(f"│  {title}")
        out.append(f"│    {s['calls']:>4} calls · saved ${s['saved']:.4f} vs Claude · {spent} spent")
        for model, d in sorted(s["by_model"].items(), key=lambda x: -x[1]["saved"])[:10]:
            out.append(f"│      {model:<26} {d['calls']:>3}×   saved ${d['saved']:.4f}")
        out.append("│")

    section("FREE / LOCAL  (ollama · codex · gemini-cli)", free, True)
    section("PAID EXTERNAL  (gemini · openai · …)", paid, False)

    out.append("│  Note: counts only prompts LLM Router ROUTED (conversation turns). Tokens")
    out.append("│        consumed by downstream agents/tools are not metered here.")
    out.append("╰" + "─" * 60 + "╯")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    period = "all"
    if "--period" in argv:
        i = argv.index("--period")
        if i + 1 < len(argv):
            period = argv[i + 1]
    print(render_savings_report(period))
    return 0
