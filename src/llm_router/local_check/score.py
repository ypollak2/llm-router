"""local-check scoring, gates and verdicts (LC-5). Ported from PREREG-Q37 ``score_q37.py``; pure, no model call, no I/O.

Per item: success = programmatic grader ok AND judge pass. A programmatically failed answer is never judged (the conjunction is already false).
The pooled k of n over commit_message + pr_description decides (D-241 bar: point >= 0.80 and Wilson LB >= 0.60, ``stats.kmin``):

  PASS          k >= kmin(n)
  FAIL          k <= kfail(n)  (Wilson UB < 0.80: a powered failure)
  INCONCLUSIVE  anything between (and a PASS verdict is never produced by a programmatic-only run)
  NOT_PASSED    the run stopped early because failures made kmin unreachable; partial counts, NOT a powered fail
  PROBE         ``--no-judge`` and not FAIL: programmatic rate only, cannot pass
  INVALID       a validity gate failed (G0, G1, G3a, G3b, G5); nothing is recorded

Lane guard: a lane whose own rate is < 0.70 is excluded from a PASS (it is recorded as not passed with its own counts).
Gate G1 counts every timeout / error / empty answer ONCE per row (the Q36 lesson: counting the three separately double-counted one failure).
Gates G2 (Sonnet floor), G3c (spread) and G4 (human-style audit) of Q37 moved to bank release time (design section 4).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from llm_router.local_check import grader, stats
from llm_router.local_check.judge import served_model_matches

BAR_POINT = 0.80
BAR_LB = 0.60
LANE_GUARD_MIN = 0.70            # a lane below this own rate is excluded from the PASS action
G1_MIN_VALID = 0.85
G1_MAX_BAD_PER_70 = 3            # timeouts + errors + empties, each row once: at most 3 per 70 items
MIN_N = 50                       # no PASS / FAIL below this many cumulative assigned items (scope too narrow)
MIN_LANE_N = 20                  # nor when a recorded lane has fewer scored items than this
JUDGE_ERROR_TOLERANCE = 0.05     # answers still unjudged after the re-judge passes: at most max(1, floor(5% of judged calls)), excluded from n
CONCENTRATION_MIN = 0.40         # one grader rule explains at least this share of failures -> record it

PASS, FAIL, INCONCLUSIVE, INVALID, NOT_PASSED, PROBE = "PASS", "FAIL", "INCONCLUSIVE", "INVALID", "NOT_PASSED", "PROBE"
JUDGE, NO_JUDGE = "judge", "none"


@dataclass(frozen=True)
class Row:
    """One graded item. Holds no prompt or answer text."""
    item_id: str
    job: str
    size: str
    lang: str
    present: bool                  # the local model produced a record for this item
    valid: bool                    # a non-empty answer
    error: bool                    # runtime error or timeout
    empty: bool
    prog: bool                     # programmatic grader ok (valid, no rule violated, not truncated)
    judge: str | None              # pass | fail | judge_error | None (not judged)
    ok: bool                       # prog AND judge == "pass" (programmatic only when judge mode is "none")
    reasons: tuple[str, ...]       # grader rule names that failed

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "Row":
        return Row(str(d["item_id"]), str(d["job"]), str(d["size"]), str(d["lang"]), bool(d["present"]), bool(d["valid"]), bool(d["error"]),
                   bool(d["empty"]), bool(d["prog"]), d.get("judge"), bool(d["ok"]), tuple(d.get("reasons", ())))


def grader_item(item: Any) -> dict[str, Any]:
    """The mapping ``grader.grade_text`` expects, from a bank Item (attributes job, diff, files, task)."""
    return {"stratum": item.job, "diff": item.diff, "files": list(item.files), "messages": [{"role": "user", "content": item.task}]}


def prog_ok(item: Any, rec: Mapping[str, Any] | None) -> bool:
    """Programmatic outcome of one runner record (used by the runner's early-stop failure count and by ``grade_row``)."""
    return grade_row(item, rec, None, judge_mode=NO_JUDGE).prog


def grade_row(item: Any, rec: Mapping[str, Any] | None, judge: str | None, *, judge_mode: str = JUDGE) -> Row:
    """Grade one item. ``rec`` is the runner record (status, text, finish_reason, ...) or None when the item has no record."""
    if rec is None:
        return Row(item.id, item.job, item.size, item.lang, False, False, False, False, False, None, False, ("missing",))
    status = rec.get("status")
    g = grader.grade_text(grader_item(item), rec.get("text") or "")
    reasons = list(g["reasons"])
    if status == "truncated" or rec.get("finish_reason") == "length":
        reasons.append("truncated")
    reasons = list(dict.fromkeys(reasons))
    prog = bool(g["valid"] and not reasons)
    error = status in ("timeout", "error") or bool(rec.get("error"))
    ok = prog if judge_mode == NO_JUDGE else bool(prog and judge == "pass")
    return Row(item.id, item.job, item.size, item.lang, True, bool(g["valid"]), error, not g["valid"], prog, judge, ok, tuple(reasons))


def rate(rows: Iterable[Row], f=lambda r: True) -> dict[str, Any]:
    sel = [r for r in rows if f(r)]
    k = sum(1 for r in sel if r.ok)
    p, lo, hi = stats.wilson(k, len(sel))
    return {"n": len(sel), "k": k, "rate": p, "lb": lo, "ub": hi}


def rule_concentration(rows: Iterable[Row]) -> dict[str, Any] | None:
    """If ONE grader rule is among the reasons of >= 40% of the failed items, report it (rule, share, failures); else None."""
    failed = [r for r in rows if r.present and not r.ok]
    if not failed:
        return None
    counts: dict[str, int] = {}
    for r in failed:
        for rule in set(r.reasons):
            counts[rule] = counts.get(rule, 0) + 1
    if not counts:
        return None
    rule, c = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    share = c / len(failed)
    return {"rule": rule, "share": round(share, 4), "failures": len(failed), "with_rule": c} if share >= CONCENTRATION_MIN else None


@dataclass
class GateInputs:
    """Evidence the gates read. ``None`` means the gate was not measured, which cannot pass."""
    bank_sha_ok: bool | None = None            # the bank re-hashed to its manifest sha256 (loader) and to the sha the run started with
    judge_pinned: str | None = None            # manifest judge_id
    judge_served: Sequence[str] = ()           # served model ids seen on every judge call of this check (plants + answers)
    digest_start: str | None = None
    digest_end: str | None = None
    g3a: Mapping[str, Any] | None = None       # grader_cases.run() result, run before any model call
    g3b: Any = None                            # judge.G3bResult (or anything with .status and .reasons); judge mode only


@dataclass
class Result:
    verdict: str
    why: str
    mode: str
    n_assigned: int                    # items the verdict is about (cumulative over draws)
    n_scored: int                      # items with a record (n_assigned unless the run stopped early)
    k: int
    kmin: int | None
    kfail: int | None
    pooled: dict[str, Any]
    lanes: dict[str, dict[str, Any]]
    guard_excluded: list[str]
    gates: dict[str, dict[str, Any]]
    rule_concentration: dict[str, Any] | None
    incomplete: bool
    k_prog: int = 0
    excluded_unjudged: int = 0         # answers the judge could not rate (within the 5% tolerance): excluded from n

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gate(status: bool | None, **detail: Any) -> dict[str, Any]:
    return {"status": "NOT_MEASURED" if status is None else ("PASS" if status else "FAIL"), **detail}


def judge_error_tolerance(rows: Iterable[Row]) -> int:
    """How many answers may stay unjudged (judge_error): max(1, floor(5% of the judge calls = programmatically ok answers))."""
    judged = sum(1 for r in rows if r.prog)
    return max(1, int(judged * JUDGE_ERROR_TOLERANCE))


def gates(rows: Sequence[Row], assigned: Sequence[str], abandoned: Sequence[str], early_stop: bool, g: GateInputs, mode: str, k_min_reach: bool) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    # G0 integrity: bank hash, judge id as pinned, model digest unchanged start -> score.
    # Every served id (a comma list from the CLI route counts each model) must match the pin; the dated-snapshot rule is judge.served_model_matches.
    served = sorted(set(g.judge_served))
    judge_ok = True if mode == NO_JUDGE else bool(served) and g.judge_pinned is not None and all(served_model_matches(x, g.judge_pinned)[0] for x in served)
    digest_ok = g.digest_start == g.digest_end
    g0_ok = None if g.bank_sha_ok is None else bool(g.bank_sha_ok and judge_ok and digest_ok)
    out["G0_integrity"] = _gate(g0_ok, bank_sha_ok=g.bank_sha_ok, judge_pinned=g.judge_pinned, judge_served=served, judge_ok=judge_ok,
                                digest_unchanged=digest_ok)
    # G1 serves: valid >= 85%, timeouts/errors/empties <= 3 per 70, each row counted once.
    n = len(rows)
    valid = sum(1 for r in rows if r.valid)
    bad = sum(1 for r in rows if r.error or r.empty)
    out["G1_serves"] = _gate(bool(n and valid / n >= G1_MIN_VALID and bad * 70 <= G1_MAX_BAD_PER_70 * n), n=n, valid=valid,
                             valid_frac=(valid / n if n else 0.0), error_empty_timeout=bad)
    # G3a grader self-test (103 cases), run before any model call.
    a = g.g3a
    out["G3a_grader_selftest"] = _gate(None if a is None else bool(isinstance(a.get("n"), int) and a["n"] > 0 and not a.get("unexpected")),
                                       n=None if a is None else a.get("n"), unexpected=None if a is None else len(a.get("unexpected", ())))
    # G3b plants under the live judge (judge mode only).
    if mode == NO_JUDGE:
        out["G3b_plants"] = {"status": "N/A", "reason": "no judge in this run"}
    else:
        b = g.g3b
        out["G3b_plants"] = _gate(None if b is None else b.status == "PASS", reasons=None if b is None else list(getattr(b, "reasons", ())))
    # G5 completeness: every assigned item has a record (or was left unrun by the early stop / abandoned by an earlier early stop),
    # and every programmatically ok answer has a judge verdict.
    have = {r.item_id for r in rows if r.present}
    unrun = [i for i in assigned if i not in have and i not in set(abandoned)]
    missing_items = [] if (early_stop and k_min_reach) else unrun
    # A judge_error (after the in-call retry and the re-judge passes) is tolerated up to 5% of judged calls and excluded from n by ``score``;
    # an answer that was never judged at all is never tolerated.
    unjudged = [r.item_id for r in rows if mode == JUDGE and r.prog and r.judge not in ("pass", "fail", "judge_error")]
    errored = [r.item_id for r in rows if mode == JUDGE and r.prog and r.judge == "judge_error"]
    tol = judge_error_tolerance(rows) if mode == JUDGE else 0
    out["G5_complete"] = _gate(not missing_items and not unjudged and len(errored) <= tol, missing_items=len(missing_items), unjudged=len(unjudged),
                               judge_errors=len(errored), judge_error_tolerance=tol,
                               unrun_by_early_stop=len(unrun) if (early_stop and k_min_reach) else 0)
    return out


def score(rows: Sequence[Row], assigned: Sequence[str], g: GateInputs, *, mode: str = JUDGE, early_stop: bool = False, abandoned: Sequence[str] = ()) -> Result:
    """Verdict over ``rows`` (graded items of the cumulative union). ``assigned`` are the item ids the verdict is about.
    ``abandoned`` are assigned items an EARLIER early-stopped attempt never ran: they count as not passed.
    ``early_stop`` says the latest run stopped before running every item; it is believed only if the failures really made kmin unreachable."""
    if mode not in (JUDGE, NO_JUDGE):
        raise ValueError("mode must be 'judge' or 'none'")
    # Judge errors within the tolerance are EXCLUDED from n (not counted as passes or fails); beyond it G5 fails below.
    errored = {r.item_id for r in rows if r.present and r.prog and r.judge == "judge_error"} if mode == JUDGE else set()
    excluded = errored if 0 < len(errored) <= judge_error_tolerance([r for r in rows if r.present]) else set()
    if excluded:
        rows = [r for r in rows if r.item_id not in excluded]
        assigned = [i for i in assigned if i not in excluded]
    n = len(assigned)
    kmin = stats.kmin(n, BAR_POINT, BAR_LB) if n else None
    kfail = stats.kfail(n, BAR_POINT) if n else None
    present = [r for r in rows if r.present]
    k = sum(1 for r in present if r.ok)
    failures = sum(1 for r in present if not r.ok) + len(set(abandoned))
    unreachable = kmin is not None and n - failures < kmin
    have = {r.item_id for r in present} | set(abandoned)
    incomplete = any(i not in have for i in assigned)
    gt = gates(present, assigned, abandoned, early_stop, g, mode, unreachable)
    narrow = ""
    if n < MIN_N:
        narrow = f"scope too narrow (n={n} < {MIN_N})"
    else:
        small = {j: sum(1 for r in present if r.job == j) for j in sorted({r.job for r in present})}
        small = {j: c for j, c in small.items() if c < MIN_LANE_N}
        if small:
            narrow = "scope too narrow (lane " + ", ".join(f"{j} n={c}" for j, c in small.items()) + f" < {MIN_LANE_N})"
    pooled = rate(present)
    lanes = {job: rate(present, lambda r, job=job: r.job == job) for job in sorted({r.job for r in present})}
    conc = rule_concentration(present)
    base = dict(mode=mode, n_assigned=n, n_scored=len(present), k=k, kmin=kmin, kfail=kfail, pooled=pooled, lanes=lanes, gates=gt,
                rule_concentration=conc, incomplete=incomplete, k_prog=sum(1 for r in present if r.prog), excluded_unjudged=len(excluded))
    failed_gates = [name for name, v in gt.items() if v["status"] in ("FAIL", "NOT_MEASURED")]
    if failed_gates:
        return Result(INVALID, "validity gate(s) failed or not measured: " + ", ".join(failed_gates), guard_excluded=[], **base)
    if incomplete:
        # Only reachable here with early_stop and unreachable kmin (otherwise G5 failed above).
        return Result(NOT_PASSED, f"stopped early: k={k} of {len(present)} scored; kmin={kmin} of {n} is unreachable after {failures} failures (not a powered fail)",
                      guard_excluded=[], **base)
    if mode == NO_JUDGE:
        if kfail is not None and k <= kfail:
            if narrow:
                return Result(INCONCLUSIVE, narrow, guard_excluded=[], **base)
            return Result(FAIL, f"programmatic rate {k}/{n} <= kfail {kfail} (Wilson UB < 0.80 even before the judge)", guard_excluded=[], **base)
        return Result(PROBE, f"programmatic only: {k}/{n}; a judge-less run cannot pass", guard_excluded=[], **base)
    if narrow:
        # Never a PASS or a powered FAIL on a tiny scope; nothing is recorded.
        return Result(INCONCLUSIVE, narrow, guard_excluded=[], **base)
    if kmin is not None and k >= kmin:
        excluded = [j for j, v in lanes.items() if v["rate"] < LANE_GUARD_MIN]
        return Result(PASS, f"k={k}/{n} >= kmin {kmin}" + (f"; lane guard excludes {', '.join(excluded)}" if excluded else ""), guard_excluded=excluded, **base)
    if kfail is not None and k <= kfail:
        return Result(FAIL, f"k={k}/{n} <= kfail {kfail}: Wilson UB < 0.80", guard_excluded=[], **base)
    return Result(INCONCLUSIVE, f"k={k}/{n}; kmin={kmin}, kfail={kfail}", guard_excluded=[], **base)


def redact_invalid(res: Result) -> Result:
    """An INVALID outcome shows the gates only: no k, no lane or pooled rates (a rerun must not be steerable by what an invalid run revealed)."""
    if res.verdict != INVALID:
        return res
    return replace(res, k=0, kmin=None, kfail=None, pooled={}, lanes={}, rule_concentration=None, k_prog=0, n_scored=0, excluded_unjudged=0,
                   incomplete=False)


def record_plan(res: Result, *, cumulative: bool = False) -> dict[str, dict[str, Any]]:
    """The ``jobs_result`` for ``eligibility_store.record_local_check``; empty when nothing is to be recorded.

    Counts are k over ``n_assigned`` (items an earlier early-stopped attempt abandoned count as not passed), never over the scored rows only.
    PASS: every lane that clears the guard gets the pooled counts; an excluded lane gets its OWN counts as FAIL (so an older pass for it is replaced).
    FAIL: every lane gets the pooled counts. NOT_PASSED: the first attempt records the partial counts it has (k of the items scored); a
    cumulative one (``cumulative``: an earlier attempt exists) records k over n_assigned. INCONCLUSIVE writes nothing: a PASS recorded earlier for
    the same configuration is kept and the rerun is only logged (D-263). INVALID and PROBE record nothing.
    A ``--no-judge`` run (mode != JUDGE) records nothing at all (LC-10k): it must never replace or downgrade a judged verdict, so it lives in the attempt log only."""
    if res.mode != JUDGE:
        return {}
    jobs = sorted(res.lanes)
    extra = {"rule_concentration": res.rule_concentration} if res.rule_concentration else {}
    pooled = {"k": res.k, "n": res.n_assigned}
    if res.verdict == PASS:
        out: dict[str, dict[str, Any]] = {}
        for j in jobs:
            if j in res.guard_excluded:
                out[j] = {"k": res.lanes[j]["k"], "n": res.lanes[j]["n"], "verdict": FAIL, "status": "lane_guard", **extra}
            else:
                out[j] = {**pooled, "verdict": PASS, **extra}
        return out
    if res.verdict == FAIL:
        return {j: {**pooled, "verdict": FAIL, **extra} for j in jobs}
    if res.verdict == NOT_PASSED:
        counts = pooled if cumulative else {"k": res.pooled["k"], "n": res.pooled["n"]}
        return {j: {**counts, "verdict": NOT_PASSED, **extra} for j in jobs}
    return {}
