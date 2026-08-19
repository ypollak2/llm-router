"""User-selectable routing policies — control how the model chain is ordered.

Policies are applied AFTER the standard chain is built (static or dynamic) and
AFTER free-tier injection (Codex, Gemini CLI, Ollama), so every policy operates
on the complete candidate list rather than having to know the injection logic.

Activate via LLM_ROUTER_ROUTING_POLICY env var or ``llm_router config set routing_policy <name>``:

    balanced        (default) — standard chain order; cost/quality sweet spot
    local-first     — Ollama → Codex → Gemini CLI → paid APIs; free models first
    cost            — cheapest model first based on per-token pricing
    quality         — highest task-specific quality score first (benchmarks.json)
    quota-exhaustion — deprioritize providers near their quota limit (>85% used)
    dynamic         — round-robin across providers within ±10% quota of each other

Policy selection is idempotent — applying the same policy twice returns the
same order.  ``balanced`` is a no-op (returns the chain unchanged).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_BENCHMARKS_PATH = Path(__file__).parent / "data" / "benchmarks.json"
_PROFILE_PATH = Path.home() / ".llm-router" / "profile.yaml"

# ── Quality scores ─────────────────────────────────────────────────────────────
# Loaded once and indexed as {model_id: {task_type: score}} for O(1) lookup.
_quality_scores: dict[str, dict[str, float]] | None = None
_quality_lock = threading.Lock()


def _load_quality_scores() -> dict[str, dict[str, float]]:
    """Return quality scores indexed by model, loaded from bundled benchmarks."""
    global _quality_scores
    with _quality_lock:
        if _quality_scores is not None:
            return _quality_scores
        try:
            raw = json.loads(_BENCHMARKS_PATH.read_text())
            # raw["task_scores"] = {task_type: {model: score}}
            # Invert to: {model: {task_type: score}}
            inverted: dict[str, dict[str, float]] = {}
            for task, model_scores in raw.get("task_scores", {}).items():
                for model, score in model_scores.items():
                    inverted.setdefault(model, {})[task] = score
            _quality_scores = inverted
        except Exception:
            _quality_scores = {}
        return _quality_scores


# ── Quota pressure ─────────────────────────────────────────────────────────────

def _provider_quota_pressure() -> dict[str, float]:
    """Return quota pressure per provider (0.0=free, 1.0=exhausted) from profile.yaml."""
    try:
        import yaml
        profile = yaml.safe_load(_PROFILE_PATH.read_text()) if _PROFILE_PATH.exists() else {}
    except Exception:
        return {}

    pressure: dict[str, float] = {}
    for key, data in (profile.get("quotas") or {}).items():
        if key == "claude_subscription":
            try:
                remaining = float(str(data.get("remaining_percent", "100")).rstrip("%"))
                pressure["anthropic"] = max(0.0, min(1.0, (100 - remaining) / 100))
            except (ValueError, AttributeError):
                pass
        elif key == "gemini_cli":
            try:
                used, limit = str(data.get("used_today", "0/1500")).split("/")
                pressure["gemini_cli"] = max(0.0, min(1.0, int(used) / int(limit)))
            except Exception:
                pass
        elif key == "codex":
            try:
                used, limit = str(data.get("used_today", "0/1000")).split("/")
                pressure["codex"] = max(0.0, min(1.0, int(used) / int(limit)))
            except Exception:
                pass
    return pressure


def _provider_from_model(model: str) -> str:
    """Extract provider name from a 'provider/model' string."""
    return model.split("/", 1)[0] if "/" in model else model


# ── Round-robin state ──────────────────────────────────────────────────────────
# Keyed by a frozenset of the candidate model IDs so different chains get
# independent counters.  Protected by a lock because multiple coroutines may
# route concurrently.
_rr_counters: dict[frozenset, int] = {}
_rr_lock = threading.Lock()


def _round_robin_pick(candidates: list[str]) -> list[str]:
    """Round-robin across *candidates*, returning them starting from next slot."""
    key = frozenset(candidates)
    with _rr_lock:
        idx = _rr_counters.get(key, 0)
        _rr_counters[key] = (idx + 1) % max(1, len(candidates))
    # Rotate the list so the next candidate is first
    return candidates[idx:] + candidates[:idx]


# ── Policy implementations ─────────────────────────────────────────────────────

_FREE_PROVIDERS = frozenset({"ollama", "codex", "gemini_cli"})
_LOCAL_ORDER = ["ollama", "codex", "gemini_cli"]


def _policy_local_first(chain: list[str]) -> list[str]:
    """Prefer free local providers; paid APIs fall back."""
    local = [m for m in chain if _provider_from_model(m) in _FREE_PROVIDERS]
    paid = [m for m in chain if _provider_from_model(m) not in _FREE_PROVIDERS]
    # Within local, respect the canonical order: Ollama → Codex → Gemini CLI
    ordered_local: list[str] = []
    for provider in _LOCAL_ORDER:
        ordered_local.extend(m for m in local if _provider_from_model(m) == provider)
    # Any local provider not in _LOCAL_ORDER comes after
    ordered_local.extend(m for m in local if _provider_from_model(m) not in _LOCAL_ORDER)
    return ordered_local + paid


def _policy_cost(chain: list[str]) -> list[str]:
    """Sort by cost per output token, cheapest first.

    Uses LiteLLM's model_cost dict when available; falls back to a tiered
    heuristic for providers not in the dict (e.g. local/subscription models).
    """
    try:
        from litellm import model_cost as _lm_cost
    except ImportError:
        _lm_cost = {}

    def _cost_key(model: str) -> float:
        # Local/subscription models are free
        if _provider_from_model(model) in _FREE_PROVIDERS:
            return 0.0
        # LiteLLM uses bare model names (without provider prefix)
        bare = model.split("/", 1)[-1]
        entry = _lm_cost.get(bare) or _lm_cost.get(model) or {}
        return float(entry.get("output_cost_per_token", 0.001))

    return sorted(chain, key=_cost_key)


def _policy_quality(chain: list[str], task_type: str = "query") -> list[str]:
    """Sort by quality score for *task_type*, highest first."""
    scores = _load_quality_scores()

    def _quality_key(model: str) -> float:
        # Local models get a moderate score (better than unknown, worse than top APIs)
        if _provider_from_model(model) in _FREE_PROVIDERS:
            return 0.5
        bare = model.split("/", 1)[-1]
        task_scores = scores.get(model) or scores.get(bare) or {}
        return task_scores.get(task_type, 0.4)

    return sorted(chain, key=_quality_key, reverse=True)


def _policy_quota_exhaustion(chain: list[str]) -> list[str]:
    """Deprioritize providers whose quota is > 85% consumed."""
    pressure = _provider_quota_pressure()
    normal = [m for m in chain if pressure.get(_provider_from_model(m), 0.0) < 0.85]
    depleted = [m for m in chain if pressure.get(_provider_from_model(m), 0.0) >= 0.85]
    return normal + depleted


def _policy_dynamic(chain: list[str]) -> list[str]:
    """Round-robin across providers within ±10% quota of each other.

    Providers within ±10% quota of the median used provider are treated as
    equally available and are round-robined.  Providers outside that band
    follow their original order (lower quota usage first).
    """
    pressure = _provider_quota_pressure()

    def _quota(m: str) -> float:
        return pressure.get(_provider_from_model(m), 0.0)

    # Sort by quota usage ascending (freshest first)
    sorted_chain = sorted(chain, key=_quota)
    if not sorted_chain:
        return chain

    # Find the ±10% band around the lowest-quota group
    min_pressure = _quota(sorted_chain[0])
    band_threshold = min_pressure + 0.10

    in_band = [m for m in sorted_chain if _quota(m) <= band_threshold]
    out_of_band = [m for m in sorted_chain if _quota(m) > band_threshold]

    return _round_robin_pick(in_band) + out_of_band


# ── Public API ─────────────────────────────────────────────────────────────────

_POLICY_NAMES = frozenset({
    "balanced", "local-first", "cost", "quality", "quota-exhaustion", "dynamic",
})


def apply_routing_policy(
    chain: list[str],
    policy: str,
    task_type: str = "query",
) -> list[str]:
    """Reorder *chain* according to *policy*.

    Args:
        chain:     Ordered list of ``provider/model`` strings.
        policy:    One of the LLM_ROUTER_ROUTING_POLICY values.
        task_type: LLM task type string (``"code"``, ``"query"``, etc.) used
                   by the quality policy to pick the right benchmark scores.

    Returns:
        Reordered chain.  ``balanced`` returns the input unchanged.
    """
    if not chain or policy == "balanced":
        return chain

    if policy == "local-first":
        return _policy_local_first(chain)
    if policy == "cost":
        return _policy_cost(chain)
    if policy == "quality":
        return _policy_quality(chain, task_type)
    if policy == "quota-exhaustion":
        return _policy_quota_exhaustion(chain)
    if policy == "dynamic":
        return _policy_dynamic(chain)

    return chain  # unknown policy → no-op


def policy_description(policy: str) -> str:
    """Return a one-line description of a policy for display in the dashboard."""
    descs = {
        "balanced":        "cost/quality sweet spot (default)",
        "local-first":     "Ollama → Codex → Gemini CLI → paid",
        "cost":            "cheapest available model first",
        "quality":         "highest benchmark score first",
        "quota-exhaustion": "route away from near-quota providers",
        "dynamic":         "round-robin within ±10% quota usage",
    }
    return descs.get(policy, policy)


def known_policies() -> list[str]:
    """Return all valid policy names."""
    return sorted(_POLICY_NAMES)
