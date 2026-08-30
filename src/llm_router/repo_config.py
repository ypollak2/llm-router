"""Repo-aware YAML routing config — v2.4.

Loads two optional YAML files and merges them:
  1. ~/.llm-router/routing.yaml   — user-level overrides (always applied)
  2. .llm_router.yml              — repo-level overrides (searched up from cwd)

Precedence (high → low):
  env vars > repo config > user config > built-in defaults

Only *routing policy* lives here (profile, enforce mode, block_providers,
model pins, daily caps). Secrets (API keys) stay in env / .env files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llm_router.types import RoutingProfile

# ── Schema ────────────────────────────────────────────────────────────────────

# Derived from the real routing-tier enum (llm_router.types.RoutingProfile)
# rather than a hand-maintained literal — GH#65 found this set had drifted
# (it was missing "reasoning" and "quota_balanced"/"subscription_local" were
# reintroduced from the plan by hand and still incomplete). Keeping it wired
# to the enum means it can't silently drift again.
VALID_PROFILES  = {p.value for p in RoutingProfile}
VALID_ENFORCE   = {"shadow", "suggest", "advise", "smart", "enforce", "hard", "soft", "off"}
VALID_TASK_TYPES = {"query", "code", "analyze", "generate", "research", "image", "video", "audio"}

# ── GH#65: LLM_ROUTER_PROFILE collision ─────────────────────────────────────
#
# LLM_ROUTER_PROFILE used to be read here for the *routing cost tier*
# (budget/balanced/premium/...) AND separately by llm_router.profile /
# identity.py / server.py for the completely unrelated *enterprise identity*
# axis (developer/enterprise). profile.py already renamed its side to
# LLM_ROUTER_DEPLOYMENT_PROFILE (see PROFILE_ENV there) — re-renaming that
# again would break users who already followed that deprecation warning
# (this is what the GH#65 reporter hit: they renamed their env per the
# identity-side guidance and silently broke routing, which fell through to
# None because this reader never knew LLM_ROUTER_DEPLOYMENT_PROFILE existed).
#
# So the ROUTING side takes the new name instead: LLM_ROUTER_COST_PROFILE.
# The legacy LLM_ROUTER_PROFILE name is still honored as a fallback, but
# ONLY when its value is actually a valid routing tier — a value like
# "developer" or "enterprise" is identity-axis data, not routing data, and
# must never be misinterpreted here. This value-domain filter is what makes
# the two readers mutually exclusive even during the deprecation window.
_COST_PROFILE_ENV = "LLM_ROUTER_COST_PROFILE"
_LEGACY_COST_PROFILE_ENV = "LLM_ROUTER_PROFILE"

# One-shot latch so the deprecation warning fires once per process, mirroring
# the pattern in llm_router.profile (_legacy_warning_emitted /
# _maybe_emit_legacy_warning) rather than inventing a second mechanism.
_legacy_cost_profile_warning_emitted = False


def _maybe_emit_legacy_cost_profile_warning(value: str) -> None:
    """Print a one-shot deprecation warning when LLM_ROUTER_PROFILE is read
    as a routing cost tier. Latched at module level so a long-running
    process emits the message once, not on every ``effective_profile()``
    call."""
    global _legacy_cost_profile_warning_emitted
    if _legacy_cost_profile_warning_emitted:
        return
    _legacy_cost_profile_warning_emitted = True
    import sys
    sys.stderr.write(
        f"[llm_router] DEPRECATED: {_LEGACY_COST_PROFILE_ENV}={value!r} read "
        f"as a routing cost tier; this env name collides with the "
        f"enterprise-identity profile axis (see llm_router.profile). Rename "
        f"your env to {_COST_PROFILE_ENV} (GH#65). Backward-compat support "
        "will be removed in a future release.\n"
    )


def _reset_legacy_cost_profile_warning_latch() -> None:
    """Test affordance — reset the one-shot latch so the warning can be
    re-observed in subsequent tests. Not part of the public API."""
    global _legacy_cost_profile_warning_emitted
    _legacy_cost_profile_warning_emitted = False


@dataclass
class TaskRouteOverride:
    """Per-task-type model/provider pin."""
    model: str | None = None      # e.g. "ollama/qwen2.5-coder" or "gpt-4o"
    provider: str | None = None   # e.g. "ollama", "openai", "perplexity"


@dataclass
class RepoConfig:
    """Merged routing policy from user + repo YAML files.

    All fields are optional — omitting a field means "use the default".
    """
    profile: str | None = None                            # budget | balanced | premium | reasoning | quota_balanced | subscription_local
    enforce: str | None = None                            # shadow | suggest | enforce
    block_providers: list[str] = field(default_factory=list)
    block_models: list[str] = field(default_factory=list)   # model-level deny (v3.2)
    allow_models: list[str] = field(default_factory=list)   # model-level allow-list (v3.2)
    routing: dict[str, TaskRouteOverride] = field(default_factory=dict)
    agentic_model: str | None = None  # preferred model for agentic/reasoning tasks (v0.5.5)
    daily_caps: dict[str, float] = field(default_factory=dict)  # task_type → USD; "_total" key for global
    # Source info (not a user field — set by loader)
    _sources: list[str] = field(default_factory=list)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def model_override(self, task_type: str) -> str | None:
        """Return a pinned model for this task type, or None."""
        return self.routing.get(task_type, TaskRouteOverride()).model

    def provider_override(self, task_type: str) -> str | None:
        """Return a pinned provider for this task type, or None."""
        return self.routing.get(task_type, TaskRouteOverride()).provider

    def daily_cap_for(self, task_type: str) -> float | None:
        """Return per-task daily cap in USD, or None if not set."""
        return self.daily_caps.get(task_type) or None

    def total_daily_cap(self) -> float | None:
        """Return global daily cap in USD, or None if not set."""
        return self.daily_caps.get("_total") or None

    def effective_enforce(self) -> str:
        """Return enforce mode: env var wins, then repo config, then the shared
        built-in default ('smart') — aligned with enforce_config.DEFAULT_ENFORCE
        so every module agrees on the out-of-box default (F01)."""
        env = os.environ.get("LLM_ROUTER_ENFORCE", "").lower()
        if env in VALID_ENFORCE:
            return env
        if self.enforce and self.enforce in VALID_ENFORCE:
            return self.enforce
        try:
            from llm_router.enforce_config import DEFAULT_ENFORCE
            return DEFAULT_ENFORCE
        except Exception:
            return "smart"

    def effective_profile(self) -> str | None:
        """Return profile: env var wins, then repo config.

        Reads ``LLM_ROUTER_COST_PROFILE`` first (GH#65). Falls back to the
        legacy ``LLM_ROUTER_PROFILE`` name only when its value is a valid
        routing tier — that same env name is also read by
        ``llm_router.profile`` / ``identity.py`` / ``server.py`` for the
        unrelated enterprise-identity axis (``developer``/``enterprise``),
        so a value outside the routing-tier domain is never routing data
        and must be ignored here rather than misinterpreted.
        """
        # Literal env names here (not the module constants below) so the
        # env_registry AST scan (tests/test_env_registry.py) can see these
        # reads directly instead of needing an _INDIRECT_READS exemption.
        env = os.environ.get("LLM_ROUTER_COST_PROFILE", "").lower()
        if env in VALID_PROFILES:
            return env
        legacy = os.environ.get("LLM_ROUTER_PROFILE", "").lower()
        if legacy in VALID_PROFILES:
            _maybe_emit_legacy_cost_profile_warning(legacy)
            return legacy
        return self.profile


# ── Loaders ───────────────────────────────────────────────────────────────────

def _parse_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _dict_to_config(data: dict[str, Any], source: str) -> RepoConfig:
    """Convert a raw YAML dict into a RepoConfig (lenient — ignores unknown keys)."""
    cfg = RepoConfig(_sources=[source])

    if "profile" in data and str(data["profile"]).lower() in VALID_PROFILES:
        cfg.profile = str(data["profile"]).lower()

    if "enforce" in data and str(data["enforce"]).lower() in VALID_ENFORCE:
        cfg.enforce = str(data["enforce"]).lower()

    if isinstance(data.get("block_providers"), list):
        cfg.block_providers = [str(p).lower() for p in data["block_providers"]]

    if isinstance(data.get("block_models"), list):
        cfg.block_models = [str(m) for m in data["block_models"]]

    if isinstance(data.get("allow_models"), list):
        cfg.allow_models = [str(m) for m in data["allow_models"]]

    if isinstance(data.get("routing"), dict):
        for task, opts in data["routing"].items():
            if task not in VALID_TASK_TYPES:
                continue
            if isinstance(opts, dict):
                cfg.routing[task] = TaskRouteOverride(
                    model=opts.get("model"),
                    provider=opts.get("provider"),
                )

    if data.get("agentic_model"):
        cfg.agentic_model = str(data["agentic_model"])

    if isinstance(data.get("daily_caps"), dict):
        for key, val in data["daily_caps"].items():
            try:
                cfg.daily_caps[key] = float(val)
            except (TypeError, ValueError):
                pass

    return cfg


def _merge(base: RepoConfig, override: RepoConfig) -> RepoConfig:
    """Merge two configs — override wins for scalar fields, lists are combined."""
    merged = RepoConfig(
        profile        = override.profile        or base.profile,
        enforce        = override.enforce        or base.enforce,
        block_providers= list({*base.block_providers, *override.block_providers}),
        block_models   = list({*base.block_models,    *override.block_models}),
        allow_models   = list({*base.allow_models,    *override.allow_models}),
        routing        = {**base.routing, **override.routing},
        agentic_model  = override.agentic_model or base.agentic_model,
        daily_caps     = {**base.daily_caps, **override.daily_caps},
        _sources       = base._sources + override._sources,
    )
    return merged


def find_repo_config_path(start: Path | None = None) -> Path | None:
    """Search start (default cwd) and ancestors for .llm_router.yml."""
    here = start or Path.cwd()
    for candidate in [here, *here.parents]:
        p = candidate / ".llm_router.yml"
        if p.exists():
            return p
        # Stop at filesystem root or home directory
        if candidate == candidate.parent or candidate == Path.home():
            break
    return None


def load_user_config() -> RepoConfig:
    """Load ~/.llm-router/routing.yaml (user-level config)."""
    path = Path.home() / ".llm-router" / "routing.yaml"
    if not path.exists():
        return RepoConfig()
    return _dict_to_config(_parse_yaml(path), str(path))


def load_repo_config(start: Path | None = None) -> RepoConfig:
    """Load .llm_router.yml from cwd or nearest ancestor (repo-level config)."""
    path = find_repo_config_path(start)
    if path is None:
        return RepoConfig()
    return _dict_to_config(_parse_yaml(path), str(path))


def effective_config(start: Path | None = None) -> RepoConfig:
    """Return merged config: user config + repo config (repo wins)."""
    user = load_user_config()
    repo = load_repo_config(start)
    return _merge(user, repo)


# ── Fingerprinting ────────────────────────────────────────────────────────────

_FINGERPRINT_RULES: list[tuple[list[str], str, str]] = [
    # (indicator files, repo_type, suggested profile)
    (["Cargo.toml"],                              "rust",    "budget"),
    (["go.mod"],                                  "go",      "budget"),
    (["pyproject.toml", "setup.py", "setup.cfg"], "python",  "budget"),
    (["package.json"],                             "node",    "balanced"),
    (["pom.xml", "build.gradle"],                 "java",    "balanced"),
    (["*.swift", "Package.swift"],                "swift",   "balanced"),
    (["Gemfile"],                                  "ruby",    "balanced"),
    (["composer.json"],                            "php",     "balanced"),
]


def fingerprint_repo(path: Path | None = None) -> tuple[str, str]:
    """Detect repo language and suggest a routing profile.

    Returns:
        (repo_type, suggested_profile) — both are strings.
    """
    root = path or Path.cwd()
    for indicators, repo_type, profile in _FINGERPRINT_RULES:
        for indicator in indicators:
            if "*" in indicator:
                if list(root.glob(indicator)):
                    return repo_type, profile
            elif (root / indicator).exists():
                return repo_type, profile
    return "generic", "balanced"
