"""The persistence redactor must work in the configuration users actually install.

WHY THIS EXISTS
===============

``persist_redact`` lived in ``llm_router/enterprise/redaction.py``. ``enterprise/``
is excluded from public distributions. Five persistence paths imported it inside
a ``try/except`` that falls back to ``secret_scrubber.scrub_text``:

    result_cache · semantic_cache · idempotency · context · session_store

So on every real install the import failed, the fallback ran, and the fallback
does not carry those patterns. Measured against the PUBLISHED `llm-routing`
package (installed from PyPI into a clean venv, `llm_router.enterprise` confirmed
absent), `scrub_text` alone:

    jwt           LEAKED
    slack_token   LEAKED
    email         LEAKED
    us_ssn        LEAKED
    us_phone      LEAKED
    credit_card   LEAKED
    prose_secret  LEAKED

Seven of seven, straight to disk.

THE SUITE WAS GREEN THE WHOLE TIME
==================================

7741 tests passing, including the persistence-hardening tests that assert
exactly these secrets do NOT reach disk. They passed because the development
tree HAS ``enterprise/``, so the primary path was always taken. The tests
exercised the one configuration no user runs.

That is the finding worth keeping: not "a pattern was missing" but "a security
control was only ever tested in its strongest configuration". A fallback that
is never exercised is not a fallback, it is an untested branch that happens to
be the one that ships.

It surfaced from the downstream sync — llm-routing excludes ``enterprise/`` too,
so its suite ran the shipped configuration and nine tests failed immediately.
The sync found an upstream defect by accident, because it changed which
configuration got tested.

THE FIX
=======

The module moved to ``llm_router/persist_redaction.py``, in core, where it ships.
``enterprise/redaction.py`` is now a re-export shim so existing imports keep
working. Nothing in it was ever enterprise-specific — it is a pattern table, a
Luhn check, and a layered scrub.

CONTROL (re-run if edited)
==========================

Point ``session_store._scrub_secrets`` back at ``llm_router.enterprise.redaction``
AND delete the shim: ``test_every_persistence_path_uses_the_core_redactor``
fails, and every leak assertion below fails with the secret in the output.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from llm_router.persist_redaction import persist_redact

#: One per pattern family the enterprise redactor carried and the fallback did
#: not. Values are synthetic.
LEAKY_INPUTS = {
    "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "slack_token": "xoxb-1234567890-abcdefghijkl",
    "email": "victim@example.com",
    "us_ssn": "123-45-6789",
    "us_phone": "+1-555-123-4567",
    "credit_card": "4111 1111 1111 1111",
    "prose_secret": "the launch code is ORANGE-742",
}

#: Every module that writes user content to disk and must redact first.
PERSISTENCE_MODULES = (
    "llm_router.result_cache",
    "llm_router.semantic_cache",
    "llm_router.idempotency",
    "llm_router.context",
    "llm_router.session_store",
)


@pytest.mark.parametrize("name,secret", sorted(LEAKY_INPUTS.items()))
def test_secret_does_not_survive_persist_redact(name: str, secret: str):
    out = persist_redact(f"prefix {secret} suffix")
    assert secret not in out, (
        f"{name} survived persist_redact and would be written to disk verbatim"
    )


def test_the_redactor_is_importable_without_enterprise():
    """The whole point: it must resolve from core, not from enterprise/."""
    module = importlib.import_module("llm_router.persist_redaction")
    assert not module.__name__.startswith("llm_router.enterprise"), (
        "persist_redaction resolved back into the enterprise tree, which does "
        "not ship — the move has been undone"
    )


def test_every_persistence_path_uses_the_core_redactor():
    """All five, checked by source rather than by one representative.

    Checking one and assuming the rest is how four of these stayed broken while
    the fifth looked fine. Any new persistence path that imports the enterprise
    shim instead fails here.
    """
    offenders = []
    for name in PERSISTENCE_MODULES:
        src = inspect.getsource(importlib.import_module(name))
        if "from llm_router.enterprise.redaction import persist_redact" in src:
            offenders.append(name)
        elif "persist_redact" in src and "llm_router.persist_redaction" not in src:
            offenders.append(f"{name} (uses persist_redact from an unknown source)")
    assert not offenders, (
        f"these persistence paths do not import the core redactor: {offenders}. "
        f"In a published install their import fails and they silently fall back "
        f"to secret_scrubber.scrub_text, which carries none of these patterns."
    )


def test_the_enterprise_shim_still_resolves():
    """Existing enterprise imports must not break on the move."""
    shim = importlib.import_module("llm_router.enterprise.redaction")
    assert shim.persist_redact is persist_redact, (
        "the back-compat shim no longer points at the core implementation, so "
        "enterprise callers would get a different redactor than everyone else"
    )


def test_the_fallback_gap_is_recorded_not_assumed():
    """secret_scrubber alone is genuinely weaker — assert it, do not trust it.

    If someone later strengthens `scrub_text` to cover these, this test tells
    them so rather than leaving the docstring above quietly wrong. It asserts
    the gap EXISTS, so the day it closes is a visible event.
    """
    from llm_router.secret_scrubber import scrub_text

    still_leaking = [
        name for name, secret in LEAKY_INPUTS.items() if secret in scrub_text(secret)
    ]
    assert still_leaking, (
        "secret_scrubber.scrub_text now covers every pattern this test tracks. "
        "That is good news, and it means the fallback is no longer a downgrade "
        "— update this file and the persist_redaction docstring to say so."
    )
