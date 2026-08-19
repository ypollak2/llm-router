"""G-025 — auto trace ID across sidecar restart.

The agentic-safety floor needs a request identifier that survives
process restart so a crashed-and-replayed call can be deduped and
correlated across the audit / lineage / log surfaces. The audit's
G-025 row called out that lineage was lost on restart unless the
caller supplied an ``idempotency_key`` manually.

Solution: a **deterministic, content-addressed** trace ID derived
from the inputs that identify "this exact turn":

* ``prompt_hash`` — SHA-256 of the prompt text, truncated.
  Different prompts always produce different trace ids.
* ``agent_id`` — when set, narrows the namespace so two agents
  asking the same prompt produce distinct traces.
* ``parent_trace_id`` — chains nested calls. The parent's id is
  hashed into the child so the lineage is reconstructable from the
  trace ids alone.
* ``turn_num`` — the agent-loop iteration count. Without this,
  two consecutive turns of the same prompt + agent would collide
  (the inner loop's first and second iterations are different
  events even though their inputs match).

The output is a stable string (``trace_`` + 16 hex chars) that:

* **Same inputs → same id.** A restart-and-replay produces the
  same id, so the audit row already exists and downstream dedup
  works without the caller engineering anything.
* **Different inputs → different id.** Even one character changed
  in the prompt shifts every output bit.
* **No PII leakage.** Only hashes of the prompt appear in the id;
  the prompt itself is never persisted in the trace value.

This is intentionally NOT a cryptographic random nonce — that
shape doesn't survive restart. Determinism is the whole point.
"""
from __future__ import annotations

import hashlib


_TRACE_ID_PREFIX = "trace_"
_TRACE_ID_HEX_LEN = 16  # 16 hex chars = 64 bits of distinguishability


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_prompt(prompt: str) -> str:
    """Public helper — the prompt hash used as one input of the
    trace id. Exposed so the caller can include it alongside the
    trace id without recomputing (and so tests can pin the exact
    SHA-256 output for a known input).
    """
    return _stable_hash(prompt)[:32]


def derive_trace_id(
    *,
    prompt: str,
    agent_id: str | None = None,
    parent_trace_id: str | None = None,
    turn_num: int = 0,
) -> str:
    """Compute the deterministic trace id from the inputs.

    Args:
        prompt: The exact prompt the call would send (or did send).
            Hashed; never persisted as-is in the output.
        agent_id: Optional agent definition id. ``None`` is treated
            as the empty string — calls that don't know their agent
            still produce a stable id.
        parent_trace_id: Trace id of the parent call when chaining.
            ``None`` for a root call.
        turn_num: Iteration count within the agent loop. Defaults
            to ``0`` for direct (non-agent) calls.

    Returns:
        A string of the form ``trace_<16 hex chars>``.

    Properties (pinned by tests):

    * Pure function — same inputs always produce the same output.
    * Different ``prompt`` / ``agent_id`` / ``parent_trace_id`` /
      ``turn_num`` produce different outputs.
    * No ``None`` input causes a TypeError — empty string is the
      sentinel.
    """
    parts = [
        hash_prompt(prompt),
        agent_id or "",
        parent_trace_id or "",
        str(int(turn_num)),
    ]
    composite = "|".join(parts)
    digest = _stable_hash(composite)[:_TRACE_ID_HEX_LEN]
    return f"{_TRACE_ID_PREFIX}{digest}"


__all__ = [
    "derive_trace_id",
    "hash_prompt",
]
