"""Claude-tier rewrite: which Claude model a forwarded call runs on (opt-in).

Every Claude tier speaks one Anthropic schema, so moving a call from Opus to
Sonnet or Haiku is a ``body["model"]`` rewrite with client tools untouched.
The Phase 0.4 probe (2026-09-29, n=5 calls per model) found Max quota is one
shared weekly pool that drains in proportion to per-call cost, with Opus at
1.68x Sonnet and 6.15x Haiku per call, so an easy turn moved down a tier
drains that pool more slowly.

The decision, per call, in order (the first that applies wins):

``unknown_model``   the requested model is not a configured tier: unchanged.
``config_pinned``   the requested id is in ``pinned_models``: unchanged.
``side_call``       no client tools (titles, probes): unchanged.
``user_pinned``     the transcript shows the user ran ``/model``: unchanged.
``first_call``      the conversation's first call: unchanged.
``policy``          the router's own classifier (``choose_model`` ->
                    ``classify_signals(GATEWAY_POLICY)`` and
                    ``router._build_and_filter_chain``) over the newest human
                    prompt gives (task_type, complexity); ``route`` maps that
                    to a tier. Never above the requested tier unless
                    ``allow_upgrade``.
``thinking_floor``  as ``policy``, raised to the cheapest tier that accepts
                    the request's ``thinking.type`` and, if it sets one,
                    ``output_config.effort`` (Haiku 4.5 takes neither adaptive
                    thinking nor effort; Claude Code sends both).
``sticky``          the policy wanted a different tier, but the conversation
                    stays where it was (``cache_cost``): same complexity
                    class and the cache is warm.

Model ids come from the YAML policy (``claude_tiers.yaml`` beside this module,
or the file ``--tier-policy`` / ``LLM_ROUTER_PROXY_TIER_POLICY`` names), never from literals here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llm_router.proxy.cache_cost import Stickiness, conversation_key, switch_cost_usd
from llm_router.proxy.steps import has_client_tools, is_first_call, tier_text, user_pinned_model

DEFAULT_POLICY_PATH = Path(__file__).with_name("claude_tiers.yaml")
THINKING_TYPES = ("enabled", "adaptive")

REASON_UNKNOWN_MODEL = "unknown_model"
REASON_CONFIG_PINNED = "config_pinned"
REASON_SIDE_CALL = "side_call"
REASON_USER_PINNED = "user_pinned"
REASON_FIRST_CALL = "first_call"
REASON_POLICY = "policy"
REASON_THINKING_FLOOR = "thinking_floor"
REASON_STICKY = "sticky"
REASON_DECISION_ERROR = "decision_error"


@dataclass(frozen=True)
class Tier:
    name: str
    model: str
    also: tuple[str, ...] = ()
    thinking: frozenset = frozenset()
    effort: bool = True


@dataclass
class TierDecision:
    requested_model: str | None
    served_model: str | None
    tier: str | None
    reason: str
    switched: bool = False
    switch_cost_usd: float | None = None
    task_type: str | None = None
    complexity: str | None = None
    chain_head: list = field(default_factory=list)

    @property
    def rewritten(self) -> bool:
        return self.served_model is not None and self.served_model != self.requested_model


def _canonical(model: str | None) -> str | None:
    if not model:
        return None
    from llm_router import pricing

    return pricing.resolve(model) or model.strip().lower()


class ClaudeTierPolicy:
    """(task_type, complexity) -> Claude tier, with the no-downgrade rules."""

    def __init__(self, tiers: list[Tier], route: dict[str, dict[str, str]], *,
                 allow_upgrade: bool = False, pinned_models: tuple[str, ...] = (),
                 cold_gap_s: float = 3600.0, switch_after_first_call: bool = False) -> None:
        if not tiers:
            raise ValueError("tier policy has no tiers")
        self.tiers = tiers
        self.rank = {t.name: i for i, t in enumerate(tiers)}
        self.by_name = {t.name: t for t in tiers}
        for task, table in route.items():
            for cx, name in table.items():
                if name not in self.by_name:
                    raise ValueError(f"route {task}.{cx} names unknown tier {name!r}")
        if "default" not in route:
            raise ValueError("tier policy route needs a `default` table")
        self.route = route
        self.allow_upgrade = allow_upgrade
        self.pinned = frozenset(filter(None, (_canonical(m) for m in pinned_models)))
        self.cold_gap_s = cold_gap_s
        self.switch_after_first_call = switch_after_first_call
        self._ids: dict[str, Tier] = {}
        for t in tiers:
            for mid in (t.model, *t.also):
                self._ids[_canonical(mid)] = t

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeTierPolicy":
        tiers = []
        for t in data.get("tiers") or []:
            if not isinstance(t, dict) or not t.get("name") or not t.get("model"):
                raise ValueError(f"tier entry needs name and model: {t!r}")
            thinking = frozenset(t.get("thinking") or ())
            bad = thinking - set(THINKING_TYPES)
            if bad:
                raise ValueError(f"tier {t['name']}: unknown thinking type(s) {sorted(bad)}")
            tiers.append(Tier(str(t["name"]), str(t["model"]), tuple(t.get("also") or ()), thinking,
                              bool(t.get("effort", True))))
        stick = data.get("stickiness") or {}
        return cls(tiers, {str(k): dict(v or {}) for k, v in (data.get("route") or {}).items()},
                   allow_upgrade=bool(data.get("allow_upgrade", False)),
                   pinned_models=tuple(data.get("pinned_models") or ()),
                   cold_gap_s=float(stick.get("cold_gap_s", 3600.0)),
                   switch_after_first_call=bool(stick.get("switch_after_first_call", False)))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ClaudeTierPolicy":
        import yaml

        target = Path(path or DEFAULT_POLICY_PATH)
        data = yaml.safe_load(target.expanduser().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"tier policy {target} is not a mapping")
        return cls.from_dict(data)

    # ── lookups ─────────────────────────────────────────────────────────────

    def tier_of(self, model: str | None) -> Tier | None:
        return self._ids.get(_canonical(model)) if model else None

    def tier_for(self, task_type: str | None, complexity: str | None) -> Tier | None:
        name = (self.route.get(task_type or "") or {}).get(complexity or "") or \
            self.route["default"].get(complexity or "")
        return self.by_name.get(name) if name else None

    @staticmethod
    def _accepts(tier: Tier, thinking: str | None, effort: bool) -> bool:
        return (thinking is None or thinking in tier.thinking) and (not effort or tier.effort)

    def _allowed(self, tier: Tier, requested: Tier, thinking: str | None, effort: bool) -> bool:
        if not self.allow_upgrade and self.rank[tier.name] > self.rank[requested.name]:
            return False
        return self._accepts(tier, thinking, effort)

    # ── the decision ────────────────────────────────────────────────────────

    async def decide(self, body: dict, session_id: str | None, sticky: Stickiness,
                     classify=None) -> TierDecision:
        """``classify(text) -> {"task_type", "complexity", "chain_head", ...}``
        defaults to ``backends.choose_model(text, None, anthropic=True)``."""
        requested = body.get("model") if isinstance(body.get("model"), str) else None
        req_tier = self.tier_of(requested)

        def keep(reason: str, **kw) -> TierDecision:
            return TierDecision(requested, requested, req_tier.name if req_tier else None, reason, **kw)

        if req_tier is None:
            return keep(REASON_UNKNOWN_MODEL)
        if _canonical(requested) in self.pinned:
            return keep(REASON_CONFIG_PINNED)
        if not has_client_tools(body):
            return keep(REASON_SIDE_CALL)
        if user_pinned_model(body):
            return keep(REASON_USER_PINNED)
        key = conversation_key(body, session_id)
        if is_first_call(body):
            sticky.record(key, requested, None, REASON_FIRST_CALL)
            return keep(REASON_FIRST_CALL)

        if classify is None:
            from llm_router.proxy.backends import choose_model

            async def classify(text: str) -> dict:
                return await choose_model(text, None, anthropic=True)

        choice = await classify(tier_text(body))
        task, cx = choice.get("task_type"), choice.get("complexity")
        thinking = (body.get("thinking") or {}).get("type") if isinstance(body.get("thinking"), dict) else None
        thinking = thinking if thinking in THINKING_TYPES else None
        oc = body.get("output_config")
        effort = isinstance(oc, dict) and oc.get("effort") is not None

        target = self.tier_for(task, cx) or req_tier
        reason = REASON_POLICY
        if not self.allow_upgrade and self.rank[target.name] > self.rank[req_tier.name]:
            target = req_tier
        if not self._accepts(target, thinking, effort):
            floor = next((t for t in self.tiers[self.rank[target.name]:]
                          if self._allowed(t, req_tier, thinking, effort)), None)
            target = floor or req_tier
            reason = REASON_THINKING_FLOOR

        state = sticky.get(key)
        prev_model = state.model if state is not None else requested
        served = target.model
        cold = state is not None and sticky.is_cold(state)
        # A first-call state carries no class (it was never classified), so it
        # is not a "class change": the conversation stays unless handed off.
        class_changed = state is not None and state.complexity is not None and state.complexity != cx
        handoff = state is not None and self.switch_after_first_call and state.reason == REASON_FIRST_CALL
        if (state is not None and _canonical(prev_model) != _canonical(served)
                and not class_changed and not cold and not handoff):
            prev_tier = self.tier_of(prev_model)
            if prev_tier is not None and self._allowed(prev_tier, req_tier, thinking, effort):
                served, target, reason = prev_model, prev_tier, REASON_STICKY
        if _canonical(served) == _canonical(requested):
            served = requested  # keep the client's own spelling when nothing changes

        switched = _canonical(served) != _canonical(prev_model)
        cost = None
        if switched:
            cost = 0.0 if cold else switch_cost_usd(served, state.prefix_tokens if state else None)
        sticky.record(key, served, cx, reason)
        return TierDecision(requested, served, target.name, reason, switched=switched, switch_cost_usd=cost,
                            task_type=task, complexity=cx, chain_head=list(choice.get("chain_head") or [])[:4])
