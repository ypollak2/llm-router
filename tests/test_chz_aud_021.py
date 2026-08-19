"""Regression tests for CHZ-AUD-021.

The Anthropic API-key pattern in secret_scrubber missed the *real* key
format ``sk-ant-api03-<base64url>`` because the credential portion contains
hyphens and underscores that were absent from the original character class.
These table-driven tests exercise realistic provider key shapes to ensure
secrets are actually redacted from log values.
"""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_router.secret_scrubber import _scrub_value  # noqa: E402


# (raw value, expected redaction marker)
REAL_KEY_SHAPES = [
    # Real modern Anthropic key: sk-ant-api03- + base64url (has - and _).
    (
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEF",
        "[REDACTED-ANTHROPIC_API_KEY]",
    ),
    (
        "sk-ant-api03-_abc-DEF_ghi-JKL_mno-PQR_stu-VWX_yz012345_67",
        "[REDACTED-ANTHROPIC_API_KEY]",
    ),
    # Legacy-style anthropic key (no hyphenated credential) still matches.
    (
        "sk-ant-AbCdEfGhIjKlMnOpQrStUvWx",
        "[REDACTED-ANTHROPIC_API_KEY]",
    ),
    # OpenAI project key: sk-proj- + base64url with _ and -.
    (
        "sk-proj-Ab_Cd-Ef1234567890ghijklMNOPqrstuvwx",
        "[REDACTED-OPENAI_API_KEY]",
    ),
    # Classic OpenAI key.
    (
        "sk-1234567890abcdefghijABCDEFGHIJ",
        "[REDACTED-OPENAI_API_KEY]",
    ),
    # Google API key.
    (
        "AIzaSyD-1234567890abcdefghijklmnopqrstuvwx",
        "[REDACTED-GOOGLE_API_KEY]",
    ),
    # AWS access key id.
    (
        "AKIAIOSFODNN7EXAMPLE",
        "[REDACTED-AWS_KEY_ID]",
    ),
]


@pytest.mark.parametrize("raw,expected", REAL_KEY_SHAPES)
def test_scrub_value_redacts_real_provider_key_shapes(raw, expected):
    assert _scrub_value(raw) == expected


def test_real_anthropic_key_embedded_in_message_is_redacted():
    key = "sk-ant-api03-AbCd_Ef-Gh1234567890IjKlMnOpQrStUvWx"
    msg = f"call failed with Authorization: Bearer {key}"
    result = _scrub_value(msg)
    assert key not in result


NON_SECRETS = [
    "hello world",
    "sk-ant-short",  # too short to be a credential
    "",
]


@pytest.mark.parametrize("raw", NON_SECRETS)
def test_scrub_value_leaves_non_secrets_unchanged(raw):
    assert _scrub_value(raw) == raw
