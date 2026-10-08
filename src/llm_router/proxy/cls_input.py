"""The classifier's input, built from the request body.

``assemble(body)`` gives the local classifier what a developer had in front of
them when they typed the prompt, and nothing the proxy re-sends on every call:

* the newest human prompt, normalized, last 2,000 chars;
* up to 3 earlier human prompts, normalized, 300 chars each;
* the tail (500 chars) of the assistant's last text before the newest prompt.

``tool_result``, ``tool_use`` and ``thinking`` blocks are skipped, and so are
Claude Code's ``<system-reminder>`` blocks (``prompt_key.normalize``). The layout
is the one the p_eval judges and the eval harness saw ("Working directory: ...",
"Earlier user prompt i/n:", "Assistant's last message before this prompt
(tail):"), so a verdict measured offline is a verdict the proxy would reproduce.

The result is an :class:`~llm_router.local_classifier.Assembled`: a ``str`` that
also carries the context and the prompt separately. It holds prompt text, so it
is passed to the local model and never logged; the shadow log keeps hashes only.
"""

from __future__ import annotations

import re

from llm_router.local_classifier import Assembled
from llm_router.prompt_key import normalize
from llm_router.proxy.steps import _text_of, non_system

PROMPT_CHARS = 2000
EARLIER_PROMPTS = 3
EARLIER_CHARS = 300
ASSISTANT_CHARS = 500
# How far back the scan may look, in messages, for the newest prompt and again for its
# context. Bounds the worst case (one prompt followed by a long tool loop); see assemble.
MAX_SCAN_MESSAGES = 400

_FIRST = "(This is the FIRST prompt of the session: no prior context.)"
_CWD_RE = re.compile(r"Primary working directory:\s*(\S[^\n]*)")


def _human(content) -> str:
    """A user turn's own text. ``_text_of`` keeps only ``text`` blocks, so tool
    results are already out; reminders and whitespace go through ``normalize``."""
    return normalize(_text_of(content))


def _system_text(body: dict) -> str:
    system = body.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _cwd(body: dict) -> str | None:
    found = _CWD_RE.search(_system_text(body))
    return found.group(1).strip() if found else None


def assemble(body: dict) -> Assembled:
    """Classifier input for the conversation's newest human prompt (``""`` prompt
    when the body has none, which the caller should treat as nothing to classify).

    The history is read backwards from the newest message and the scan stops as soon
    as it holds everything the input uses (the newest prompt, 3 earlier prompts, the
    assistant's last text), and after at most :data:`MAX_SCAN_MESSAGES` messages
    either way (P1.7-c: a full forward walk held the GIL for 0.7 s on a 1,800-message
    history and delayed concurrent continuations)."""
    msgs = non_system(body.get("messages") or [])
    stop = max(len(msgs) - MAX_SCAN_MESSAGES, 0)
    newest, prompt = -1, ""
    for i in range(len(msgs) - 1, stop - 1, -1):
        if msgs[i].get("role") == "user":
            text = _human(msgs[i].get("content"))
            if text:
                newest, prompt = i, text[-PROMPT_CHARS:]
                break
    earlier: list[str] = []  # newest first while scanning
    assistant = ""
    for i in range(newest - 1, max(newest - MAX_SCAN_MESSAGES, 0) - 1, -1):
        role = msgs[i].get("role")
        if role == "user" and len(earlier) < EARLIER_PROMPTS:
            text = _human(msgs[i].get("content"))
            if text:
                earlier.append(text[:EARLIER_CHARS])
        elif role == "assistant" and not assistant:
            assistant = _human(msgs[i].get("content"))  # the last one before the newest prompt
        if assistant and len(earlier) == EARLIER_PROMPTS:
            break
    earlier.reverse()
    # The window ended before the scan found all it looks for: the input may lack context
    # the full history holds. The shadow record keeps this flag so a report can count it.
    capped = newest > MAX_SCAN_MESSAGES and not (assistant and len(earlier) == EARLIER_PROMPTS)
    parts = []
    cwd = _cwd(body)
    if cwd:
        parts.append(f"Working directory: {cwd}")
    if not earlier and not assistant and not capped:
        parts.append(_FIRST)
    parts += [f"Earlier user prompt {j + 1}/{len(earlier)}:\n{u}" for j, u in enumerate(earlier)]
    if assistant:
        tail = assistant if len(assistant) <= ASSISTANT_CHARS else "[...] " + assistant[-ASSISTANT_CHARS:]
        parts.append(f"Assistant's last message before this prompt (tail):\n{tail}")
    out = Assembled("\n\n".join(parts), prompt)
    out.capped = capped
    return out
