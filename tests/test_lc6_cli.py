"""LC-6: the `llm-router local-check` CLI. Fake runtime adapter, fake judge, injected environment: no model, no network, no real HOME state."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router.commands import local_check as LCC
from llm_router.local_check import attempts as A, runner as R, runtimes, score as S, session as SE
from llm_router.local_check.budget import BudgetExhausted
from llm_router.local_check.judge import JudgeSetupError
from tests.test_lc5_scoring import BANK, Clock, FakeAdapter, FakeJudge, invalid_adapter

MODEL = "fake-model:1b"


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """LC-10i: the CLI writes local_routing.json and reads the model inventory under the llm-router home; keep both in the test dir."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))


class Sink:
    def __init__(self):
        self.out: list[str] = []
        self.err: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.out)

    @property
    def etext(self) -> str:
        return "\n".join(self.err)


def deps(tmp_path: Path, adapter: FakeAdapter | None = None, judge: FakeJudge | None = None, *, env: dict | None = None, claude: bool = True,
         interactive: bool = False, answers: list[str] | None = None, endpoints: list | None = None, clock: Clock | None = None,
         quota: tuple = (0.10, "ok", 0.0), **kw) -> tuple[LCC.Deps, Sink, SimpleNamespace]:
    sink = Sink()
    adapter = adapter or FakeAdapter()
    judge = judge or FakeJudge()
    seen = SimpleNamespace(adapter=adapter, judge=judge, discover_calls=[], adapter_kw=[], api_calls=[], cli_calls=[])
    eps = [runtimes.Endpoint("ollama", "http://127.0.0.1:11434", [MODEL])] if endpoints is None else endpoints
    ans = list(answers or [])

    def discover(endpoint=None, **k):
        seen.discover_calls.append((endpoint, k))
        return eps

    def make_adapter(ep, **k):
        seen.adapter_kw.append(k)
        return adapter

    def api_backend(model, key, base):
        seen.api_calls.append((model, key, base))
        judge.metered = True
        return judge

    def cli_backend(model, claude_bin):
        seen.cli_calls.append((model, claude_bin))
        judge.metered = False
        return judge

    d = LCC.Deps(environ=env if env is not None else {}, which=lambda n: "/usr/bin/claude" if (claude and n == "claude") else None,
                 config_key=lambda: "", interactive=interactive, input_fn=lambda p: ans.pop(0), out=sink.out.append, err=sink.err.append,
                 discover=discover, make_adapter=make_adapter, load_bank=lambda: BANK, hardware=lambda: {"total_gb": 32, "usable_gb": 20, "gpu": None, "arch": "arm64"},
                 quota_reader=lambda: quota, proxy_ports=lambda: {8787, 8797}, api_backend=api_backend, cli_backend=cli_backend,
                 now=clock or Clock(), runs_root=tmp_path / "runs", eligibility_path=tmp_path / "elig.json", **kw)
    return d, sink, seen


def run(argv: list[str], d: LCC.Deps) -> int:
    return LCC.cmd_local_check(argv, d)


def lanes(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "elig.json").read_text())[MODEL]["text_job_local"]


# ------------------------------------------------------------------------------------------ dry run, consent
def test_dry_run_makes_zero_model_calls_and_writes_nothing(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path)
    assert run(["--dry-run", "--model", MODEL], d) == LCC.EXIT_OK
    assert seen.adapter.calls == 0 and seen.judge.calls == 0
    assert not (tmp_path / "runs").exists() and not (tmp_path / "elig.json").exists()
    t = sink.text
    for needle in (f"Model:      {MODEL}", "sha256:aaa", "Hardware:   RAM 32 GB, 25.6 GB usable for a model", "70 of 70", "Judge: your Claude subscription", "Cap: the run stops at 120 requests",
                   "Proceed", "Dry run: no model call was made"):
        assert needle in t or needle.replace("Proceed", "What is sent to Anthropic") in t, needle
    assert "Proceed? [y/N]" not in t              # a dry run never asks


def test_dry_run_with_api_key_shows_cost_estimate_and_cap(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-test-key-123456"})
    assert run(["--dry-run", "--model", MODEL], d) == 0
    assert "metered API" in sink.text and "~$" in sink.text and "$3.00" in sink.text and seen.judge.calls == 0
    assert "sk-ant-test-key-123456" not in sink.text + sink.etext


def test_non_tty_without_yes_refuses_and_runs_nothing(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, interactive=False)
    assert run(["--model", MODEL], d) == LCC.EXIT_NOT_PASSED
    assert "--yes" in sink.text and seen.adapter.calls == 0 and seen.judge.calls == 0 and not (tmp_path / "elig.json").exists()


def test_tty_n_declines_and_y_runs(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, interactive=True, answers=["n"])
    assert run(["--model", MODEL], d) == LCC.EXIT_NOT_PASSED and seen.adapter.calls == 0 and "Not started." in sink.text
    d, sink, seen = deps(tmp_path / "b", interactive=True, answers=["y"])
    assert run(["--model", MODEL], d) == LCC.EXIT_OK and seen.adapter.calls == 70


# ------------------------------------------------------------------------------------------ a full pass, with the verdict screen
def test_pass_run_prints_verdict_lane_table_gates_and_records(tmp_path: Path) -> None:
    fails = {i.id: "judgefail" for i in BANK.draw(1)[:10]}
    d, sink, seen = deps(tmp_path, FakeAdapter(fails))
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    t = sink.text
    assert "Verdict: PASS" in t and "(n = 70;" in t and "kmin" not in t.split("Verdict:")[0]
    assert "commit_message" in t and "pr_description" in t and "pooled" in t and "Wilson LB" in t
    assert "gates: G0 PASS" in t and "G3b PASS" in t
    assert "recorded: commit_message, pr_description" in t
    assert "later update" not in t and "routing: on for commit_message -- disable with `llm-router local-check disable commit_message`" in t
    assert "judge: " in t and "calls" in t
    for lane in lanes(tmp_path).values():
        assert lane["grade"] == "passed" and lane["source"] == "local_check"
    assert seen.cli_calls == [(BANK.manifest["judge_id"], "/usr/bin/claude")]


def test_fail_run_exit_code_and_explain_histogram(tmp_path: Path) -> None:
    items = BANK.draw(1)
    bad = {i.id: "progfail" for i in items[:30]}              # grader rejects: "Here is the commit message" preamble
    d, sink, _ = deps(tmp_path, FakeAdapter(bad))
    code = run(["--yes", "--model", MODEL], d)
    assert code == LCC.EXIT_NOT_PASSED and ("Verdict: FAIL" in sink.text or "Verdict: NOT_PASSED" in sink.text)
    d2, s2, _ = deps(tmp_path)
    assert run(["explain"], d2) == 0
    t = s2.text
    assert "no item text" in t.lower() or "No item text" in t
    assert "failures" in t
    shown = {i.id for i in items[:30]}
    assert any(iid in t for iid in shown)
    assert "Here is the commit message" not in t              # never any answer or prompt text


def test_explain_without_a_run_and_status_before_and_after(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path)
    assert run(["explain"], d) == 0 and "No finished run" in sink.text
    d, sink, _ = deps(tmp_path)
    assert run(["status"], d) == 0 and "recorded lanes: none" in sink.text and "attempts: none" in sink.text
    d, sink, _ = deps(tmp_path, FakeAdapter({i.id: "judgefail" for i in BANK.draw(1)[:10]}))
    assert run(["--yes", "--model", MODEL], d) == 0
    d, sink, _ = deps(tmp_path)
    assert run(["status"], d) == 0
    assert "passed 60/70" in sink.text and "1 of 2 draws used" in sink.text and "next draw: after" in sink.text


def test_status_lists_an_open_run(tmp_path: Path) -> None:
    d, _, _ = deps(tmp_path, invalid_adapter())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_INVALID
    d, sink, _ = deps(tmp_path)
    run(["status"], d)
    assert "open run" in sink.text and "re-run to resume" in sink.text


def test_invalid_run_shows_gates_only_exit_13(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, invalid_adapter())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_INVALID
    assert "INVALID (nothing recorded)" in sink.text and "G1 FAIL" in sink.text and "k/n" not in sink.text
    assert not (tmp_path / "elig.json").exists()


# ------------------------------------------------------------------------------------------ route selection (D-260, D-264)
PORTS = {8787, 8797}


def sel(env: dict, claude: bool = True, **kw) -> LCC.Route:
    return LCC.select_route(env, lambda n: "/bin/claude" if claude else None, PORTS, config_key=lambda: "", **kw)


def test_key_means_api_route_else_claude_cli_else_nothing() -> None:
    assert sel({"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa"}).kind == "api"
    assert sel({}).kind == "subscription"
    r = sel({}, claude=False)
    assert r.kind == "" and "--no-judge" in r.error
    assert sel({"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa"}, want="subscription").kind == "subscription"
    assert "no Anthropic API key" in sel({}, want="api").error


@pytest.mark.parametrize("url", ["http://127.0.0.1:8787", "http://localhost:8797/", "http://127.0.0.1:8787/v1"])
def test_own_proxy_base_url_goes_direct(url: str) -> None:
    r = sel({"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa", "ANTHROPIC_BASE_URL": url})
    assert r.kind == "api" and r.configured_base_url == "" and r.notes and "own local proxy" in r.notes[0]


def test_proxy_port_comes_from_the_sentinel_and_env_not_a_hardcoded_port(tmp_path: Path, monkeypatch) -> None:
    from llm_router import proxy_default as pd
    monkeypatch.setattr(pd, "read_sentinel", lambda: {"port": 9911, "upstream_port": 9912})
    ports = LCC.own_proxy_ports({"LLM_ROUTER_PROXY_PORT": "9913"})
    assert {9911, 9912, 9913, pd.DEFAULT_PORT, pd.DEFAULT_UPSTREAM_PORT} <= ports
    assert LCC.points_at_own_proxy("http://127.0.0.1:9912", ports) and not LCC.points_at_own_proxy("http://127.0.0.1:9999", ports)
    assert not LCC.points_at_own_proxy("http://example.com:8787", ports)          # same port, not loopback


@pytest.mark.parametrize("url", ["https://gateway.corp.example/anthropic", "http://127.0.0.1:4000", "http://10.0.0.5:8787"])
def test_custom_base_url_refuses_the_api_route_and_suggests_subscription(url: str) -> None:
    r = sel({"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa", "ANTHROPIC_BASE_URL": url})
    assert r.kind == "" and "subscription" in r.error and "refused" in r.error


def test_custom_base_url_cli_exit_code_and_no_calls(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa", "ANTHROPIC_BASE_URL": "https://gateway.corp.example"})
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NO_JUDGE_ROUTE
    assert "subscription" in sink.etext and seen.adapter.calls == 0 and seen.api_calls == []
    d, sink, seen = deps(tmp_path / "b", env={"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa", "ANTHROPIC_BASE_URL": "https://gateway.corp.example"})
    assert run(["--yes", "--model", MODEL, "--judge-route", "subscription"], d) == 0        # the suggested way out works
    assert seen.cli_calls and seen.api_calls == []


def test_own_proxy_run_passes_empty_base_url_to_the_api_backend(tmp_path: Path) -> None:
    d, _, seen = deps(tmp_path, FakeAdapter({i.id: "judgefail" for i in BANK.draw(1)[:10]}),
                      env={"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})
    assert run(["--yes", "--model", MODEL], d) == 0
    assert seen.api_calls and all(base == "" for _, _, base in seen.api_calls) and seen.cli_calls == []


def test_no_route_without_no_judge_fails_and_no_judge_still_works(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, claude=False)
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NO_JUDGE_ROUTE and "--no-judge" in sink.etext and seen.adapter.calls == 0
    d, sink, seen = deps(tmp_path, claude=False)
    assert run(["--yes", "--no-judge", "--model", MODEL], d) in (LCC.EXIT_OK, LCC.EXIT_NOT_PASSED)
    assert seen.judge.calls == 0 and seen.adapter.calls == 70 and "never a PASS" in sink.text and "Verdict: PROBE" in sink.text
    assert not (tmp_path / "elig.json").exists()


def test_unpriced_judge_model_is_refused_before_consent(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-aaaaaaaa"})

    def boom(model, key, base):
        raise JudgeSetupError("judge model has no price in the price table")
    d.api_backend = boom
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NO_JUDGE_ROUTE and "no price" in sink.etext and seen.adapter.calls == 0


# ------------------------------------------------------------------------------------------ subscription quota
@pytest.mark.parametrize("pressure,code,needle", [(0.50, 0, ""), (0.85, 0, "85%"), (0.97, LCC.EXIT_QUOTA, "97%")])
def test_subscription_quota_warn_and_refuse(tmp_path: Path, pressure: float, code: int, needle: str) -> None:
    d, sink, seen = deps(tmp_path, FakeAdapter({i.id: "judgefail" for i in BANK.draw(1)[:10]}), quota=(pressure, "ok", 0.0))
    assert run(["--yes", "--model", MODEL], d) == code
    assert needle in sink.text + sink.etext
    if code == LCC.EXIT_QUOTA:
        assert seen.adapter.calls == 0


def test_quota_force_overrides_the_refusal(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, FakeAdapter({i.id: "judgefail" for i in BANK.draw(1)[:10]}), quota=(0.97, "ok", 0.0))
    assert run(["--yes", "--force", "--model", MODEL], d) == 0 and seen.adapter.calls == 70


# ------------------------------------------------------------------------------------------ scope floor
def test_below_50_items_fit_refuses_with_raise_num_ctx_advice(tmp_path: Path) -> None:
    sizes = sorted(len(i.task) for i in BANK.draw(1))
    cap = sizes[40]                                            # only ~41 items fit
    d, sink, seen = deps(tmp_path, FakeAdapter(max_chars=cap))
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_REFUSED
    assert "num_ctx" in sink.etext and "at least 50" in sink.etext and seen.adapter.calls == 0 and seen.judge.calls == 0


def test_dry_run_reports_the_same_refusal(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, FakeAdapter(max_chars=sorted(len(i.task) for i in BANK.draw(1))[40]))
    assert run(["--dry-run", "--model", MODEL], d) == LCC.EXIT_REFUSED and "num_ctx" in sink.etext and seen.adapter.calls == 0


def test_quick_profile_narrows_scope(tmp_path: Path) -> None:
    standard, _, _ = deps(tmp_path / "s")
    s1 = Sink()
    standard.out = s1.out.append
    run(["--dry-run", "--model", MODEL], standard)
    d, s2, _ = deps(tmp_path / "q")
    code = run(["--dry-run", "--profile", "quick", "--model", MODEL], d)
    fit = sum(1 for i in BANK.draw(1) if len(i.task) <= LCC.QUICK_MAX_CHARS)
    if fit >= 50:
        assert code == 0 and f"{fit} of 70" in s2.text and "profile quick" in s2.text
    else:
        assert code == LCC.EXIT_REFUSED


# ------------------------------------------------------------------------------------------ runtime / model selection, remote
def test_allow_remote_needs_an_explicit_endpoint(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path)
    assert run(["--allow-remote", "--dry-run"], d) == LCC.EXIT_USAGE and "--endpoint" in sink.etext


def test_remote_endpoint_refused_without_allow_remote_and_passed_with_it(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path)

    def discover(endpoint=None, **k):
        seen.discover_calls.append((endpoint, k))
        if not k.get("allow_remote"):
            raise runtimes.RuntimeError_("non-loopback endpoint")
        return [runtimes.Endpoint("openai", "http://gpu-box:8000", [MODEL])]
    d.discover = discover
    assert run(["--endpoint", "http://gpu-box:8000", "--dry-run"], d) == LCC.EXIT_NO_RUNTIME and "--allow-remote" in sink.etext
    assert run(["--endpoint", "http://gpu-box:8000", "--allow-remote", "--dry-run", "--model", MODEL], d) == 0
    assert seen.adapter_kw[-1] == {"allow_remote": True} and "REMOTE" in sink.text


def test_no_runtime_unknown_model_and_ambiguous_model(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, endpoints=[])
    assert run(["--dry-run"], d) == LCC.EXIT_NO_RUNTIME and "No local runtime" in sink.etext
    d, sink, _ = deps(tmp_path)
    assert run(["--dry-run", "--model", "nope"], d) == LCC.EXIT_NO_RUNTIME and MODEL in sink.etext
    two = [runtimes.Endpoint("ollama", "http://127.0.0.1:11434", [MODEL, "other:7b"])]
    d, sink, _ = deps(tmp_path, endpoints=two)
    assert run(["--dry-run"], d) == LCC.EXIT_NO_RUNTIME and "pass --model" in sink.etext
    d, sink, _ = deps(tmp_path, endpoints=two, interactive=True, answers=["1"])
    assert run(["--dry-run"], d) == 0 and MODEL in sink.text


def test_unknown_subcommand_and_bad_flag_are_usage_errors(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path)
    assert run(["frobnicate"], d) == LCC.EXIT_USAGE
    assert run(["--profile", "huge"], d) == LCC.EXIT_USAGE


# ------------------------------------------------------------------------------------------ error mapping
class Boom(Exception):
    pass


@pytest.mark.parametrize("exc,code,needle", [
    (runtimes.ProbeFailed("timed out"), LCC.EXIT_PROBE_FAILED, "stopped answering"),
    (R.RunStartError("cannot confirm digest"), LCC.EXIT_PROBE_FAILED, "stopped answering"),
    (R.RunMismatch("different settings"), LCC.EXIT_REFUSED, "--fresh"),
    (BudgetExhausted("call cap reached (120/120)"), LCC.EXIT_BUDGET, "call cap"),
    (JudgeSetupError("an instruction file exists above the judge cwd"), LCC.EXIT_NO_JUDGE_ROUTE, "instruction file"),
    (KeyboardInterrupt(), LCC.EXIT_INTERRUPTED, "re-run to resume"),
])
def test_exceptions_map_to_clear_messages_and_exit_codes(tmp_path: Path, monkeypatch, exc: BaseException, code: int, needle: str) -> None:
    def raiser(chk):
        raise exc
    monkeypatch.setattr(SE, "run_check", raiser)
    d, sink, _ = deps(tmp_path)
    assert run(["--yes", "--model", MODEL], d) == code
    assert needle in sink.text + sink.etext


def test_probe_failure_while_planning(tmp_path: Path) -> None:
    ad = FakeAdapter()

    def bad_digest(model):
        raise runtimes.ProbeFailed("connection refused")
    ad.digest = bad_digest
    d, sink, _ = deps(tmp_path, ad)
    assert run(["--dry-run", "--model", MODEL], d) == LCC.EXIT_PROBE_FAILED and "did not answer a probe" in sink.etext


def test_session_statuses_map_to_exit_codes(tmp_path: Path, monkeypatch) -> None:
    for status, code in (("lock_held", LCC.EXIT_LOCK), ("budget_stop", LCC.EXIT_BUDGET), ("interrupted", LCC.EXIT_INTERRUPTED), ("refused", LCC.EXIT_REFUSED)):
        monkeypatch.setattr(SE, "run_check", lambda chk, s=status: SE.Outcome(s, "why"))
        d, sink, _ = deps(tmp_path)
        assert run(["--yes", "--model", MODEL], d) == code, status


def test_ctrl_c_during_the_local_arm_is_resumable(tmp_path: Path) -> None:
    ad = FakeAdapter()
    real = ad.generate

    def gen(model, prompt, **kw):
        if ad.calls >= 8:
            raise KeyboardInterrupt
        return real(model, prompt, **kw)
    ad.generate = gen
    d, sink, _ = deps(tmp_path, ad)
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_INTERRUPTED and "re-run to resume" in sink.text
    ad.generate = real
    d, sink, _ = deps(tmp_path, ad, FakeJudge())
    assert run(["--yes", "--model", MODEL], d) in (0, 1)                      # the same command resumes the same run dir
    assert len(list((tmp_path / "runs").glob("*/results.jsonl"))) == 1


def test_judge_setup_error_mid_run_keeps_the_local_answers_and_the_next_run_resumes(tmp_path: Path) -> None:
    class CliGuard(FakeJudge):
        def complete(self, prompt):
            if self.answer_calls >= 3 and self.calls > 40:
                raise JudgeSetupError("instruction file above cwd")
            return super().complete(prompt)
    ad = FakeAdapter()
    d, sink, _ = deps(tmp_path, ad, CliGuard())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NO_JUDGE_ROUTE
    calls = ad.calls
    assert calls == 70 and A.AttemptLog(tmp_path / "runs").entries()[-1]["state"] == "open"        # LC-5b: reserved, no strike
    assert A.AttemptLog(tmp_path / "runs").entries()[-1]["strike"] is False
    d, sink, _ = deps(tmp_path, ad, FakeJudge())
    assert run(["--yes", "--model", MODEL], d) == 0 and ad.calls == calls


# ------------------------------------------------------------------------------------------ LC-5b (session)
def sess(tmp_path: Path, adapter=None, judge=None, clock=None, **kw) -> SE.Outcome:
    return SE.run_check(SE.Check(model=MODEL, runtime="fake", bank=BANK, adapter=adapter or FakeAdapter(), runs_root=tmp_path / "runs",
                                 backend=judge or FakeJudge(), budget=__import__("llm_router.local_check.budget", fromlist=["Budget"]).Budget(metered=False, cap_calls=500),
                                 now=clock or Clock(), eligibility_path=tmp_path / "elig.json", **kw))


def test_unexpected_exception_after_the_local_arm_holds_the_reservation_without_a_strike(tmp_path: Path) -> None:
    class Crash(FakeJudge):
        def complete(self, prompt):
            if self.answer_calls >= 2:
                raise RuntimeError("kaboom")
            return super().complete(prompt)
    clock, ad = Clock(), FakeAdapter()
    with pytest.raises(RuntimeError):
        sess(tmp_path, ad, Crash(), clock)
    e = A.AttemptLog(tmp_path / "runs").entries()
    assert len(e) == 1 and e[0]["state"] == "open" and e[0]["strike"] is False and "RuntimeError" in e[0]["reason"] and not e[0]["consumed"]
    calls = ad.calls
    o = sess(tmp_path, ad, FakeJudge(), clock)                                # resumes: no new local calls, same run id
    assert o.result.verdict == S.PASS and ad.calls == calls and o.run_id == e[0]["run_id"]


def test_ctrl_c_while_judging_holds_the_reservation(tmp_path: Path) -> None:
    class Stop(FakeJudge):
        def complete(self, prompt):
            if self.answer_calls >= 2:
                raise KeyboardInterrupt
            return super().complete(prompt)
    with pytest.raises(KeyboardInterrupt):
        sess(tmp_path, FakeAdapter(), Stop(), Clock())
    e = A.AttemptLog(tmp_path / "runs").entries()
    assert e[0]["state"] == "open" and e[0]["strike"] is False and e[0]["reason"] == "interrupted"


@pytest.mark.parametrize("how", ["deleted", "emptied"])
def test_missing_or_empty_run_dir_of_an_open_reservation_is_a_discard_strike(tmp_path: Path, how: str) -> None:
    import shutil
    clock = Clock()
    o1 = sess(tmp_path, invalid_adapter(), clock=clock)
    assert o1.result.verdict == S.INVALID
    run_dir = tmp_path / "runs" / o1.run_id
    if how == "deleted":
        shutil.rmtree(run_dir)
    else:
        (run_dir / "results.jsonl").write_text("")
    o2 = sess(tmp_path, FakeAdapter(), clock=clock)
    assert o2.run_id != o1.run_id and o2.result.verdict == S.PASS and o2.draw == 1
    assert any("discarded" in n and "a strike" in n for n in o2.notes)
    es = A.AttemptLog(tmp_path / "runs").entries()
    assert [e["state"] for e in es if e["run_id"] == o1.run_id] == ["open", "discarded"] and es[1]["strike"]
    assert A.AttemptLog(tmp_path / "runs").strikes(es[0]["config_key"]) == 2        # the INVALID and the deletion: the third would burn the draw


def test_deleting_the_run_dir_is_not_a_free_fresh_third_strike_burns_the_draw(tmp_path: Path) -> None:
    import shutil
    clock = Clock()
    o1 = sess(tmp_path, invalid_adapter(), clock=clock)                        # strike 1
    shutil.rmtree(tmp_path / "runs" / o1.run_id)
    o2 = sess(tmp_path, invalid_adapter(), clock=clock)                        # strike 2 (deletion), new run invalid: strike 3 -> burnt
    assert o2.result.verdict == S.INVALID
    last = A.AttemptLog(tmp_path / "runs").entries()[-1]
    assert last["state"] == "burnt" and last["consumed"] is True


def test_reservation_expiry_after_7_days_is_not_a_strike(tmp_path: Path) -> None:
    clock = Clock()
    o1 = sess(tmp_path, invalid_adapter(), clock=clock)
    clock.t += A.RESERVATION_TTL_S + 1
    o2 = sess(tmp_path, FakeAdapter(), clock=clock)
    assert o2.run_id != o1.run_id and o2.notes == []
    assert A.AttemptLog(tmp_path / "runs").strikes(A.AttemptLog(tmp_path / "runs").entries()[0]["config_key"]) == 1       # only the INVALID


def test_text_job_lines_status_inconclusive_reads_as_failed(tmp_path: Path) -> None:
    """The removed dead branch: only not_passed has its own wording; any other status falls back to 'failed'."""
    from llm_router.resolver import eligibility_store as ES
    p = tmp_path / "e.json"
    lane = {"k": 40, "n": 70, "verdict": "NOT_PASSED"}
    prov = {"bank_version": "v1", "bank_sha256": "a" * 64, "settings_hash": "h", "date": "2026-10-20"}
    ES.record_local_check(MODEL, {"commit_message": lane}, prov, path=p)
    assert "not passed 40/70" in "\n".join(ES.text_job_lines(path=p))


# ------------------------------------------------------------------------------------------ wiring
def test_cli_main_dispatches_local_check_and_help_is_inert(monkeypatch, capsys) -> None:
    from llm_router import cli
    assert "local-check" in cli._OWN_HELP_ANYWHERE and "local-check" in cli.__doc__
    called = []
    monkeypatch.setattr(LCC, "cmd_local_check", lambda argv, deps=None: called.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["llm-router", "local-check", "--dry-run"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and called == [["--dry-run"]]
    monkeypatch.undo()
    assert LCC.cmd_local_check(["--help"], LCC.Deps(environ={})) == 0
    assert "--dry-run" in capsys.readouterr().out
