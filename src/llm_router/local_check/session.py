"""One full local-check (LC-5): anti-retry plan -> G3a -> G3b -> serial local run -> judge -> score -> attempt log -> record.

Library only (the CLI is LC-6). Everything external is injected: the runtime ``adapter`` (LC-3), the judge ``backend`` and ``budget`` (LC-4),
the clock, the run/attempt directory and the eligibility path. Consumes the public APIs of ``runner``, ``judge``, ``budget`` and ``bank``
without modifying them.

Nothing is recorded unless the verdict is PASS / FAIL / NOT_PASSED (or an INCONCLUSIVE cumulative that supersedes an earlier recorded PASS of the
same configuration). INVALID, INCONCLUSIVE, PROBE, a refusal, a budget stop and an interrupted run write nothing to the eligibility store.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
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
    run_id: str | None = None                      # pass an existing id to resume that run
    now: Callable[[], float] = time.time
    eligibility_path: Path | None = None
    lock_path: Path | None = None
    bank_loader: Callable[[], Bank] = load_bank    # re-verifies the bank hash at scoring time (G0)
    probe_n: int = R.PROBE_N
    base_timeout_s: float = R.BASE_TIMEOUT_S


@dataclass
class Outcome:
    status: str                                    # scored | refused | budget_stop | interrupted | lock_held
    reason: str = ""
    result: S.Result | None = None
    draw: int | None = None
    run_id: str | None = None
    record_report: dict[str, dict[str, str]] = field(default_factory=dict)
    attempts: int = 0


def _append_line(path: Path, rec: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as f:
        f.write(json.dumps(rec, sort_keys=True).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)


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
        if isinstance(r, dict) and "item_id" in r:
            out[r["item_id"]] = r
    return out


def _plant_dicts(bank: Bank) -> list[dict]:
    return [{"id": p.id, "kind": p.kind, "text": p.text, "messages": [{"role": "user", "content": p.task}]} for p in bank.plants()]


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def run_check(c: Check) -> Outcome:
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

    # 1. Anti-retry plan.
    # A --no-judge probe cannot pass, consumes no draw and never joins the union: it is outside the anti-retry rules (always draw 1).
    plan = A.Plan(1) if c.no_judge else log.plan(key, ts, force=c.force)
    if plan.draw is None:
        return Outcome("refused", plan.refused or "")
    draw = plan.draw
    g = S.GateInputs(judge_pinned=bank.manifest.get("judge_id"), digest_start=digest0)

    # 2. G3a: the grader's 103 self-tests, before any model call.
    g.g3a = grader_cases.run()
    if g.g3a.get("unexpected"):
        res = S.score([], [], g, mode=mode)
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
    kmin_cum = stats.kmin(len(assigned_all), S.BAR_POINT, S.BAR_LB) if assigned_all else None
    min_pass = None if kmin_cum is None else kmin_cum - sum(1 for r in prior_rows if r.ok)
    if min_pass is not None and min_pass > len(items):
        # e.g. an earlier attempt stopped early: its unrun items count as not passed, so the cumulative bar can no longer be reached.
        return Outcome("refused", f"the cumulative bar (kmin {kmin_cum} of {len(assigned_all)}) can no longer be reached with the items left in this draw", None, draw, None)

    # 4. G3b: plants under the live judge, before the local arm (fail fast; a budget stop is resumable).
    served: list[str] = list(plan.prior_served)
    if mode == S.JUDGE:
        g3b = J.run_g3b(c.backend, c.budget, _plant_dicts(bank))
        g.g3b = g3b
        served += [s for s in g3b.served_models if s not in served]
        if g3b.status == "INCONCLUSIVE":
            return Outcome("budget_stop", g3b.stop_reason, None, draw, None)
        if g3b.status != "PASS":
            g.judge_served = served
            g.digest_end = digest0
            g.bank_sha_ok = True
            res = S.score([], [], g, mode=mode)
            return Outcome("scored", "plants gate failed", res, draw, None)

    # 5. Serial local run, early stop when the bar is unreachable.
    run_id = c.run_id or f"{_date(ts)}-{key[:8]}-d{draw}-{int(ts)}"
    run_dir = Path(c.runs_root) / run_id
    rn = R.Runner(c.adapter, c.model, [R.SimpleItem(i.id, i.task) for i in items], run_dir, runtime=c.runtime, lock_path=c.lock_path,
                  base_timeout_s=c.base_timeout_s, probe_n=c.probe_n, min_pass=min_pass,
                  is_failure=lambda rec: not S.prog_ok(by_id[rec["item_id"]], rec))
    try:
        out = rn.run()
    except R.LockHeld as e:
        return Outcome("lock_held", str(e), None, draw, run_id)
    if out.status == "interrupted":
        return Outcome("interrupted", "interrupted; resume with the same run id", None, draw, run_id)
    recs = {r["item_id"]: r for r in out.records if r["item_id"] in by_id}
    digest1 = c.adapter.digest(c.model)

    # 6. Judge every programmatically ok answer once (persisted, so a resume or a budget stop loses nothing).
    judged = _load_judgements(run_dir / "judge.jsonl")
    if mode == S.JUDGE:
        for iid, rec in recs.items():
            if iid in judged or not S.prog_ok(by_id[iid], rec):
                continue
            prompt = J.judge_prompt(by_id[iid].task, grader.strip_think(rec.get("text") or "").strip())
            try:
                o = J.judge_answer(c.backend, c.budget, prompt)
            except BudgetExhausted as e:
                return Outcome("budget_stop", e.reason, None, draw, run_id)
            judged[iid] = {"item_id": iid, "verdict": o.verdict, "defect": o.defect, "served_model": o.served_model, "cost_usd": o.cost_usd}
            _append_line(run_dir / "judge.jsonl", judged[iid])
        served += [r["served_model"] for r in judged.values() if r.get("served_model") and r["served_model"] not in served]

    # 7. Score the cumulative union.
    try:
        reloaded = c.bank_loader()
        g.bank_sha_ok = reloaded.sha256 == bank.sha256
    except Exception:                                  # noqa: BLE001 - any load failure means the bank is not verified
        g.bank_sha_ok = False
    g.judge_served, g.digest_end = served, digest1
    rows_now = [S.grade_row(by_id[i], recs.get(i), (judged.get(i) or {}).get("verdict"), judge_mode=mode) for i in assigned_now if i in recs]
    unrun = [i for i in assigned_now if i not in recs]
    res = S.score(prior_rows + rows_now, assigned_all, g, mode=mode, early_stop=out.status == "early_stop", abandoned=plan.prior_abandoned)

    # 8. Attempt log, then the record.
    consumed = mode == S.JUDGE and res.verdict in (S.PASS, S.FAIL, S.NOT_PASSED, S.INCONCLUSIVE)
    n_attempts = len(plan.prior) + (1 if consumed else 0)
    jobs = S.record_plan(res, demote_prior_pass=plan.recorded_pass)
    report: dict[str, dict[str, str]] = {}
    if jobs:
        provenance = {
            "bank_version": f"v{bank.version}", "bank_sha256": bank.sha256, "settings_hash": settings, "model_digest": digest0, "runtime": c.runtime,
            "judge": {"backend": getattr(c.backend, "name", None) if mode == S.JUDGE else "none",
                      "model_served": served[0] if (mode == S.JUDGE and len(served) == 1) else None, "mode": mode},
            "run_id": run_id, "date": _date(ts),
            "scope": {"max_prompt_chars": max_chars, "items_excluded_by_context": n_drawn - len(items), "draws": sorted({e["draw"] for e in plan.prior} | {draw})},
            "attempts": max(1, n_attempts),
        }
        report = eligibility_store.record_local_check(c.model, jobs, provenance, path=c.eligibility_path)
    recorded = res.verdict == S.PASS and any(v["status"] == "written" for v in report.values())
    log.append({"config_key": key, "config": {"model": c.model, "digest": digest0, "bank_version": bank.version, "bank_sha256": bank.sha256,
                                              "settings_hash": settings, "runtime": c.runtime},
                "draw": draw, "run_id": run_id, "ts": ts, "date": _date(ts), "mode": mode, "verdict": res.verdict, "k": res.k, "n": res.n_assigned,
                "consumed": consumed, "recorded": recorded, "rows": [r.to_dict() for r in rows_now], "assigned": assigned_now, "unrun": unrun,
                "judge_served": served})
    return Outcome("scored", res.why, res, draw, run_id, report, n_attempts)
