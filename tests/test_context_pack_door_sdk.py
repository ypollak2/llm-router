"""P1.1 door 6, the SDK: ``route(messages=)`` packs with ``build_pack(door="sdk")``
inside the SDK's own ``build_chain`` + ``execute_chain`` / agent-loop path (D-98).

Model choice, agent loop, timeout and the ``host="sdk"`` ledger rows are the same
with and without ``messages``; ``route_and_call`` is never reached. The history is
gated by two independent checks, each pinned by its own test: (A) the privacy
target is the most external provider in the chain, because a fallback model gets
the same context; (B) ``session_store.allows_session_content`` for that target
under the privacy mode (D-91/D-95: only "local"/"ollama" are on-device).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_router import call_identity, context_pack, failopen, router, sdk, session_store
from llm_router.hooks import chain_builder, direct_executor, savings_logger
from llm_router.hooks.direct_executor import DirectResult, ModelSpec

OLLAMA = ModelSpec("ollama", "qwen-x")
OPENAI = ModelSpec("openai", "gpt-x")
HISTORY = [
    {"role": "user", "content": "U1"},
    {"role": "assistant", "content": "A1"},
]


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("LLM_ROUTER_") or key in (
            "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR",
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    monkeypatch.setattr(call_identity, "call_session_id", lambda: None)


class Rig:
    """Fakes for the SDK's executors and meters; records every call."""

    def __init__(self, monkeypatch, chain=(OLLAMA,), tools=False):
        self.chain = list(chain)
        self.chains, self.text, self.agent, self.meters, self.packs = [], [], [], [], []
        monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0.0))
        monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: tools)
        monkeypatch.setattr(chain_builder, "build_chain", self._build_chain)
        monkeypatch.setattr(direct_executor, "execute_chain", self._execute_chain)
        monkeypatch.setattr(direct_executor, "execute_agent", self._execute_agent)
        monkeypatch.setattr(direct_executor, "_local_num_ctx", lambda model: 8192)
        monkeypatch.setattr(savings_logger, "log_direct_to_db",
                            lambda **kw: self.meters.append(("db", kw["classifier_type"])))
        monkeypatch.setattr(savings_logger, "log_direct_savings",
                            lambda **kw: self.meters.append(("savings", kw["host"])))

        async def no_route_and_call(*a, **k):
            raise AssertionError("the SDK door must not go through route_and_call (D-98)")

        monkeypatch.setattr(router, "route_and_call", no_route_and_call)
        real = context_pack.build_pack

        def spy(*args, **kwargs):
            self.packs.append((args, kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(context_pack, "build_pack", spy)

    def _build_chain(self, complexity, zone, task_type, **_kwargs):
        self.chains.append((complexity, zone, task_type))
        return list(self.chain)

    def _result(self, chain):
        return DirectResult(text="ok", model=chain[0], latency_ms=3,
                            input_tokens=1, output_tokens=2)

    def _execute_chain(self, prompt, chain, task_type, timeout=4, **kw):
        self.text.append({"prompt": prompt, "chain": chain, "task_type": task_type,
                          "timeout": timeout, **kw})
        return self._result(chain)

    def _execute_agent(self, prompt, chain, timeout=60, **kw):
        self.agent.append({"prompt": prompt, "chain": chain, "timeout": timeout, **kw})
        return self._result(chain)

    def history(self):
        return self.text[-1].get("history")


def _route(**kw):
    kw.setdefault("task_type", "query")
    kw.setdefault("complexity", "simple")
    return sdk.route("REQ-TEXT", **kw)


# ── the D-98 path ─────────────────────────────────────────────────────────────


def test_messages_reach_execute_chain_as_history(monkeypatch):
    rig = Rig(monkeypatch)
    r = _route(messages=HISTORY)
    assert (r.text, r.model) == ("ok", "ollama/qwen-x")
    assert rig.text[-1]["prompt"] == "REQ-TEXT"
    assert rig.history() == HISTORY
    assert [(k["door"], k["target_provider"]) for _a, k in rig.packs] == [("sdk", "ollama")]
    assert rig.packs[0][0] == ("REQ-TEXT", HISTORY)


def test_model_choice_timeout_and_ledger_are_the_same_with_and_without_messages(monkeypatch):
    rig = Rig(monkeypatch, chain=(OLLAMA, OPENAI))
    _route(timeout=7)
    _route(timeout=7, messages=HISTORY)
    plain, packed = rig.text
    assert rig.chains[0] == rig.chains[1] == ("simple", "green", "query")
    assert plain["chain"] == packed["chain"] == [OLLAMA, OPENAI]
    assert plain["timeout"] == packed["timeout"] == 7
    assert plain["task_type"] == packed["task_type"] == "query"
    assert rig.meters == [("db", "sdk"), ("savings", "sdk")] * 2


def test_without_messages_the_executor_call_is_unchanged(monkeypatch):
    rig = Rig(monkeypatch)
    _route(timeout=9)
    assert rig.text == [{"prompt": "REQ-TEXT", "chain": [OLLAMA], "task_type": "query",
                         "timeout": 9}]
    assert rig.packs == []


def test_agent_loop_keeps_its_chain_and_timeout_and_gets_the_history_as_context(monkeypatch):
    rig = Rig(monkeypatch, tools=True)
    _route(timeout=30)
    _route(timeout=30, messages=HISTORY)
    plain, packed = rig.agent
    assert rig.chains == [("complex", "green", "code")] * 2
    assert plain["chain"] == packed["chain"] and plain["timeout"] == packed["timeout"] == 180
    assert "context" not in plain and "history" not in packed
    assert "[user]\nU1" in packed["context"] and "[assistant]\nA1" in packed["context"]
    assert rig.text == []
    assert rig.meters == [("db", "sdk"), ("savings", "sdk")] * 2


def test_request_is_sent_verbatim_and_never_trimmed(monkeypatch):
    rig = Rig(monkeypatch)
    big = "x" * 200_000
    sdk.route(big, task_type="query", complexity="simple", messages=HISTORY)
    assert rig.text[-1]["prompt"] == big


def test_trailing_copy_of_the_request_is_not_sent_twice(monkeypatch):
    rig = Rig(monkeypatch)
    _route(messages=[*HISTORY, {"role": "user", "content": "REQ-TEXT"}])
    assert rig.history() == HISTORY


def test_leading_assistant_turns_are_dropped(monkeypatch):
    rig = Rig(monkeypatch)
    _route(messages=[{"role": "assistant", "content": "A0"}, *HISTORY])
    assert rig.history() == HISTORY


def test_only_the_last_seven_messages_go_when_the_conversation_does_not_fit(monkeypatch):
    rig = Rig(monkeypatch)
    long = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"T{i} " + "w " * 900}
            for i in range(20)]
    _route(messages=long)
    sent = rig.history()
    assert len(sent) <= 7 and sent[-1]["content"].startswith("T19")


def test_empty_messages_sends_the_request_alone(monkeypatch):
    rig = Rig(monkeypatch)
    _route(messages=[])
    assert rig.history() is None and rig.text[-1]["context"] is None


def test_malformed_messages_are_refused_before_any_routing(monkeypatch):
    rig = Rig(monkeypatch)
    for bad in ([{"role": "user"}], "not a list", [{"role": "user", "content": 3}]):
        with pytest.raises(ValueError):
            _route(messages=bad)
    assert rig.chains == [] and rig.text == []


def test_pack_error_records_and_sends_the_request_alone(monkeypatch):
    rig = Rig(monkeypatch)
    error = RuntimeError("pack down")
    records = []

    def boom(*a, **k):
        raise error

    monkeypatch.setattr(context_pack, "build_pack", boom)
    monkeypatch.setattr(failopen, "record", lambda *a: records.append(a))
    assert _route(messages=HISTORY).text == "ok"
    assert records == [("CHZ-FO-SDK-PACK", error)]
    assert rig.text[-1] == {"prompt": "REQ-TEXT", "chain": [OLLAMA], "task_type": "query",
                            "timeout": 150}


def test_the_sdk_module_never_calls_route_and_call():
    text = Path(sdk.__file__).read_text(encoding="utf-8")
    assert "route_and_call" not in text


# ── privacy gate A: the target is the most external provider in the chain ─────


@pytest.mark.parametrize("order", ["local_first", "cloud_first", "cloud_between"])
@pytest.mark.parametrize("cloud", ["openai", "gemini", "codex", "gemini_cli"])
def test_gate_a_local_mode_withholds_history_when_any_fallback_is_cloud(monkeypatch, cloud,
                                                                         order):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    c = ModelSpec(cloud, "m")
    chain = {"local_first": (OLLAMA, c), "cloud_first": (c, OLLAMA),
             "cloud_between": (OLLAMA, c, ModelSpec("ollama", "other"))}[order]
    rig = Rig(monkeypatch, chain=chain)
    _route(messages=HISTORY)
    assert rig.history() is None
    assert rig.packs[0][1]["target_provider"] == cloud


def test_gate_a_claude_entries_are_never_called_so_they_do_not_withhold(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    claude = ModelSpec("claude", "sonnet")
    rig = Rig(monkeypatch, chain=(OLLAMA, claude))
    _route(messages=HISTORY)
    assert rig.history() == HISTORY
    rig = Rig(monkeypatch, chain=(OLLAMA, claude, OPENAI))
    _route(messages=HISTORY)
    assert rig.history() is None


def test_gate_a_local_mode_sends_history_to_an_all_local_chain(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    rig = Rig(monkeypatch, chain=(OLLAMA, ModelSpec("ollama", "other")))
    _route(messages=HISTORY)
    assert rig.history() == HISTORY


def test_gate_a_mutation_first_provider_target_would_leak(monkeypatch):
    """Pins gate A alone: with gate B intact, a target taken from the first model
    (local) instead of the most external one sends history to a cloud fallback."""
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    rig = Rig(monkeypatch, chain=(OLLAMA, OPENAI))
    _route(messages=HISTORY)
    assert rig.history() is None
    monkeypatch.setattr(sdk, "_privacy_target", lambda chain: chain[0].provider)
    _route(messages=HISTORY)
    assert rig.history() == HISTORY


# ── privacy gate B: allows_session_content under the privacy mode ─────────────


def test_gate_b_off_mode_withholds_history_even_from_local(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    rig = Rig(monkeypatch, chain=(OLLAMA,))
    _route(messages=HISTORY)
    assert rig.history() is None


def test_gate_b_all_mode_sends_history_to_cloud(monkeypatch):
    rig = Rig(monkeypatch, chain=(OPENAI,))
    _route(messages=HISTORY)
    assert rig.history() == HISTORY


def test_gate_b_mutation_permissive_rule_would_leak(monkeypatch):
    """Pins gate B alone: with gate A intact (target = openai), a rule that allows
    every provider sends history to the cloud under privacy mode local."""
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    rig = Rig(monkeypatch, chain=(OPENAI,))
    _route(messages=HISTORY)
    assert rig.history() is None
    monkeypatch.setattr(session_store, "allows_session_content", lambda provider: True)
    _route(messages=HISTORY)
    assert rig.history() == HISTORY


def test_agent_loop_target_is_its_ollama_models_only(monkeypatch):
    """The agent loop calls ollama models only, so a cloud entry it skips does not
    withhold the history; the gate itself still applies (mode off)."""
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    rig = Rig(monkeypatch, chain=(OPENAI, OLLAMA), tools=True)
    _route(messages=HISTORY)
    assert "U1" in rig.agent[-1]["context"]
    assert rig.packs[-1][1]["target_provider"] == "ollama"
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    _route(messages=HISTORY)
    assert "U1" not in (rig.agent[-1].get("context") or "")


def test_agent_loop_without_an_ollama_model_is_the_plain_call(monkeypatch):
    rig = Rig(monkeypatch, chain=(OPENAI,), tools=True)
    _route(timeout=30, messages=HISTORY)
    assert rig.packs == [] and "context" not in rig.agent[-1]


def test_history_shape_fits_chat_apis(monkeypatch):
    rig = Rig(monkeypatch)
    _route(messages=[
        {"role": "user", "content": "U1"},
        {"role": "user", "content": "U2"},
        {"role": "function", "content": "F"},
        {"role": "assistant", "content": "A1"},
        {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "U3"},
    ])
    assert rig.history() == [
        {"role": "user", "content": "U1\n\nU2\n\n[function] F"},
        {"role": "assistant", "content": "A1\n\nA2"},
        {"role": "user", "content": "U3"},
    ]


# ── end to end through the real execute_chain ─────────────────────────────────


def test_history_reaches_the_provider_call_through_the_real_executor(monkeypatch):
    seen = []

    def fake_call(prompt, model, timeout, history, system_prompt):
        seen.append((prompt, model, timeout, history))
        return "a real answer from the model", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    monkeypatch.setattr(chain_builder, "build_chain", lambda c, z, t, **k: [OPENAI])
    monkeypatch.setitem(direct_executor._PROVIDER_CALLS, "openai", fake_call)
    monkeypatch.setattr(direct_executor, "_paid_budget_exhausted", lambda p: False)
    monkeypatch.setattr(direct_executor, "_provider_reset_blocked", lambda p: False)
    monkeypatch.setattr(direct_executor, "_okf_inject", lambda prompt, **k: prompt)
    monkeypatch.setattr(direct_executor, "_okf_enrich", lambda *a: None)
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    r = _route(timeout=11, messages=HISTORY)
    assert r.text == "a real answer from the model"
    assert seen == [("REQ-TEXT", "gpt-x", 11.0, HISTORY)]
