"""Text-free, single-use scoring of a frozen redo validation draw.

Per-kind precision and recall are reported only for runs frozen with include_untagged.
tune_log.jsonl is append-only by procedure. A non-baseline freeze binds the log prefix
(tune_log_sha256/tune_log_bytes) and the labels digest in frozen.json, so deleting or rewriting
that prefix AFTER freeze is refused at test scoring. Editing the log BEFORE freeze cannot be
detected by this tool: it is a procedure violation that only the procedure rule prevents.
The freeze compares candidates only in the canonical tune set (``tune_argmax``): the one live tune
run's first complete scoring fixes its labels, scored pairs and projects dir, and every current-sha
candidate with tune rows must be scored there.
"""
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

from llm_router import northstar, paths, redo_signal
from llm_router.local_check.stats import wilson
from llm_router.redo_sample import VOID_FILE, _sha, _write_private, tune_dir_name

STRATA = ("F", "S", "R")
LABELS = ("redo", "not_redo", "unclear", "lost")
RULE = "precision>=0.80 and TP>=15"
COMPLETENESS_RULE = ("all flagged-stratum (F) pairs must be labelled; "
                     "any lost F pair voids; overall lost > 5% voids")
NOT_ESTIMABLE_CONCENTRATED = "not estimable (concentrated draw)"
MAX_WIDENINGS = 2           # distinct non-baseline (candidate, detector SHA) pairs (M0.9 rule)
TUNE_SIGNALS = (redo_signal.SIGNAL_REASK, redo_signal.SIGNAL_CONTRADICTION, redo_signal.SIGNAL_REPEAT)


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
        return dict(estimable=False, reason=NOT_ESTIMABLE_CONCENTRATED)
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
        detector_variant=frozen.get("detector_variant", redo_signal.BASELINE_VARIANT),
        tune_labels_sha256=frozen.get("tune_labels_sha256"),
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
    # Legacy organic-only manifests also carry ``kind``: only an include_untagged run has
    # per-kind populations to report against.
    if frozen.get("include_untagged") is True and all("kind" in row for row in manifest_rows):
        kinds = sorted({row["kind"] for row in manifest_rows})
        result["precision_by_kind"] = {}
        result["recall_by_kind"] = {}
        if result["concentrated"]:
            result["recall_by_kind_reason"] = NOT_ESTIMABLE_CONCENTRATED
        for kind in kinds:
            subset = [(row, label) for row, label in resolved if row["kind"] == kind]
            result["precision_by_kind"][kind] = _precision(subset)
            populations = {
                h: frozen["strata"][h].get("by_kind", {}).get(kind, {})
                for h in STRATA}
            result["recall_by_kind"][kind] = (
                _recall_point(subset, {"strata": populations})
                if not result["concentrated"]
                and all("N" in populations[h] for h in STRATA) else None)
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
        kind_recall = (f"{value:.4f}" if value is not None
                       else result.get("recall_by_kind_reason", "null"))
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
    parser.add_argument("--tune", action="store_true",
                        help="score a candidate detector variant against a tune set's labels")
    parser.add_argument("--candidate", choices=sorted(redo_signal.VARIANTS), default=None)
    parser.add_argument("--projects-dir", type=Path, default=None)
    options = parser.parse_args(args)
    if not options.tune and (options.candidate or options.projects_dir):
        parser.error("--candidate and --projects-dir need --tune")
    if options.tune:
        return cmd_redo_score_tune(options)
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
        if frozen.get("purpose") == "tune":
            print("Refused: tune run: use --tune.")
            return 2
        manifest = (run_dir / "manifest.jsonl").read_bytes()
        if frozen["detector_sha256"] != hashlib.sha256(
                Path(redo_signal.__file__).read_bytes()).hexdigest():
            print("Refused: the detector source (redo_signal.py) changed after this run was frozen "
                  "(for example a run frozen before PR #706 or PR #708 edited redo_signal.py). "
                  "The frozen test half stays unspent: re-freeze a new run with the current detector; "
                  "do not edit the old run.")
            return 2
        if frozen.get("detector_variant", redo_signal.BASELINE_VARIANT) == redo_signal.BASELINE_VARIANT:
            # A real baseline freeze records null tune provenance; a non-null field means frozen.json
            # was edited (for example detector_variant rewritten to the baseline to skip verification).
            if any(frozen.get(key) is not None for key in TUNE_PROVENANCE_KEYS):
                print("Refused: a baseline freeze carries tune provenance; frozen.json was edited.")
                return 2
        else:
            try:
                verify_tune_provenance(
                    paths.state_path("redo_validation", "tune_log.jsonl"), frozen)
            except TuneFreezeRefused as refused:
                if refused.cause == "labels_mismatch":
                    print(refused.message)
                else:
                    print("Refused: frozen tune-log provenance no longer passes the freeze gate: "
                          + refused.message.removeprefix("Refused: "))
                return 2
            except (OSError, ValueError, KeyError, TypeError):
                print("Refused: frozen tune-log provenance is invalid or unavailable.")
                return 2
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


# ---- tune scoring: candidate detector variants against a tune set (no single-use marker) ----

def _prf(tp, fp, fn):
    def ratio(num, den):
        return num / den if den else None
    return dict(tp=tp, fp=fp, fn=fn, precision=ratio(tp, tp + fp), recall=ratio(tp, tp + fn),
                f1=ratio(2 * tp, 2 * tp + fp + fn))


def _scope_counts(scored, in_scope):
    """``scored``: (weight, label, signals) for labelled pairs. Unweighted and stratum-weighted counts."""
    out = {}
    for name, weight_of in (("unweighted", lambda w: 1.0), ("weighted", lambda w: w)):
        tp = fp = fn = 0.0
        for weight, label, signals in scored:
            w = weight_of(weight)
            hit = in_scope(signals)
            if label == "redo":
                tp, fn = (tp + w, fn) if hit else (tp, fn + w)
            elif hit:
                fp += w
        if name == "unweighted":
            out[name] = _prf(int(tp), int(fp), int(fn))
        else:
            out[name] = {k: (round(v, 4) if isinstance(v, float) else v)
                         for k, v in _prf(tp, fp, fn).items()}
    return out


def score_tune_candidate(rows, labels_by_pair, signals_by_pair) -> dict:
    """Pure, text-free. ``labels_by_pair``: pair_id -> redo|not_redo|unclear|lost; ``signals_by_pair``:
    pair_id -> the candidate's signals (absent when the transcript could not be re-read, counted lost).

    Per signal: tp = redo and the signal fired, fp = not_redo and it fired, fn = redo and it did not.
    Overall counts a pair as flagged when any signal fired. Weighted counts use the manifest's stratum
    weights N_h/n_h, so recall is not biased by the thin sampling of unflagged strata."""
    scored = []
    scored_ids = []
    n_unclear = n_lost = 0
    for row in rows:
        label = labels_by_pair.get(row["pair_id"], "lost")
        signals = signals_by_pair.get(row["pair_id"])
        if label == "unclear":
            n_unclear += 1
        elif label not in ("redo", "not_redo") or signals is None:
            n_lost += 1
        else:
            weight = round(row["weight_N_h_combined"] / row["n_h_combined"], 6)
            scored.append((weight, label, tuple(signals)))
            scored_ids.append(row["pair_id"])
    return dict(
        n_manifest=len(rows), n_scored=len(scored), n_unclear=n_unclear, n_lost=n_lost,
        scored_pairs_sha256=scored_pairs_sha256(scored_ids),
        overall=_scope_counts(scored, bool),
        per_signal={sig: _scope_counts(scored, lambda signals, sig=sig: sig in signals)
                    for sig in TUNE_SIGNALS})


def scored_pairs_sha256(pair_ids) -> str:
    """sha256 over the sorted pair ids a tune score actually scored (one per line): which pairs, not
    how many, so two rows with equal counts on different pairs never compare as one tune set."""
    return hashlib.sha256("\n".join(sorted(pair_ids)).encode("utf-8")).hexdigest()


def projects_dir_sha256(projects_dir) -> str:
    """sha256 of the projects dir's real path: records which transcript tree was read, never the path."""
    return _sha(os.path.realpath(projects_dir))


def candidate_signals(rows, frozen, projects_dir, variant) -> tuple[dict, int]:
    """Re-derive the candidate's signals per manifest pair from the transcripts, in memory only.

    Returns ``(pair_id -> signals, number of sessions that raised)``; pairs not found stay absent."""
    salt = frozen["salt"]
    wanted = {}
    for row in rows:
        wanted.setdefault(row["session_hash"], []).append(row)
    out, errors, seen = {}, 0, set()
    for path in sorted(Path(projects_dir).glob("*/*.jsonl")):
        sid = path.stem
        session_rows = wanted.get(_sha(f"{salt}|{sid}")[:16])
        if sid in seen or not session_rows:
            continue
        seen.add(sid)
        try:
            prompts = redo_signal.human_prompts(redo_signal.read_transcript(path))
            for row in session_rows:
                turn = row["turn_index"]
                if 1 <= turn < len(prompts) and _sha(f"{sid}|{turn}|{salt}")[:16] == row["pair_id"]:
                    out[row["pair_id"]] = tuple(redo_signal._signals(
                        prompts[turn].text, prompts[turn - 1].text, variant))
        except (OSError, ValueError, KeyError, TypeError):
            errors += 1
    return out, errors


def resolve_tune_labels(rows, label_rows) -> dict:
    """pair_id -> label, the owner label overriding the judge's; invalid or conflicting rows raise."""
    ids = {row["pair_id"] for row in rows}
    by_pair = {}
    for row in label_rows:
        pair, label, source = row["pair_id"], row["label"], row["source"]
        if pair not in ids or label not in LABELS or source not in ("owner", "judge"):
            raise ValueError("invalid label row")
        sources = by_pair.setdefault(pair, {})
        if source in sources and sources[source] != label:
            raise ValueError("conflicting labels")
        sources[source] = label
    return {pair: sources.get("owner", sources.get("judge")) for pair, sources in by_pair.items()}


def detector_sha256() -> str:
    return hashlib.sha256(Path(redo_signal.__file__).read_bytes()).hexdigest()


def tune_row_sha256(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def _parse_tune_log(content: bytes) -> list[dict]:
    rows = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("candidate"), str) or not row["candidate"]:
            raise ValueError("invalid tune log")
        if row.get("detector_sha256") is not None and not isinstance(row["detector_sha256"], str):
            raise ValueError("invalid detector SHA")
        # Reject non-JSON numeric values anywhere, including older candidate rows.
        tune_row_sha256(row)
    return rows


TUNE_PROVENANCE_KEYS = ("tune_row_sha256", "tune_log_sha256", "tune_log_bytes", "tune_run",
                        "tune_manifest_sha256", "tune_labels_sha256", "tune_scored_pairs_sha256",
                        "tune_projects_dir_sha256")


class TuneFreezeRefused(ValueError):
    """A non-baseline freeze (or its later verification) is refused for one named ``cause``.

    ``message`` is counts-only: no path, pair id or text."""

    def __init__(self, cause: str, message: str) -> None:
        self.cause = cause
        self.message = message
        super().__init__(message)


def labels_sha256(paths_in: list[Path]) -> str:
    """Digest of the label files a tune score read: sha256 over the sorted per-file sha256 hex digests
    (one per line), so the order the files were passed in does not matter. Bytes only, never a path."""
    digests = sorted(hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths_in)
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


OWNER_DONE_FILE = "owner_done.json"
OWNER_LABEL_FILE = "labels_owner.jsonl"
JUDGE_LABEL_FILE = "labels_judge.jsonl"


def _owner_label_count(content: bytes) -> int:
    """Distinct pair ids the owner label file holds (a malformed line is not a label)."""
    ids = set()
    for line in content.decode("utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("pair_id"), str):
            ids.add(item["pair_id"])
    return len(ids)


# Design P36C-DESIGN.md section 3 step 2: the owner labels every F pair "plus 20 random S/R pairs for the
# recall side". The marker needs at least that many S/R spot checks in the owner sample.
OWNER_MIN_SPOT_CHECKS = 20
OWNER_SAMPLE_FILE = "owner_sample.json"


def owner_sample_state(run_dir: Path) -> tuple[int, str] | None:
    """``(others, digest)`` of the owner sample of ``run_dir`` NOW: the number of S/R spot checks recorded in
    ``owner_sample.json`` and the sha256 over the sorted ids of the whole sample (every F pair of the
    manifest plus those spot checks, one per line). None when either file is missing or malformed."""
    try:
        saved = json.loads((run_dir / OWNER_SAMPLE_FILE).read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in
                (run_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None
    others = saved.get("others") if isinstance(saved, dict) else None
    if (not isinstance(others, list) or not all(isinstance(item, str) for item in others)
            or len(set(others)) != len(others) or not all(isinstance(row, dict) for row in rows)):
        return None
    frontier = {row.get("pair_id") for row in rows if row.get("stratum") == "F"}
    if not all(isinstance(item, str) for item in frontier) or frontier.intersection(others):
        return None
    sample = sorted(frontier | set(others))
    return len(others), hashlib.sha256("\n".join(sample).encode("utf-8")).hexdigest()


def write_owner_done(run_dir: Path) -> dict | None:
    """Record the owner labelling pass of ``run_dir`` as complete: ``owner_done.json`` holds counts, the
    sha256 of ``labels_owner.jsonl`` and the digest of the owner sample at that moment (no pair id, no
    text). An explicit marker, because the owner may agree with the judge on every pair. Writes nothing and
    returns None when the owner sample holds fewer than ``OWNER_MIN_SPOT_CHECKS`` S/R spot checks. The
    caller checks that every sampled pair is labelled. Atomic replace; a later owner label or a changed
    owner sample (a larger ``--others``) makes it stale."""
    state = owner_sample_state(run_dir)
    if state is None or state[0] < OWNER_MIN_SPOT_CHECKS:
        return None
    content = (run_dir / OWNER_LABEL_FILE).read_bytes()
    record = dict(n_labelled=_owner_label_count(content),
                  labels_owner_sha256=hashlib.sha256(content).hexdigest(),
                  others=state[0], owner_sample_sha256=state[1])
    temporary = run_dir / (OWNER_DONE_FILE + ".tmp")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
              encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, run_dir / OWNER_DONE_FILE)
    return record


def owner_done_valid(run_dir: Path) -> bool:
    """The owner pass is recorded complete, ``labels_owner.jsonl`` is still the file it recorded, the
    marker holds at least ``OWNER_MIN_SPOT_CHECKS`` spot checks, and the owner sample is still the one it
    recorded (a sample extended by a larger ``--others`` is not covered by an older marker)."""
    try:
        record = json.loads((run_dir / OWNER_DONE_FILE).read_text(encoding="utf-8"))
        content = (run_dir / OWNER_LABEL_FILE).read_bytes()
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    state = owner_sample_state(run_dir)
    return (type(record.get("n_labelled")) is int
            and record["n_labelled"] > 0
            and record["n_labelled"] == _owner_label_count(content)
            and record.get("labels_owner_sha256") == hashlib.sha256(content).hexdigest()
            and type(record.get("others")) is int
            and record["others"] >= OWNER_MIN_SPOT_CHECKS
            and state is not None
            and (record["others"], record.get("owner_sample_sha256")) == state)


def _f1(row: dict):
    value = row["overall"]["unweighted"]["f1"]
    if value is not None and (not isinstance(value, float) or not math.isfinite(value)):
        raise ValueError("invalid tune F1")
    return value


TUNE_SET_KEYS = ("tune_run", "tune_manifest_sha256", "labels_sha256", "scored_pairs_sha256",
                 "projects_dir_sha256")


def _tune_set(row: dict):
    """A row's tune-set identity: tune run, tune manifest, labels digest, WHICH pairs were scored, the
    projects dir read, and the unclear count. Rows compare only when all of these are equal."""
    return tuple(row.get(key) for key in TUNE_SET_KEYS) + (row.get("n_unclear"),)


def _has_identity(row: dict) -> bool:
    key = _tune_set(row)
    return all(isinstance(value, str) for value in key[:-1]) and type(key[-1]) is int


def _complete(row: dict) -> bool:
    """Every manifest pair was labelled (or unclear) and re-read: no lost pair (B1 of the PR #708 review)."""
    return _has_identity(row) and type(row.get("n_lost")) is int and row["n_lost"] == 0


def _canonical_row(row: dict) -> bool:
    """A complete row scored AFTER the owner labelling pass was recorded complete (``owner_done``) and
    reading the owner label file. Earlier scorings print F1 and are logged, but are non-canonical."""
    return _complete(row) and row.get("owner_done") is True


def canonical_tune_set(rows: list[dict], tune_run: str):
    """The tune-set identity of the FIRST canonical row (complete, owner pass done) scored on
    ``tune_run``, or None. Its labels digest is the run's canonical labels source: a later score on that
    run with other labels is refused."""
    return next((_tune_set(row) for row in rows
                 if row.get("tune_run") == tune_run and _canonical_row(row)), None)


def canonical_tune_run() -> str:
    """The one live (not voided) tune run of this study; refuses with counts only otherwise."""
    from llm_router.redo_sample import tune_run_states
    live, voided = tune_run_states()
    if len(live) != 1:
        raise TuneFreezeRefused(
            "tune_runs", f"Refused: a non-baseline freeze needs exactly one live tune run; found {len(live)} "
            f"live and {len(voided)} voided.")
    return live[0]


def tune_argmax(rows: list[dict], candidate: str, detector_sha: str, tune_run: str) -> dict:
    """The candidate's latest row at ``detector_sha`` in the canonical tune set, if it is the strict F1
    argmax there.

    The canonical tune set is fixed by the run, not by the candidate: the identity (``_tune_set``) of the
    first complete row on ``tune_run``, the study's one live tune run. Only complete rows (n_lost 0) with
    exactly that identity are compared. The set must hold a v2 baseline row with a defined F1, and EVERY
    non-baseline candidate with any tune row at ``detector_sha`` must have a row in it, so leaving a
    competitor out of the set refuses. Each competitor (a (candidate, detector sha) pair of ANOTHER
    candidate) is ranked by its MAXIMUM F1 in the set, so re-scoring cannot lower it; the candidate by its
    MINIMUM over its rows at ``detector_sha`` in the set; its own rows at other shas are not competitors.
    A tie refuses. Raises ``TuneFreezeRefused``."""
    for row in rows:
        _f1(row)
    own = [row for row in rows if row["candidate"] == candidate]
    if not own:
        raise TuneFreezeRefused("no_row", "Refused: the tune log has no row for this candidate; "
                                "score it with redo-score --tune first.")
    current = [row for row in own if row.get("detector_sha256") == detector_sha]
    if not current:
        raise TuneFreezeRefused(
            "stale_sha", f"Refused: none of the candidate's {len(own)} tune row(s) was scored at the "
            "current detector sha256 (redo_signal.py changed after scoring).")
    key = canonical_tune_set(rows, tune_run)
    if key is None:
        if any(_complete(row) and row.get("tune_run") == tune_run for row in rows):
            raise TuneFreezeRefused(
                "owner_pending", "Refused: the live tune run has complete scorings but none was made after "
                "the owner labelling pass was recorded complete (redo-label --owner to the end); re-score "
                "the baseline and candidates after it.")
        raise TuneFreezeRefused(
            "no_complete", "Refused: the live tune run has no complete tune scoring (every manifest pair "
            "labelled and re-read, n_lost 0); complete its labels, then score the baseline and candidates.")
    same_set = [row for row in rows if _canonical_row(row) and _tune_set(row) == key]
    mine_rows = [row for row in same_set
                 if row["candidate"] == candidate and row.get("detector_sha256") == detector_sha]
    if not mine_rows:
        on_run = [row for row in current if row.get("tune_run") == tune_run]
        if any(not _has_identity(row) for row in on_run):
            raise TuneFreezeRefused(
                "labels_mismatch", "Refused: the candidate's tune row has no labels digest, scored-pair "
                "digest or tune-set identity (written before this binding), so its tune set cannot be "
                "matched.")
        lost = [row["n_lost"] for row in on_run if type(row.get("n_lost")) is int and row["n_lost"] > 0]
        if lost:
            raise TuneFreezeRefused(
                "incomplete", f"Refused: incomplete tune scoring: the candidate's {len(lost)} row(s) on the "
                f"live tune run lost pairs (largest n_lost {max(lost)}); every manifest pair must be "
                "labelled and its transcript re-read.")
        if any(_complete(row) and row.get("owner_done") is not True for row in on_run):
            raise TuneFreezeRefused(
                "owner_pending", "Refused: the candidate's complete tune row(s) were scored before the "
                "owner labelling pass was recorded complete; re-score it now.")
        raise TuneFreezeRefused(
            "not_in_set", "Refused: the candidate has no row in the canonical tune set (the live tune "
            "run's first complete scoring: same labels digest, scored pairs and projects dir); score it "
            "there.")
    baseline = [_f1(row) for row in same_set if row["candidate"] == redo_signal.BASELINE_VARIANT
                and _f1(row) is not None]
    if not baseline:
        raise TuneFreezeRefused(
            "no_baseline", f"Refused: the canonical tune set has no {redo_signal.BASELINE_VARIANT} "
            "baseline row with a defined F1; score the baseline there first.")
    required = {row["candidate"] for row in rows if row.get("detector_sha256") == detector_sha
                and row["candidate"] not in (redo_signal.BASELINE_VARIANT, candidate)}
    present = {row["candidate"] for row in same_set if row.get("detector_sha256") == detector_sha}
    missing = required - present
    if missing:
        raise TuneFreezeRefused(
            "incomplete_comparison", f"Refused: incomplete comparison: {len(missing)} other candidate(s) "
            "with tune rows at the current detector sha256 have no row in the canonical tune set; score "
            "each there before freezing.")
    mine = [_f1(row) for row in mine_rows]
    if any(value is None for value in mine):
        raise TuneFreezeRefused("undefined_f1", "Refused: the candidate's tune F1 is undefined.")
    best = min(mine)
    competitors: dict = {}
    for row in same_set:
        pair = (row["candidate"], row.get("detector_sha256"))
        value = _f1(row)
        if row["candidate"] != candidate and value is not None:
            competitors[pair] = max(value, competitors.get(pair, value))
    higher = sum(value > best for value in competitors.values())
    tied = sum(value == best for value in competitors.values())
    if higher:
        raise TuneFreezeRefused(
            "not_argmax", f"Refused: the candidate is not the tune F1 argmax: {higher} of "
            f"{len(competitors)} competitor(s) in its tune set score higher.")
    if tied:
        raise TuneFreezeRefused(
            "tie", f"Refused: the candidate ties {tied} of {len(competitors)} competitor(s) on tune F1 "
            "in its tune set; a tie fails closed.")
    return mine_rows[-1]


def _check_labels_on_disk(selected: dict, tune_run: str) -> None:
    """Recompute the digest of the label files the canonical scoring read, from the run dir NOW, and
    require the owner pass marker to still match; refuse (counts only) on any difference."""
    run_dir = paths.state_path("redo_validation", tune_run)
    names = selected.get("label_names")
    allowed = {OWNER_LABEL_FILE, JUDGE_LABEL_FILE}
    changed = TuneFreezeRefused(
        "labels_changed", "Refused: labels on disk changed since canonical scoring; restore the original "
        "label files or void the run.")
    if (not isinstance(names, list) or not names or not set(names) <= allowed
            or OWNER_LABEL_FILE not in names):
        raise changed
    try:
        current = labels_sha256([run_dir / name for name in names])
    except OSError as exc:
        raise changed from exc
    if current != selected["labels_sha256"] or not owner_done_valid(run_dir):
        raise changed


def freeze_tune_provenance(path: Path, candidate: str, detector_sha: str) -> dict:
    # Parse and hash one byte snapshot, never separate reads of a changing log.
    if not os.path.lexists(path):
        raise TuneFreezeRefused("no_log", "Refused: non-baseline freeze needs a tune log and none exists; "
                                "score the baseline and the candidate with redo-score --tune first.")
    content = path.read_bytes()
    run = canonical_tune_run()
    selected = tune_argmax(_parse_tune_log(content), candidate, detector_sha, run)
    _check_labels_on_disk(selected, run)
    return dict(tune_row_sha256=tune_row_sha256(selected),
                tune_log_sha256=hashlib.sha256(content).hexdigest(), tune_log_bytes=len(content),
                tune_run=selected["tune_run"], tune_manifest_sha256=selected["tune_manifest_sha256"],
                tune_labels_sha256=selected["labels_sha256"],
                tune_scored_pairs_sha256=selected["scored_pairs_sha256"],
                tune_projects_dir_sha256=selected["projects_dir_sha256"])


def verify_tune_provenance(path: Path, frozen: dict) -> None:
    """Re-run the freeze gate on the log prefix bound at freeze, against the study's live tune run now;
    the same row must win, on the same tune set. Only changes AFTER freeze are detectable here."""
    size = frozen["tune_log_bytes"]
    if type(size) is not int or size <= 0:
        raise ValueError("invalid tune log length")
    content = path.read_bytes()
    prefix = content[:size]
    if len(prefix) != size or hashlib.sha256(prefix).hexdigest() != frozen["tune_log_sha256"]:
        raise ValueError("tune log prefix changed")
    _parse_tune_log(content)
    selected = tune_argmax(_parse_tune_log(prefix), frozen["detector_variant"], frozen["detector_sha256"],
                           canonical_tune_run())
    if tune_row_sha256(selected) != frozen["tune_row_sha256"]:
        raise ValueError("selected tune row missing")
    if (selected["labels_sha256"] != frozen["tune_labels_sha256"]
            or selected["tune_run"] != frozen["tune_run"]
            or selected["tune_manifest_sha256"] != frozen["tune_manifest_sha256"]
            or selected["scored_pairs_sha256"] != frozen["tune_scored_pairs_sha256"]
            or selected["projects_dir_sha256"] != frozen["tune_projects_dir_sha256"]):
        raise TuneFreezeRefused("labels_mismatch", "Refused: frozen tune labels digest or tune set does "
                                "not match the selected tune row.")


def _read_tune_log(path: Path) -> list[dict]:
    if not os.path.lexists(path):
        return []
    return _parse_tune_log(path.read_bytes())


def _append_tune_log(path: Path, row: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _fmt(value):
    return "n/a" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))


def _print_scope(name, block):
    u, w = block["unweighted"], block["weighted"]
    print(f"{name}: tp={u['tp']} fp={u['fp']} fn={u['fn']} precision={_fmt(u['precision'])} "
          f"recall={_fmt(u['recall'])} f1={_fmt(u['f1'])} | weighted tp={_fmt(w['tp'])} "
          f"fp={_fmt(w['fp'])} fn={_fmt(w['fn'])} precision={_fmt(w['precision'])} "
          f"recall={_fmt(w['recall'])} f1={_fmt(w['f1'])}")


def cmd_redo_score_tune(options) -> int:
    """Score one candidate against a tune set. Repeatable: never writes ``test.used`` or ``result.json``;
    refuses a run whose frozen.json is not ``purpose: tune``, a voided tune run, a third distinct
    non-baseline (candidate, detector SHA) pair, and labels other than the run's canonical labels (the
    labels digest of the run's first complete scoring)."""
    candidate = options.candidate or redo_signal.BASELINE_VARIANT
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", options.run):
        print("Refused: invalid run id.")
        return 2
    # ``tune-<id>`` is the tune dir; a bare dir name that exists (e.g. a test run) is reached too,
    # so that the purpose guard below can refuse it by name rather than pretend it is missing.
    run = tune_dir_name(options.run)
    if not paths.state_path("redo_validation", run).is_dir() and paths.state_path(
            "redo_validation", options.run).is_dir():
        run = options.run
    run_dir = paths.state_path("redo_validation", run)
    try:
        frozen = json.loads((run_dir / "frozen.json").read_text(encoding="utf-8"))
        manifest = (run_dir / "manifest.jsonl").read_bytes()
        if frozen["manifest_sha256"] != hashlib.sha256(manifest).hexdigest():
            raise ValueError("manifest mismatch")
        rows = [json.loads(line) for line in manifest.decode("utf-8").splitlines() if line.strip()]
        purpose = frozen.get("purpose")
    except (OSError, ValueError, KeyError, TypeError):
        print("Refused: invalid or unavailable scoring inputs.")
        return 2
    if purpose != "tune":
        print("Refused: run purpose is not tune.")
        return 2
    if os.path.lexists(run_dir / VOID_FILE):
        print("Refused: tune run is voided; it cannot be scored.")
        return 2
    log_path = paths.state_path("redo_validation", "tune_log.jsonl")
    try:
        detector_sha = detector_sha256()
        log = _read_tune_log(log_path)
        logged = {(r["candidate"], r.get("detector_sha256")) for r in log
                  if r["candidate"] != redo_signal.BASELINE_VARIANT}
    except (OSError, ValueError, KeyError, TypeError):
        print("Refused: tune log unreadable.")
        return 2
    if (candidate != redo_signal.BASELINE_VARIANT and (candidate, detector_sha) not in logged
            and len(logged) >= MAX_WIDENINGS):
        print(f"Refused: at most {MAX_WIDENINGS} widenings "
              f"({len(logged)} distinct candidate+detector-sha pairs already scored).")
        return 2
    try:
        label_paths = options.labels or [p for p in (run_dir / "labels_owner.jsonl",
                                                     run_dir / "labels_judge.jsonl")
                                         if os.path.lexists(p)]
        label_rows = []
        for path in label_paths:
            label_rows.extend(json.loads(line) for line in
                              path.read_text(encoding="utf-8").splitlines() if line.strip())
        labels = resolve_tune_labels(rows, label_rows)
        labels_digest = labels_sha256(label_paths)
        label_names = sorted(p.name for p in label_paths)
        # Canonical only when the owner pass is recorded complete and the owner file was read from the run dir.
        owner_done = (owner_done_valid(run_dir) and OWNER_LABEL_FILE in label_names
                      and all(Path(p).resolve().parent == run_dir.resolve() for p in label_paths))
        projects_dir = options.projects_dir or northstar.claude_projects_dir()
        projects_digest = projects_dir_sha256(projects_dir)
        projects_default = projects_digest == projects_dir_sha256(northstar.claude_projects_dir())
        signals, errors = candidate_signals(rows, frozen, projects_dir, candidate)
        result = score_tune_candidate(rows, labels, signals)
    except (OSError, ValueError, KeyError, TypeError):
        print("Refused: invalid or unavailable scoring inputs.")
        return 2
    canonical = canonical_tune_set(log, run)
    if canonical is not None and canonical[TUNE_SET_KEYS.index("labels_sha256")] != labels_digest:
        print("Refused: labels changed for this tune run (its first complete scoring used other label "
              "files); void the run with redo-sample --void-tune and draw a new tune set.")
        return 2
    print(f"tune run: {run}\ncandidate: {candidate}")
    if not owner_done:
        print("NON-CANONICAL: the owner labelling pass is not recorded complete for this run (or the owner "
              "labels were not read); F1 is printed and logged but locks nothing and cannot be frozen. "
              "Finish redo-label --owner, then re-score.")
    print(f"n_manifest: {result['n_manifest']} n_scored: {result['n_scored']} "
          f"n_unclear: {result['n_unclear']} n_lost: {result['n_lost']}")
    if errors:
        print(f"read errors: {errors}")
    _print_scope("overall", result["overall"])
    for sig in TUNE_SIGNALS:
        _print_scope(sig, result["per_signal"][sig])
    if not result["n_scored"]:
        print("WARNING: no scored pairs; nothing logged (an empty set is not a scored candidate)")
        return 3
    try:
        _append_tune_log(log_path, dict(
            ts=datetime.now(timezone.utc).isoformat(), tune_run=run, candidate=candidate,
            detector_sha256=detector_sha, tune_manifest_sha256=frozen["manifest_sha256"],
            labels_sha256=labels_digest, label_names=label_names, owner_done=owner_done,
            scored_pairs_sha256=result["scored_pairs_sha256"],
            projects_dir_sha256=projects_digest, projects_dir_default=projects_default,
            n_manifest=result["n_manifest"],
            variant_params={"variant": candidate, "description": redo_signal.VARIANTS[candidate]},
            n_scored=result["n_scored"], n_unclear=result["n_unclear"], n_lost=result["n_lost"],
            overall=result["overall"], per_signal=result["per_signal"]))
    except OSError:
        print("Refused: tune log unavailable.")
        return 2
    return 0
