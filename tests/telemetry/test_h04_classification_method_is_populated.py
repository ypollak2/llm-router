"""H-04 — the field explaining WHY a model was chosen was empty for 23,773 rows.

    classification_method populated: 0 of 23,773

Not sparsely populated. Zero, for the ledger's entire history. The cause was a
key mismatch that was silent on both sides:

    writers (router.py, 4 sites)   read  classification_data.get("method")
    builders (tools/routing.py)    write classification_data["classifier_type"]

`.get` returns None for a missing key, so the row was written anyway with an
empty field. Nothing raised, nothing logged. Any analysis of "which classifier
routes best" ran over an empty column and returned "no difference found" — a
clean-looking answer to a question the data could not address.

One site in the same file already read the correct key (`router.py:1548`), which
is what made this a drift rather than a decision.

The anti-vacuity test below is the important one: asserting "the field is
populated" over an empty fixture set passes trivially, and that is the same
shape of mistake as the bug itself.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from llm_router.router import _classification_method


@pytest.fixture
def ledger(tmp_path, monkeypatch) -> pathlib.Path:
    path = tmp_path / "routing_quality.jsonl"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(path))
    return path


def _rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# What producers in this repo actually emit.
PRODUCER_SHAPES = [
    ({"classifier_type": "heuristic"}, "heuristic"),
    ({"classifier_type": "semantic"}, "semantic"),
    ({"classifier_type": "build_fast_path"}, "build_fast_path"),
    # tolerated for any external caller using the old spelling
    ({"method": "legacy_spelling"}, "legacy_spelling"),
    # both present: the canonical key wins
    ({"classifier_type": "semantic", "method": "stale"}, "semantic"),
]


@pytest.mark.parametrize("data, expected", PRODUCER_SHAPES)
def test_resolver_reads_the_key_producers_write(data, expected):
    assert _classification_method(data) == expected


@pytest.mark.parametrize("empty", [None, {}, {"classifier_type": ""}, {"classifier_type": None}])
def test_absent_classification_is_none_not_empty_string(empty):
    """None means "not recorded". An empty string reads as a recorded blank."""
    assert _classification_method(empty) is None


def test_a_real_ledger_row_carries_the_method(ledger):
    """End to end: the value survives into the file, which is where it was lost."""
    from llm_router import router
    from llm_router.classify import TaskType
    from llm_router.profiles import RoutingProfile

    router._emit_quality_terminal(
        outcome="failed",
        correlation_id="r1",
        task_type=TaskType.CODE,
        profile=RoutingProfile.BALANCED,
        chain_attempts=["m"],
        chain_errors=[("m", "boom")],
        classification_method=_classification_method({"classifier_type": "heuristic"}),
    )
    rows = _rows(ledger)
    assert len(rows) == 1
    assert rows[0]["classification_method"] == "heuristic", (
        f"classification_method is {rows[0]['classification_method']!r} — still empty"
    )


def test_the_old_key_is_gone_from_the_ledger_writers():
    """The specific drift, pinned. `.get("method")` at a ledger write is the bug."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "llm_router" / "router.py").read_text(encoding="utf-8")
    assert '(classification_data or {}).get("method")' not in src, (
        "a ledger writer is reading `method` again; producers write `classifier_type`"
    )


def test_this_check_is_not_vacuous():
    """The denominator guard, and the same mistake the bug was.

    Asserting "every row has a method" over zero rows passes. Asserting the
    resolver works over zero shapes passes. Both must be shown non-empty first.
    """
    assert len(PRODUCER_SHAPES) >= 4, "too few producer shapes to prove anything"

    # and the resolver must be capable of returning None, or the parametrised
    # "absent" cases above would be proving nothing either
    assert _classification_method({"unrelated": "x"}) is None

    # a known-positive: the canonical key must genuinely be read
    assert _classification_method({"classifier_type": "sentinel"}) == "sentinel"


def test_producers_in_this_repo_still_use_the_canonical_key():
    """If a producer switches spelling, this fails here rather than in the data.

    The whole defect was that the two ends drifted and nothing compared them.
    """
    routing = (pathlib.Path(__file__).resolve().parents[2]
               / "src" / "llm_router" / "tools" / "routing.py").read_text(encoding="utf-8")
    assert '"classifier_type"' in routing, (
        "tools/routing.py no longer writes `classifier_type` — update "
        "_classification_method in router.py in the same commit"
    )
