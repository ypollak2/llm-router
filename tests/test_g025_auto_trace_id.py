"""G-025 — deterministic auto trace ID derivation.

A restart-and-replay produces the same trace id, so dedup /
correlation work without the caller supplying an idempotency_key.
Different inputs (prompt, agent_id, parent_trace_id, turn_num)
produce different trace ids — collision space is 16 hex chars
(64 bits) which is enough for the per-org call volumes the audit
considered.
"""
from __future__ import annotations

import pytest

from llm_router.trace_id import derive_trace_id, hash_prompt


# ── 1. Shape ──────────────────────────────────────────────────────────────


def test_output_has_documented_prefix_and_length() -> None:
    tid = derive_trace_id(prompt="hello")
    assert tid.startswith("trace_")
    # 16 hex chars after the prefix.
    body = tid[len("trace_"):]
    assert len(body) == 16
    int(body, 16)  # parses as hex


def test_hash_prompt_returns_32_hex_chars() -> None:
    """The public ``hash_prompt`` helper is the per-prompt identifier
    callers can persist alongside the trace id for forensic
    correlation. Pin the length so a future "use blake3" refactor
    doesn't silently shorten it."""
    h = hash_prompt("hello")
    assert len(h) == 32
    int(h, 16)


# ── 2. Determinism (the load-bearing property) ────────────────────────────


def test_same_inputs_produce_same_trace_id() -> None:
    """The whole point of G-025 — restart-and-replay produces the
    same id so dedup works without an opaque idempotency key."""
    inputs = dict(
        prompt="write a haiku",
        agent_id="doc-summariser",
        parent_trace_id="trace_abc",
        turn_num=3,
    )
    assert derive_trace_id(**inputs) == derive_trace_id(**inputs)


def test_repeatable_across_function_calls() -> None:
    a = derive_trace_id(prompt="x")
    b = derive_trace_id(prompt="x")
    c = derive_trace_id(prompt="x")
    assert a == b == c


# ── 3. Input sensitivity (no collisions across distinct inputs) ───────────


@pytest.mark.parametrize(
    "input_changes",
    [
        {"prompt": "different"},
        {"agent_id": "different"},
        {"parent_trace_id": "different"},
        {"turn_num": 99},
    ],
)
def test_each_input_field_affects_output(input_changes: dict) -> None:
    base = dict(
        prompt="write a haiku",
        agent_id="doc-summariser",
        parent_trace_id="trace_abc",
        turn_num=3,
    )
    a = derive_trace_id(**base)
    b = derive_trace_id(**{**base, **input_changes})
    assert a != b, f"changing {list(input_changes)[0]} did not change output"


def test_prompt_one_char_change_changes_output() -> None:
    """The hash propagates — even a single-character diff in the
    prompt flips many bits of the output (avalanche)."""
    a = derive_trace_id(prompt="hello world")
    b = derive_trace_id(prompt="hello worle")
    assert a != b


def test_turn_num_zero_vs_one_distinguishable() -> None:
    """Two consecutive iterations of the same prompt+agent must
    produce distinct ids — without this every audit row in the
    inner loop of an agent would collide."""
    base = dict(prompt="x", agent_id="a", parent_trace_id=None)
    a = derive_trace_id(**base, turn_num=0)
    b = derive_trace_id(**base, turn_num=1)
    assert a != b


# ── 4. None-handling (callers don't always know the parent / agent) ──────


def test_none_agent_id_treated_as_empty() -> None:
    """Direct calls (no agent context) must still produce a stable
    id without the caller pre-coercing to empty string."""
    a = derive_trace_id(prompt="x", agent_id=None)
    b = derive_trace_id(prompt="x", agent_id="")
    assert a == b


def test_none_parent_trace_id_treated_as_empty() -> None:
    a = derive_trace_id(prompt="x", parent_trace_id=None)
    b = derive_trace_id(prompt="x", parent_trace_id="")
    assert a == b


def test_default_turn_num_is_zero() -> None:
    a = derive_trace_id(prompt="x")
    b = derive_trace_id(prompt="x", turn_num=0)
    assert a == b


# ── 5. Lineage scenarios (the actual user value) ─────────────────────────


def test_parent_child_chain_distinguishable() -> None:
    """An agent spawns a sub-agent — both calls have the same
    prompt, but the child's parent_trace_id differs, so the trace
    ids differ. This is what makes the lineage reconstructable."""
    parent = derive_trace_id(prompt="research X")
    child_a = derive_trace_id(
        prompt="research X", parent_trace_id=parent,
    )
    child_b = derive_trace_id(
        prompt="research X", parent_trace_id="other_parent",
    )
    assert parent != child_a
    assert child_a != child_b


def test_restart_and_replay_dedup_scenario() -> None:
    """The canonical use case. A worker dies mid-call; the
    supervisor restarts and re-issues the same logical call. The
    audit row already has this trace id, so the second issuance is
    a dedup. Pin that the supervisor doesn't need to remember
    anything — the same inputs produce the same trace id."""
    pre_crash = derive_trace_id(
        prompt="summarise this doc",
        agent_id="ds",
        parent_trace_id="trace_root",
        turn_num=5,
    )
    post_restart = derive_trace_id(
        prompt="summarise this doc",
        agent_id="ds",
        parent_trace_id="trace_root",
        turn_num=5,
    )
    assert pre_crash == post_restart


# ── 6. Long-prompt stability ─────────────────────────────────────────────


def test_long_prompt_does_not_blow_up_id_length() -> None:
    """Pin that a 100 KB prompt still produces a fixed-length trace
    id. SHA-256 + truncation guarantees this; the test exists so a
    future "use the full digest" refactor breaks loudly."""
    huge = "x" * 100_000
    tid = derive_trace_id(prompt=huge)
    assert tid.startswith("trace_")
    assert len(tid) == len("trace_") + 16


# ── 7. Hash determinism for a known input (regression pin) ───────────────


def test_known_input_produces_pinned_output() -> None:
    """Concrete pin so a future refactor that changes the
    composition order ("|".join order) doesn't silently re-hash
    every existing audit row's trace id."""
    tid = derive_trace_id(
        prompt="hello",
        agent_id="a",
        parent_trace_id="trace_p",
        turn_num=1,
    )
    # Recompute manually to confirm the contract — the composition
    # is ``hash_prompt(prompt) + "|" + agent_id + "|" +
    # parent_trace_id + "|" + str(turn_num)``.
    import hashlib
    composite = "|".join([
        hashlib.sha256(b"hello").hexdigest()[:32],
        "a",
        "trace_p",
        "1",
    ])
    expected = (
        "trace_"
        + hashlib.sha256(composite.encode()).hexdigest()[:16]
    )
    assert tid == expected
