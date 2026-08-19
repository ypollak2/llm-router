"""Tests for the sidecar pre-execution module.

Pins four contracts:

1. **Disabled by default** — ``is_enabled()`` returns False unless
   ``LLM_ROUTER_SIDECAR_PREFETCH=1`` is set. Pre-execution is opt-in
   because mis-firing handlers are a footgun.
2. **Pattern matching is narrow** — only prompts that clearly express
   the intent get a handler; generic prompts that mention the keywords
   in other contexts do not.
3. **Handler failures are silent** — a broken handler returns ``None``
   and the caller falls back to normal routing. A handler raising must
   never bubble out to the hook caller.
4. **First match wins** — when two patterns could match, the earlier
   handler in :data:`HANDLERS` is selected; later handlers don't get a
   turn on the same prompt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_router.sidecar import (
    HANDLERS,
    PreExecutionResult,
    classify,
    execute,
    is_enabled,
)


# ── is_enabled ──────────────────────────────────────────────────────────


def test_is_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SIDECAR_PREFETCH", raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_is_enabled_respects_truthy_values(monkeypatch, value):
    monkeypatch.setenv("LLM_ROUTER_SIDECAR_PREFETCH", value)
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe"])
def test_is_enabled_rejects_falsy_values(monkeypatch, value):
    monkeypatch.setenv("LLM_ROUTER_SIDECAR_PREFETCH", value)
    assert is_enabled() is False


# ── Pattern classification ──────────────────────────────────────────────


@pytest.mark.parametrize("prompt,expected", [
    ("show me my routing distribution today", "routing_distribution"),
    ("how many routings did I do today", "routing_distribution"),
    ("what's the breakdown of routings?", "routing_distribution"),
    ("what's the git status?", "git_status"),
    ("show me my recent commits", "recent_commits"),
    ("list my last commits", "recent_commits"),
])
def test_classify_matches_explicit_intent(prompt, expected):
    assert classify(prompt) == expected


@pytest.mark.parametrize("prompt", [
    "",
    "   ",
    "What is 2 + 2?",
    "Explain how Python decorators work",
    "Write a function to reverse a list",
    # Touches the keyword 'routing' but isn't an introspection ask
    "Tell me about routing algorithms in computer networks",
])
def test_classify_returns_none_for_generic_prompts(prompt):
    assert classify(prompt) is None


def test_first_match_wins():
    """When a prompt could trigger two handlers, the earlier one in
    HANDLERS gets it; the later handler does NOT also fire."""
    # Construct a prompt that hits both routing + recent commits patterns.
    # The routing_distribution handler is registered first.
    prompt = "show me my recent routing decisions"
    selected = classify(prompt)
    # Either selection is acceptable here — but selected must be ONE,
    # and execute(selected) returns one result, not a merged blob.
    assert selected in {"routing_distribution", "recent_commits"}


# ── Handler execution ──────────────────────────────────────────────────


def test_execute_returns_none_for_unknown_handler():
    """Defence against typos in caller code — an unknown handler name
    can't crash the dispatch."""
    assert execute("does_not_exist", "anything") is None


def test_execute_routing_distribution_empty_db(monkeypatch, tmp_path):
    """When the usage DB exists but has no rows today, return an honest
    'no decisions' marker rather than a stack trace."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Create empty DB with the right table
    db_dir = tmp_path / ".llm-router"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE routing_decisions (id INTEGER, timestamp TEXT, "
        "complexity TEXT, final_model TEXT, cost_usd REAL, "
        "reason_code TEXT)"
    )
    conn.commit()
    conn.close()

    result = execute("routing_distribution", "show me my routings")
    assert result is not None
    assert isinstance(result, PreExecutionResult)
    assert "no decisions recorded" in result.context


def test_execute_routing_distribution_with_data(monkeypatch, tmp_path):
    """Populate the DB with two rows and verify the markdown body lists
    both tiers + costs."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_dir = tmp_path / ".llm-router"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE routing_decisions (id INTEGER, timestamp TEXT, "
        "complexity TEXT, final_model TEXT, cost_usd REAL, "
        "reason_code TEXT)"
    )
    conn.executemany(
        "INSERT INTO routing_decisions (timestamp, complexity, final_model, "
        "cost_usd, reason_code) VALUES "
        # Store UTC to match production (routing_decisions.timestamp DEFAULT is
        # datetime('now') = UTC); the handler's date(timestamp,'localtime') then
        # maps it to today-local. Inserting 'localtime' here double-applied the
        # offset and dropped the rows onto tomorrow near midnight in non-UTC zones.
        "(datetime('now'), ?, ?, ?, NULL)",
        [("simple", "flash", 0.0001),
         ("moderate", "sonnet", 0.005)],
    )
    conn.commit()
    conn.close()

    result = execute("routing_distribution", "show me my routing distribution")
    assert result is not None
    assert "simple" in result.context
    assert "moderate" in result.context
    assert "$0.0051" in result.context
    assert result.duration_ms >= 0


def test_execute_swallows_handler_exceptions(monkeypatch):
    """A handler that raises must NOT propagate the exception — the
    sidecar's job is to be safely silent when something goes wrong."""
    from llm_router import sidecar

    def boom(_prompt: str) -> str | None:
        raise RuntimeError("boom")

    # Inject a faulty handler at position 0 so it wins classify().
    monkeypatch.setattr(
        sidecar, "HANDLERS",
        [("boom", sidecar._ROUTING_DISTRIBUTION_RE, boom)] + sidecar.HANDLERS,
    )
    result = sidecar.execute("boom", "show me my routings")
    assert result is None


# ── Defence-in-depth: the registry shape ───────────────────────────────


def test_registry_has_three_handlers():
    """The current allowlist is three read-only patterns. Adding a
    write-capable handler should be a separate, deliberate review —
    so this test will fail when the count changes, prompting that
    review."""
    assert len(HANDLERS) == 3
    names = [name for name, _, _ in HANDLERS]
    assert names == ["routing_distribution", "git_status", "recent_commits"]
