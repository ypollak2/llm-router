"""Prompt-cache layout (PLAN v16 P1.4, R-CTX-8): stable part first, marked for caching.

Order of a request built here, most stable first:

  system[0]  instructions digest        (stable across a project's sessions)
  system[1]  knowledge block (OKF/semantic)  <- breakpoint 1 (end of stable system)
  messages   ``recent`` turns, verbatim      <- breakpoint 2 (last recent message)
  messages   the request                     never marked, never reordered

Anthropic caches by exact prefix, so anything volatile before a breakpoint
voids it. The request is the most volatile part and goes last, unmarked.

OPT-IN, DEFAULT OFF: nothing on the proxy, hook, session-start or statusline
path imports this module. `enabled()` reads ``LLM_ROUTER_CACHE_LAYOUT``; a
caller must check it and import this lazily. The pure builders below do not
read the flag so they stay unit-testable.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

_ON = ("1", "on", "true", "yes")
_EPHEMERAL = {"type": "ephemeral"}


def enabled() -> bool:
    """``LLM_ROUTER_CACHE_LAYOUT=on`` opts in. Default off."""
    return os.environ.get("LLM_ROUTER_CACHE_LAYOUT", "").strip().lower() in _ON


def _text_block(text: str, *, cached: bool = False) -> dict:
    block = {"type": "text", "text": text}
    if cached:
        block["cache_control"] = dict(_EPHEMERAL)
    return block


def _as_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in (content or [])]


@dataclass
class CacheLayout:
    """Anthropic-native request pieces: ``system`` blocks plus ``messages``."""

    system: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)

    @property
    def stable_prefix(self) -> str:
        """Text everything up to the last breakpoint is made of (hash input)."""
        parts: list[str] = []
        for b in self.system:
            parts.append(b.get("text", ""))
        for m in self.messages[:-1]:
            parts.append(m["role"] + ":" + "".join(b.get("text", "") for b in m["content"]))
        return "\n".join(parts)

    @property
    def prefix_hash(self) -> str:
        return hashlib.sha256(self.stable_prefix.encode()).hexdigest()[:16]

    def breakpoints(self) -> list[tuple[str, int]]:
        """Every cache_control position as ("system"|"messages", index)."""
        out = [("system", i) for i, b in enumerate(self.system) if "cache_control" in b]
        for i, m in enumerate(self.messages):
            if any("cache_control" in b for b in m["content"]):
                out.append(("messages", i))
        return out

    def as_openai_messages(self) -> list[dict]:
        """The same layout as an OpenAI-format list (system message with blocks),
        the shape `providers.py` hands to litellm."""
        return [{"role": "system", "content": [dict(b) for b in self.system]}, *self.messages]


def build_layout(request: str, *, instructions: str = "", knowledge: str = "",
                 recent: list[dict] | None = None, mark: bool = True) -> CacheLayout:
    """Assemble the layout. ``recent`` is ``[{"role", "content"}]`` verbatim.

    Breakpoint 1 sits on the last stable system block (knowledge, else
    instructions); breakpoint 2 on the last ``recent`` message. A breakpoint is
    skipped when its section is empty. The request is the final user message
    and is never given ``cache_control``. ``mark=False`` builds the same order
    with no breakpoints (the control arm of the measurement).
    """
    system: list[dict] = []
    if instructions.strip():
        system.append(_text_block(instructions))
    if knowledge.strip():
        system.append(_text_block(knowledge))
    if system and mark:
        system[-1]["cache_control"] = dict(_EPHEMERAL)

    msgs = [{"role": m["role"], "content": _as_blocks(m.get("content"))} for m in (recent or [])]
    if msgs and mark:
        msgs[-1]["content"][-1]["cache_control"] = dict(_EPHEMERAL)
    msgs.append({"role": "user", "content": [_text_block(request)]})
    return CacheLayout(system=system, messages=msgs)
