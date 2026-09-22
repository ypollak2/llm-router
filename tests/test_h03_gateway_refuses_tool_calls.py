"""H-03 — the gateway silently discarded tool definitions.

`_OAIRequest` had no `tools` field, so Pydantic dropped it **before the handler
body ran** — no code path could have logged it even if one had wanted to. And
`finish_reason` was the literal string `"stop"`, so a client could never observe
`tool_use` either.

An OpenAI-compatible client doing function calling therefore received a
well-formed, plausible answer to a question it had not asked, with nothing
anywhere to indicate its tools had been thrown away. A wrong answer that looks
right and reports success is the worst shape a failure can take.

**Why this refuses rather than implements.** The router returns text:
`route_and_call` has no tool-call channel and no backend in the chain is wired
for one. Building that is a feature, not a remediation, and half-building it
would reproduce the same silent wrongness somewhere new. Refusing is the honest
behaviour — the client learns immediately, in its own protocol, that this
gateway cannot serve the request, and is told what to do instead.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from llm_router import gateway  # noqa: E402

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]


# The gateway blocks cross-origin requests (CHZ-SEC-04: browser CSRF and DNS
# rebinding against a loopback port that can trigger paid model calls).
# TestClient sends `Host: testserver`, which that guard correctly rejects with
# 403 — so a loopback Host is required to reach the handler at all.
LOOPBACK = {"Host": "127.0.0.1:8080"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """T-22: this file must not touch the network.

    Measured during the audit: 14s wall clock, an OpenAI auth error, an Ollama
    connect attempt, and a LIVE Codex call returning 200 in 8.2s. A test that
    refuses tool calls should never reach a provider — if it does, either the
    refusal did not fire or the assertion is measuring the wrong thing, and
    both look like a pass when the network happens to be up.

    Blocking the socket rather than timing the run: "it was fast" is evidence
    about this machine, "it could not connect" is evidence about the code.
    Loopback is left open because `TestClient` uses it in-process.
    """
    import socket

    real_connect = socket.socket.connect

    def _blocked(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address, *a, **kw)
        raise AssertionError(
            f"this test reached the network ({host}). The gateway must refuse a "
            f"tool-call request before any provider is contacted (T-22)."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    yield


@pytest.fixture
def client():
    return TestClient(gateway.app, raise_server_exceptions=False)


def test_openai_chat_refuses_a_tool_call_request(client):
    r = client.post("/v1/chat/completions", headers=LOOPBACK, json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "what is the weather in Lisbon"}],
        "tools": TOOLS,
    })
    assert r.status_code == 400, (
        f"gateway returned {r.status_code}; a dropped tool definition must not "
        f"look like success"
    )
    detail = r.json().get("detail", "")
    assert "tool" in detail.lower()
    assert "remove" in detail.lower(), "the refusal must say what to do instead"


def test_anthropic_messages_refuses_a_tool_call_request(client):
    r = client.post("/v1/messages", headers=LOOPBACK, json={
        "model": "claude-opus-4-6",
        "messages": [{"role": "user", "content": "what is the weather in Lisbon"}],
        "tools": TOOLS,
    })
    assert r.status_code == 400, f"anthropic endpoint returned {r.status_code}"


def test_a_forced_tool_choice_is_also_refused(client):
    """`tool_choice` without `tools` still asks for something unsupported."""
    r = client.post("/v1/chat/completions", headers=LOOPBACK, json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
        "tool_choice": {"type": "function", "function": {"name": "x"}},
    })
    assert r.status_code == 400


@pytest.mark.parametrize("payload", [
    {"tools": []},              # explicitly empty is not a request for tools
    {"tool_choice": "none"},    # explicitly declining is not a request for tools
    {},
])
def test_requests_without_tools_are_not_refused(client, payload):
    """Anti-over-correction: refusing everything would break ordinary traffic.

    Checks the guard, not the route — a routing failure here is a different
    concern and must not be mistaken for a tools refusal.
    """
    gateway._refuse_tools_if_present(payload.get("tools"), payload.get("tool_choice"))


def test_the_fields_are_visible_to_the_handler():
    """The defect was invisibility, so pin visibility.

    Without a declared field Pydantic discards the key and no guard can fire.
    """
    assert "tools" in gateway._OAIRequest.model_fields
    assert "tool_choice" in gateway._OAIRequest.model_fields
    assert "tools" in gateway._AnthropicRequest.model_fields


def test_finish_reason_is_derived_not_asserted():
    """`"stop"` is correct for a text completion; hardcoding it is not."""
    class _Truncated:
        finish_reason = "length"

    class _Normal:
        pass

    assert gateway._finish_reason(_Truncated()) == "length"
    assert gateway._finish_reason(_Normal()) == "stop"

    class _AnthropicShaped:
        stop_reason = "max_tokens"

    assert gateway._finish_reason(_AnthropicShaped()) == "length"


def test_the_literal_is_gone_from_the_response_builders():
    """Pin the mechanism: a hardcoded value cannot report a truncated answer."""
    src = pathlib.Path(gateway.__file__).read_text(encoding="utf-8")
    assert '"finish_reason": "stop"' not in src, (
        "finish_reason is hardcoded again; a client cannot distinguish a "
        "complete answer from a truncated one"
    )


def test_the_cross_origin_guard_is_still_in_force(client):
    """Not part of H-03, but it is what made these tests 403 at first.

    The audit recorded "no per-request authentication" on this gateway. That is
    true — and it is not the same as unguarded: a browser CSRF or DNS-rebinding
    request carrying a non-loopback Host is rejected before any handler runs.
    Worth keeping true while M-08 is addressed separately.
    """
    r = client.post("/v1/chat/completions", json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert r.status_code == 403, (
        "a non-loopback Host reached a handler that can trigger a paid model call"
    )
