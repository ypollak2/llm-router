"""audit/28 — the routing rate counts drafts PRODUCED, not drafts USED.

`hooks/draft_usage.py` already decides correctly whether a draft became the
answer, and wrote its verdict only to the debug log. Nothing tallied it, so the
number was computed, correct, and read by nobody — the CLASS-A shape R12 exists
to stop.

Measured when this was added: **0 used of 1130 offered**, lifetime, on a machine
whose banner reported "routes: 200".

Three separate defects are pinned here:

1. `routing_report.draft_acceptance()` — the tally that did not exist.
2. The counter registry entry, so `doctor` reads it (R12: a counter with no
   reader is not instrumentation).
3. `_build_mini_summary`'s `n = len(rows)` where `rows = recent(limit=200)` —
   the banner reported its own QUERY LIMIT as a count, and read "routes: 200"
   forever once the store held 200 rows. Measured at 9,015 rows.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from llm_router import counter_registry
from llm_router.routing_report import draft_acceptance

HOOK = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "llm_router" / "hooks" / "auto-route.py"
)


def _log(tmp_path, *lines: str) -> pathlib.Path:
    p = tmp_path / "auto-route-debug.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ── 1. The tally ──────────────────────────────────────────────────────────


def test_a_discarded_draft_counts_as_offered_not_used(tmp_path) -> None:
    log = _log(
        tmp_path,
        "[INVOCATION 1.0] DRAFT UNUSED: the draft from invocation 0.5 "
        "(ollama/qwen3.5:latest) was discarded; Claude answered instead",
        "[INVOCATION 2.0] DRAFT UNUSED: the draft from invocation 1.5 "
        "(ollama/qwen3.5:latest) was discarded; Claude answered instead",
    )
    assert draft_acceptance(log) == (0, 2)


def test_a_relayed_draft_counts_as_used(tmp_path) -> None:
    log = _log(
        tmp_path,
        "[INVOCATION 1.0] DRAFT USED: relayed as the answer",
        "[INVOCATION 2.0] DRAFT UNUSED: was discarded; Claude answered instead",
    )
    assert draft_acceptance(log) == (1, 2)


def test_it_returns_a_pair_never_a_bare_rate(tmp_path) -> None:
    """CLAUDE.md: a rate without its denominator is not a measurement.

    0 of 3 is a quiet morning; 0 of 1130 is the finding.
    """
    used, offered = draft_acceptance(_log(tmp_path, "nothing relevant here"))
    assert (used, offered) == (0, 0)


def test_a_missing_log_is_zero_zero_not_a_crash(tmp_path) -> None:
    assert draft_acceptance(tmp_path / "does-not-exist.log") == (0, 0)


# ── 2. The counter reaches an operator ────────────────────────────────────


def test_draft_acceptance_is_registered() -> None:
    """R12: the whole point is that something READS it."""
    assert "draft_acceptance" in counter_registry.counter_ids()


def test_no_drafts_offered_reads_unknown_not_a_clean_zero(monkeypatch) -> None:
    """Denominator disappearance.

    A machine that never drafted and one whose every draft was discarded are
    different facts. Only the second is a problem.
    """
    import llm_router.routing_report as rr

    monkeypatch.setattr(rr, "draft_acceptance", lambda *a, **k: (0, 0))
    reading = counter_registry.read_one("draft_acceptance")
    assert reading.value is None
    assert "no drafts offered" in reading.unknown_reason


def test_it_alarms_on_a_low_share_but_only_with_volume(monkeypatch) -> None:
    """Inverted vs every other counter here: LOW is the problem.

    And 0 of 3 must not alarm — this repo has reported four rates that were
    noise, all four briefly taken as a collapse.
    """
    import llm_router.routing_report as rr

    monkeypatch.setattr(rr, "draft_acceptance", lambda *a, **k: (0, 3))
    assert counter_registry.read_one("draft_acceptance").alarming is False

    monkeypatch.setattr(rr, "draft_acceptance", lambda *a, **k: (0, 44))
    assert counter_registry.read_one("draft_acceptance").alarming is True

    monkeypatch.setattr(rr, "draft_acceptance", lambda *a, **k: (40, 44))
    assert counter_registry.read_one("draft_acceptance").alarming is False


# ── 3. The banner stops reporting its own limit ───────────────────────────


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("auto_route_a28", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_banner_does_not_print_a_bare_count_at_the_limit(hook, monkeypatch) -> None:
    """`routes: 200` was the LIMIT, not a measurement.

    Asserted on the rendered string, because the rendered string is what misled
    a reader every 10 prompts.
    """
    class _Store:
        def recent(self, limit):
            return [{"model_tier": "local", "task_type": "code"}] * limit

    import llm_router.lineage as lineage

    monkeypatch.setattr(lineage, "LineageStore", _Store)
    out = hook._build_mini_summary()
    assert out is not None
    assert "routes: 200" not in out, (
        "the banner is reporting its own query limit as a count again"
    )
    assert "last 200" in out


def test_the_banner_prints_an_exact_count_below_the_limit(hook, monkeypatch) -> None:
    """Anti-vacuity: it must still report a real number when there is one."""
    class _Store:
        def recent(self, limit):
            return [{"model_tier": "local", "task_type": "code"}] * 7

    import llm_router.lineage as lineage

    monkeypatch.setattr(lineage, "LineageStore", _Store)
    out = hook._build_mini_summary()
    assert "routes: 7" in out and "last" not in out


def test_the_banner_shows_drafts_used_over_offered(hook, monkeypatch) -> None:
    class _Store:
        def recent(self, limit):
            return [{"model_tier": "local", "task_type": "code"}] * 5

    import llm_router.lineage as lineage
    import llm_router.routing_report as rr

    monkeypatch.setattr(lineage, "LineageStore", _Store)
    monkeypatch.setattr(rr, "draft_acceptance", lambda *a, **k: (0, 44))
    assert "drafts used: 0/44" in hook._build_mini_summary()


def test_the_banner_no_longer_prints_a_structurally_zero_cost(hook, monkeypatch) -> None:
    """"recorded cost" summed a placeholder column and always read $0.0000.

    1387 of 1601 routing_decisions carry a flat $0.01 placeholder and the
    trusted rows are all free/local/subscription. Printing it every 10 prompts
    taught the reader to skip the line that matters.
    """
    class _Store:
        def recent(self, limit):
            return [{"model_tier": "local", "task_type": "code", "cost_usd": 0.0}] * 5

    import llm_router.lineage as lineage

    monkeypatch.setattr(lineage, "LineageStore", _Store)
    out = hook._build_mini_summary()
    assert "recorded cost" not in out and "$0.0000" not in out
