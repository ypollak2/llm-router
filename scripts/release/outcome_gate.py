#!/usr/bin/env python3
"""Release gate over the outcome audit (Phase 0.1).

Reads the artifact ``outcome_audit.py`` wrote for this release and FAILS the
release when either holds:

1. **An inflated source leaks into a headline figure.** Checked from the rows,
   not trusted from the summary:
   * a ``used``/``corrected`` row whose signal is a known inflated source
     (``door_call``/``verified_used``, the Codex ``delegated`` dispatch flag,
     a flat per-turn agentic credit);
   * a ``used``/``corrected`` row whose signal is not on the audit's
     content-bearing allowlist (judge signals are allowed only while the judge
     is ACTIVE). An allowlist, so a new heuristic has to be declared
     content-bearing before it can move the number;
   * ``excluded_inflated_sources.*.counted_as_used`` above zero;
   * a headline ``used``/``attempted`` that differs from a recount of the rows
     (something other than the rows fed the number).
   A leak fails even with ``--skip-outcome``: the skip exists for a machine
   with nothing to audit, not for a number that is wrong.

2. **The corrected used rate drops below the previous release's recorded
   value** (``used / attempted`` from the headline, compared as recorded, no
   tolerance). The history lives in ``docs/measurements/outcome_audit_history.json``
   (summary numbers only, no transcript text). The first run, with no
   history, records a baseline and passes. A re-run of a release compares
   against the release before it and replaces its own entry.

Below ``MIN_N`` (50) attempted units the rate is "too few to tell" and the
gate fails unless ``--skip-outcome "<reason>"`` is given; with zero units it
fails the same way (a check that found nothing to check has not passed).

    python3 scripts/release/outcome_gate.py
    python3 scripts/release/outcome_gate.py --skip-outcome "fresh machine, no transcripts"

The gate imports nothing from ``src/`` and nothing under ``src/`` imports it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY = ROOT / "docs" / "measurements" / "outcome_audit_history.json"


def _audit_module():
    name = "_og_outcome_audit"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("outcome_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_A = _audit_module()
MIN_N = _A.MIN_N


def check_leaks(rows: list[dict], summary: dict) -> list[str]:
    problems: list[str] = []
    judge_active = (summary.get("judge") or {}).get("status") == "ACTIVE"
    for r in rows:
        if r.get("label") not in _A.LABELS:
            problems.append(f"{r.get('unit_id')}: unknown label {r.get('label')!r}")
        if r.get("kind") not in _A.KINDS:
            problems.append(f"{r.get('unit_id')}: unknown kind {r.get('kind')!r}")
        if tuple(r) != _A.ROW_KEYS:
            problems.append(f"{r.get('unit_id')}: row keys {tuple(r)} != {_A.ROW_KEYS}")
        if r.get("label") not in ("used", "corrected"):
            continue
        sig = r.get("signal") or ""
        if sig in _A.FORBIDDEN_USED_SIGNALS:
            problems.append(f"{r['unit_id']}: inflated source {sig!r} counted as {r['label']}")
        elif sig.startswith(_A.JUDGE_SIGNAL_PREFIX):
            if not judge_active:
                problems.append(f"{r['unit_id']}: judge label {sig!r} counted while judge is not ACTIVE")
        elif sig not in _A.CONTENT_BEARING_SIGNALS:
            problems.append(f"{r['unit_id']}: signal {sig!r} is not content-bearing, counted as {r['label']}")
    for name, v in (summary.get("excluded_inflated_sources") or {}).items():
        if (v or {}).get("counted_as_used", 0):
            problems.append(f"excluded source {name} has counted_as_used={v['counted_as_used']}")
    head = summary.get("headline") or {}
    used = sum(r.get("label") == "used" for r in rows)
    if head.get("used") != used or head.get("attempted") != len(rows):
        problems.append(f"headline used/attempted {head.get('used')}/{head.get('attempted')} "
                        f"!= recount {used}/{len(rows)} from the rows")
    expected_rate = used / len(rows) if rows else None
    rate = head.get("rate")
    if (rate is None) != (expected_rate is None) or (
            rate is not None and abs(rate - expected_rate) > 1e-12):
        problems.append(f"headline rate {rate} != recount {expected_rate} from the rows")
    return problems


def _load_history(path: Path) -> dict:
    if not path.exists():
        return {"releases": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("releases", [])
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artifact-dir", type=Path, default=None)
    ap.add_argument("--release", default=None)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    ap.add_argument("--skip-outcome", default=None, metavar="REASON")
    args = ap.parse_args(argv)

    release = args.release or _A.current_release()
    art = args.artifact_dir or _A.default_artifact_dir()
    jsonl = art / f"outcome_audit_{release}.jsonl"
    summ = art / f"outcome_audit_{release}.summary.json"
    if not jsonl.exists() or not summ.exists():
        print(f"outcome gate: FAIL — no audit artifact for {release} in {art}; "
              f"run scripts/release/outcome_audit.py first")
        return 1
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = json.loads(summ.read_text(encoding="utf-8"))

    leaks = check_leaks(rows, summary)
    if leaks:
        print(f"outcome gate: FAIL — {len(leaks)} inflated or unverifiable figure(s):")
        for p in leaks[:20]:
            print(f"  - {p}")
        return 1
    print(f"outcome gate: no inflated source in the headline ({len(rows)} rows checked)")

    head = summary["headline"]
    n, used, rate = head["attempted"], head["used"], head["rate"]
    if n < MIN_N:
        msg = "nothing to audit (n=0)" if n == 0 else f"too few to tell (n={n} < {MIN_N})"
        if args.skip_outcome:
            print(f"outcome gate: SKIPPED — {msg}; reason: {args.skip_outcome}")
            return 0
        print(f"outcome gate: FAIL — {msg}. Pass --skip-outcome \"<reason>\" to release anyway.")
        return 1

    history = _load_history(args.history)
    releases = history["releases"]
    idx = next((i for i, e in enumerate(releases) if e.get("release") == release), None)
    # The release BEFORE this one: on a re-run, the entry preceding its own
    # (never a later release that happens to be recorded after it).
    previous = releases[:idx] if idx is not None else releases
    entry = {"release": release, "used_rate": rate, "used": used, "attempted": n,
             "ci95": head.get("ci95"), "window_days": summary.get("window_days"),
             "recorded_at": datetime.now(timezone.utc).isoformat()}
    if previous:
        prev = previous[-1]
        if rate < prev["used_rate"]:
            print(f"outcome gate: FAIL — used rate {used}/{n} = {100 * rate:.3f}% dropped below "
                  f"{prev['release']}'s recorded {100 * prev['used_rate']:.3f}% "
                  f"({prev['used']}/{prev['attempted']})")
            return 1
        print(f"outcome gate: PASS — used rate {used}/{n} = {100 * rate:.3f}% "
              f">= {prev['release']}'s {100 * prev['used_rate']:.3f}%")
    else:
        print(f"outcome gate: PASS — baseline recorded: used rate {used}/{n} = {100 * rate:.3f}% "
              f"(no earlier release recorded, nothing to compare against)")
    if idx is not None:
        releases[idx] = entry
    else:
        releases.append(entry)
    args.history.parent.mkdir(parents=True, exist_ok=True)
    args.history.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    print(f"  recorded in {args.history} (commit it with the release)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
