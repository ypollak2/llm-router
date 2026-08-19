"""Model registry — benchmark-derived metadata for every routable model.

Sourced primarily from https://artificialanalysis.ai/leaderboards/models
which publishes quality scores, prices, latency p50, and capabilities
across all major LLMs. We ship a static snapshot under
``config/models.yaml`` so the registry works offline.

.. warning::
   This docstring used to end "it's refreshed periodically via
   ``scripts/refresh-model-registry.py``". **That script does not exist and
   never did** (WP-12 / RED8-08), so the documented refresh path pointed at a
   file nobody wrote — which is how "periodically" became "not since July".

   The registry is a MANUALLY CURATED SNAPSHOT; nothing fetches a ranking at
   runtime. NORTH_STAR described it as a "live", "continuously-updated"
   leaderboard, and that clause was the load-bearing justification for the
   whole "Claude is not axiomatically the top" position.

   Refresh by hand-editing ``config/models.yaml`` and bumping its
   ``snapshot_date``. ``scripts/check_model_registry_freshness.py`` fails CI once
   the snapshot exceeds the cadence, so the schedule now has a mechanism instead
   of a dead pointer.

The router consumes this registry to:
    - Tag each routing decision with the chosen model's tier + quality
    - Compute "could we have used something cheaper at the same quality?"
    - Build the cost-vs-quality Pareto frontier in the benchmark harness
    - Drive empirical lookup tables (v0.0.3 quality_gap derivation)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from llm_router import pricing as _pricing
from llm_router.lineage import Tier


@dataclass(frozen=True)
class ModelMetadata:
    """One row of the registry — everything the router needs to decide."""

    id: str  # canonical id, e.g. "openai/gpt-4o-mini"
    provider: str  # "openai", "anthropic", "google", "ollama", ...
    tier: Tier
    quality_score: float  # 0.0–1.0, normalized from artificialanalysis.ai
    price_per_1m_input_usd: float
    price_per_1m_output_usd: float
    latency_p50_ms: int = 0  # observed median latency
    context_window: int = 0  # in tokens
    capabilities: tuple[str, ...] = ()  # "vision", "function-calling", "json", ...
    source: str = "artificialanalysis.ai"  # provenance
    notes: str = ""

    @property
    def cost_efficiency(self) -> float:
        """Quality per dollar (per 1M tokens, weighted output-heavy)."""
        avg_price = (
            self.price_per_1m_input_usd * 0.3
            + self.price_per_1m_output_usd * 0.7
        )
        if avg_price <= 0:
            return float("inf")  # free models top the chart
        return self.quality_score / avg_price


@dataclass
class ModelRegistry:
    """Lookup + filter operations over a set of ModelMetadata."""

    models: dict[str, ModelMetadata] = field(default_factory=dict)

    @classmethod
    def from_models(cls, models: Iterable[ModelMetadata]) -> "ModelRegistry":
        return cls(models={m.id: m for m in models})

    @classmethod
    def from_yaml(cls, path: Path) -> "ModelRegistry":
        import yaml

        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict) or "models" not in raw:
            raise ValueError(f"{path}: expected top-level 'models:' list")
        return cls.from_models(_parse(m) for m in raw["models"])

    @classmethod
    def load_default(cls) -> "ModelRegistry":
        """Load config/models.yaml from the project root.

        Falls back to a hardcoded minimal registry if the file is absent
        (so the import never breaks LLM Router).
        """
        # Project-level config
        candidates = [
            Path.cwd() / "config" / "models.yaml",
            Path(__file__).resolve().parent.parent.parent / "config" / "models.yaml",
        ]
        for path in candidates:
            if path.exists():
                return cls.from_yaml(path)
        return cls.from_models(_BUNDLED_DEFAULTS)

    # ── Lookup ─────────────────────────────────────────────────────────

    def get(self, model_id: str) -> ModelMetadata | None:
        return self.models.get(model_id)

    def all(self) -> list[ModelMetadata]:
        return list(self.models.values())

    def by_tier(self, tier: Tier) -> list[ModelMetadata]:
        return [m for m in self.models.values() if m.tier == tier]

    def by_provider(self, provider: str) -> list[ModelMetadata]:
        return [m for m in self.models.values() if m.provider == provider]

    def with_capability(self, capability: str) -> list[ModelMetadata]:
        return [m for m in self.models.values() if capability in m.capabilities]

    def cheaper_with_equal_quality(
        self, target: ModelMetadata, quality_tolerance: float = 0.05
    ) -> list[ModelMetadata]:
        """Find models with quality within tolerance of target but cheaper.

        The empirical lookup table can use this to suggest downshifts:
        'you chose GPT-4o, but Sonnet costs less at the same quality'.
        """
        target_price = target.price_per_1m_input_usd
        out = []
        for m in self.models.values():
            if m.id == target.id:
                continue
            if abs(m.quality_score - target.quality_score) > quality_tolerance:
                continue
            if m.price_per_1m_input_usd < target_price:
                out.append(m)
        return sorted(out, key=lambda m: m.price_per_1m_input_usd)

    def pareto_frontier(self) -> list[ModelMetadata]:
        """Models on the cost/quality frontier — no other model weakly
        dominates (≤ cost AND ≥ quality, strict in at least one).

        A model is dominated when some other model is at-least-as-cheap
        AND at-least-as-high-quality AND strictly better in at least one
        of the two dimensions. This is the standard Pareto definition;
        equal-quality + strictly-cheaper drops the more expensive one.
        """
        front = []
        for cand in self.models.values():
            dominated = False
            for other in self.models.values():
                if other.id == cand.id:
                    continue
                cheaper = (
                    other.price_per_1m_input_usd < cand.price_per_1m_input_usd
                )
                better = other.quality_score > cand.quality_score
                cheaper_or_equal = (
                    other.price_per_1m_input_usd
                    <= cand.price_per_1m_input_usd
                )
                better_or_equal = other.quality_score >= cand.quality_score
                if cheaper_or_equal and better_or_equal and (cheaper or better):
                    dominated = True
                    break
            if not dominated:
                front.append(cand)
        return sorted(front, key=lambda m: m.price_per_1m_input_usd)


# ────────────────────────────────────────────────────────────────────────
# YAML parser
# ────────────────────────────────────────────────────────────────────────

def _parse(entry: dict) -> ModelMetadata:
    required = ("id", "provider", "tier", "quality_score",
                "price_per_1m_input_usd", "price_per_1m_output_usd")
    missing = [k for k in required if k not in entry]
    if missing:
        raise ValueError(f"model entry missing required keys: {missing}")

    tier_str = entry["tier"]
    try:
        tier = Tier(tier_str)
    except ValueError:
        raise ValueError(
            f"invalid tier {tier_str!r} for model {entry['id']!r}; "
            f"must be one of {[t.value for t in Tier]}"
        )

    return ModelMetadata(
        id=str(entry["id"]),
        provider=str(entry["provider"]),
        tier=tier,
        quality_score=float(entry["quality_score"]),
        price_per_1m_input_usd=float(entry["price_per_1m_input_usd"]),
        price_per_1m_output_usd=float(entry["price_per_1m_output_usd"]),
        latency_p50_ms=int(entry.get("latency_p50_ms", 0)),
        context_window=int(entry.get("context_window", 0)),
        capabilities=tuple(entry.get("capabilities", ())),
        source=str(entry.get("source", "artificialanalysis.ai")),
        notes=str(entry.get("notes", "")),
    )


# ────────────────────────────────────────────────────────────────────────
# Bundled defaults — used when config/models.yaml is absent
# Updated 2026-06; values approximate, refresh from artificialanalysis.ai
# ────────────────────────────────────────────────────────────────────────

def _priced(
    *,
    price_override: tuple[float, float] | None = None,
    override_reason: str = "",
    **fields: Any,
) -> ModelMetadata:
    """Build a :class:`ModelMetadata` whose rates come from ``llm_router.pricing``.

    WP-03: every entry below used to carry its own two price literals, and two
    of them tripped the retired-rate lint. Those two were, unusually, *correct*
    — Claude 3.5 Haiku really was $0.80/$4.00 and Claude 3 Opus really was
    $15/$75. That is what made this table dangerous rather than merely wrong:
    the retired rates it legitimately holds are indistinguishable, at a glance,
    from the retired rates that leaked into tables describing *current* models.
    Sourcing every rate from one module removes the ambiguity.

    A catalogue entry may still diverge from list pricing — o3's figure here is
    inflated to account for extended-thinking tokens — but it must now say so
    via ``override_reason`` instead of quietly disagreeing.
    """
    if price_override is not None:
        if not override_reason:
            raise ValueError(f"price_override for {fields.get('id')!r} needs an override_reason")
        in_usd, out_usd = price_override
        notes = fields.pop("notes", "")
        fields["notes"] = f"{notes} [price override: {override_reason}]".strip()
    else:
        p = _pricing.price_for(str(fields["id"]))
        if p is None:
            raise ValueError(
                f"{fields['id']!r} has no entry in llm_router.pricing. Add it there "
                f"rather than hardcoding a rate here (INV-COST-004)."
            )
        in_usd, out_usd = p.input, p.output
    return ModelMetadata(
        price_per_1m_input_usd=in_usd,
        price_per_1m_output_usd=out_usd,
        **fields,
    )


_BUNDLED_DEFAULTS: tuple[ModelMetadata, ...] = (
    _priced(
        id="ollama/qwen3.5:latest", provider="ollama", tier=Tier.LOCAL,
        quality_score=0.68,
        latency_p50_ms=1800, context_window=32768,
        capabilities=("function-calling",),
        notes="Local Ollama; free at the API boundary",
    ),
    # WP-03: was anthropic/claude-3.5-haiku. ⚠ BEHAVIOURAL — this is the model
    # id the fallback catalogue offers when config/models.yaml is absent, so
    # this changes what a config-less install can select. The old entry named a
    # retired model at its retired rate; a "default" catalogue that can only
    # offer 2024 models is a defect in its own right, and its $0.80/$4.00 is
    # also the exact pair that leaked into tables describing *current* Haiku.
    _priced(
        id="anthropic/claude-haiku-4-5", provider="anthropic", tier=Tier.CHEAP,
        quality_score=0.74,
        latency_p50_ms=900, context_window=200000,
        capabilities=("function-calling", "vision"),
    ),
    _priced(
        id="google/gemini-1.5-flash-8b", provider="google", tier=Tier.CHEAP,
        quality_score=0.65,
        latency_p50_ms=600, context_window=1_000_000,
        capabilities=("function-calling", "vision", "json"),
    ),
    _priced(
        id="openai/gpt-4o-mini", provider="openai", tier=Tier.CHEAP,
        quality_score=0.72,
        latency_p50_ms=800, context_window=128000,
        capabilities=("function-calling", "vision", "json"),
    ),
    _priced(
        id="openai/gpt-4o", provider="openai", tier=Tier.MID,
        quality_score=0.85,
        latency_p50_ms=1500, context_window=128000,
        capabilities=("function-calling", "vision", "json"),
    ),
    _priced(
        id="anthropic/claude-3.5-sonnet", provider="anthropic", tier=Tier.MID,
        quality_score=0.88,
        latency_p50_ms=1700, context_window=200000,
        capabilities=("function-calling", "vision"),
    ),
    _priced(
        id="google/gemini-1.5-pro", provider="google", tier=Tier.MID,
        quality_score=0.82,
        latency_p50_ms=1800, context_window=2_000_000,
        capabilities=("function-calling", "vision", "json"),
    ),
    _priced(
        id="openai/o3", provider="openai", tier=Tier.PREMIUM,
        quality_score=0.94,
        latency_p50_ms=8000, context_window=200000,
        capabilities=("function-calling", "vision", "json", "reasoning"),
        notes="Reasoning-tier — cost reflects extended thinking tokens",
        # 30x o3's list rate. This is a ranking penalty for reasoning-token
        # amplification, not a price, and it is preserved exactly so routing
        # behaviour does not move inside a pricing refactor. It is also the
        # only entry in this catalogue that disagrees with llm_router.pricing —
        # whether a 30x multiplier is still the right penalty is a routing
        # question, and it now has to be answered out loud.
        price_override=(60.0, 240.0),
        override_reason="reasoning-token amplification; ranking penalty, not list price",
    ),
    # WP-03: was anthropic/claude-3-opus at $15/$75. ⚠ BEHAVIOURAL, same as the
    # Haiku entry above. Claude 3 Opus is retired, and its genuine list price is
    # byte-identical to the retired pair this whole work package exists to keep
    # out of the tree — so keeping the entry would have meant either an unlintable
    # pricing module or a hardcoded rate here. Current Opus is $5/$25.
    _priced(
        id="anthropic/claude-opus-5", provider="anthropic", tier=Tier.PREMIUM,
        quality_score=0.90,
        latency_p50_ms=3000, context_window=200000,
        capabilities=("function-calling", "vision"),
    ),
    _priced(
        id="perplexity/sonar", provider="perplexity", tier=Tier.MID,
        quality_score=0.78,
        latency_p50_ms=3500, context_window=128000,
        capabilities=("web-grounded", "citations"),
        notes="Web-grounded; cost includes search backend",
    ),
)
