"""Configuration loaded from environment variables.

Uses Pydantic Settings to load API keys and router preferences from
environment variables (and optionally a ``.env`` file). The configuration
is accessed via the ``get_config()`` singleton, which also calls
``apply_keys_to_env()`` on first load to export keys into ``os.environ``
where LiteLLM expects them.

Configuration is organized into five sections:
  1. **Text LLM providers** — API keys for OpenAI, Anthropic, Gemini, etc.
  2. **Media providers** — API keys for fal, Stability, ElevenLabs, etc.
  3. **Router settings** — profile, tier, budget, database path.
  4. **Smart routing** — token budget, quality mode, min model floor.
  5. **Health / defaults** — circuit breaker tuning, request defaults.
"""

from __future__ import annotations

import threading
import time
import urllib.request
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings

from llm_router.paths import state_path
from llm_router.types import QualityMode, RoutingProfile, Tier
from llm_router.routing_hints import validate_config_upgrade, log_routing_decision

# ── Ollama reachability cache ─────────────────────────────────────────────────
# Checked at most once per TTL to avoid a network call on every routing
# decision. Starts as None so the first call always does a live probe.
_ollama_reachable_cache: bool | None = None
_ollama_cache_time: float = 0.0
_OLLAMA_PROBE_TTL = 60.0  # seconds


def probe_ollama(base_url: str) -> bool:
    """Return True if Ollama is reachable, with a 60-second result cache.

    The result is cached to avoid a network probe on every call to
    ``available_providers``, which is invoked per routing request.
    Cache is invalidated after ``_OLLAMA_PROBE_TTL`` seconds so that a
    freshly-started Ollama process is detected within one minute.

    Args:
        base_url: Ollama base URL, e.g. ``"http://localhost:11434"``.

    Returns:
        True if Ollama responds to ``GET /api/tags`` within 1 second.
    """
    global _ollama_reachable_cache, _ollama_cache_time
    now = time.monotonic()
    if _ollama_reachable_cache is not None and (now - _ollama_cache_time) < _OLLAMA_PROBE_TTL:
        return _ollama_reachable_cache
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=1):
            _ollama_reachable_cache = True
    except Exception:
        _ollama_reachable_cache = False
    _ollama_cache_time = now
    return _ollama_reachable_cache


# ── pxpipe reachability cache ──────────────────────────────────────────────
# Same rationale as probe_ollama above — this runs from a sync quirk hook
# on every dispatch to a heavy model, so it must not do a live network
# probe each time.
_pxpipe_reachable_cache: bool | None = None
_pxpipe_cache_time: float = 0.0
_PXPIPE_PROBE_TTL = 60.0  # seconds


def probe_pxpipe(base_url: str) -> bool:
    """Return True if a pxpipe proxy is reachable, with a 60-second cache.

    Args:
        base_url: pxpipe proxy base URL, e.g. ``"http://127.0.0.1:47821"``.

    Returns:
        True if the pxpipe dashboard responds within 1 second.
    """
    global _pxpipe_reachable_cache, _pxpipe_cache_time
    now = time.monotonic()
    if _pxpipe_reachable_cache is not None and (now - _pxpipe_cache_time) < _PXPIPE_PROBE_TTL:
        return _pxpipe_reachable_cache
    try:
        with urllib.request.urlopen(base_url, timeout=1):
            _pxpipe_reachable_cache = True
    except Exception:
        _pxpipe_reachable_cache = False
    _pxpipe_cache_time = now
    return _pxpipe_reachable_cache


def validate_ollama_url(url: str) -> str:
    """CHZ-SEC-06: reject unsafe Ollama endpoints before any urlopen.

    ``LLM_ROUTER_OLLAMA_URL`` / ``OLLAMA_URL`` reached ``urlopen`` with no scheme or
    host validation, so ``file://`` was accepted (local file read) and
    cloud-metadata addresses (169.254.169.254, ::ffff:169.254.169.254) were
    attempted — a classic SSRF sink. Returns the URL unchanged when safe, or ``""``
    (Ollama disabled) when not. Only ``http``/``https`` to a non-metadata,
    non-link-local host are allowed.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except Exception:
        return ""
    if p.scheme not in ("http", "https"):
        return ""
    host = (p.hostname or "").lower()
    if not host:
        return ""
    # Block cloud-metadata + link-local + unspecified addresses.
    _BLOCKED_HOSTS = {
        "169.254.169.254", "metadata.google.internal", "metadata",
        "0.0.0.0", "::", "[::]",
    }
    if host in _BLOCKED_HOSTS:
        return ""
    if host.startswith("169.254.") or host.startswith("fe80:") or "169.254.169.254" in host:
        return ""
    return url


# ── GH#69: LLM_ROUTER_PROFILE collision, third reader ──────────────────────
#
# ``RouterConfig.llm_router_profile`` is the field that actually drives live
# routing (router.py, orchestrator.py, state.py, tools/routing.py, the
# dashboard, ...) — unlike ``repo_config.RepoConfig.effective_profile()``,
# whose only caller is the ``llm_router config`` display (GH#65 fixed THAT
# reader by adding ``LLM_ROUTER_COST_PROFILE`` with a value-domain-filtered
# fallback to the legacy name). This field is a THIRD, independent reader:
# pydantic-settings binds it to ``LLM_ROUTER_PROFILE`` purely by naming
# convention and validates it strictly against ``RoutingProfile`` — so any
# value outside the six routing tiers (most notably ``enterprise``/
# ``developer``, which are ``llm_router.profile`` / ``identity.py``'s
# UNRELATED deployment-identity axis) raised ``pydantic_core.ValidationError``
# at ``RouterConfig()`` construction time, which server.py triggers at
# IMPORT time — so the MCP server could not boot at all under the one env
# value historically documented to select the enterprise profile.
#
# Fix: apply the same value-domain filter GH#65 established, extended so
# this field (the one that matters for real routing) finally honors
# ``LLM_ROUTER_COST_PROFILE`` too, with precedence over the legacy name —
# via ``validation_alias`` below — and a ``mode="before"`` validator that
# NEVER raises for an unrecognized value: it warns once and falls back to
# the default profile instead. A value that is merely unrecognized must
# not prevent the server from starting.
_VALID_LLM_ROUTER_PROFILES = {p.value for p in RoutingProfile}
_llm_router_profile_fallback_warning_emitted = False


def _maybe_emit_llm_router_profile_fallback_warning(value: str) -> None:
    """One-shot stderr warning when ``llm_router_profile`` falls back.

    Mirrors the latch pattern in ``llm_router.profile`` /
    ``llm_router.repo_config`` (``_maybe_emit_legacy_warning`` /
    ``_maybe_emit_legacy_cost_profile_warning``) so a long-running process
    emits the message once rather than on every ``RouterConfig()`` build.
    """
    global _llm_router_profile_fallback_warning_emitted
    if _llm_router_profile_fallback_warning_emitted:
        return
    _llm_router_profile_fallback_warning_emitted = True
    import sys
    sys.stderr.write(
        f"[llm_router] WARNING: LLM_ROUTER_PROFILE (or LLM_ROUTER_COST_PROFILE) "
        f"is set to {value!r}, which is not a valid routing tier "
        f"({sorted(_VALID_LLM_ROUTER_PROFILES)}). Falling back to the default "
        f"profile ({RoutingProfile.BALANCED.value!r}) instead of crashing. "
        "If you meant the deployment-identity axis (developer/enterprise), "
        "set LLM_ROUTER_DEPLOYMENT_PROFILE instead (see llm_router.profile). "
        "If you meant a routing cost tier, use LLM_ROUTER_COST_PROFILE and "
        "rename/remove the stale LLM_ROUTER_PROFILE (GH#65/GH#69).\n"
    )


def _reset_llm_router_profile_fallback_warning_latch() -> None:
    """Test affordance — reset the one-shot latch. Not part of the public API."""
    global _llm_router_profile_fallback_warning_emitted
    _llm_router_profile_fallback_warning_emitted = False


class RouterConfig(BaseSettings):
    """Central configuration for the LLM Router.

    All fields are loaded from environment variables (case-insensitive) or a
    ``.env`` file. Providers with empty API keys are considered unconfigured
    and excluded from routing.
    """

    # ── Text LLM providers ──
    openai_api_key: str = ""
    gemini_api_key: str = ""
    perplexity_api_key: str = ""
    anthropic_api_key: str = ""
    mistral_api_key: str = ""
    deepseek_api_key: str = ""
    groq_api_key: str = ""
    together_api_key: str = ""
    xai_api_key: str = ""
    cohere_api_key: str = ""
    # ── New leaderboard providers (OpenAI-compatible) ──
    moonshot_api_key: str = ""   # Kimi (Moonshot AI) — api.moonshot.cn/v1
    minimax_api_key: str = ""    # MiniMax — api.minimax.chat/v1
    zhipu_api_key: str = ""      # Z AI / Zhipu — open.bigmodel.cn/api/paas/v4
    arcee_api_key: str = ""      # ArceeAI — conductor.arcee.ai/v1
    # Plan 06 Step 2 — OpenRouter aggregator (qwen/deepseek/gemini-flash-lite/etc).
    # LiteLLM reads OPENROUTER_API_KEY directly so no explicit env propagation needed;
    # we surface it here only to gate `available_providers` and to enable the
    # `~/.llm-router/config.yaml` fallback path used by enterprise installs.
    openrouter_api_key: str = ""

    # ── Claude Pro/Max subscription ──
    # Set to True when using llm_router inside Claude Code (Pro/Max subscription).
    # When enabled, all anthropic/* models are EXCLUDED from routing chains.
    # Rationale: you are already using Claude Code — routing back to Claude via
    # the Anthropic API would require a SEPARATE API key AND additional billing.
    # The router's job in this mode is to route tasks to non-Claude alternatives
    # (Codex, Ollama, Gemini, GPT-4o, Perplexity, etc.) to save your Claude quota.
    llm_router_claude_subscription: bool = False

    # ── Claude offload pressure cap ──
    # Max COMBINED Claude subscription pressure (5h session + weekly, from
    # claude_usage.get_claude_pressure(), 0.0-1.0) at which Claude may still be
    # used for OFFLOAD (the llm_* tools, via the claude CLI). Above this, anthropic/*
    # is dropped from offload chains so offload never starves your primary Claude Code
    # work. 1.0 = always allow; 0.0 = disable Claude offload entirely.
    # Env: LLM_ROUTER_CLAUDE_OFFLOAD_MAX_PRESSURE.
    llm_router_claude_offload_max_pressure: float = 0.80

    # ── Gemini Subscription (Google One AI Pro) (v9.0.1) ──
    # Set to True when using llm_router inside Gemini CLI with a subscription.
    # When enabled, all gemini/* models (API) are EXCLUDED from routing chains.
    # Instead, the router uses gemini_cli/* to route tasks back to Gemini via
    # the local binary (free via subscription).
    llm_router_gemini_subscription: bool = False

    # ── claw-code mode ──
    # Set to True when running inside claw-code (open-source Claude alternative).
    # In claw-code every API call is paid — there is no subscription quota.
    # Effect: Ollama is injected at the front of ALL routing chains (not just
    # BUDGET or when Claude quota is high) so free local inference is always
    # tried first before spending money on cloud APIs.
    # Set automatically by `llm_router install` when ~/.claw-code/ is detected.
    llm_router_claw_code: bool = False

    # ── pxpipe integration (heavy-model context compression) ──
    # pxpipe (https://github.com/teamchong/pxpipe) is a local proxy that
    # rewrites bulky request context (system prompt, tool docs, older
    # history) into compact PNGs before it reaches Claude's API — image
    # tokens are cheaper than dense text tokens at Anthropic's pricing, so
    # this cuts the bill on expensive, high-token-count calls. Opt-in and
    # off by default: it requires `npx pxpipe-proxy` running locally, and
    # only pays off for API-key-mode dispatch to the specific "heavy" models
    # listed in llm_router_pxpipe_heavy_models — LLM Router never makes a real
    # network call to Anthropic in subscription mode (see
    # llm_router_claude_subscription above), so this has no effect there.
    llm_router_pxpipe_enabled: bool = False

    # Local pxpipe proxy endpoint. Matches pxpipe's own default port.
    llm_router_pxpipe_url: str = "http://127.0.0.1:47821"

    # Comma-separated model names (bare, no provider prefix) to route through
    # pxpipe when llm_router_pxpipe_enabled is True. Deliberately mirrors
    # pxpipe's own conservative default (PXPIPE_MODELS=claude-fable-5,gpt-5.6)
    # rather than every Claude model — Opus 4.7/4.8 has a documented ~7%
    # image-misread rate and is opt-in even within pxpipe itself.
    llm_router_pxpipe_heavy_models: str = "claude-fable-5"

    # ── Ollama (local inference — no API key needed) ──
    # Set ollama_base_url to enable Ollama as a task answerer (e.g. http://localhost:11434).
    # When configured, Ollama models are ALWAYS prepended to the routing chain
    # regardless of profile or quota pressure — they are free and local, so there
    # is no reason to skip them. If a model can't answer the task it fails fast
    # and the chain falls through to paid APIs.
    # Note: OLLAMA_URL (used by hooks) is separate — it controls which model
    # classifies prompts locally. OLLAMA_BASE_URL here controls which models
    # ANSWER tasks. Both should be set for full local-first operation.
    # Example: ollama_budget_models="gemma4:latest,qwen3.5:latest"
    ollama_base_url: str = ""               # empty = Ollama disabled
    ollama_budget_models: str = ""          # comma-separated model names

    @field_validator("ollama_base_url")
    @classmethod
    def _validate_ollama_base_url(cls, v: str) -> str:
        # CHZ-SEC-06: a configured/env-injected Ollama URL is validated at the
        # boundary, so every direct `.ollama_base_url` read (semantic_cache,
        # discover, …) is already scheme/host-safe. Unsafe → "" (disabled).
        return validate_ollama_url(v)

    # ── OpenAI-compatible local inference (llama.cpp, vLLM, TGI, LM Studio) ──
    # Any server that speaks /v1/chat/completions (OpenAI wire format) works here.
    # Example: openai_compat_base_url="http://localhost:8080/v1"
    #          openai_compat_models="llama-3.2-8b,mistral-7b"
    openai_compat_base_url: str = ""        # empty = disabled
    openai_compat_models: str = ""          # comma-separated model names

    # ── Agentic model routing (v0.5.5) ──
    # Preferred model for agentic / tool-reasoning tasks (analyze, generate,
    # query, research). When set, it is pinned at the absolute FRONT of the
    # routing chain for those task types — ahead of the generic Ollama injection
    # and every other reorder — so a strong tool-calling model (e.g. Hermes)
    # leads agent work while dedicated coders still win CODE tasks.
    # Example: LLM_ROUTER_AGENTIC_MODEL=ollama/hermes3:8b
    llm_router_agentic_model: str = ""          # LLM_ROUTER_AGENTIC_MODEL

    # ── Media providers ──
    fal_key: str = ""               # fal.ai — Flux, video, audio
    stability_api_key: str = ""     # Stability AI — Stable Diffusion
    elevenlabs_api_key: str = ""    # ElevenLabs — voice/TTS
    runway_api_key: str = ""        # Runway — video generation
    replicate_api_token: str = ""   # Replicate — various models

    # ── Router settings ──
    # GH#69 (see the block above the class): reads LLM_ROUTER_COST_PROFILE
    # first (GH#65's de-collided name), then falls back to the legacy
    # LLM_ROUTER_PROFILE — validated below so neither name can crash
    # construction when it holds a value outside the routing-tier domain.
    llm_router_profile: RoutingProfile = Field(
        default=RoutingProfile.BALANCED,
        validation_alias=AliasChoices("LLM_ROUTER_COST_PROFILE", "LLM_ROUTER_PROFILE"),
    )

    @field_validator("llm_router_profile", mode="before")
    @classmethod
    def _validate_llm_router_profile(cls, v: object) -> object:
        """Never let an unrecognized value crash config construction (GH#69).

        A stale ``LLM_ROUTER_PROFILE=enterprise`` (or any other garbage
        string — this must not special-case ``"enterprise"``) is not a
        routing tier; raising here took down ``RouterConfig()`` at import
        time and therefore the whole MCP server. Fall back to the default
        profile with a one-shot warning instead.
        """
        if v is None or isinstance(v, RoutingProfile):
            return v
        s = str(v).strip()
        if not s:
            return v
        if s.lower() in _VALID_LLM_ROUTER_PROFILES:
            return s.lower()
        # `v` came from whichever alias AliasChoices picked FIRST BY
        # PRESENCE (LLM_ROUTER_COST_PROFILE if set at all, else the legacy
        # name) — not by validity. So an invalid LLM_ROUTER_COST_PROFILE
        # must still give the legacy LLM_ROUTER_PROFILE its own chance
        # before giving up, exactly mirroring the two-step check in
        # repo_config.effective_profile() (GH#65).
        import os
        legacy = os.environ.get("LLM_ROUTER_PROFILE", "").strip().lower()
        if legacy and legacy != s.lower() and legacy in _VALID_LLM_ROUTER_PROFILES:
            return legacy
        _maybe_emit_llm_router_profile_fallback_warning(s)
        return RoutingProfile.BALANCED

    llm_router_tier: Tier = Tier.FREE
    # RED2-07: a default_factory, not a bare default. As a bare default this
    # expression ran at CLASS-DEFINITION time, freezing the real home directory
    # at import — so LLM_ROUTER_HOME could not redirect it, and neither could
    # monkeypatching Path.home() afterwards. A test that believed it was
    # sandboxed wrote to the operator's live usage.db and destroyed real data
    # (evidence/AUDITOR_INCIDENT.md). Resolved per instantiation through
    # llm_router.paths, which reads LLM_ROUTER_HOME at call time.
    llm_router_db_path: Path = Field(default_factory=lambda: state_path("usage.db"))
    llm_router_monthly_budget: float = 20.0  # $20/month default cap
    llm_router_daily_spend_limit: float = 0.0  # 0 = disabled; >0 fires alert when crossed

    # ── Persistence hardening (sensitive-content lifecycle) ──
    # Whether result_cache/semantic_cache/session_store scrub credentials,
    # tokens, and PII from content BEFORE it is written to disk. On by
    # default — persistence is not a safe place for raw secrets.
    llm_router_persist_redaction: bool = True   # LLM_ROUTER_PERSIST_REDACTION
    # Opt-in escape hatch: when true, skip redaction entirely and persist
    # verbatim content. Off by default; only for trusted local debugging.
    llm_router_persist_raw: bool = False        # LLM_ROUTER_PERSIST_RAW
    # Retention window for persisted content. Rows/lines older than this are
    # PHYSICALLY deleted (not just filtered from queries) the next time the
    # owning store is opened or written to. 0 disables purging.
    llm_router_persist_ttl_days: int = 30       # LLM_ROUTER_PERSIST_TTL_DAYS

    # ── Explainability (v8.2.0) ──
    # Controls routing explanation visibility on every response.
    # "footer" (default): compact one-line after response
    # "header": one-line before response
    # "verbose": full chain breakdown
    # "off": no explanation shown
    llm_router_explain: str = "footer"

    # ── Context optimization (v8.3.0) ──
    # "auto" (default): Stage 1 (structural) + Stage 2 (recency weighting)
    # "off": pass context unchanged
    llm_router_context_optimizer: str = "auto"

    # ── Smart routing settings ──
    daily_token_budget: int = 500_000       # 500k tokens/day default cap
    quality_mode: QualityMode = QualityMode.BALANCED
    min_model: str = "haiku"                # floor: never route below this

    # ── Quota-balanced routing settings (v7.1.0) ──
    # Used by QUOTA_BALANCED profile to track Codex daily quota independently.
    codex_daily_limit: int = 1000           # Codex free tier = 1000 requests/day

    # ── Team Dashboard settings (v3.0) ──
    # llm_router_team_endpoint: webhook URL for push notifications.
    # Channel auto-detected: hooks.slack.com → Slack, discord.com → Discord,
    # api.telegram.org/bot* → Telegram, anything else → generic JSON POST.
    llm_router_team_endpoint: str = ""   # e.g. https://hooks.slack.com/...
    llm_router_user_id: str = ""         # override auto-detected git email
    llm_router_team_chat_id: str = ""    # Telegram chat_id (only for Telegram)

    # ── Digest settings (v3.3) ──
    # Separate from team_endpoint — digest goes to a different channel/webhook.
    # Falls back to llm_router_team_endpoint if not set.
    llm_router_webhook_url: str = ""     # LLM_ROUTER_WEBHOOK_URL

    # ── Tool slim mode (v4.0; consolidated default since 0.10.0) ──
    # Reduce the number of registered MCP tools to save context tokens.
    # Values: "consolidated" (11 front doors — 1.0 surface, DEFAULT),
    # "off" (all legacy tools), "routing" (12 tools), "core" (4 tools).
    # Set LLM_ROUTER_SLIM=off to restore the full legacy surface (escape hatch).
    llm_router_slim: str = "consolidated"   # LLM_ROUTER_SLIM (consolidated=11 doors, off=all)

    # ── Cost-threshold escalation (v4.0) ──
    # Block any single call estimated above this cost until approved via
    # llm_approve_route. 0.0 = disabled (default). Example: 0.10 = $0.10/call cap.
    llm_router_escalate_above: float = 0.0   # LLM_ROUTER_ESCALATE_ABOVE (per-call USD)
    # Hard stop: cancel all calls once session spend exceeds this total.
    # 0.0 = disabled. Example: 1.0 = $1.00/session hard stop.
    llm_router_hard_stop_above: float = 0.0  # LLM_ROUTER_HARD_STOP_ABOVE (session USD)

    # ── Agent loop circuit breaker (v8.0+) ──
    # Maximum nesting depth for Agent tool calls. When an agent spawns another
    # agent, nesting depth is incremented. If depth reaches this limit, new
    # Agent calls are blocked. Exempt: Explore agents (pure retrieval, no cost).
    # Use LLM_ROUTER_MAX_AGENT_DEPTH to override. Default: 3.
    llm_router_max_agent_depth: int = 3  # LLM_ROUTER_MAX_AGENT_DEPTH

    # ── Routing policy (v0.5.0) ──
    # Controls the model-selection strategy applied to every routing request.
    # Set LLM_ROUTER_ROUTING_POLICY to one of:
    #   balanced        (default) — cost/quality sweet spot; standard chain order
    #   local-first     — Ollama → Codex → Gemini CLI → paid APIs
    #   cost            — cheapest available model first
    #   quality         — highest-quality model first (from benchmarks.json scores)
    #   quota-exhaustion — route away from providers near their quota limit
    #   dynamic         — round-robin between models within ±10% quota usage
    llm_router_routing_policy: str = "balanced"  # LLM_ROUTER_ROUTING_POLICY

    # ── HuggingFace Inference API ──
    # Used by the discovery layer to access free-tier hosted models.
    # Accepts HF_TOKEN or HUGGINGFACE_API_KEY from environment.
    huggingface_api_key: str = ""   # HF_TOKEN / HUGGINGFACE_API_KEY

    # ── Adaptive Universal Router settings (v5.0+) ──
    # Discovery cache TTL in seconds. After this window, available models are
    # re-scanned (Ollama list, HF API check, env var re-read). Default: 30 min.
    llm_router_discovery_ttl: int = 1800     # LLM_ROUTER_DISCOVERY_TTL

    # Benchmark data refresh interval in days. After this many days the cached
    # leaderboard scores are re-fetched in the background. Default: 7 days.
    llm_router_benchmark_ttl_days: int = 7   # LLM_ROUTER_BENCHMARK_TTL_DAYS

    # Per-provider monthly budget caps (USD). 0.0 = no cap (unlimited).
    # When a provider's tracked spend reaches its cap, budget_availability
    # drops to 0.0 and it sinks to the bottom of all routing chains automatically.
    llm_router_budget_openai: float = 0.0       # LLM_ROUTER_BUDGET_OPENAI
    llm_router_budget_gemini: float = 0.0       # LLM_ROUTER_BUDGET_GEMINI
    llm_router_budget_groq: float = 0.0         # LLM_ROUTER_BUDGET_GROQ
    llm_router_budget_deepseek: float = 0.0     # LLM_ROUTER_BUDGET_DEEPSEEK
    llm_router_budget_together: float = 0.0     # LLM_ROUTER_BUDGET_TOGETHER
    llm_router_budget_perplexity: float = 0.0   # LLM_ROUTER_BUDGET_PERPLEXITY
    llm_router_budget_mistral: float = 0.0      # LLM_ROUTER_BUDGET_MISTRAL

    # ── Enterprise integrations (v5.1) ──
    helicone_api_key: str = ""
    llm_router_helicone_pull: bool = False  # Pull spend from Helicone API
    llm_router_litellm_budget_db: str = ""  # Path to LiteLLM proxy budget DB
    # How to combine spend seen across multiple tracking systems:
    # "max" assumes sources overlap and keeps the highest single observed total.
    # "sum" treats sources as independent traffic channels and adds them together.
    llm_router_spend_aggregation: Literal["max", "sum"] = "max"

    # ── Community Benchmarks settings (v3.4) ──
    # Set to true to opt in to anonymous routing quality sharing (future upload).
    # In v3.4 this only prepares a local export file; upload requires a future
    # server endpoint to be ready.
    llm_router_community: bool = False   # LLM_ROUTER_COMMUNITY

    # ── Context injection settings ──
    context_enabled: bool = True          # inject session/history context into routed calls
    context_max_messages: int = 5         # max recent session messages to include
    context_max_previous_sessions: int = 3  # max past session summaries to include
    context_max_tokens: int = 1500        # token budget for all injected context

    # ── Session Context Accumulator settings ──
    # Durable, cross-process JSONL log of session events (user prompts, routed
    # Q&A, tool calls) re-injected as extra context on subsequent routed calls
    # and direct-execution draft calls. See LLM_ROUTER_SESSION_CONTEXT for the
    # single ergonomic on/off/local/all override (checked first by
    # session_store.get_mode(), which falls back to these two booleans).
    session_context_enabled: bool = True          # SESSION_CONTEXT_ENABLED
    session_context_share_external: bool = True   # SESSION_CONTEXT_SHARE_EXTERNAL — allow durable events into non-local (paid API) routed calls; default "all" per approved decision
    session_context_max_tokens_mcp: int = 1500    # SESSION_CONTEXT_MAX_TOKENS_MCP — budget for MCP-routed call injection
    session_context_max_tokens_draft: int = 800   # SESSION_CONTEXT_MAX_TOKENS_DRAFT — budget for hook-level direct/draft call injection

    # ── Compaction settings ──
    compaction_mode: str = "structural"  # off | structural | full
    compaction_threshold: int = 4000     # token threshold to trigger compaction

    # ── Prompt caching (Anthropic only) ──
    # Auto-injects cache_control breakpoints on long stable context (system prompts,
    # conversation history) to save up to 90% on repeated Anthropic API calls.
    # min_tokens: Anthropic requires ≥1024 for Sonnet/Opus, ≥2048 for Haiku.
    prompt_cache_enabled: bool = True
    prompt_cache_min_tokens: int = 1024

    # ── Caveman mode (token-efficient output) ──
    # Caveman reduces output tokens by ~75% via structured terseness rules:
    # removes filler, uses fragments, preserves only technical substance.
    # Intensity: "off" | "lite" | "full" | "ultra" (default "full")
    caveman_mode: str = "full"  # off, lite, full, ultra

    # ── Health check settings ──
    health_failure_threshold: int = 2
    health_cooldown_seconds: int = 30

    # ── Request defaults ──
    default_max_tokens: int = 4096
    default_temperature: float = 0.7
    request_timeout: int = 120
    # Media generation (especially video) can take several minutes; separate
    # timeout prevents premature cancellation of long-running generation jobs.
    media_request_timeout: int = 600

    model_config = {
        "env_file": (Path.home() / ".llm-router" / ".env", ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        # GH#69: llm_router_profile sets an explicit validation_alias
        # (AliasChoices) to read LLM_ROUTER_COST_PROFILE ahead of the legacy
        # LLM_ROUTER_PROFILE name. Without populate_by_name, an explicit
        # validation_alias also stops the plain field name from being
        # accepted as a constructor kwarg — every OTHER field in this class
        # is still constructible by field name (`RouterConfig(foo_bar=...)`,
        # as the existing test suite does throughout), so this keeps
        # `RouterConfig(llm_router_profile=...)` working the same way
        # rather than silently ignoring the kwarg and falling back to the
        # default profile.
        "populate_by_name": True,
    }

    # Maps each Pydantic field name to (provider_name, litellm_env_var).
    # This dual mapping serves two purposes:
    #   1. provider_name: used by available_providers to check which providers
    #      have keys configured.
    #   2. litellm_env_var: the specific env var name that LiteLLM expects
    #      (which sometimes differs from our field name, e.g.
    #      perplexity_api_key -> PERPLEXITYAI_API_KEY).
    _PROVIDER_MAP: dict[str, tuple[str, str]] = {
        "openai_api_key": ("openai", "OPENAI_API_KEY"),
        "gemini_api_key": ("gemini", "GEMINI_API_KEY"),
        "perplexity_api_key": ("perplexity", "PERPLEXITYAI_API_KEY"),
        "anthropic_api_key": ("anthropic", "ANTHROPIC_API_KEY"),
        "mistral_api_key": ("mistral", "MISTRAL_API_KEY"),
        "deepseek_api_key": ("deepseek", "DEEPSEEK_API_KEY"),
        "groq_api_key": ("groq", "GROQ_API_KEY"),
        "together_api_key": ("together", "TOGETHER_API_KEY"),
        "xai_api_key": ("xai", "XAI_API_KEY"),
        "cohere_api_key": ("cohere", "COHERE_API_KEY"),
        "moonshot_api_key": ("moonshot", "MOONSHOT_API_KEY"),
        "minimax_api_key": ("minimax", "MINIMAX_API_KEY"),
        "zhipu_api_key": ("zhipu", "ZHIPU_API_KEY"),
        "arcee_api_key": ("arcee", "ARCEE_API_KEY"),
        "openrouter_api_key": ("openrouter", "OPENROUTER_API_KEY"),
        "fal_key": ("fal", "FAL_KEY"),
        "stability_api_key": ("stability", "STABILITY_API_KEY"),
        "elevenlabs_api_key": ("elevenlabs", "ELEVENLABS_API_KEY"),
        "runway_api_key": ("runway", "RUNWAY_API_KEY"),
        "replicate_api_token": ("replicate", "REPLICATE_API_TOKEN"),
        "huggingface_api_key": ("huggingface", "HF_TOKEN"),
    }

    def provider_api_key(self, provider: str) -> str | None:
        """Resolve a provider's API key through the pluggable secrets vault.

        Default backend (``LLM_ROUTER_SECRETS_BACKEND`` unset / ``env``) reads
        the same env vars as before — zero behaviour change. Registering a
        real vault backend (HashiCorp / AWS / GCP via
        ``secrets_vault.register_backend``) transparently redirects key
        resolution here without touching callers. Fail-open: a vault
        outage degrades to env.
        """
        from llm_router.secrets_vault import get_provider_key
        return get_provider_key(provider)

    @property
    def available_providers(self) -> set[str]:
        """Return the set of all providers that have a non-empty API key configured.

        This includes both text and media providers. Used by the router to
        filter the model chain to only models whose provider is available.

        Ollama is treated specially: it has no API key, so it is included
        whenever ``ollama_base_url`` is set.

        Returns:
            Set of provider name strings (e.g. ``{"openai", "anthropic", "fal"}``).
        """
        providers = set()
        for field_name, (provider_name, _) in self._PROVIDER_MAP.items():
            if getattr(self, field_name, ""):
                providers.add(provider_name)
        ollama_url = self.effective_ollama_base_url
        if ollama_url and probe_ollama(ollama_url):
            providers.add("ollama")
        if self.openai_compat_base_url:
            providers.add("openai_compat")
        # In subscription mode, home providers are intentionally excluded:
        # we never route back via API when already inside the subscription agent.
        # Routing back would require a separate API key AND add duplicate
        # billing — wrong in every scenario.
        if self.llm_router_claude_subscription:
            providers.discard("anthropic")
        if self.llm_router_gemini_subscription:
            providers.discard("gemini")
        return providers

    @property
    def effective_ollama_base_url(self) -> str:
        """Return the Ollama endpoint LLM Router should probe/use.

        LLM Router should work on a default Ollama install without a local preset:
        if no URL is configured, probe the standard local daemon endpoint.
        """
        import os

        candidate = (
            self.ollama_base_url
            or os.getenv("OLLAMA_BASE_URL", "")
            or os.getenv("OLLAMA_URL", "")
            or ("" if os.getenv("PYTEST_CURRENT_TEST") else "http://localhost:11434")
        )
        # CHZ-SEC-06: never hand an unvalidated URL to a network call.
        return validate_ollama_url(candidate)

    @property
    def text_providers(self) -> set[str]:
        """Return available providers that support text LLM completion.

        Note that OpenAI and Gemini appear in both text and media sets,
        since they offer both capabilities.

        Returns:
            Subset of ``available_providers`` that support text generation.
        """
        return self.available_providers & {
            "openai", "gemini", "perplexity", "anthropic",
            "mistral", "deepseek", "groq", "together", "xai", "cohere", "ollama",
            "huggingface", "openrouter", "openai_compat",
        }

    @property
    def media_providers(self) -> set[str]:
        """Return available providers that support media generation (image/video/audio).

        Returns:
            Subset of ``available_providers`` that support media generation.
        """
        return self.available_providers & {
            "openai", "gemini", "fal", "stability", "elevenlabs", "runway", "replicate",
        }

    def ollama_models_for_profile(self, profile: "RoutingProfile") -> list[str]:
        """Return Ollama model IDs for the BUDGET profile (legacy behaviour).

        Kept for backward compatibility. Prefer ``all_ollama_models()`` when
        injecting Ollama under quota pressure regardless of profile.
        """
        if not self.effective_ollama_base_url or profile != RoutingProfile.BUDGET:
            return []
        return self.all_ollama_models()

    def all_ollama_models(self) -> list[str]:
        """Return all configured Ollama model IDs regardless of routing profile.

        Used by the pressure-aware routing layer to inject local/free models
        when Claude subscription quota is running high (>= 85%).

        Try live discovery cache first; fall back to OLLAMA_BUDGET_MODELS env var
        for backward compatibility when cache is empty or missing.

        Returns:
            List of LiteLLM model IDs like ``["ollama/qwen3.5:latest", "ollama/qwen3.6:27b"]``,
            or an empty list when Ollama is not configured.
        """
        import os

        # Resolve the effective base URL: explicit config > env var > localhost default.
        effective_url = self.effective_ollama_base_url

        # Discovery cache takes priority — it represents what's actually running.
        # Only trust the cache if Ollama is also reachable at the effective URL.
        if not os.getenv("PYTEST_CURRENT_TEST"):
            try:
                from llm_router.discover import get_cached_ollama_models
                cached_models = get_cached_ollama_models()
                if cached_models and probe_ollama(effective_url):
                    # Ensure OLLAMA_API_BASE is set so LiteLLM knows where to send calls.
                    os.environ.setdefault("OLLAMA_API_BASE", effective_url)
                    return cached_models
            except Exception:
                pass

        # Fall back to env/configured model names for backward compatibility.
        # If the user gave an explicit base URL, preserve historical behavior and
        # return the configured names without requiring a live probe in this method.
        # For auto-default localhost, require a successful probe first.
        if not self.ollama_base_url and not probe_ollama(effective_url):
            return []

        os.environ.setdefault("OLLAMA_API_BASE", effective_url)
        return [f"ollama/{m.strip()}" for m in self.ollama_budget_models.split(",") if m.strip()]

    def all_openai_compat_models(self) -> list[str]:
        """Return model IDs for the configured OpenAI-compatible local server.

        Returns ``["openai_compat/model-name", ...]`` so the router can inject
        them into the chain. The quirk layer (``OpenAICompatQuirks``) rewrites
        the prefix to ``openai/`` and injects ``api_base`` before the LiteLLM call.

        When ``openai_compat_base_url`` is not set, falls back to the first
        auto-detected local platform (LM Studio, Jan, vLLM, etc.) so users get
        local routing without any manual config.
        """
        if self.openai_compat_base_url and self.openai_compat_models:
            return [
                f"openai_compat/{m.strip()}"
                for m in self.openai_compat_models.split(",")
                if m.strip()
            ]
        # Auto-detect: find first running non-Ollama OpenAI-compat platform
        try:
            import os
            if not os.getenv("PYTEST_CURRENT_TEST"):
                from llm_router.local_platforms import get_first_openai_compat
                result = get_first_openai_compat()
                if result:
                    _url, models = result
                    # Temporarily set so OpenAICompatQuirks can read the base_url
                    object.__setattr__(self, "openai_compat_base_url", _url)
                    return [f"openai_compat/{m}" for m in models[:5] if m]
        except Exception:
            pass
        return []

    def model_post_init(self, __context: dict) -> None:
        # Skip in test mode (pytest sets this env var)
        import os
        if os.getenv("PYTEST_CURRENT_TEST"):
            return

        try:
            from llm_router.safe_config import load_safe_config
            safe_config_data = load_safe_config()
            if not safe_config_data:
                return

            # Only fill in fields that are still empty (don't override .env)
            for field_name, value in safe_config_data.items():
                if not value or not isinstance(value, (str, bool, int, float)):
                    continue
                current = getattr(self, field_name, None)
                # Only apply if current value is empty/False
                if not current:
                    try:
                        setattr(self, field_name, value)
                    except (ValueError, AttributeError):
                        pass  # Silently skip invalid fields
        except Exception:
            pass  # Silently fail — fallback config is optional

    def apply_keys_to_env(self) -> None:
        """Export all configured API keys into ``os.environ``.

        LiteLLM reads API keys from environment variables at call time rather
        than accepting them as constructor arguments. This method bridges the
        gap by copying keys from our Pydantic config into the environment
        using the LiteLLM-expected variable names (from ``_PROVIDER_MAP``).

        In subscription mode (``llm_router_claude_subscription=True``),
        ``ANTHROPIC_API_KEY`` is intentionally NOT exported. This ensures
        LiteLLM cannot make Anthropic API calls even if an ``anthropic/*``
        model slips through any code path — a hard guarantee on top of the
        ``available_providers`` filter.

        Called automatically by ``get_config()`` on first load.
        """
        import os
        for field_name, (provider_name, env_var) in self._PROVIDER_MAP.items():
            value = getattr(self, field_name, "")
            if not value:
                continue
            # Never export the Anthropic key in subscription mode — prevents
            # LiteLLM from making direct Anthropic API calls that would incur
            # separate billing on top of the Claude Code subscription.
            if self.llm_router_claude_subscription and provider_name == "anthropic":
                continue
            os.environ[env_var] = value
        # LiteLLM reads Ollama's base URL from OLLAMA_API_BASE
        if self.ollama_base_url:
            os.environ.setdefault("OLLAMA_API_BASE", self.ollama_base_url)


_config: RouterConfig | None = None
_config_lock = threading.Lock()

def get_config() -> RouterConfig:
    """Return the singleton RouterConfig instance."""
    import os
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = RouterConfig()
                _config.apply_keys_to_env()
    if _config.llm_router_claude_subscription:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    return _config


async def validate_config_migration(
    old_version: int, new_version: int, old_keys: set[str], new_keys: set[str]
) -> tuple[bool, str]:
    """Routing Point 3.3: Validate config version migration safely.

    Detects breaking changes, suggests migration path, warns about data loss.

    Args:
        old_version: Current config version
        new_version: Target config version
        old_keys:    Keys in current config
        new_keys:    Keys expected in new version

    Returns:
        (can_upgrade, reasoning) tuple.
    """
    try:
        can_upgrade, reasoning = await validate_config_upgrade(
            old_version=old_version,
            new_version=new_version,
            old_keys=old_keys,
            new_keys=new_keys,
        )
        log_routing_decision(
            routing_point="config_migration_validation",
            decision="approved" if can_upgrade else "requires-manual-review",
            reasoning=reasoning,
            metadata={
                "from_version": old_version,
                "to_version": new_version,
                "keys_lost": len(old_keys - new_keys),
            },
        )
    except Exception as e:
        can_upgrade = False
        reasoning = f"manual-review-required: {e}"
        log_routing_decision(
            routing_point="config_migration_validation",
            decision="degraded",
            reasoning=reasoning,
            metadata={"from_version": old_version, "to_version": new_version},
        )

    return can_upgrade, reasoning
