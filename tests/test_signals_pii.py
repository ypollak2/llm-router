"""Tests for llm_router.signals.pii — secret detection must catch the common shapes."""
from __future__ import annotations

import pytest

from llm_router.signals.pii import PiiSignal


@pytest.fixture
def detector() -> PiiSignal:
    return PiiSignal()


@pytest.mark.parametrize(
    "prompt,should_fire",
    [
        # OpenAI keys
        ("Here's my OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012", True),
        ("legacy: sk-1234567890abcdefghijklmnopqrst", True),
        # Anthropic
        ("ANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", True),
        # Google
        ("GEMINI_API_KEY=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456", True),
        # GitHub
        ("token: ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", True),
        # AWS
        ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", True),
        # Slack
        ("Slack hook: xoxb-1234567890-abcdefghij", True),
        # Private key block
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBA...", True),
        # JWT
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", True),

        # NEGATIVES — must not false-positive
        ("How do I authenticate with OpenAI?", False),
        ("My password is short", False),
        ("Use sk- prefix for keys", False),
        ("AIza is the prefix for Google API keys", False),
        ("", False),
    ],
)
def test_pii_detection(detector: PiiSignal, prompt: str, should_fire: bool) -> None:
    score = detector.evaluate(prompt)
    assert score.fires is should_fire, f"prompt={prompt!r} evidence={score.evidence}"


def test_evidence_never_leaks_the_secret(detector: PiiSignal) -> None:
    """The most important guarantee — log evidence must not contain the matched value."""
    leaky_prompts = [
        "key=sk-proj-MUSTNOTAPPEARINLOGS1234567",
        "AKIAMUSTNOTAPPEARINLOG",
        "ghp_MUSTNOTAPPEARINLOGmustnotappearinlog123",
    ]
    for prompt in leaky_prompts:
        score = detector.evaluate(prompt)
        assert "MUSTNOTAPPEAR" not in score.evidence, (
            f"PII signal leaked secret in evidence: {score.evidence!r}"
        )


def test_score_is_binary_on_match(detector: PiiSignal) -> None:
    score = detector.evaluate("OPENAI_API_KEY=sk-proj-aaaaaaaaaaaaaaaaaaaa")
    assert score.score == 1.0


def test_score_is_zero_on_clean_prompt(detector: PiiSignal) -> None:
    score = detector.evaluate("What's the weather like?")
    assert score.score == 0.0
