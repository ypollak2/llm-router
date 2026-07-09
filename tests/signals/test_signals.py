"""Tests for llm_router.signals — PII detection + force-local routing."""

import pytest

from llm_router.signals import PiiSignal, SignalScore, detect_pii, force_local_for_pii

SECRETS = [
    ("openai_key", "here is my key sk-proj-abcdefghij1234567890XYZ"),
    ("anthropic_key", "token sk-ant-abcdefghij1234567890abcdefg"),
    ("gemini_key", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"),
    ("github_token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
    ("jwt", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY.dBjftJeZ4CVP-mB92K"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB"),
    ("env_assignment", "SECRET_TOKEN=sk-abcdefghij1234567890"),
]

CLEAN = [
    "What is the capital of France?",
    "Refactor this function to use early returns",
    "def add(a, b):\n    return a + b",
    "Explain how OAuth works at a high level",
]


@pytest.mark.parametrize("label,prompt", SECRETS)
def test_pii_fires_on_secrets(label, prompt):
    s = PiiSignal().evaluate(prompt)
    assert s.fires is True
    assert s.score == 1.0


@pytest.mark.parametrize("label,prompt", SECRETS)
def test_evidence_never_leaks_the_value(label, prompt):
    s = PiiSignal().evaluate(prompt)
    # evidence names the pattern but must not contain the raw secret substring
    secret_token = prompt.split()[-1] if "=" not in prompt else prompt.split("=")[-1]
    assert secret_token not in s.evidence
    assert "matched pattern" in s.evidence


@pytest.mark.parametrize("prompt", CLEAN)
def test_no_fire_on_clean(prompt):
    assert PiiSignal().evaluate(prompt).fires is False
    assert detect_pii(prompt) is None


def test_force_local_noop_on_clean():
    chain = ["ollama/hermes3:8b", "anthropic/claude-sonnet", "openai/gpt-4o"]
    assert force_local_for_pii(chain, "What is 2+2?") == chain


def test_force_local_filters_to_local_on_secret():
    chain = ["ollama/hermes3:8b", "anthropic/claude-sonnet", "openai/gpt-4o", "vllm/mixtral"]
    out = force_local_for_pii(chain, "my key is sk-ant-abcdefghij1234567890abcdefg")
    assert out == ["ollama/hermes3:8b", "vllm/mixtral"]


def test_force_local_fail_closed_when_no_local():
    """PII + no local model in chain ⇒ empty (caller must refuse, never leak)."""
    chain = ["anthropic/claude-sonnet", "openai/gpt-4o"]
    out = force_local_for_pii(chain, "sk-proj-abcdefghij1234567890XYZ")
    assert out == []


def test_signalscore_fires_property():
    assert SignalScore("x", 0.6, 0.5).fires is True
    assert SignalScore("x", 0.4, 0.5).fires is False
