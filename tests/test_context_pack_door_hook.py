"""Behavioural coverage for the DIRECT draft's hook context-pack door."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("qa_routing_on")

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "auto-route.py"
PROMPT = "What does the retry logic in this repo do?"
MARKER = "PRIVATE-P11-HOOK-TRANSCRIPT-MARKER"
TOOL_RESULT = "retry.py uses three attempts with exponential backoff"


def _load(monkeypatch):
    name = f"auto_route_p11_hook_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_hook(monkeypatch, tmp_path):
    import llm_router.context_injection as ci
    import llm_router.context_pack as cp
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de

    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    turns = [
        ("user", f"Earlier retry question: {MARKER}"),
        ("assistant", "Earlier answer about retries."),
        ("assistant", [
            {"type": "text", "text": "Inspecting retry.py."},
            {"type": "tool_use", "id": "read-retry", "name": "Read",
             "input": {"file_path": "retry.py"}},
        ]),
        ("user", [
            {"type": "tool_result", "tool_use_id": "read-retry",
             "content": TOOL_RESULT},
        ]),
        ("assistant", "The retry implementation is now available."),
    ]
    transcript.write_text("".join(
        json.dumps({"type": role, "message": {"role": role, "content": content}}) + "\n"
        for role, content in turns
    ), encoding="utf-8")

    real_build_pack = cp.build_pack
    pack_calls = []
    packs = []

    def spy(*args, **kwargs):
        pack_calls.append((args, kwargs))
        pack = real_build_pack(*args, **kwargs)
        packs.append(pack)
        return pack

    monkeypatch.setattr(cp, "build_pack", spy)
    # Keep retrieval independent of host indexes; the inject test overrides this.
    monkeypatch.setattr(ci, "inject", lambda request, **kwargs: request)
    monkeypatch.setattr(de, "_local_num_ctx", lambda model: 8192)

    def _run(prompt=PROMPT, *, providers=("ollama",), direct="1",
             mode="all", relay="on", needs_tools=False, readonly_loop=False):
        pack_calls.clear()
        packs.clear()
        for key, value in {
            "HOME": str(home),
            "LLM_ROUTER_HOME": str(home / ".llm-router"),
            "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
            "LLM_ROUTER_HOOK_LLM_LAYER": "off",
            "LLM_ROUTER_DIRECT_EXECUTION": direct,
            "LLM_ROUTER_ENFORCE": "suggest",
            "LLM_ROUTER_ZERO_CLAUDE": "off",
            "LLM_ROUTER_FREE_TIER_DRAFTS": "off",
            "LLM_ROUTER_SESSION_CONTEXT": mode,
            "LLM_ROUTER_HISTORY_RELAY": relay,
            "LLM_ROUTER_LOCAL_AGENT_LOOP": "on" if readonly_loop else "off",
            "LLM_ROUTER_ENGINE_MODE": "off",
        }.items():
            monkeypatch.setenv(key, value)

        ar = _load(monkeypatch)
        monkeypatch.setattr(ar, "log_routing_decision", lambda **kwargs: None)
        # Exercise draft wiring independently of the earlier context-rescue gate.
        monkeypatch.setattr(ar, "_is_context_dependent", lambda prompt: False)
        chain = [de.ModelSpec(provider=p, model="fake-model") for p in providers]
        built = []
        chain_calls = []
        agent_calls = []
        log = []

        def build_chain(complexity, zone, task):
            built.append(task)
            return list(chain)

        def execute_chain(*args, **kwargs):
            chain_calls.append((args, kwargs))
            return None

        def execute_agent(*args, **kwargs):
            agent_calls.append((args, kwargs))
            return None

        monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 10.0))
        monkeypatch.setattr(cb, "build_chain", build_chain)
        monkeypatch.setattr(cb, "needs_claude_tools", lambda prompt, task: needs_tools)
        monkeypatch.setattr(de, "execute_chain", execute_chain)
        monkeypatch.setattr(de, "execute_agent", execute_agent)
        monkeypatch.setattr(ar, "_debug_log", log.append)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
            "prompt": prompt,
            "session_id": "sess-p11hook",
            "transcript_path": str(transcript),
            "cwd": str(project),
        })))
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        try:
            ar.main()
        except SystemExit as exc:
            assert exc.code in (None, 0)
        assert not any("DIRECT ERROR:" in line for line in log), log
        assert any("task=query" in line for line in log), log
        return SimpleNamespace(
            pack_calls=list(pack_calls), packs=list(packs), built=built,
            chain_calls=chain_calls, agent_calls=agent_calls, log=log,
            project=project,
        )

    return _run


def _material(call):
    return json.dumps({
        "history": call[1].get("history", []),
        "context": call[1].get("context"),
    })


def test_draft_builds_one_hook_pack_and_relays_tool_blocks(run_hook, monkeypatch):
    import llm_router.session_store as ss

    def unexpected_session_context(*args, **kwargs):
        pytest.fail("pack draft must not build legacy session context")

    monkeypatch.setattr(ss, "build_session_context", unexpected_session_context)
    result = run_hook()
    assert result.built == ["query"]
    assert len(result.pack_calls) == len(result.chain_calls) == 1
    args, kwargs = result.pack_calls[0]
    assert args == (PROMPT,)
    assert kwargs["door"] == "hook"
    assert kwargs["session_id"] == "sess-p11hook"
    assert kwargs["target_provider"] == "ollama"
    assert kwargs["project_root"] == str(result.project)
    assert kwargs["transcript_path"].endswith("transcript.jsonl")
    assert kwargs["target_window"] == 8192
    history = result.chain_calls[0][1]["history"]
    assert history == result.packs[0].recent
    assert history is not result.packs[0].recent
    assert MARKER in history[0]["content"]
    assert any("Earlier answer about retries." in turn["content"] for turn in history)
    assert any("[tool_use: Read]" in turn["content"] for turn in history)
    assert any("[tool_result]" in turn["content"] and TOOL_RESULT in turn["content"]
               for turn in history)
    assert result.chain_calls[0][0][0] == PROMPT
    assert result.chain_calls[0][1]["session_id"] == "sess-p11hook"
    assert result.chain_calls[0][1]["root"] == str(result.project)


@pytest.mark.parametrize("options", [{"direct": "0"}, {"providers": ()}])
def test_no_pack_or_execution_when_drafts_off_or_chain_empty(run_hook, options):
    result = run_hook(**options)
    assert result.pack_calls == []
    assert result.chain_calls == []
    assert result.agent_calls == []


def test_request_is_verbatim_at_two_hundred_thousand_characters(run_hook):
    prefix, suffix = " \n" + PROMPT + "\n", "\n "
    prompt = prefix + "x" * (200_000 - len(prefix) - len(suffix)) + suffix
    assert len(prompt) == 200_000
    result = run_hook(prompt)
    assert len(result.pack_calls) == len(result.chain_calls) == 1
    assert result.pack_calls[0][0] == (prompt,)
    assert result.chain_calls[0][0][0] == prompt


@pytest.mark.parametrize("providers,mode,allowed,target", [
    (("ollama", "gemini"), "local", False, "gemini"),
    (("ollama", "gemini"), "all", True, "gemini"),
    (("ollama",), "local", True, "ollama"),
])
def test_privacy_uses_external_fallback_provider(run_hook, providers, mode, allowed, target):
    result = run_hook(providers=providers, mode=mode)
    assert len(result.pack_calls) == len(result.chain_calls) == 1
    assert result.pack_calls[0][1]["target_provider"] == target
    assert (MARKER in _material(result.chain_calls[0])) is allowed


def test_removing_privacy_gate_leaks_transcript_in_mixed_local_chain(run_hook, monkeypatch):
    import llm_router.context_pack as cp

    baseline = run_hook(providers=("ollama", "gemini"), mode="local")
    assert len(baseline.chain_calls) == 1
    assert MARKER not in _material(baseline.chain_calls[0])
    monkeypatch.setattr(cp, "_session_content_allowed", lambda provider: True)
    mutated = run_hook(providers=("ollama", "gemini"), mode="local")
    assert len(mutated.pack_calls) == len(mutated.chain_calls) == 1
    assert MARKER in _material(mutated.chain_calls[0])


def test_codex_in_chain_is_not_privacy_local_withholds_session_content(run_hook, monkeypatch):
    # D-91/D-95: codex is not privacy-local. Pin LOCAL_PROVIDERS so this does not
    # depend on the merge order of the PR that removes codex/gemini_cli from it.
    import llm_router.session_store as ss

    monkeypatch.setattr(ss, "LOCAL_PROVIDERS", ("local", "ollama"))
    result = run_hook(providers=("ollama", "codex"), mode="local")
    assert len(result.pack_calls) == len(result.chain_calls) == 1
    assert result.pack_calls[0][1]["target_provider"] == "codex"
    assert MARKER not in _material(result.chain_calls[0])
    assert TOOL_RESULT not in _material(result.chain_calls[0])


def test_pack_failure_runs_legacy_draft_without_error(run_hook, monkeypatch):
    import llm_router.context_pack as cp

    attempts = []

    def broken(*args, **kwargs):
        attempts.append((args, kwargs))
        raise RuntimeError("pack unavailable")

    monkeypatch.setattr(cp, "build_pack", broken)
    result = run_hook()
    assert len(attempts) == len(result.chain_calls) == 1
    assert attempts[0][0] == (PROMPT,)
    assert result.chain_calls[0][0][0] == PROMPT
    assert result.chain_calls[0][1].get("okf_inject", True) is True
    assert MARKER in _material(result.chain_calls[0])


def test_history_relay_off_withholds_transcript_even_in_all_mode(run_hook):
    result = run_hook(mode="all", relay="off")
    assert len(result.pack_calls) == len(result.chain_calls) == 1
    assert MARKER in json.dumps(result.packs[0].recent)
    assert result.chain_calls[0][1]["history"] == []
    assert MARKER not in _material(result.chain_calls[0])
    assert any("HISTORY RELAY OFF (privacy gate)" in line for line in result.log)


@pytest.mark.parametrize("needs_tools,readonly_loop", [(True, False), (False, True)])
@pytest.mark.parametrize("relay", ["on", "off"])
def test_agent_paths_receive_recent_turns_only_when_relay_on(
    run_hook, needs_tools, readonly_loop, relay,
):
    result = run_hook(needs_tools=needs_tools, readonly_loop=readonly_loop, relay=relay)
    assert len(result.pack_calls) == len(result.agent_calls) == 1
    args, kwargs = result.agent_calls[0]
    assert args[0] == PROMPT
    assert kwargs["read_only"] is True
    assert kwargs["session_id"] == "sess-p11hook"
    if relay == "on":
        assert f"[user]\nEarlier retry question: {MARKER}" in kwargs["context"]
        assert "[tool_use: Read]" in kwargs["context"]
        assert TOOL_RESULT in kwargs["context"]
    else:
        assert MARKER not in (kwargs["context"] or "")
        assert TOOL_RESULT not in (kwargs["context"] or "")
    assert len(result.chain_calls) == (0 if needs_tools else 1)


def test_pack_project_injects_once_and_disables_executor_injection(run_hook, monkeypatch):
    import llm_router.context_injection as ci

    injected = []

    def inject(request, **kwargs):
        injected.append((request, kwargs))
        return "KNOWLEDGE\n\n" + request

    monkeypatch.setattr(ci, "inject", inject)
    result = run_hook()
    assert len(result.pack_calls) == len(result.chain_calls) == len(injected) == 1
    assert injected[0][0] == PROMPT
    assert injected[0][1]["root"] == str(result.project)
    assert result.packs[0].project == "KNOWLEDGE"
    assert "KNOWLEDGE" in result.chain_calls[0][1]["context"]
    assert result.chain_calls[0][1]["okf_inject"] is False
