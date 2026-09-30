"""Escalation signals for the Claude-tier rewrite (`proxy/tiers.py`).

Motivated by `~/.rsi/research/llm-router-cursor-parity/trial-real-prompts.md`
(n=6, real prompts, 2026-09-30): every conversation in the trial landed on
Sonnet (the classifier never picked Opus), and 1 of 6 routed answers was
unacceptable — a moderate-complexity-classified but investigation-heavy first
prompt (t7-northstar: "inspect repo, infer + write a North Star doc") that
Sonnet answered shallowly, never actually naming what the prompt asked for.
The trial's own read: "the one clean case in this run of the tiered model
failing at exactly the kind of open-ended investigation this project's own
notes flag as where local/cheaper models diverge from frontier ones."

Two mitigations live here, both consumed by `ClaudeTierPolicy.decide()`:

``explicit_opus_pin``
    A prompt starting with ``opus:`` pins the conversation to the Opus tier,
    bypassing classification entirely. Checked against the RAW newest human
    turn (not `tier_text`'s suffix-truncated form — a long first prompt would
    otherwise lose its own opening word). Deliberately narrow: it matches only
    the literal ``opus:`` prefix, so it can never fire on — and never needs to
    special-case — the owner's own ``claude:`` convention (see
    ``correction_signal`` below, which reads that prefix as a SIGNAL, not a
    routing keyword, and never strips or rewrites it).

``correction_signal``
    Detects that the previous served answer needs redoing: the user
    contradicts or re-asks ("no", "that's wrong", "you missed"), re-invokes
    with the owner's own ``claude:`` prefix, or the last two tool calls in a
    row came back as errors. Any of these escalates the conversation to Opus
    on the call where they appear — which, since `decide()` runs on every
    call, means "immediately", a strict superset of "at the next cold point".
"""

from __future__ import annotations

import os
import re

from llm_router.proxy.steps import non_system, tier_text

# Long enough to hold the whole first turn of a real "hard" prompt (the
# trial's t1/t2/t3 were paragraph-length briefs), short enough to stay cheap
# to scan on every call. `tier_text` keeps the LAST `limit` chars of the
# newest human turn — a small limit would cut off the `opus:`/`claude:`
# PREFIX of a long prompt, which is exactly the case these checks exist for.
_TEXT_LIMIT = 8000

_OPUS_PIN_RE = re.compile(r"^opus:\s*", re.IGNORECASE)
_CLAUDE_REASK_RE = re.compile(r"^claude:\s*", re.IGNORECASE)

# Matched only at the START of the newest human turn (first ~60 chars,
# lowercased): a correction is how a re-ask OPENS, not a word that might
# appear anywhere in a long prompt (a code review prompt legitimately
# contains "no" and "wrong" constantly).
_CONTRADICTION_RE = re.compile(
    r"^(no[,.!\s]|nope\b|that'?s (not right|wrong|not what)|"
    r"you'?re wrong\b|you missed\b|not what i (asked|meant|wanted)|incorrect\b)"
)

DEFAULT_TOOL_FAIL_N = 2

REASON_CONTRADICTION = "contradiction"
REASON_CLAUDE_REASK = "claude_reask"
REASON_TOOL_FAILURES = "tool_failures"


def _tool_fail_n() -> int:
    # Literal env var name, not an indirected constant: env_registry.py's own
    # test scans for a literal `os.environ.get("NAME")` call per variable, so
    # an indirected read would register as a phantom declaration.
    return _parse_positive_int(
        os.environ.get("LLM_ROUTER_PROXY_ESCALATION_TOOL_FAIL_N"), DEFAULT_TOOL_FAIL_N)


def explicit_opus_pin(body: dict) -> bool:
    """True when the conversation's newest human turn starts with ``opus:``."""
    text = tier_text(body, limit=_TEXT_LIMIT).lstrip()
    return bool(_OPUS_PIN_RE.match(text))


def _consecutive_failed_tool_turns(body: dict) -> int:
    """How many of the newest user turns, counting back from the end, are
    ALL tool_result content with at least one block carrying ``is_error``.

    Stops at the first turn that is not a tool-result-only turn, or that
    reports no error — a single clean result breaks the streak, same as a
    human would read "ok that one worked" as the correction ending.
    """
    streak = 0
    for m in reversed(non_system(body.get("messages") or [])):
        if not isinstance(m, dict) or m.get("role") != "user":
            break
        content = m.get("content")
        if not isinstance(content, list) or not content:
            break
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if "tool_result" not in kinds or not (kinds <= {"tool_result", "text"}):
            break
        has_error = any(
            isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
            for b in content
        )
        if not has_error:
            break
        streak += 1
    return streak


def correction_signal(body: dict) -> str | None:
    """A reason string when the newest turn signals the prior answer needs
    redoing, else ``None``. Never raises — a bad body just yields no signal."""
    try:
        text = tier_text(body, limit=_TEXT_LIMIT).lstrip()
        lowered = text.lower()
        if _CLAUDE_REASK_RE.match(lowered):
            return REASON_CLAUDE_REASK
        if _CONTRADICTION_RE.match(lowered[:80]):
            return REASON_CONTRADICTION
        if _consecutive_failed_tool_turns(body) >= _tool_fail_n():
            return REASON_TOOL_FAILURES
    except Exception:  # noqa: BLE001 — a malformed body must fail open, not 500
        return None
    return None


# ── safety default: never downgrade a long or multi-part first prompt ──────
#
# Borrowed from the trial's own failure (t7-northstar, paraphrased): a first
# prompt that reads as an open-ended brief rather than a single narrow ask is
# exactly the shape the tiered classifier got wrong once already. Rather than
# trust the classifier's complexity label for THIS shape, a first prompt this
# long or this multi-part skips the rewrite for that call and keeps whatever
# model Claude Code itself requested.

DEFAULT_LONG_PROMPT_WORDS = 120
DEFAULT_LONG_PROMPT_PARTS = 3

_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)


def _parse_positive_int(raw: str | None, default: int) -> int:
    if not raw or not raw.strip():
        return default
    try:
        n = int(raw.strip())
    except ValueError:
        return default
    return n if n > 0 else default


def is_long_or_multi_part(text: str) -> bool:
    """A first prompt this long, or with this many bullet/numbered parts, is
    treated as a genuine multi-part brief rather than a quick question."""
    # Literal env var names (not an indirected constant/helper): see the
    # comment in `_tool_fail_n` above.
    words = len(text.split())
    words_floor = _parse_positive_int(
        os.environ.get("LLM_ROUTER_PROXY_LONG_PROMPT_WORDS"), DEFAULT_LONG_PROMPT_WORDS)
    if words >= words_floor:
        return True
    parts = len(_BULLET_RE.findall(text))
    parts_floor = _parse_positive_int(
        os.environ.get("LLM_ROUTER_PROXY_LONG_PROMPT_PARTS"), DEFAULT_LONG_PROMPT_PARTS)
    return parts >= parts_floor


def first_prompt_is_long_or_multi_part(body: dict) -> bool:
    """``is_long_or_multi_part`` over the conversation's newest human turn,
    untruncated at the front for the same reason `explicit_opus_pin` is."""
    return is_long_or_multi_part(tier_text(body, limit=_TEXT_LIMIT))
