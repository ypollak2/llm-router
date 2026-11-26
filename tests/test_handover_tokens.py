"""LLM_ROUTER_HANDOVER_TOKENS: the status-bar hook asks for a hand-over file at N context tokens.

Context size = last main-chain assistant usage in the transcript tail (input + cache_read +
cache_creation). Off by default; fails open; stdout is one JSON object or nothing.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "status-bar.py"
SECRET = "SECRET-PROMPT-TEXT-xyz"


def _load():
    spec = importlib.util.spec_from_file_location("status_bar_handover", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LLM_ROUTER_HANDOVER_TOKENS", raising=False)
    (tmp_path / "state").mkdir()
    return _load()


def _transcript(tmp_path, total, name="t.jsonl", extra=()):
    rec = {"type": "assistant", "isSidechain": False,
           "message": {"content": [{"type": "text", "text": SECRET}],
                       "usage": {"input_tokens": 2, "cache_read_input_tokens": total - 2 - 1000,
                                 "cache_creation_input_tokens": 1000, "output_tokens": 50}}}
    p = tmp_path / name
    p.write_text("\n".join([json.dumps({"type": "user", "message": {"content": SECRET}}),
                            json.dumps(rec), *extra]) + "\n")
    return str(p)


def _run(mod, monkeypatch, payload, cache=True):
    if cache:
        Path(mod._cache_path()).write_text(
            json.dumps({"ts": time.time(), "key": mod._cache_key(), "status": "STATUS"}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        mod.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    text = out.getvalue()
    if text:
        json.loads(text)  # JSON only: one object, nothing else on stdout
    return text


def _payload(tmp_path, total, sid="s1"):
    return {"session_id": sid, "prompt": "hi", "transcript_path": _transcript(tmp_path, total)}


def test_149k_no_instruction_151k_once(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", "150000")
    low = _run(hook, monkeypatch, _payload(tmp_path, 149_000))
    assert "additionalContext" not in low and "STATUS" in low
    high = _payload(tmp_path, 151_000)
    first = json.loads(_run(hook, monkeypatch, high))["hookSpecificOutput"]
    assert first["additionalContext"].startswith("CONTEXT HAND-OVER") and "151,000" in first["additionalContext"]
    assert first["systemMessage"] == "STATUS" and "\n" not in first["additionalContext"]
    for _ in range(9):  # the next 9 prompts of the same session stay quiet
        assert "additionalContext" not in _run(hook, monkeypatch, high)


def test_fires_exactly_every_ten(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", "150000")
    high = _payload(tmp_path, 160_000)
    fired = ["additionalContext" in _run(hook, monkeypatch, high) for _ in range(21)]
    assert [i for i, f in enumerate(fired) if f] == [0, 10, 20]


def test_sessions_are_independent_and_state_is_private(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", "150000")
    assert "additionalContext" in _run(hook, monkeypatch, _payload(tmp_path, 160_000, "a"))
    assert "additionalContext" in _run(hook, monkeypatch, _payload(tmp_path, 160_000, "b"))
    state = Path(hook._state_dir()) / "handover_state.json"
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    assert SECRET not in state.read_text()


@pytest.mark.parametrize("value", [None, "0", "", "abc", "-5"])
def test_flag_off_is_byte_identical(hook, monkeypatch, tmp_path, value):
    if value is not None:
        monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", value)
    out = _run(hook, monkeypatch, _payload(tmp_path, 900_000))
    assert out == json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "systemMessage": "STATUS"}}) + "\n"
    assert not (Path(hook._state_dir()) / "handover_state.json").exists()


def test_flag_off_no_cache_prints_nothing(hook, monkeypatch, tmp_path):
    assert _run(hook, monkeypatch, _payload(tmp_path, 900_000), cache=False) == ""


def test_instruction_without_status_cache(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", "150000")
    out = json.loads(_run(hook, monkeypatch, _payload(tmp_path, 151_000), cache=False))["hookSpecificOutput"]
    assert "additionalContext" in out and "systemMessage" not in out


@pytest.mark.parametrize("kind", ["absent", "garbage", "no_usage", "dir", "none_path", "sidechain_only"])
def test_malformed_transcript_fails_open(hook, monkeypatch, tmp_path, kind):
    monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", "150000")
    tp = {"absent": str(tmp_path / "nope.jsonl"), "dir": str(tmp_path), "none_path": None}.get(kind)
    if kind == "garbage":
        (tmp_path / "g.jsonl").write_text("{not json\n\x00\xff\n")
        tp = str(tmp_path / "g.jsonl")
    if kind == "no_usage":
        (tmp_path / "n.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": "x"}}) + "\n")
        tp = str(tmp_path / "n.jsonl")
    if kind == "sidechain_only":
        (tmp_path / "s.jsonl").write_text(json.dumps({"type": "assistant", "isSidechain": True,
                                                      "message": {"usage": {"input_tokens": 999_999}}}) + "\n")
        tp = str(tmp_path / "s.jsonl")
    out = _run(hook, monkeypatch, {"session_id": "s1", "transcript_path": tp})
    assert "additionalContext" not in out and "STATUS" in out


def test_non_dict_payload_fails_open(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HANDOVER_TOKENS", "150000")
    assert "additionalContext" not in _run(hook, monkeypatch, ["x"])


def test_reads_only_the_tail(hook, monkeypatch, tmp_path):
    big = json.dumps({"type": "user", "message": {"content": "x" * 5_000_000}})
    p = tmp_path / "big.jsonl"
    p.write_text(big + "\n" + Path(_transcript(tmp_path, 160_000)).read_text())
    assert hook._context_tokens(str(p)) == 160_000


def test_the_hook_pair_is_identical():
    assert HOOK_PATH.read_bytes() == (HOOK_PATH.parents[3] / "hooks" / "status-bar.py").read_bytes()
