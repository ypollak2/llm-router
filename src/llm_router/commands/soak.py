"""``llm_router soak`` -- Phase 0 Step 8 CLI: run the realized-savings soak
corpus end-to-end and write ``soak/report.json``.

Thin wrapper around ``soak.report.run_soak_n`` + ``write_report``. All the
hermetic mocking (no live API keys, no live provider dispatch) lives in
``soak/replay.py``; this module just drives the async pipeline N times and
prints a short summary.

Flags:
  --use-gold-complexity  Hermetic default. Resolve each corpus row's
                          complexity from its own ``gold_complexity`` label
                          instead of the classifier path -- this is also the
                          CI-safe path (G7 gate), since it guarantees no live
                          classifier call is attempted.
  --full                 Opt-in: resolve complexity via the normal
                          (non-gold) path instead of the corpus's
                          gold_complexity label. Mutually exclusive with
                          --use-gold-complexity (the last one wins if both
                          are passed; --full overrides the default).
  --runs N                Phase 0.2 FIX A: number of independent replay runs
                          to aggregate into a stable, citeable figure
                          (default: soak.report.DEFAULT_N_SOAK_RUNS -- a
                          single run's point estimate is NOT run-to-run
                          reproducible; see NONDETERMINISM_NOTE in the
                          written report). The headline to cite is
                          point_median + conservative_ci_lower, never a
                          single run's point/ci95.
  --out PATH              Where to write report.json. Defaults to
                          soak/report.json (soak.report.DEFAULT_REPORT_PATH).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from soak.report import DEFAULT_N_SOAK_RUNS, DEFAULT_REPORT_PATH, run_soak_n, write_report

__all__ = ["cmd_soak"]


def cmd_soak(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm_router soak")
    parser.add_argument(
        "--use-gold-complexity",
        action="store_true",
        default=True,
        help="Use each corpus row's gold_complexity as the complexity_hint "
             "(hermetic default -- no live classifier call).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Resolve complexity via the normal (non-gold) path instead of "
             "gold_complexity. Overrides --use-gold-complexity.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_N_SOAK_RUNS,
        help="Number of independent replay runs to aggregate "
             f"(default: {DEFAULT_N_SOAK_RUNS}). Phase 0.2 FIX A: a single "
             "run's point estimate is not run-to-run reproducible -- cite "
             "point_median + conservative_ci_lower from the N-run aggregate.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"Path to write report.json (default: {DEFAULT_REPORT_PATH}).",
    )
    opts = parser.parse_args(args)

    use_gold_complexity = not opts.full

    if opts.runs < 1:
        print(f"soak failed: --runs must be >= 1, got {opts.runs}", file=sys.stderr)
        return 1

    try:
        report = asyncio.run(
            run_soak_n(
                n_runs=opts.runs,
                use_gold_complexity=use_gold_complexity,
            )
        )
    except ValueError as err:
        print(f"soak failed: {err}", file=sys.stderr)
        return 1

    out_path = Path(opts.out) if opts.out else None
    written = write_report(report, out_path)

    print(f"Wrote {written}")
    print(f"  n_soak_runs={report['n_soak_runs']}")
    print(f"  corpus_version={report['corpus_version']}  n_routes={report['n_routes']}")
    print(f"  host_mode_split={report['host_mode_split']}")
    net_agg = report["net_realized_savings_usd"]["metered"]
    quota_agg = report["realized_quota_tokens_saved"]["subscription"]
    print(
        "  net_realized_savings_usd.metered.point_median="
        f"{net_agg['point_median']}  run_spread={net_agg['run_spread']}  "
        f"conservative_ci_lower={net_agg['conservative_ci_lower']}"
    )
    print(
        "  realized_quota_tokens_saved.subscription.point_median="
        f"{quota_agg['point_median']}  run_spread={quota_agg['run_spread']}  "
        f"conservative_ci_lower={quota_agg['conservative_ci_lower']}"
    )
    overhead = report["overhead_as_pct_of_gross"]
    overhead_label = "null (not measured -- hook overhead requires the external PreToolUse hook, not exercised here)" if overhead is None else overhead
    print(f"  overhead_as_pct_of_gross={overhead_label}")
    print(f"  soak_dispatch_failure_rate={report['soak_dispatch_failure_rate']}  adoption_unknown_fraction={report['adoption_unknown_fraction']}")
    print(f"  effective_sample_size={report['effective_sample_size']}")
    # Phase 0.1 FIX 3, extended to N runs (Phase 0.2 FIX A): never surface
    # only the point estimate -- state plainly whether the CI lower bound,
    # robust across ALL N runs, actually supports a savings claim.
    if report["savings_claim_supported"]:
        print(
            "  savings_claim_supported=True (a headline metric's conservative_ci_lower, "
            f"the min CI lower bound across all {report['n_soak_runs']} runs, clears 0)"
        )
    else:
        print(
            "  savings_claim_supported=False -- across all "
            f"{report['n_soak_runs']} runs this is an infrastructure smoke-test "
            "(pipeline ran, report schema complete), NOT proof of a saving."
        )
    return 0
