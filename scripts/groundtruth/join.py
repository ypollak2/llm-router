"""Reconstruct an evaluation unit from a route_id. No heuristics.

The contract, and the whole reason the v3 schema exists:

    given route_id       -> exactly one RouteLedgerRecord
    given prompt_sha256  -> the captured task text, if capture was on

Both are exact-key lookups. Nothing here uses timestamp proximity, ordering,
session adjacency or any other guess. If the key is absent the unit is
`incomplete` and says which half is missing — an unjoinable route is reported,
never inferred.

That restraint is the point. Before v3 the only way to pair a prompt with its
routing decision was to line up two logs by time and hope, and the previous
audit showed why that fails: the two stores are written by different processes,
one of them has no timestamp at all, and roughly a third of the rows were
benchmark traffic that would have matched something.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

def llm_router_home() -> Path:
    """Router state dir, resolved on every call.

    M-04: this was a module-level constant reading the environment once, at
    import. That honoured LLM_ROUTER_HOME only if it was already set when the
    module first loaded, so a caller that set it afterwards — every test, and
    any process switching profiles — silently read the operator's real state.
    """
    return Path(os.environ.get("LLM_ROUTER_HOME", "").strip() or Path.home() / ".llm-router")

INCOMPLETE_SYNTHETIC = "synthetic-or-unknown-provenance"
INCOMPLETE_NO_CAPTURE = "no-captured-prompt"
INCOMPLETE_NO_HASH = "route-has-no-prompt-sha256"
INCOMPLETE_LEGACY = "pre-v3-route"


@dataclass
class EvaluationUnit:
    """One route, reassembled: the decision plus the task that caused it."""

    route_id: str
    prompt_sha256: str | None = None
    prompt: str | None = None            # scrubbed text, when captured
    session_id: str | None = None
    task_type: str | None = None
    complexity: str | None = None
    classification_method: str | None = None
    chosen_tier: int | str | None = None
    final_tier: int | str | None = None
    chosen_model: str | None = None
    final_model: str | None = None
    route_succeeded: bool | None = None
    verification_attempted: bool | None = None
    verification_passed: bool | None = None
    verification_type: str | None = None
    verifier_name: str | None = None
    latency_ms: float | None = None
    actual_cost_usd: float | None = None
    baseline_cost_usd: float | None = None
    schema_version: int = 1
    ts: float | None = None
    incomplete_reasons: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True when this unit can be used as an evaluation task.

        Requires the prompt text. A route with a hash but no captured text is
        traceable but not evaluable, and the distinction matters: the first is
        a capture-was-off problem, the second would be a bug.
        """
        return not self.incomplete_reasons and bool(self.prompt)


def _iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                yield obj


def load_capture_index(path: Path | None = None) -> dict[str, dict]:
    """prompt_sha256 -> captured record. Last write wins for a repeated hash.

    A repeated hash is not a conflict: the same prompt asked twice is the same
    task, which is exactly the property that makes the hash a usable key.
    """
    p = path or (llm_router_home() / "prompt_capture.jsonl")
    index: dict[str, dict] = {}
    for rec in _iter_jsonl(p):
        sha = rec.get("prompt_sha256")
        if sha:
            index[sha] = rec
    return index


def load_routes(path: Path | None = None) -> dict[str, dict]:
    """route_id -> ledger record. Later rows win, matching ledger replay order."""
    p = path or (llm_router_home() / "routing_quality.jsonl")
    out: dict[str, dict] = {}
    for rec in _iter_jsonl(p):
        rid = rec.get("route_id")
        if rid:
            out[rid] = rec
    return out


def join_route(route: dict, capture_index: dict[str, dict]) -> EvaluationUnit:
    """Build one EvaluationUnit from a ledger row plus the capture index."""
    unit = EvaluationUnit(
        route_id=route.get("route_id", ""),
        prompt_sha256=route.get("prompt_sha256"),
        session_id=route.get("session_id"),
        task_type=route.get("task_type"),
        complexity=route.get("complexity"),
        classification_method=route.get("classification_method"),
        chosen_tier=route.get("chosen_tier"),
        final_tier=route.get("final_tier"),
        chosen_model=route.get("chosen_model"),
        final_model=route.get("final_model"),
        route_succeeded=route.get("route_succeeded"),
        verification_attempted=route.get("verification_attempted"),
        verification_passed=route.get("verification_passed"),
        verification_type=route.get("verification_type"),
        verifier_name=route.get("verifier_name"),
        latency_ms=route.get("latency_ms"),
        actual_cost_usd=route.get("actual_cost_usd"),
        baseline_cost_usd=route.get("baseline_cost_usd"),
        schema_version=int(route.get("schema_version", 1) or 1),
        ts=route.get("ts"),
    )

    # Provenance first. A row that cannot prove it came from real usage is not
    # an evaluation unit, however complete the rest of it looks — and a row
    # predating the `synthetic` field cannot prove it.
    try:
        from llm_router.routing_quality import is_evaluable
    except Exception:  # noqa: BLE001 — fail CLOSED: unverifiable provenance is excluded
        def is_evaluable(row: dict) -> bool:  # type: ignore[misc]
            return "synthetic" in row and not row.get("synthetic")
    if not is_evaluable(route):
        unit.incomplete_reasons.append(INCOMPLETE_SYNTHETIC)
        return unit

    if unit.schema_version < 3:
        unit.incomplete_reasons.append(INCOMPLETE_LEGACY)
        return unit
    if not unit.prompt_sha256:
        unit.incomplete_reasons.append(INCOMPLETE_NO_HASH)
        return unit

    cap = capture_index.get(unit.prompt_sha256)
    if not cap:
        unit.incomplete_reasons.append(INCOMPLETE_NO_CAPTURE)
        return unit

    unit.prompt = cap.get("prompt")
    # The capture store also holds decision metadata. Prefer the ledger's copy:
    # it is written by the router itself, so it is authoritative. Fill only what
    # the ledger left empty.
    for attr in ("task_type", "complexity", "classification_method", "session_id"):
        if getattr(unit, attr) in (None, "", "unknown") and cap.get(attr):
            setattr(unit, attr, cap[attr])
    return unit


def reconstruct(route_id: str, *, ledger: Path | None = None,
                capture: Path | None = None) -> EvaluationUnit | None:
    """The headline operation: route_id -> evaluation unit, or None if unknown."""
    routes = load_routes(ledger)
    route = routes.get(route_id)
    if route is None:
        return None
    return join_route(route, load_capture_index(capture))


def reconstruct_all(*, ledger: Path | None = None,
                    capture: Path | None = None) -> list[EvaluationUnit]:
    index = load_capture_index(capture)
    return [join_route(r, index) for r in load_routes(ledger).values()]


def coverage(units: list[EvaluationUnit]) -> dict[str, int]:
    """How much of the ledger is actually reconstructable, and why not.

    Print this before quoting any dataset size. The denominator is every route;
    a dataset built from `complete` alone will otherwise look representative
    while silently describing whatever fraction had capture switched on.
    """
    out: dict[str, int] = {"routes": len(units), "complete": 0}
    for u in units:
        if u.is_complete:
            out["complete"] += 1
        for reason in (u.incomplete_reasons or ["prompt-text-missing"]):
            if not u.is_complete:
                out[reason] = out.get(reason, 0) + 1
    return out
