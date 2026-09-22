"""M-07 — `secret_scrubber` is the only scrubber. `error_sanitization` is gone.

`secret_scrubber.scrub_text` claims to be the single source of truth, and its
docstring says three drifted scrubbers were *unified* under CHZ-SEC-01. Checking
that claim in the 2026-09-21 audit showed one of the three was **orphaned, not
fixed**: `error_sanitization.py` survived with 0 production callers, a weaker
pattern set, and a name that invited exactly the wiring that would reintroduce
the leak.

Measured before deletion, against the same six credential classes:

    class        error_sanitization    secret_scrubber
    anthropic              MISSES              redacts
    openai                 MISSES              redacts
    github                 MISSES              redacts
    jwt                    MISSES              MISSES
    pem                    MISSES              redacts
    aws                   redacts              redacts

Five of six. It also logged the *pre-redaction* original through stdlib
`logging.debug(..., extra=...)` — invisible under this project's structlog
config, live under any host that renders LogRecord extras (Sentry breadcrumbs,
python-json-logger).

The `jwt` row is why this file exists rather than a commit message. The audit
listed JWT as a class `error_sanitization` missed, which read as though the
canonical scrubber covered it. It did not. The comparison had been measuring a
gap **both** scrubbers shared, and deleting the weaker module would have quietly
closed the file on a hole that stayed open. JWT coverage was added to
`secret_scrubber` in the same change.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "llm_router"


def _fixture(prefix: str, body: str) -> str:
    return prefix + body


def test_error_sanitization_module_is_gone():
    """Deleted, not merely unused. An unused module gets re-wired."""
    assert not (SRC / "error_sanitization.py").exists(), (
        "error_sanitization.py is back. It has no callers, misses five of six "
        "credential classes, and leaks the pre-redaction original to logging "
        "extras. Delegate to secret_scrubber.scrub_text instead."
    )
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("llm_router.error_sanitization")


def test_canonical_scrubber_is_actually_a_superset():
    """The claim this module makes about itself, checked against its rivals.

    Five other secret-pattern tables live in this repository:

        org_policy._PLAINTEXT_SECRET_PATTERNS
        signals/pii._SECRET_PATTERNS
        library/store._SECRET_PATTERNS
        hooks/agent-route._AGENT_SECRET_PATTERNS   (byte-identical to the above)

    The audit counted four scrubbers. There were six tables, and — the part that
    matters — the canonical one was **not** a superset of the others: it missed
    Slack tokens, JWTs and the pk-/rk- key prefixes that the weaker, largely
    unused tables already carried. Six content stores delegate here, so a Slack
    token reaching any of them was persisted in plaintext.

    A shape-based test ("no module may define patterns") would fail on the two
    legitimate *detectors*, which flag rather than rewrite and reasonably carry
    broader rules. So this asserts the property instead: whatever else exists,
    the canonical scrubber must cover it.
    """
    from llm_router.secret_scrubber import scrub_text

    # every shape carried by any other table in the repo
    rivals = {
        "slack_bot": _fixture("xoxb-", "123456789012-abcdefghijklmnopqrst"),
        "slack_user": _fixture("xoxp-", "123456789012-abcdefghijklmnopqrst"),
        "pk_prefixed": _fixture("pk-", "AbCdEfGhIjKlMnOpQrStUvWx"),
        "rk_prefixed": _fixture("rk-", "AbCdEfGhIjKlMnOpQrStUvWx"),
        "jwt": _fixture("eyJhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0NTYifQ.dBjftJeZ4CVPmB92K27u"),
        "openssh_block": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    }
    missing = [k for k, v in rivals.items() if scrub_text(v) == v]
    assert not missing, (
        f"the canonical scrubber misses {len(missing)} shape(s) that other "
        f"pattern tables in this repo already carry: {missing}. Every content "
        f"store delegates here, so these persist in plaintext."
    )


def test_scrubber_does_not_redact_ordinary_content():
    """Anti-over-correction. A scrubber rewrites; a false positive corrupts data.

    This is why signals/pii's bare 40-char base64 rule was deliberately NOT
    adopted: it matches any git SHA, hash or embedding fragment, and a cached
    response is real content that must survive.
    """
    from llm_router.secret_scrubber import scrub_text

    benign = [
        "a git sha 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a",
        "version pk-1 shipped",
        "the package-lock file",
        "llm_router.semantic.scope.scope_key",
    ]
    for text in benign:
        assert scrub_text(text) == text, f"ordinary content was redacted: {text!r}"


CREDENTIALS = {
    "anthropic": _fixture("sk-ant-api03-", "A" * 40),
    "openai": _fixture("sk-proj-", "B" * 40),
    "github": _fixture("ghp_", "C" * 36),
    "aws": _fixture("AKIA", "IOSFODNN7EXAMPLE"),
    "jwt": _fixture("eyJhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0NTYifQ.dBjftJeZ4CVPmB92K27u"),
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
}


@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_canonical_scrubber_covers_every_class_the_orphan_was_measured_on(name):
    """The survivor must cover everything the comparison was drawn over."""
    from llm_router.secret_scrubber import scrub_text

    value = CREDENTIALS[name]
    assert scrub_text(value) != value, f"canonical scrubber does not redact {name}"


def test_jwt_pattern_does_not_swallow_ordinary_text():
    """Anti-over-correction: a greedy pattern would redact dotted identifiers.

    Three base64url segments is a common enough shape that an unanchored version
    of this pattern would match module paths and prose.
    """
    from llm_router.secret_scrubber import scrub_text

    benign = [
        "llm_router.semantic.scope.scope_key",
        "Run the thing. Then check it. Done.",
        "version 1.2.3 released",
        "a.b.c",
    ]
    for text in benign:
        assert scrub_text(text) == text, f"JWT pattern wrongly redacted: {text!r}"


def test_this_check_is_not_vacuous():
    """The fixture set must be non-empty and genuinely secret-shaped."""
    assert len(CREDENTIALS) >= 6
    from llm_router.secret_scrubber import SECRET_PATTERNS

    assert "jwt" in SECRET_PATTERNS, "the JWT pattern this test was written for is missing"
