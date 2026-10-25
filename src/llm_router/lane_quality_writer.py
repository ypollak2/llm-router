"""The lane A ``shadow_pairs`` writer (PR3; plan W1-4; D-292 item 4): turns the judged Haiku-vs-Frontier pairs of
lane A into the batch row S1 reads in ``lane_quality.jsonl``. Nothing wrote ``kind=shadow_pairs`` rows before this,
so without it the lane ``(proxy, haiku, tech_op)`` could never be quality-sampled and lane A would add 0 to the
S1 numerator (``lane_quality`` only reads).

INPUT. ``shadow_frontier/verdicts.jsonl`` rows of lane A (``lane == "A"``): ``task_group``, ``rate_phase``,
``acceptable`` (Haiku's answer judged acceptable), ``frontier_acceptable``, ``cannot_judge``, ``judge_model``,
``judge_brief_sha256``, ``served_body_sha`` / ``replay_body_sha``. The arm's rows carry no ``lane`` and are never
read here. A judge error, timeout, unparsed or malformed reply, or model mismatch is a verdict row with
``cannot_judge: true`` and a ``cannot_judge_reason`` (written by ``shadow_frontier.judge_pending`` for lane A pairs
only), so it counts in the cannot_judge share (prereg section 6).

PER TASK GROUP, over the window ``[since, until]`` (default: the owner flip ``lane_a_shadow.read_flip`` to now; the
trailing 14 days when no flip is recorded):

* **one pair per step** (prereg section 3): rows are reduced to the FIRST verdict of each
  ``(session_id, msg_count)``; later ones are ``excluded["duplicate_step"]``;
* a **decided** pair has two boolean verdicts and ``cannot_judge`` not true. ``n`` counts decided pairs only;
  ``cannot_judge`` pairs are excluded from ``n``, ``k_A`` and ``k_F`` and reported with their share of all judged
  attempts;
* a row that lacks ``task_group`` or ``rate_phase``, was judged by another model or brief, or whose
  ``served_body_sha`` differs from the ``replay_body_sha`` (or lacks either) is not evidence: it is counted in
  ``excluded`` (``missing_tags``, ``judge_mismatch``, ``body_sha_mismatch``) and never in ``n``;
* ``k_A`` / ``k_F`` = decided pairs where Haiku / the Frontier answer was acceptable;
* ``gap_pts = (k_F/n - k_A/n) * 100`` (positive = Haiku worse), ``newcombe_lb`` = the lower bound of
  ``acc_A - acc_F`` from ``eligibility.newcombe_diff_ci`` (unpaired Newcombe method 10), both through
  ``eligibility.evidence_from_counts``, the function behind ``eligibility.grade`` and
  ``lane_quality.batch_passed``. One rule, no copy of it.

WINDOW (D-301, amended by D-302; the prereg owner's decisions). The S1 window of a task group closes at whichever
comes FIRST: (a) the first judge batch with at least 60 decided pairs (after the dedupe) AND a cannot_judge share of
at most 20%, or (b) 28 days after the owner flip. The closing batch is the one FINAL row (``window_complete: true``),
and the only one S1 counts; ``lane_quality`` ignores a ``shadow_pairs`` row without it.

* A batch with n >= 60 and MORE than 20% cannot_judge does NOT close the window (a judge outage must not burn it):
  it is written NON-final (``window_complete: false``, ``diagnostic: true``) and the window stays open; a later
  batch at or under 20% closes it informative.
* At flip + 28 days the window closes NOT INFORMATIVE (``informative: false``, no S1 credit) when n < 60 OR the last
  batch's cannot_judge share is above 20%.
* The pair window is clamped to ``until = min(now, flip + 28 d)``: a pair after day 28 never counts, even when the
  writer first runs later. Pairs from before the flip are never counted and show in ``excluded["before_flip"]``.
* The flip (``lane_a_flip.json``, written by the proxy: the first request that saw all four switches on) is the
  window start. With no flip recorded the n >= 60 close still applies and the 28-day close never fires.
* Once a task group has a final row nothing is written for it again (``window_closed``) and nothing reopens it,
  not even when the row is later invalidated (D-302: a changed lane A model, judge brief sha, prompt set or breaker
  thresholds); only a prereg amendment opens a new window. Each row is stamped with those four identities.

The 60, 20% and 28 days are ``lane_a_shadow.CONSTANTS``.

OUTPUT, one JSON line appended to ``lane_quality.jsonl`` (0600 through ``state_files``):

    {"ts": <epoch s>, "door": "proxy", "served_class": "haiku", "task_group": "tech_op",
     "kind": "shadow_pairs", "n": 64, "gap_pts": 1.6, "newcombe_lb": -7.4, "source": "lane_a_pairs:...",
     "lane": "A", "k_laneA": 60, "k_frontier": 61, "cannot_judge": 3, "cannot_judge_share": 0.045,
     "excluded": {...}, "window_complete": true, "informative": true, "close_reason": "n_reached",
     "not_informative_reason": null, "identity": {...4 keys}, "flip_ts": <epoch s>, "since": <epoch s>,
     "until": <epoch s>}

``door/served_class/task_group/kind/n/gap_pts/newcombe_lb/source/ts`` are the keys ``lane_quality`` reads (plus
``window_complete`` and ``informative``); the rest is audit.

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

from llm_router import lane_a_shadow, lane_quality, paths, shadow_frontier, state_files

MIN_PAIRS = lane_a_shadow.FINAL_WINDOW_PAIRS
JUDGE_MODEL = shadow_frontier.JUDGE_MODEL
UNTAGGED = "(untagged)"
CLOSE_PAIRS = "n_reached"
CLOSE_TIME = "window_28d"


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


def tally(rows: Iterable[dict], *, since: float, until: float, brief_sha: str | None = None,
          flip: float | None = None) -> dict[str, dict]:
    """``{task_group: counts}`` over the lane A verdict ``rows`` stamped inside ``[since, until]``.

    One pair per step: rows are first reduced to the first verdict of each ``(session_id, msg_count)``
    (``lane_a_shadow.dedupe_steps``); the rest are ``excluded["duplicate_step"]``.

    ``counts``: ``n`` (decided), ``k_a``, ``k_f``, ``cannot_judge`` (judged attempts that decided nothing: explicit
    cannot_judge, a null label, and the judge failures of ``shadow_frontier.judge_pending``, which are verdict rows
    marked cannot_judge with a reason), ``excluded`` (``{reason: count}``) and ``ids`` (the decided pairs' task ids,
    for the source hash). With a ``flip``, lane A rows stamped before it are never counted and are reported as
    ``excluded["before_flip"]`` (every such row, before the dedupe)."""
    if brief_sha is None:
        brief_sha = shadow_frontier.JUDGE_BRIEF_SHA256
    out: dict[str, dict] = {}

    def group(name: str) -> dict:
        return out.setdefault(name, {"n": 0, "k_a": 0, "k_f": 0, "cannot_judge": 0, "excluded": {}, "ids": []})

    def exclude(name: str, why: str) -> None:
        g = group(name)
        g["excluded"][why] = g["excluded"].get(why, 0) + 1

    def name_of(r: dict) -> str:
        return r["task_group"] if isinstance(r.get("task_group"), str) and r["task_group"] else UNTAGGED

    inside = []
    for r in rows:
        if not lane_a_shadow.is_lane_a(r):
            continue
        ts = _ts(r.get("ts"))
        if ts is None:
            continue
        if flip is not None and ts < flip:
            exclude(name_of(r), "before_flip")
            continue
        if not since <= ts <= until:
            continue
        inside.append(r)
    kept, dropped = lane_a_shadow.dedupe_steps(inside)
    for r in dropped:
        exclude(name_of(r), "duplicate_step")
    for r in kept:
        name = name_of(r)
        if name == UNTAGGED or r.get("rate_phase") not in (lane_a_shadow.PHASE_FILL, lane_a_shadow.PHASE_STEADY):
            exclude(name, "missing_tags")
            continue
        g = group(name)
        if r.get("judge_model") != JUDGE_MODEL or r.get("judge_brief_sha256") != brief_sha:
            exclude(name, "judge_mismatch")
            continue
        served, replayed = r.get("served_body_sha"), r.get("replay_body_sha")
        if not (isinstance(served, str) and served and served == replayed):
            exclude(name, "body_sha_mismatch")  # prereg section 3: a mismatch (or an unverifiable pair) voids the pair
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


def batch_row(task_group: str, counts: dict, *, now: float, since: float, until: float,
              flip: float | None = None, identity: dict | None = None) -> tuple[dict | None, str]:
    """``(row, status)`` for one task group, or ``(None, "n_below_min")`` while nothing is due. D-301 / D-302:

    * n >= 60 and cannot_judge share <= 20%: the FINAL informative row (``n_reached``);
    * n >= 60 and share > 20% before day 28: a NON-final diagnostic row (``window_complete: false``), status
      ``diagnostic``: the window stays open;
    * 28 days after the owner flip (``until`` has reached ``flip + 28 d``): the FINAL row, NOT INFORMATIVE when
      n < 60 (``n_below_min``) or the share is above 20% (``cannot_judge_share``).

    Only a final row is counted by S1, and a NOT INFORMATIVE one earns no credit. Every row is stamped with the four
    ``identity`` values (default: the identities the proxy recorded, D-303) that D-302 ties a final row's validity
    to. When those are unknown there is no row: ``(None, "identity_unknown")``, so nothing is written and the window
    is not closed."""
    from llm_router import eligibility

    n, cj = counts["n"], counts["cannot_judge"]
    attempts = n + cj
    share = (cj / attempts) if attempts else 0.0
    too_many_cj = share > lane_a_shadow.CANNOT_JUDGE_MAX_SHARE
    past_28d = flip is not None and until >= flip + lane_a_shadow.FINAL_WINDOW_DAYS * 86400.0
    final = True
    if n >= MIN_PAIRS and not too_many_cj:
        close, why = CLOSE_PAIRS, None
    elif past_28d:
        close, why = CLOSE_TIME, ("n_below_min" if n < MIN_PAIRS else "cannot_judge_share")
    elif n >= MIN_PAIRS:  # over 20% cannot_judge, window still open: diagnostic only
        close, why, final = None, "cannot_judge_share", False
    else:
        return None, "n_below_min"
    informative = why is None
    identity = identity if identity is not None else lane_a_shadow.current_identities()
    if identity is None:
        return None, "identity_unknown"
    # The source names the evidence, not the window it was found in.
    digest = hashlib.sha256(
        ("\n".join(sorted(counts["ids"])) + f"\n{counts['k_a']}/{counts['k_f']}").encode("utf-8")).hexdigest()[:16]
    row = {"ts": round(now, 3), "door": lane_a_shadow.DOOR, "served_class": lane_a_shadow.SERVED_CLASS,
           "task_group": task_group, "kind": "shadow_pairs", "n": n, "gap_pts": None, "newcombe_lb": None,
           "source": f"lane_a_pairs:{task_group}:n={n}:{digest}", "lane": lane_a_shadow.LANE_TAG,
           "k_laneA": counts["k_a"], "k_frontier": counts["k_f"], "cannot_judge": cj,
           "cannot_judge_share": round(share, 4), "excluded": dict(sorted(counts.get("excluded", {}).items())),
           "window_complete": final, "informative": informative, "close_reason": close, "not_informative_reason": why,
           "identity": identity,
           "flip_ts": flip, "since": since, "until": until}
    if not final:
        row["diagnostic"] = True
        return row, "diagnostic"
    if informative:
        ev = eligibility.evidence_from_counts(counts["k_a"], n, counts["k_f"], n)
        row.update(n=ev.n, gap_pts=ev.gap_pts, newcombe_lb=ev.lb_pts)
    return row, "written" if informative else "not_informative"


def closed_groups(rows: Iterable[dict]) -> set[str]:
    """Task groups of lane A whose window is complete: a FINAL ``shadow_pairs`` row exists. Nothing reopens one
    (D-301, D-302): only a preregistration amendment can."""
    return lane_a_shadow.final_groups(rows)


def write_all(*, now: float | None = None, since: float | None = None, until: float | None = None,
              dry_run: bool = False, verdicts: Path | None = None, out: Path | None = None) -> dict:
    """Write the FINAL row of each task group whose window closes now (see ``batch_row``). Returns a summary
    ``{"groups": {task_group: {...counts, "status", "grade"}}, "written": int}``. Creates no file when there is no
    lane A verdict row (except the NOT INFORMATIVE rows of the proxy's known task groups at flip + 28 d), none under
    ``dry_run``, and no row (status ``identity_unknown``) while the proxy has recorded no identity (D-303). The window start is the owner flip (``lane_a_shadow.read_flip``)
    when recorded, else the trailing 14 days; an explicit ``since`` overrides it. The window END is always clamped to
    ``flip + 28 d`` (D-302): a pair after day 28 never counts."""
    now = time.time() if now is None else now
    until = now if until is None else until
    flip = lane_a_shadow.read_flip()
    if flip is not None:
        until = min(until, flip + lane_a_shadow.FINAL_WINDOW_DAYS * 86400.0)
    if since is None:
        since = flip if flip is not None else until - lane_a_shadow.WINDOW_DAYS * 86400.0
    rows = _read_jsonl(verdicts or _verdicts_path())
    groups = tally(rows, since=since, until=until, flip=flip)
    identity_row = lane_a_shadow.read_identity_row()
    if flip is not None and identity_row is not None and until >= flip + lane_a_shadow.FINAL_WINDOW_DAYS * 86400.0:
        # NIT-2: at flip + 28 d a task group the proxy could serve but that has no lane A row still closes
        # NOT INFORMATIVE (n = 0). The groups come from the proxy's recorded identity; with none recorded the
        # group set is unknown and a group with no row gets no row.
        for g in identity_row.get("task_groups") or ():
            if isinstance(g, str) and g:
                groups.setdefault(g, {"n": 0, "k_a": 0, "k_f": 0, "cannot_judge": 0, "excluded": {}, "ids": []})
    target = out or paths.state_path(lane_quality.FILE)
    existing = lane_quality.read_rows(target) if groups else []
    closed = closed_groups(existing)
    summary: dict = {"groups": {}, "written": 0, "diagnostic": 0, "since": since, "until": until, "flip_ts": flip}
    from llm_router import eligibility

    identity = identity_row["identity"] if identity_row is not None else None

    for name, counts in sorted(groups.items()):
        info = {k: counts[k] for k in ("n", "k_a", "k_f", "cannot_judge", "excluded")}
        summary["groups"][name] = info
        if name == UNTAGGED:
            info["status"] = "untagged"
            continue
        if name in closed:
            info["status"] = "window_closed"  # complete: later batches neither count nor reopen it
            continue
        row, status = batch_row(name, counts, now=now, since=since, until=until, flip=flip, identity=identity)
        if row is None:
            info["status"] = status
            continue
        if row.get("diagnostic"):
            info["status"] = "diagnostic"  # n >= 60 but over 20% cannot_judge: the window stays open (D-302)
            info["cannot_judge_share"] = row["cannot_judge_share"]
            if dry_run or _same_diagnostic(existing, name, row):
                continue
            with state_files.open_private(target, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            summary["diagnostic"] += 1
            continue
        if row["informative"]:
            info["grade"] = eligibility.grade({"n": row["n"], "gap_pts": row["gap_pts"], "lb_pts": row["newcombe_lb"]})
            info.update(gap_pts=row["gap_pts"], newcombe_lb=row["newcombe_lb"])
        info["close_reason"] = row["close_reason"]
        if dry_run:
            info["status"] = "would_write" if row["informative"] else "would_write_not_informative"
            continue
        with state_files.open_private(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        info["status"] = status
        summary["written"] += 1
    return summary


def _same_diagnostic(existing: list[dict], task_group: str, row: dict) -> bool:
    """The latest diagnostic row of the group already says the same (same n and cannot_judge): do not repeat it."""
    last = [r for r in existing if r.get("kind") == "shadow_pairs" and r.get("diagnostic") is True
            and r.get("task_group") == task_group and r.get("door") == row["door"]]
    return bool(last) and (last[-1].get("n"), last[-1].get("cannot_judge")) == (row["n"], row["cannot_judge"])


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
