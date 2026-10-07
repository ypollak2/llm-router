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
``haiku_rewrite``   opt-in (``haiku_rewrite: true`` in the policy YAML, OFF
                    by default): when the floor above would otherwise move a
                    ``haiku``-targeted call up to ``sonnet`` purely because
                    of ``thinking``/``effort``, AND the call is eligible
                    (``ClaudeTierPolicy._haiku_eligible`` -- no image/document
                    content anywhere in the transcript, only custom client
                    tools, and the body's approximate size under
                    ``HAIKU_MAX_CONTEXT_TOKENS``), the call stays on
                    ``haiku`` and ``TierDecision.body_rewrite`` is set so
                    ``server.py`` rewrites the body with
                    ``proxy.translate.for_haiku`` before forwarding instead
                    of raising the tier. Never bypasses ``allow_upgrade``,
                    stickiness, or the escalation check below -- it only
                    changes what happens when the policy already landed on
                    ``haiku``.
``haiku_fold_system`` opt-in (``haiku_fold_system: true`` in the policy YAML, OFF
                    by default, only effective with ``haiku_rewrite``): a body
                    with mid-conversation ``role: "system"`` messages is
                    eligible for the rewrite too, because ``for_haiku`` folds
                    them into user messages (M0.7).
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

``quota_pressure``  the LAST step, applied to whatever the steps above chose
                    (including a tier ``sticky`` would hold, and the
                    ``first_call`` / ``long_first_prompt_floor`` keeps, whose
                    own reason then moves to ``detail``). The Claude
                    subscription's pressure -- max(session %, weekly %) from
                    the cached ``usage.json`` (``proxy/quota_pressure.py``,
                    never a network call) -- at or above ``cap_at`` moves any
                    tier above Sonnet down to Sonnet; at or above
                    ``moderate_at`` a ``moderate`` turn also moves to Haiku when
                    Haiku accepts the body as sent or ``haiku_rewrite`` makes
                    it eligible (otherwise the thinking floor keeps it on
                    Sonnet). Max quota drains in proportion to per-call cost,
                    so this spends the remaining quota more slowly; a cache
                    re-write on the switch is minor next to running out.
                    Exempt: ``unknown_model``, ``config_pinned``,
                    ``side_call``, ``explicit_opus_pin`` and ``user_pinned``
                    (they return before it), and ``escalation`` -- which still
                    reaches Opus, recorded as ``escalation_under_pressure``
                    when pressure is at or above ``cap_at``. An unknown,
                    stale or switched-off reading changes nothing (fail open);
                    the reading's value and state are on every decision.
                    Nothing changes at 98%+ either: the proxy forwards Claude
                    Code's calls to Anthropic only, so there is no non-Claude
                    tier to move to.

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

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_router.proxy import escalation
from llm_router.proxy import quota_pressure as quota_pressure_mod
from llm_router.proxy.cache_cost import ConvState, Stickiness, conversation_key, switch_cost_usd
from llm_router.proxy.steps import has_client_tools, is_first_call, non_system, tier_text, user_pinned_model

DEFAULT_POLICY_PATH = Path(__file__).with_name("claude_tiers.yaml")
THINKING_TYPES = ("enabled", "adaptive")

# Claude Haiku 4.5's context window is 200K tokens, against 1M on Sonnet
# 5.5/Opus 5.5/Fable 5.1 (platform.claude.com/docs/en/about-claude/models/
# overview, fetched 2026-10-02). The limit below leaves a 25% margin under
# that ceiling for the system prompt, tool schemas, and output this
# chars/4 estimate (``token_budget.estimate_tokens``, a hot-path
# approximation, not an exact count) does not see.
HAIKU_MAX_CONTEXT_TOKENS = 150_000
REWRITE_HAIKU = "haiku"

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
# Phase "haiku-tier": opt-in body rewrite in place of the thinking floor, see
# the module docstring's ``haiku_rewrite`` entry.
REASON_HAIKU_REWRITE = "haiku_rewrite"
# Quota-aware tiers (proxy/quota_pressure.py), see the module docstring's
# ``quota_pressure`` entry. Defaults pending owner approval (2026-10-03).
REASON_QUOTA_PRESSURE = "quota_pressure"
REASON_ESCALATION_UNDER_PRESSURE = "escalation_under_pressure"
DEFAULT_QUOTA_CAP_AT = 0.70
DEFAULT_QUOTA_MODERATE_AT = 0.90


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
    body_rewrite: str | None = None  # REWRITE_HAIKU when server.py must run translate.for_haiku on the body
    quota_pressure: float | None = None  # max(session, weekly) 0-1 the decision saw, when readable
    quota_state: str | None = None  # quota_pressure.STATE_* (ok / stale / unknown / off)
    # What the policy table said for (task_type, complexity), BEFORE the no-upgrade
    # clamp, the thinking floor, escalation, stickiness and quota pressure moved it.
    # None where the classifier never ran (the early keep / pin returns): not computed.
    proposed_tier: str | None = None

    @property
    def rewritten(self) -> bool:
        return self.served_model is not None and self.served_model != self.requested_model


def policy_version(path: str | Path | None = None) -> str:
    """Identity of the tier policy a ledger row was decided under: a 12-hex
    sha256 over the policy file's bytes plus the router version. Changes when
    either the YAML or the router release changes, so rows from before and
    after a policy edit are separable. ``"unreadable"`` if the file cannot be
    read (never raises: it is called at proxy start and must not cost a call)."""
    from llm_router import __version__

    target = Path(path or DEFAULT_POLICY_PATH).expanduser()
    try:
        raw = target.read_bytes()
    except OSError:
        return "unreadable"
    return hashlib.sha256(raw + b"\0" + str(__version__).encode("utf-8")).hexdigest()[:12]


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


def _has_media(body: dict) -> bool:
    """True when any message in the WHOLE transcript (not just the newest
    turn -- ``steps._newest_turn_has_media`` is a different, narrower check
    used for step eligibility) carries an image or document, including one
    nested inside a ``tool_result``. Haiku serving is excluded on sight of
    either: this rewrite only targets the narrow text-only class."""
    for m in non_system(body.get("messages") or []):
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("image", "document"):
                return True
            inner = block.get("content")
            if block.get("type") == "tool_result" and isinstance(inner, list):
                if any(isinstance(x, dict) and x.get("type") in ("image", "document") for x in inner):
                    return True
    return False


def _has_mid_conversation_system_message(body: dict) -> bool:
    """True when any entry in ``messages`` -- including the ones
    ``non_system()`` filters out, which is the point here -- has
    ``role: "system"``.

    Found live, not in any doc: Claude Code 2.1.285 (beta flags
    ``mid-conversation-system-2026-04-07`` and ``per-turn-control-2026-07-01``)
    sends one of these on every call, carrying the per-turn
    ``output_config.effort`` -- it is not an occasional mid-conversation
    event, it is standard shape. ``for_haiku`` strips a top-level
    ``output_config``, but this one lives on the message, which
    ``non_system()``-based scans (``_has_media``, ``_only_custom_tools``)
    never see, since they exist to skip exactly this role. Haiku 4.5
    rejects the role outright -- a live smoke call got back
    ``{"type":"invalid_request_error","message":"role 'system' is not
    supported on this model"}`` -- and dropping the message instead of
    the whole rewrite would silently discard real content (environment
    info, reminders) and/or an effort change the turn actually made, so
    by default this is an eligibility exclusion. With ``haiku_fold_system:
    true`` (M0.7, off by default, smoke-tested only) ``for_haiku`` instead
    folds each such message into a ``<system-reminder>`` text block of a user
    message, keeping the text and dropping only the effort control. Whether
    the CURRENT Claude Code still sends the message on every call is not
    checked by this docstring: ``has_mid_system`` in the proxy ledger (M0.5)
    measures it."""
    return any(isinstance(m, dict) and m.get("role") == "system" for m in body.get("messages") or [])


# Public name for the ledger (proxy/server.py); the tests keep the private one.
has_mid_conversation_system_message = _has_mid_conversation_system_message

#: Why a body cannot go to Haiku, as ``tier_haiku_block`` in the ledger (M0.5).
HAIKU_BLOCK_SYSTEM_MESSAGE = "system_message"
HAIKU_BLOCK_MEDIA = "media"
HAIKU_BLOCK_BUILTIN_TOOLS = "builtin_tools"
HAIKU_BLOCK_CONTEXT = "context"
HAIKU_BLOCK_NONE = "none"


def haiku_block_reason(body: dict, *, fold_system: bool = False) -> str:
    """The first reason this body cannot be sent to Haiku, else ``"none"``.

    One of ``system_message | media | builtin_tools | context | none``. The checks
    are the ones ``ClaudeTierPolicy._haiku_body_ok`` has always made, so a body is
    eligible exactly when this returns ``"none"``. The mid-conversation system
    message is listed first so the ledger attributes a body that fails several
    checks to the one the fold (M0.7) would remove. With ``fold_system`` (the policy's
    ``haiku_folds_system``) that check is skipped: ``translate.for_haiku`` folds the
    message into a user message. The policy's own question ("is there a haiku tier at
    all") is not a body property and is not asked here."""
    if not fold_system and _has_mid_conversation_system_message(body):
        return HAIKU_BLOCK_SYSTEM_MESSAGE
    if _has_media(body):
        return HAIKU_BLOCK_MEDIA
    if not _only_custom_tools(body):
        return HAIKU_BLOCK_BUILTIN_TOOLS
    if _approx_context_tokens(body) > HAIKU_MAX_CONTEXT_TOKENS:
        return HAIKU_BLOCK_CONTEXT
    return HAIKU_BLOCK_NONE


def _only_custom_tools(body: dict) -> bool:
    """True when every client tool is a plain custom function (no ``type``,
    or ``type: "custom"``). Anthropic's built-in server tools (bash,
    text_editor, computer, web_search, code_execution, ...) carry a
    model-specific ``type`` id and are excluded from this narrow rewrite --
    Claude Code's own tool calls are all custom (``tests/fixtures/proxy/
    continuation_request.json`` has none with a ``type``)."""
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("type") not in (None, "custom"):
            return False
    return True


def _approx_context_tokens(body: dict) -> int:
    """Fast chars/4 estimate (``token_budget.estimate_tokens``) of what the
    call sends as context: messages, system prompt, and tool schemas. A
    hot-path approximation, not a token count Anthropic would bill -- the
    eligibility check below leaves a 25% margin under Haiku's real 200K
    window specifically because of this estimate's error."""
    from llm_router.token_budget import estimate_tokens

    parts = [json.dumps(body.get("messages") or []), json.dumps(body.get("system") or ""),
             json.dumps(body.get("tools") or [])]
    return estimate_tokens("".join(parts))


class ClaudeTierPolicy:
    """(task_type, complexity) -> Claude tier, with the no-downgrade rules."""

    def __init__(self, tiers: list[Tier], route: dict[str, dict[str, str]], *,
                 allow_upgrade: bool = False, pinned_models: tuple[str, ...] = (),
                 cold_gap_s: float = 3600.0, switch_after_first_call: bool = False,
                 conversation_level: bool = False, classify=None,
                 complexity_knn: bool = False, haiku_rewrite: bool = False,
                 haiku_fold_system: bool = False, quota_pressure: dict | None = None, quota=None) -> None:
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
        # ``haiku_rewrite: true`` serves eligible turns on the Haiku tier by
        # rewriting the body (see ``_haiku_eligible``); OFF by default.
        self.haiku_rewrite = haiku_rewrite
        # ``haiku_fold_system: true`` (M0.7, OFF by default) makes a body with a
        # mid-conversation ``role: "system"`` message eligible for Haiku: the rewrite
        # folds it into a user message (``translate.fold_system_messages``). Only
        # effective together with ``haiku_rewrite`` (``haiku_folds_system``).
        if not isinstance(haiku_fold_system, bool):
            raise ValueError(f"haiku_fold_system must be true or false, got {haiku_fold_system!r}")
        self.haiku_fold_system = haiku_fold_system
        self._classify = with_complexity_knn(classify) if complexity_knn else classify
        # Quota pressure step (``quota_pressure:`` in the policy YAML). ``quota``
        # is a ``() -> QuotaReading`` hook for tests; the default reads the
        # cached usage.json with the configured staleness limit.
        if quota_pressure is not None and not isinstance(quota_pressure, dict):
            raise ValueError("quota_pressure must be a mapping")
        qp = dict(quota_pressure or {})
        self.quota_enabled = bool(qp.get("enabled", True))
        self.quota_cap_at = float(qp.get("cap_at", DEFAULT_QUOTA_CAP_AT))
        self.quota_moderate_at = float(qp.get("moderate_at", DEFAULT_QUOTA_MODERATE_AT))
        self.quota_max_age_s = float(qp.get("max_age_s", quota_pressure_mod.DEFAULT_MAX_AGE_S))
        if not 0.0 <= self.quota_cap_at <= self.quota_moderate_at:
            raise ValueError("quota_pressure needs 0 <= cap_at <= moderate_at")
        self._quota = quota
        # Stamped on every proxy ledger row (tier_policy_version). ``load`` sets
        # it from the file; a policy built from a dict has no file to hash.
        self.policy_version: str | None = None
        self._ids: dict[str, Tier] = {}
        for t in tiers:
            for mid in (t.model, *t.also):
                self._ids[_canonical(mid)] = t

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict, *, conversation_level: bool = False, classify=None,
                  quota=None) -> "ClaudeTierPolicy":
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
                   complexity_knn=bool(data.get("complexity_knn", False)),
                   haiku_rewrite=bool(data.get("haiku_rewrite", False)),
                   haiku_fold_system=data.get("haiku_fold_system", False),
                   quota_pressure=data.get("quota_pressure"), quota=quota)

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
        policy = cls.from_dict(data, conversation_level=conversation_level, classify=classify)
        policy.policy_version = policy_version(target)
        return policy

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

    def _allowed(self, tier: Tier, requested: Tier, thinking: str | None, effort: bool,
                 haiku_ok: bool = False) -> bool:
        if not self.allow_upgrade and self.rank[tier.name] > self.rank[requested.name]:
            return False
        return self._accepts(tier, thinking, effort) or self._rewritable(tier, haiku_ok)

    @property
    def haiku_folds_system(self) -> bool:
        """True when a mid-conversation system message is folded for Haiku: the fold
        runs inside the Haiku rewrite, so it needs ``haiku_rewrite`` as well."""
        return self.haiku_fold_system and self.haiku_rewrite

    def _rewritable(self, tier: Tier, haiku_ok: bool) -> bool:
        """True when ``tier`` is the Haiku tier and this body may be rewritten for it."""
        haiku = self.by_name.get("haiku")
        return bool(haiku_ok and haiku is not None and tier.name == haiku.name)

    def _haiku_eligible(self, body: dict) -> bool:
        """The narrow class of turns the Haiku rewrite may serve.

        Needs ``haiku_rewrite`` on, a ``haiku`` tier in the policy, no images or
        documents anywhere in the conversation, only custom (client-defined)
        tools -- Anthropic built-in/server tools are not assumed to be
        supported -- no mid-conversation ``role: "system"`` message
        (``_has_mid_conversation_system_message`` -- Haiku 400s on the role
        itself, and this is the gate that currently excludes real Claude Code
        2.1.285 traffic, which sends one on every call) -- and an approximate
        context under ``HAIKU_MAX_CONTEXT_TOKENS`` (Haiku 4.5's window is
        200K, the other tiers' is 1M). Whether the turn is simple/mechanical
        is the classifier's call, made by ``decide``.
        """
        if not self.haiku_rewrite:
            return False
        return self._haiku_body_ok(body)

    def _haiku_body_ok(self, body: dict) -> bool:
        """Body-shape eligibility for Haiku (no media, custom tools only, no
        mid-conversation system message, size), independent of the
        ``haiku_rewrite`` flag: a body Haiku would 400 must never be sent to it,
        rewritten or not."""
        if "haiku" not in self.by_name:
            return False
        return haiku_block_reason(body, fold_system=self.haiku_folds_system) == HAIKU_BLOCK_NONE

    # ── the decision ────────────────────────────────────────────────────────

    def _read_quota(self) -> "quota_pressure_mod.QuotaReading":
        """The pressure reading for this call; any failure is ``unknown``."""
        if not self.quota_enabled:
            return quota_pressure_mod.QuotaReading(None, quota_pressure_mod.STATE_OFF)
        try:
            if self._quota is not None:
                return self._quota()
            return quota_pressure_mod.read(max_age_s=self.quota_max_age_s)
        except Exception:  # noqa: BLE001 - fail open: a bad reading never costs a call
            return quota_pressure_mod.QuotaReading(None, quota_pressure_mod.STATE_UNKNOWN)

    def _pressure_cap(self, tier: Tier, pressure: float | None, thinking: str | None,
                      effort: bool) -> Tier | None:
        """The Sonnet tier when ``pressure`` is at or above ``cap_at`` and
        ``tier`` ranks above Sonnet, else None (no change)."""
        cap = self.by_name.get("sonnet")
        if pressure is None or pressure < self.quota_cap_at or cap is None:
            return None
        if self.rank[tier.name] <= self.rank[cap.name] or not self._accepts(cap, thinking, effort):
            return None
        return cap

    async def decide(self, body: dict, session_id: str | None, sticky: Stickiness,
                     classify=None) -> TierDecision:
        """``classify(text) -> {"task_type", "complexity", "chain_head", ...}``
        defaults to ``backends.choose_model(text, None, anthropic=True)``."""
        reading = self._read_quota()
        # Only a fresh measurement drives the step; stale/unknown/off fail open.
        pressure = reading.pressure if reading.state == quota_pressure_mod.STATE_OK else None
        decision = await self._decide(body, session_id, sticky, classify, pressure)
        decision.quota_pressure, decision.quota_state = reading.pressure, reading.state
        return decision

    async def _decide(self, body: dict, session_id: str | None, sticky: Stickiness,
                      classify, pressure: float | None) -> TierDecision:
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
        thinking = (body.get("thinking") or {}).get("type") if isinstance(body.get("thinking"), dict) else None
        thinking = thinking if thinking in THINKING_TYPES else None
        oc = body.get("output_config")
        effort = isinstance(oc, dict) and oc.get("effort") is not None

        def keep_first(reason: str) -> TierDecision:
            # A first-call keep, unless quota pressure caps it: then the capped
            # tier is served and remembered (with the keep's own reason, so the
            # post-first-call handoff still recognises it), and the keep's
            # reason moves to ``detail``. Not a switch: nothing is cached yet.
            cap = self._pressure_cap(req_tier, pressure, thinking, effort)
            if cap is None:
                sticky.record(key, requested, None, reason)
                return keep(reason)
            sticky.record(key, cap.model, None, reason)
            return TierDecision(requested, cap.model, cap.name, REASON_QUOTA_PRESSURE, detail=reason)

        first = is_first_call(body)
        if first and escalation.first_prompt_is_long_or_multi_part(body):
            # Safety default: a first prompt that reads as a multi-part brief
            # rather than a quick question is exactly the shape the trial's
            # own tiering got wrong once (proxy/escalation.py docstring,
            # t7-northstar). Skip the rewrite for THIS call and keep whatever
            # model Claude Code itself requested; the policy still applies
            # normally to every later call in the conversation.
            return keep_first(REASON_LONG_FIRST_PROMPT)
        if first and not self.conversation_level:
            # Per-turn mode: the first call is exempt (the switch cost of a
            # rewrite on the not-yet-cached prefix is the whole prompt).
            return keep_first(REASON_FIRST_CALL)

        if classify is None:
            classify = self._classify
        if classify is None:
            classify = _default_classify

        choice = await classify(tier_text(body))
        task, cx = choice.get("task_type"), choice.get("complexity")

        target = self.tier_for(task, cx) or req_tier
        proposed = target.name  # pre-override: the policy table's own answer
        reason = REASON_POLICY
        if not self.allow_upgrade and self.rank[target.name] > self.rank[req_tier.name]:
            target = req_tier
        haiku_ok = self._haiku_eligible(body)
        if not self._accepts(target, thinking, effort):
            if self._rewritable(target, haiku_ok):
                reason = REASON_HAIKU_REWRITE  # stay on Haiku; the body is rewritten for it
            else:
                floor = next((t for t in self.tiers[self.rank[target.name]:]
                              if self._allowed(t, req_tier, thinking, effort, haiku_ok)), None)
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
            if prev_tier is not None and self._allowed(prev_tier, req_tier, thinking, effort, haiku_ok):
                served, target, reason = prev_model, prev_tier, REASON_STICKY
        if detail is not None:
            # Escalation is exempt from the cap and still reaches Opus; under
            # pressure that is recorded, since it spends the scarce quota.
            if pressure is not None and pressure >= self.quota_cap_at and target is opus_tier:
                reason = REASON_ESCALATION_UNDER_PRESSURE
        else:
            # Quota pressure, last: it caps whatever was chosen above, a tier
            # stickiness would hold included.
            cap = self._pressure_cap(target, pressure, thinking, effort)
            if cap is not None:
                target, served, reason = cap, cap.model, REASON_QUOTA_PRESSURE
            haiku = self.by_name.get("haiku")
            if (pressure is not None and pressure >= self.quota_moderate_at and cx == "moderate"
                    and haiku is not None and self.rank[target.name] > self.rank[haiku.name]
                    and self._haiku_body_ok(body)
                    and self._allowed(haiku, req_tier, thinking, effort, haiku_ok)):
                target, served, reason = haiku, haiku.model, REASON_QUOTA_PRESSURE
        if _canonical(served) == _canonical(requested):
            served = requested  # keep the client's own spelling when nothing changes

        # A genuine first decision (state is None) is not a "switch": nothing
        # was previously cached on a different model to re-write.
        switched = state is not None and _canonical(served) != _canonical(prev_model)
        cost = None
        if switched:
            cost = 0.0 if cold else switch_cost_usd(served, state.prefix_tokens if state else None)
        sticky.record(key, served, cx, reason)
        # Rewrite exactly when the FINAL target is Haiku and the body as sent
        # would not be accepted by it (escalation / stickiness that moved the
        # target off Haiku clear this).
        # With the fold on, a body that carries a system message needs the rewrite even
        # when Haiku takes its thinking/effort as sent.
        needs_rewrite = (not self._accepts(target, thinking, effort)
                         or (self.haiku_folds_system and _has_mid_conversation_system_message(body)))
        body_rewrite = REWRITE_HAIKU if self._rewritable(target, haiku_ok) and needs_rewrite else None
        return TierDecision(requested, served, target.name, reason, switched=switched, switch_cost_usd=cost,
                            body_rewrite=body_rewrite,
                            task_type=task, complexity=cx, chain_head=list(choice.get("chain_head") or [])[:4],
                            complexity_score=choice.get("complexity_score"), detail=detail,
                            proposed_tier=proposed)
