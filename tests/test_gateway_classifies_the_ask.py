"""The gateway must classify the ASK, not the transcript — audit 2026-09-22, T-03.

Every wire-compatible endpoint flattened system prompt + full history into one
string and handed that to the complexity heuristic, which thresholds on
character length (<600 simple / 600-2000 moderate / >2000 complex). So a client
with a boilerplate system preamble — which is every IDE and SDK integration,
the module's own advertised audience — pushed trivial turns up a tier:

    _classify(_flatten([user "hi"]))                 -> ('analyze', 'simple')
    _classify(_flatten([system <1.7KB>, user "hi"])) -> ('analyze', 'moderate')

The product exists to route cheap turns cheaply. On its flagship path it did
the opposite, systematically, and no test constructed a multi-role message list
to notice (TEST_GAP_ANALYSIS.md).

These tests assert the CALL SITE. Asserting that ``_latest_user_turn`` returns
the right string would pass even if no endpoint called it — which is how T-03
shipped past a suite that already tested ``classify_signals`` directly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_router import gateway as G

# Well over the 2000-char COMPLEX threshold on its own.
SYSTEM_BOILERPLATE = (
    "You are a meticulous senior engineering assistant. Follow the house style. "
    "Never invent APIs. Cite files by path and line. Prefer the smallest change. "
) * 20
ASK = "hi"


@pytest.fixture
def captured(monkeypatch):
    """Capture the payload the gateway hands the router, and stub the call."""
    seen: list[dict] = []

    async def _fake_route_payload_async(payload: dict) -> dict:
        seen.append(dict(payload))
        return {"text": "ok", "provider": "anthropic", "model": "claude-haiku-4-5",
                "input_tokens": 3, "output_tokens": 2, "cost_usd": 0.0}

    import llm_router.route_server as RS
    monkeypatch.setattr(RS, "route_payload_async", _fake_route_payload_async)
    return seen


@pytest.fixture
def client():
    return TestClient(G.app, base_url="http://127.0.0.1")


def _complexity(seen: list[dict]) -> str:
    assert seen, "the endpoint never reached the router — the test proved nothing"
    return seen[-1]["complexity"]


def _prompt(seen: list[dict]) -> str:
    assert seen, "the endpoint never reached the router — the test proved nothing"
    return seen[-1]["prompt"]


# ── the defect, per endpoint ──────────────────────────────────────────────────

def test_openai_chat_ignores_the_system_prompt_when_classifying(client, captured):
    client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [{"role": "system", "content": SYSTEM_BOILERPLATE},
                     {"role": "user", "content": ASK}],
    })
    assert _complexity(captured) == "simple", (
        f"{len(SYSTEM_BOILERPLATE)} chars of system boilerplate inflated "
        f'"{ASK}" to {_complexity(captured)!r}'
    )


def test_anthropic_messages_ignores_the_system_field_when_classifying(client, captured):
    client.post("/v1/messages", json={
        "model": "auto",
        "system": SYSTEM_BOILERPLATE,
        "messages": [{"role": "user", "content": ASK}],
        "max_tokens": 64,
    })
    assert _complexity(captured) == "simple"


def test_ollama_chat_ignores_the_system_prompt_when_classifying(client, captured):
    client.post("/api/chat", json={
        "model": "auto",
        "messages": [{"role": "system", "content": SYSTEM_BOILERPLATE},
                     {"role": "user", "content": ASK}],
    })
    assert _complexity(captured) == "simple"


def test_openai_responses_ignores_instructions_when_classifying(client, captured):
    client.post("/v1/responses", json={
        "model": "auto",
        "instructions": SYSTEM_BOILERPLATE,
        "input": [{"role": "user", "content": ASK}],
    })
    assert _complexity(captured) == "simple"


# ── history is not the ask either ─────────────────────────────────────────────

def test_a_long_prior_turn_does_not_inflate_a_trivial_followup(client, captured):
    """Assistant history is context, not difficulty.

    A ten-turn conversation makes every subsequent "thanks" look COMPLEX under
    a character-length threshold. Same bug, different source.
    """
    client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [
            {"role": "user", "content": "explain async"},
            {"role": "assistant", "content": "Async in Python. " * 300},
            {"role": "user", "content": ASK},
        ],
    })
    assert _complexity(captured) == "simple"


# ── what we SEND must not shrink ──────────────────────────────────────────────

def test_the_system_prompt_still_reaches_the_model(client, captured):
    """Classifying less must not mean sending less.

    The fix narrows the CLASSIFICATION input only. If it also narrowed the
    payload, every client's system prompt would silently stop applying — a far
    worse bug than the one being fixed, and one that looks like a quality
    regression rather than a routing change.
    """
    client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [{"role": "system", "content": SYSTEM_BOILERPLATE},
                     {"role": "user", "content": ASK}],
    })
    sent = _prompt(captured)
    assert SYSTEM_BOILERPLATE.strip()[:60] in sent, "system prompt was dropped from the payload"
    assert ASK in sent


def test_the_anthropic_system_field_still_reaches_the_model(client, captured):
    client.post("/v1/messages", json={
        "model": "auto",
        "system": SYSTEM_BOILERPLATE,
        "messages": [{"role": "user", "content": ASK}],
        "max_tokens": 64,
    })
    assert SYSTEM_BOILERPLATE.strip()[:60] in _prompt(captured)


def test_responses_instructions_still_reach_the_model(client, captured):
    client.post("/v1/responses", json={
        "model": "auto",
        "instructions": SYSTEM_BOILERPLATE,
        "input": [{"role": "user", "content": ASK}],
    })
    assert SYSTEM_BOILERPLATE.strip()[:60] in _prompt(captured)


# ── a real ask must still classify up ─────────────────────────────────────────

def test_a_genuinely_long_ask_still_classifies_up(client, captured):
    """The fix must not clamp everything to simple.

    If it did, every one of the tests above would pass for the wrong reason and
    the router would under-route instead of over-routing. This is the
    anti-vacuity guard for the whole file.
    """
    long_ask = (
        "Implement a distributed rate limiter with Redis, covering token bucket "
        "and sliding window, with failover, tests and benchmarks. " * 25
    )
    client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [{"role": "user", "content": long_ask}],
    })
    assert _complexity(captured) != "simple", (
        f"a {len(long_ask)}-char ask classified as simple — the classifier is "
        "no longer reading the user turn at all"
    )


def test_no_user_turn_falls_back_to_the_full_prompt(client, captured):
    """Zero user messages must not classify an empty string as simple.

    An empty classification input takes the `simple` branch for every request —
    the same bug pointed the other way, and one that would look like a win.
    """
    client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [{"role": "system", "content": SYSTEM_BOILERPLATE},
                     {"role": "assistant", "content": SYSTEM_BOILERPLATE}],
    })
    assert _complexity(captured) != "simple"


# ── the native path is unchanged ──────────────────────────────────────────────

def test_the_native_route_endpoint_still_classifies_its_whole_prompt(captured):
    """On `/route` the caller's prompt IS the ask — there is no transcript to strip.

    Pinned so a later "just always use the last user turn" simplification cannot
    quietly change the native path's behaviour.
    """
    import asyncio

    long_prompt = "Design a multi-region failover strategy. " * 80
    asyncio.run(G._route(long_prompt, None, None))
    assert _complexity(captured) != "simple"
