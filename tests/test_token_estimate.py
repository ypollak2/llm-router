"""P2: one calibrated estimator; local-path estimators delegate only behind the flag."""
from __future__ import annotations

import json
import math

import pytest

from llm_router import token_estimate
from llm_router.local_agent import compact
from llm_router.local_check import runtimes
from llm_router.local_context_guard import CALIBRATED_SAFETY_MARGIN, estimate_payload_tokens
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


SHAPES = [
    PAYLOAD,
    {"messages": [{"role": "user", "content": "x"}]},
    {"messages": [{"role": "user", "content": "日本語のテキスト " * 40}], "tools": [{"name": "t", "input_schema": {"type": "object"}}]},
    {"messages": [{"role": "user", "content": "\n".join(f"line {i}: 0x{i:08x} ok" for i in range(300))}]},
    {},
]


@pytest.mark.parametrize("payload", SHAPES)
def test_flag_off_keeps_old_formulas(monkeypatch, payload):
    monkeypatch.delenv(token_estimate.ENV_FLAG, raising=False)
    chars = len(json.dumps(payload, default=str))
    assert estimate_payload_tokens(payload) == math.ceil(max(1, chars) / 3.5 * 1.15)
    assert runtimes.max_prompt_chars(32768) == int((32768 - runtimes.MAX_TOKENS - runtimes.PROMPT_RESERVE_TOKENS) * 3.0)
    body = {"messages": [{"role": "user", "content": json.dumps(payload)}], "tools": []}
    raw = json.dumps(compact.to_ollama_messages(body)) + json.dumps(compact.ollama_tools([]))
    assert compact.estimate_tokens(body) == len(raw) // 4


def test_flag_on_delegates(monkeypatch):
    monkeypatch.setenv(token_estimate.ENV_FLAG, "1")
    legacy = math.ceil(max(1, len(json.dumps(PAYLOAD, default=str))) / 3.5 * 1.15)
    calibrated = math.ceil(token_estimate.est_tokens(PAYLOAD) * CALIBRATED_SAFETY_MARGIN)
    assert estimate_payload_tokens(PAYLOAD) == max(legacy, calibrated)
    assert CALIBRATED_SAFETY_MARGIN == 1.05
    payload = json.dumps(compact.to_ollama_messages(BODY)) + json.dumps(compact.ollama_tools([]))
    assert compact.estimate_tokens(BODY) == token_estimate.est_tokens(payload)


@pytest.mark.parametrize("payload", SHAPES)
def test_flag_on_guard_never_looser_than_legacy(monkeypatch, payload):
    monkeypatch.setenv(token_estimate.ENV_FLAG, "1")
    chars = len(json.dumps(payload, default=str))
    assert estimate_payload_tokens(payload) >= math.ceil(max(1, chars) / 3.5 * 1.15)


def test_flag_on_unserialisable_payload_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv(token_estimate.ENV_FLAG, "1")
    payload = {1: 2, (1, 2): "x" * 400}  # tuple key: json.dumps raises TypeError even with default=str
    legacy = math.ceil(max(1, sum(len(str(v)) for v in payload.values())) / 3.5 * 1.15)
    assert estimate_payload_tokens(payload) == legacy


def test_flag_on_char_budget_stays_conservative_for_digit_heavy_text(monkeypatch):
    monkeypatch.setenv(token_estimate.ENV_FLAG, "1")
    budget = runtimes.max_prompt_chars(32768)
    assert budget == int((32768 - runtimes.MAX_TOKENS - runtimes.PROMPT_RESERVE_TOKENS) * 3.0)
    assert runtimes.max_prompt_chars(None) is None
    room = 32768 - runtimes.MAX_TOKENS - runtimes.PROMPT_RESERVE_TOKENS
    digit_text = "1234567890" * (budget // 10)  # fits the char budget, but is ~1.1 tokens per char
    assert len(digit_text) <= budget
    assert token_estimate.est_tokens(digit_text) > room  # the text check catches what chars cannot
