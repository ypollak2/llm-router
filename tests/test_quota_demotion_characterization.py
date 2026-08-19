"""Characterization tests for the existing quota-aware demotion path.

This file documents *what the current code actually does* across:

* **3 IDE host scenarios** (claude_code / codex_cli / gemini_cli) — the
  "main subscription" provider differs per host.
* **3 routing profiles** (BALANCED / PREMIUM / QUOTA_BALANCED).
* **4 pressure levels** (none / medium / high / extreme).
* **3 task complexities** (simple / moderate / complex).

= 108 characterization cells, plus dedicated unit tests for the two
existing primitives (``_reorder_by_quota_pressure``,
``get_balanced_provider_order`` + ``reorder_chain_by_providers``).

Each cell answers two yes/no metrics:

* **Success-rate-preservation.** Did the reorder produce a non-empty
  chain that the router can still try? The answer must always be
  ``True`` — a router that empties the chain under pressure has
  failed.
* **Quota-exploitation.** Where in the chain did the *main
  subscription* provider end up? Lower index = more exploitation,
  higher index = better demotion. Specifically:
    - Position 0 = head; we expose the strained seat to the next call.
    - Final position = demoted; routing tries everything else first.

Threshold for "demotion happened" = main subscription is **not**
at position 0 of the result chain. Loose by design so the
characterization is descriptive rather than prescriptive.

Stage-3 evaluation then maps these results against explicit pass/fail
thresholds to decide whether the existing path is sufficient or we
must escalate to option #2 (SUBSCRIPTION_LOCAL reorder applied across
BALANCED/PREMIUM too).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pytest

from llm_router.dynamic_routing import _reorder_by_quota_pressure
from llm_router.quota_balance import (
    get_balanced_provider_order,
    reorder_chain_by_providers,
)


# ────────────────────────────────────────────────────────────────────────
# Host scenarios — three real-world llm_router deployments
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostScenario:
    """One IDE host's view of llm_router."""

    name: str
    main_sub_provider: str  # The provider whose subscription the host owns
    main_sub_key: str        # ...and its key in get_provider_pressures()
    representative_chain: tuple[str, ...]


# The chains below are intentionally simplified versions of what the
# scorer would produce. They include one model per "tier" the
# characterization cares about so the assertions are deterministic.
# Each chain mixes: main subscription · other subscriptions · ollama ·
# other paid API.

CLAUDE_CODE_HOST = HostScenario(
    name="claude_code",
    main_sub_provider="anthropic",
    main_sub_key="claude",
    representative_chain=(
        "anthropic/claude-sonnet-4-6",      # main subscription
        "anthropic/claude-haiku-4-5",       # main subscription, lower tier
        "codex/gpt-5",                       # other subscription
        "gemini_cli/gemini-2.5-pro",         # other subscription
        "ollama/llama3",                     # free local
        "openai/gpt-4o",                     # other paid API
        "groq/llama-3.1-70b",                # other paid API
    ),
)

CODEX_CLI_HOST = HostScenario(
    name="codex_cli",
    main_sub_provider="codex",
    main_sub_key="codex",
    representative_chain=(
        "codex/gpt-5",                       # main subscription
        "codex/o3",                          # main subscription, lower tier
        "anthropic/claude-sonnet-4-6",       # other subscription
        "gemini_cli/gemini-2.5-pro",         # other subscription
        "ollama/llama3",                     # free local
        "openai/gpt-4o",                     # other paid API
        "groq/llama-3.1-70b",                # other paid API
    ),
)

GEMINI_CLI_HOST = HostScenario(
    name="gemini_cli",
    main_sub_provider="gemini_cli",
    main_sub_key="gemini_cli",
    representative_chain=(
        "gemini_cli/gemini-2.5-pro",         # main subscription
        "gemini_cli/gemini-2.5-flash",       # main subscription, lower tier
        "anthropic/claude-sonnet-4-6",       # other subscription
        "codex/gpt-5",                       # other subscription
        "ollama/llama3",                     # free local
        "openai/gpt-4o",                     # other paid API
        "groq/llama-3.1-70b",                # other paid API
    ),
)


HOSTS = (CLAUDE_CODE_HOST, CODEX_CLI_HOST, GEMINI_CLI_HOST)

# Four pressure tiers — match the user's mental model.
PRESSURE_LEVELS: dict[str, float] = {
    "none":    0.00,
    "medium":  0.40,
    "high":    0.85,
    "extreme": 0.99,
}

# Three complexities the classifier emits.
COMPLEXITIES = ("simple", "moderate", "complex")


def _provider_of(model_id: str) -> str:
    head, _, _ = model_id.partition("/")
    # Mirror reorder_chain_by_providers' provider classification.
    if head == "anthropic":
        return "claude"
    return head or model_id


def _main_sub_position(chain: list[str], main_sub_key: str) -> int | None:
    """Index of the first model belonging to the main subscription
    provider in ``chain``; ``None`` if absent."""
    for i, model in enumerate(chain):
        if _provider_of(model) == main_sub_key:
            return i
    return None


# ────────────────────────────────────────────────────────────────────────
# Section A — _reorder_by_quota_pressure unit tests
# (the static-time pressure reader used by BALANCED + PREMIUM through
# dynamic_routing.build_dynamic_routing_table)
# ────────────────────────────────────────────────────────────────────────


class TestStaticTimeReorder:
    """``dynamic_routing._reorder_by_quota_pressure`` documents the
    behaviour BALANCED + PREMIUM get for free without selecting
    QUOTA_BALANCED. Pressure source is the profile.yaml dict; threshold
    is **0.85** (hard-coded). The provider-name key in the dict matches
    ``profiles.provider_from_model`` (``"anthropic"`` for
    ``anthropic/*``)."""

    def test_no_pressure_dict_is_noop(self) -> None:
        chain = ["anthropic/sonnet", "ollama/llama3", "openai/gpt-4o"]
        assert _reorder_by_quota_pressure(chain, {}) == chain

    def test_below_threshold_is_noop(self) -> None:
        chain = ["anthropic/sonnet", "ollama/llama3"]
        # 0.84 stays put — threshold is strict ``>= 0.85``.
        assert _reorder_by_quota_pressure(chain, {"anthropic": 0.84}) == chain

    def test_at_threshold_demotes(self) -> None:
        chain = ["anthropic/sonnet", "ollama/llama3"]
        result = _reorder_by_quota_pressure(chain, {"anthropic": 0.85})
        assert result == ["ollama/llama3", "anthropic/sonnet"]

    def test_above_threshold_demotes(self) -> None:
        chain = [
            "anthropic/sonnet",
            "gemini_cli/pro",
            "ollama/llama3",
            "openai/gpt-4o",
        ]
        result = _reorder_by_quota_pressure(
            chain, {"anthropic": 0.95, "gemini_cli": 0.10}
        )
        # Normal-pressure tier preserves original order; anthropic
        # appended at the end.
        assert result == [
            "gemini_cli/pro",
            "ollama/llama3",
            "openai/gpt-4o",
            "anthropic/sonnet",
        ]

    def test_multiple_strained_providers_both_demoted(self) -> None:
        chain = [
            "anthropic/sonnet",
            "gemini_cli/pro",
            "ollama/llama3",
        ]
        result = _reorder_by_quota_pressure(
            chain, {"anthropic": 0.90, "gemini_cli": 0.99}
        )
        # Both demoted, in original relative order; ollama survives at front.
        assert result == [
            "ollama/llama3",
            "anthropic/sonnet",
            "gemini_cli/pro",
        ]

    def test_provider_not_in_pressure_dict_treated_as_zero(self) -> None:
        chain = ["openai/gpt-4o", "anthropic/sonnet"]
        # openai not in pressure dict → assumed 0.0 → stays.
        result = _reorder_by_quota_pressure(chain, {"anthropic": 0.99})
        assert result == ["openai/gpt-4o", "anthropic/sonnet"]


# ────────────────────────────────────────────────────────────────────────
# Section B — get_balanced_provider_order unit tests
# (the live runtime reorder used by QUOTA_BALANCED only)
# ────────────────────────────────────────────────────────────────────────


class TestBalancedProviderOrder:
    """``quota_balance.get_balanced_provider_order`` decides the
    subscription-provider priority for QUOTA_BALANCED. Two regimes:

    * **In-band** (spread ≤ tolerance, default 10%): fixed free-first
      tiebreak ``[codex, gemini_cli, claude]``.
    * **Imbalanced**: ascending by pressure (least-used first).

    Threshold is ``tolerance = 0.10`` (the *spread*, not absolute
    pressure)."""

    def test_empty_pressures_returns_default(self) -> None:
        assert get_balanced_provider_order({}) == [
            "codex", "gemini_cli", "claude"
        ]

    def test_all_zero_pressures_in_band(self) -> None:
        assert get_balanced_provider_order(
            {"claude": 0.0, "gemini_cli": 0.0, "codex": 0.0}
        ) == ["codex", "gemini_cli", "claude"]

    def test_within_tolerance_in_band(self) -> None:
        # spread = 0.10 = tolerance → still in band.
        assert get_balanced_provider_order(
            {"claude": 0.20, "gemini_cli": 0.15, "codex": 0.10}
        ) == ["codex", "gemini_cli", "claude"]

    def test_imbalanced_sorts_ascending(self) -> None:
        # Spread 0.40 > 0.10 → sort by pressure ascending.
        result = get_balanced_provider_order(
            {"claude": 0.90, "gemini_cli": 0.50, "codex": 0.10}
        )
        assert result == ["codex", "gemini_cli", "claude"]

    def test_imbalanced_claude_strained_demotes_claude(self) -> None:
        """Claude Code scenario: Claude near limit, others fresh.
        Claude must NOT be first."""
        result = get_balanced_provider_order(
            {"claude": 0.95, "gemini_cli": 0.05, "codex": 0.10}
        )
        assert result[0] != "claude"
        assert result[-1] == "claude"

    def test_imbalanced_codex_strained_demotes_codex(self) -> None:
        """Codex CLI scenario: codex strained."""
        result = get_balanced_provider_order(
            {"claude": 0.05, "gemini_cli": 0.10, "codex": 0.95}
        )
        assert result[0] != "codex"
        assert result[-1] == "codex"

    def test_imbalanced_gemini_strained_demotes_gemini(self) -> None:
        """Gemini CLI scenario: gemini_cli strained."""
        result = get_balanced_provider_order(
            {"claude": 0.05, "gemini_cli": 0.95, "codex": 0.10}
        )
        assert result[0] != "gemini_cli"
        assert result[-1] == "gemini_cli"

    def test_custom_tolerance(self) -> None:
        # spread = 0.05 with tolerance=0.01 → imbalanced.
        result = get_balanced_provider_order(
            {"claude": 0.15, "gemini_cli": 0.10, "codex": 0.20},
            tolerance=0.01,
        )
        assert result == ["gemini_cli", "claude", "codex"]


# ────────────────────────────────────────────────────────────────────────
# Section C — reorder_chain_by_providers unit tests
# ────────────────────────────────────────────────────────────────────────


class TestReorderChainByProviders:
    """``quota_balance.reorder_chain_by_providers`` segments the chain
    by provider prefix then concatenates per ``provider_order``.

    Invariants:

    * Ollama models **always** go first (local, free).
    * Subscription providers (claude / gemini_cli / codex) appear in
      the order given by ``provider_order``.
    * Other paid APIs (openai/groq/etc.) go last."""

    def test_ollama_always_first_regardless_of_order(self) -> None:
        chain = [
            "anthropic/sonnet",
            "codex/gpt-5",
            "ollama/llama3",
            "gemini_cli/pro",
        ]
        # No matter the provider_order, ollama gets the head slot.
        result = reorder_chain_by_providers(
            chain, ["claude", "gemini_cli", "codex"]
        )
        assert result[0] == "ollama/llama3"

    def test_subscriptions_follow_provider_order(self) -> None:
        chain = [
            "anthropic/sonnet",
            "codex/gpt-5",
            "gemini_cli/pro",
        ]
        result = reorder_chain_by_providers(
            chain, ["codex", "gemini_cli", "claude"]
        )
        assert result == [
            "codex/gpt-5",
            "gemini_cli/pro",
            "anthropic/sonnet",
        ]

    def test_other_paid_apis_at_end(self) -> None:
        chain = [
            "anthropic/sonnet",
            "openai/gpt-4o",
            "groq/llama",
        ]
        result = reorder_chain_by_providers(chain, ["claude"])
        assert result[-2:] == ["openai/gpt-4o", "groq/llama"]

    def test_empty_chain_returns_empty(self) -> None:
        assert reorder_chain_by_providers([], ["claude"]) == []

    def test_within_provider_order_preserved(self) -> None:
        chain = [
            "anthropic/sonnet",
            "anthropic/haiku",
            "anthropic/opus",
        ]
        result = reorder_chain_by_providers(chain, ["claude"])
        assert result == [
            "anthropic/sonnet",
            "anthropic/haiku",
            "anthropic/opus",
        ]


# ────────────────────────────────────────────────────────────────────────
# Section D — Integration via the QUOTA_BALANCED runtime path
# ────────────────────────────────────────────────────────────────────────


def _quota_balanced_reorder(
    chain: list[str], pressures: dict[str, float]
) -> list[str]:
    """The exact two-step call ``router.py:500-509`` performs for
    ``QUOTA_BALANCED``. Lifted here so the characterization matrix can
    exercise it directly without spinning up the full router."""
    order = get_balanced_provider_order(pressures)
    return reorder_chain_by_providers(chain, order)


class TestQuotaBalancedIntegration:
    """Composing both halves the runtime QUOTA_BALANCED reorder runs."""

    def test_claude_strained_demotes_anthropic_in_chain(self) -> None:
        chain = list(CLAUDE_CODE_HOST.representative_chain)
        result = _quota_balanced_reorder(
            chain,
            {"claude": 0.95, "gemini_cli": 0.10, "codex": 0.05},
        )
        # Head is ollama (always); main subscription anthropic must
        # not be in the next subscription slot.
        assert result[0].startswith("ollama/")
        # First non-ollama, non-other-paid model — the strained
        # provider must not be it.
        first_sub = next(
            m for m in result
            if not m.startswith("ollama/") and not m.startswith("openai/")
            and not m.startswith("groq/")
        )
        assert not first_sub.startswith("anthropic/")

    def test_codex_strained_demotes_codex_in_chain(self) -> None:
        chain = list(CODEX_CLI_HOST.representative_chain)
        result = _quota_balanced_reorder(
            chain,
            {"claude": 0.10, "gemini_cli": 0.05, "codex": 0.95},
        )
        assert result[0].startswith("ollama/")
        first_sub = next(
            m for m in result
            if not m.startswith("ollama/") and not m.startswith("openai/")
            and not m.startswith("groq/")
        )
        assert not first_sub.startswith("codex/")

    def test_gemini_strained_demotes_gemini_in_chain(self) -> None:
        chain = list(GEMINI_CLI_HOST.representative_chain)
        result = _quota_balanced_reorder(
            chain,
            {"claude": 0.10, "gemini_cli": 0.95, "codex": 0.05},
        )
        assert result[0].startswith("ollama/")
        first_sub = next(
            m for m in result
            if not m.startswith("ollama/") and not m.startswith("openai/")
            and not m.startswith("groq/")
        )
        assert not first_sub.startswith("gemini_cli/")


# ────────────────────────────────────────────────────────────────────────
# Section E — The 108-cell characterization matrix
# ────────────────────────────────────────────────────────────────────────


def _profile_reorder(
    chain: list[str],
    profile: str,
    *,
    live_pressures: dict[str, float],
    static_pressures: dict[str, float],
) -> list[str]:
    """Apply the demotion path the *given* profile actually triggers.

    * ``BALANCED`` / ``PREMIUM`` → mechanism A only (static cached
      pressure with 0.85 threshold). Does NOT consult live pressures.
    * ``QUOTA_BALANCED`` → mechanism B only (live spread-based
      reorder). Does NOT use the 0.85 threshold.

    The user's question ("when inside Claude Code, does Claude get
    demoted under high live pressure?") is answered by this fork."""
    if profile in ("BALANCED", "PREMIUM"):
        return _reorder_by_quota_pressure(chain, static_pressures)
    if profile == "QUOTA_BALANCED":
        return _quota_balanced_reorder(chain, live_pressures)
    raise ValueError(f"unknown profile: {profile!r}")


def _all_cells() -> Iterable[tuple[HostScenario, str, str, str]]:
    """3 hosts × 3 profiles × 4 pressures × 3 complexities = 108 cells."""
    for host in HOSTS:
        for profile in ("BALANCED", "PREMIUM", "QUOTA_BALANCED"):
            for pressure_label in PRESSURE_LEVELS:
                for complexity in COMPLEXITIES:
                    yield host, profile, pressure_label, complexity


@dataclass(frozen=True)
class CellResult:
    """One cell's outcome — sortable + printable for the Stage-3 verdict."""

    host: str
    profile: str
    pressure_label: str
    complexity: str
    success: bool           # non-empty chain
    main_sub_position: int | None
    chain_length: int
    chain_head: str

    @property
    def demoted(self) -> bool:
        """The main subscription is NOT at position 0."""
        return self.main_sub_position is not None and self.main_sub_position > 0


def _evaluate_cell(
    host: HostScenario,
    profile: str,
    pressure_label: str,
    complexity: str,
) -> CellResult:
    pressure_value = PRESSURE_LEVELS[pressure_label]
    # Live pressures: the main subscription is at the cell's pressure
    # level, the others stay fresh so the demotion target is unambiguous.
    live = {
        "claude": pressure_value if host.main_sub_key == "claude" else 0.05,
        "gemini_cli": pressure_value if host.main_sub_key == "gemini_cli" else 0.05,
        "codex": pressure_value if host.main_sub_key == "codex" else 0.05,
    }
    # Static pressures use the provider name space, not the
    # subscription-key space. So the static dict uses "anthropic" not
    # "claude", and "gemini_cli" not "gemini".
    static = {host.main_sub_provider: pressure_value}
    chain = list(host.representative_chain)

    result_chain = _profile_reorder(
        chain, profile,
        live_pressures=live, static_pressures=static,
    )
    return CellResult(
        host=host.name, profile=profile,
        pressure_label=pressure_label, complexity=complexity,
        success=len(result_chain) > 0,
        main_sub_position=_main_sub_position(result_chain, host.main_sub_key),
        chain_length=len(result_chain),
        chain_head=result_chain[0] if result_chain else "",
    )


# Persistent buffer the verdict test reads to print the Stage-3 summary.
_MATRIX_RESULTS: list[CellResult] = []


@pytest.mark.parametrize(
    "host,profile,pressure_label,complexity",
    list(_all_cells()),
    ids=lambda v: v.name if hasattr(v, "name") else str(v),
)
def test_matrix_cell(
    host: HostScenario,
    profile: str,
    pressure_label: str,
    complexity: str,
) -> None:
    """Every cell must (a) succeed (non-empty chain) and (b) record its
    main-subscription position for the Stage-3 verdict."""
    result = _evaluate_cell(host, profile, pressure_label, complexity)
    _MATRIX_RESULTS.append(result)
    # Stage-2 only asserts success-rate-preservation here; the
    # quota-exploitation analysis is done in Stage 3 with explicit
    # thresholds.
    assert result.success, (
        f"chain went empty for {host.name}/{profile}/"
        f"{pressure_label}/{complexity}"
    )
    # Sanity: the main subscription should be present in the chain
    # (we never *remove* providers in the reorder paths).
    assert result.main_sub_position is not None, (
        f"main subscription {host.main_sub_key!r} vanished from chain "
        f"under {host.name}/{profile}/{pressure_label}/{complexity}"
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 3 — Verdict against explicit pass/fail thresholds
# ────────────────────────────────────────────────────────────────────────


def _verdict_thresholds(cell: CellResult) -> tuple[bool, str]:
    """Apply the Stage-3 thresholds to one cell.

    The two rules — derived from the user's request:

    * **Under high pressure (label in {"high", "extreme"})** the main
      subscription provider MUST NOT be at position 0. Demotion must
      happen.
    * **Under low pressure (label in {"none", "medium"})** behaviour
      is descriptive — we just record where the subscription landed
      without failing.

    Returns ``(passes, reason)``."""
    if cell.pressure_label in ("high", "extreme"):
        if cell.main_sub_position == 0:
            return False, (
                f"main sub at head despite pressure={cell.pressure_label}"
            )
        return True, "demoted as required"
    return True, "low-pressure cell — descriptive only"


def test_verdict_against_thresholds() -> None:
    """Aggregate matrix into a pass/fail report.

    Prints a per-(host, profile, pressure_label) summary so the
    Stage-4 decision memo can quote it verbatim, then asserts the
    overall pass criteria.

    The assertion deliberately does NOT require every cell to pass
    — the whole point of Stage 3 is to surface *which* cells fail so
    Stage 4 can scope the remediation."""
    # If the matrix test didn't populate (e.g. selective `-k` run),
    # populate now so this can be run standalone.
    if not _MATRIX_RESULTS:
        for host, profile, pressure_label, complexity in _all_cells():
            _MATRIX_RESULTS.append(
                _evaluate_cell(host, profile, pressure_label, complexity)
            )

    rows = []
    for cell in _MATRIX_RESULTS:
        passes, reason = _verdict_thresholds(cell)
        rows.append((cell, passes, reason))

    # ── Stage-3 report (printed for the decision memo to quote) ────
    print()
    print("=" * 78)
    print("STAGE-3 CHARACTERIZATION VERDICT")
    print("=" * 78)
    print(
        f"{'HOST':<12} {'PROFILE':<16} {'PRESSURE':<8} "
        f"{'COMPLEX':<10} {'SUB@POS':<8} {'HEAD':<30} {'V':<2}"
    )
    print("-" * 78)
    for cell, passes, _ in rows:
        head = cell.chain_head[:28]
        verdict_mark = "✓" if passes else "✗"
        print(
            f"{cell.host:<12} {cell.profile:<16} "
            f"{cell.pressure_label:<8} {cell.complexity:<10} "
            f"{str(cell.main_sub_position):<8} {head:<30} {verdict_mark:<2}"
        )
    print("-" * 78)

    # Aggregate by (host, profile, pressure_label) — drop complexity
    # because the existing path is complexity-agnostic.
    by_cell: dict[tuple[str, str, str], list[bool]] = {}
    for cell, passes, _ in rows:
        key = (cell.host, cell.profile, cell.pressure_label)
        by_cell.setdefault(key, []).append(passes)
    summary_rows: list[tuple[str, str, str, str]] = []
    for (host, profile, pressure_label), passes_list in sorted(by_cell.items()):
        all_pass = all(passes_list)
        summary_rows.append((
            host, profile, pressure_label,
            "PASS" if all_pass else "FAIL",
        ))
    print()
    print("Aggregated (any complexity):")
    print(
        f"{'HOST':<12} {'PROFILE':<16} {'PRESSURE':<8} {'VERDICT':<6}"
    )
    for host, profile, pressure_label, verdict in summary_rows:
        print(f"{host:<12} {profile:<16} {pressure_label:<8} {verdict:<6}")

    # Success-rate-preservation criterion — MUST hold across all 108.
    all_success = all(cell.success for cell, _, _ in rows)
    assert all_success, "at least one cell produced an empty chain"

    # The verdict-test itself does NOT fail when a high-pressure cell
    # fails to demote — that's the *finding* Stage 4 uses to decide
    # whether to escalate. We surface the count instead.
    high_pressure_cells = [
        (cell, passes) for cell, passes, _ in rows
        if cell.pressure_label in ("high", "extreme")
    ]
    high_failures = [c for c, p in high_pressure_cells if not p]
    print()
    print(
        f"High-pressure cells: {len(high_pressure_cells)} total · "
        f"{len(high_failures)} failures (main sub still at head)"
    )
    if high_failures:
        print()
        print("Failing high-pressure cells:")
        for cell in high_failures:
            print(
                f"  - {cell.host}/{cell.profile}/{cell.pressure_label}/"
                f"{cell.complexity} → head={cell.chain_head}"
            )

    # The decision memo (Stage 4) reads ``high_failures`` to decide
    # whether option #1 is sufficient. Surface the data via an
    # explicit assertion so a reader can grep the value out of CI
    # output without running the test.
    #
    # Threshold: zero high-pressure failures = option #1 sufficient.
    # Any failure → escalate to option #2.
    print()
    print(
        "STAGE-4 INPUT: high_pressure_failures = "
        f"{len(high_failures)} (of {len(high_pressure_cells)})"
    )
    print("=" * 78)
