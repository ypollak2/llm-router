"""Batched observation masking for Anthropic Messages bodies (plan P4; unwired, P5 wires it).

Pure function, no model call, no I/O. Tool_result bodies older than a verbatim window are
replaced by a short deterministic placeholder that keeps the tool name, file paths, exit code
and error lines. The mask boundary moves only in steps of ``batch`` messages, so on a growing
conversation the masked prefix is byte-identical between batch boundaries (a sliding mask
changes the prefix every turn and defeats prefix reuse; JetBrains "Complexity Trap" M=10,
SWE-agent polling). Never masked: the first message, the last ``keep_last`` messages, and any
tool_use block (only results), so tool_use / tool_result ids stay paired.

Flag: LLM_ROUTER_PROXY_MASK (default off). Nothing imports this module while it is off.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from llm_router.proxy.local_mode import estimate_prompt_tokens

DEFAULT_KEEP_LAST = 10
DEFAULT_BATCH = 10
PLACEHOLDER_PREFIX = "[tool result masked:"
MAX_PATHS = 8
MAX_ERRORS = 5
MAX_ERROR_CHARS = 200

_PATH_RE = re.compile(r"(?<![\w/.:@+~-])(?:~|\.{1,2})?/?(?:[\w.@+-]+/)+[\w.@+-]+(?::\d+)?")
_EXIT_RE = re.compile(r"(?i)\b(?:exit(?:ed)?(?:\s+with)?(?:\s+(?:code|status))?|return\s?code|status\s+code)[:=\s]+(-?\d+)")
_ERROR_RE = re.compile(r"(?i)(?:error|exception|traceback|failed|failure|fatal|panic|assertion)")


def enabled() -> bool:
    """LLM_ROUTER_PROXY_MASK, default off."""
    return os.environ.get("LLM_ROUTER_PROXY_MASK", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class MaskStats:
    masked: int
    chars_saved: int
    cut: int  # messages [1, cut) were eligible
    est_tokens_before: int
    est_tokens_after: int


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) and b.get("type") == "text"
                         else (f"[{b.get('type')}]" if isinstance(b, dict) else str(b)) for b in content)
    return "" if content is None else str(content)


def placeholder(name: str, text: str, is_error: bool = False) -> str:
    """Deterministic summary of one tool result: same input, same bytes."""
    parts = [f"{name or '?'}, {len(text)} chars"]
    if is_error:
        parts.append("is_error")
    m = _EXIT_RE.search(text)
    if m:
        parts.append(f"exit code {m.group(1)}")
    paths: list[str] = []
    for p in _PATH_RE.findall(text):
        if p not in paths:
            paths.append(p)
        if len(paths) >= MAX_PATHS:
            break
    if paths:
        parts.append("paths: " + ", ".join(paths))
    errors: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and _ERROR_RE.search(line):
            line = line[:MAX_ERROR_CHARS]
            if line not in errors:
                errors.append(line)
        if len(errors) >= MAX_ERRORS:
            break
    if errors:
        parts.append("errors: " + " | ".join(errors))
    return f"{PLACEHOLDER_PREFIX} {'; '.join(parts)}]"


def boundary(n_messages: int, keep_last: int = DEFAULT_KEEP_LAST, batch: int = DEFAULT_BATCH) -> int:
    """Index below which results are masked: the largest multiple of ``batch`` that leaves
    at least ``keep_last`` messages verbatim. Depends only on the count, so it is constant
    between batch boundaries."""
    batch = max(1, batch)
    return max(0, (n_messages - max(0, keep_last)) // batch * batch)


def mask_body(body: dict, *, keep_last: int = DEFAULT_KEEP_LAST,
              batch: int = DEFAULT_BATCH) -> tuple[dict, MaskStats]:
    """Return (new body, stats). The input is never mutated; untouched messages are shared."""
    messages = body.get("messages") or []
    cut = boundary(len(messages), keep_last, batch)
    names: dict[str, str] = {}
    out: list = []
    masked = saved = 0
    for i, m in enumerate(messages):
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            out.append(m)
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                names[b["id"]] = str(b.get("name", "?"))
        if i == 0 or i >= cut:
            out.append(m)
            continue
        new_blocks, changed = [], False
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text = _result_text(b.get("content"))
                if not text.startswith(PLACEHOLDER_PREFIX):
                    ph = placeholder(names.get(b.get("tool_use_id"), "?"), text, bool(b.get("is_error")))
                    if len(ph) < len(text):
                        b = {**b, "content": ph}
                        changed = True
                        masked += 1
                        saved += len(text) - len(ph)
            new_blocks.append(b)
        out.append({**m, "content": new_blocks} if changed else m)
    new_body = {**body, "messages": out} if masked else body
    return new_body, MaskStats(masked, saved, cut, estimate_prompt_tokens(body),
                               estimate_prompt_tokens(new_body) if masked else estimate_prompt_tokens(body))
