"""Secret scrubbing for structured logs to prevent credential leakage.

This module provides structlog processors that redact sensitive information
from log records before they're written to stdout/files. It prevents accidental
leakage of API keys, tokens, passwords, and other credentials.
"""

import re
from typing import Any

# Common secret patterns to detect and redact
SECRET_PATTERNS = {
    # API keys (various formats) - must be before more general patterns
    # Real Anthropic keys are sk-ant-api03-<base64url>, whose credential
    # portion contains hyphens and underscores, so those must be allowed
    # in the character class or the key passes through unscrubbed.
    "anthropic_api_key": re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"),
    # OpenAI project keys (sk-proj-...) likewise use base64url with _ and -.
    "openai_api_key": re.compile(r"sk-(?:proj-)?[a-zA-Z0-9_-]{20,}"),
    "google_api_key": re.compile(r"AIza[a-zA-Z0-9\-_]{35,}"),
    "gemini_api_key": re.compile(r"GOOG[a-zA-Z0-9]{10,}"),
    # AWS credentials
    "aws_key_id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "aws_secret": re.compile(r"aws[_-]?secret[_-]?access[_-]?key[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9/+]{40}", re.IGNORECASE),
    # Other tokens and secrets
    "bearer_token": re.compile(r"bearer\s+[a-zA-Z0-9._\-]+", re.IGNORECASE),
    "authorization": re.compile(r"authorization[\"']?\s*[:=]\s*[\"']?[^\s\"']+", re.IGNORECASE),
    "api_key": re.compile(r"api[_-]?key[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9]{20,}", re.IGNORECASE),
    "token": re.compile(r"['\"]?token['\"]?\s*[:=]\s*['\"]?[a-zA-Z0-9._\-]{20,}['\"]?", re.IGNORECASE),
    "password": re.compile(r"password[\"']?\s*[:=]\s*[\"']?[^\"'\s:;,]+[\"']?", re.IGNORECASE),
    "secret": re.compile(r"secret[\"']?\s*[:=]\s*[\"']?[^\"'\s:;,]+[\"']?", re.IGNORECASE),
    # Merged from session_store (CHZ-SEC-01: unify the drifted scrubbers so this
    # module is the single superset). GitHub tokens, generic UPPER_KEY=value
    # assignments, and PEM private-key blocks.
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "env_key_assignment": re.compile(r"\b[A-Z][A-Z0-9_]*_(?:API_)?KEY\s*[=:]\s*\S+"),
    "private_key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
}

# Field names that should never be logged
SENSITIVE_FIELD_NAMES = {
    "api_key",
    "api_keys",
    "apikey",
    "api-key",
    "secret",
    "secrets",
    "password",
    "passwords",
    "token",
    "tokens",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "private_key",
    "access_key",
    "secret_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "signing_key",
}

# Environment variable names that should never be logged
SENSITIVE_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_URL",
    "REDIS_URL",
}


def _should_scrub_field(field_name: str) -> bool:
    """Check if a field name indicates sensitive content.
    
    Matches field names like api_key, apiKey, api-key, etc.
    Uses word boundaries to avoid false positives like 'keys'.
    """
    field_lower = field_name.lower()
    
    # First, check if the exact field name matches
    if field_lower in SENSITIVE_FIELD_NAMES:
        return True
    
    # Also check normalized versions (remove underscores, hyphens)
    normalized = re.sub(r'[_\-\s]+', '', field_lower)
    if normalized in SENSITIVE_FIELD_NAMES:
        return True
    
    # Check if field name starts with any sensitive field name
    # (to catch things like "api_key_value" -> starts with "api_key")
    for sensitive in SENSITIVE_FIELD_NAMES:
        if field_lower.startswith(sensitive + '_') or \
           field_lower.startswith(sensitive + '-') or \
           field_lower == sensitive:
            return True
    
    return False


def scrub_text(text: str) -> str:
    """Canonical text scrubber (CHZ-SEC-01/02/09): redact secret *substrings*.

    Unlike ``_scrub_value`` (which replaces an entire field value when any
    pattern hits — right for structured log fields), this substitutes only the
    matched credential inside a larger body of text, so a transcript line or an
    error string keeps its non-secret content. This is the single source of
    truth every content store should call, replacing the three drifted
    per-module scrubbers (secret_scrubber / session_store / error_sanitization).
    """
    if not text or not isinstance(text, str):
        return text
    for pattern_name, pattern in SECRET_PATTERNS.items():
        text = pattern.sub(f"[REDACTED-{pattern_name.upper()}]", text)
    return text


def _scrub_value(value: Any) -> Any:
    """Scrub a single value for secrets."""
    if not isinstance(value, str):
        return value

    if len(value) == 0:
        return value

    # Check against all patterns
    for pattern_name, pattern in SECRET_PATTERNS.items():
        if pattern.search(value):
            # Return a redacted version preserving length hint
            return f"[REDACTED-{pattern_name.upper()}]"

    return value


def scrub_event(event: dict[str, Any]) -> dict[str, Any]:
    """Scrub secrets from a structlog event dict.

    This processor removes sensitive information from log events before they're
    written to disk/stdout. It checks both field names and values.

    Args:
        event: The structlog event dict.

    Returns:
        The event dict with sensitive fields redacted.
    """
    scrubbed = {}

    for key, value in event.items():
        # Only redact string values if the field name indicates secrets
        # (for dicts/lists, we'll scrub recursively)
        if _should_scrub_field(key) and isinstance(value, str):
            scrubbed[key] = "[REDACTED]"
            continue

        # Scrub sensitive values
        if isinstance(value, str):
            scrubbed[key] = _scrub_value(value)
        elif isinstance(value, dict):
            # Recursively scrub nested dicts
            scrubbed[key] = scrub_event(value)
        elif isinstance(value, (list, tuple)):
            # Scrub items in lists/tuples
            scrubbed_items = []
            for v in value:
                if isinstance(v, dict):
                    scrubbed_items.append(scrub_event(v))
                elif isinstance(v, str):
                    scrubbed_items.append(_scrub_value(v))
                else:
                    scrubbed_items.append(v)
            scrubbed[key] = type(value)(scrubbed_items)
        else:
            scrubbed[key] = value

    return scrubbed


# WP-15: scrub_environment() deleted. It had ZERO production callers and a
# NARROWER allowlist than the one actually in use (safe_subprocess's), so it
# read as a second, weaker layer of protection that nothing invoked. Dead
# safety code is worse than none: it makes an auditor count a defence that
# does not run. The live path is safe_subprocess's env allowlist (WP-01).

def structlog_scrubber_processor(logger, name, event):
    """Structlog processor for scrubbing secrets from events.

    This should be added to the structlog processor chain to automatically
    redact secrets before they're logged.

    Example:
        structlog.configure(processors=[
            ...,
            structlog_scrubber_processor,
            ...
        ])

    Args:
        logger: The structlog logger.
        name: The name of the logger.
        event: The event dict to process.

    Returns:
        The scrubbed event dict.
    """
    return scrub_event(event)
