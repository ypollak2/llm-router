"""LC-4: local-check judge backends, plants gate G3b, consent text, budget cap, quota pressure. Fakes only: no real API or CLI call is made."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import pricing
from llm_router.local_check import budget as B
from llm_router.local_check import consent as C
from llm_router.local_check import judge as J

PASS = '{"verdict": "pass", "defect": null, "quote": "", "reason": "ok"}'
FAIL = '{"verdict": "fail", "defect": "A", "quote": "x", "reason": "wrong change"}'
KEY = "sk-ant-api03-SECRETSECRETSECRET1234567890"


def _priced_model() -> str:
    return sorted(pricing.known_models())[0]


class FakeAnthropic:
    """Records create() kwargs; replies come from ``script`` (str text or Exception)."""

    def __init__(self, script, model_served="served-id-1", usage=(1000, 100)):
        self.script, self.calls, self.served, self.usage = list(script), [], model_served, usage
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(text=r)], model=self.served,
                               usage=SimpleNamespace(input_tokens=self.usage[0], output_tokens=self.usage[1]))


def api_backend(script, model=None, **kw):
    fake = FakeAnthropic(script, **kw)
    return J.ApiBackend(model or _priced_model(), KEY, client_factory=lambda key: fake), fake


class ScriptBackend:
    name, metered, model = "fake", False, "m"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        return r if isinstance(r, J.RawReply) else J.RawReply(text=r, cost_usd=0.01)


def plants(good=12, swapped=12, mutated=6):
    out = []
    for kind, n in (("good", good), ("swapped", swapped), ("mutated", mutated)):
        out += [{"id": f"{kind}{i}", "kind": kind, "text": "msg", "messages": [{"role": "user", "content": "diff"}]} for i in range(n)]
    return out


def replies_for(ps, wrong=(), err=()):
    """right answer per plant kind; ids in ``wrong`` get the opposite verdict, ids in ``err`` an unparseable reply (twice, so the retry fails too)."""
    out = []
    for p in ps:
        right = PASS if p["kind"] == "good" else FAIL
        if p["id"] in err:
            out += ["garbage", "garbage"]
        else:
            out.append((FAIL if right == PASS else PASS) if p["id"] in wrong else right)
    return out


# ---- verdict parsing --------------------------------------------------------------------------------------------------------------------
def test_parse_verdict():
    assert J.parse_verdict(PASS)[0] == "pass"
    assert J.parse_verdict("noise " + FAIL + " tail")[:2] == ("fail", "A")
    assert J.parse_verdict('{"verdict": "fail", "defect": null}')[0] is None      # fail without a defect letter is unparseable
    assert J.parse_verdict("nonsense")[0] is None and J.parse_verdict("")[0] is None
    assert J.parse_verdict("[1]")[0] is None


def test_prompt_carries_input_and_candidate():
    p = J.judge_prompt("THE-DIFF", "THE-ANSWER")
    assert "THE-DIFF" in p and "THE-ANSWER" in p and "NEVER fail an answer for any of those" in p


# ---- API backend -------------------------------------------------------------------------------------------------------------------------
def test_api_backend_pins_model_temp0_records_served_and_exact_cost():
    model = _priced_model()
    be, fake = api_backend([PASS], model=model, model_served="served-xyz", usage=(2000, 50))
    rep = be.complete("PROMPT")
    kw = fake.calls[0]
    assert kw["model"] == model and kw["temperature"] == 0 and kw["messages"][0]["content"] == "PROMPT"
    assert rep.served_model == "served-xyz"
    # the served id is unpriced in this fake, so the exact cost is unknown (None), never 0
    assert rep.cost_usd is None
    be2, _ = api_backend([PASS], model=model, model_served=model, usage=(2000, 50))
    assert be2.complete("p").cost_usd == pricing.cost_usd(model, 2000, 50)


def test_api_backend_requires_model_and_key():
    with pytest.raises(J.JudgeSetupError):
        J.ApiBackend("", KEY)
    with pytest.raises(J.JudgeSetupError):
        J.ApiBackend("m", "")


def test_api_error_does_not_leak_key(capsys, caplog):
    be, _ = api_backend([RuntimeError("401 bad key " + KEY)])
    rep = be.complete("p")
    assert rep.error == "RuntimeError" and KEY not in repr(rep)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err + caplog.text


def test_api_reply_text_is_redacted():
    be, _ = api_backend([PASS + " " + KEY])
    assert KEY not in be.complete("p").text


def test_resolve_api_key_order():
    assert J.resolve_api_key({"ANTHROPIC_API_KEY": " k1 "}, config_key=lambda: "k2") == "k1"
    assert J.resolve_api_key({}, config_key=lambda: "k2") == "k2"
    assert J.resolve_api_key({}, config_key=lambda: "") == ""

    def boom():
        raise RuntimeError("config broken")
    assert J.resolve_api_key({}, config_key=boom) == ""


# ---- claude -p backend (fake CLI script) -------------------------------------------------------------------------------------------------
FAKE_CLI = """#!{py}
import json, os, sys
state = {state!r}
rec = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ), "home": os.environ.get("HOME"), "stdin": sys.stdin.read()}}
n = 0
if os.path.exists(state):
    n = int(open(state).read())
open(state, "w").write(str(n + 1))
open(state + ".rec" + str(n), "w").write(json.dumps(rec))
fails = int(open(state + ".fails").read()) if os.path.exists(state + ".fails") else 0
if n < fails:
    print(json.dumps({{"is_error": True, "result": "boom"}})); sys.exit(1)
print(json.dumps({{"result": {reply!r}, "total_cost_usd": 0.02, "is_error": False, "modelUsage": {{"served-cli-model": {{}}}}}}))
"""


def make_cli(tmp_path, reply=PASS, fails=0):
    state = tmp_path / "state"
    (tmp_path / "state.fails").write_text(str(fails))
    script = tmp_path / "fakeclaude"
    script.write_text(FAKE_CLI.format(py=sys.executable, state=str(state), reply=reply))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), state


def recs(state):
    return [json.loads(p.read_text()) for p in sorted(state.parent.glob("state.rec*"))]


def test_cli_backend_flags_cwd_env_and_real_home(tmp_path):
    exe, state = make_cli(tmp_path)
    environ = {"PATH": os.environ.get("PATH", ""), "HOME": "/real/home/for/auth", "ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "sk-other",
               "LLM_ROUTER_SECRET_THING": "zzz", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-value-123"}
    be = J.ClaudeCliBackend("pinned-model", claude_bin=exe, environ=environ, temp_root=str(tmp_path / "t"))
    (tmp_path / "t").mkdir()
    rep = be.complete("THE PROMPT")
    r = recs(state)[0]
    argv = r["argv"]
    assert rep.error is None and rep.served_model == "served-cli-model" and rep.text == PASS
    for flag in ("-p", "--safe-mode", "--no-session-persistence", "--settings", "--tools", "--output-format"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "pinned-model" and argv[argv.index("--tools") + 1] == "" and argv[argv.index("--output-format") + 1] == "json"
    assert json.loads(Path(argv[argv.index("--settings") + 1]).read_text()) == {"disableAllHooks": True}
    assert r["stdin"] == "THE PROMPT" and "THE PROMPT" not in " ".join(argv)           # prompt on stdin, never argv
    assert r["home"] == "/real/home/for/auth"                                          # real HOME (auth), not a scratch dir
    assert "ANTHROPIC_API_KEY" not in r["env"] and "OPENAI_API_KEY" not in r["env"] and "LLM_ROUTER_SECRET_THING" not in r["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r["env"]                                       # the user's own subscription token is allowed through
    assert Path(r["cwd"]).resolve().parent.parent == (tmp_path / "t").resolve()
    be.cleanup()


def test_cli_secrets_not_in_reply_or_repr(tmp_path):
    exe, state = make_cli(tmp_path, reply="leak oauth-token-value-123 end")
    be = J.ClaudeCliBackend("m", claude_bin=exe, environ={"PATH": os.environ["PATH"], "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-value-123"}, temp_root=str(tmp_path))
    rep = be.complete("p")
    assert "oauth-token-value-123" not in rep.text
    be.cleanup()


def test_cli_cwd_guard_refuses_instruction_file_above(tmp_path):
    exe, state = make_cli(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "AGENTS.md").write_text("rules")
    be = J.ClaudeCliBackend("m", claude_bin=exe, environ={"PATH": os.environ["PATH"]}, temp_root=str(root))
    with pytest.raises(J.JudgeSetupError, match="instruction file"):
        be.complete("p")
    assert not list(tmp_path.glob("state*rec*"))                                      # the CLI was never started
    be.cleanup()


def test_cli_guard_catches_claude_md_and_dot_claude(tmp_path):
    exe, _ = make_cli(tmp_path)
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        root = tmp_path / name.replace(".", "_")
        root.mkdir()
        (root / name).write_text("x")
        with pytest.raises(J.JudgeSetupError):
            J.ClaudeCliBackend("m", claude_bin=exe, environ={}, temp_root=str(root)).complete("p")
    root = tmp_path / "dotc"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("x")
    with pytest.raises(J.JudgeSetupError):
        J.ClaudeCliBackend("m", claude_bin=exe, environ={}, temp_root=str(root)).complete("p")


def test_cli_missing_binary_and_model(monkeypatch):
    monkeypatch.setattr(J.shutil, "which", lambda name: None)
    with pytest.raises(J.JudgeSetupError, match="not on PATH"):
        J.ClaudeCliBackend("m")
    with pytest.raises(J.JudgeSetupError):
        J.ClaudeCliBackend("", claude_bin="/bin/true")


def test_cli_retries_once_on_cli_error_then_succeeds(tmp_path):
    exe, state = make_cli(tmp_path, fails=1)
    be = J.ClaudeCliBackend("m", claude_bin=exe, environ={"PATH": os.environ["PATH"]}, temp_root=str(tmp_path))
    bud = B.Budget(metered=False)
    o = J.judge_answer(be, bud, "p")
    assert o.verdict == "pass" and o.attempts == 2 and bud.calls == 2 and len(recs(state)) == 2
    be.cleanup()


def test_cli_two_failures_is_judge_error_not_a_third_call(tmp_path):
    exe, state = make_cli(tmp_path, fails=5)
    be = J.ClaudeCliBackend("m", claude_bin=exe, environ={"PATH": os.environ["PATH"]}, temp_root=str(tmp_path))
    o = J.judge_answer(be, B.Budget(metered=False), "p")
    assert o.verdict == "judge_error" and o.attempts == 2 and len(recs(state)) == 2
    be.cleanup()


def test_cli_timeout_is_an_error_reply(tmp_path):
    import subprocess

    def runner(*a, **k):
        raise subprocess.TimeoutExpired("claude", 240)
    be = J.ClaudeCliBackend("m", claude_bin="/bin/true", runner=runner, environ={}, temp_root=str(tmp_path))
    assert be.complete("p").error == "TimeoutExpired"
    assert J.CLI_TIMEOUT_S == 240
    be.cleanup()


# ---- judge_answer + budget ---------------------------------------------------------------------------------------------------------------
def test_retry_once_on_unparseable_then_ok():
    be = ScriptBackend(["garbage", PASS])
    bud = B.Budget()
    o = J.judge_answer(be, bud, "p")
    assert (o.verdict, o.attempts, bud.calls) == ("pass", 2, 2)
    assert o.cost_usd == pytest.approx(0.02)


def test_unknown_cost_stays_unknown():
    be = ScriptBackend([J.RawReply(text=PASS, cost_usd=None)])
    bud = B.Budget()
    o = J.judge_answer(be, bud, "p")
    assert o.cost_usd is None and bud.unpriced_calls == 1 and bud.spent_usd == 0.0


def test_budget_call_cap_stops_before_the_call():
    be = ScriptBackend([PASS] * 10)
    bud = B.Budget(cap_calls=2)
    J.judge_answer(be, bud, "p")
    J.judge_answer(be, bud, "p")
    with pytest.raises(B.BudgetExhausted, match="call cap"):
        J.judge_answer(be, bud, "p")
    assert len(be.prompts) == 2


def test_budget_usd_cap_and_lookahead():
    bud = B.Budget(cap_usd=0.05, est_call_usd=0.03)
    bud.check()
    bud.record(0.03)
    with pytest.raises(B.BudgetExhausted, match="could exceed"):
        bud.check()
    bud2 = B.Budget(cap_usd=0.05)
    bud2.record(0.05)
    with pytest.raises(B.BudgetExhausted, match="USD cap"):
        bud2.check()


def test_subscription_budget_ignores_usd_but_caps_calls():
    bud = B.Budget(cap_usd=0.01, cap_calls=3, metered=False)
    for _ in range(3):
        bud.check()
        bud.record(5.0)
    assert bud.spent_usd == 0.0
    with pytest.raises(B.BudgetExhausted):
        bud.check()


def test_defaults_are_3_usd_120_calls():
    b = B.Budget()
    assert (b.cap_usd, b.cap_calls) == (3.0, 120)


def test_retry_attempt_is_budget_checked():
    be = ScriptBackend(["garbage", PASS])
    with pytest.raises(B.BudgetExhausted):
        J.judge_answer(be, B.Budget(cap_calls=1), "p")


def test_estimate_comes_from_pricing_source():
    m = _priced_model()
    est = B.per_call_estimate_usd(m)
    assert est == pytest.approx(pricing.cost_usd(m, B.EST_INPUT_TOKENS, B.EST_OUTPUT_TOKENS) * B.EST_MARGIN)
    assert B.estimate_run_usd(m) == pytest.approx(est * 100)
    assert B.per_call_estimate_usd("definitely-not-a-priced-model-id") is None
    assert B.estimate_run_usd("definitely-not-a-priced-model-id") is None


# ---- G3b ---------------------------------------------------------------------------------------------------------------------------------
def rows_for(good_ok=12, sw_ok=12, mu_ok=6, errors=None):
    rows = []
    for kind, n, ok, bad in (("good", 12, good_ok, "fail"), ("swapped", 12, sw_ok, "pass"), ("mutated", 6, mu_ok, "pass")):
        right = "pass" if kind == "good" else "fail"
        rows += [J.PlantRow(f"{kind}{i}", kind, right if i < ok else bad) for i in range(n)]
    for k, v in (errors or {}).items():
        rows[k].verdict = v
    return rows


@pytest.mark.parametrize("args,status", [
    (dict(), "PASS"),
    (dict(good_ok=11, sw_ok=11, mu_ok=5), "PASS"),                  # exact thresholds
    (dict(good_ok=10), "FAIL"),
    (dict(sw_ok=10), "FAIL"),
    (dict(mu_ok=4), "FAIL"),
])
def test_g3b_boundaries(args, status):
    st, counts, reasons, rate = J.evaluate_g3b(rows_for(**args))
    assert st == status and (status == "PASS") == (not reasons)


def test_g3b_error_rate_boundary():
    # 1 error of 30 = 3.3% <= 5% (with 11/12 good still met): PASS; 2 of 30 = 6.7% > 5%: FAIL even if each kind still meets its minimum
    st, _, _, rate = J.evaluate_g3b(rows_for(errors={0: "judge_error"}))
    assert st == "PASS" and rate == pytest.approx(1 / 30)
    st, _, reasons, rate = J.evaluate_g3b(rows_for(errors={0: "judge_error", 12: "judge_error"}))
    assert st == "FAIL" and any("judge errors" in r for r in reasons)


def test_g3b_empty_and_wrong_counts_do_not_pass():
    st, _, reasons, rate = J.evaluate_g3b([])
    assert st == "FAIL" and rate is None and "no plants judged" in reasons
    st, _, reasons, _ = J.evaluate_g3b(rows_for()[:-1])
    assert st == "FAIL" and any("plants judged" in r for r in reasons)


def test_run_g3b_pass_end_to_end():
    ps = plants()
    be = ScriptBackend(replies_for(ps))
    bud = B.Budget()
    r = J.run_g3b(be, bud, ps)
    assert r.status == "PASS" and r.n_judged == 30 and r.calls == 30 and r.errors == 0
    assert r.counts["good"] == {"n": 12, "correct": 12, "errors": 0}


def test_run_g3b_fail_on_one_wrong_too_many():
    ps = plants()
    r = J.run_g3b(ScriptBackend(replies_for(ps, wrong={"good0", "good1"})), B.Budget(), ps)
    assert r.status == "FAIL" and r.counts["good"]["correct"] == 10


def test_run_g3b_judge_errors_counted_and_fail():
    ps = plants()
    r = J.run_g3b(ScriptBackend(replies_for(ps, err={"good0", "swapped0"})), B.Budget(cap_calls=200), ps)
    assert r.errors == 2 and r.status == "FAIL" and r.error_rate == pytest.approx(2 / 30)


def test_run_g3b_budget_stop_is_inconclusive_never_pass():
    ps = plants()
    r = J.run_g3b(ScriptBackend(replies_for(ps)), B.Budget(cap_calls=10), ps)
    assert r.status == "INCONCLUSIVE" and r.n_judged == 10 and "call cap" in r.stop_reason


def test_run_g3b_records_served_models_and_cost():
    ps = plants()
    reps = [J.RawReply(text=t, served_model="served-1", cost_usd=0.01) for t in replies_for(ps)]
    r = J.run_g3b(ScriptBackend(reps), B.Budget(), ps)
    assert r.served_models == ["served-1"] and r.cost_usd == pytest.approx(0.30)


# ---- quota ---------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("v,force,action", [(0.50, False, "ok"), (0.80, False, "ok"), (0.85, False, "warn"), (0.95, False, "warn"), (0.96, False, "refuse"), (0.96, True, "warn")])
def test_quota_thresholds(v, force, action):
    d = B.check_subscription_quota(force=force, reader=lambda: (v, "ok", 1.0))
    assert d.action == action and d.pressure == v


@pytest.mark.parametrize("reading", [(None, "unknown", None), (0.99, "stale", 1.0)])
def test_quota_unknown_is_not_zero_and_not_refused(reading):
    d = B.check_subscription_quota(reader=lambda: reading)
    assert d.action == "unknown" and d.pressure is None and "could not be read" in d.message


# ---- consent -------------------------------------------------------------------------------------------------------------------------------
def test_consent_text_api():
    t = C.build_consent_text(C.ROUTE_API, "judge-model-x", estimate_usd=1.84)
    for must in ("judge-model-x", "ANTHROPIC_API_KEY", "public benchmark task text", "your local model's answers", "Anthropic",
                 "$1.84", "~100 requests", "$3.00", "120 requests", "Proceed? [y/N]", "not sent"):
        assert must in t, must


def test_consent_text_api_unpriced():
    t = C.build_consent_text(C.ROUTE_API, "m", estimate_usd=None)
    assert "cannot be estimated" in t and "NO dollar cap" in t and "$3.00" not in t     # call-capped only: no dollar claim
    assert "120 requests" in t


def test_consent_priced_api_states_dollar_cap():
    t = C.build_consent_text(C.ROUTE_API, "m", estimate_usd=1.0)
    assert "$3.00 or 120 requests" in t and "resumed" not in t


def test_consent_text_subscription():
    t = C.build_consent_text(C.ROUTE_SUBSCRIPTION, "m", quota_note="Your Claude plan is at 85% of its limit; x")
    assert "counts toward your plan limit" in t and "~100 requests" in t and "85%" in t
    assert "No API charges" not in t and "uses your Claude subscription" in t and "billed to that key" in t
    assert "ANTHROPIC_API_KEY" not in t and "$" not in t


def test_ask_consent_variants():
    out: list[str] = []
    assert C.ask_consent("t", yes=True, print_fn=out.append) is True
    assert C.ask_consent("t", interactive=False, print_fn=out.append) is False and "--yes" in out[-1]
    assert C.ask_consent("t", input_fn=lambda _: "Y", print_fn=out.append) is True
    assert C.ask_consent("t", input_fn=lambda _: "", print_fn=out.append) is False
    assert C.ask_consent("t", input_fn=lambda _: "no", print_fn=out.append) is False

    def eof(_):
        raise EOFError
    assert C.ask_consent("t", input_fn=eof, print_fn=out.append) is False


# ---- LC-4b -------------------------------------------------------------------------------------------------------------------------------
def test_default_client_ignores_anthropic_base_url_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9999")
    client = J.ApiBackend._default_client(KEY)
    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_custom_provider_base_url_refused():
    with pytest.raises(J.JudgeSetupError, match="custom base URL"):
        J.ApiBackend(_priced_model(), KEY, client_factory=lambda k: None, configured_base_url="http://localhost:4000")
    with pytest.raises(J.JudgeSetupError):
        J.ApiBackend(_priced_model(), KEY, client_factory=lambda k: None, configured_base_url="https://api.anthropic.com.evil.example")
    J.ApiBackend(_priced_model(), KEY, client_factory=lambda k: None, configured_base_url="https://api.anthropic.com")   # the real endpoint is fine
    J.ApiBackend(_priced_model(), KEY, client_factory=lambda k: None, configured_base_url="")


def test_unpriced_model_refused_on_api_route():
    with pytest.raises(J.JudgeSetupError, match="no price"):
        J.ApiBackend("definitely-not-a-priced-model-id", KEY, client_factory=lambda k: None)
    with pytest.raises(J.JudgeSetupError, match="no price"):
        J.ApiBackend("ollama/llama3.2", KEY, client_factory=lambda k: None)


def test_unpriced_metered_call_stops_the_budget():
    bud = B.Budget()
    bud.record(None)
    with pytest.raises(B.BudgetExhausted, match="could not be priced"):
        bud.check()
    B.Budget(metered=False).record(None)          # subscription: nothing to price


def test_empty_candidate_fails_without_a_call():
    be = ScriptBackend([])
    bud = B.Budget()
    for empty in ("", "   \n"):
        o = J.judge_candidate(be, bud, "diff", empty)
        assert o.verdict == "fail" and o.attempts == 0 and o.cost_usd == 0.0
    assert be.prompts == [] and bud.calls == 0


def test_run_g3b_empty_plant_is_fail_no_call():
    ps = plants()
    ps[12]["text"] = ""                                   # swapped0: expected fail, judged without a call
    replies = replies_for(ps)
    del replies[12]
    be = ScriptBackend(replies)
    r = J.run_g3b(be, B.Budget(), ps)
    assert r.status == "PASS" and r.n_judged == 30 and r.calls == 29 and len(be.prompts) == 29


def test_cli_env_allow_list_has_proxy_and_ca_vars():
    env = {k: "v" for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")}
    got = J.cli_env(env)
    assert set(got) == {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}


def test_cli_context_manager_removes_temp_dir(tmp_path):
    exe, _ = make_cli(tmp_path)
    root = tmp_path / "t"
    root.mkdir()
    with J.ClaudeCliBackend("m", claude_bin=exe, environ={"PATH": os.environ["PATH"]}, temp_root=str(root)) as be:
        be.complete("p")
        assert list(root.iterdir())
    assert not list(root.iterdir())
    be.cleanup()                                          # idempotent


def test_cli_cost_is_notional_field(tmp_path):
    exe, _ = make_cli(tmp_path)
    with J.ClaudeCliBackend("m", claude_bin=exe, environ={"PATH": os.environ["PATH"]}, temp_root=str(tmp_path)) as be:
        rep = be.complete("p")
        assert rep.cost_usd is None and rep.notional_cost_usd == 0.02
        bud = B.Budget(metered=False)
        o = J.judge_answer(be, bud, "p")
        assert o.cost_usd is None and o.notional_cost_usd == pytest.approx(0.02) and bud.spent_usd == 0.0


def test_recheck_quota_between_batches():
    assert B.recheck_quota(reader=lambda: (0.5, "ok", 1.0)).action == "ok"
    assert B.recheck_quota(reader=lambda: (0.9, "ok", 1.0)).action == "warn"
    assert B.recheck_quota(reader=lambda: (None, "unknown", None)).action == "unknown"
    with pytest.raises(B.BudgetExhausted, match="quota"):
        B.recheck_quota(reader=lambda: (0.97, "ok", 1.0))
    assert B.recheck_quota(force=True, reader=lambda: (0.97, "ok", 1.0)).action == "warn"


def test_served_model_matches_exact_alias_and_mismatch():
    m = _priced_model()
    assert J.served_model_matches(m, m) == (True, "ok")
    assert J.served_model_matches(m + "," + m, m)[0]
    ok, why = J.served_model_matches("some-other-model", m)
    assert not ok and "does not match" in why
    assert not J.served_model_matches("", m)[0] and not J.served_model_matches(m, "")[0]
    # alias: any other spelling the price table resolves to the same canonical id
    canon = pricing.resolve(m)
    assert J.served_model_matches("anthropic/" + m, m)[0] == (pricing.resolve("anthropic/" + m) == canon)
