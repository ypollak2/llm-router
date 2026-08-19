"""PII / secret signal — detect API keys, tokens, .env-style secrets.

When this signal fires, the decision engine forces local-only routing
(Ollama) — the prompt is never sent to an external API. This is the single
most important safety signal in LLM Router.

Patterns cover the most common production-leak shapes; presidio-analyzer
can be plugged in for broader coverage in v0.0.3+.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from llm_router.signals.base import SignalScore


# Patterns ordered by specificity — more specific first wins.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("gemini_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret", re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("env_assignment", re.compile(r"\b[A-Z][A-Z0-9_]{4,}=[\"']?(?:sk-|gh[pousr]_|AIza|AKIA|xox)")),
)


@dataclass(frozen=True)
class PiiSignal:
    """Fires when a secret-looking pattern is found in the prompt.

    Score is 1.0 on any match (binary by design — leaking one secret is
    enough to force local routing). Evidence carries the pattern name only,
    NEVER the matched value, so logs don't leak the secret.
    """

    name: str = "pii_secret"
    threshold: float = 0.5

    def evaluate(self, prompt: str, context: dict | None = None) -> SignalScore:
        for pattern_name, regex in _SECRET_PATTERNS:
            if regex.search(prompt):
                return SignalScore(
                    name=self.name,
                    score=1.0,
                    threshold=self.threshold,
                    evidence=f"matched pattern: {pattern_name}",
                )
        return SignalScore(
            name=self.name,
            score=0.0,
            threshold=self.threshold,
            evidence="no secret patterns matched",
        )
