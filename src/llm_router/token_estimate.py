"""One calibrated token estimator for the local path (plan P2, R4 gap 6).

Seven estimators with different units sat on the local path (chars/4, chars/3,
chars/3.5 x 1.15, ...). This is the one fitted to REAL Ollama prompt counts
(qwen3.6:35b-a3b-coding, Ollama 0.32.13, 2026-10-05, n=7 payloads; estimate/real
between 1.02x and 1.21x, never under; ``tests/test_proxy_local_mode.py::CALIBRATION``):
digits are one token each, wide characters ~1.5, everything else ~3.6 chars/token.

The local-path estimators delegate here only when ``LLM_ROUTER_EST_CALIBRATED``
is on (default off, today's formulas byte-for-byte).
"""
from __future__ import annotations

import json
import math
import os

ENV_FLAG = "LLM_ROUTER_EST_CALIBRATED"
DIGIT_TOKENS = 1.1
WIDE_CHAR_TOKENS = 1.5
OTHER_CHARS_PER_TOKEN = 3.6


def calibrated_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_EST_CALIBRATED", "").strip().lower() in ("1", "true", "yes", "on")


def est_tokens(data: object) -> int:
    """Estimated prompt tokens of ``data``: a string, or any JSON-able payload
    (serialized the way the backend receives it)."""
    text = data if isinstance(data, str) else json.dumps(data, default=str, ensure_ascii=False)
    digits = sum(c.isdigit() for c in text)
    wide = sum(ord(c) > 127 for c in text)
    return math.ceil(DIGIT_TOKENS * digits + WIDE_CHAR_TOKENS * wide
                     + (len(text) - digits - wide) / OTHER_CHARS_PER_TOKEN)


def chars_for_tokens(tokens: int) -> int:
    """Inverse for budgets given in tokens: characters of ordinary text."""
    return max(0, int(tokens * OTHER_CHARS_PER_TOKEN))
