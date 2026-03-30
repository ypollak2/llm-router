#!/usr/bin/env python3
# llm-router-hook-version: 1
"""SessionStart hook — inject routing banner + reset session tracking.

Fires once when a new Claude Code session begins. Two jobs:
  1. Inject a compact routing table at position 0 of the context window,
     so routing rules are always salient regardless of session length.
  2. Reset the session stats tracker so session-end summary is accurate.
"""

from __future__ import annotations

import json
import os
import sys
import time

STATE_DIR = os.path.expanduser("~/.llm-router")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")

# Tool → cheapest model it uses (for the banner display)
ROUTING_TABLE = [
    ("query/*", "llm_query", "Haiku", "50x cheaper"),
    ("code/*", "llm_code", "Sonnet", "10x cheaper"),
    ("generate/*", "llm_generate", "Flash", "20x cheaper"),
    ("research/*", "llm_research", "Perplexity", "web-grounded"),
    ("analyze/*", "llm_analyze", "Sonnet", "deep analysis"),
    ("image/*", "llm_image", "Imagen/DALL-E", "image models"),
]

BANNER = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm-router ACTIVE — mandatory routing in effect           ║
╠════════════════════════════════════════════════════════════════╣
║  query/*    → llm_query    (Haiku — 50x cheaper than Opus)   ║
║  code/*     → llm_code     (Sonnet — 10x cheaper)            ║
║  generate/* → llm_generate (Gemini Flash — 20x cheaper)      ║
║  research/* → llm_research (Perplexity — web-grounded)       ║
║  analyze/*  → llm_analyze  (Sonnet — deep analysis)          ║
╠════════════════════════════════════════════════════════════════╣
║  EVERY prompt gets classified. Call the tool. Don't answer   ║
║  yourself. FORBIDDEN: Agent subagents · self-answer ·        ║
║  WebSearch · WebFetch when a ROUTE hint is present.          ║
║  The cheap model's output IS your response.                  ║
╚════════════════════════════════════════════════════════════════╝
""".strip()


def _reset_session_stats() -> None:
    """Write current timestamp as session start marker."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(SESSION_START_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def main() -> None:
    try:
        json.load(sys.stdin)  # consume input (may be empty)
    except (json.JSONDecodeError, EOFError):
        pass

    _reset_session_stats()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "contextForAgent": BANNER,
        }
    }))


if __name__ == "__main__":
    main()
