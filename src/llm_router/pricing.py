"""Canonical model pricing — the single source of truth for money in LLM Router.

Every price in this codebase comes from here. Nothing else may define one.

WHY THIS MODULE EXISTS
----------------------
An audit of v1.1.1 found the same stale Opus rate ($15/$75, which is Opus *3*
pricing) living in five independent tables, three of which fed user-visible
savings figures — a 3x overstatement. Git history shows that exact bug being
fixed locally four separate times and returning every time, because each fix
touched one table and left the others alone.

Two design decisions follow from that history, and both matter more than the
numbers themselves:

1. **Keyed by model ID, never by family alias.** The older tables keyed on
   ``"opus"``. That cannot be correct once two Opus versions have different
   prices — which is exactly what happened when Opus 4.5 cut the rate. A
   family-keyed table has no representation for "Opus, but which one", so it is
   guaranteed to be wrong for somebody. Aliases still resolve (see
   :func:`resolve`), but they resolve *to* a model ID; they never carry a price.

2. **Cache rates are derived, not stored.** Anthropic's cache pricing is a fixed
   ratio of the input rate (read 0.1x, write 1.25x at the 5-minute TTL). Storing
   four numbers per model where two plus a formula will do creates three more
   things to get out of sync. Verified against the previously-correct Sonnet
   entry: 3.00 input -> 0.30 read / 3.75 write, matching to the cent.

Enforced by ``scripts/lint_pricing.py`` in CI: a price literal anywhere outside
this module fails the build.

SOURCES
-------
Anthropic list pricing as of ``PRICES_AS_OF``. OpenAI/Google rates are carried
forward from the tables this module replaces and are marked ``verified=False``
where they were not independently confirmed during consolidation — see
:func:`unverified_models`. An unverified price is still a price; it is flagged so
nobody mistakes provenance for accuracy.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass

__all__ = [
    "PRICES_AS_OF",
    "STALENESS_DAYS",
    "Price",
    "price_for",
    "resolve",
    "input_rate",
    "output_rate",
    "cache_read_rate",
    "cache_write_rate",
    "cost_usd",
    "is_free",
    "known_models",
    "unverified_models",
    "staleness_days",
    "is_stale",
    "rates_per_m",
    "output_per_1k",
    "blended_per_1k",
    "SAVINGS_BASELINE_MODEL",
    "savings_baseline_model",
    "savings_baseline_rates",
]

# Date the Anthropic rates below were last confirmed against published pricing.
# Bump this ONLY when the numbers are re-checked, never as a formality — a stale
# date that says "fresh" is worse than an honest old one.
PRICES_AS_OF = _dt.date(2026, 8, 11)

#: Age at which the table is considered stale and callers should warn.
STALENESS_DAYS = 90

# Cache rate ratios, applied to the input rate. Anthropic publishes these as
# fixed multiples, so deriving them removes an entire class of drift.
_CACHE_READ_RATIO = 0.10
_CACHE_WRITE_RATIO = 1.25  # 5-minute TTL; the 1-hour TTL is 2.0x

# ── Savings baseline policy (WP-05) ───────────────────────────────────────────
# THE counterfactual: what the same work would have cost had LLM Router not routed
# it. Exactly one policy, defined here, next to the rates it resolves against.
#
# Three policies used to coexist, two of them introduced to override the other:
# a tiered picker in cost.py (query -> haiku, complex -> opus, else sonnet,
# justified as stopping savings being "overstated"), a flat opus-4-8 table in
# savings_logger.py (justified by the tiered baseline "not reflecting how the
# user actually works" -- the direct negation), and a flat opus list rate in the
# dashboard and session-end hook. For a QUERY task the first credits haiku
# ($1/$5) and the second opus ($5/$25): a 5x spread on the identical call,
# settled by whichever surface happened to render it.
#
# Resolved flat, because the honest counterfactual is what the user would
# ACTUALLY have spent: a Claude Code subscriber runs their top model, they do
# not hand-pick a cheaper Claude per prompt. The tiered version priced a
# counterfactual nobody performs, and it read as conservative while in fact
# comparing against a workflow that did not exist.
#
# This yields the larger number, so it carries the heavier burden: every surface
# reporting it must label WHAT it is measured against, and quota runway must
# never be added to a cash figure.
SAVINGS_BASELINE_MODEL = "claude-opus-5"

#: Env override, retained for back-compat. Routed through this module so no
#: surface reads it directly -- per-surface reads are how the policies diverged.
_SAVINGS_BASELINE_ENV = "LLM_ROUTER_SAVINGS_BASELINE"


@dataclass(frozen=True)
class Price:
    """Per-million-token rates for one model.

    ``cache_read``/``cache_write`` default to ``None`` and are derived from
    ``input`` on access. Set them explicitly only for a provider that does not
    follow the standard ratio.
    """

    model_id: str
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    verified: bool = True
    note: str = ""

    @property
    def cache_read_rate(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * _CACHE_READ_RATIO

    @property
    def cache_write_rate(self) -> float:
        return self.cache_write if self.cache_write is not None else self.input * _CACHE_WRITE_RATIO


# Sonnet 5 runs introductory pricing through 2026-08-31, then reverts. Encoded
# rather than hardcoded to one side: picking "standard" understates cost today,
# picking "intro" overstates it from September. Both are resolved by date, and
# tests pass an explicit date so no test depends on the wall clock.
_SONNET_5_INTRO_UNTIL = _dt.date(2026, 8, 31)
_SONNET_5_INTRO = (2.00, 10.00)
_SONNET_5_STANDARD = (3.00, 15.00)

# The table is split by provider rather than written as one dict, and that is
# load-bearing rather than cosmetic. lint_pricing.py checks for retired rate
# *pairs* within a single assignment, and once this module covers enough models
# two unrelated entries start supplying the two halves of a retired pair by
# coincidence — Qwen3-Coder's $0.25 input beside Gemini Pro's $1.25 input reads
# as the retired $0.25/$1.25 Haiku tier. Per-provider blocks keep the check
# meaningful instead of forcing it to be switched off here, which is the one
# place it must never be switched off.
_ANTHROPIC: dict[str, Price] = {
    # $5/$25 across the current Opus line. The $15/$75 this replaces is Opus 3
    # pricing, retired 2026-01-05 — see the module docstring.
    "claude-opus-5": Price("claude-opus-5", 5.00, 25.00),
    "claude-opus-4-8": Price("claude-opus-4-8", 5.00, 25.00),
    "claude-opus-4-7": Price("claude-opus-4-7", 5.00, 25.00),
    "claude-opus-4-6": Price("claude-opus-4-6", 5.00, 25.00),
    "claude-opus-4-5": Price("claude-opus-4-5", 5.00, 25.00),
    "claude-sonnet-5": Price("claude-sonnet-5", *_SONNET_5_STANDARD, note="intro pricing until 2026-08-31"),
    "claude-sonnet-4-6": Price("claude-sonnet-4-6", 3.00, 15.00),
    "claude-sonnet-4-5": Price("claude-sonnet-4-5", 3.00, 15.00),
    # $1.00/$5.00. The 0.80, 0.25 and 0.25 values this replaces were all wrong.
    "claude-haiku-4-5": Price("claude-haiku-4-5", 1.00, 5.00),
    "claude-fable-5": Price("claude-fable-5", 10.00, 50.00),
    # Retired lines, kept so historical rows still price correctly — never a
    # default. `verified` because a retired line's list price is settled and
    # cannot drift again. That is the opposite of the $15/$75 defect, which was
    # a *live* model being charged at its predecessor's rate.
    #
    # Claude 3.5 Haiku ($0.80/$4.00) and Claude 3 Opus ($15/$75) are absent on
    # purpose. Their real historical rates are byte-identical to the two retired
    # pairs the lint bans, so carrying them would make this module unlintable —
    # and nothing should be routing to either in 2026. They were removed from
    # the bundled catalogue rather than smuggled in here.
    "claude-sonnet-4": Price("claude-sonnet-4", 3.00, 15.00, note="retired line; final list price"),
    "claude-3.5-sonnet": Price("claude-3.5-sonnet", 3.00, 15.00, note="retired line; final list price"),
}

# cache_read is given explicitly wherever a provider does not use Anthropic's
# 0.1x ratio; cache_write is 1.25x everywhere, so it always derives.
_OPENAI: dict[str, Price] = {
    "gpt-5.5": Price("gpt-5.5", 3.00, 12.00, verified=False),
    "gpt-5.4": Price("gpt-5.4", 5.00, 20.00, cache_read=1.25, verified=False),
    "gpt-5-mini": Price("gpt-5-mini", 0.40, 2.00, cache_read=0.10, verified=False),
    "gpt-4o": Price("gpt-4o", 2.50, 10.00, cache_read=1.25, verified=False),
    "gpt-4o-mini": Price("gpt-4o-mini", 0.15, 0.60, cache_read=0.075, verified=False),
    "gpt-4.1": Price("gpt-4.1", 2.00, 8.00, verified=False),
    "gpt-4.1-mini": Price("gpt-4.1-mini", 0.10, 0.40, verified=False),
    # $2/$8, not the $15/$60 three tables still carry. hooks/savings_logger.py
    # had already repriced it locally ("repriced from stale $15/$60") — that
    # single corrected copy is the provenance for this entry, and the fact that
    # one table was fixed while three were not is the whole reason this module
    # exists.
    "o3": Price("o3", 2.00, 8.00, cache_read=0.50, verified=False, note="repriced from the retired $15/$60 tier"),
    "o3-mini": Price("o3-mini", 1.10, 4.40, cache_read=0.275, verified=False),
}

# 2.5-flash and 2.0-flash were previously carried at 0.075/0.30 — which is *1.5*
# Flash's rate. Consolidation is what surfaced it: three tables said 0.075/0.30
# and one said 0.30/2.50, and the odd one out was the correct one.
_GOOGLE: dict[str, Price] = {
    "gemini-2.5-pro": Price("gemini-2.5-pro", 1.25, 10.00, cache_read=0.31, verified=False),
    "gemini-2.5-flash": Price("gemini-2.5-flash", 0.30, 2.50, cache_read=0.075, verified=False),
    "gemini-2.5-flash-lite": Price("gemini-2.5-flash-lite", 0.10, 0.40, cache_read=0.025, verified=False),
    "gemini-2.0-pro": Price("gemini-2.0-pro", 1.25, 5.00, cache_read=0.31, verified=False),
    "gemini-2.0-flash": Price("gemini-2.0-flash", 0.10, 0.40, cache_read=0.025, verified=False),
    "gemini-1.5-pro": Price("gemini-1.5-pro", 1.25, 5.00, cache_read=0.31, verified=False),
    "gemini-1.5-flash": Price("gemini-1.5-flash", 0.075, 0.30, cache_read=0.019, verified=False),
    "gemini-1.5-flash-8b": Price("gemini-1.5-flash-8b", 0.0375, 0.15, verified=False),
    "gemini-3.1-flash-lite": Price("gemini-3.1-flash-lite", 0.10, 0.40, verified=False),
}

# Open-weight / third-party pools (OpenRouter, Groq, DeepSeek, Perplexity).
# Approximated from public listings; the policy diff tolerates ~20% drift before
# it misranks, which is why these are flagged rather than trusted.
_OPEN_WEIGHT: dict[str, Price] = {
    "qwen3-235b-a22b-2507": Price("qwen3-235b-a22b-2507", 0.15, 0.55, verified=False),
    "qwen3-coder-next": Price("qwen3-coder-next", 0.25, 0.90, verified=False),
    "qwen3-next-80b-a3b-instruct": Price("qwen3-next-80b-a3b-instruct", 0.10, 0.40, verified=False),
    "grok-4.3": Price("grok-4.3", 0.50, 1.50, verified=False),
    "deepseek-v4-flash": Price("deepseek-v4-flash", 0.07, 0.50, verified=False),
    "deepseek-chat": Price("deepseek-chat", 0.27, 1.10, verified=False),
    "deepseek-reasoner": Price("deepseek-reasoner", 0.55, 2.19, verified=False),
    "llama-3.3-70b-versatile": Price("llama-3.3-70b-versatile", 0.59, 0.79, verified=False),
    # These three reached this module from a table that stored only a per-1K
    # *output* figure. That output rate is carried forward unchanged so no
    # displayed number moves on a rate nobody could confirm; the input rate is an
    # estimate and is labelled as one rather than quietly presented as data.
    "deepseek-v4-pro": Price("deepseek-v4-pro", 0.55, 2.60, verified=False, note="input estimated; output carried forward"),
    "mistral-large-latest": Price("mistral-large-latest", 2.00, 8.00, verified=False, note="input estimated; output carried forward"),
    "grok-3": Price("grok-3", 3.00, 9.00, verified=False, note="input estimated; output carried forward"),
    "sonar-pro": Price("sonar-pro", 3.00, 15.00, verified=False),
    # $1/$1 flat, taken from the bundled model catalogue's entry rather than
    # guessed — the search backend is priced into the flat rate.
    "sonar": Price("sonar", 1.00, 1.00, verified=False),
    "mistral-small-latest": Price("mistral-small-latest", 0.20, 0.60, verified=False),
    "command-r-plus": Price("command-r-plus", 2.50, 10.00, verified=False),
}

# Genuinely zero, not "unknown". Callers must not conflate the two.
_LOCAL: dict[str, Price] = {
    "ollama": Price("ollama", 0.0, 0.0),
}

_PRICES: dict[str, Price] = {**_ANTHROPIC, **_OPENAI, **_GOOGLE, **_OPEN_WEIGHT, **_LOCAL}

# Family aliases and legacy spellings -> model ID. An alias NEVER carries a
# price: that is the defect this module exists to prevent. Family names resolve
# to the current member of that family, so "opus" tracks the ladder instead of
# freezing at whichever version was current when someone typed it.
_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-opus-4-8-fast": "claude-opus-4-8",
    "claude-opus-5-fast": "claude-opus-5",
}


def _normalize(model: str) -> str:
    """Strip provider prefixes and vendor decorations, lowercase."""
    m = (model or "").strip().lower()
    for prefix in ("anthropic/", "openai/", "google/", "gemini/", "ollama/", "litellm/"):
        if m.startswith(prefix):
            m = m[len(prefix) :]
            break
    return m


def resolve(model: str) -> str | None:
    """Canonical model ID for ``model``, or ``None`` if unknown.

    ``None`` means *unknown*, which is not the same as free. Callers must
    surface unknown as unknown rather than coercing it to zero — a zero price
    silently turns missing knowledge into a favourable number.
    """
    m = _normalize(model)
    if m in _PRICES:
        return m
    if m in _ALIASES:
        return _ALIASES[m]
    # Any remaining "vendor/model" spelling: callers write the same model as
    # "o3", "openai/o3" and "deepseek/deepseek-chat" depending on which registry
    # they came from. Strip one leading segment rather than enumerate vendors —
    # an unlisted vendor prefix silently returning None is how a priced model
    # becomes an unpriced one.
    if "/" in m:
        tail = m.split("/", 1)[1]
        if tail in _PRICES:
            return tail
        if tail in _ALIASES:
            return _ALIASES[tail]
    if m.startswith("ollama") or ":" in m:
        # Ollama tags look like "qwen2.5-coder:7b" — local, and free.
        return "ollama"
    return None


def price_for(model: str, *, as_of: _dt.date | None = None) -> Price | None:
    """:class:`Price` for ``model``, or ``None`` when unknown.

    ``as_of`` selects time-dependent rates (currently only Sonnet 5's
    introductory period). Pass it explicitly in tests so no assertion depends on
    the wall clock.
    """
    key = resolve(model)
    if key is None:
        return None
    price = _PRICES[key]
    if key == "claude-sonnet-5":
        today = as_of or _dt.date.today()
        if today <= _SONNET_5_INTRO_UNTIL:
            return Price(key, *_SONNET_5_INTRO, note=f"introductory pricing through {_SONNET_5_INTRO_UNTIL}")
    return price


def input_rate(model: str, *, as_of: _dt.date | None = None) -> float | None:
    p = price_for(model, as_of=as_of)
    return None if p is None else p.input


def output_rate(model: str, *, as_of: _dt.date | None = None) -> float | None:
    p = price_for(model, as_of=as_of)
    return None if p is None else p.output


def cache_read_rate(model: str, *, as_of: _dt.date | None = None) -> float | None:
    p = price_for(model, as_of=as_of)
    return None if p is None else p.cache_read_rate


def cache_write_rate(model: str, *, as_of: _dt.date | None = None) -> float | None:
    p = price_for(model, as_of=as_of)
    return None if p is None else p.cache_write_rate


def cost_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    *,
    as_of: _dt.date | None = None,
) -> float | None:
    """Total USD for a call, or ``None`` when the model's price is unknown.

    Returning ``None`` rather than ``0.0`` is deliberate and load-bearing: a
    caller that cannot price a call must say so. Coercing to zero is how an
    unpriced model silently becomes free and inflates reported savings.
    """
    p = price_for(model, as_of=as_of)
    if p is None:
        return None
    return (
        (input_tokens / 1_000_000) * p.input
        + (output_tokens / 1_000_000) * p.output
        + (cache_read_tokens / 1_000_000) * p.cache_read_rate
        + (cache_write_tokens / 1_000_000) * p.cache_write_rate
    )


def rates_per_m(model: str, *, as_of: _dt.date | None = None) -> dict[str, float] | None:
    """The four per-million rates as a plain dict, or ``None`` when unknown.

    Exists so the tables this module replaced can be *derived* rather than
    retyped. Callers that already speak
    ``{"input", "output", "cache_read", "cache_write"}`` keep their shape and
    lose their literals.
    """
    p = price_for(model, as_of=as_of)
    if p is None:
        return None
    return {
        "input": p.input,
        "output": p.output,
        "cache_read": p.cache_read_rate,
        "cache_write": p.cache_write_rate,
    }


def output_per_1k(model: str, *, as_of: _dt.date | None = None) -> float | None:
    """Output-token cost per 1K tokens. ``None`` when unknown."""
    r = output_rate(model, as_of=as_of)
    return None if r is None else r / 1000.0


def blended_per_1k(model: str, *, as_of: _dt.date | None = None) -> float | None:
    """Rough per-1K cost at an even input/output mix. ``None`` when unknown.

    A single blended number is a *ranking* aid, not a bill. It is offered here
    only because several call sites already wanted one and each had invented its
    own mix — an even split is what the majority of them were already using
    (Opus $15/$75 -> 0.045, Sonnet $3/$15 -> 0.009). Anything charging a user
    must price the components separately via :func:`cost_usd`.
    """
    p = price_for(model, as_of=as_of)
    return None if p is None else (p.input + p.output) / 2.0 / 1000.0


def is_free(model: str) -> bool:
    """True only for models that are *known* to cost nothing.

    An unknown model is not free — it is unknown, and this returns False.
    """
    p = price_for(model)
    return p is not None and p.input == 0.0 and p.output == 0.0


def known_models() -> frozenset[str]:
    return frozenset(_PRICES)


def unverified_models() -> frozenset[str]:
    """Models whose rates were carried forward without independent confirmation."""
    return frozenset(k for k, v in _PRICES.items() if not v.verified)


def savings_baseline_model() -> str:
    """The one model every savings figure is measured against.

    Honours ``LLM_ROUTER_SAVINGS_BASELINE`` for back-compat, but only when the value
    resolves to a model this table actually prices. An unrecognised override
    falls back to the default rather than propagating: an unpriced baseline
    yields a 0.0 rate, which renders every routed call as having saved nothing —
    the exact "a failure that looks like data" shape RED2-02 was raised over.
    """
    override = os.environ.get(_SAVINGS_BASELINE_ENV, "").strip().lower()
    if override:
        resolved = resolve(override)
        if resolved is not None:
            return resolved
    return SAVINGS_BASELINE_MODEL


def savings_baseline_rates() -> tuple[float, float]:
    """``(input_per_m, output_per_m)`` for :func:`savings_baseline_model`."""
    model = savings_baseline_model()
    in_rate = input_rate(model)
    out_rate = output_rate(model)
    if in_rate is None or out_rate is None:  # pragma: no cover — resolve() gates this
        in_rate = input_rate(SAVINGS_BASELINE_MODEL)
        out_rate = output_rate(SAVINGS_BASELINE_MODEL)
    return float(in_rate), float(out_rate)


def staleness_days(*, as_of: _dt.date | None = None) -> int:
    return ((as_of or _dt.date.today()) - PRICES_AS_OF).days


def is_stale(*, as_of: _dt.date | None = None) -> bool:
    """True when the table is older than :data:`STALENESS_DAYS`.

    LLM Router reports money to users. A price table nobody has re-checked in a
    quarter should say so out loud rather than quietly keep reporting.
    """
    if os.environ.get("LLM_ROUTER_SUPPRESS_PRICING_STALENESS"):
        return False
    return staleness_days(as_of=as_of) > STALENESS_DAYS
