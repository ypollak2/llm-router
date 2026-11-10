"""BIND W1 / F1 + F2: ``agent-route.py`` prints ONE decision object on stdout, on every emit path.

Root cause (docs/BUGS.md BIND-1): ``_log_cli_savings`` -> ``model_tracking`` logs
``[debug] Tracked: ...`` with structlog's default sink, which is STDOUT, and ``agent-route.py`` never
configured logging. Claude Code treats stdout that does not start with ``{`` as plain text and applies no
decision: the 317 recorded Codex handoffs (2026-10-04..2026-11-03, ``north_star_units.jsonl``) all ran
Claude's own sub-agent as well. The same pollution hit the model-pin ``allow`` when Codex was benched
mid-run (a ``provider_unavailable_until`` warning in front of the JSON).

These tests run the real hook script as a subprocess, one per emit path, with a real executable standing in
for ``codex`` and NO patching of logging or of ``_log_cli_savings``. Each path also asserts that the log line
that used to leak really fired (it is on stderr): an unreachable path would otherwise pass with nothing
checked.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.secret_fixtures import real_shaped
from tests._bind_support import (
    delivered_text,
    AGENT_ROUTE,
    SUITABLE_PROMPT,
    UNSUITABLE_PROMPT,
    agent_payload,
    decision_of,
    hook_env,
    run_hook,
    stub_calls,
    write_codex_stub,
)

# Every test starts the real hook as a subprocess (up to four per test); the repo default of 30 s is for
# in-process tests and a loaded runner can double the 8 s the slowest takes here.
pytestmark = [
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell stub and flock"),
    pytest.mark.timeout(180),
]

ANSWER = "STUB-CODEX-ANSWER-0f3a: the pager drops the last row because the loop stops one early."


@pytest.fixture
def ctx(tmp_path):
    """Isolated state dir, a stub codex that answers, and a runner for the real hook."""
    argv_log = tmp_path / "codex_argv.jsonl"

    class Ctx:
        home = tmp_path
        log = argv_log

        def stub(self, **kw):
            kw.setdefault("argv_log", argv_log)
            kw.setdefault("answer", ANSWER)
            return write_codex_stub(tmp_path / "bin", **kw)

        def run(self, payload, *, codex=None, **env):
            if codex is not None:
                env["CODEX_PATH"] = str(codex)
            return run_hook(AGENT_ROUTE, payload, hook_env(tmp_path, **env), cwd=tmp_path)

    return Ctx()


def test_litellm_handler_is_kept_off_stdout_by_default_and_an_explicit_value_wins(ctx):
    """LiteLLM's own handler writes records below WARNING to stdout and defaults to DEBUG. The hook sets
    LITELLM_LOG=ERROR before anything imports it; the Codex child inherits the environment, so it can report it."""
    ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub())
    assert stub_calls(ctx.log)[0]["env"]["LITELLM_LOG"] == "ERROR"


def test_an_explicit_litellm_log_is_not_overridden(ctx):
    ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(), LITELLM_LOG="DEBUG")
    assert stub_calls(ctx.log)[0]["env"]["LITELLM_LOG"] == "DEBUG"


# ---------------------------------------------------------------------------------- delegated


def test_delegated_handoff_is_one_json_object(ctx):
    """The path that was void 317 of 317 times: Codex answers, the real ``_log_cli_savings`` runs."""
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub())
    decision = decision_of(proc)
    assert decision is not None, proc.stderr[-500:]
    assert ANSWER in delivered_text(decision)
    # Non-vacuous: the line that used to lead the JSON on stdout was emitted, on stderr.
    assert "Tracked:" in proc.stderr, proc.stderr[-800:]
    assert "Tracked:" not in proc.stdout
    # ... through the stderr logging pipeline (structlog -> stdlib), which names the logger: layer 1 ran.
    assert "llm_router.model_tracking" in proc.stderr, proc.stderr[-800:]
    assert len(stub_calls(ctx.log)) == 1  # the real run_codex launched the stub exactly once


def test_a_library_that_logs_to_stdout_at_exit_cannot_follow_the_decision(ctx):
    """LiteLLM keeps its own handler, which writes every record below WARNING to ``sys.stdout`` looked up when
    the record is emitted. A delegated run imports LiteLLM once the answer is long enough to be judged
    (cost.log_routing_decision -> judge -> providers), and LiteLLM's logging worker logs from an atexit handler:
    with DEBUG on, that record came out on stdout AFTER the JSON ("Extra data"). LITELLM_LOG=DEBUG is set here
    on purpose so the default the hook applies cannot hide it: this test is the one that needs ``sys.stdout`` to
    stay on stderr until the process exits."""
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(), LITELLM_LOG="DEBUG")
    decision = decision_of(proc)
    assert decision is not None and ANSWER in delivered_text(decision), proc.stderr[-500:]
    assert "LiteLLM" in proc.stderr, "LiteLLM did not log at all, so this proved nothing: " + proc.stderr[-400:]
    assert "LiteLLM" not in proc.stdout


def test_codex_benched_mid_run_still_prints_only_the_pin(ctx):
    """A usage limit benches Codex while the hook runs; ``provider_unavailable_until`` is logged at WARNING.
    21 model pins in the window printed that line first, and the pin was honoured in 1 of the 20 that could be
    determined (docs/BUGS.md BIND-1)."""
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(mode="quota"))
    decision = decision_of(proc)
    assert decision is not None, proc.stderr[-500:]
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "provider_unavailable_until" in proc.stderr, proc.stderr[-800:]
    assert "provider_unavailable_until" not in proc.stdout


def test_codex_failure_falls_through_to_one_pin(ctx):
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(mode="fail"))
    decision = decision_of(proc)
    assert decision is not None and decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(stub_calls(ctx.log)) == 1


# ------------------------------------------------------------------------- model pin / approvals


@pytest.mark.parametrize(
    "label, payload_kw, env",
    [
        ("codex_off_pin", dict(prompt=SUITABLE_PROMPT), {"LLM_ROUTER_AGENT_ROUTE_CODEX": "off"}),
        ("unsuitable_pin", dict(prompt=UNSUITABLE_PROMPT), {}),
        ("explore_pin", dict(prompt="look at the repo layout", subagent_type="Explore"), {}),
        ("retrieval_pin", dict(prompt="find all files named conftest.py"), {}),
    ],
)
def test_model_pin_paths_print_one_json_object(ctx, label, payload_kw, env):
    proc = ctx.run(agent_payload(**payload_kw), codex=ctx.stub(), **env)
    decision = decision_of(proc)
    assert decision is not None, (label, proc.stderr[-500:])
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "model" in decision["hookSpecificOutput"]["updatedInput"]
    assert stub_calls(ctx.log) == [], f"{label}: Codex must not run on this path"


@pytest.mark.parametrize(
    "label, payload_kw, env",
    [
        ("headless_skip", dict(prompt=SUITABLE_PROMPT), {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}),
        ("empty_prompt", dict(prompt="   "), {}),
        ("allowlisted_type", dict(prompt=SUITABLE_PROMPT, subagent_type="qa"),
         {"LLM_ROUTER_AGENT_ROUTE_ALLOW": "qa"}),
        ("secret_prompt_not_sent",
         dict(prompt=SUITABLE_PROMPT + " token " + real_shaped("github_token", 4)), {}),
    ],
)
def test_no_decision_paths_print_nothing_or_one_object(ctx, label, payload_kw, env):
    proc = ctx.run(agent_payload(**payload_kw), codex=ctx.stub(), **env)
    decision = decision_of(proc)  # may be None (approve silently) but never a polluted stream
    if decision is not None:
        assert decision.get("decision") != "block"
    assert stub_calls(ctx.log) == [], f"{label}: nothing may be sent to Codex on this path"


# ------------------------------------------------------------------------------------ blocks


def test_nesting_breaker_block_is_one_json_object(ctx):
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(), LLM_ROUTER_MAX_AGENT_DEPTH="0")
    decision = decision_of(proc)
    assert decision is not None and decision["decision"] == "block"
    assert "nesting limit" in decision["reason"].lower()


def test_concurrency_breaker_block_is_one_json_object(ctx):
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(),
                   LLM_ROUTER_MAX_CONCURRENT_AGENTS="0")
    decision = decision_of(proc)
    assert decision is not None and decision["decision"] == "block"
    assert "in flight" in decision["reason"].lower()


_LEGACY_ROUTING_OFF = {
    "LLM_ROUTER_ALLOW_SUBAGENTS": "off",
    "LLM_ROUTER_AGENT_ROUTE_CODEX": "off",
    "LLM_ROUTER_SUBAGENT_DIRECT": "off",
    "LLM_ROUTER_SUBAGENT_CLI_DELEGATION": "off",
}


def test_session_budget_block_is_one_json_object(ctx):
    state = ctx.home / ".llm-router"
    state.mkdir(parents=True, exist_ok=True)
    (state / "session_budget.json").write_text(json.dumps(
        {"session_id": "bind-test-session", "initial": 30.0, "remaining": 0.0,
         "provisional_spend": 30.0, "timestamp": 0}))
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(), **_LEGACY_ROUTING_OFF)
    decision = decision_of(proc)
    assert decision is not None and decision["decision"] == "block"
    assert "exceed session budget" in decision["reason"]


def test_blocked_reasoning_block_is_one_json_object(ctx):
    proc = ctx.run(agent_payload(SUITABLE_PROMPT), codex=ctx.stub(), **_LEGACY_ROUTING_OFF)
    decision = decision_of(proc)
    assert decision is not None and decision["decision"] == "block"
    assert "AGENT-ROUTE" in decision["reason"]


def test_cli_delegation_block_is_one_json_object(ctx):
    """Phase 2: bigger work goes to Codex through ``_try_cli_delegation`` (a long prompt is ``complex``)."""
    long_prompt = SUITABLE_PROMPT + " " + ("Check every call site and report the exact lines. " * 14)
    assert len(long_prompt) > 500
    env = dict(_LEGACY_ROUTING_OFF)
    env.pop("LLM_ROUTER_SUBAGENT_CLI_DELEGATION")
    proc = ctx.run(agent_payload(long_prompt), codex=ctx.stub(), **env)
    decision = decision_of(proc)
    assert decision is not None and decision["decision"] == "block", proc.stderr[-500:]
    assert ANSWER in decision["reason"]
    assert "Tracked:" in proc.stderr and "Tracked:" not in proc.stdout
    assert len(stub_calls(ctx.log)) == 1


# --------------------------------------------------------------------------- local DIRECT path

# `_try_direct_subagent` is off by default (owner decision 2026-10-02) and, with the default
# LLM_ROUTER_ALLOW_SUBAGENTS=on, unreachable; reaching a local model needs a model-discovery cache, the
# promotion gate and Q&A routing. Only the MODEL CALL is replaced here. The script runs the real hook file as
# `__main__` in its own process, so `log_direct_savings`, `log_direct_to_db` and `_govern_run` (where the
# logging happens) are real, and so is stdout.
_DIRECT_DRIVER = """
import runpy, sys
import llm_router.hooks.chain_builder as cb
import llm_router.hooks.direct_executor as de

def build_chain(*a, **k):
    return [de.ModelSpec("ollama", "stub-local")]

def execute_chain(*a, **k):
    return de.DirectResult(text="STUB_DIRECT_ANSWER", model=de.ModelSpec("ollama", "stub-local"),
                           latency_ms=5, input_tokens=10, output_tokens=5)

cb.build_chain, de.execute_chain = build_chain, execute_chain
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def test_direct_subagent_block_is_one_json_object(ctx, tmp_path):
    import subprocess

    driver = tmp_path / "direct_driver.py"
    driver.write_text(_DIRECT_DRIVER)
    env = hook_env(tmp_path, **{**_LEGACY_ROUTING_OFF, "LLM_ROUTER_SUBAGENT_DIRECT": "on"})
    proc = subprocess.run(
        [sys.executable, str(driver), str(AGENT_ROUTE)],
        input=json.dumps(agent_payload("what does the retry helper do when the budget is spent?")),
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    decision = decision_of(proc)
    assert decision is not None and decision["decision"] == "block", proc.stderr[-600:]
    assert "STUB_DIRECT_ANSWER" in decision["reason"]
    assert "Tracked:" in proc.stderr and "Tracked:" not in proc.stdout, proc.stderr[-600:]
