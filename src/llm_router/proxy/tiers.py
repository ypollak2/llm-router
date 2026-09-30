"""Claude-tier rewrite: which Claude model a forwarded call runs on (opt-in).

Every Claude tier speaks one Anthropic schema, so moving a call from Opus to
Sonnet or Haiku is a ``body["model"]`` rewrite with client tools untouched.
The Phase 0.4 probe (2026-09-29, n=5 calls per model) found Max quota is one
shared weekly pool that drains in proportion to per-call cost, with Opus at
1.68x Sonnet and 6.15x Haiku per call, so an easy turn moved down a tier
drains that pool more slowly.

The decision, per call, in order (the first that applies wins):

``unknown_model``   the requested model is not a configured tier: unchanged.
``config_pinned``   the requested id is in ``pinned_models``: unchanged. Checked
                    BEFORE ``explicit_opus_pin``, deliberately: an admin-level
                    pin outranks a user's ``opus:`` text, so a request on a
                    pinned id stays ``config_pinned`` (not escalated) even
                    with that prefix -- a fail-TO-escalate, never a downgrade.
``side_call``       no client tools (titles, probes): unchanged.
``explicit_opus_pin`` the newest human turn starts with ``opus:``
                    (``proxy/escalation.py``): pinned to the Opus tier,
                    bypassing ``allow_upgrade`` the same way ``config_pinned``
                    does. Checked BEFORE ``user_pinned`` so it can still
                    escalate a conversation that ran ``/model`` earlier.
``user_pinned``     the transcript shows the user ran ``/model``: unchanged.
``long_first_prompt_floor`` the conversation's first call, and the prompt is
                    long or multi-part (``proxy/escalation.py``,
                    ``is_long_or_multi_part``): unchanged, in EVERY mode
                    (unlike ``first_call`` below, this applies in conversation
                    mode too). Safety default paraphrasing the trial's one
                    unacceptable answer (t7-northstar) -- see that module's
                    docstring.
``first_call``      the conversation's first call, **in per-turn mode only**
                    (``--tiers on``): unchanged. In conversation mode
                    (``--tiers conversation``, ``conversation_level=True``)
                    the first call is classified and rewritten exactly like
                    any other call below -- there is no exemption -- and the
                    tier it lands on then rides stickiness for the rest of
                    the conversation. This is the Phase 1.2b change: the Key
                    finding (1.2) was that per-turn switching loses money
                    under prompt caching, so the decision that matters is the
                    one made once, at the conversation's start.
``policy``          the router's own classifier (``choose_model`` ->
                    ``classify_signals(GATEWAY_POLICY)`` and
                    ``router._build_and_filter_chain``) over the newest human
                    prompt gives (task_type, complexity); ``route`` maps that
                    to a tier. Never above the requested tier unless
                    ``allow_upgrade``. A future Phase 2 kNN scorer replaces
                    this call only: pass ``classify=`` to ``decide()`` (or a
                    ``classify`` constructor argument), never a callsite
                    change in ``server.py``.
``thinking_floor``  as ``policy``, raised to the cheapest tier that accepts
                    the request's ``thinking.type`` and, if it sets one,
                    ``output_config.effort`` (Haiku 4.5 takes neither adaptive
                    thinking nor effort; Claude Code sends both).
``escalation``      checked after ``thinking_floor``, on EVERY call (not just
                    the first): ``escalation.correction_signal`` found a
                    contradiction, a ``claude:`` re-ask, or a run of failed
                    tool calls in the newest turn. Forces the Opus tier and is
                    treated as a class change so the ``sticky`` branch below
                    cannot immediately pull it back down; ``detail`` carries
                    which signal fired. Runs on every call, so it fires the
                    moment the signal appears -- "immediately", a superset of
                    "at the next cold point".
``sticky``          the policy wanted a different tier, but the conversation
                    stays where it was (``cache_cost``): same complexity
                    class and the cache is warm.

In conversation mode, a mid-conversation move (``class_changed``) only breaks
stickiness when the new class ranks a HIGHER tier than the one the
conversation is already sitting on (an escalation) -- never a downgrade back
toward a cheaper tier once committed. A cold point (``cold_gap_s`` with no
call) still resets stickiness either way, same as per-turn mode: "escalate
within a conversation only at a cold point, or when the complexity class
clearly rises."

Model ids come from the YAML policy (``claude_tiers.yaml`` beside this module,
or the file ``--tier-policy`` / ``LLM_ROUTER_PROXY_TIER_POLICY`` names), never from literals here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_router.proxy import escalation
from llm_router.proxy.cache_cost import ConvState, Stickiness, conversation_key, switch_cost_usd
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
REASON_UNSEEN = "unseen"  # internal state marker, never a row's tier_reason
# Phase "proxy-default": explicit/automatic escalation (proxy/escalation.py)
# and the long-first-prompt safety floor. See that module's docstring for the
# trial evidence behind each one.
REASON_EXPLICIT_OPUS_PIN = "explicit_opus_pin"
REASON_ESCALATION = "escalation"  # detail carries escalation.REASON_* (contradiction/claude_reask/tool_failures)
REASON_LONG_FIRST_PROMPT = "long_first_prompt_floor"


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
    complexity_score: float | None = None  # complexity_knn P(needs frontier), when consulted
    detail: str | None = None  # e.g. escalation.REASON_* when reason == REASON_ESCALATION

    @property
    def rewritten(self) -> bool:
        return self.served_model is not None and self.served_model != self.requested_model


async def _default_classify(text: str) -> dict:
    from llm_router.proxy.backends import choose_model

    return await choose_model(text, None, anthropic=True)


def with_complexity_knn(base=None):
    """Wrap a ``classify`` callable with the Phase 2.1 learned score.

    The wrapped result's ``complexity`` is moved across the frontier boundary
    by ``complexity_knn`` (``frontier_complexity``) and carries
    ``complexity_score``; when the score abstains (no artifact, embedder down)
    the base classification is returned unchanged with ``complexity_score``
    None. The module is looked up at call time so tests can stub it.
    """
    base = base or _default_classify

    async def classify(text: str) -> dict:
        from llm_router import complexity_knn

        choice = dict(await base(text))
        ks = await complexity_knn.complexity_score(text)
        if ks is None:
            choice["complexity_score"] = None
            return choice
        choice["complexity"] = complexity_knn.frontier_complexity(choice.get("complexity"), ks.needs_frontier)
        choice["complexity_score"] = round(ks.score, 4)
        return choice

    return classify


def _canonical(model: str | None) -> str | None:
    if not model:
        return None
    from llm_router import pricing

    return pricing.resolve(model) or model.strip().lower()


class ClaudeTierPolicy:
    """(task_type, complexity) -> Claude tier, with the no-downgrade rules."""

    def __init__(self, tiers: list[Tier], route: dict[str, dict[str, str]], *,
                 allow_upgrade: bool = False, pinned_models: tuple[str, ...] = (),
                 cold_gap_s: float = 3600.0, switch_after_first_call: bool = False,
                 conversation_level: bool = False, classify=None,
                 complexity_knn: bool = False) -> None:
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
        self.conversation_level = conversation_level
        # Phase 2 hook point: a kNN scorer replaces this callable only, never
        # a callsite in server.py. ``decide()``'s own ``classify=`` argument
        # (per call, mainly for tests) wins over this one when both are given.
        # ``complexity_knn: true`` in the policy YAML plugs the Phase 2.1 score
        # in exactly here, as a wrapper on that callable (off by default).
        self.complexity_knn = complexity_knn
        self._classify = with_complexity_knn(classify) if complexity_knn else classify
        self._ids: dict[str, Tier] = {}
        for t in tiers:
            for mid in (t.model, *t.also):
                self._ids[_canonical(mid)] = t

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict, *, conversation_level: bool = False, classify=None) -> "ClaudeTierPolicy":
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
                   switch_after_first_call=bool(stick.get("switch_after_first_call", False)),
                   conversation_level=conversation_level, classify=classify,
                   complexity_knn=bool(data.get("complexity_knn", False)))

    @classmethod
    def load(cls, path: str | Path | None = None, *, conversation_level: bool = False,
              classify=None) -> "ClaudeTierPolicy":
        import yaml

        target = Path(path or DEFAULT_POLICY_PATH)
        try:
            data = yaml.safe_load(target.expanduser().read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"tier policy {target} is not valid YAML: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError(f"tier policy {target} is not a mapping")
        return cls.from_dict(data, conversation_level=conversation_level, classify=classify)

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
        key = conversation_key(body, session_id)
        opus_tier = self.by_name.get("opus")
        if opus_tier is not None and escalation.explicit_opus_pin(body):
            # `opus:` — an explicit pin, never subject to the no-upgrade rule
            # (config_pinned/pinned_models bypass it the same way): the user
            # asked for Opus by name, so this call and, via stickiness, the
            # rest of the conversation go there regardless of what the
            # classifier would have said.
            sticky.record(key, opus_tier.model, None, REASON_EXPLICIT_OPUS_PIN)
            return TierDecision(requested, opus_tier.model, opus_tier.name, REASON_EXPLICIT_OPUS_PIN,
                                switched=_canonical(opus_tier.model) != _canonical(requested))
        if user_pinned_model(body):
            return keep(REASON_USER_PINNED)
        first = is_first_call(body)
        if first and escalation.first_prompt_is_long_or_multi_part(body):
            # Safety default: a first prompt that reads as a multi-part brief
            # rather than a quick question is exactly the shape the trial's
            # own tiering got wrong once (proxy/escalation.py docstring,
            # t7-northstar). Skip the rewrite for THIS call and keep whatever
            # model Claude Code itself requested; the policy still applies
            # normally to every later call in the conversation.
            sticky.record(key, requested, None, REASON_LONG_FIRST_PROMPT)
            return keep(REASON_LONG_FIRST_PROMPT)
        if first and not self.conversation_level:
            # Per-turn mode: the first call is exempt (the switch cost of a
            # rewrite on the not-yet-cached prefix is the whole prompt).
            sticky.record(key, requested, None, REASON_FIRST_CALL)
            return keep(REASON_FIRST_CALL)

        if classify is None:
            classify = self._classify
        if classify is None:
            classify = _default_classify

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

        detail = None
        if opus_tier is not None and self.rank[opus_tier.name] >= self.rank[target.name]:
            # Automatic escalation (proxy/escalation.py): a contradiction, a
            # `claude:` re-ask, or a run of failed tool calls means the prior
            # answer needs redoing. Applied AFTER the no-upgrade cap above —
            # like the explicit `opus:` pin, this is a deliberate bypass of
            # "policy only ever moves down", not a bug in it. Runs on every
            # call (not just the first), so it fires the moment the signal
            # appears rather than waiting for a cold point.
            signal = escalation.correction_signal(body)
            if signal is not None and self._accepts(opus_tier, thinking, effort):
                target, reason, detail = opus_tier, REASON_ESCALATION, signal

        state = sticky.get(key)
        if state is None and not first:
            # Mid-conversation but unseen (e.g. the proxy restarted): treat it as
            # last served on the requested model, class unknown, cache warm, so
            # the stickiness rules below apply instead of an immediate switch.
            state = ConvState(requested, None, time.time(), REASON_UNSEEN)
        # ``state`` stays None only for a genuine conversation-mode first call:
        # there is no prior decision to be sticky to, so the classified target
        # below applies directly -- this IS the conversation-level decision.
        prev_model = state.model if state is not None else None
        prev_tier = self.tier_of(prev_model) if state is not None else None
        served = target.model
        cold = state is not None and sticky.is_cold(state)
        # A first-call state carries no class (it was never classified), so it
        # is not a "class change": the conversation stays unless handed off.
        class_changed = state is not None and state.complexity is not None and state.complexity != cx
        if self.conversation_level and class_changed and prev_tier is not None:
            # Conversation mode only escalates (a class that ranks a HIGHER
            # tier breaks stickiness); a class that would rank the same or a
            # cheaper tier never pulls a committed conversation back down.
            class_changed = self.rank[target.name] > self.rank[prev_tier.name]
        if detail is not None:
            # A correction-signal escalation must not be pulled back down by
            # the "no class change -> stay sticky" branch below just because
            # the CLASSIFIED complexity of this turn happens to match the
            # conversation's last one — the signal is an escalation event in
            # its own right, independent of what the classifier said.
            class_changed = True
        handoff = state is not None and self.switch_after_first_call and state.reason == REASON_FIRST_CALL
        if (state is not None and _canonical(prev_model) != _canonical(served)
                and not class_changed and not cold and not handoff):
            if prev_tier is not None and self._allowed(prev_tier, req_tier, thinking, effort):
                served, target, reason = prev_model, prev_tier, REASON_STICKY
        if _canonical(served) == _canonical(requested):
            served = requested  # keep the client's own spelling when nothing changes

        # A genuine first decision (state is None) is not a "switch": nothing
        # was previously cached on a different model to re-write.
        switched = state is not None and _canonical(served) != _canonical(prev_model)
        cost = None
        if switched:
            cost = 0.0 if cold else switch_cost_usd(served, state.prefix_tokens if state else None)
        sticky.record(key, served, cx, reason)
        return TierDecision(requested, served, target.name, reason, switched=switched, switch_cost_usd=cost,
                            task_type=task, complexity=cx, chain_head=list(choice.get("chain_head") or [])[:4],
                            complexity_score=choice.get("complexity_score"), detail=detail)
