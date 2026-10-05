"""The resolver: tier + needs + the user's real setup -> a model, or an honest "none".

Order of decisions (and why):

1. HARD ELIGIBILITY, never averaged. A model that fails any of these is out, and
   every failing reason is recorded: present in the inventory, privacy, authorized,
   execution path verified, quota not benched/exhausted, each required capability,
   context fits. An unknown fact fails the check (unknown is not "fine").
2. TIER QUALIFICATION. The model's measured (or, weaker, prior) ceiling must reach
   the requested tier. Models that pass step 1 but sit below the tier are NOT chosen;
   they appear on the ladder flagged ``below_tier`` with a warning.
3. PICK among the qualified: tightest tier fit (do not spend FRONTIER on EASY),
   not quota-pressured, measured over prior, cheaper route, cheaper price, lower
   pressure, id.
4. LADDER: the other qualified models, then below-tier models (flagged), then the
   user's configured model (``keep_configured``) or "ask the user". With nothing
   qualified the result is ``no_eligible`` and ``model`` is None: the caller keeps
   the configured model or asks, it is never silently downgraded.

Nothing here executes a model or touches the network; it is a pure function of the
:class:`Setup` it is given, so it is testable table-style.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from llm_router.resolver.types import (
    CAP_NO,
    CAP_YES,
    PRIVACY_LOCAL,
    ROUTE_API,
    ROUTE_LOCAL,
    SRC_MEASURED,
    Inventory,
    ModelEntry,
    Tier,
)

#: Pressure at which a subscription route is demoted behind alternatives (the
#: proxy steps down from Opus at 0.70/0.90; this is the resolver's own, higher bar).
PRESSURE_DEMOTE = 0.85
#: Pressure at which a route is treated as exhausted (the 99% hard cap in budget.py).
PRESSURE_EXHAUSTED = 0.99
DEFAULT_MAX_INVENTORY_AGE_S = 24 * 3600.0
LONG_CONTEXT_TOKENS = 128_000

STATUS_ROUTED = "routed"
STATUS_NO_ELIGIBLE = "no_eligible"

_COST_RANK = {ROUTE_LOCAL: 0, "subscription": 1, ROUTE_API: 2}


@dataclass(frozen=True)
class Needs:
    tools: bool = False
    vision: bool = False
    thinking: bool = False
    json: bool = False
    context_tokens: int | None = None
    local_only: bool = False

    def __post_init__(self) -> None:
        if self.context_tokens is not None and self.context_tokens <= 0:
            raise ValueError(f"context_tokens must be positive, got {self.context_tokens}")

    @classmethod
    def parse(cls, spec: str | None) -> "Needs":
        """``"tools,vision,ctx=64000,local"``. ``long_context`` means 128k tokens."""
        kw: dict = {}
        for raw in (spec or "").split(","):
            tok = raw.strip().lower().replace("_", "-")
            if not tok:
                continue
            if tok in ("tools", "vision", "thinking", "json"):
                kw[tok] = True
            elif tok in ("local", "local-only", "privacy=local-only", "privacy=local"):
                kw["local_only"] = True
            elif tok == "long-context":
                kw["context_tokens"] = LONG_CONTEXT_TOKENS
            elif tok.startswith("ctx="):
                try:
                    n = int(tok[4:])
                except ValueError:
                    raise ValueError(f"bad context size in {raw!r}") from None
                if n <= 0:
                    raise ValueError(f"context size must be positive: {raw!r}")
                kw["context_tokens"] = n
            else:
                raise ValueError(
                    f"unknown need {raw!r}; expected tools, vision, thinking, json, "
                    "long-context, ctx=N, local-only")
        return cls(**kw)

    def describe(self) -> str:
        parts = [n for n in ("tools", "vision", "thinking", "json") if getattr(self, n)]
        if self.context_tokens:
            parts.append(f"ctx>={self.context_tokens}")
        if self.local_only:
            parts.append("local-only")
        return ",".join(parts) or "none"


@dataclass
class Setup:
    inventory: Inventory
    configured_model: str | None = None


@dataclass(frozen=True)
class Rung:
    kind: str                 # "model" | "configured" | "ask_user"
    model: str | None
    route: str | None
    below_tier: bool = False
    note: str = ""


@dataclass(frozen=True)
class Rejection:
    model: str
    reasons: tuple[str, ...]


@dataclass
class Resolution:
    status: str
    tier: str
    needs: Needs
    model: str | None
    route: str | None
    fallbacks: list[Rung] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""
    rejected: list[Rejection] = field(default_factory=list)
    keep_configured: str | None = None

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


# ------------------------------------------------------------------ eligibility

def _cap_failure(m: ModelEntry, name: str) -> str | None:
    c = m.cap(name)
    if c.state == CAP_YES:
        return None
    if c.state == CAP_NO:
        how = "measured" if c.source == SRC_MEASURED else "declared"
        return f"needs {name}: {how} as unsupported"
    return f"needs {name}: capability unknown (not reported, not measured)"


def hard_failures(m: ModelEntry, needs: Needs, now: float) -> list[str]:
    """Every hard-eligibility check this model fails. Empty means eligible."""
    out: list[str] = []
    if not m.present:
        out.append("removed since the last inventory")
    if needs.local_only and m.privacy != PRIVACY_LOCAL:
        out.append("privacy=local-only but this model is cloud-hosted")
    if not m.authorized:
        out.append(f"not authorized ({m.auth_detail or 'no credential'})")
    if not m.path_verified:
        out.append(f"execution path not verified ({m.path_detail or 'no evidence'})")
    q = m.quota
    if q.state == "benched":
        until = ""
        if q.benched_until:
            left = max(0, int(q.benched_until - now))
            until = f" for another {left // 3600}h{left % 3600 // 60:02d}m"
        out.append(f"provider is benched after a reported usage limit{until}")
    elif q.pressure is not None and q.pressure >= PRESSURE_EXHAUSTED:
        out.append(f"quota exhausted ({q.pressure * 100:.0f}% used)")
    for name in ("tools", "vision", "thinking", "json"):
        if getattr(needs, name):
            why = _cap_failure(m, name)
            if why:
                out.append(why)
    if needs.context_tokens:
        eff = m.effective_context
        if eff is None:
            out.append(f"needs {needs.context_tokens} tokens of context: window unknown")
        elif eff < needs.context_tokens:
            out.append(f"needs {needs.context_tokens} tokens of context: only {eff} usable")
    return out


def _ceiling_rank(m: ModelEntry, allow_unmeasured: bool) -> int | None:
    if m.tier_ceiling:
        return Tier.parse(m.tier_ceiling).rank
    if m.tier_basis == "unmeasured" and allow_unmeasured:
        return Tier.EASY.rank
    return None


def _pressured(m: ModelEntry) -> bool:
    return m.quota.pressure is not None and m.quota.pressure >= PRESSURE_DEMOTE


def _price(m: ModelEntry) -> float:
    return (m.price_in_per_mtok or 0.0) + (m.price_out_per_mtok or 0.0)


def _sort_key(m: ModelEntry, want_rank: int, allow_unmeasured: bool):
    rank = _ceiling_rank(m, allow_unmeasured) or 0
    return (rank - want_rank, _pressured(m), m.tier_basis != "measured",
            _COST_RANK.get(m.route_kind, 9), _price(m), m.quota.pressure or 0.0, m.id)


# ---------------------------------------------------------------------- resolve

def resolve(
    tier: Tier | str,
    needs: Needs | None,
    setup: Setup,
    *,
    now: float | None = None,
    allow_unmeasured: bool = False,
    require_measured: bool = False,
    max_inventory_age_s: float = DEFAULT_MAX_INVENTORY_AGE_S,
) -> Resolution:
    t = tier if isinstance(tier, Tier) else Tier.parse(tier)
    needs = needs or Needs()
    now = time.time() if now is None else now
    inv = setup.inventory
    warnings: list[str] = []
    rejected: list[Rejection] = []

    age = now - inv.generated_at
    if age > max_inventory_age_s:
        warnings.append(f"inventory is {age / 3600:.1f}h old; models may have changed (run `llm-router inventory`)")

    configured = setup.configured_model
    if configured and (cm := inv.get(configured)) is not None and not cm.present:
        warnings.append(f"configured model {configured} was removed since the last inventory")

    eligible: list[ModelEntry] = []
    for m in inv.models:
        fails = hard_failures(m, needs, now)
        if fails:
            rejected.append(Rejection(m.id, tuple(fails)))
        else:
            eligible.append(m)

    qualified: list[ModelEntry] = []
    below: list[ModelEntry] = []
    for m in eligible:
        rank = _ceiling_rank(m, allow_unmeasured)
        if rank is None:
            why = ("failed the EASY probes" if m.tier_basis == "measured"
                   else "not measured, so no tier is qualified (run `llm-router calibrate`)")
            rejected.append(Rejection(m.id, (why,)))
        elif require_measured and m.tier_basis != "measured":
            rejected.append(Rejection(m.id, ("require_measured: its tier rests on a prior, not a measurement",)))
        elif rank >= t.rank:
            qualified.append(m)
        else:
            below.append(m)

    qualified.sort(key=lambda m: _sort_key(m, t.rank, allow_unmeasured))
    below.sort(key=lambda m: (-(_ceiling_rank(m, allow_unmeasured) or 0),)
               + _sort_key(m, t.rank, allow_unmeasured)[1:])

    ladder = _tail(configured, needs, inv, warnings)

    if not qualified:
        rungs = [Rung("model", m.id, m.route_kind, True,
                      f"{m.tier_ceiling or 'EASY (assumed)'} < requested {t.value}") for m in below]
        if below:
            warnings.append(
                f"no model qualifies for {t.value}; {len(below)} lower-tier model(s) are on the ladder "
                "flagged below_tier and are NOT selected automatically")
        reason = (f"no eligible model for {t.value} (needs: {needs.describe()}): "
                  f"{len(inv.models)} model(s) in the inventory, {len(rejected)} rejected by hard checks"
                  f"{', ' + str(len(below)) + ' below the tier' if below else ''}")
        return Resolution(STATUS_NO_ELIGIBLE, t.value, needs, None, None, rungs + ladder,
                          warnings, reason, rejected, _keep(configured, needs, inv))

    best = qualified[0]
    warnings.extend(_model_warnings(best, needs, t))
    best_fit = _sort_key(best, t.rank, allow_unmeasured)[0]
    skipped = [m for m in qualified[1:] if not _pressured(best) and _pressured(m)
               and _sort_key(m, t.rank, allow_unmeasured)[0] == best_fit]
    reason = (f"{best.id}: qualified for {t.value} ({best.tier_ceiling}, {best.tier_basis}); "
              f"tightest tier fit among {len(qualified)} qualified, route {best.route_kind}")
    for m in skipped:
        reason += f"; {m.id} deprioritised at {m.quota.pressure * 100:.0f}% quota pressure"
    rungs = [Rung("model", m.id, m.route_kind, False, "same tier") for m in qualified[1:]]
    rungs += [Rung("model", m.id, m.route_kind, True,
                   f"{m.tier_ceiling or 'EASY (assumed)'} < requested {t.value}") for m in below]
    if below:
        warnings.append(f"ladder rungs below {t.value} are a downgrade; using one needs an explicit decision")
    return Resolution(STATUS_ROUTED, t.value, needs, best.id, best.route_kind, rungs + ladder,
                      warnings, reason, rejected, _keep(configured, needs, inv))


def _keep(configured: str | None, needs: Needs, inv: Inventory) -> str | None:
    """The configured model, when keeping it does not violate the request."""
    if not configured:
        return None
    m = inv.get(configured)
    if needs.local_only and (m is None or m.privacy != PRIVACY_LOCAL):
        return None
    return configured


def _tail(configured: str | None, needs: Needs, inv: Inventory, warnings: list[str]) -> list[Rung]:
    if not configured:
        return [Rung("ask_user", None, None, False, "no configured model to keep; ask the user to choose")]
    if _keep(configured, needs, inv) is None:
        warnings.append(
            f"configured model {configured} cannot be kept under privacy=local-only "
            "(not known to be local); ask the user")
        return [Rung("ask_user", None, None, False,
                     f"configured model {configured} is not known to be local")]
    return [Rung("configured", configured, None, False, "keep the user's configured model")]


def _model_warnings(m: ModelEntry, needs: Needs, t: Tier) -> list[str]:
    out: list[str] = []
    if m.tier_basis == "unmeasured":
        out.append(f"{m.id}: no measurement and no registry class; EASY is an assumption "
                   "(run `llm-router calibrate`)")
    if m.tier_basis == "prior":
        out.append(f"{m.id}: its {m.tier_ceiling} ceiling comes from the curated registry, not a "
                   "measurement on this setup (run `llm-router calibrate`)")
    if m.quota.pressure is not None and m.quota.pressure >= PRESSURE_DEMOTE:
        out.append(f"{m.id}: quota pressure {m.quota.pressure * 100:.0f}%; it is the best qualified "
                   "option but close to its limit")
    elif m.route_kind == "subscription" and m.quota.pressure is None:
        out.append(f"{m.id}: quota pressure unknown ({m.quota.detail or 'no reading'})")
    if m.route_kind == ROUTE_API:
        out.append(f"{m.id}: metered API route; each call costs money")
    for name in ("tools", "vision", "thinking", "json"):
        if getattr(needs, name) and m.cap(name).source != SRC_MEASURED:
            out.append(f"{m.id}: {name} is declared, not measured here")
    if needs.context_tokens and m.measured_recall_tokens is None:
        out.append(f"{m.id}: context window is declared; long-context recall was not measured")
    return out
