"""The lane A ``shadow_pairs`` writer (PR3; plan W1-4; D-292 item 4): turns the judged Haiku-vs-Frontier pairs of
lane A into the batch row S1 reads in ``lane_quality.jsonl``. Nothing wrote ``kind=shadow_pairs`` rows before this,
so without it the lane ``(proxy, haiku, tech_op)`` could never be quality-sampled and lane A would add 0 to the
S1 numerator (``lane_quality`` only reads).

INPUT. ``shadow_frontier/verdicts.jsonl`` rows of lane A (``lane == "A"``): ``task_group``, ``rate_phase``,
``acceptable`` (Haiku's answer judged acceptable), ``frontier_acceptable``, ``cannot_judge``, ``judge_model``,
``judge_brief_sha256``. The arm's rows carry no ``lane`` and are never read here.

PER TASK GROUP, over the window ``[since, until]`` (default: the trailing 14 days):

* a **decided** pair has two boolean verdicts and ``cannot_judge`` not true. ``n`` counts decided pairs only;
  ``cannot_judge`` pairs are excluded from ``n``, ``k_A`` and ``k_F`` and reported with their share of all judged
  attempts;
* a row that lacks ``task_group`` or ``rate_phase``, was judged by another model, or under another brief, is not
  evidence: it is counted in ``excluded`` and never in ``n``;
* ``k_A`` / ``k_F`` = decided pairs where Haiku / the Frontier answer was acceptable;
* ``gap_pts = (k_F/n - k_A/n) * 100`` (positive = Haiku worse), ``newcombe_lb`` = the lower bound of
  ``acc_A - acc_F`` from ``eligibility.newcombe_diff_ci`` (unpaired Newcombe method 10), both through
  ``eligibility.evidence_from_counts``, the function behind ``eligibility.grade`` and
  ``lane_quality.batch_passed``. One rule, no copy of it;
* a row is written ONLY at ``n >= 60`` and a cannot_judge share of at most 20% (below that the lane is
  "not informative": nothing is written and ``lane_quality`` keeps whatever batch it had). A written batch may
  pass or fail: a failing one un-samples the lane, which is the point of "the latest batch decides".

OUTPUT, one JSON line appended to ``lane_quality.jsonl`` (0600 through ``state_files``):

    {"ts": <epoch s>, "door": "proxy", "served_class": "haiku", "task_group": "tech_op",
     "kind": "shadow_pairs", "n": 64, "gap_pts": 1.6, "newcombe_lb": -7.4, "source": "lane_a_pairs:...",
     "lane": "A", "k_laneA": 60, "k_frontier": 61, "cannot_judge": 3, "cannot_judge_share": 0.045,
     "since": <epoch s>, "until": <epoch s>}

``door/served_class/task_group/kind/n/gap_pts/newcombe_lb/source/ts`` are the keys ``lane_quality`` reads; the rest
is audit. A batch with the same ``source`` is not written twice (the source hashes ``n``, the decided pair ids and
their counts), so running the writer again changes nothing.

Run: ``python -m llm_router.lane_quality_writer [--since EPOCH_OR_ISO] [--until EPOCH_OR_ISO] [--dry-run]``;
``python -m llm_router.shadow_frontier judge`` runs it after the judge. It makes no model call and writes
nothing when there is no lane A verdict row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from llm_router import lane_a_shadow, lane_quality, paths, state_files

MIN_PAIRS = 60
JUDGE_MODEL = "sonnet"


def _verdicts_path() -> Path:
    from llm_router.proxy import haiku_guard

    return haiku_guard.shadow_verdicts_path()


def _read_jsonl(path: Path) -> list[dict]:
    rows = lane_a_shadow.read_verdict_rows(path)
    return rows or []


def _ts(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def tally(rows: Iterable[dict], *, since: float, until: float, brief_sha: str | None = None) -> dict[str, dict]:
    """``{task_group: counts}`` over the lane A verdict ``rows`` stamped inside ``[since, until]``.

    ``counts``: ``n`` (decided), ``k_a``, ``k_f``, ``cannot_judge``, ``excluded`` (``{reason: count}``) and
    ``ids`` (the decided pairs' task ids, for the source hash)."""
    if brief_sha is None:
        from llm_router import shadow_frontier

        brief_sha = shadow_frontier.JUDGE_BRIEF_SHA256
    out: dict[str, dict] = {}

    def group(name: str) -> dict:
        return out.setdefault(name, {"n": 0, "k_a": 0, "k_f": 0, "cannot_judge": 0, "excluded": {}, "ids": []})

    for r in rows:
        if not lane_a_shadow.is_lane_a(r):
            continue
        ts = _ts(r.get("ts"))
        if ts is None or not since <= ts <= until:
            continue
        name = r.get("task_group") if isinstance(r.get("task_group"), str) and r.get("task_group") else None
        if name is None or r.get("rate_phase") not in (lane_a_shadow.PHASE_FILL, lane_a_shadow.PHASE_STEADY):
            group(name or "(untagged)")["excluded"]["missing_tags"] = \
                group(name or "(untagged)")["excluded"].get("missing_tags", 0) + 1
            continue
        g = group(name)
        if r.get("judge_model") != JUDGE_MODEL or r.get("judge_brief_sha256") != brief_sha:
            g["excluded"]["judge_mismatch"] = g["excluded"].get("judge_mismatch", 0) + 1
            continue
        if r.get("cannot_judge") is True:
            g["cannot_judge"] += 1
            continue
        if not lane_a_shadow.is_decided(r):
            g["cannot_judge"] += 1  # a null label is "cannot judge" for the lane (prereg section 6)
            continue
        g["n"] += 1
        g["k_a"] += 1 if r["acceptable"] else 0
        g["k_f"] += 1 if r["frontier_acceptable"] else 0
        g["ids"].append(str(r.get("task_id")))
    return out


def batch_row(task_group: str, counts: dict, *, now: float, since: float, until: float) -> tuple[dict | None, str]:
    """``(row, status)``: the ``lane_quality`` row for one task group, or None with the reason it is not written
    (``n_below_min`` or ``cannot_judge_share``)."""
    from llm_router import eligibility

    n, cj = counts["n"], counts["cannot_judge"]
    attempts = n + cj
    share = (cj / attempts) if attempts else 0.0
    if n < MIN_PAIRS:
        return None, "n_below_min"
    if share > lane_a_shadow.CANNOT_JUDGE_MAX_SHARE:
        return None, "cannot_judge_share"
    ev = eligibility.evidence_from_counts(counts["k_a"], n, counts["k_f"], n)
    # The source names the evidence, not the window it was found in: the same decided pairs and counts give the same
    # source however often the writer runs, so a re-run writes nothing new.
    digest = hashlib.sha256(
        ("\n".join(sorted(counts["ids"])) + f"\n{counts['k_a']}/{counts['k_f']}").encode("utf-8")).hexdigest()[:16]
    source = f"lane_a_pairs:{task_group}:n={n}:{digest}"
    return {"ts": round(now, 3), "door": lane_a_shadow.DOOR, "served_class": lane_a_shadow.SERVED_CLASS,
            "task_group": task_group, "kind": "shadow_pairs", "n": ev.n, "gap_pts": ev.gap_pts,
            "newcombe_lb": ev.lb_pts, "source": source, "lane": lane_a_shadow.LANE_TAG,
            "k_laneA": counts["k_a"], "k_frontier": counts["k_f"], "cannot_judge": cj,
            "cannot_judge_share": round(share, 4), "since": since, "until": until}, "written"


def write_all(*, now: float | None = None, since: float | None = None, until: float | None = None,
              dry_run: bool = False, verdicts: Path | None = None, out: Path | None = None) -> dict:
    """Write one batch row per task group that is informative. Returns a summary
    ``{"groups": {task_group: {...counts, "status", "grade"}}, "written": int}``. Creates no file when there is no
    lane A verdict row, and none under ``dry_run``."""
    now = time.time() if now is None else now
    until = now if until is None else until
    since = until - lane_a_shadow.WINDOW_DAYS * 86400.0 if since is None else since
    rows = _read_jsonl(verdicts or _verdicts_path())
    groups = tally(rows, since=since, until=until)
    target = out or paths.state_path(lane_quality.FILE)
    have = {r.get("source") for r in lane_quality.read_rows(target)} if groups else set()
    summary: dict = {"groups": {}, "written": 0, "since": since, "until": until}
    from llm_router import eligibility

    for name, counts in sorted(groups.items()):
        row, status = batch_row(name, counts, now=now, since=since, until=until)
        info = {k: counts[k] for k in ("n", "k_a", "k_f", "cannot_judge", "excluded")}
        if row is None:
            info["status"] = status
        else:
            info["grade"] = eligibility.grade({"n": row["n"], "gap_pts": row["gap_pts"], "lb_pts": row["newcombe_lb"]})
            info.update(gap_pts=row["gap_pts"], newcombe_lb=row["newcombe_lb"])
            if row["source"] in have:
                info["status"] = "already_written"
            elif dry_run:
                info["status"] = "would_write"
            else:
                with state_files.open_private(target, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
                have.add(row["source"])
                info["status"] = "written"
                summary["written"] += 1
        summary["groups"][name] = info
    return summary


def _parse_when(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        d = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.lane_quality_writer",
                                 description="Lane A: write the judged Haiku-vs-Frontier pairs to lane_quality.jsonl.")
    ap.add_argument("--since", help="window start (epoch seconds or ISO 8601); default 14 days before --until")
    ap.add_argument("--until", help="window end (epoch seconds or ISO 8601); default now")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    a = ap.parse_args(argv)
    summary = write_all(since=_parse_when(a.since) if a.since else None,
                        until=_parse_when(a.until) if a.until else None, dry_run=a.dry_run)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
