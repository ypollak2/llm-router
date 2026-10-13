"""P1.1 door 4: codex_agent.run_codex builds its context with build_pack(door="codex").

A fake ``codex`` executable records its argv; no real Codex and no real rollout
file is used (the rollout is a synthetic fixture).
"""

from __future__ import annotations

import asyncio
import json
import stat
import textwrap
from pathlib import Path

import pytest

from llm_router import codex_agent, context_pack
from llm_router.codex_agent import run_codex


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))


def _fake_codex(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "argv.json"
    path = tmp_path / "codex"
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""
        import json, sys
        open({str(log)!r}, "w").write(json.dumps(sys.argv))
        print(json.dumps({{"type": "item.completed",
                          "item": {{"type": "agent_message", "text": "ok"}}}}))
    """))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path), log


def _run(monkeypatch, tmp_path, prompt, **kw):
    binary, log = _fake_codex(tmp_path)
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: binary)
    res = asyncio.run(run_codex(prompt, model="m", working_dir=str(tmp_path), timeout=20, **kw))
    return res, json.loads(log.read_text())[-1]


def _spy(monkeypatch):
    calls = []
    real = context_pack.build_pack

    def spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(context_pack, "build_pack", spy)
    return calls


def _rollout(path: Path, texts: list[tuple[str, str]]) -> Path:
    lines = [json.dumps({"type": "session_meta", "payload": {"id": "x"}})]
    for role, text in texts:
        kind = "input_text" if role == "user" else "output_text"
        lines.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": role, "content": [{"type": kind, "text": text}]}}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_build_pack_called_once_with_codex_door_and_request_untruncated(monkeypatch, tmp_path):
    calls = _spy(monkeypatch)
    request = "R" * 50_000 + " END-OF-REQUEST"
    res, sent = _run(monkeypatch, tmp_path, request)
    assert res.success
    assert len(calls) == 1
    assert calls[0][1]["door"] == "codex"
    assert sent.endswith(request)
    assert "[…truncated" not in sent


def test_rollout_file_becomes_recent_messages(monkeypatch, tmp_path):
    rollout = _rollout(tmp_path / "rollout-synthetic.jsonl",
                       [("user", "earlier question ZZQ"), ("assistant", "earlier answer YYQ")])
    calls = _spy(monkeypatch)
    res, sent = _run(monkeypatch, tmp_path, "the new request", transcript_path=str(rollout))
    assert res.success
    assert calls[0][1]["transcript_path"] == str(rollout)
    assert "earlier question ZZQ" in sent and "earlier answer YYQ" in sent
    assert sent.endswith("the new request")


def test_long_tool_result_in_rollout_is_elided_in_recent(monkeypatch, tmp_path):
    out = "H" * 1_500 + "M" * 3_000 + "T" * 1_500
    rollout = tmp_path / "rollout-tool.jsonl"
    rollout.write_text("\n".join(json.dumps(x) for x in [
        {"type": "response_item", "payload": {"type": "function_call", "name": "sh",
                                              "arguments": "{}", "call_id": "c1"}},
        {"type": "response_item", "payload": {"type": "function_call_output",
                                              "call_id": "c1", "output": out}},
    ]) + "\n")
    # target_window=1 forces mode="pack", so the 7-message cap rendering applies.
    _, sent = _run(monkeypatch, tmp_path, "go", transcript_path=str(rollout), target_window=1)
    assert "[…truncated 3000 chars…]" in sent
    assert "M" * 100 not in sent


def test_missing_rollout_falls_back_to_caller_messages(monkeypatch, tmp_path):
    msgs = [{"role": "user", "content": "caller turn QQX"},
            {"role": "assistant", "content": "caller reply WWX"}]
    res, sent = _run(monkeypatch, tmp_path, "req",
                     transcript_path=str(tmp_path / "does-not-exist.jsonl"), messages=msgs)
    assert res.success
    assert "caller turn QQX" in sent and "caller reply WWX" in sent
    assert sent.endswith("req")


def test_missing_rollout_and_no_messages_sends_request_only(monkeypatch, tmp_path):
    res, sent = _run(monkeypatch, tmp_path, "only the request",
                     transcript_path=str(tmp_path / "nope.jsonl"))
    assert res.success
    assert sent == "only the request"


# -- D-43(b): the privacy gate (target_provider="codex") ----------------------

def _convo(tmp_path):
    rollout = _rollout(tmp_path / "rollout-priv.jsonl", [("user", "secret turn PRV1")])
    return rollout, [{"role": "user", "content": "caller turn PRV2"}]


@pytest.mark.parametrize("mode", ["off"])
def test_privacy_off_withholds_rollout_and_caller_messages(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", mode)
    rollout, msgs = _convo(tmp_path)
    _, sent = _run(monkeypatch, tmp_path, "req", transcript_path=str(rollout))
    assert "PRV1" not in sent
    _, sent = _run(monkeypatch, tmp_path, "req", messages=msgs)
    assert "PRV2" not in sent
    assert sent.endswith("req")


def test_privacy_all_mode_lets_session_content_through(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    rollout, msgs = _convo(tmp_path)
    _, sent = _run(monkeypatch, tmp_path, "req", transcript_path=str(rollout))
    assert "PRV1" in sent
    _, sent = _run(monkeypatch, tmp_path, "req", messages=msgs)
    assert "PRV2" in sent


def test_privacy_local_mode_withholds_from_codex(monkeypatch, tmp_path):
    # D-91: codex is cloud, so "local" keeps session content on this machine.
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    rollout, msgs = _convo(tmp_path)
    _, sent = _run(monkeypatch, tmp_path, "req", transcript_path=str(rollout))
    assert "PRV1" not in sent
    _, sent = _run(monkeypatch, tmp_path, "req", messages=msgs)
    assert "PRV2" not in sent
    assert sent.endswith("req")


def test_codex_is_not_a_privacy_local_provider(monkeypatch):
    from llm_router import session_store
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    assert not session_store.allows_session_content("codex")
    assert session_store.allows_session_content("ollama")
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    assert session_store.allows_session_content("codex")
