"""P2: one calibrated estimator; local-path estimators delegate only behind the flag."""
from __future__ import annotations

import json

import pytest

from llm_router import token_estimate
from llm_router.local_agent import compact
from llm_router.local_check import runtimes
from llm_router.local_context_guard import estimate_payload_tokens
from llm_router.proxy import local_mode

PAYLOAD = {"messages": [{"role": "user", "content": "call 1234567890 now é" * 50}], "model": "m"}
BODY = {"messages": [{"role": "user", "content": "hello 42 " * 100}], "tools": []}


def test_known_strings():
    assert token_estimate.est_tokens("") == 0
    assert token_estimate.est_tokens("1234567890") == 11  # 10 digits x 1.1
    assert token_estimate.est_tokens("a" * 36) == 10  # 3.6 chars/token
    assert token_estimate.est_tokens("é" * 4) == 6  # wide x 1.5
    assert token_estimate.est_tokens({"a": "1"}) == token_estimate.est_tokens(json.dumps({"a": "1"}))


def test_local_mode_is_the_same_formula():
    assert local_mode.estimate_prompt_tokens(PAYLOAD) == token_estimate.est_tokens(PAYLOAD)


@pytest.mark.parametrize("value,expected", [("", False), ("0", False), ("1", True), ("true", True)])
def test_flag_parse(monkeypatch, value, expected):
    monkeypatch.setenv(token_estimate.ENV_FLAG, value)
    assert token_estimate.calibrated_enabled() is expected


def test_flag_off_keeps_old_formulas(monkeypatch):
    monkeypatch.delenv(token_estimate.ENV_FLAG, raising=False)
    chars = len(json.dumps(PAYLOAD, default=str))
    assert estimate_payload_tokens(PAYLOAD) == __import__("math").ceil(chars / 3.5 * 1.15)
    assert runtimes.max_prompt_chars(32768) == int((32768 - runtimes.MAX_TOKENS - runtimes.PROMPT_RESERVE_TOKENS) * 3.0)
    payload = json.dumps(compact.to_ollama_messages(BODY)) + json.dumps(compact.ollama_tools([]))
    assert compact.estimate_tokens(BODY) == len(payload) // 4


def test_flag_on_delegates(monkeypatch):
    monkeypatch.setenv(token_estimate.ENV_FLAG, "1")
    assert estimate_payload_tokens(PAYLOAD) == token_estimate.est_tokens(PAYLOAD)
    room = 32768 - runtimes.MAX_TOKENS - runtimes.PROMPT_RESERVE_TOKENS
    assert runtimes.max_prompt_chars(32768) == token_estimate.chars_for_tokens(room)
    assert runtimes.max_prompt_chars(None) is None
    payload = json.dumps(compact.to_ollama_messages(BODY)) + json.dumps(compact.ollama_tools([]))
    assert compact.estimate_tokens(BODY) == token_estimate.est_tokens(payload)
