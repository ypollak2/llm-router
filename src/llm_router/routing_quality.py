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

Schema versioning: v2 rows carry ``schema_version=2``, v3 rows ``3``. Legacy v1 rows
(written by the deprecated :class:`RouteRecord` / :func:`record`) lack it and are read
with legacy semantics — they NEVER contribute to v2+ verification / mis-route / quality
metrics. Quality denominators test ``>= 2``, not ``== 2``: v3 changed no quality
semantics, and an equality test would empty every denominator on the next bump while
still reporting a clean-looking rate.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

CURRENT_SCHEMA_VERSION = 3

# ── v3: traceability ─────────────────────────────────────────────────────────
# v2 recorded what the router DID but not what it did it TO. Audited 2026-09-20:
# prompt text lived only in conversation transcripts, which carry no route_id,
# no task_type and no timestamp, so no historical route could be connected to
# the task that produced it. Ground truth was therefore underivable from 22,356
# records — not for want of volume, but for want of a join key.
#
# v3 adds that key as a HASH, never as text. `routing_quality.jsonl` keeps its
# existing guarantee that no prompt content is persisted here; the text, when
# captured at all, lives in the opt-in scrubbed store and is joined on
# `prompt_sha256`. Reading the ledger therefore reveals nothing it did not
# already reveal.
#
# The contract v3 is required to satisfy:
#   given route_id       -> exactly one record
#   given prompt_sha256  -> the captured task text, if capture was enabled
# with no reliance on timestamp proximity or any other heuristic.
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

ENV_SYNTHETIC = "LLM_ROUTER_SYNTHETIC"


def detect_synthetic() -> bool:
    """Is this process a test or benchmark run? Explicit signals only.

    * ``LLM_ROUTER_SYNTHETIC=1`` — what a harness sets deliberately.
    * ``PYTEST_CURRENT_TEST`` — pytest sets this itself for every test, so it
      is a statement by the test runner about its own run, not an inference
      drawn from the data afterwards.

    Nothing here looks at the model name, the session id or the token counts.
    Each of those has been tried and each has been wrong.
    """
    if os.environ.get(ENV_SYNTHETIC, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return "PYTEST_CURRENT_TEST" in os.environ


def is_evaluable(row: dict) -> bool:
    """May this ledger row feed Ground Truth or a published quality number?

    Three states, and the middle one is the point:

        synthetic=False   production. Usable.
        synthetic=True    test traffic. Excluded.
        field absent      UNKNOWN — written before provenance existed.

    Unknown is excluded, not admitted. Treating unknown as production is
    exactly how 29% test traffic ended up inside every historical figure, and
    the cost of wrongly excluding an old real row is a smaller sample, while
    the cost of wrongly including a fixture is a number that is quietly false.
    """
    if "synthetic" not in row:
        return False
    return not row.get("synthetic")


@dataclass
class RouteLedgerRecord:
    """Exactly one logical route's measured outcome. schema_version=3."""

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

    # ── v3 traceability ──────────────────────────────────────────────────────
    # Everything below exists so a route can be reconstructed as an evaluation
    # unit later. None of it is prompt or response CONTENT; see the v3 note at
    # the top of this module.

    # Groups the routes belonging to one conversation. Present in
    # auto-route-debug.log since v1 and absent here, which is why the two logs
    # could never be joined.
    session_id: str | None = None

    # THE JOIN KEY. sha256 of the exact prompt, via trace_id.hash_prompt().
    # Not reversible, and stable across restarts, so a replayed call lands on
    # the same key. Null when the caller did not supply the prompt.
    prompt_sha256: str | None = None

    # Same treatment for the model's output: enough to tell two responses
    # apart and to confirm a captured response is the one this route produced.
    response_sha256: str | None = None

    # Wall-clock for the route. time.monotonic() at the call site, not
    # time.time(): this machine's sleep advances the latter, and one benchmark
    # task was recorded at 918s of which 902s was the laptop asleep.
    latency_ms: float | None = None

    # Decision metadata. Both exist in model_tracking.jsonl, which has no
    # route_id, so they were unjoinable to the outcome they explain.
    complexity: str | None = None                # simple | moderate | complex
    classification_method: str | None = None     # heuristic | semantic | fast-path | …

    # HOW acceptability was decided, not just whether it passed.
    # verification_attempted/passed above say pass/fail; these say by what.
    # Kept deliberately parallel to the ground-truth record so a production
    # verification and an offline one are directly comparable.
    verification_type: str | None = None         # mechanical | sandbox | judge | human
    verifier_name: str | None = None             # e.g. "pytest", "words()", "judge:haiku"

    # Pointer into the capture store when prompt text was persisted for this
    # route. Null means no text was captured — the normal case, since capture
    # is opt-in. Never a file path outside the capture root.
    capture_ref: str | None = None

    # ── Provenance ───────────────────────────────────────────────────────────
    # True when this row was produced by a test, benchmark or harness rather
    # than by a person using the router. Set from EXPLICIT signals only — an
    # env var the harness sets, or pytest's own marker — never inferred from
    # the model name or a suspicious-looking session id.
    #
    # Why explicit: 29% of this ledger is test traffic carrying no marker, and
    # every attempt to identify it after the fact has been a guess that needed
    # revising. `final_model == "test/mock-model"` catches most of it and
    # misses a harness pointed at a real model; `session_id == "unknown"` was
    # 54% of one day's log for unrelated reasons. A flag the writer sets is the
    # only signal that cannot be wrong about what produced the row.
    #
    # Rows written before this field existed carry no value for it. They are
    # UNKNOWN provenance, not production — see `is_evaluable`.
    synthetic: bool = field(default_factory=lambda: detect_synthetic())


def _default_ledger() -> Path:
    return Path(os.environ.get("LLM_ROUTER_ROUTING_LEDGER",
                               str(Path.home() / ".llm-router" / "routing_quality.jsonl")))


def stamp_trace(
    rec: RouteLedgerRecord,
    *,
    prompt: str | None = None,
    response: str | None = None,
    session_id: str | None = None,
    latency_ms: float | None = None,
    complexity: str | None = None,
    classification_method: str | None = None,
    capture_ref: str | None = None,
) -> RouteLedgerRecord:
    """Fill the v3 traceability fields on *rec*, hashing content rather than storing it.

    This is the only supported way to populate ``prompt_sha256`` /
    ``response_sha256``, so there is one hash convention rather than the three
    that already exist in this tree (``trace_id.hash_prompt``,
    ``result_cache._prompt_hash``, ``semantic`` scope keys). It delegates to
    :func:`llm_router.trace_id.hash_prompt` — added for audit G-025 and, until
    now, wired to nothing.

    Note the difference from ``result_cache._prompt_hash``, which lowercases and
    strips before hashing. That is correct for a cache, where near-identical
    prompts *should* collide. It is wrong here: an evaluation unit is the exact
    task the user sent, so the hash is taken over the exact bytes.

    Mutates and returns *rec* for chaining. Never raises.
    """
    try:
        from llm_router.trace_id import hash_prompt  # local: keeps module dep-free
    except Exception:  # noqa: BLE001 — traceability must not break routing
        hash_prompt = None  # type: ignore[assignment]

    if prompt is not None and hash_prompt is not None:
        rec.prompt_sha256 = hash_prompt(prompt)
    if response is not None and hash_prompt is not None:
        rec.response_sha256 = hash_prompt(response)
    if session_id is not None:
        rec.session_id = session_id
    if latency_ms is not None:
        rec.latency_ms = round(float(latency_ms), 3)
    if complexity is not None:
        rec.complexity = complexity
    if classification_method is not None:
        rec.classification_method = classification_method
    if capture_ref is not None:
        rec.capture_ref = capture_ref
    return rec


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
    # `>= 2`, never `== 2`. v3 adds traceability fields and changes no quality
    # semantics, so a v3 row belongs in exactly the denominators a v2 row does.
    # An equality test here would have silently emptied every quality
    # denominator the moment the schema was bumped, and the rate would have
    # read as a clean 0% rather than as a missing measurement.
    v2 = [r for r in rows
          if r.get("schema_version", 1) >= 2 and r.get("parent_route_id") is None]
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
