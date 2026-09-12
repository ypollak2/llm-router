"""Keep the task in the window.

The measured window on this machine is ~16,400 tokens, and on overflow llama.cpp
(`--context-shift --keep 4`) discards the OLDEST tokens — the system prompt and
the question — then answers about whatever survived. Silently.

`read_file` returned up to 50,000 characters, about 12,500 tokens: one read of a
large file consumed 76% of the window on its own. The file the failing harness
task reads is 217,729 characters (~54k tokens). So the loop was reliably
destroying its own instructions on the first read and then wandering, which is
exactly the "repeats a read, then refuses to finish" trace.

Three rules, in the order they matter:

  1. Do not create the big context. A slice plus "search for the rest" beats a
     truncated dump that no longer fits.
  2. Pin the task. Re-state the question every turn rather than trusting it to
     survive at the front of a window that evicts from the front.
  3. Evict deliberately. When the history must shrink, drop the OLDEST TOOL
     RESULTS — never the system prompt, never the question.
"""
from __future__ import annotations

import os

# ~4 chars per token is the standard rough conversion and is what the rest of
# this tree uses; precision is not what makes this safe, headroom is.
CHARS_PER_TOKEN = 4


def window_tokens() -> int:
    """Token budget the loop plans against.

    Deliberately BELOW the measured ceiling. Planning against the exact limit
    means the first estimate that runs slightly long evicts the task, and the
    failure is silent — so the margin is the feature.
    """
    # NOT "..._WINDOW_TOKENS": `hosts/base.routing_env` strips any name
    # containing TOKEN as credential-shaped, so that spelling would be silently
    # dropped from the environment propagated to a subprocess host — the setting
    # would appear to work here and vanish there.
    raw = os.environ.get("LLM_ROUTER_AGENT_WINDOW", "").strip()
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return 12000


def max_tool_result_chars() -> int:
    """Ceiling on ONE tool result.

    A quarter of the window: enough for a real slice of a file, small enough
    that four of them still leave room for the conversation that interprets
    them. The old value was 50,000 — three times the entire window.
    """
    raw = os.environ.get("LLM_ROUTER_MAX_TOOL_RESULT_CHARS", "").strip()
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return (window_tokens() * CHARS_PER_TOKEN) // 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // CHARS_PER_TOKEN)


def messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(str(m.get("content") or "")) for m in messages)


def truncate_tool_result(text: str, *, hint: str = "") -> str:
    """Cap one tool result, and say what to do instead of silently cutting.

    A bare "(truncated)" tells the model its read failed but not how to succeed,
    so it reads again — the loop we are trying to break. The hint names the
    cheaper tool.
    """
    cap = max_tool_result_chars()
    if len(text) <= cap:
        return text
    tail = (
        f"\n\n... TRUNCATED at {cap:,} of {len(text):,} characters. "
        f"Do NOT read this file again — it will be truncated identically. "
    )
    return text[:cap] + tail + (hint or
        "Use search_files to find the specific lines you need, or read_file with "
        "`offset` and `limit` to read a specific range.")


def prune(messages: list[dict], *, keep_recent_tools: int = 2) -> list[dict]:
    """Shrink the history to fit, dropping the oldest TOOL RESULTS first.

    Never touches the system prompt (index 0) or the original question
    (index 1). Those are the two things llama.cpp's own eviction takes first,
    and losing them is the failure this module exists to prevent — so when the
    harness evicts, it evicts the opposite end.
    """
    if messages_tokens(messages) <= window_tokens():
        return messages

    head, body = messages[:2], messages[2:]
    tool_positions = [i for i, m in enumerate(body) if m.get("role") == "tool"]
    droppable = tool_positions[:-keep_recent_tools] if keep_recent_tools else tool_positions

    for i in droppable:
        if messages_tokens(head + body) <= window_tokens():
            break
        original = str(body[i].get("content") or "")
        body[i] = {
            "role": "tool",
            # Say that it happened. A silently vanished result invites the model
            # to re-run the call that produced it.
            "content": (f"[earlier tool result dropped to stay within the context "
                        f"window — {len(original):,} characters. Do not re-run it; "
                        f"ask for something narrower if you still need it.]"),
        }
    return head + body


def restate(prompt: str) -> str:
    """The reminder appended each turn, so the task cannot be forgotten."""
    return (f"\n\n[Reminder — the task you are working on: {prompt.strip()}]\n"
            f"If you can answer it now, call finish.")
