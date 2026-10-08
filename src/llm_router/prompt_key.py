"""A stable, text-free key for one human prompt.

The proxy ledger stores ``key(prompt)`` as ``text_sha`` and, in M4, the hook
computes the same key on the raw typed prompt, so the two sides can be joined
without either of them storing the prompt itself (the ledger holds hashes only).

Claude Code wraps hook ``additionalContext`` in ``<system-reminder>`` blocks and
puts them in the same API user message as the typed prompt, so the key ignores
those blocks. It does not truncate: the hook hashes the whole prompt, so the
proxy must hash the whole newest human text too (``steps.newest_human_text``).
A key over a truncated tail would differ between the two sides for any prompt
longer than the truncation limit.
"""

from __future__ import annotations

import hashlib
import re

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_WS_RE = re.compile(r"\s+")

KEY_HEX_CHARS = 16


def normalize(text: str | None) -> str:
    """Strip every ``<system-reminder>...</system-reminder>`` block and collapse
    whitespace. No truncation."""
    return _WS_RE.sub(" ", _REMINDER_RE.sub("", text or "")).strip()


def key(text: str | None) -> str:
    """First 16 hex chars of sha256 over the normalized full text."""
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:KEY_HEX_CHARS]
