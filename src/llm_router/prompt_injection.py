"""Prompt injection detection and mitigation.

Detects and logs suspected prompt injection attempts to prevent:
- System prompt extraction
- Instruction bypass attacks
- Unauthorized data exfiltration
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Patterns that suggest prompt injection attempts
_INJECTION_PATTERNS = {
    # System prompt extraction (various phrasings)
    r"system.*(prompt|message|instruction)",
    r"(show|reveal|display|print).*instructions?",
    r"what.*your.*instructions?",
    r"internal.*instructions?",
    # Instruction bypass (various phrasings)
    r"(ignore|forget|disregard|override|cancel).*instructions?",
    r"previous.*(instruction|request|order)",
    r"(forget|disregard|ignore).*(told|said|instructed|ordered)",
    # Data extraction (API keys, secrets, env vars)
    r"api.*keys?",
    r"environment.*variables?",
    r"(show|dump|leak).*(secret|key|token)",
    r"secrets?",
    r"tokens?",
    # Jailbreak attempts
    r"(pretend|act|imagine).*restrictions?",
    r"(no|without).*restrictions?",
    r"different.*system",
    r"not.*an?.*ai",
    # DAN-style attacks
    r"do.*anything",
    r"(developer|debug|dev).*mode",
}


def _is_injection_attempt(text: str) -> bool:
    """Check if text contains potential injection patterns.
    
    Normalizes encoding to detect bypass attempts using URL encoding, 
    unicode obfuscation, or zero-width characters before pattern matching.

    Args:
        text: User input to analyze

    Returns:
        True if suspicious patterns detected, False otherwise
    """
    import unicodedata
    import urllib.parse
    
    # Normalize unicode (NFKC) to decompose lookalike characters
    text = unicodedata.normalize('NFKC', text)
    
    # Decode URL-encoded sequences (e.g., "%2F" → "/")
    try:
        text = urllib.parse.unquote(text)
    except Exception:
        pass  # If decoding fails, continue with original text
    
    # Remove zero-width characters that attackers use to bypass filters
    # Includes zero-width space, zero-width joiner, zero-width non-joiner, etc.
    zero_width_chars = ['\u200b', '\u200c', '\u200d', '\ufeff']
    for zwc in zero_width_chars:
        text = text.replace(zwc, '')
    
    # Convert to lowercase for case-insensitive pattern matching
    text_lower = text.lower()
    
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def sanitize_prompt(user_prompt: str, log_suspected: bool = True) -> str:
    """Sanitize user prompt to prevent injection attacks.

    Wraps the user prompt with clear delimiters to separate it from
    system instructions, making prompt injection harder.

    Args:
        user_prompt: Raw user input
        log_suspected: Whether to log suspected injection attempts

    Returns:
        Sanitized prompt with clear user/system boundaries
    """
    if _is_injection_attempt(user_prompt):
        if log_suspected:
            logger.warning(
                "Suspected prompt injection detected in user input. "
                "Wrapping with safety markers.",
                extra={"user_input_length": len(user_prompt)},
            )

    # Wrap user prompt with clear boundaries to prevent instruction mixing
    return (
        "═══════════════════════════════════════════════════════════\n"
        "USER REQUEST (start):\n"
        "═══════════════════════════════════════════════════════════\n"
        f"{user_prompt}\n"
        "═══════════════════════════════════════════════════════════\n"
        "USER REQUEST (end): You MUST only respond to the user request above.\n"
        "═══════════════════════════════════════════════════════════\n"
    )


def detect_injections_in_batch(prompts: list[str]) -> dict[str, Any]:
    """Analyze multiple prompts for injection attempts.

    Useful for batch operations or monitoring.

    Args:
        prompts: List of prompts to analyze

    Returns:
        Dictionary with analysis results:
        - total: Total prompts analyzed
        - suspected: Count of suspected injection attempts
        - indices: List of indices where injections were detected
        - patterns: List of patterns that matched (for logging)
    """
    suspected_indices = []
    matched_patterns = []

    for idx, prompt in enumerate(prompts):
        for pattern in _INJECTION_PATTERNS:
            if re.search(pattern, prompt.lower()):
                if idx not in suspected_indices:
                    suspected_indices.append(idx)
                matched_patterns.append((idx, pattern))

    return {
        "total": len(prompts),
        "suspected": len(suspected_indices),
        "indices": suspected_indices,
        "matched_patterns": matched_patterns,
    }
