"""Text-free, single-use scoring of a frozen redo validation draw."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from llm_router import paths, redo_signal
from llm_router.local_check.stats import wilson
from llm_router.redo_sample import _write_private

STRATA = ("F", "S", "R")
LABELS = ("redo", "not_redo", "unclear", "lost")
RULE = "precision>=0.80 and TP>=15"
COMPLETENESS_RULE = ("all flagged-stratum (F) pairs must be labelled; "
                     "any lost F pair voids; overall lost > 5% voids")


def _precision(rows):
    counts = Counter(label for row, label in rows if row["stratum"] == "F")
    tp, fp = counts["redo"], counts["not_redo"]
    point, lo, hi = wilson(tp, tp + fp)
    return dict(tp=tp, fp=fp, n=tp + fp,
                point=point if math.isfinite(point) else None,
                lo=lo if math.isfinite(lo) else None,
                hi=hi if math.isfinite(hi) else None)


def _gate(precision):
    tp, n = precision["tp"], precision["n"]
    passed = n > 0 and tp * 5 >= 4 * n and tp >= 15
    return dict(rule=RULE, passed=passed, verdict="pass" if passed else "fail",
                reason="threshold met" if passed else "threshold not met")


def _recall_point(rows, frozen):
    counts = {h: Counter() for h in STRATA}
    for row, label in rows:
        if label in ("redo", "not_redo"):
            counts[row["stratum"]][label] += 1
    totals = {}
    for h in STRATA:
        population = frozen["strata"][h]["N"]
        effective = sum(counts[h].values())
        if population > 0 and not effective:
            return None
        totals[h] = population * counts[h]["redo"] / effective if effective else 0
    denominator = sum(totals.values())
    return totals["F"] / denominator if denominator else None


def _percentile(values, q):
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def _precision_bootstrap(rows, seed, reps):
    clusters = {}
    for row, label in rows:
        clusters.setdefault(row["session_hash"], []).append((row, label))
    keys = sorted(clusters)
    rng = random.Random(seed)
    values = []
    for _ in range(reps):
        replicate = [item for _ in keys
                     for item in clusters[rng.choice(keys)]]
        value = _precision(replicate)["point"]
        if value is not None:
            values.append(value)
    result = dict(lo=None, hi=None, reps=reps, seed=seed,
                  n_discarded=reps - len(values))
    if values and result["n_discarded"] <= reps / 2:
        values.sort()
        result.update(lo=_percentile(values, 0.025),
                      hi=_percentile(values, 0.975))
    return result


def _recall(rows, frozen, seed, reps):
    if frozen.get("concentrated", False):
        return dict(estimable=False, reason="not estimable (concentrated draw)")
    point = _recall_point(rows, frozen)
    result = dict(estimable=True, recall_point=point, ci_lo=None, ci_hi=None,
                  reps=reps, seed=seed, n_discarded=0,
                  assumption="unclear/lost missing at random within strata")
    if point is None:
        result.update(estimable=False, reason="not estimable (empty stratum)",
                      n_discarded=reps)
        return result
    clusters = {}
    for row, label in rows:
        clusters.setdefault(row["session_hash"], []).append((row, label))
    keys = sorted(clusters)
    rng = random.Random(seed)
    values = []
    for _ in range(reps):
        replicate = [item for _ in keys
                     for item in clusters[rng.choice(keys)]]
        value = _recall_point(replicate, frozen)
        if value is not None:
            values.append(value)
    result["n_discarded"] = reps - len(values)
    if values and result["n_discarded"] <= reps / 2:
        values.sort()
        result.update(ci_lo=_percentile(values, 0.025),
                      ci_hi=_percentile(values, 0.975))
    else:
        result["ci_reason"] = "too many discarded replicates"
    return result


def score(manifest_rows, frozen, labels, *, seed=360, reps=2000) -> dict:
    """Pure scoring; labels are JSONL-shaped rows.

    Recall drops unclear/lost labels under a within-stratum missing-at-random
    assumption. Bootstrap draws whole sessions, retaining frozen populations.
    """
    if reps < 1:
        raise ValueError("reps must be positive")
    manifest_rows = list(manifest_rows)
    ids = {row["pair_id"] for row in manifest_rows}
    if len(ids) != len(manifest_rows):
        raise ValueError("duplicate manifest pair")
    by_pair = {}
    for row in labels:
        pair, label, source = row["pair_id"], row["label"], row["source"]
        if pair not in ids or label not in LABELS or source not in ("owner", "judge"):
            raise ValueError("invalid label row")
        sources = by_pair.setdefault(pair, {})
        if source in sources and sources[source] != label:
            raise ValueError("conflicting labels")
        sources[source] = label
    resolved = []
    source_counts = Counter()
    overlapping = []
    for row in manifest_rows:
        sources = by_pair.get(row["pair_id"], {})
        source = "owner" if "owner" in sources else "judge" if "judge" in sources else "none"
        source_counts[source] += 1
        resolved.append((row, sources.get(source, "lost")))
        if "owner" in sources and "judge" in sources:
            overlapping.append((sources["owner"], sources["judge"]))
    n_agreement = len(overlapping)
    agree = sum(a == b for a, b in overlapping)
    kappa = None
    if n_agreement:
        owners = Counter(a for a, b in overlapping)
        judges = Counter(b for a, b in overlapping)
        expected_numerator = sum(owners[k] * judges[k] for k in LABELS)
        denominator = n_agreement ** 2 - expected_numerator
        if denominator:
            kappa = (agree * n_agreement - expected_numerator) / denominator
    overall = Counter(label for row, label in resolved)
    label_counts = {k: overall[k] for k in LABELS}
    label_counts["by_stratum"] = {
        h: {k: sum(row["stratum"] == h and label == k for row, label in resolved)
            for k in LABELS} for h in STRATA}
    precision = _precision(resolved)
    basis = "capped per-session F draw" if frozen.get("concentrated") else "stratum F draw"
    precision["basis"] = basis
    precision["bootstrap"] = _precision_bootstrap(resolved, seed, reps)
    gate = _gate(precision)
    sensitivity = {}
    for name, target, replacement in (
            ("unclear_as_redo", "unclear", "redo"),
            ("unclear_as_not_redo", "unclear", "not_redo"),
            ("lost_as_not_redo", "lost", "not_redo")):
        altered = [(row, replacement if label == target and
                    (target != "lost" or row["stratum"] == "F") else label)
                   for row, label in resolved]
        p = _precision(altered)
        sensitivity[name] = dict(
            precision=p, gate=_gate(p), recall=_recall(altered, frozen, seed, reps),
            counts={k: sum(label == k for row, label in altered) for k in LABELS})
    if any(item["gate"]["passed"] != gate["passed"] for item in sensitivity.values()):
        reason = ("unclear sensitivity" if any(
            sensitivity[name]["gate"]["passed"] != gate["passed"]
            for name in ("unclear_as_redo", "unclear_as_not_redo"))
            else "lost sensitivity")
        gate.update(passed=False, verdict="inconclusive", reason=reason)
    lost_fraction = overall["lost"] / len(resolved) if resolved else 0.0
    if overall["lost"] * 20 > len(resolved):
        gate.update(passed=False, verdict="void", reason="lost fraction exceeds 0.05")
    if label_counts["by_stratum"]["F"]["lost"]:
        gate.update(passed=False, verdict="void", reason="flagged-stratum label lost")
    validity = {}
    warnings = []
    for name, counts in (("F", label_counts["by_stratum"]["F"]),
                         ("overall", label_counts)):
        labelled = sum(counts[k] for k in LABELS if k != "lost")
        unclear = counts["unclear"]
        rate = unclear / labelled if labelled else None
        validity[name] = dict(unclear=unclear, labelled=labelled, rate=rate)
        if rate is not None and rate > 0.10:
            warnings.append(f"unclear rate exceeds 0.10 ({name}: {unclear}/{labelled})")
    result = dict(
        completeness_rule=COMPLETENESS_RULE, validity=validity, warnings=warnings,
        run_id=frozen.get("run_id"), detector_version=frozen["detector_version"],
        detector_sha=frozen["detector_sha256"],
        manifest_sha256=frozen["manifest_sha256"], n_manifest=len(resolved),
        n_by_stratum={h: sum(row["stratum"] == h for row in manifest_rows) for h in STRATA},
        label_counts=label_counts,
        label_source_counts={k: source_counts[k] for k in ("owner", "judge", "none")},
        owner_judge_agreement=dict(n=n_agreement, agree=agree,
                                  percent=100 * agree / n_agreement if n_agreement else None,
                                  kappa=kappa),
        precision=precision,
        precision_by_signal={
            signal: _precision([(row, label) for row, label in resolved
                                if signal in row["signals"]])
            for signal in ("reask", "contradiction", "repeat")},
        recall=_recall(resolved, frozen, seed, reps), sensitivity=sensitivity,
        gate=gate, lost_fraction=lost_fraction, window=frozen.get("window"),
        concentrated=bool(frozen.get("concentrated", False)))
    if all("kind" in row for row in manifest_rows):
        kinds = sorted({row["kind"] for row in manifest_rows})
        result["precision_by_kind"] = {}
        result["recall_by_kind"] = {}
        for kind in kinds:
            subset = [(row, label) for row, label in resolved if row["kind"] == kind]
            result["precision_by_kind"][kind] = _precision(subset)
            populations = {
                h: frozen["strata"][h].get("by_kind", {}).get(kind, {})
                for h in STRATA}
            result["recall_by_kind"][kind] = (
                _recall_point(subset, {"strata": populations})
                if all("N" in populations[h] for h in STRATA) else None)
    if result["concentrated"]:
        result["precision_basis"] = basis
    return result


def _print_result(result):
    p = result["precision"]
    if p["n"]:
        print(f"precision: {p['tp']}/{p['n']} = {p['point']:.4f} "
              f"wilson95 [{p['lo']:.4f}, {p['hi']:.4f}]")
    else:
        print("precision: 0/0 = null")
    bootstrap = p["bootstrap"]
    print(f"precision bootstrap95 [{bootstrap['lo']}, {bootstrap['hi']}] "
          f"discarded: {bootstrap['n_discarded']}/{bootstrap['reps']}")
    for warning in result["warnings"]:
        print("warning: " + warning)
    recall = result["recall"]
    if recall["estimable"]:
        print(f"recall: {recall['recall_point']:.4f} "
              f"ci95 [{recall['ci_lo']}, {recall['ci_hi']}] "
              f"discarded: {recall['n_discarded']}/{recall['reps']}")
    else:
        print("recall: " + recall["reason"])
    for kind, precision in sorted(result.get("precision_by_kind", {}).items()):
        point = "null" if precision["point"] is None else f"{precision['point']:.4f}"
        value = result["recall_by_kind"][kind]
        kind_recall = "null" if value is None else f"{value:.4f}"
        print(f"kind {kind}: precision {precision['tp']}/{precision['n']} = "
              f"{point} recall {kind_recall}")
    print("gate: " + result["gate"]["verdict"])
    print(f"lost: {result['label_counts']['lost']}/{result['n_manifest']}")
    print(f"unclear: {result['label_counts']['unclear']}")
    for source, count in result["label_source_counts"].items():
        print(f"source {source}: {count}")


def cmd_redo_score(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm-router redo-score")
    parser.add_argument("--run", required=True)
    parser.add_argument("--labels", type=Path, action="append")
    parser.add_argument("--dry-score", action="store_true")
    parser.add_argument("--seed", type=int, default=360)
    parser.add_argument("--reps", type=int, default=2000)
    options = parser.parse_args(args)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", options.run):
        print("Refused: invalid run id.")
        return 2
    if options.reps < 1:
        print("Refused: invalid bootstrap repetitions.")
        return 2
    run_dir = paths.state_path("redo_validation", options.run)
    if os.path.lexists(run_dir / "result.json"):
        print("Refused: run already used.")
        return 2
    if os.path.lexists(run_dir / "test.used"):
        print("Refused: run is spent (test.used exists) and has no result.json; "
              "fail-safe, it cannot be re-scored.")
        return 2
    try:
        frozen = json.loads((run_dir / "frozen.json").read_text(encoding="utf-8"))
        manifest = (run_dir / "manifest.jsonl").read_bytes()
        if frozen["detector_sha256"] != hashlib.sha256(
                Path(redo_signal.__file__).read_bytes()).hexdigest():
            raise ValueError("detector mismatch")
        if frozen["manifest_sha256"] != hashlib.sha256(manifest).hexdigest():
            raise ValueError("manifest mismatch")
        rows = [json.loads(line) for line in manifest.decode("utf-8").splitlines() if line.strip()]
        labels = []
        for path in options.labels or [run_dir / "labels.jsonl"]:
            labels.extend(json.loads(line) for line in
                          path.read_text(encoding="utf-8").splitlines() if line.strip())
        result = score(rows, frozen, labels, seed=options.seed, reps=options.reps)
        result["run_id"] = options.run
        content = json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
    except (OSError, ValueError, KeyError, TypeError):
        print("Refused: invalid or unavailable scoring inputs.")
        return 2
    if options.dry_score:
        for h in STRATA:
            counts = result["label_counts"]["by_stratum"][h]
            labelled = sum(counts[k] for k in LABELS if k != "lost")
            print(f"{h}: labelled={labelled} unclear={counts['unclear']} "
                  f"lost={counts['lost']}")
        print("manifest sha: match")
        print("detector sha: match")
        print(f"agreement n: {result['owner_judge_agreement']['n']}")
        return 0
    if not options.dry_score:
        try:
            _write_private(run_dir / "test.used", datetime.now(timezone.utc).isoformat() + "\n")
            _write_private(run_dir / "result.json", content)
        except OSError:
            print("Refused: scoring output unavailable.")
            return 2
    _print_result(result)
    return 0
