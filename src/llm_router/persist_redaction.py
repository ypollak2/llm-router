"""Prompt PII redaction — scrub sensitive patterns BEFORE the lineage write.

Default policy catches:
    - OpenAI / Anthropic / Gemini / GitHub / AWS / Slack API keys
    - JWTs + private-key blocks
    - Email addresses
    - US phone numbers (E.164 + common US formats)
    - US Social Security numbers
    - Credit card numbers (Luhn-checked)
    - IPv4 + IPv6 addresses (optional — off by default; many code prompts
      legitimately reference IPs)

Each detected pattern is replaced with `[REDACTED:type]` so the redacted
prompt remains human-readable for audit but the sensitive value is gone.

Policies are pluggable: organizations can register custom patterns
(e.g. employee IDs, internal hostnames, proprietary product codenames)
via RedactionPolicy.with_patterns().
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from llm_router.plugins.redaction import RedactionResult

log = logging.getLogger("llm_router.persist_redaction")

_LUHN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _luhn_valid(card: str) -> bool:
    digits = [int(c) for c in card if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# ────────────────────────────────────────────────────────────────────────
# Pattern definitions
# ────────────────────────────────────────────────────────────────────────

_DEFAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # API keys (specific patterns first to avoid being eaten by generic ones)
    # anthropic_key MUST come before openai_key; openai_key uses a
    # negative lookahead to avoid swallowing sk-ant- prefixes.
    ("anthropic_key",  re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key",     re.compile(r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("gemini_key",     re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("github_token",   re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token",    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt",            re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private_key",    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL)),
    # PII
    ("email",          re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("us_ssn",         re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("us_phone",       re.compile(r"(?:\+1[-\s]?)?\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}\b")),
)


@dataclass(frozen=True)
class RedactionPolicy:
    """Configurable redaction. Tuple of (name, compiled_regex) pairs."""

    patterns: tuple[tuple[str, re.Pattern[str]], ...] = _DEFAULT_PATTERNS
    enabled: bool = True
    luhn_check_credit_cards: bool = True

    @classmethod
    def default(cls) -> "RedactionPolicy":
        return cls()

    @classmethod
    def disabled(cls) -> "RedactionPolicy":
        """Skip redaction entirely. Use for ad-hoc dev shells only."""
        return cls(enabled=False)

    def with_patterns(
        self, extra: list[tuple[str, str | re.Pattern[str]]]
    ) -> "RedactionPolicy":
        """Return a new policy with additional org-specific patterns appended.

        `extra` is a list of (name, regex_str_or_compiled) tuples. Names
        appear in the placeholder text — pick something the audit reader
        will recognize."""
        compiled = [
            (n, p if isinstance(p, re.Pattern) else re.compile(p))
            for n, p in extra
        ]
        return RedactionPolicy(
            patterns=self.patterns + tuple(compiled),
            enabled=self.enabled,
            luhn_check_credit_cards=self.luhn_check_credit_cards,
        )


def redact_prompt(
    prompt: str, policy: RedactionPolicy | None = None
) -> RedactionResult:
    """Replace sensitive patterns in `prompt` with `[REDACTED:type]` markers.

    Returns a RedactionResult with the scrubbed text + per-pattern hit
    counts. When policy is disabled, returns the original prompt with
    no counts.
    """
    policy = policy or RedactionPolicy.default()
    if not policy.enabled or not prompt:
        return RedactionResult(text=prompt, counts={}, any_redactions=False)

    counts: dict[str, int] = {}
    out = prompt
    for name, pattern in policy.patterns:
        def _sub(match, _name=name, _counts=counts):
            _counts[_name] = _counts.get(_name, 0) + 1
            return f"[REDACTED:{_name}]"
        out = pattern.sub(_sub, out)

    # Credit-card pass — separate because we need Luhn validation
    if policy.luhn_check_credit_cards:
        def _maybe_card(match):
            raw = match.group(0)
            if _luhn_valid(raw):
                counts["credit_card"] = counts.get("credit_card", 0) + 1
                return "[REDACTED:credit_card]"
            return raw
        out = _LUHN_RE.sub(_maybe_card, out)

    return RedactionResult(
        text=out, counts=counts,
        any_redactions=bool(counts),
    )


# ────────────────────────────────────────────────────────────────────────
# Persist-on-write redaction — used by result_cache / semantic_cache /
# session_store before ANY content touches a DB row or JSONL line.
# ────────────────────────────────────────────────────────────────────────

# Broadened "prose" pass: catches unanchored keyword-then-value phrasing
# that anchored `key[:=]value` patterns in secret_scrubber miss entirely,
# e.g. "the launch code is ORANGE-742" or "the password was hunter2play".
_PROSE_SECRET_RE = re.compile(
    r"\b((?:launch\s+code|access\s+code|passcode|password|pass\s*phrase|"
    r"secret\s*(?:key)?|api\s*key|auth\s*token|credential|pin\s*code|"
    r"security\s*code)\s*(?:is|was|[:=])\s*)"
    r"[\"']?([A-Za-z0-9][A-Za-z0-9_-]{2,})[\"']?",
    re.IGNORECASE,
)

_REDACTION_FAILURE_PLACEHOLDER = "[REDACTION-FAILED: content withheld]"


def _redact_prose_secrets(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        return f"{match.group(1)}[REDACTED:prose_secret]"
    return _PROSE_SECRET_RE.sub(_sub, text)


def persist_redact(text: str) -> str:
    """Scrub *text* before it is written to any on-disk store.

    Every persistence path (result_cache rows, semantic_cache rows,
    session_store JSONL lines, and their FTS shadow tables) must run
    content through this function before the write — never after.

    Layered defense:
      1. ``redact_prompt`` — structured PII/credential patterns (this module).
      2. ``secret_scrubber.scrub_text`` — the canonical anchored-substring
         scrubber (API keys, tokens, ``key: value`` / ``key=value`` forms).
      3. A broadened "prose" pass for unanchored phrasing like "the launch
         code is ORANGE-742" that (1) and (2) don't catch.

    Config gates (``llm_router.config``):
      - ``LLM_ROUTER_PERSIST_RAW=1`` — skip redaction entirely (opt-in escape
        hatch for trusted local debugging only). Default off.
      - ``LLM_ROUTER_PERSIST_REDACTION=off`` — same effect, independent flag.
        Default on.

    Safe-failure: ANY internal error returns a fixed placeholder, never the
    raw input. Persistence must never fall through to writing the secret.
    """
    if not text or not isinstance(text, str):
        return text

    try:
        from llm_router.config import get_config
        config = get_config()
        if getattr(config, "llm_router_persist_raw", False):
            return text
        if not getattr(config, "llm_router_persist_redaction", True):
            return text
    except Exception as exc:  # noqa: BLE001 — config unavailable, fail safe below
        log.debug("persist_redact: config lookup failed, redacting anyway: %s", exc)

    try:
        out = redact_prompt(text, RedactionPolicy.default()).text
        from llm_router.secret_scrubber import scrub_text
        out = scrub_text(out)
        out = _redact_prose_secrets(out)
        return out
    except Exception as exc:  # noqa: BLE001 — never persist raw content on failure
        log.debug("persist_redact: redaction failed, withholding content: %s", exc)
        return _REDACTION_FAILURE_PLACEHOLDER
