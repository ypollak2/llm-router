"""Repo-aware YAML routing config — v2.4.

Loads two optional YAML files and merges them:
  1. ~/.llm-router/routing.yaml   — user-level overrides (always applied)
  2. .llm-router.yml              — repo-level overrides (searched up from cwd)

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

# ── Schema ────────────────────────────────────────────────────────────────────

VALID_PROFILES  = {"budget", "balanced", "premium"}
VALID_ENFORCE   = {"shadow", "suggest", "enforce", "hard", "soft", "off"}
VALID_TASK_TYPES = {"query", "code", "analyze", "generate", "research", "image", "video", "audio"}


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
    profile: str | None = None                            # budget | balanced | premium
    enforce: str | None = None                            # shadow | suggest | enforce
    block_providers: list[str] = field(default_factory=list)
    block_models: list[str] = field(default_factory=list)   # model-level deny (v3.2)
    allow_models: list[str] = field(default_factory=list)   # model-level allow-list (v3.2)
    routing: dict[str, TaskRouteOverride] = field(default_factory=dict)
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
        """Return enforce mode: env var wins, then repo config, then 'hard'."""
        env = os.environ.get("LLM_ROUTER_ENFORCE", "").lower()
        if env in VALID_ENFORCE:
            return env
        if self.enforce and self.enforce in VALID_ENFORCE:
            return self.enforce
        return "hard"

    def effective_profile(self) -> str | None:
        """Return profile: env var wins, then repo config."""
        env = os.environ.get("LLM_ROUTER_PROFILE", "").lower()
        if env in VALID_PROFILES:
            return env
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
        daily_caps     = {**base.daily_caps, **override.daily_caps},
        _sources       = base._sources + override._sources,
    )
    return merged


def find_repo_config_path(start: Path | None = None) -> Path | None:
    """Search start (default cwd) and ancestors for .llm-router.yml."""
    here = start or Path.cwd()
    for candidate in [here, *here.parents]:
        p = candidate / ".llm-router.yml"
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
    """Load .llm-router.yml from cwd or nearest ancestor (repo-level config)."""
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
