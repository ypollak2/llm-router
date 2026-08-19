"""Regression tests for the model-aware token counter.

count_tokens replaces the chars/4 heuristic for cost-attribution paths
(dashboards, quota enforcement, audit). It uses tiktoken when available
and falls back to chars/4 when tiktoken is missing, the model is
unknown, or encoding load fails — so the contract is:

* Always returns ``int >= 1``.
* Never raises (tokenizer errors swallowed → chars/4 fallback).
* Is closer to the model's real tokenizer than chars/4 when tiktoken
  is installed and the model is known.
"""

from __future__ import annotations

import pytest

from llm_router.token_budget import (
    count_tokens,
    estimate_tokens,
    _HAS_TIKTOKEN,
)


def test_count_tokens_returns_at_least_one():
    assert count_tokens("") == 1
    assert count_tokens(" ", model="gpt-4o") == 1
    assert count_tokens("hi") >= 1


def test_count_tokens_never_raises_on_unknown_model():
    out = count_tokens("hello world", model="some-future-model-name")
    assert out >= 1


@pytest.mark.skipif(not _HAS_TIKTOKEN, reason="tiktoken not installed")
def test_count_tokens_with_gpt4o_beats_chars_div_4_on_english():
    """For real English text, tiktoken should give a different (more
    accurate) count than chars/4."""
    text = (
        "The quick brown fox jumps over the lazy dog. "
        "Premature optimization is the root of all evil."
    )
    heuristic = estimate_tokens(text)
    accurate = count_tokens(text, model="gpt-4o")
    # They must differ — otherwise the new code path is no better than
    # the old one. A real tokenizer also yields a non-tiny number.
    assert accurate != heuristic
    assert accurate > 5


@pytest.mark.skipif(not _HAS_TIKTOKEN, reason="tiktoken not installed")
def test_count_tokens_handles_code_realistically():
    """Code tokenizes very differently from English (lots of punctuation
    and short tokens). Confirm the counter handles it."""
    code = "def f(x):\n    return x ** 2 + 3 * x - 1\n"
    accurate = count_tokens(code, model="gpt-4o")
    assert 5 <= accurate <= 40, (
        f"Expected reasonable code-token count, got {accurate}"
    )


def test_count_tokens_falls_back_when_encoding_missing(monkeypatch):
    """If _get_encoding returns None, chars/4 must take over."""
    monkeypatch.setattr(
        "llm_router.token_budget._get_encoding",
        lambda _model: None,
    )
    text = "a" * 100  # 100 chars → 25 tokens via chars/4
    assert count_tokens(text, model="gpt-4o") == 25


def test_estimate_tokens_kept_for_hot_path():
    """The legacy heuristic stays — its perf characteristics differ.

    Some hot paths (budget checks at routing time) prefer the
    allocation-free chars/4 over a tiktoken call. estimate_tokens()
    must still exist and behave as before.
    """
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 40) == 10
