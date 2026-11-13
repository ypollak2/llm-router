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
   tolerance unless ``--tolerance-alpha`` is given, see ``dropped``). The history lives in ``docs/measurements/outcome_audit_history.json``
   (summary numbers only, no transcript text). The first run, with no
   history, records a baseline and passes. A re-run of a release compares
   against the release before it and replaces its own entry.

   **Only against the same audit version.** Every entry records the audit's
   ``audit_version`` (an entry without one is version 1, the audit before
   2026-11-03). A rate scored by one version is not comparable with a rate scored
   by another: the transcripts behind 15.4.0's recorded 0.229% (2/873) are purged,
   so it cannot be re-scored. The gate therefore compares a run with the latest
   entry OF ITS OWN VERSION. When the only earlier entries are of another version
   it prints the mismatch and does what ``--cross-version`` says:

   * ``refuse`` (default): FAIL, nothing is written. The owner decides.
   * ``rebaseline``: PASS without comparing; this run becomes version N's baseline
     and the next release is compared like for like.
   * ``compare``: compare the numbers anyway, label the result CROSS-VERSION, and
     record that it was.

   The decision is recorded in the history entry (``cross_version``).

   **A recorded baseline is sticky.** The baseline of an audit version is its earliest
   history entry (the first run, or the rebaseline that began it). Every entry records the
   sha256 of the audit artifact it was scored from (``artifact_sha256``), with the rate and n.
   A re-run for the baseline release needs no ``--cross-version`` flag and is compared with
   THAT entry: the same artifact passes unchanged; a different one fails if its rate is lower
   and otherwise passes; in neither case is the baseline replaced. Replacing it takes
   ``--force-baseline "<reason>"``, which records what it replaced and why. (An earlier gate
   let a re-run with ``--cross-version rebaseline`` overwrite 3.093% with 0.000%.) A later
   release is unchanged: a re-run is compared with the release before it and replaces its
   own entry.

   A ``not_applicable`` row (a probe, a status view) is outside ``attempted``; the
   gate accepts it only with a signal from the audit's ``NOT_APPLICABLE_SIGNALS``
   and prints how many there are, because shrinking the denominator raises the rate.

Below ``MIN_N`` (50) attempted units the rate is "too few to tell" and the
gate fails unless ``--skip-outcome "<reason>"`` is given; with zero units it
fails the same way (a check that found nothing to check has not passed).

    python3 scripts/release/outcome_gate.py
    python3 scripts/release/outcome_gate.py --skip-outcome "fresh machine, no transcripts"

The gate imports nothing from ``src/`` and nothing under ``src/`` imports it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from statistics import NormalDist
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY = ROOT / "docs" / "measurements" / "outcome_audit_history.json"
CROSS_VERSION_POLICIES = ("refuse", "rebaseline", "compare")


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
        if r.get("label") not in _A.ALL_LABELS:
            problems.append(f"{r.get('unit_id')}: unknown label {r.get('label')!r}")
        if r.get("kind") not in _A.KINDS:
            problems.append(f"{r.get('unit_id')}: unknown kind {r.get('kind')!r}")
        if tuple(r) != _A.ROW_KEYS:
            problems.append(f"{r.get('unit_id')}: row keys {tuple(r)} != {_A.ROW_KEYS}")
        if r.get("label") == _A.NOT_APPLICABLE:
            # An exclusion raises the used rate, so it has to name an allowed reason.
            person = r.get("signal") == "human_review" and r.get("human_reviewed") is True
            if r.get("signal") not in _A.NOT_APPLICABLE_SIGNALS and not person:
                problems.append(f"{r.get('unit_id')}: signal {r.get('signal')!r} is not an allowed "
                                f"not_applicable reason")
            continue
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
    attempted = sum(r.get("label") != _A.NOT_APPLICABLE for r in rows)  # not_applicable is outside the denominator
    if head.get("used") != used or head.get("attempted") != attempted:
        problems.append(f"headline used/attempted {head.get('used')}/{head.get('attempted')} "
                        f"!= recount {used}/{attempted} from the rows")
    expected_rate = used / attempted if attempted else None
    rate = head.get("rate")
    if (rate is None) != (expected_rate is None) or (
            rate is not None and abs(rate - expected_rate) > 1e-12):
        problems.append(f"headline rate {rate} != recount {expected_rate} from the rows")
    return problems


def dropped(used: int, n: int, base_used: int, base_n: int, rate: float, base_rate: float,
            alpha: float = 0.0) -> bool:
    """Is the current used rate a real drop from the baseline's?

    ``alpha == 0`` (default) is the original point comparison ``rate < base_rate``,
    no tolerance, decisions identical to before.

    ``alpha > 0``: one-sided pooled two-proportion z-test (Fleiss, Levin & Paik,
    *Statistical Methods for Rates and Proportions*, 3rd ed., sec. 4.2; the
    z-test of H0: p1 = p2 against H1: p1 < p2):

        p_pool = (k1 + k2) / (n1 + n2)
        z = (k1/n1 - k2/n2) / sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))

    with k1/n1 the current run and k2/n2 the baseline. It is a drop only when
    ``rate < base_rate`` and the one-sided p-value ``Phi(z)`` is <= ``alpha``,
    i.e. the fall is larger than sampling noise at that level. Both samples'
    noise is counted (the baseline is itself a sample).
    """
    if not rate < base_rate:
        return False
    if alpha <= 0:
        return True
    pool = (used + base_used) / (n + base_n)
    var = pool * (1 - pool) * (1 / n + 1 / base_n)
    if var <= 0:
        return True
    return NormalDist().cdf((rate - base_rate) / var ** 0.5) <= alpha


def audit_version(obj: dict) -> int:
    """The audit version an artifact summary or a history entry was scored with; an
    entry from before versions were recorded is version 1."""
    v = obj.get("audit_version", 1)
    return v if isinstance(v, int) else 1


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
    ap.add_argument("--cross-version", choices=CROSS_VERSION_POLICIES, default="refuse",
                    help="what to do when the only earlier history entries were scored by another audit "
                         "version: refuse (default, FAIL), rebaseline (PASS, this run becomes the baseline), "
                         "compare (compare the numbers anyway, labelled CROSS-VERSION)")
    ap.add_argument("--force-baseline", default=None, metavar="REASON",
                    help="replace the baseline already recorded for this release and audit version with this run's "
                         "artifact (default: a re-run never replaces it); the old values and the reason are recorded")
    ap.add_argument("--tolerance-alpha", type=float, default=0.0, metavar="ALPHA",
                    help="fail a drop only when it is beyond sampling noise: one-sided two-proportion z-test at this "
                         "significance level (e.g. 0.05). Default 0 = fail any drop, as before")
    args = ap.parse_args(argv)
    if not 0.0 <= args.tolerance_alpha < 1.0:
        ap.error("--tolerance-alpha must be in [0, 1)")
    alpha = args.tolerance_alpha

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

    cur_v = audit_version(summary)
    na = head.get("not_applicable", 0)
    if na:
        print(f"outcome gate: {na} unit(s) are not_applicable and outside the denominator "
              f"({(summary.get('not_applicable') or {}).get('by_signal')}); "
              f"used rate with them counted as attempted: "
              f"{100 * (summary.get('not_applicable') or {}).get('used_rate_if_included', 0):.3f}%")
    history = _load_history(args.history)
    releases = history["releases"]
    idx = next((i for i, e in enumerate(releases) if e.get("release") == release), None)
    # The releases BEFORE this one: on a re-run, the entries preceding its own
    # (never a later release that happens to be recorded after it).
    earlier = releases[:idx] if idx is not None else releases
    same = [e for e in earlier if audit_version(e) == cur_v]
    sha = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    entry = {"release": release, "audit_version": cur_v, "used_rate": rate, "used": used, "attempted": n,
             "ci95": head.get("ci95"), "window_days": summary.get("window_days"),
             "recorded_at": datetime.now(timezone.utc).isoformat(), "artifact_sha256": sha}
    if na:
        entry["not_applicable"] = na
    # The BASELINE of an audit version is its earliest entry (the first run, or the rebaseline that began it).
    # A re-run of that release is compared with it and never replaces it; a later release keeps the older rule:
    # a re-run is compared with the release before it and replaces its own entry.
    own = releases[idx] if idx is not None and audit_version(releases[idx]) == cur_v and not same else None
    if own is not None and not args.force_baseline:
        # An RC re-run of the baseline release: compare with the baseline, like for like; never replace it
        # (and never ask for --cross-version again).
        if args.cross_version != "refuse":
            print(f"outcome gate: note — --cross-version {args.cross_version} is not needed: {release} already has a "
                  f"v{cur_v} baseline, and a re-run is compared with it")
        recorded = f"{release}'s recorded {100 * own['used_rate']:.3f}% ({own['used']}/{own['attempted']})"
        if own.get("artifact_sha256") == sha:
            print(f"outcome gate: PASS — this artifact (sha256 {sha[:12]}) is the recorded v{cur_v} baseline of {release}: "
                  f"{used}/{n} = {100 * rate:.3f}%; nothing to record")
            return 0
        if dropped(used, n, own["used"], own["attempted"], rate, own["used_rate"], alpha):
            print(f"outcome gate: FAIL — used rate {used}/{n} = {100 * rate:.3f}% dropped below {recorded}; "
                  f"a different artifact (sha256 {sha[:12]}, recorded {str(own.get('artifact_sha256'))[:12]}). "
                  f"The recorded baseline was NOT replaced; --force-baseline \"<reason>\" replaces it deliberately.")
            return 1
        print(f"outcome gate: PASS — used rate {used}/{n} = {100 * rate:.3f}% >= {recorded}; a different artifact "
              f"(sha256 {sha[:12]}, recorded {str(own.get('artifact_sha256'))[:12]}), so the recorded baseline was "
              f"kept, not replaced (--force-baseline \"<reason>\" replaces it)")
        return 0
    if own is not None:
        entry["replaced"] = {**{k: own.get(k) for k in ("used_rate", "used", "attempted", "artifact_sha256", "recorded_at")},
                             "reason": args.force_baseline}
        print(f"outcome gate: WARNING — --force-baseline replaces {release}'s recorded v{cur_v} baseline "
              f"{100 * own['used_rate']:.3f}% ({own['used']}/{own['attempted']}) with {used}/{n} = {100 * rate:.3f}%; "
              f"reason: {args.force_baseline}")
    elif args.force_baseline:
        print(f"outcome gate: note — --force-baseline given but {release} is not the v{cur_v} baseline release; ignored")
    if own is not None:
        pass
    elif same:
        prev = same[-1]
        if dropped(used, n, prev["used"], prev["attempted"], rate, prev["used_rate"], alpha):
            print(f"outcome gate: FAIL — used rate {used}/{n} = {100 * rate:.3f}% dropped below "
                  f"{prev['release']}'s recorded {100 * prev['used_rate']:.3f}% "
                  f"({prev['used']}/{prev['attempted']})")
            return 1
        print(f"outcome gate: PASS — used rate {used}/{n} = {100 * rate:.3f}% "
              f">= {prev['release']}'s {100 * prev['used_rate']:.3f}%")
    elif earlier:
        prev = earlier[-1]
        prev_v = audit_version(prev)
        print("outcome gate: !!! AUDIT VERSION MISMATCH !!!")
        print(f"  this run was scored by audit v{cur_v}; the latest recorded entry ({prev['release']}: "
              f"{100 * prev['used_rate']:.3f}% = {prev['used']}/{prev['attempted']}) was scored by audit v{prev_v}.")
        print("  A used rate is only comparable within one audit version, and that release's transcripts are "
              "purged, so it cannot be re-scored.")
        entry["cross_version"] = {"policy": args.cross_version,
                                  "against": {"release": prev["release"], "audit_version": prev_v,
                                              "used_rate": prev["used_rate"], "used": prev["used"],
                                              "attempted": prev["attempted"]}}
        if args.cross_version == "refuse":
            print(f"outcome gate: FAIL — not comparable. Decide how to treat the change of method, then re-run "
                  f"with --cross-version rebaseline (this run becomes the v{cur_v} baseline, not compared) or "
                  f"--cross-version compare (compare the numbers anyway, recorded as CROSS-VERSION); "
                  f"through pre-release-verify.sh that is --outcome-cross-version rebaseline|compare. "
                  f"Nothing was written to {args.history}.")
            return 1
        if args.cross_version == "compare":
            if dropped(used, n, prev["used"], prev["attempted"], rate, prev["used_rate"], alpha):
                print(f"outcome gate: FAIL (CROSS-VERSION, v{prev_v} -> v{cur_v}, not like for like) — used rate "
                      f"{used}/{n} = {100 * rate:.3f}% dropped below {prev['release']}'s {100 * prev['used_rate']:.3f}%")
                return 1
            print(f"outcome gate: PASS (CROSS-VERSION, v{prev_v} -> v{cur_v}, not like for like) — used rate "
                  f"{used}/{n} = {100 * rate:.3f}% >= {prev['release']}'s {100 * prev['used_rate']:.3f}%")
        else:
            print(f"outcome gate: PASS (REBASELINED, v{prev_v} -> v{cur_v}, NOT compared) — used rate "
                  f"{used}/{n} = {100 * rate:.3f}% is now the v{cur_v} baseline; "
                  f"{prev['release']}'s {100 * prev['used_rate']:.3f}% was not used")
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
