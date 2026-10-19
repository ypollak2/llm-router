"""Shared, cheap user-prefix parsing and text-free override examples.

A prefix beats the classifier, tier, task heuristics, and the hook's
Q&A-local policy. It does not override hard limits: route_and_call's
BudgetExceededError and the quality breaker still apply.
A prefix never authorizes spending past a cap.
Resolve overrides before consulting the quality breaker.
Refused and host-handled overrides do not consult the breaker.
Routed overrides still consult it and use the normal budget checks.
Unknown prefixes are ignored and their prompts forwarded untouched.
Override resolution is fail-open: parse or lookup errors mean no prefix.
Only hashes of stripped prompts are recorded, never prompt text.
This module must remain cheap to import from hooks.
"""

from __future__ import annotations

import os  # noqa: F401 (tests patch prefixes.os.open)
from pathlib import Path
import re
import time
from dataclasses import dataclass

from llm_router import examples, failopen, paths, prompt_key

PREFIX_NAMES = ("local", "claude", "opus", "sonnet", "haiku", "codex")
CLAUDE_TIER_PREFIXES = ("opus", "sonnet", "haiku")
_PREFIX_RE = re.compile(
    r"^\s*(local|claude|native|opus|sonnet|haiku|codex)\s*:\s*", re.I
)
_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Override:
    name: str
    stripped: str

    @property
    def is_claude(self) -> bool:
        return self.name in ("claude", *CLAUDE_TIER_PREFIXES)

    @property
    def forces_external(self) -> bool:
        return self.name in ("local", "codex")


def parse(text: str) -> Override | None:
    """Recognize one leading prefix; invalid or empty requests are untouched."""
    try:
        if not isinstance(text, str):
            return None
        match = _PREFIX_RE.match(text)
        if match is None:
            return None
        stripped = text[match.end():].strip()
        if not stripped:
            return None
        name = match[1].lower()
        return Override("claude" if name == "native" else name, stripped)
    except Exception as exc:  # noqa: BLE001 -- hook callers must never fail
        failopen.record("LR-FO-PREFIX-PARSE", exc)
        return None


def strip_prefix(text: str) -> str:
    override = parse(text)
    return override.stripped if override else text


def _human_remainder(text: str) -> str:
    """The human text one block keeps, selected exactly as ``steps._human_parts``
    does: harness blocks and a leading ``Caveat:`` line removed, then stripped."""
    from llm_router.proxy.steps import _CAVEAT, _strip_block

    rest = _strip_block(text)[0].strip()
    if _CAVEAT.match(rest):
        rest = rest.split("\n", 1)[1].strip() if "\n" in rest else ""
    return rest


def _strip_in_block(text: str) -> str | None:
    """``text`` with the prefix removed from its human remainder, or None when that
    remainder does not start with a prefix. Harness text in the same block stays."""
    rest = _human_remainder(text)
    override = parse(rest)
    at = text.find(rest) if rest else -1
    if override is None or at < 0:
        return None
    return text[:at] + override.stripped + text[at + len(rest):]


def strip_body(body: dict) -> dict | None:
    """Copy the changed path to the newest human message, stripping one prefix.

    The block is chosen as detection chooses it (``newest_human_text``): the first
    text block that keeps human text once harness blocks, ``Caveat:`` lines and
    whitespace-only blocks are removed. Tool results and harness-only turns are
    skipped. Unchanged messages, blocks and request fields keep their values.
    """
    try:
        from llm_router.proxy.steps import newest_human_text

        messages = body.get("messages")
        if not isinstance(messages, list):
            return None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if not newest_human_text({"messages": [message]}):
                continue
            content = message.get("content")
            if isinstance(content, str):
                changed = _strip_in_block(content)
                if changed is None:
                    return None
            elif isinstance(content, list):
                changed = list(content)
                for block_index, block in enumerate(content):
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                            and _human_remainder(block["text"])):
                        text = _strip_in_block(block["text"])
                        if text is None:
                            return None
                        changed[block_index] = dict(block, text=text)
                        break
                else:
                    return None
            else:
                return None
            copied = list(messages)
            copied[index] = dict(message, content=changed)
            return dict(body, messages=copied)
    except Exception as exc:  # noqa: BLE001 -- request handling is fail-open
        failopen.record("LR-FO-PROXY-PREFIX", exc)
    return None


def record_example(stripped_text, forced, decided, door, *, now=None, path=None) -> bool:
    """Append one private, bounded example. Evidence failures never stop routing."""
    try:
        if door not in {"hook", "proxy", "mcp"}:
            raise ValueError("invalid prefix example door")
        target = Path(path) if path is not None else paths.llm_router_home() / "examples.jsonl"
        row = {
            "text_sha": prompt_key.key(stripped_text),
            "forced": forced,
            "decided": decided,
            "door": door,
            "ts": time.time() if now is None else now,
        }
        row.update(examples.embedding_fields(stripped_text))
        examples.append_row(row, path=target, max_bytes=_MAX_BYTES)
        return True
    except Exception as exc:  # noqa: BLE001 -- telemetry is fail-open
        failopen.record("LR-FO-PREFIX-EXAMPLE", exc)
        return False
