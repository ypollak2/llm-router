"""Judge-driven model cascade — synchronous scoring + escalation trigger.

Background
----------
``llm_router.judge`` runs in fire-and-forget mode: after a routed call
completes, a background task scores the response and writes the score
into ``routing_decisions``. That's good for offline quality tracking
but doesn't help the *current* turn — if the cheap model produced a
weak answer, the user sees it before the score lands.

This module adds the **synchronous** path: score a fraction of routed
calls inline, and when the score is below ``threshold``, signal the
caller to retry with the next model in the chain (or a configurable
stronger tier). That closes the quality feedback loop within the same
turn — the audit's #2 recommendation (cascade-driving judge).

Two surfaces:

* :func:`should_cascade` — pure decision function. Given a score and a
  threshold, return True to retry. Trivial wrapper but isolating it
  makes the policy editable in one place.
* :func:`judge_inline` — call the cheap judge model synchronously,
  parse the score, and return it. Designed for hot paths: short
  prompt, deterministic temperature, hard token cap. Falls back to
  ``None`` on any failure so callers can short-circuit rather than
  block on a degraded judge provider.

Configuration is via env vars so it can be flipped per session without
code changes:

* ``LLM_ROUTER_JUDGE_CASCADE_THRESHOLD`` — default ``0.0`` (cascade
  disabled). Set to e.g. ``0.6`` to cascade when the judge scores
  below 60%.
* ``LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE`` — default ``0.0`` (no inline
  judging). Set to e.g. ``0.25`` to inline-judge 25% of calls.
* ``LLM_ROUTER_JUDGE_MODEL`` — override the judge model
  (default ``claude-haiku-4-5-20251001``).

When both threshold and sample-rate are nonzero, the router (caller)
queries :func:`judge_inline` for the sampled fraction and uses the
score to decide whether to cascade. The remaining (1 - sample_rate)
of calls skip inline judging entirely — the existing background judge
in :mod:`llm_router.judge` still scores them for offline tracking.
"""

from __future__ import annotations

import os
import random
import re

from llm_router.providers import call_llm


__all__ = [
    "JUDGE_DISABLED",
    "should_cascade",
    "judge_inline",
    "_cascade_threshold",
    "_cascade_sample_rate",
]


# Sentinel: when threshold is 0.0 or negative the feature is fully disabled,
# which is the production default until a benchmarked threshold is chosen.
JUDGE_DISABLED = 0.0


def _cascade_threshold() -> float:
    """Threshold below which the judge triggers a cascade.

    Read from ``LLM_ROUTER_JUDGE_CASCADE_THRESHOLD``; returns ``0.0`` (disabled)
    on any parse failure so a typo in env doesn't accidentally turn the
    feature on with a junk threshold.
    """
    raw = os.environ.get("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "").strip()
    if not raw:
        return JUDGE_DISABLED
    try:
        value = float(raw)
    except ValueError:
        return JUDGE_DISABLED
    if value < 0.0 or value > 1.0:
        return JUDGE_DISABLED
    return value


def _cascade_sample_rate() -> float:
    """Fraction of calls to inline-judge. Default 0.0 (off)."""
    raw = os.environ.get("LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, value))


def should_judge_inline(rng: random.Random | None = None) -> bool:
    """Roll the sampling die. Default RNG is module-level — tests pass a
    seeded ``Random`` to make decisions deterministic."""
    rate = _cascade_sample_rate()
    if rate <= 0.0:
        return False
    r = rng or random
    return r.random() < rate


def should_cascade(score: float | None,
                   threshold: float | None = None) -> bool:
    """Return True iff ``score`` is finite, below ``threshold``, and the
    feature is enabled.

    Treating ``None`` as "don't cascade" matches our overall design
    principle: a degraded judge must not be allowed to amplify failures
    by triggering retries on every call. If the score didn't come back,
    the original response stands.
    """
    if score is None:
        return False
    effective = threshold if threshold is not None else _cascade_threshold()
    if effective <= JUDGE_DISABLED:
        return False
    return score < effective


# ── Inline scoring ────────────────────────────────────────────────────────


_JUDGE_PROMPT_TEMPLATE = """\
You are an evaluator. Score the response to the user prompt below on a
single scale from 0 (useless) to 1 (excellent). Consider relevance,
completeness, and correctness for the stated task type.

Reply with ONLY a JSON object of the form {{"score": 0.NN}} — no prose,
no markdown.

Task type: {task_type}

User prompt:
---
{prompt}
---

Response to evaluate:
---
{response}
---
"""


def _build_judge_prompt(prompt: str, response: str, task_type: str) -> str:
    # Truncate aggressively — the judge model gets a hard token cap,
    # and the most important signal is whether the response answered
    # the question, not the full context.
    p = (prompt or "")[:1500]
    r = (response or "")[:1500]
    return _JUDGE_PROMPT_TEMPLATE.format(
        task_type=task_type or "query",
        prompt=p,
        response=r,
    )


_SCORE_RE = re.compile(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)')


def _parse_score(text: str) -> float | None:
    """Extract the numeric score from the judge model's reply.

    Tolerates both pure JSON and prose-wrapped JSON because cheap
    judge models sometimes ignore the "no prose" instruction.
    """
    if not text:
        return None
    m = _SCORE_RE.search(text)
    if not m:
        return None
    try:
        score = float(m.group(1))
    except ValueError:
        return None
    return max(0.0, min(1.0, score))


async def judge_inline(prompt: str,
                       response: str,
                       task_type: str,
                       *,
                       model: str | None = None) -> float | None:
    """Synchronously score ``response`` for cascade-decisioning.

    Returns ``None`` on any failure — the caller treats that as "leave
    the original response alone". The hard limits (50 tokens, t=0,
    1500-char prompts) keep per-call cost under ~$0.0001 even with the
    Anthropic-tier judge models.
    """
    judge_model = model or os.environ.get(
        "LLM_ROUTER_JUDGE_MODEL", "claude-haiku-4-5-20251001"
    )
    try:
        judge_response = await call_llm(
            model=judge_model,
            messages=[{
                "role": "user",
                "content": _build_judge_prompt(prompt, response, task_type),
            }],
            temperature=0.0,
            max_tokens=50,
        )
    except Exception:
        return None
    return _parse_score(judge_response.content)
