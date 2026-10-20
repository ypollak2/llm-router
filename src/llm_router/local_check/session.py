"""One full local-check (LC-5): anti-retry plan -> G3a -> G3b -> serial local run -> judge -> score -> attempt log -> record.

Library only (the CLI is LC-6). Everything external is injected: the runtime ``adapter`` (LC-3), the judge ``backend`` and ``budget`` (LC-4),
the clock, the run/attempt directory and the eligibility path. Consumes the public APIs of ``runner``, ``judge``, ``budget`` and ``bank``
without modifying them.

Nothing is recorded unless the verdict is PASS / FAIL / NOT_PASSED. INVALID, INCONCLUSIVE (an earlier recorded PASS is kept; the rerun is only
logged), PROBE, a refusal, a budget stop and an interrupted run write nothing to the eligibility store. A run that stops after a local item ran
without a verdict leaves a reservation (see ``attempts``): the next run of the configuration resumes it. Floor: PASS/FAIL need n >= 50 and
>= 20 items per lane, and a draw whose context-fit count is below that is refused before the plant gate and the local arm.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from llm_router.local_check import attempts as A
from llm_router.local_check import grader, grader_cases, judge as J, runner as R, score as S, stats
from llm_router.local_check.bank import Bank, load_bank
from llm_router.local_check.budget import Budget, BudgetExhausted
from llm_router.resolver import eligibility_store


@dataclass
class Check:
    model: str
    runtime: str
    bank: Bank
    adapter: Any                                   # LC-3 adapter: generate / digest / settings_hash / max_prompt_chars
    runs_root: Path
    backend: Any = None                            # LC-4 judge backend; None only with no_judge
    budget: Budget | None = None
    no_judge: bool = False
    force: bool = False
    fresh: bool = False                            # discard an open reservation of this configuration and start a new run (counts as a strike)
    run_id: str | None = None                      # pass an existing id to resume that run
    now: Callable[[], float] = time.time
    eligibility_path: Path | None = None
    lock_path: Path | None = None
    bank_loader: Callable[[], Bank] = load_bank    # re-verifies the bank hash at scoring time (G0)
    probe_n: int = R.PROBE_N
    base_timeout_s: float = R.BASE_TIMEOUT_S


@dataclass
class Outcome:
    status: str                                    # scored | refused | record_refused | budget_stop | interrupted | lock_held
    reason: str = ""
    result: S.Result | None = None
    draw: int | None = None
    run_id: str | None = None
    record_report: dict[str, dict[str, str]] = field(default_factory=dict)
    attempts: int = 0
    notes: list[str] = field(default_factory=list)   # things the caller should tell the user (e.g. a discarded reservation)


def _append_line(path: Path, rec: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as f:
        f.write(json.dumps(rec, sort_keys=True).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)


def _load_g3b(run_dir: Path) -> J.G3bResult | None:
    """The plants gate of this run id, if it already PASSED (a resume does not pay for ~30 plant calls again)."""
    try:
        d = json.loads((run_dir / "g3b.json").read_text())
        d["rows"] = [J.PlantRow(**r) for r in d.get("rows", [])]
        r = J.G3bResult(**d)
    except (OSError, ValueError, TypeError):
        return None
    return r if r.status == "PASS" else None


def _save_g3b(run_dir: Path, g3b: J.G3bResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(run_dir / "g3b.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(asdict(g3b), sort_keys=True))


def _load_judgements(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and "item_id" in r and r.get("verdict") in ("pass", "fail"):      # a judge_error is never cached
            out[r["item_id"]] = r
    return out


def _plant_dicts(bank: Bank) -> list[dict]:
    return [{"id": p.id, "kind": p.kind, "text": p.text, "messages": [{"role": "user", "content": p.task}]} for p in bank.plants()]


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _run_has_results(run_dir: Path) -> bool:
    """True when the run directory holds at least one local result (an untrustworthy dir counts as present: the runner reports it)."""
    try:
        return bool(R.RunStore(run_dir).load())
    except R.RunMismatch:
        return True
    except OSError:
        return False


def _hold(log: A.AttemptLog, base: dict, plan: A.Plan, *, reason: str, strike: bool, state: str = "open") -> bool:
    """A judged run stopped after a local item ran without a verdict: log the run dir as a reservation (``open``) so the next run of this
    configuration and draw resumes it. An INVALID (``strike``) is free for the first FREE_STRIKES per configuration; later ones also burn the
    draw (consumed, no items). Returns True when the draw was burnt."""
    strikes = plan.strikes + (1 if strike else 0)
    burn = strike and strikes > A.FREE_STRIKES
    log.append({**base, "state": "burnt" if burn else state, "strike": strike, "consumed": burn, "reason": reason,
                "rows": [], "assigned": [], "unrun": [], "k": None, "judge_served": []})
    return burn


def run_check(c: Check) -> Outcome:
    notes: list[str] = []
    out = _run_check(c, notes)
    out.notes = notes
    return out


def _run_check(c: Check, notes: list[str]) -> Outcome:
    mode = S.NO_JUDGE if c.no_judge else S.JUDGE
    if mode == S.JUDGE and (c.backend is None or c.budget is None):
        raise ValueError("a judged check needs a judge backend and a budget (use no_judge=True for a programmatic-only probe)")
    bank = c.bank
    ts = c.now()
    digest0 = c.adapter.digest(c.model)
    settings = c.adapter.settings_hash(c.model)
    max_chars = c.adapter.max_prompt_chars(c.model)
    key = A.config_key(c.model, digest0, bank.version, bank.sha256, settings)
    log = A.AttemptLog(c.runs_root)
    config = {"model": c.model, "digest": digest0, "bank_version": bank.version, "bank_sha256": bank.sha256, "settings_hash": settings, "runtime": c.runtime}

    # 1. Anti-retry plan.
    # A --no-judge probe cannot pass, consumes no draw and never joins the union: it is outside the anti-retry rules (always draw 1).
    plan = A.Plan(1) if c.no_judge else log.plan(key, ts, force=c.force)
    if plan.draw is None:
        return Outcome("refused", plan.refused or "")
    resv = plan.reservation
    fresh_suffix = ""
    discard_reason = None
    if resv and c.fresh:
        discard_reason = "discarded by --fresh"
    elif resv and not _run_has_results(Path(c.runs_root) / resv["run_id"]):
        # The reservation is keyed on the run id only: deleting its directory and rerunning would be a free --fresh. A missing or empty run
        # directory of an open reservation is therefore a discard, and a strike.
        discard_reason = "the run directory of the open reservation is missing or empty"
    if discard_reason:
        # Discarding a reservation is a strike; the third and later also burn the draw.
        burn = _hold(log, {"config_key": key, "config": config, "draw": plan.draw, "run_id": resv["run_id"], "ts": ts, "date": _date(ts), "mode": mode,
                           "verdict": "DISCARDED"}, plan, reason=discard_reason, strike=True, state="discarded")
        notes.append(f"reservation {resv['run_id']} discarded ({discard_reason}): a strike" + ("; the draw is burnt" if burn else ""))
        fresh_suffix = f"-f{plan.strikes + 1}"
        resv = None
        if burn:
            plan = log.plan(key, ts, force=c.force)
            if plan.draw is None:
                return Outcome("refused", plan.refused or "")
        else:
            plan.strikes += 1
    draw = plan.draw
    if resv and c.run_id and c.run_id != resv["run_id"]:
        return Outcome("refused", f"run {resv['run_id']} is open for this configuration (the local arm already ran): resume it, or discard it with fresh (a strike)",
                       None, draw, resv["run_id"])
    run_id = resv["run_id"] if resv else (c.run_id or f"{_date(ts)}-{key[:8]}-d{draw}-{int(ts)}{fresh_suffix}")
    run_dir = Path(c.runs_root) / run_id
    base_log = {"config_key": key, "config": config, "draw": draw, "run_id": run_id, "ts": ts, "date": _date(ts), "mode": mode}
    g = S.GateInputs(judge_pinned=bank.manifest.get("judge_id"), digest_start=digest0)

    # 2. G3a: the grader's 103 self-tests, before any model call.
    g.g3a = grader_cases.run()
    if g.g3a.get("unexpected"):
        res = S.redact_invalid(S.score([], [], g, mode=mode))
        return Outcome("scored", "grader self-test failed", res, draw, None)

    # 3. Items of this draw that fit the model's context; the scope is recorded with the lane.
    items = bank.draw(draw)
    n_drawn = len(items)
    fit_ids = {i.id for i in items if max_chars is None or len(i.task) <= max_chars}
    items = [i for i in items if i.id in fit_ids]
    by_id = {i.id: i for i in items}
    assigned_now = [i.id for i in items]
    assigned_all = A.union_ids(plan.prior_assigned, assigned_now)
    prior_rows = [S.Row.from_dict(d) for d in plan.prior_rows]
    if mode == S.JUDGE:
        # Floor, before the plant gate and the local arm spend anything: a PASS/FAIL needs n >= 50 and >= 20 items per lane.
        per_lane = Counter(i.job for i in items) + Counter(r.job for r in prior_rows if r.present)
        thin = [j for j in sorted({i.job for i in bank.draw(1)}) if per_lane[j] < S.MIN_LANE_N]
        if len(assigned_all) < S.MIN_N or thin:
            return Outcome("refused", f"scope too narrow: {len(assigned_all)} items fit the model's context (need >= {S.MIN_N}, and >= {S.MIN_LANE_N} per lane"
                           + (f"; thin: {', '.join(thin)}" if thin else "") + "); raise num_ctx or use a longer-context model", None, draw, None)
    kmin_cum = stats.kmin(len(assigned_all), S.BAR_POINT, S.BAR_LB) if assigned_all else None
    min_pass = None if kmin_cum is None else kmin_cum - sum(1 for r in prior_rows if r.ok)
    if min_pass is not None and min_pass > len(items):
        # e.g. an earlier attempt stopped early: its unrun items count as not passed, so the cumulative bar can no longer be reached.
        return Outcome("refused", f"the cumulative bar (kmin {kmin_cum} of {len(assigned_all)}) can no longer be reached with the items left in this draw", None, draw, None)

    # 4. G3b: plants under the live judge, before the local arm (fail fast; a budget stop is resumable; a PASS is cached for this run id).
    served: list[str] = list(plan.prior_served)
    if mode == S.JUDGE:
        g3b = _load_g3b(run_dir)
        if g3b is None:
            g3b = J.run_g3b(c.backend, c.budget, _plant_dicts(bank))
            if g3b.status == "PASS":
                _save_g3b(run_dir, g3b)
        g.g3b = g3b
        served += [s for s in g3b.served_models if s not in served]
        if g3b.status == "INCONCLUSIVE":
            return Outcome("budget_stop", g3b.stop_reason, None, draw, None)
        if g3b.status != "PASS":
            g.judge_served = served
            g.digest_end = digest0
            g.bank_sha_ok = True
            res = S.redact_invalid(S.score([], [], g, mode=mode))
            return Outcome("scored", "plants gate failed", res, draw, None)

    holds = mode == S.JUDGE
    try:
        # 5. Serial local run, early stop when the bar is unreachable.
        try:
            rn = R.Runner(c.adapter, c.model, [R.SimpleItem(i.id, i.task) for i in items], run_dir, runtime=c.runtime, lock_path=c.lock_path,
                          base_timeout_s=c.base_timeout_s, probe_n=c.probe_n, min_pass=min_pass,
                          is_failure=lambda rec: not S.prog_ok(by_id[rec["item_id"]], rec))
            out = rn.run()
        except R.LockHeld as e:
            return Outcome("lock_held", str(e), None, draw, run_id)
        except (R.RunMismatch, R.RunDirError) as e:
            return Outcome("refused", f"cannot resume run {run_id}: {e}; discard it with fresh (a strike)", None, draw, run_id)
        if out.status == "interrupted":
            if holds and out.records:
                _hold(log, {**base_log, "verdict": "INTERRUPTED"}, plan, reason="interrupted", strike=False)
            return Outcome("interrupted", "interrupted; resume with the same run id", None, draw, run_id)
        recs = {r["item_id"]: r for r in out.records if r["item_id"] in by_id}
        digest1 = c.adapter.digest(c.model)

        # 6. Judge every programmatically ok answer (persisted, so a resume or a budget stop loses nothing). A judge_error is never cached: errored
        # answers are re-judged at the end of the run (up to 2 more passes); what stays unjudged is tolerated by ``score`` up to 5% (excluded from n).
        judged = _load_judgements(run_dir / "judge.jsonl")
        errored: set[str] = set()
        if mode == S.JUDGE:
            pending = [iid for iid, rec in recs.items() if iid not in judged and S.prog_ok(by_id[iid], rec)]
            for _pass in range(3):
                again: list[str] = []
                for iid in pending:
                    rec = recs[iid]
                    prompt = J.judge_prompt(by_id[iid].task, grader.strip_think(rec.get("text") or "").strip())
                    try:
                        o = J.judge_answer(c.backend, c.budget, prompt)
                    except BudgetExhausted as e:
                        _hold(log, {**base_log, "verdict": "BUDGET_STOP"}, plan, reason=e.reason, strike=False)
                        return Outcome("budget_stop", e.reason, None, draw, run_id)
                    if o.verdict == "judge_error":
                        again.append(iid)
                        continue
                    judged[iid] = {"item_id": iid, "verdict": o.verdict, "defect": o.defect, "served_model": o.served_model, "cost_usd": o.cost_usd}
                    _append_line(run_dir / "judge.jsonl", judged[iid])
                pending = again
                if not pending:
                    break
            errored = set(pending)
            served += [r["served_model"] for r in judged.values() if r.get("served_model") and r["served_model"] not in served]

        # 7. Score the cumulative union.
        try:
            reloaded = c.bank_loader()
            g.bank_sha_ok = reloaded.sha256 == bank.sha256
        except Exception:                                  # noqa: BLE001 - any load failure means the bank is not verified
            g.bank_sha_ok = False
        g.judge_served, g.digest_end = served, digest1
        rows_now = [S.grade_row(by_id[i], recs.get(i), "judge_error" if i in errored else (judged.get(i) or {}).get("verdict"), judge_mode=mode)
                    for i in assigned_now if i in recs]
        unrun = [i for i in assigned_now if i not in recs]
        res = S.score(prior_rows + rows_now, assigned_all, g, mode=mode, early_stop=out.status == "early_stop", abandoned=plan.prior_abandoned)

        # 8a. INVALID after the local arm: a strike and a reservation; the outcome shows the gates only (no k, no rates).
        if res.verdict == S.INVALID:
            res = S.redact_invalid(res)
            if holds:
                _hold(log, {**base_log, "verdict": S.INVALID}, plan, reason=res.why, strike=True)
            else:
                log.append({**base_log, "verdict": S.INVALID, "k": None, "n": res.n_assigned, "consumed": False, "recorded": False, "rows": [],
                            "assigned": assigned_now, "unrun": unrun, "judge_served": served})
            return Outcome("scored", res.why, res, draw, run_id, {}, len(plan.prior))

        # 8b. The record, then the attempt log.
        jobs = S.record_plan(res, cumulative=bool(plan.prior))
        report: dict[str, dict[str, str]] = {}
        max_n = len(plan.prior) + 1
        if jobs:
            provenance = {
                "bank_version": f"v{bank.version}", "bank_sha256": bank.sha256, "settings_hash": settings, "model_digest": digest0, "runtime": c.runtime,
                "judge": {"backend": getattr(c.backend, "name", None) if mode == S.JUDGE else "none",
                          "model_served": (", ".join(served) or None) if mode == S.JUDGE else None, "mode": mode},
                "run_id": run_id, "date": _date(ts),
                "scope": {"max_prompt_chars": max_chars, "items_excluded_by_context": n_drawn - len(items), "draws": sorted({e["draw"] for e in plan.prior} | {draw})},
                "attempts": max_n,
            }
            report = eligibility_store.record_local_check(c.model, jobs, provenance, path=c.eligibility_path)
        refused = {j: v for j, v in report.items() if v["status"] != "written"}
        if refused:
            # A store refusal is an error, not a "scored" result, and it consumes no draw: the run stays reserved and a rerun resumes it.
            why = "eligibility store refused: " + "; ".join(f"{j}: {v['reason']}" for j, v in sorted(refused.items()))
            if holds:
                _hold(log, {**base_log, "verdict": res.verdict}, plan, reason=why, strike=False)
            else:
                log.append({**base_log, "verdict": res.verdict, "k": res.k, "n": res.n_assigned, "consumed": False, "recorded": False, "rows": [],
                            "assigned": assigned_now, "unrun": unrun, "judge_served": served, "reason": why})
            return Outcome("record_refused", why, res, draw, run_id, report, len(plan.prior))
        consumed = mode == S.JUDGE and res.verdict in (S.PASS, S.FAIL, S.NOT_PASSED, S.INCONCLUSIVE)
        n_attempts = len(plan.prior) + (1 if consumed else 0)
        recorded = res.verdict == S.PASS and any(v["status"] == "written" for v in report.values())
        log.append({**base_log, "state": "scored", "verdict": res.verdict, "k": res.k, "n": res.n_assigned, "consumed": consumed, "recorded": recorded,
                    "rows": [r.to_dict() for r in rows_now], "assigned": assigned_now, "unrun": unrun, "judge_served": served,
                    "excluded_unjudged": res.excluded_unjudged})
        return Outcome("scored", res.why, res, draw, run_id, report, n_attempts)
    except BaseException as exc:
        # An unexpected exception (or a Ctrl-C outside the local loop) after the local arm ran leaves the run dir as a reservation, not a free reroll.
        if holds and _run_has_results(run_dir):
            with contextlib.suppress(Exception):
                _hold(log, {**base_log, "verdict": "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR"}, plan,
                      reason="interrupted" if isinstance(exc, KeyboardInterrupt) else f"unexpected {type(exc).__name__}", strike=False)
        raise
