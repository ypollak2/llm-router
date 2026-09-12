"""A real prompt, a real model, real ledgers on disk.

Every defect the re-audit found lived in a seam: the receipt bridge that lived
in a different function than the parameter meant to reach it, the importer that
crashed on a value another component had just started producing, the endpoint
whose response shape no test had ever handed to a real client. Each side had
passing unit tests. Only running the whole thing catches that class.

These are marked ``e2e`` and are NOT in the default selection — they need a
local model and take seconds, not milliseconds.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.requires_ollama]

PROMPT = "Reply with exactly the word: pong"


@pytest.mark.asyncio
async def test_route_and_call_reaches_a_real_model(ollama_only_env, read_ledgers):
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    resp = await route_and_call(TaskType.QUERY, PROMPT, complexity_hint="simple")

    assert resp.content.strip(), "empty completion from a real model"
    assert resp.provider == "ollama", f"routed to {resp.provider}, not the only configured provider"
    assert resp.input_tokens > 0 and resp.output_tokens > 0, "token counts not recorded"
    assert resp.cost_usd == 0.0, "a local model must be free"


@pytest.mark.asyncio
async def test_a_real_call_writes_every_ledger(ollama_only_env, read_ledgers):
    """usage.db, the savings JSONL and receipts.db must all see the same call.

    This is the invariant the accounting work was about, and it had never been
    asserted end-to-end — only per-writer, with the writers mocked.
    """
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    await route_and_call(TaskType.QUERY, PROMPT, complexity_hint="simple")

    led = read_ledgers()

    assert len(led["savings_jsonl"]) == 1, (
        f"expected exactly one savings row, got {len(led['savings_jsonl'])} — "
        f"hosts={[r.get('host') for r in led['savings_jsonl']]}"
    )
    rec = led["savings_jsonl"][0]
    assert rec["cost_state"] == "known", "a local model is priced; it must not be 'unknown'"
    assert rec["external_cost"] == 0.0
    assert rec["estimated_saved"] > 0, "routing to a free model saved nothing?"
    assert rec["input_tokens"] > 0 and rec["output_tokens"] > 0

    assert led["usage_rows"], "the call never reached usage.db"

    # store_receipt is deliberately fire-and-forget so it never blocks a
    # response, so poll rather than assume it has landed. Asserting immediately
    # would make this test flaky in exactly the way that teaches people to
    # rerun a suite instead of reading it.
    for _ in range(50):
        if read_ledgers()["receipts"] >= 1:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("no receipt stored within 5s")


@pytest.mark.asyncio
async def test_token_counts_agree_across_ledgers(ollama_only_env, read_ledgers):
    """The same call must not be reported with two different token counts."""
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    resp = await route_and_call(TaskType.QUERY, PROMPT, complexity_hint="simple")
    led = read_ledgers()

    jsonl = led["savings_jsonl"][0]
    assert jsonl["input_tokens"] == resp.input_tokens
    assert jsonl["output_tokens"] == resp.output_tokens

    usage = led["usage_rows"][0]
    assert usage["input_tokens"] == resp.input_tokens, (
        f"usage.db says {usage['input_tokens']} in, response says {resp.input_tokens}"
    )
    assert usage["output_tokens"] == resp.output_tokens


def test_sdk_route_reaches_a_real_model(ollama_only_env, read_ledgers):
    """`from llm_router import route` — the path F-02 repointed at the core
    router. Nothing had ever driven it against a real provider."""
    from llm_router import route

    r = route(PROMPT, task_type="query", complexity="simple")

    assert r.text.strip()
    assert r.provider == "ollama"
    assert r.total_tokens > 0

    led = read_ledgers()
    assert len(led["savings_jsonl"]) == 1
    assert led["savings_jsonl"][0]["host"] == "sdk", (
        "the SDK's host tag did not survive the single-writer change"
    )


@pytest.mark.asyncio
async def test_two_calls_produce_two_rows_not_four(ollama_only_env, read_ledgers):
    """The double-logging defect, asserted where it actually happened."""
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    # Distinct prompts: an identical second prompt hits the semantic cache and
    # never dispatches, which would make this pass for the wrong reason.
    for word in ("alpha", "bravo"):
        await route_and_call(
            TaskType.QUERY, f"Reply with exactly the word: {word}",
            complexity_hint="simple",
        )

    rows = read_ledgers()["savings_jsonl"]
    assert len(rows) == 2, f"2 calls produced {len(rows)} savings rows"
