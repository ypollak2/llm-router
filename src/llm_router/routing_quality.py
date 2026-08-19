"""North Star measurement: a fail-open per-route quality ledger (schema v2).

The North Star is "route to the cheapest capable model, escalate on failure" — and
it must be MEASURED, not assumed. Every routed execution appends a
:class:`RouteLedgerRecord` to ``~/.llm-router/routing_quality.jsonl``; :func:`summarize`
reads it back into HONEST split metrics that never conflate:

  * telemetry recording   (a row exists)          vs
  * route success         (``route_succeeded``)    vs
  * verified quality      (``verification_passed``) vs
  * technical fallback    (``fallback_reason`` ∈ infra set, ``mis_route=None``) vs
  * quality escalation    (``quality_escalation_occurred``, ``mis_route=True``).

A completion route that ran no tools records ``tool_execution_succeeded=None`` — NOT
``True`` — and ``verification_passed=None``: unverified is honestly unverified.

Recording is FAIL-OPEN: a ledger write must never raise into the routing path. If the
ledger can't be written, the route still proceeds — we lose a metric, not a turn.

Schema versioning: v2 rows carry ``schema_version=2``. Legacy v1 rows (written by the
deprecated :class:`RouteRecord` / :func:`record`) lack it and are read with legacy
semantics — they NEVER contribute to v2 verification / mis-route / quality metrics.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

CURRENT_SCHEMA_VERSION = 2
BASELINE_POLICY_VERSION = "north-star-v1"

FallbackReason = Literal[
    "provider_failure",
    "timeout",
    "rate_limit",
    "health_skip",
    "policy_rejection",
    "budget_exhausted",
    "cost_cap",
    "capability_failure",
    "verification_failure",
    "quality_failure",
]

RouteKind = Literal[
    "completion",
    "delegate",
    "bounded_operational",
    "delegate_substep",
]

# Fallback reasons that imply the FIRST-choice tier was actually wrong (a quality/
# capability failure, not infrastructure). Only these permit ``mis_route=True``.
_QUALITY_REASONS: frozenset[str] = frozenset(
    {"capability_failure", "verification_failure", "quality_failure"}
)


@dataclass
class RouteLedgerRecord:
    """Exactly one logical route's measured outcome. schema_version=2."""

    # --- Identity ---
    schema_version: int = CURRENT_SCHEMA_VERSION
    route_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_route_id: str | None = None          # set for delegate_substep only
    route_kind: RouteKind = "completion"
    task_type: str = "unknown"

    # --- Tier and model selection ---
    chosen_tier: int | str | None = None        # tier FIRST attempted
    final_tier: int | str | None = None         # tier that ultimately succeeded
    chosen_model: str | None = None
    final_model: str | None = None

    # --- Route outcome ---
    route_succeeded: bool = False               # model returned a usable response

    # --- Tool execution (null = not applicable, e.g. completion route) ---
    tool_execution_attempted: bool = False
    tool_execution_succeeded: bool | None = None  # null iff not attempted

    # --- Objective verification (null = not attempted) ---
    verification_attempted: bool = False
    verification_passed: bool | None = None     # null iff not attempted

    # --- Fallback and escalation ---
    fallback_occurred: bool = False
    fallback_reason: FallbackReason | None = None  # null iff fallback_occurred=False

    # Quality-driven escalation: cheap tier produced an answer that FAILED an
    # objective check. NOT set for technical fallbacks (timeout, rate_limit, …).
    quality_escalation_occurred: bool = False
    quality_escalation_reason: str | None = None

    # mis_route: initial routing decision was wrong.
    #   True  = inferred from capability/verification/quality failure of first tier.
    #   None  = UNKNOWN (technical fallback or unverified completion — can't know).
    #   False = route was correct AND verified (first tier cleared its check).
    mis_route: bool | None = None

    # weak_pass: passed the objective check but ONLY on the weakest (tier-0/local)
    # agent. Surfaced for review; not a cause for re-run.
    weak_pass: bool | None = None

    # --- Cost ---
    actual_cost_usd: float = 0.0
    baseline_cost_usd: float = 0.0              # see §4.3 baseline policy
    saved_usd: float = 0.0
    failed_attempt_cost_usd: float = 0.0        # cost of failed-fallback attempts only

    # Token breakdown (null = not recorded by model)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    tool_cost_usd: float | None = None          # null = not separately metered

    # Pricing versioning — required for reproducibility
    baseline_policy_version: str = BASELINE_POLICY_VERSION
    price_table_version: str = "unknown"

    # --- Diagnostics ---
    chain_attempts: list[str] = field(default_factory=list)
    chain_errors: list[dict[str, str]] = field(default_factory=list)  # [{model, reason}]

    ts: float = 0.0                              # unix time; stamped on write if 0


def _default_ledger() -> Path:
    return Path(os.environ.get("LLM_ROUTER_ROUTING_LEDGER",
                               str(Path.home() / ".llm-router" / "routing_quality.jsonl")))


def record_route(rec: RouteLedgerRecord, path: str | None = None) -> bool:
    """Append *rec* to the ledger. FAIL-OPEN: returns False on any error, never raises."""
    try:
        if not rec.ts:
            rec.ts = time.time()
        p = Path(path) if path else _default_ledger()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec)) + "\n")
        return True
    except Exception:  # noqa: BLE001 — a ledger failure must never break routing
        return False


def load_records(path: str | None = None) -> list[dict[str, Any]]:
    """Load all ledger rows, normalizing ``schema_version``.

    - rows missing ``schema_version`` are treated as legacy v1 (``schema_version=1``)
    - malformed JSON lines are tagged ``{"_invalid": True}`` and excluded from all
      quality denominators by :func:`summarize` — never crash the reader.
    """
    try:
        p = Path(path) if path else _default_ledger()
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001 — missing/unreadable ledger reads as empty
        return []
    rows: list[dict[str, Any]] = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
            if not isinstance(row, dict):
                rows.append({"_invalid": True})
                continue
            row.setdefault("schema_version", 1)  # legacy rows lack it
            rows.append(row)
        except Exception:  # noqa: BLE001 — one bad line must not sink the read
            rows.append({"_invalid": True})
    return rows


def _classify_reason(reason: str) -> tuple[FallbackReason, bool | None]:
    """Map one raw ``chain_errors`` reason string to (FallbackReason, mis_route).

    Quality/verification/capability failures set ``mis_route=True`` (the first-choice
    tier was genuinely inadequate). Every technical/infra reason sets ``mis_route=None``
    — a timeout or rate-limit tells us NOTHING about whether the route was correct.
    """
    r = reason.lower()
    # Quality / verification / capability — these are the ONLY mis_route=True cases.
    if "gate_failed" in r or "verification" in r:
        return "verification_failure", True
    if "low_quality" in r or "quality" in r:
        return "quality_failure", True
    if "capability" in r:
        return "capability_failure", True
    # Technical / infra — mis_route stays UNKNOWN (None).
    if "budget" in r:
        return "budget_exhausted", None
    # Policy BEFORE the generic cost check: "policy:turn_cost:<n>" contains "cost"
    # but is a turn-cost POLICY rejection, not a cost-cap skip.
    if "policy" in r or "turn_cost" in r:
        return "policy_rejection", None
    if "premium_capped" in r or "cost_skipped" in r or "projected" in r or "cost" in r:
        return "cost_cap", None
    if "unhealthy" in r or "health" in r:
        return "health_skip", None
    if "ratelimit" in r or "rate_limit" in r or "rate limit" in r:
        return "rate_limit", None
    if "timeout" in r or "deadline" in r:
        return "timeout", None
    return "provider_failure", None


def derive_fallback_reason(
    chain_errors: list[tuple[str, str]],
) -> tuple[FallbackReason | None, bool | None]:
    """Return ``(fallback_reason, mis_route)`` for a route's fallback trail.

    Empty trail → ``(None, None)``: no fallback, and (for an unverified completion
    route) mis_route is unknown, never falsely ``False``.

    A quality/verification/capability failure ANYWHERE in the trail dominates: the
    first-choice tier failed on quality, so ``mis_route=True`` and that reason is
    reported. Otherwise the LAST (most recent) technical reason is reported with
    ``mis_route=None`` — a technical fallback never implies a wrong route.
    """
    if not chain_errors:
        return None, None
    classified = [_classify_reason(reason) for _model, reason in chain_errors]
    for fb_reason, mis in classified:
        if mis is True:
            return fb_reason, True
    return classified[-1][0], None


def summarize(path: str | None = None) -> dict[str, Any]:
    """Read the ledger into HONEST, non-conflating routing-quality metrics.

    Denominators are explicit: verification rates are computed ONLY over routes where
    verification was attempted; mis-route rate ONLY over rows where it is inferred
    (not None); legacy v1 rows never enter any v2 quality denominator.
    """
    rows = load_records(path)
    v2 = [r for r in rows
          if r.get("schema_version", 1) == 2 and r.get("parent_route_id") is None]
    legacy = [r for r in rows if not r.get("_invalid") and r.get("schema_version", 1) == 1]
    invalid = [r for r in rows if r.get("_invalid")]

    def rate(subset: list[dict], key: str, value: Any = True) -> float | None:
        if not subset:
            return None
        return sum(1 for r in subset if r.get(key) == value) / len(subset)

    verified = [r for r in v2 if r.get("verification_attempted") is True]
    attempted_tools = [r for r in v2 if r.get("tool_execution_attempted") is True]
    completions = [r for r in v2 if r.get("route_kind") == "completion"]
    inferred_mis = [r for r in v2 if r.get("mis_route") is not None]

    _TECH = {"provider_failure", "timeout", "rate_limit", "health_skip",
             "policy_rejection", "budget_exhausted", "cost_cap"}

    cost_by_kind: dict[str, float] = {}
    for kind in ("completion", "delegate", "bounded_operational"):
        kind_rows = [r for r in v2 if r.get("route_kind") == kind]
        cost_by_kind[kind] = round(sum(float(r.get("saved_usd", 0.0)) for r in kind_rows), 4)

    price_versions = {r.get("price_table_version", "unknown") for r in v2}

    return {
        "total_rows": len(rows),
        "schema_v2_rows": len(v2),
        "legacy_rows": len(legacy),
        "invalid_rows": len(invalid),

        # Proxy: v2 / (v2 + legacy). WRONG on a fresh ledger (100% on 0 real routes);
        # true coverage needs an external count of total route attempts. Documented gap.
        "ledger_coverage_rate": len(v2) / max(1, len(v2) + len(legacy)),

        "verified_route_rate": rate(v2, "verification_attempted", True),
        "unverified_route_rate": rate(v2, "verification_attempted", False),

        "technical_fallback_rate": (
            sum(1 for r in v2 if r.get("fallback_occurred")
                and r.get("fallback_reason") in _TECH) / max(1, len(v2))
        ),
        "quality_escalation_rate": rate(v2, "quality_escalation_occurred", True),

        # Pass rate ONLY over routes where verification was actually attempted.
        "verification_pass_rate": (
            sum(1 for r in verified if r.get("verification_passed") is True)
            / max(1, len(verified))
        ) if verified else None,
        "tool_execution_success_rate": (
            sum(1 for r in attempted_tools if r.get("tool_execution_succeeded") is True)
            / max(1, len(attempted_tools))
        ) if attempted_tools else None,

        "cost_savings_by_route_kind": cost_by_kind,
        "total_saved_usd": round(sum(float(r.get("saved_usd", 0.0)) for r in v2), 4),

        # Fraction of completion routes with NO objective verification (the honest
        # "we don't know if these were good" signal).
        "unknown_quality_completion_rate": (
            sum(1 for r in completions if not r.get("verification_attempted"))
            / max(1, len(completions))
        ) if completions else None,

        # mis_route only over rows where it is inferred (not None).
        "mis_route_rate_inferred": (
            sum(1 for r in inferred_mis if r.get("mis_route") is True)
            / max(1, len(inferred_mis))
        ) if inferred_mis else None,

        # Reproducibility guard: warn when rows span multiple price-table versions.
        "price_table_versions": sorted(price_versions),
        "price_table_version_mixed": len(price_versions) > 1,
    }


# ── Delegate path (aggregate-delegation-only): emit ONE v2 row per delegation ──

def record_delegation(result: dict[str, Any], path: str | None = None,
                      route_kind: RouteKind = "delegate") -> bool:
    """Build a v2 :class:`RouteLedgerRecord` from an MGEE delegation result and record it.

    Aggregate-delegation-only: this is the single parent row for the whole operation
    (``route_kind`` is ``delegate`` or ``bounded_operational``). The MGEE engine's
    internal ``route_and_call`` invocations are emitted with ``suppress_ledger=True`` so
    they never double-count here.

    Escalation is quality-driven: a milestone cleared by a tier above the cheapest
    attempted means the initial routing under-shot on QUALITY (mis_route=True), which
    is distinct from a technical fallback.
    """
    try:
        tiers = [m.get("achieved_by") for m in (result.get("milestones") or [])
                 if m.get("achieved_by") is not None]
        cheapest = min(tiers) if tiers else None
        final_tier = max(tiers) if tiers else None
        escalated = bool(tiers) and any(t > cheapest for t in tiers)
        completed = result.get("outcome") == "complete"
        succeeded = result.get("outcome") in ("complete", "surfaced")
        weak_pass = completed and bool(tiers) and max(tiers) == 0
        savings = result.get("savings") or {}
        actual = float(savings.get("actual_usd", 0.0) or 0.0)
        baseline = float(savings.get("baseline_usd", 0.0) or 0.0)
        rec = RouteLedgerRecord(
            route_kind=route_kind,
            task_type=route_kind,
            chosen_tier=cheapest,
            final_tier=final_tier,
            route_succeeded=succeeded,
            tool_execution_attempted=True,
            tool_execution_succeeded=succeeded,
            verification_attempted=True,          # MGEE runs objective acceptance checks
            verification_passed=completed,
            fallback_occurred=escalated,
            fallback_reason="verification_failure" if escalated else None,
            quality_escalation_occurred=escalated,
            quality_escalation_reason="milestone escalated to a stronger tier"
            if escalated else None,
            mis_route=True if escalated else (False if completed else None),
            weak_pass=weak_pass,
            actual_cost_usd=actual,
            baseline_cost_usd=baseline,
            saved_usd=float(savings.get("saved_usd", 0.0) or 0.0),
        )
        return record_route(rec, path=path)
    except Exception:  # noqa: BLE001 — never break the delegation path
        return False


# ── Deprecated v1 API (kept for backward compat; writes legacy rows) ──────────

@dataclass
class RouteRecord:
    """DEPRECATED (schema v1). Retained only for backward compatibility; new code
    must use :class:`RouteLedgerRecord` + :func:`record_route`. Rows written via
    :func:`record` lack ``schema_version`` and are read with legacy semantics — they
    never contribute to v2 quality metrics."""
    task_type: str
    chosen_tier: int | str
    needed_escalation: bool
    completion: bool
    tool_success: bool
    actual_cost: float = 0.0
    baseline_cost: float = 0.0
    saved: float = 0.0
    mis_route: bool = False
    weak_pass: bool = False
    ts: float = 0.0


def record(rec: RouteRecord, path: str | None = None) -> bool:
    """DEPRECATED: append a legacy v1 record. Prefer :func:`record_route`.

    FAIL-OPEN: returns False on any error, never raises."""
    try:
        if not rec.ts:
            rec.ts = time.time()
        p = Path(path) if path else _default_ledger()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec)) + "\n")
        return True
    except Exception:  # noqa: BLE001 — a ledger failure must never break routing
        return False
