"""`llm-router calibrate` -- measure what each model can actually do, here.

Local models only unless ``--allow-paid``; with it the estimated cost is printed
BEFORE anything is sent. Results are MEASURED capability on this setup, stored in
``state_path("capability_profile.json")``; they are not a global ranking.
"""

from __future__ import annotations

import sys

_HELP = """usage: llm-router calibrate [--models a,b,*glob*] [--budget-s N] [--tokens N]
                             [--allow-paid] [--yes] [--max-usd N] [--dry-run]

  --models      comma-separated model ids / substrings / globs (default: every local model)
  --budget-s    wall-clock budget for the whole run (default 300)
  --tokens      long-context recall size in tokens (default 8000)
  --allow-paid  also probe subscription and API models (spends quota or money;
                the estimate is printed first)
  --yes         required with --allow-paid and no --models when more than 3 paid models
                would be probed
  --max-usd     refuse to start if the estimated metered cost exceeds N (default 1.00)
  --dry-run     print the plan and estimate, send nothing
"""


def _parse(args: list[str]) -> dict | None:
    opts: dict = {"models": None, "budget_s": None, "tokens": None,
                  "allow_paid": False, "dry_run": False, "yes": False, "max_usd": 1.0}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--allow-paid":
            opts["allow_paid"] = True
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a == "--yes":
            opts["yes"] = True
        elif a in ("--models", "--budget-s", "--tokens", "--max-usd") and i + 1 < len(args):
            val = args[i + 1]
            i += 1
            try:
                if a == "--models":
                    opts["models"] = [m.strip() for m in val.split(",") if m.strip()]
                elif a == "--budget-s":
                    opts["budget_s"] = float(val)
                elif a == "--max-usd":
                    opts["max_usd"] = float(val)
                else:
                    opts["tokens"] = int(val)
            except ValueError:
                return None
        else:
            return None
        i += 1
    return opts


def cmd_calibrate(args: list[str]) -> int:
    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0
    opts = _parse(args)
    if opts is None:
        print("llm-router calibrate: bad arguments", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        return 2

    from llm_router.resolver import calibrate as cal
    from llm_router.resolver import inventory as inv_mod
    from llm_router.resolver import profile as profile_mod

    probes = inv_mod.Probes()
    inv = inv_mod.collect_inventory(probes)
    tokens = opts["tokens"] or cal.DEFAULT_RECALL_TOKENS
    budget = opts["budget_s"] or cal.DEFAULT_BUDGET_S

    chosen, skipped = cal.select_models(inv, opts["models"], allow_paid=opts["allow_paid"])
    for mid, why in skipped:
        print(f"skip {mid}: {why}")
    if not chosen:
        print("Nothing to calibrate. Is Ollama running with models installed? "
              "(see `llm-router inventory`)")
        return 1

    estimates = [cal.estimate_cost(m, tokens) for m in chosen]
    paid = [e for e in estimates if e.usd != 0.0 or "quota" in e.note]
    print("Plan:")
    for e in estimates:
        usd = "unknown" if e.usd is None else f"${e.usd:.4f}"
        print(f"  {e.model_id}: ~{e.input_tokens} in / ~{e.output_tokens} out tokens, est. {usd} ({e.note})")
    if paid:
        known = sum(e.usd for e in paid if e.usd)
        print(f"Estimated spend: ${known:.4f} metered"
              f"{' + some unpriced models' if any(e.usd is None for e in paid) else ''}"
              f"{' + subscription quota' if any('quota' in e.note for e in paid) else ''}. "
              f"Budget {budget:.0f}s.")
    metered = sum(e.usd for e in estimates if e.usd)
    if paid:
        print(f"Total estimate: ${metered:.4f} metered across {len(paid)} paid model(s); "
              f"cap ${opts['max_usd']:.2f} (--max-usd).")
    if opts["dry_run"]:
        print("--dry-run: nothing sent.")
        return 0
    if len(paid) > 3 and not opts["models"] and not opts["yes"]:
        print(f"{len(paid)} paid models would be probed without --models. Name them, or pass --yes.",
              file=sys.stderr)
        return 2
    if metered > opts["max_usd"]:
        print(f"Estimated ${metered:.4f} exceeds --max-usd ${opts['max_usd']:.2f}; nothing sent.",
              file=sys.stderr)
        return 2

    def completer_for(m):
        return cal.default_completer(m, probes.http_post, probes.ollama_base())

    report = cal.run_calibration(
        inv, patterns=opts["models"], allow_paid=opts["allow_paid"], budget_s=budget,
        tokens=tokens, completer_for=completer_for,
        on_start=lambda m: print(f"probing {m.id} ..."))
    if report.profiles:
        path = profile_mod.save_profiles(report.profiles)
        print(f"\nMeasured on this setup (not a global ranking); saved to {path}")
    for mid, mp in report.profiles.items():
        marks = " ".join(
            f"{n}={'?' if p.ok is None else ('pass' if p.ok else 'FAIL')}" for n, p in mp.probes.items())
        print(f"  {mid}: ceiling={mp.tier_ceiling or 'none'}  [{marks}]")
        print(f"      {mp.tier_detail}")
    for mid in report.unreachable:
        print(f"  {mid}: unreachable, not recorded (any earlier measurement is kept)")
    for mid in report.out_of_budget:
        print(f"  {mid}: not finished within the {budget:.0f}s budget; not recorded")
    return 0 if report.profiles else 1
