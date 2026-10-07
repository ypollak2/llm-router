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
    when the body has none, which the caller should treat as nothing to classify)."""
    msgs = non_system(body.get("messages") or [])
    newest = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user" and _human(msgs[i].get("content")):
            newest = i
            break
    prompt = _human(msgs[newest].get("content"))[-PROMPT_CHARS:] if newest >= 0 else ""
    earlier: list[str] = []
    assistant = ""
    for m in msgs[:max(newest, 0)]:
        text = _human(m.get("content"))
        if not text:
            continue
        if m.get("role") == "user":
            earlier.append(text[:EARLIER_CHARS])
        elif m.get("role") == "assistant":
            assistant = text  # the last one before the newest prompt wins
    earlier = earlier[-EARLIER_PROMPTS:]
    parts = []
    cwd = _cwd(body)
    if cwd:
        parts.append(f"Working directory: {cwd}")
    if not earlier and not assistant:
        parts.append(_FIRST)
    parts += [f"Earlier user prompt {j + 1}/{len(earlier)}:\n{u}" for j, u in enumerate(earlier)]
    if assistant:
        tail = assistant if len(assistant) <= ASSISTANT_CHARS else "[...] " + assistant[-ASSISTANT_CHARS:]
        parts.append(f"Assistant's last message before this prompt (tail):\n{tail}")
    return Assembled("\n\n".join(parts), prompt)
