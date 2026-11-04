"""W10: the routing verdict inside the real hooks (a subprocess per scenario, a real HTTP server standing in
for Ollama, a real executable standing in for ``codex``).

* ``auto-route.py``  every real prompt gets a verdict; in ``on`` mode a confident opus verdict quiets the hint,
  in ``shadow`` nothing changes, with no verdict nothing changes (fail-open).
* ``agent-route.py`` every routed Agent spawn gets a verdict; in ``on`` mode a confident opus verdict keeps the
  spawn on Claude (no Codex run), the unit stays countable as ``unsuitable`` with ``reason_code=verdict_claude``.
* ``session-start.py`` warms the decision model instead of the generic model.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from llm_router import door_verdict as dv
from tests._bind_support import (
    AGENT_ROUTE,
    HOOKS_SRC,
    SUITABLE_PROMPT,
    agent_payload,
    decision_of,
    hook_env,
    init_repo,
    ledger_rows,
    run_hook,
    stub_calls,
    write_codex_stub,
)
from tests._fake_ollama import FakeOllama

pytestmark = [pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell stub"),
              pytest.mark.timeout(180)]

AUTO_ROUTE = HOOKS_SRC / "auto-route.py"
OPUS = {"haiku": 0.05, "sonnet": 0.15, "opus": 0.8}
SONNET = {"haiku": 0.1, "sonnet": 0.7, "opus": 0.2}
ANSWER = "STUB-CODEX-ANSWER-9d2f: the pager drops the last row because the loop stops one early."


@pytest.fixture
def fake():
    f = FakeOllama()
    yield f
    f.close()


def _classifier_env(url: str, mode: str) -> dict[str, str]:
    return {
        "LLM_ROUTER_LOCAL_CLASSIFIER": mode,
        "LLM_ROUTER_CLASSIFIER_BACKEND": "systemone",
        "LLM_ROUTER_DECISION_MODEL": "nimble:9b",
        "LLM_ROUTER_OLLAMA_URL": url,
    }


def _verdict_rows(home: Path) -> list[dict]:
    p = home / ".llm-router" / dv.LEDGER
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


# ===================================================================================== auto-route (prompts)

PROMPT = "What does os.path.join do on Windows when the second argument is absolute?"


def _prompt(tmp_path, mode: str, url: str, prompt: str = PROMPT, **extra):
    env = hook_env(tmp_path, LLM_ROUTER_QA_ROUTING="on", LLM_ROUTER_ENFORCE="suggest",
                   LLM_ROUTER_DIRECT_EXECUTION="0", LLM_ROUTER_DISABLE_LLM_CLASSIFIERS="1",
                   **_classifier_env(url, mode), **extra)
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "door-s1", "cwd": str(tmp_path)}
    proc = run_hook(AUTO_ROUTE, payload, env)
    assert proc.returncode == 0, proc.stderr[-400:]
    return proc


SHOWN = "SUGGESTED"                     # the suggest-mode hint
QUIET = "OBSERVATION [shadow]"          # the passive display a quiet task gets


def test_off_is_todays_hook_and_makes_no_call(tmp_path, fake):
    proc = _prompt(tmp_path, "off", fake.url)
    assert SHOWN in proc.stdout                         # the static-head hint, as before
    assert fake.calls == 0 and _verdict_rows(tmp_path) == []


def test_shadow_logs_one_verdict_for_the_prompt_and_changes_nothing(tmp_path, fake):
    fake.probs = OPUS
    off = _prompt(tmp_path, "off", fake.url)
    proc = _prompt(tmp_path, "shadow", fake.url)
    assert fake.calls == 1
    rows = _verdict_rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert (r["hook_kind"], r["mode"], r["tier"], r["door"], r["applied"]) == ("prompt", "shadow", "opus", "claude", False)
    assert r["session_id"] == "door-s1" and r["source"] == "llm" and r["resident"] is False
    assert PROMPT not in json.dumps(r)
    assert SHOWN in proc.stdout and SHOWN in off.stdout


def test_on_quiets_the_hint_for_a_confident_opus_verdict(tmp_path, fake):
    fake.probs = OPUS
    proc = _prompt(tmp_path, "on", fake.url)
    assert SHOWN not in proc.stdout and "OBSERVATION [local-verdict]" in proc.stdout
    # it says why, names no tool or model, and is not the generic "would route" observation of the shadow mode
    assert "would route" not in proc.stdout and "[shadow]" not in proc.stdout and "llm(" not in proc.stdout
    assert "opus-level" in proc.stdout and "p_max 0.80" in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["applied"], r["outcome"]) == ("claude", True, "hint_quieted")


def test_on_keeps_the_hint_for_a_sonnet_verdict(tmp_path, fake):
    fake.probs = SONNET
    proc = _prompt(tmp_path, "on", fake.url)
    assert SHOWN in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["applied"], r["outcome"]) == ("mcp", False, "no_change")


def test_on_with_a_down_model_is_todays_hook(tmp_path, fake):
    """Fail-open: Ollama answers 500, the hook behaves as with the classifier off and still logs the miss."""
    fake.fail = True
    proc = _prompt(tmp_path, "on", fake.url)
    assert SHOWN in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["applied"], r["source"]) == ("", False, "timeout")


HAIKU = {"haiku": 0.9, "sonnet": 0.08, "opus": 0.02}


def _codex(tmp_path):
    return str(write_codex_stub(tmp_path / "bin", argv_log=tmp_path / "codex_argv.jsonl", answer="x"))


def test_on_names_the_codex_door_for_a_sonnet_verdict(tmp_path, fake):
    fake.probs = SONNET
    proc = _prompt(tmp_path, "on", fake.url, CODEX_PATH=_codex(tmp_path))
    assert SHOWN in proc.stdout and "CODEX VERDICT" in proc.stdout and 'prompt=\\"codex: ' in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["applied"], r["outcome"]) == ("codex", True, "hint_prefers_codex")


def test_shadow_adds_no_door_note_even_for_a_sonnet_verdict(tmp_path, fake):
    fake.probs = SONNET
    proc = _prompt(tmp_path, "shadow", fake.url, CODEX_PATH=_codex(tmp_path))
    assert "VERDICT" not in proc.stdout
    assert _verdict_rows(tmp_path)[0]["applied"] is False


def test_on_names_the_local_lane_for_a_confident_haiku_where_a_lane_passed(tmp_path, fake):
    fake.probs = HAIKU
    (tmp_path / ".llm-router").mkdir(exist_ok=True)
    (tmp_path / ".llm-router" / "lane_quality.jsonl").write_text(json.dumps(
        {"ts": time.time(), "door": "mcp", "served_class": "local", "task_group": "qa", "kind": "audit",
         "n": 30, "acceptable": 28, "source": "t"}) + "\n")
    proc = _prompt(tmp_path, "on", fake.url, CODEX_PATH=_codex(tmp_path))
    assert "LOCAL VERDICT" in proc.stdout and 'prompt=\\"local: ' in proc.stdout and "CODEX VERDICT" not in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["lane"], r["outcome"]) == ("local", "qa", "hint_prefers_local")


def test_on_without_a_passed_lane_a_haiku_verdict_goes_to_codex(tmp_path, fake):
    fake.probs = HAIKU
    proc = _prompt(tmp_path, "on", fake.url, CODEX_PATH=_codex(tmp_path))
    assert "CODEX VERDICT" in proc.stdout and "LOCAL VERDICT" not in proc.stdout


def test_on_sends_a_commit_message_prompt_to_the_local_text_job_when_its_switch_is_on(tmp_path, fake):
    from llm_router.local_check import routing

    fake.probs = HAIKU
    state = tmp_path / ".llm-router"
    state.mkdir(exist_ok=True)
    routing.enable("qwen3.6:35b-a3b-coding", "commit_message", "manual", path=state / routing.routing_path().name)
    proc = _prompt(tmp_path, "on", fake.url, prompt="Write a commit message for the staged diff in the retry module",
                   CODEX_PATH=_codex(tmp_path))
    assert "LOCAL VERDICT" in proc.stdout and "llm_text_job" in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["lane"], r["outcome"]) == ("local", "text_job:commit_message", "hint_prefers_local")


def test_a_quiet_prompt_gets_no_door_note_and_the_row_says_so(tmp_path, fake):
    """A Q&A prompt shown as a passive observation (QA routing off) has no hint to carry the note."""
    fake.probs = SONNET
    env = hook_env(tmp_path, LLM_ROUTER_ENFORCE="suggest", LLM_ROUTER_DIRECT_EXECUTION="0",
                   CODEX_PATH=_codex(tmp_path), **_classifier_env(fake.url, "on"))
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": PROMPT, "session_id": "door-s9", "cwd": str(tmp_path)}
    proc = run_hook(AUTO_ROUTE, payload, env)
    assert proc.returncode == 0 and "VERDICT" not in proc.stdout
    r = _verdict_rows(tmp_path)[0]
    assert (r["door"], r["outcome"]) == ("codex", "no_hint_shown")


def test_on_does_not_name_codex_when_the_meter_reserve_refuses(tmp_path, fake):
    """D-373: the router chain may not spend the Plus meter below the reserve; a hint that sent Claude to that call would
    be a refused call. Reserve 0 refuses everything (the counters verdict), so the hint stays the default one."""
    fake.probs = SONNET
    proc = _prompt(tmp_path, "on", fake.url, CODEX_PATH=_codex(tmp_path), LLM_ROUTER_CODEX_RESERVE_PCT="0")
    assert SHOWN in proc.stdout and "CODEX VERDICT" not in proc.stdout
    assert _verdict_rows(tmp_path)[0]["door"] == "mcp"


def test_on_does_not_name_codex_when_the_window_budget_is_spent(tmp_path, fake):
    fake.probs = SONNET
    proc = _prompt(tmp_path, "on", fake.url, CODEX_PATH=_codex(tmp_path), LLM_ROUTER_CODEX_WINDOW_BUDGET="0")
    assert SHOWN in proc.stdout and "CODEX VERDICT" not in proc.stdout


def test_a_prompt_with_a_secret_is_never_sent_to_the_model_by_either_hook(tmp_path, fake):
    secret = "ghp_Zk3Qx9LmW2vTb7Yc4Rn8Hd5Pf1Js6GaE0uXo"
    _prompt(tmp_path, "shadow", fake.url, prompt=f"What does this token do? {secret}")
    assert fake.calls == 0
    rows = _verdict_rows(tmp_path)
    assert rows and rows[0]["source"] == "withheld_secret" and secret not in json.dumps(rows)


def test_shadow_does_not_load_the_decision_model_over_the_text_job_lanes_model(tmp_path, fake):
    """CLS-EVICT-1: the lane model stays resident; the verdict says why it was skipped."""
    fake.others = ["qwen3.6:35b-a3b-coding"]
    proc = _prompt(tmp_path, "shadow", fake.url)
    assert SHOWN in proc.stdout and fake.calls == 0 and fake.others == ["qwen3.6:35b-a3b-coding"]
    assert _verdict_rows(tmp_path)[0]["source"] == "skipped_slot_busy"


def _raw(tmp_path, url: str, prompt: str, session: str):
    env = hook_env(tmp_path, LLM_ROUTER_QA_ROUTING="on", LLM_ROUTER_ENFORCE="suggest",
                   LLM_ROUTER_DIRECT_EXECUTION="0", **_classifier_env(url, "on"))
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": session, "cwd": str(tmp_path)}
    proc = run_hook(AUTO_ROUTE, payload, env)
    assert proc.returncode == 0, proc.stderr[-400:]
    return proc


def test_a_user_prefix_takes_no_verdict(tmp_path, fake):
    """`claude:` is the user's own door; the model is not asked and nothing is logged."""
    fake.probs = OPUS
    _raw(tmp_path, fake.url, "claude: " + PROMPT, "door-s2")
    assert fake.calls == 0 and _verdict_rows(tmp_path) == []


def test_a_bare_acknowledgement_takes_no_verdict(tmp_path, fake):
    """"yes" is bypassed to the host agent before any routing decision; there is no door to pick."""
    _raw(tmp_path, fake.url, "yes", "door-s3")
    assert fake.calls == 0 and _verdict_rows(tmp_path) == []


# ===================================================================================== agent-route (spawns)


@pytest.fixture
def ctx(tmp_path):
    argv_log = tmp_path / "codex_argv.jsonl"
    project = init_repo(tmp_path / "project")

    class Ctx:
        home = tmp_path
        log = argv_log
        proj = project

        def run(self, mode: str, url: str, **env):
            codex = write_codex_stub(tmp_path / "bin", argv_log=argv_log, answer=ANSWER)
            hook_cwd = tmp_path / "hookproc"
            hook_cwd.mkdir(exist_ok=True)
            e = hook_env(tmp_path, CODEX_PATH=str(codex), **_classifier_env(url, mode), **env)
            return run_hook(AGENT_ROUTE, agent_payload(SUITABLE_PROMPT, cwd=str(project)), e, cwd=hook_cwd)

    return Ctx()


def test_spawn_off_delegates_to_codex_as_before(ctx, fake):
    decision = decision_of(ctx.run("off", fake.url))
    assert ANSWER in decision["reason"] and len(stub_calls(ctx.log)) == 1
    assert fake.calls == 0


def test_spawn_shadow_logs_a_verdict_and_still_delegates(ctx, fake):
    fake.probs = OPUS
    decision = decision_of(ctx.run("shadow", fake.url))
    assert ANSWER in decision["reason"] and len(stub_calls(ctx.log)) == 1
    r = _verdict_rows(ctx.home)[0]
    assert (r["hook_kind"], r["tier"], r["door"], r["applied"], r["tool_use_id"]) == (
        "agent", "opus", "claude", False, "toolu_bind_1")


def test_spawn_on_keeps_a_confident_opus_spawn_on_claude(ctx, fake):
    fake.probs = OPUS
    ctx.run("on", fake.url)
    assert stub_calls(ctx.log) == []                              # no Codex process was started
    r = _verdict_rows(ctx.home)[0]
    assert (r["door"], r["applied"], r["outcome"]) == ("claude", True, "codex_skipped")
    units = [u for u in ledger_rows(ctx.home) if u.get("lever") == "agent_route_codex"]
    assert units and units[-1]["outcome"] == "unsuitable" and units[-1].get("reason_code") == "verdict_claude"


def test_spawn_on_lets_a_sonnet_spawn_through_the_existing_codex_gates(ctx, fake):
    fake.probs = SONNET
    decision = decision_of(ctx.run("on", fake.url))
    assert ANSWER in decision["reason"] and len(stub_calls(ctx.log)) == 1
    assert _verdict_rows(ctx.home)[0]["door"] == "codex"


def test_spawn_on_with_a_down_model_delegates_as_before(ctx, fake):
    fake.fail = True
    decision = decision_of(ctx.run("on", fake.url))
    assert ANSWER in decision["reason"] and len(stub_calls(ctx.log)) == 1


def test_spawn_explore_and_retrieval_get_no_verdict(ctx, fake):
    e = hook_env(ctx.home, **_classifier_env(fake.url, "shadow"))
    run_hook(AGENT_ROUTE, agent_payload("find the file", subagent_type="Explore", cwd=str(ctx.proj)), e)
    assert fake.calls == 0 and _verdict_rows(ctx.home) == []


# ===================================================================================== session start


def _session_start_module():
    spec = importlib.util.spec_from_file_location("ss_w10", HOOKS_SRC / "session-start.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_start_warms_the_decision_model_only_in_on_and_skips_the_other_two(monkeypatch, fake):
    for k, v in _classifier_env(fake.url, "on").items():
        monkeypatch.setenv(k, v)
    mod = _session_start_module()
    assert mod._warm_classifier_bg() == "nimble:9b" and fake.resident
    # the child's step order: classifier first, the other two only when it did not load
    src = (HOOKS_SRC / "session-start.py").read_text()
    assert src.index("_step(_warm_classifier_bg)") < src.index("_step(_warm_edit_model_bg)")
    assert "if not _step(_warm_classifier_bg):" in src


def test_session_start_in_shadow_loads_nothing_so_the_old_warm_ups_run(monkeypatch, fake):
    """CLS-EVICT-1: a log-only verdict must not put nimble in front of the models the other lanes warm."""
    for k, v in _classifier_env(fake.url, "shadow").items():
        monkeypatch.setenv(k, v)
    mod = _session_start_module()
    assert mod._warm_classifier_bg() == "" and fake.calls == 0 and not fake.resident


def test_session_start_falls_back_to_the_old_warm_ups_when_nothing_loaded(monkeypatch, fake):
    for k, v in _classifier_env(fake.url, "off").items():
        monkeypatch.setenv(k, v)
    mod = _session_start_module()
    assert mod._warm_classifier_bg() == "" and fake.calls == 0
