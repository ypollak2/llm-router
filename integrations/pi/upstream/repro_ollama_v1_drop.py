#!/usr/bin/env python3
"""Ollama's OpenAI-compatible endpoint silently drops leading messages when the prompt
exceeds the loaded context, and ignores `truncate: false` there.

    python3 repro_ollama_v1_drop.py MODEL [BASE_URL]

Sends (1) one ~25k-token log, (2) the same log twice after a user request, with the
request asking to quote a sentinel. Measured 2026-10-04, Ollama 0.32.13,
qwen3.6:35b-a3b-coding, context 32768: (1) prompt_tokens=25247, (2) prompt_tokens=25270,
also with "truncate": false. Two logs cannot fit in 25,270 tokens, so the first
messages (the request and the first log) were dropped, and no error was returned.
Loads MODEL if it is not loaded.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def log_text(n: int, needle: str) -> str:
    rows = [f"2026-10-04T10:{i % 60:02d}:{(i * 7) % 60:02d} INFO worker-{i % 23} processed batch {1000 + i} in {i % 97}ms status=ok"
            for i in range(n)]
    rows[n // 2] = f"CHECKSUM={needle}"
    return "\n".join(rows)


def call(base: str, model: str, messages: list, extra: dict | None = None):
    body = {"model": model, "messages": messages, "max_tokens": 64, "stream": False, **(extra or {})}
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read()).get("usage")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:200]!r}"


def main() -> int:
    model = sys.argv[1]
    base = (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:11434").rstrip("/")
    a, b = log_text(640, "AAAA1111"), log_text(640, "BBBB2222")
    print("one log:            ", call(base, model, [{"role": "user", "content": a + "\nWhat is the CHECKSUM?"}]))
    msgs = [{"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "SENTINEL-42: report the CHECKSUM of both logs."},
            {"role": "user", "content": "first log:\n" + a},
            {"role": "user", "content": "second log:\n" + b + "\nQuote the SENTINEL id of my first message."}]
    print("two logs:           ", call(base, model, msgs))
    print("two logs, truncate=false:", call(base, model, msgs, {"truncate": False}))
    print("expected: an error (or prompt_tokens about twice the single-log count), not a silent drop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
