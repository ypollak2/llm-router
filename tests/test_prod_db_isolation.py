"""Unisolated tests must not write into the user's real ~/.llm-router/usage.db.

WHAT WENT WRONG
---------------
`cost.py` carried a "stub-detection guard" whose docstring claimed that unisolated
tests "can never pollute the real ~/.llm-router/usage.db". It matched an exact fingerprint:

    input_tokens == 100 and output_tokens in (50, 100) and cost_usd in (0.001, 0.003)

The fixtures actually in use look nothing like that — ``in=62/out=164/cost=0.000108``,
``in=74/out=1``, ``in=97/out=126``. Measured against the rows already in the production
database, that guard would have blocked **0 of 28,536** (0.0%).

The result: 28,536 synthetic routing decisions in the user's real database, 69.4% of
every row in the table, all naming ``openai/gpt-4o-mini``. The dashboard reported them
as routing behaviour. Filtering them out (they are exactly the rows with
``classifier_type='unknown'``) shows what the router actually does: ``hermes3:8b``
(local) 38.6%, ``gpt-4o`` 35.6% — and gpt-4o-mini 0.0%.

So the product's primary user-facing surface understated local routing by 3x and
invented a 69% share for a model the router never chose.

WHY A FINGERPRINT WAS THE WRONG SHAPE
-------------------------------------
It enumerates the stub values its author happened to know about. Every fixture added
afterwards walks through. The property that actually matters is not "does this row look
synthetic" but "is a test writing to the production database" — which is directly
observable, needs no guesswork, and cannot drift as fixtures change.

That is the same defect class as the frozen mutation sample, the `also_copy` substring
check, and the sha256 restore check: a guard that matches only the shapes its author
imagined, reporting clean while blind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.cost import _refuse_unisolated_test_write


@pytest.fixture(autouse=True)
def _no_stub_override(monkeypatch):
    """These assertions are about the default posture, not the escape hatch."""
    monkeypatch.delenv("LLM_ROUTER_ALLOW_STUBS", raising=False)


def _prod_path() -> Path:
    return Path.home() / ".llm-router" / "usage.db"


def test_refuses_when_a_test_targets_the_real_production_database():
    """The load-bearing assertion. This test is itself running under pytest, so
    PYTEST_CURRENT_TEST is set — exactly the situation that produced 28,536 rows."""
    assert _refuse_unisolated_test_write(_prod_path()) is True


def test_allows_an_isolated_temp_database(tmp_path):
    """The 7,000-test suite writes constantly via the temp_db fixture, which repoints
    LLM_ROUTER_DB_PATH. Blocking those would break the suite and teach everyone to set
    LLM_ROUTER_ALLOW_STUBS=1 permanently, which would put us straight back here."""
    assert _refuse_unisolated_test_write(tmp_path / "usage.db") is False


def test_allows_production_writes_outside_pytest(monkeypatch):
    """Real usage must be unaffected. Without PYTEST_CURRENT_TEST this is a user, not
    a test, and their routing decisions belong in their database."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert _refuse_unisolated_test_write(_prod_path()) is False


def test_escape_hatch_is_honoured(monkeypatch):
    """Tests that deliberately exercise the production path can opt in."""
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    assert _refuse_unisolated_test_write(_prod_path()) is False


def test_the_old_fingerprint_would_not_have_caught_the_real_fixtures():
    """Pins the defect so the fingerprint cannot quietly come back as the only check.

    These are real (input_tokens, output_tokens, cost_usd) triples taken from the
    polluted production rows. Not one satisfies the old condition.
    """
    observed = [
        (62, 164, 0.00010769999999999999),
        (74, 1, 1.17e-05),
        (97, 126, 9.015e-05),
        (70, 174, 0.00011489999999999999),
        (80, 97, 7.019999999999999e-05),
    ]
    for inp, out, cost in observed:
        old_guard_would_block = inp == 100 and out in (50, 100) and cost in (0.001, 0.003)
        assert not old_guard_would_block, (
            f"fixture {(inp, out, cost)} would have been caught by the fingerprint; "
            "the sample of real polluted rows is no longer representative"
        )


def test_guard_does_not_depend_on_token_or_cost_values():
    """The replacement must key on ISOLATION, not on what a row looks like.

    Asserted by construction: the function takes only a path. If someone reintroduces
    value-sniffing they have to change this signature, which fails here loudly.
    """
    import inspect

    params = list(inspect.signature(_refuse_unisolated_test_write).parameters)
    assert params == ["db_path"], (
        f"expected a path-only signature, got {params!r} — a guard that inspects row "
        "values is the fingerprint defect returning under a new name"
    )
