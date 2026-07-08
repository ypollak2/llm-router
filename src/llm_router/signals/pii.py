"""PII / secret signal — detect API keys, tokens, .env-style secrets.

When this signal fires, the router can force local-only routing so the prompt
is never sent to an external API (see llm_router.signals.force_local_for_pii).
Evidence carries the pattern NAME only, never the matched value, so logs and
lineage never leak the secret.

Ported from Chuzom's signals/pii.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from llm_router.signals.base import SignalScore

# Patterns ordered by specificity — more specific first wins.
_SECRET_PATTERNS = (
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("gemini_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("env_assignment", re.compile(r"\b[A-Z][A-Z0-9_]{4,}=[\"']?(?:sk-|gh[pousr]_|AIza|AKIA|xox)")),
)


@dataclass(frozen=True)
class PiiSignal:
    """Fires (score 1.0) when a secret-looking pattern is found. Binary by
    design — one leaked secret is enough to force local routing."""

    name: str = "pii_secret"
    threshold: float = 0.5

    def evaluate(self, prompt: str, context: Optional[dict] = None) -> SignalScore:
        for pattern_name, regex in _SECRET_PATTERNS:
            if regex.search(prompt):
                return SignalScore(self.name, 1.0, self.threshold,
                                   evidence=f"matched pattern: {pattern_name}")
        return SignalScore(self.name, 0.0, self.threshold,
                           evidence="no secret patterns matched")
