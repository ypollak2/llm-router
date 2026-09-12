"""The gateway, end to end: real server, real SDK client, real model.

``test_f03b_gateway_sdk_conformance`` drives real SDKs against a real server but
mocks the router underneath. That proves the wire format; it cannot prove the
whole path works. This does: an official client → HTTP → the gateway → the
router → a real local model → back through the SDK's own parser.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.requires_ollama]

PROMPT = "Reply with exactly the word: pong"


def test_openai_sdk_end_to_end(live_gateway, read_ledgers):
    from openai import OpenAI

    client = OpenAI(api_key="unused", base_url=f"{live_gateway}/v1", max_retries=0)
    r = client.chat.completions.create(
        model="auto", messages=[{"role": "user", "content": PROMPT}], max_tokens=32,
    )

    assert r.choices[0].message.content.strip(), "empty completion"
    assert r.usage.total_tokens > 0
    assert "ollama" in r.model

    rows = read_ledgers()["savings_jsonl"]
    assert len(rows) == 1, f"one gateway call produced {len(rows)} savings rows"
    assert rows[0]["host"] == "gateway", (
        f"host tag lost on the real path: {rows[0]['host']!r}"
    )


def test_responses_endpoint_end_to_end(live_gateway):
    """The endpoint whose shape no test had ever handed to a real client."""
    from openai import OpenAI

    client = OpenAI(api_key="unused", base_url=f"{live_gateway}/v1", max_retries=0)
    r = client.responses.create(model="auto", input=PROMPT)

    assert r.output_text.strip()
    assert r.status == "completed"


def test_anthropic_sdk_end_to_end(live_gateway):
    from anthropic import Anthropic

    client = Anthropic(api_key="unused", base_url=live_gateway, max_retries=0)
    # "auto" = let the router pick. Naming an anthropic model here would be
    # correctly refused: this environment has no Anthropic credentials, and the
    # availability gate holds an override to the same bar as a chain entry.
    msg = client.messages.create(
        model="auto", max_tokens=32,
        system="Answer in one word.",
        messages=[{"role": "user", "content": PROMPT}],
    )

    assert msg.content[0].text.strip()
    assert msg.stop_reason == "end_turn"


def test_policy_refuses_an_unavailable_provider_over_http(live_gateway):
    """The F-01 availability gate, exercised through a real client on the real
    wire rather than by calling the chain builder directly."""
    from anthropic import BadRequestError, Anthropic

    client = Anthropic(api_key="unused", base_url=live_gateway, max_retries=0)
    with pytest.raises(BadRequestError) as exc:
        client.messages.create(
            model="claude-haiku-4-5", max_tokens=32,
            messages=[{"role": "user", "content": PROMPT}],
        )
    assert "not available" in str(exc.value)


def test_ollama_endpoint_end_to_end(live_gateway, ollama_model):
    """Options are honoured now, so a real call must accept them."""
    import httpx

    r = httpx.post(f"{live_gateway}/api/chat", timeout=120, json={
        "model": f"ollama/{ollama_model}",
        "messages": [{"role": "user", "content": PROMPT}],
        "options": {"temperature": 0.1, "num_predict": 32},
    })
    assert r.status_code == 200, r.text
    assert r.json()["message"]["content"].strip()


def test_refusals_are_real_over_http(live_gateway):
    """The 400 path, end to end, with no mock deciding it."""
    from openai import BadRequestError, OpenAI

    client = OpenAI(api_key="unused", base_url=f"{live_gateway}/v1", max_retries=0)
    with pytest.raises(BadRequestError):
        client.chat.completions.create(
            model="auto", messages=[{"role": "user", "content": PROMPT}], stream=True,
        )
