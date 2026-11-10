"""Size-aware deadline for a local step on the proxy path (flag ``LLM_ROUTER_DEADLINE_BY_SIZE``, off by default).

Owner decision 2026-11-10: "Size-based, 10 min cap: deadline grows with prompt size (measured prefill
rate x 1.5, max 10 min). Stuck calls still fall back to Claude."

    deadline = min(cap, BASE_S + MARGIN * predicted_prefill_s(est_prompt_tokens))

Prefill is super-linear in prompt length on this machine: ~31 s at 30k tokens and ~93 s at 60k (n=1 each;
RESEARCH-SUMMARY-TIME-20261110), a ratio of 3.0 for a 2x size, so ``t = a * tokens ** 1.585`` (log2 3).
A flat tokens/rate model fitted at 60k would put 30k at 46 s, 50% too long at the small end and, fitted
at 30k, 50% too short at the large end, which is where a stuck call must not be declared early.

The rate comes from the proxy's own ledger when it has enough rows: each recent served local row with
``backend_usage.prompt_tokens`` and ``prompt_eval_s`` predicts the time for the new size through the same
exponent, and the median of the rows nearest in size is used. With fewer than ``MIN_ROWS`` usable rows the
constant ``DEFAULT_PREFILL_TOK_S`` (tokens/s at the 60k reference point) applies.

The deadline never goes below the value that is configured today (the 8 s hedge, the 30 s or 120 s step
budget): the flag can only lengthen a wait, never shorten one. Hook-path budgets are not touched here.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from pathlib import Path

ENV_FLAG = "LLM_ROUTER_DEADLINE_BY_SIZE"
ENV_CAP = "LLM_ROUTER_DEADLINE_CAP_S"
ENV_RATE = "LLM_ROUTER_PREFILL_TOK_S"

#: Cap below Claude Code's own request timeout (SDK default 600 s, API_TIMEOUT_MS), leaving ~2 min for the
#: fall-back-to-Claude call that follows a local timeout to complete before the client gives up.
DEFAULT_CAP_S = 480.0
#: Tokens per second of prefill at the 60k reference point (60000 / 93 s, measured, n=1).
DEFAULT_PREFILL_TOK_S = 645.0
REFERENCE_TOKENS = 60_000
#: log2(93 / 31): the measured time ratio for a doubling from 30k to 60k.
EXPONENT = 1.585
BASE_S = 8.0
MARGIN = 1.5
MIN_ROWS = 5
MAX_ROWS = 200
#: Rows below this are dominated by load and cache noise, not prefill.
MIN_ROW_TOKENS = 2_000
NEAREST_ROWS = 25
TAIL_BYTES = 4 * 1024 * 1024
CACHE_S = 60.0

_cache: dict = {"at": 0.0, "path": None, "rows": []}


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_DEADLINE_BY_SIZE", "").strip().lower() in ("1", "on", "true", "yes")


def _env_float(raw: str | None, default: float) -> float:
    try:
        v = float(raw or default)
    except ValueError:
        return default
    return v if v > 0 else default


def cap_s() -> float:
    return _env_float(os.environ.get("LLM_ROUTER_DEADLINE_CAP_S"), DEFAULT_CAP_S)


def default_rate() -> float:
    return _env_float(os.environ.get("LLM_ROUTER_PREFILL_TOK_S"), DEFAULT_PREFILL_TOK_S)


def _scale(seconds: float, from_tokens: float, to_tokens: float) -> float:
    return seconds * (to_tokens / from_tokens) ** EXPONENT


def recent_samples(path: Path | None = None, *, now: float | None = None) -> list[tuple[int, float]]:
    """(prompt_tokens, prompt_eval_s) of the last served local rows. Numbers only; cached for CACHE_S."""
    from llm_router.proxy import ledger

    target = path or ledger.ledger_path()
    t = time.monotonic() if now is None else now
    if _cache["path"] == str(target) and t - _cache["at"] < CACHE_S:
        return _cache["rows"]
    out: list[tuple[int, float]] = []
    try:
        with target.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("decision") != "served":
            continue
        u = row.get("backend_usage")
        if not isinstance(u, dict):
            continue
        n, s = u.get("prompt_tokens"), u.get("prompt_eval_s")
        if (isinstance(n, (int, float)) and isinstance(s, (int, float)) and not isinstance(n, bool)
                and n >= MIN_ROW_TOKENS and s > 0):
            out.append((int(n), float(s)))
            if len(out) >= MAX_ROWS:
                break
    _cache.update(at=t, path=str(target), rows=out)
    return out


def predicted_prefill_s(tokens: int, samples: list[tuple[int, float]] | None = None) -> tuple[float, str]:
    """Predicted prefill seconds for ``tokens`` and the source of the rate ("ledger" or "default")."""
    tokens = max(1, int(tokens))
    rows = recent_samples() if samples is None else samples
    if len(rows) >= MIN_ROWS:
        near = sorted(rows, key=lambda r: abs(math.log(r[0] / tokens)))[:NEAREST_ROWS]
        return statistics.median(_scale(s, n, tokens) for n, s in near), "ledger"
    ref_s = REFERENCE_TOKENS / default_rate()
    return _scale(ref_s, REFERENCE_TOKENS, tokens), "default"


def compute(tokens: int, samples: list[tuple[int, float]] | None = None) -> dict:
    """{"deadline_s", "predicted_prefill_s", "rate_source", "est_prompt_tokens"} for a prompt of ``tokens``."""
    pred, source = predicted_prefill_s(tokens, samples)
    deadline = min(cap_s(), BASE_S + MARGIN * pred)
    return {"deadline_s": round(deadline, 1), "predicted_prefill_s": round(pred, 1),
            "rate_source": source, "est_prompt_tokens": int(tokens)}
