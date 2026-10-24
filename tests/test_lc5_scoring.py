"""LC-5: local-check scoring, gates, early stop, anti-retry, record. Fakes only (fake runtime adapter, fake judge backend); the real bank; no model call."""
from __future__ import annotations

import gzip
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router.local_check import attempts as A
from llm_router.local_check import grader_cases, score as S, session as SE, stats
from llm_router.local_check.bank import load_bank
from llm_router.local_check.budget import Budget
from llm_router.local_check.judge import RawReply
from llm_router.local_check.runtimes import GenResult

FIX = Path(__file__).parent / "fixtures"
PINNED = "claude-sonnet-5-5"
BANK = load_bank()

D268_DEVIATIONS = {
    "q37-511a487c23de": {
        "old": "ungrounded_issue_ref",
        "new": "judge:None",
        "reason": "Q37 G4-audit grader defect fixed (D-268)",
    },
}


# ---------------------------------------------------------------- pure scorer helpers
def mkrow(i: int, job: str, ok: bool, *, reasons: tuple = (), valid: bool = True, error: bool = False, empty: bool = False,
          prog: bool | None = None, judge: str | None = "x") -> S.Row:
    prog = (ok or not reasons) if prog is None else prog
    judge = ("pass" if ok else ("fail" if prog else None)) if judge == "x" else judge
    return S.Row(f"i{i}", job, "small", "py", True, valid, error, empty, prog, judge, ok, reasons)


def rows_n(k: int, n: int, *, commit_ok: int | None = None) -> list[S.Row]:
    """n rows (half commit, half pr), k ok. Failures are judge-fails (programmatically fine) unless stated."""
    half = n // 2
    jobs = ["commit_message"] * half + ["pr_description"] * (n - half)
    if commit_ok is None:
        oks = [True] * k + [False] * (n - k)
    else:
        oks = [True] * commit_ok + [False] * (half - commit_ok) + [True] * (k - commit_ok) + [False] * (n - half - (k - commit_ok))
    return [mkrow(i, jobs[i], oks[i]) for i in range(n)]


def good_gates(**kw) -> S.GateInputs:
    g = S.GateInputs(bank_sha_ok=True, judge_pinned=PINNED, judge_served=[PINNED], digest_start="d1", digest_end="d1",
                     g3a={"n": 103, "unexpected": []}, g3b=SimpleNamespace(status="PASS", reasons=[]))
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def ids(rows) -> list[str]:
    return [r.item_id for r in rows]


def verdict(rows, **kw) -> S.Result:
    return S.score(rows, ids(rows), kw.pop("gates", None) or good_gates(), **kw)


@pytest.mark.parametrize("k,n,expect", [(56, 70, S.PASS), (55, 70, S.INCONCLUSIVE), (50, 70, S.INCONCLUSIVE), (49, 70, S.FAIL), (0, 70, S.FAIL),
                                        (70, 70, S.PASS), (112, 140, S.PASS), (111, 140, S.INCONCLUSIVE), (103, 140, S.INCONCLUSIVE),
                                        (102, 140, S.FAIL)])
def test_verdict_table(k: int, n: int, expect: str) -> None:
    r = verdict(rows_n(k, n))
    assert r.verdict == expect, r.why
    assert (r.kmin, r.kfail) == (stats.kmin(n), stats.kfail(n))
    assert r.k == k and r.n_assigned == n


def test_boundaries_come_from_the_stats_not_from_this_file() -> None:
    assert (stats.kmin(70), stats.kfail(70), stats.kmin(140)) == (56, 49, 112)


def test_programmatic_failure_means_not_judged_and_not_ok() -> None:
    bank_item = BANK.draw(1)[0]
    rec = {"status": "ok", "text": "Here is the commit message: Fix things"}
    row = S.grade_row(bank_item, rec, "pass")
    assert not row.prog and not row.ok            # a judge pass cannot rescue a grader fail


# ---------------------------------------------------------------- lane guard
def test_lane_guard_excludes_a_weak_lane_from_the_pass() -> None:
    r = verdict(rows_n(56, 70, commit_ok=35))      # commit 35/35, pr 21/35 = 0.60
    assert r.verdict == S.PASS and r.guard_excluded == ["pr_description"]
    plan = S.record_plan(r)
    assert plan["commit_message"] == {"k": 56, "n": 70, "verdict": "PASS"}
    assert plan["pr_description"] == {"k": 21, "n": 35, "verdict": "FAIL", "status": "lane_guard"}


def test_lane_exactly_at_the_guard_is_kept() -> None:
    r = verdict(rows_n(60, 70, commit_ok=35))      # pr 25/35 = 0.714
    assert r.guard_excluded == []
    r2 = verdict(rows_n(56, 70, commit_ok=31))     # commit 31/35 = .886, pr 25/35 = .714
    assert r2.verdict == S.PASS and r2.guard_excluded == []


# ---------------------------------------------------------------- gates -> INVALID, nothing recorded
GATE_CASES = {
    "G0_integrity": [dict(bank_sha_ok=False), dict(judge_served=["other-model"]), dict(judge_served=[PINNED, "other-model"]), dict(judge_served=[]),
                     dict(digest_end="d2"), dict(bank_sha_ok=None)],
    "G3a_grader_selftest": [dict(g3a={"n": 103, "unexpected": ["x"]}), dict(g3a=None), dict(g3a={"n": 0, "unexpected": []})],
    "G3b_plants": [dict(g3b=SimpleNamespace(status="FAIL", reasons=["good: 9/12"])), dict(g3b=SimpleNamespace(status="INCONCLUSIVE", reasons=[])), dict(g3b=None)],
}


@pytest.mark.parametrize("gate,kw", [(g, kw) for g, cases in GATE_CASES.items() for kw in cases])
def test_each_gate_failure_is_invalid_and_records_nothing(gate: str, kw: dict) -> None:
    r = verdict(rows_n(66, 70), gates=good_gates(**kw))     # 66/70 would be a clear PASS
    assert r.verdict == S.INVALID and r.gates[gate]["status"] in ("FAIL", "NOT_MEASURED"), r.gates[gate]
    assert S.record_plan(r) == {}


def test_g1_valid_fraction() -> None:
    rows = rows_n(60, 70)
    rows[:11] = [mkrow(i, rows[i].job, False, valid=False, empty=True, reasons=("empty",), prog=False) for i in range(11)]   # 11 empties: also > 3 bad
    r = verdict(rows)
    assert r.verdict == S.INVALID and r.gates["G1_serves"]["status"] == "FAIL"


def test_g1_counts_each_row_once() -> None:
    def bad_rows(n_bad: int, flags: dict) -> list[S.Row]:
        rows = rows_n(60, 70)
        for i in range(n_bad):
            rows[i] = mkrow(i, rows[i].job, False, valid=False, prog=False, reasons=("empty",), **flags)
        return rows
    # 3 rows that are at once error AND timeout AND empty are 3 bad rows (not 9): at the limit, G1 passes.
    three = verdict(bad_rows(3, dict(error=True, empty=True)))
    assert three.gates["G1_serves"]["error_empty_timeout"] == 3 and three.gates["G1_serves"]["status"] == "PASS"
    four = verdict(bad_rows(4, dict(error=True, empty=True)))
    assert four.gates["G1_serves"]["error_empty_timeout"] == 4 and four.verdict == S.INVALID
    # the limit scales with n: 6 bad of 140 is fine, 7 is not
    rows = rows_n(130, 140)
    for i in range(6):
        rows[i] = mkrow(i, rows[i].job, False, valid=False, prog=False, error=True, empty=True, reasons=("empty",))
    assert verdict(rows).gates["G1_serves"]["status"] == "PASS"
    rows[6] = mkrow(6, rows[6].job, False, valid=False, prog=False, error=True, reasons=("empty",))
    assert verdict(rows).gates["G1_serves"]["status"] == "FAIL"


def test_g5_missing_item_and_unjudged_answer() -> None:
    rows = rows_n(60, 70)
    r = S.score(rows[:-1], ids(rows), good_gates())
    assert r.verdict == S.INVALID and r.gates["G5_complete"]["missing_items"] == 1
    rows2 = rows_n(60, 70)
    rows2[0] = mkrow(0, rows2[0].job, False, prog=True, judge=None)    # programmatically fine but never judged
    r2 = verdict(rows2)
    assert r2.verdict == S.INVALID and r2.gates["G5_complete"]["unjudged"] == 1



def test_empty_row_set_cannot_pass_a_gate() -> None:
    r = S.score([], [], good_gates())
    assert r.verdict == S.INVALID and r.gates["G1_serves"]["status"] == "FAIL"


# ---------------------------------------------------------------- early stop -> NOT_PASSED
def test_early_stop_is_not_passed_with_partial_counts_not_a_powered_fail() -> None:
    assigned = [f"i{i}" for i in range(70)]
    rows = [mkrow(i, "commit_message" if i < 35 else "pr_description", i >= 15) for i in range(45)]   # items 0..14 fail, 15..44 pass -> 15 failures
    r = S.score(rows, assigned, good_gates(), early_stop=True)
    assert r.verdict == S.NOT_PASSED and r.incomplete
    assert (r.k, r.n_scored, r.n_assigned) == (30, 45, 70)
    plan = S.record_plan(r)
    assert set(plan) == {"commit_message", "pr_description"} and all(v["verdict"] == "NOT_PASSED" and v["n"] == 45 and v["k"] == 30 for v in plan.values())


def test_early_stop_is_believed_only_when_the_bar_is_really_unreachable() -> None:
    assigned = [f"i{i}" for i in range(70)]
    rows = [mkrow(i, "commit_message" if i < 35 else "pr_description", i >= 14) for i in range(45)]    # 14 failures: 56 still reachable
    r = S.score(rows, assigned, good_gates(), early_stop=True)
    assert r.verdict == S.INVALID and r.gates["G5_complete"]["missing_items"] == 25


def test_a_complete_run_with_55_is_inconclusive_not_not_passed() -> None:
    r = verdict(rows_n(55, 70), early_stop=True)           # the runner reports early_stop on the last item; nothing is unrun
    assert r.verdict == S.INCONCLUSIVE and not r.incomplete


# ---------------------------------------------------------------- --no-judge
def test_no_judge_never_passes() -> None:
    allgood = [mkrow(i, "commit_message" if i < 35 else "pr_description", True, judge=None) for i in range(70)]
    r = verdict(allgood, mode=S.NO_JUDGE, gates=good_gates(g3b=None, judge_served=[]))
    assert r.verdict == S.PROBE and r.gates["G3b_plants"]["status"] == "N/A"
    assert S.record_plan(r) == {}
    for k in range(0, 71):
        rr = verdict([mkrow(i, "commit_message" if i < 35 else "pr_description", i < k, judge=None, prog=i < k) for i in range(70)],
                     mode=S.NO_JUDGE, gates=good_gates(g3b=None, judge_served=[]))
        assert rr.verdict != S.PASS
        assert rr.verdict == (S.FAIL if k <= 49 else S.PROBE), (k, rr.why)


def test_no_judge_fail_records_nothing() -> None:       # LC-10k: a probe never writes the eligibility store
    rows = [mkrow(i, "commit_message" if i < 35 else "pr_description", i < 40, judge=None, prog=i < 40) for i in range(70)]
    r = verdict(rows, mode=S.NO_JUDGE, gates=good_gates(g3b=None, judge_served=[]))
    assert r.verdict == S.FAIL and S.record_plan(r) == {}


# ---------------------------------------------------------------- rule concentration
def test_rule_concentration() -> None:
    rows = rows_n(56, 70)
    for i in range(56, 70):
        rows[i] = mkrow(i, rows[i].job, False, reasons=("ungrounded_symbol",) if i < 56 + 6 else ("subject_too_long",), prog=False)
    c = S.rule_concentration(rows)
    assert c and c["rule"] == "subject_too_long" and c["failures"] == 14 and c["with_rule"] == 8 and c["share"] == round(8 / 14, 4)
    rows = rows_n(56, 70)
    for i in range(56, 70):
        rows[i] = mkrow(i, rows[i].job, False, reasons=("r%d" % (i % 4),), prog=False)       # 4 rules: max share 4/14 = 0.286 < 0.40
    assert S.rule_concentration(rows) is None
    assert S.rule_concentration(rows_n(70, 70)) is None
    boundary = rows_n(60, 70)
    for i in range(60, 70):
        boundary[i] = mkrow(i, boundary[i].job, False, reasons=("a",) if i < 64 else (("b",) if i < 67 else ("c",)), prog=False)
    assert S.rule_concentration(boundary)["share"] == 0.4          # 4 of 10 is exactly the threshold
    judge_only = rows_n(60, 70)
    assert S.rule_concentration(judge_only) is None                # judge fails have no grader rule


# ---------------------------------------------------------------- anti-retry log (pure)
def test_attempt_log_draws_cooldown_and_exhaustion(tmp_path: Path) -> None:
    log = A.AttemptLog(tmp_path)
    key = A.config_key("m", "d", 1, "s" * 64, "h")
    assert log.plan(key, 1000.0).draw == 1
    log.append({"config_key": key, "draw": 1, "consumed": True, "ts": 1000.0})
    p = log.plan(key, 1000.0 + 3600)
    assert p.draw is None and "cooldown" in p.refused
    assert log.plan(key, 1000.0 + 3600, force=True).draw == 2
    assert log.plan(key, 1000.0 + A.COOLDOWN_S).draw == 2
    log.append({"config_key": key, "draw": 2, "consumed": True, "ts": 1000.0 + A.COOLDOWN_S})
    for force in (False, True):
        third = log.plan(key, 9e9, force=force)
        assert third.draw is None and "exhausted" in third.refused        # force does not lift the exhaustion
    other = A.config_key("m", "d2", 1, "s" * 64, "h")                      # new digest: fresh configuration
    assert log.plan(other, 1000.0).draw == 1
    assert A.config_key("m", "d", 1, "s" * 64, "h2") != key and A.config_key("m", "d", 2, "s" * 64, "h") != key


def test_unconsumed_attempts_do_not_count(tmp_path: Path) -> None:
    log = A.AttemptLog(tmp_path)
    key = A.config_key("m", "d", 1, "s" * 64, "h")
    for _ in range(3):
        log.append({"config_key": key, "draw": 1, "consumed": False, "ts": 1.0, "verdict": "INVALID"})
    assert log.plan(key, 2.0).draw == 1
    assert (tmp_path / A.LOG_NAME).stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- session with fakes
def answer(item, tag: str = "zq") -> str:
    base = item.files[0].rsplit("/", 1)[-1] if item.files else "file"
    if item.job == "commit_message":
        return f"Update {base} handling\n\nAdjust the code so behaviour matches expectations {tag}."
    return (f"## Summary\nThis change updates {base} so that the behaviour matches what the surrounding code expects now {tag}.\n\n"
            f"## Test plan\n- Run the existing test suite {tag}")


class FakeAdapter:
    runtime = "fake"

    def __init__(self, behaviour: dict[str, str] | None = None, digest: str | None = "sha256:aaa", max_chars: int | None = None):
        self.behaviour, self.digest_value, self.max_chars = behaviour or {}, digest, max_chars
        self.by_prompt = {i.task: i for d in (1, 2) for i in BANK.draw(d)}
        self.calls = 0
        self.change_digest_after: int | None = None

    def digest(self, model):
        return self.digest_value

    def settings_hash(self, model):
        return "meta:sha256:fake"

    def max_prompt_chars(self, model):
        return self.max_chars

    def generate(self, model, prompt, *, system=None, timeout_s=240.0):
        self.calls += 1
        if self.change_digest_after is not None and self.calls > self.change_digest_after:
            self.digest_value = "sha256:bbb"
        item = self.by_prompt[prompt]
        how = self.behaviour.get(item.id, "ok")
        if how == "error":
            return GenResult("error", error="boom", elapsed_s=0.1)
        if how == "empty":
            return GenResult("empty", text="", elapsed_s=0.1)
        tag = {"judgefail": "JUDGEFAIL", "judgeerr": "JUDGEERR"}.get(how, "zq")
        text = "Here is the commit message you asked for" if how == "progfail" else answer(item, tag)
        return GenResult("ok", text=text, raw_chars=len(text), elapsed_s=0.1, first_token_s=0.05, completion_tokens=40)


class FakeJudge:
    name, metered, model = "fake", False, PINNED

    def __init__(self, served: str = PINNED, plant_wrong: set[str] | None = None, fail_all_answers: bool = False):
        self.served, self.plant_wrong, self.fail_all, self.calls = served, plant_wrong or set(), fail_all_answers, 0
        self.err_marker = True          # answers tagged JUDGEERR get a backend error while this is set
        self.answer_calls = 0
        self.heal_after: int | None = None      # once this many answer calls were made, JUDGEERR answers are judged normally
        self.plants = {(p.task, p.text): p for p in BANK.plants()}       # the same text can be both a good and a swapped plant: key by task too

    def complete(self, prompt: str) -> RawReply:
        self.calls += 1
        verdict = "pass"
        for (task, text), p in self.plants.items():
            if f"=== TASK INPUT (verbatim) ===\n{task}\n=== END TASK INPUT ===" in prompt and f"=== CANDIDATE ANSWER ===\n{text[:4000]}\n=== END CANDIDATE" in prompt:
                right = "pass" if p.kind == "good" else "fail"
                verdict = ("fail" if right == "pass" else "pass") if p.id in self.plant_wrong else right
                break
        else:
            self.answer_calls += 1
            if self.err_marker and (self.heal_after is None or self.answer_calls <= self.heal_after) and "JUDGEERR" in prompt.split("=== CANDIDATE ANSWER ===")[-1]:
                return RawReply(error="http 529", served_model=self.served)
            if "JUDGEFAIL" in prompt.split("=== CANDIDATE ANSWER ===")[-1] or self.fail_all:
                verdict = "fail"
        body = {"verdict": "pass", "defect": None, "quote": "", "reason": "ok"} if verdict == "pass" else {"verdict": "fail", "defect": "A", "quote": "x", "reason": "wrong"}
        return RawReply(text=json.dumps(body), served_model=self.served, cost_usd=0.01)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def check(tmp_path: Path, adapter: FakeAdapter | None = None, judge: FakeJudge | None = None, clock: Clock | None = None, **kw) -> SE.Check:
    no_judge = kw.get("no_judge", False)
    return SE.Check(model="fake-model:1b", runtime="fake", bank=BANK, adapter=adapter or FakeAdapter(), runs_root=tmp_path / "runs",
                    backend=None if no_judge else (judge or FakeJudge()), budget=None if no_judge else Budget(metered=False, cap_calls=500),
                    now=clock or Clock(), eligibility_path=tmp_path / "elig.json", **kw)


def lanes(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "elig.json").read_text())["fake-model:1b"]["text_job_local"]


def fails(n: int, how: str = "judgefail", draw: int = 1) -> dict[str, str]:
    """The first n items of the draw, taken alternately from the two jobs (so no lane is wiped out by the lane guard)."""
    items = BANK.draw(draw)
    a = [i for i in items if i.job == "commit_message"]
    b = [i for i in items if i.job == "pr_description"]
    mixed = [x for pair in zip(a, b) for x in pair]
    return {i.id: how for i in mixed[:n]}


def test_session_pass_writes_full_provenance(tmp_path: Path) -> None:
    out = SE.run_check(check(tmp_path, FakeAdapter(fails(10))))          # 60/70
    assert out.status == "scored" and out.result.verdict == S.PASS and (out.result.k, out.result.n_assigned) == (60, 70), out.result and out.result.why
    assert all(v["status"] == "written" for v in out.record_report.values())
    for job, lane in lanes(tmp_path).items():
        assert lane["grade"] == "passed" and (lane["k"], lane["n"]) == (60, 70) and lane["source"] == "local_check"
        assert lane["bank_version"] == "v1" and lane["bank_sha256"] == BANK.sha256 and lane["manifest_sha256"] == BANK.sha256
        assert lane["model_digest"] == "sha256:aaa" and lane["runtime"] == "fake" and lane["settings_hash"] == "meta:sha256:fake"
        assert lane["judge"] == {"backend": "fake", "model_served": PINNED, "mode": "judge"}
        assert lane["run_id"] == out.run_id and lane["date"] == "2027-01-15" and lane["attempts"] == 1
        assert lane["scope"] == {"max_prompt_chars": None, "items_excluded_by_context": 0, "draws": [1]}
        assert "task" not in json.dumps(lane) and "diff" not in lane
    log = A.AttemptLog(tmp_path / "runs").entries()
    assert len(log) == 1 and log[0]["consumed"] and log[0]["recorded"] and log[0]["verdict"] == "PASS" and "text" not in json.dumps(log[0])


def test_session_programmatic_failures_are_never_judged(tmp_path: Path) -> None:
    judge = FakeJudge()
    SE.run_check(check(tmp_path, FakeAdapter(fails(10, "progfail")), judge))
    assert judge.calls == 30 + 60                                         # 30 plants + only the 60 programmatically ok answers


def test_session_powered_fail_is_recorded(tmp_path: Path) -> None:
    # judge-only failures are not seen by the runner, so the run completes and the verdict is a powered FAIL
    out2 = SE.run_check(check(tmp_path / "b", FakeAdapter(fails(30, "judgefail"))))
    assert out2.result.verdict == S.FAIL and (out2.result.k, out2.result.n_assigned) == (40, 70)
    lane = lanes(tmp_path / "b")["commit_message"]
    assert lane["grade"] == "failed" and (lane["k"], lane["n"]) == (40, 70) and "status" not in lane


def test_session_early_stop_records_not_passed_with_partial_counts(tmp_path: Path) -> None:
    ad = FakeAdapter(fails(70, "progfail"))
    out = SE.run_check(check(tmp_path, ad))
    assert out.result.verdict == S.NOT_PASSED
    assert ad.calls == 15                                              # the 15th failure makes 56/70 unreachable: the runner stops there
    lane = lanes(tmp_path)["commit_message"]
    assert lane["grade"] == "failed" and lane["status"] == "not_passed" and lane["verdict"] == "failed" and (lane["k"], lane["n"]) == (0, 15)


def test_session_inconclusive_writes_nothing(tmp_path: Path) -> None:
    out = SE.run_check(check(tmp_path, FakeAdapter(fails(18))))          # 52/70
    assert out.result.verdict == S.INCONCLUSIVE and out.record_report == {}
    assert not (tmp_path / "elig.json").exists()
    assert A.AttemptLog(tmp_path / "runs").entries()[0]["consumed"] is True       # an inconclusive run still consumed draw 1


def test_session_g3a_runs_before_any_model_call(tmp_path: Path, monkeypatch) -> None:
    ad = FakeAdapter()
    monkeypatch.setattr(grader_cases, "run", lambda: {"n": 103, "unexpected": ["case-x"]})
    out = SE.run_check(check(tmp_path, ad))
    assert out.result.verdict == S.INVALID and ad.calls == 0 and not (tmp_path / "elig.json").exists()


def test_session_g3b_failure_stops_before_the_local_arm(tmp_path: Path) -> None:
    ad = FakeAdapter()
    wrong = {p.id for p in BANK.plants("good")[:5]}
    out = SE.run_check(check(tmp_path, ad, FakeJudge(plant_wrong=wrong)))
    assert out.result.verdict == S.INVALID and out.result.gates["G3b_plants"]["status"] == "FAIL"
    assert ad.calls == 0 and not (tmp_path / "elig.json").exists() and A.AttemptLog(tmp_path / "runs").entries() == []


def test_session_g0_judge_served_id_differs_from_pinned(tmp_path: Path) -> None:
    out = SE.run_check(check(tmp_path, judge=FakeJudge(served="some-other-model")))
    assert out.result.verdict == S.INVALID and out.result.gates["G0_integrity"]["status"] == "FAIL"
    assert not (tmp_path / "elig.json").exists()
    e = A.AttemptLog(tmp_path / "runs").entries()[0]
    assert e["consumed"] is False and e["verdict"] == "INVALID"


def test_session_g0_digest_changes_during_the_run(tmp_path: Path) -> None:
    ad = FakeAdapter()
    ad.change_digest_after = 10
    out = SE.run_check(check(tmp_path, ad))
    assert out.result.verdict == S.INVALID and out.result.gates["G0_integrity"]["digest_unchanged"] is False
    assert not (tmp_path / "elig.json").exists()


def test_session_g0_bank_changed_since_load(tmp_path: Path) -> None:
    def tampered():
        return SimpleNamespace(sha256="0" * 64)
    out = SE.run_check(check(tmp_path, bank_loader=tampered))
    assert out.result.verdict == S.INVALID and out.result.gates["G0_integrity"]["bank_sha_ok"] is False


def test_session_g1_errors_counted_once_per_row(tmp_path: Path) -> None:
    items = BANK.draw(1)
    beh = {items[0].id: "error", items[1].id: "empty", items[2].id: "error", items[3].id: "empty"}      # 4 rows -> over 3/70
    out = SE.run_check(check(tmp_path, FakeAdapter(beh)))
    g1 = out.result.gates["G1_serves"]
    assert out.result.verdict == S.INVALID and g1["error_empty_timeout"] == 4 and g1["n"] == 70
    ok = SE.run_check(check(tmp_path / "ok", FakeAdapter({items[0].id: "error", items[1].id: "empty", items[2].id: "error"})))
    assert ok.result.gates["G1_serves"]["status"] == "PASS" and ok.result.gates["G1_serves"]["error_empty_timeout"] == 3


def test_session_budget_stop_is_inconclusive_and_records_nothing(tmp_path: Path) -> None:
    c = check(tmp_path)
    c.budget = Budget(metered=False, cap_calls=45)                     # 30 plants + 15 answers, then stop
    out = SE.run_check(c)
    assert out.status == "budget_stop" and out.result is None and not (tmp_path / "elig.json").exists()
    held = A.AttemptLog(tmp_path / "runs").entries()                  # the run dir is reserved (open), nothing consumed, no strike
    assert len(held) == 1 and held[0]["state"] == "open" and not held[0]["consumed"] and not held[0]["strike"]
    c2 = check(tmp_path, run_id=out.run_id)                           # resume: local results are on disk, only the judge calls remain
    ad2 = c2.adapter
    out2 = SE.run_check(c2)
    assert out2.status == "scored" and out2.result.verdict == S.PASS and ad2.calls == 0


def test_session_scope_excludes_items_that_do_not_fit(tmp_path: Path) -> None:
    sizes = sorted(len(i.task) for i in BANK.draw(1))
    cap = sizes[-1] - 1
    out = SE.run_check(check(tmp_path, FakeAdapter(max_chars=cap)))
    n_fit = sum(1 for s in sizes if s <= cap)
    assert out.result.n_assigned == n_fit < 70
    lane = lanes(tmp_path)["commit_message"]
    assert lane["scope"]["max_prompt_chars"] == cap and lane["scope"]["items_excluded_by_context"] == 70 - n_fit


# ---- cumulative draws -------------------------------------------------------------------------------------------------------------------
def test_second_run_uses_draw_2_and_the_cumulative_union(tmp_path: Path) -> None:
    clock = Clock()
    ad = FakeAdapter({**fails(18, draw=1)})                            # draw 1: 52/70 inconclusive
    first = SE.run_check(check(tmp_path, ad, clock=clock))
    assert first.result.verdict == S.INCONCLUSIVE and first.draw == 1
    assert SE.run_check(check(tmp_path, FakeAdapter(), clock=clock)).status == "refused"       # cooldown
    clock.t += A.COOLDOWN_S + 1
    ad2 = FakeAdapter({**fails(18, draw=1), **fails(5, draw=2)})        # draw 2: 65/70
    second = SE.run_check(check(tmp_path, ad2, clock=clock))
    assert second.draw == 2 and second.result.n_assigned == 140
    assert (second.result.k, second.result.kmin) == (52 + 65, 112) and second.result.verdict == S.PASS
    assert second.attempts == 2
    for lane in lanes(tmp_path).values():
        assert (lane["k"], lane["n"], lane["attempts"]) == (117, 140, 2) and lane["scope"]["draws"] == [1, 2]
    assert ad2.calls == 70                                              # only the unused draw ran
    # a third same-configuration run is refused, forced or not
    clock.t += 10 * A.COOLDOWN_S
    for force in (False, True):
        third = SE.run_check(check(tmp_path, FakeAdapter(), clock=clock, force=force))
        assert third.status == "refused" and "exhausted" in third.reason


def test_force_lifts_the_cooldown_but_not_the_cumulative_rule(tmp_path: Path) -> None:
    clock = Clock()
    SE.run_check(check(tmp_path, FakeAdapter(fails(18)), clock=clock))
    forced = SE.run_check(check(tmp_path, FakeAdapter(fails(18)), clock=clock, force=True))
    assert forced.status == "scored" and forced.draw == 2 and forced.result.n_assigned == 140


def test_inconclusive_rerun_writes_nothing_and_keeps_the_earlier_pass(tmp_path: Path) -> None:
    clock = Clock()
    first = SE.run_check(check(tmp_path, FakeAdapter(fails(10)), clock=clock))      # 60/70 PASS, recorded
    assert first.result.verdict == S.PASS and lanes(tmp_path)["commit_message"]["grade"] == "passed"
    clock.t += A.COOLDOWN_S + 1
    second = SE.run_check(check(tmp_path, FakeAdapter({**fails(10), **fails(25, draw=2)}), clock=clock))     # draw 2: 45/70 -> 105/140
    assert second.result.verdict == S.INCONCLUSIVE and second.record_report == {}
    lane = lanes(tmp_path)["commit_message"]           # owner D-263(a): the earlier PASS is kept, the rerun is only logged
    assert lane["grade"] == "passed" and (lane["k"], lane["n"], lane["attempts"]) == (60, 70, 1)
    log = A.AttemptLog(tmp_path / "runs").entries()
    assert [e["verdict"] for e in log] == ["PASS", "INCONCLUSIVE"] and log[1]["consumed"] and log[1]["draw"] == 2


def test_inconclusive_rerun_does_not_touch_an_earlier_recorded_fail_either(tmp_path: Path) -> None:
    clock = Clock()
    first = SE.run_check(check(tmp_path, FakeAdapter(fails(21)), clock=clock))      # 49/70 FAIL recorded
    assert first.result.verdict == S.FAIL
    clock.t += A.COOLDOWN_S + 1
    second = SE.run_check(check(tmp_path, FakeAdapter({**fails(21), **fails(10, draw=2)}), clock=clock))      # draw 2: 60/70 -> 109/140 inconclusive
    assert second.result.verdict == S.INCONCLUSIVE
    assert lanes(tmp_path)["commit_message"]["grade"] == "failed" and lanes(tmp_path)["commit_message"]["k"] == 49


def test_changed_digest_or_settings_is_a_new_configuration(tmp_path: Path) -> None:
    clock = Clock()
    SE.run_check(check(tmp_path, FakeAdapter(fails(18)), clock=clock))
    other = SE.run_check(check(tmp_path, FakeAdapter(fails(18), digest="sha256:ccc"), clock=clock))
    assert other.status == "scored" and other.draw == 1 and other.result.n_assigned == 70


def test_rerun_after_early_stop_is_refused_only_when_the_cumulative_bar_is_unreachable(tmp_path: Path) -> None:
    clock = Clock()
    first = SE.run_check(check(tmp_path, FakeAdapter(fails(70, "progfail")), clock=clock))
    assert first.result.verdict == S.NOT_PASSED
    clock.t += A.COOLDOWN_S + 1
    ad = FakeAdapter()
    second = SE.run_check(check(tmp_path, ad, clock=clock))
    assert second.status == "refused" and "can no longer be reached" in second.reason and ad.calls == 0


# ---- no-judge ---------------------------------------------------------------------------------------------------------------------------
def test_session_no_judge_cannot_pass_and_consumes_nothing(tmp_path: Path) -> None:
    out = SE.run_check(check(tmp_path, FakeAdapter(), no_judge=True))
    assert out.result.verdict == S.PROBE and out.record_report == {} and not (tmp_path / "elig.json").exists()
    e = A.AttemptLog(tmp_path / "runs").entries()[0]
    assert e["consumed"] is False and e["mode"] == "none"
    again = SE.run_check(check(tmp_path, FakeAdapter(), no_judge=True))                  # not blocked by cooldown or draws
    assert again.status == "scored"


def test_session_no_judge_early_stop_is_logged_not_recorded(tmp_path: Path) -> None:
    out2 = SE.run_check(check(tmp_path, FakeAdapter(fails(21, "progfail")), no_judge=True))
    assert out2.result.verdict == S.NOT_PASSED       # the runner stops at the 15th programmatic failure; LC-10k: attempt log only
    assert not (tmp_path / "elig.json").exists()
    e = A.AttemptLog(tmp_path / "runs").entries()[0]
    assert e["mode"] == "none" and e["verdict"] == "NOT_PASSED" and e["consumed"] is False and e["recorded"] is False


def test_judged_run_requires_backend_and_budget(tmp_path: Path) -> None:
    c = check(tmp_path)
    c.backend = None
    with pytest.raises(ValueError):
        SE.run_check(c)


# ---------------------------------------------------------------- D-263 repair round 1 (review of cf04709)
@pytest.mark.parametrize("served,ok", [(["claude-sonnet-5-5-20260101"], True), ([PINNED, "claude-sonnet-5-5-20260101"], True),
                                       ([f"{PINNED},{PINNED}-20260101"], True), ([f"{PINNED},other-model"], False), (["other-model"], False), ([""], False)])
def test_g0_judge_id_goes_through_served_model_matches(served: list[str], ok: bool) -> None:
    r = verdict(rows_n(66, 70), gates=good_gates(judge_served=served))
    assert (r.verdict == S.PASS) is ok, r.gates["G0_integrity"]
    assert r.gates["G0_integrity"]["judge_ok"] is ok


@pytest.mark.parametrize("n", [8, 20, 49])
def test_small_scope_gets_neither_pass_nor_powered_fail(n: int) -> None:
    for k in (n, 0):
        r = verdict(rows_n(k, n))
        assert r.verdict == S.INCONCLUSIVE and "scope too narrow" in r.why and S.record_plan(r) == {}, (n, k, r.why)


def test_n50_is_enough_and_a_thin_lane_is_not() -> None:
    assert verdict(rows_n(50, 50)).verdict == S.PASS                           # kmin(50) = 40
    rows = [mkrow(i, "commit_message" if i < 55 else "pr_description", True) for i in range(70)]       # pr lane has 15 items
    r = verdict(rows)
    assert r.verdict == S.INCONCLUSIVE and "pr_description" in r.why and S.record_plan(r) == {}
    probe = [mkrow(i, "commit_message" if i < 55 else "pr_description", False, judge=None, prog=False, reasons=("x",)) for i in range(70)]
    assert verdict(probe, mode=S.NO_JUDGE, gates=good_gates(g3b=None, judge_served=[])).verdict == S.INCONCLUSIVE     # no powered no-judge FAIL either


def test_judge_errors_within_5_percent_are_excluded_from_n_beyond_it_invalid() -> None:
    rows = rows_n(60, 70)
    rows[0] = mkrow(0, rows[0].job, False, prog=True, judge="judge_error")
    r = verdict(rows)
    assert r.verdict == S.PASS and r.n_assigned == 69 and r.excluded_unjudged == 1 and r.k == 59
    assert S.record_plan(r)["commit_message"]["n"] == 69
    four = rows_n(60, 70)
    for i in range(4):
        four[i] = mkrow(i, four[i].job, False, prog=True, judge="judge_error")
    r4 = verdict(four)
    assert r4.verdict == S.INVALID and r4.gates["G5_complete"]["judge_errors"] == 4 and r4.gates["G5_complete"]["judge_error_tolerance"] == 3
    shown = S.redact_invalid(r4)
    assert (shown.k, shown.pooled, shown.lanes, shown.kmin) == (0, {}, {}, None) and "G5_complete" in shown.gates


def test_record_counts_are_over_n_assigned_not_scored_rows() -> None:
    rows = rows_n(117, 132)                                    # 132 scored rows; 8 items were abandoned by an earlier early-stopped attempt
    assigned = ids(rows) + [f"x{i}" for i in range(8)]
    r = S.score(rows, assigned, good_gates(), abandoned=assigned[-8:])
    assert r.verdict == S.PASS and r.n_assigned == 140 and r.n_scored == 132
    assert all((v["k"], v["n"]) == (117, 140) for v in S.record_plan(r).values())
    assigned70 = [f"i{i}" for i in range(70)]
    part = [mkrow(i, "commit_message" if i < 35 else "pr_description", i >= 15) for i in range(45)]
    np_ = S.score(part, assigned70, good_gates(), early_stop=True)
    assert np_.verdict == S.NOT_PASSED
    assert {(v["k"], v["n"]) for v in S.record_plan(np_).values()} == {(30, 45)}                  # first attempt: partial counts
    assert {(v["k"], v["n"]) for v in S.record_plan(np_, cumulative=True).values()} == {(30, 70)}  # later attempt: over n_assigned


def test_config_key_is_keyed_on_the_digest_when_known() -> None:
    assert A.config_key("m", "sha256:a", 1, "s", "h") == A.config_key("m-alias", "sha256:a", 1, "s", "h")      # `ollama cp m m-alias`
    assert A.config_key("m", "sha256:a", 1, "s", "h") != A.config_key("m", "sha256:b", 1, "s", "h")
    assert A.config_key("m", None, 1, "s", "h") != A.config_key("m2", None, 1, "s", "h")                       # no digest: the name


def go(tmp_path: Path, adapter=None, judge=None, clock=None, **kw) -> SE.Outcome:
    return SE.run_check(check(tmp_path, adapter, judge, clock, **kw))


def test_session_accepts_a_dated_snapshot_and_a_comma_list_of_the_pinned_judge(tmp_path: Path) -> None:
    dated = go(tmp_path / "a", judge=FakeJudge(served="claude-sonnet-5-5-20260101"))
    assert dated.result.verdict == S.PASS and dated.result.gates["G0_integrity"]["judge_ok"]
    both = f"{PINNED},{PINNED}-20260101"
    listed = go(tmp_path / "b", judge=FakeJudge(served=both))
    assert listed.result.verdict == S.PASS
    assert lanes(tmp_path / "b")["commit_message"]["judge"]["model_served"] == both          # the served string is recorded, not null
    other = go(tmp_path / "c", judge=FakeJudge(served=f"{PINNED},some-other-model"))
    assert other.result.verdict == S.INVALID


def test_early_stop_then_clean_draw_records_the_102_of_140_fail(tmp_path: Path) -> None:
    clock = Clock()
    items = BANK.draw(1)
    ad1 = FakeAdapter({i.id: "progfail" for i in items[42:57]})          # 42 ok, then 15 programmatic failures: stop on the 57th item
    first = go(tmp_path, ad1, clock=clock)
    assert first.result.verdict == S.NOT_PASSED and ad1.calls == 57 and first.result.k == 42
    clock.t += A.COOLDOWN_S + 1
    second = go(tmp_path, FakeAdapter(fails(10, draw=2)), clock=clock)    # draw 2: 60/70 -> 102/140 = kfail(140)
    assert second.status == "scored" and second.result.verdict == S.FAIL, (second.status, second.reason)
    assert (second.result.k, second.result.n_assigned, second.result.kfail) == (102, 140, 102)
    assert all(v["status"] == "written" for v in second.record_report.values()) and second.record_report
    for lane in lanes(tmp_path).values():
        assert lane["grade"] == "failed" and (lane["k"], lane["n"], lane["attempts"]) == (102, 140, 2) and "status" not in lane


def test_store_refusal_is_an_error_consumes_no_draw_and_reruns_resume(tmp_path: Path, monkeypatch) -> None:
    ad = FakeAdapter()
    with monkeypatch.context() as m:
        m.setattr(SE.eligibility_store, "record_local_check",
                  lambda model, jobs, prov, path=None: {j: {"status": "refused", "reason": "grade does not match counts"} for j in jobs})
        out = go(tmp_path, ad)
    assert out.status == "record_refused" and "grade does not match counts" in out.reason
    log = A.AttemptLog(tmp_path / "runs").entries()
    assert len(log) == 1 and log[0]["state"] == "open" and log[0]["consumed"] is False
    calls = ad.calls
    again = go(tmp_path, ad)
    assert again.status == "scored" and again.draw == 1 and again.run_id == out.run_id and ad.calls == calls     # no new draw, no new local calls
    assert A.AttemptLog(tmp_path / "runs").entries()[-1]["consumed"] is True


def invalid_adapter() -> FakeAdapter:
    return FakeAdapter({i.id: "error" for i in BANK.draw(1)[:4]})         # 4 bad rows: G1 fails after the local arm


def test_invalid_after_the_local_arm_reserves_the_run_hides_counts_and_the_third_burns_the_draw(tmp_path: Path) -> None:
    clock, ad, judge = Clock(), invalid_adapter(), FakeJudge()
    o1 = go(tmp_path, ad, judge, clock)
    assert o1.result.verdict == S.INVALID and (o1.result.k, o1.result.pooled, o1.result.lanes) == (0, {}, {})
    e = A.AttemptLog(tmp_path / "runs").entries()
    assert len(e) == 1 and e[0]["state"] == "open" and e[0]["strike"] and not e[0]["consumed"] and e[0]["k"] is None and e[0]["rows"] == []
    calls, jcalls = ad.calls, judge.calls
    o2 = go(tmp_path, ad, judge, clock)                                   # strike 2: same run id, cached answers, cached plants, cached judgements
    assert o2.run_id == o1.run_id and o2.result.verdict == S.INVALID and ad.calls == calls and judge.calls == jcalls
    o3 = go(tmp_path, ad, judge, clock)                                   # strike 3: the draw is burnt
    assert o3.result.verdict == S.INVALID and ad.calls == calls
    last = A.AttemptLog(tmp_path / "runs").entries()[-1]
    assert last["state"] == "burnt" and last["consumed"] is True and last["draw"] == 1
    assert go(tmp_path, FakeAdapter(), clock=clock).status == "refused"    # cooldown after a consumed draw
    clock.t += A.COOLDOWN_S + 1
    nxt = go(tmp_path, FakeAdapter(), clock=clock)
    assert nxt.draw == 2 and nxt.result.verdict == S.PASS and nxt.run_id != o1.run_id


def test_open_run_must_be_resumed_unless_discarded_or_expired(tmp_path: Path) -> None:
    clock = Clock()
    o1 = go(tmp_path, invalid_adapter(), clock=clock)
    refused = go(tmp_path, FakeAdapter(), clock=clock, run_id="some-other-run")
    assert refused.status == "refused" and o1.run_id in refused.reason
    fresh = go(tmp_path, FakeAdapter(), clock=clock, fresh=True)
    assert fresh.run_id != o1.run_id and fresh.result.verdict == S.PASS and fresh.draw == 1
    entries = A.AttemptLog(tmp_path / "runs").entries()
    assert [e["state"] for e in entries if e["run_id"] == o1.run_id] == ["open", "discarded"] and entries[1]["strike"]
    # a reservation older than 7 days no longer binds
    t2 = tmp_path / "t2"
    c2 = Clock()
    p1 = go(t2, invalid_adapter(), clock=c2)
    c2.t += A.RESERVATION_TTL_S + 1
    p2 = go(t2, FakeAdapter(), clock=c2)
    assert p2.run_id != p1.run_id and p2.result.verdict == S.PASS


def test_budget_stop_and_g3b_failure_follow_the_reservation_rule(tmp_path: Path) -> None:
    wrong = {p.id for p in BANK.plants("good")[:5]}
    go(tmp_path / "g", FakeAdapter(), FakeJudge(plant_wrong=wrong))
    assert A.AttemptLog(tmp_path / "g" / "runs").entries() == []                 # INVALID before the local arm: nothing consumed, nothing reserved
    c = check(tmp_path / "b")
    c.budget = Budget(metered=False, cap_calls=45)
    stopped = SE.run_check(c)
    assert stopped.status == "budget_stop"
    refused = go(tmp_path / "b", FakeAdapter(), run_id="elsewhere")
    assert refused.status == "refused" and stopped.run_id in refused.reason


def test_judge_error_is_not_cached_retried_and_excluded_up_to_5_percent(tmp_path: Path) -> None:
    first_id = BANK.draw(1)[0].id
    judge = FakeJudge()
    out = go(tmp_path, FakeAdapter({first_id: "judgeerr"}), judge)
    assert out.result.verdict == S.PASS and out.result.n_assigned == 69 and out.result.excluded_unjudged == 1
    assert judge.answer_calls == 69 + 3 * 2                                    # 1 call per good answer; the errored one: 3 passes x 2 attempts
    cached = (tmp_path / "runs" / out.run_id / "judge.jsonl").read_text().splitlines()
    assert len(cached) == 69 and "judge_error" not in "".join(cached)
    assert lanes(tmp_path)["commit_message"]["n"] == 69
    healed = FakeJudge()
    healed.heal_after = 71                                                     # the errored item is judged properly on the 2nd pass
    ok = go(tmp_path / "h", FakeAdapter({first_id: "judgeerr"}), healed)
    assert ok.result.verdict == S.PASS and ok.result.n_assigned == 70 and ok.result.excluded_unjudged == 0


def test_too_many_judge_errors_is_invalid_but_keeps_answers_and_judgements_for_resume(tmp_path: Path) -> None:
    ad, judge = FakeAdapter({i.id: "judgeerr" for i in BANK.draw(1)[:4]}), FakeJudge()
    bad = go(tmp_path, ad, judge)
    assert bad.result.verdict == S.INVALID and bad.result.gates["G5_complete"]["judge_errors"] == 4 and bad.result.k == 0
    calls, jcalls = ad.calls, judge.calls
    judge.err_marker = False                                                   # the judge recovers
    good = go(tmp_path, ad, judge)
    assert good.run_id == bad.run_id and good.result.verdict == S.PASS and good.result.n_assigned == 70
    assert ad.calls == calls and judge.calls - jcalls == 4                     # only the four errored answers were judged again; no plants, no local calls


def test_a_context_that_fits_fewer_than_50_items_is_refused_before_any_spend(tmp_path: Path) -> None:
    sizes = sorted(len(i.task) for i in BANK.draw(1))
    cap = sizes[30]
    assert sum(1 for x in sizes if x <= cap) < 50
    ad, judge = FakeAdapter(max_chars=cap), FakeJudge()
    out = go(tmp_path, ad, judge)
    assert out.status == "refused" and "scope too narrow" in out.reason and "num_ctx" in out.reason
    assert ad.calls == 0 and judge.calls == 0 and A.AttemptLog(tmp_path / "runs").entries() == []


def test_text_job_lines_show_not_passed_apart_from_failed(tmp_path: Path) -> None:
    from llm_router.resolver import eligibility_store as ES
    go(tmp_path / "np", FakeAdapter(fails(70, "progfail")))
    go(tmp_path / "f", FakeAdapter(fails(30)))
    np_line = "\n".join(ES.text_job_lines(path=tmp_path / "np" / "elig.json"))
    f_line = "\n".join(ES.text_job_lines(path=tmp_path / "f" / "elig.json"))
    assert "not passed 0/15" in np_line and "failed" not in np_line
    assert "failed 40/70" in f_line and "not passed" not in f_line


# ---------------------------------------------------------------- port-equivalence with the stored Q37 run
def test_port_equivalence_q37_rescored_is_pass_60_of_70() -> None:
    """Re-score the stored Q37 qwen run (answers + recorded judge verdicts) with the ported scorer -> PASS 60/70 (32/35 commit, 28/35 PR).

    64 of the 70 frozen items are re-graded from their stored answer text (tests/fixtures/lc2). The other 6 are copyleft items whose diffs the
    repo does not ship; their recorded Q37 outcome is used as is (all six: grader ok, judge pass)."""
    cases = json.loads((FIX / "lc5" / "q37_cases.json").read_text())["cases"]
    with gzip.open(FIX / "lc2" / "q37_items.jsonl.gz", "rt", encoding="utf-8") as f:
        shipped = {d["id"]: d for d in (json.loads(x) for x in f if x.strip())}
    rows, regraded, deviations = [], 0, 0
    for iid, c in sorted(cases.items()):
        if iid in shipped:
            it = shipped[iid]
            item = SimpleNamespace(id=iid, job=it["stratum"], size=c["size"], lang=c["lang"], task=it["context"], diff=it["diff"], files=it["files"])
            rec = {"status": "truncated" if c["done_reason"] == "length" else "ok", "text": it["answers"]["qwen"], "error": c["err"]}
            row = S.grade_row(item, rec, c["judge"])
            assert row.ok == c["ok"], (iid, row.reasons, c["reason"])
            reason = ",".join(row.reasons) or ("judge:" + str(c["judge"]) if not row.ok else "accepted")
            deviation = D268_DEVIATIONS.get(iid)
            if deviation is not None:
                assert c["reason"] == deviation["old"], deviation["reason"]
                assert reason == deviation["new"], deviation["reason"]
                assert row.prog is True
                assert row.reasons == ()
                assert c["judge"] is None
                # Q37 never judged this answer (it failed the old rule), so the regraded row is programmatic-ok with no judge verdict,
                # which G5 would count as an unjudged answer. The score below replays the recorded Q37 outcome for it; with a judge
                # pass the item would add 1 to k (61/70, still PASS).
                row = replace(row, prog=False, reasons=(c["reason"],))
                deviations += 1
            else:
                assert reason == c["reason"], iid
            regraded += 1
        else:
            assert c["reason"] == "accepted" and c["judge"] == "pass"
            row = S.Row(iid, c["job"], c["size"], c["lang"], True, True, False, False, True, "pass", True, ())
        rows.append(row)
    assert regraded == 64 and len(rows) == 70
    assert deviations == 1
    res = S.score(rows, ids(rows), good_gates())
    assert res.verdict == S.PASS and (res.k, res.n_assigned, res.kmin, res.kfail) == (60, 70, 56, 49)
    assert (res.lanes["commit_message"]["k"], res.lanes["commit_message"]["n"]) == (32, 35)
    assert (res.lanes["pr_description"]["k"], res.lanes["pr_description"]["n"]) == (28, 35)
    assert res.guard_excluded == [] and res.gates["G1_serves"]["error_empty_timeout"] == 0
    assert res.rule_concentration is None                           # 10 failures: the top rule (too_many_plan_bullets) explains 3 of 10
    # the stored per-case CSV is not in the repo; the fixture's recorded ok flags are its qwen_ok column
    assert sum(1 for c in cases.values() if c["ok"]) == 60


def test_fixture_is_small_and_public() -> None:
    p = FIX / "lc5" / "q37_cases.json"
    assert p.stat().st_size < 20_000
    assert "text" not in p.read_text()[:200]
