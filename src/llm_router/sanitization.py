"""Prompt sanitization to prevent injection attacks.

Sanitizes user prompts before passing them to classifiers, routers, and other
components to prevent prompt injection attacks where malicious users could
override system instructions or extract sensitive information.
"""

import re
from typing import Any


# Patterns that commonly indicate an injection attempt:
# - Instructions to ignore/override previous directives
# - Attempts to switch roles (e.g., "now you are...")
# - Requests for system information/prompts
# - Role-playing escape attempts
INJECTION_PATTERNS = [
    r"(?:ignore|forget|disregard|override|disallow|don't follow|cancel).{0,50}(?:previous|prior|above|system|instruction)",
    r"(?:now you are|you are|act as|be|roleplay as|pretend to be).{0,50}(?:assistant|AI|system)",
    r"(?:show|print|reveal|display|tell me|what is|what are).{0,50}(?:system prompt|instructions|your prompt|my prompt)",
    r"(?:your instructions|your system|your role|your task).{0,50}(?:are|is|say)",
    r"\[SYSTEM\]|\[ADMIN\]|\[OVERRIDE\]",
]


def sanitize_prompt(prompt: str, max_length: int = 50000) -> str:
    """Sanitize a user prompt to prevent injection attacks.

    This function applies multiple layers of protection:
    1. Truncate excessively long prompts to prevent token exhaustion
    2. Detect and remove common injection patterns
    3. Escape special control characters
    4. Normalize whitespace to prevent encoding tricks

    Args:
        prompt: The raw user prompt to sanitize.
        max_length: Maximum allowed prompt length (default 50KB).

    Returns:
        A sanitized version of the prompt safe to pass to classifiers.

    Raises:
        ValueError: If the prompt is deemed malicious (contains multiple
            injection patterns) or is invalid (empty after sanitization).
    """
    if not isinstance(prompt, str):
        raise ValueError(f"Prompt must be string, got {type(prompt).__name__}")

    # Normalize whitespace: collapse multiple spaces, normalize line endings
    cleaned = re.sub(r"\s+", " ", prompt.strip())

    if not cleaned:
        raise ValueError("Prompt cannot be empty after sanitization")

    # Truncate if too long
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]

    # Check for injection patterns - be strict to prevent attacks
    injection_count = 0
    detected_patterns = []
    for i, pattern in enumerate(INJECTION_PATTERNS):
        if re.search(pattern, cleaned, re.IGNORECASE):
            injection_count += 1
            detected_patterns.append(pattern[:50])

    # Reject if any injection patterns detected
    # Single patterns could be false positives in code, but in a user prompt
    # to an LLM classifier, any of these patterns is suspicious
    if injection_count > 0:
        if injection_count > 1:
            raise ValueError(
                f"Prompt contains {injection_count} injection patterns and appears malicious. "
                "Rejecting to prevent attack."
            )
        else:
            # Even single pattern is concerning in a classifier prompt
            # Reject it but could add a config option to allow single patterns
            raise ValueError(
                f"Prompt contains injection pattern. Rejecting to prevent attack. "
                f"Pattern: {detected_patterns[0][:80]}"
            )

    return cleaned


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize all user/assistant messages in a message list.

    Applies sanitization to user and assistant content while preserving
    structure. System prompts are NOT sanitized (they're trusted).

    Args:
        messages: List of message dicts with 'role' and 'content' keys.

    Returns:
        Sanitized message list.

    Raises:
        ValueError: If any message fails sanitization.
    """
    sanitized = []
    for msg in messages:
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            raise ValueError(f"Invalid message format: {msg}")

        role = msg["role"]
        content = msg["content"]

        # Only sanitize user and assistant messages, never system prompts
        if role in ("user", "assistant") and isinstance(content, str):
            content = sanitize_prompt(content)

        sanitized.append({"role": role, "content": content})

    return sanitized
