"""Policy Engine — org/user/repo routing policy precedence (v3.2).

Policy files are loaded in precedence order (highest → lowest):
  1. ~/.llm-router/org-policy.yaml  — site/team-wide rules (new in v3.2)
  2. ~/.llm-router/routing.yaml     — user-level overrides (existing RepoConfig)
  3. .llm-router.yml                — repo-level overrides (existing RepoConfig)

The org layer (this module) adds model-level allow/deny rules that sit above
the existing provider-level block_providers in repo_config.

Org policy schema (all fields optional):
  block_providers: [openai, anthropic]
  block_models:    [openai/gpt-4o]
  allow_models:    [ollama/*, codex/*]   # if set, ONLY these pass
  task_caps:                              # per-task daily USD caps
    code: 0.50
    research: 1.00
    _total: 5.00

When both block_models and allow_models are set, allow_models wins for
matching entries (explicit allow overrides a pattern-level block).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("llm_router.policy")

ORG_POLICY_PATH = Path.home() / ".llm-router" / "org-policy.yaml"


@dataclass(frozen=False)
class OrgPolicy:
    """Policy loaded from ~/.llm-router/org-policy.yaml."""

    block_providers: list[str] = field(default_factory=list)
    block_models: list[str] = field(default_factory=list)
    allow_models: list[str] = field(default_factory=list)  # empty = allow all
    task_caps: dict[str, float] = field(default_factory=dict)
    source: str = "default"


def load_org_policy(path: Path = ORG_POLICY_PATH) -> OrgPolicy:
    """Load org-level policy from YAML file.

    Returns a default (permissive) OrgPolicy if the file is absent or invalid.
    """
    if not path.exists():
        return OrgPolicy()
    try:
        import yaml  # pyyaml installed via litellm transitive dep
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("Failed to load org policy from %s: %s", path, exc)
        return OrgPolicy()

    if not isinstance(data, dict):
        return OrgPolicy()

    raw_caps = data.get("task_caps", {})
    caps = {k: float(v) for k, v in raw_caps.items() if isinstance(v, (int, float))} if isinstance(raw_caps, dict) else {}

    return OrgPolicy(
        block_providers=list(data.get("block_providers", [])),
        block_models=list(data.get("block_models", [])),
        allow_models=list(data.get("allow_models", [])),
        task_caps=caps,
        source=str(path),
    )


def _model_matches(model: str, patterns: list[str]) -> bool:
    """Return True if *model* matches any pattern in *patterns* (glob supported)."""
    for pat in patterns:
        if fnmatch.fnmatch(model, pat) or model == pat:
            return True
        # Provider-only match: "openai" matches "openai/gpt-4o"
        if "/" not in pat and model.startswith(pat + "/"):
            return True
    return False


def apply_policy(
    models: list[str],
    task_type: str,
    org: OrgPolicy | None = None,
) -> tuple[list[str], list[str]]:
    """Filter a model list through org policy rules.

    Args:
        models:    Ordered list of candidate model strings (provider/model).
        task_type: Task type string (e.g. "code", "query").
        org:       Org policy to apply. Loads from disk if None.

    Returns:
        (allowed, blocked) — allowed models in original order, blocked list.
    """
    if org is None:
        org = load_org_policy()

    allowed: list[str] = []
    blocked: list[str] = []

    for model in models:
        from llm_router.profiles import provider_from_model
        provider = provider_from_model(model)

        # 1. Provider-level block
        if provider in org.block_providers:
            blocked.append(model)
            continue

        # 2. Model-level block (unless explicitly allowed)
        if org.block_models and _model_matches(model, org.block_models):
            # Check if it's explicitly allowed — allow overrides block
            if org.allow_models and _model_matches(model, org.allow_models):
                allowed.append(model)
            else:
                blocked.append(model)
            continue

        # 3. Allow-list: if set, only listed models pass
        if org.allow_models and not _model_matches(model, org.allow_models):
            blocked.append(model)
            continue

        allowed.append(model)

    if blocked:
        # Upgrade to WARNING for audit visibility with rule source
        log.warning(
            "POLICY AUDIT: blocked %d model(s) for task=%s: %s (rule source: %s)",
            len(blocked), task_type, blocked,
            getattr(org, '_source', 'org-policy.yaml'),
        )

    return allowed, blocked


def get_task_cap(task_type: str, org: OrgPolicy | None = None) -> float | None:
    """Return the per-task daily USD cap from org policy, or None if not set."""
    if org is None:
        org = load_org_policy()
    return org.task_caps.get(task_type) or org.task_caps.get("_total") or None


def policy_summary(org: OrgPolicy | None = None) -> str:
    """Return a formatted summary of the active org policy."""
    if org is None:
        org = load_org_policy()

    if org.source == "default":
        return "  No org policy file found (~/.llm-router/org-policy.yaml)\n  All providers and models allowed."

    lines = [f"  Source: {org.source}", ""]
    if org.block_providers:
        lines.append(f"  Blocked providers:  {', '.join(org.block_providers)}")
    if org.block_models:
        lines.append(f"  Blocked models:     {', '.join(org.block_models)}")
    if org.allow_models:
        lines.append(f"  Allow-list models:  {', '.join(org.allow_models)}")
    if org.task_caps:
        lines.append("  Per-task daily caps:")
        for task, cap in org.task_caps.items():
            lines.append(f"    {task:<12}  ${cap:.2f}/day")
    if len(lines) == 2:
        lines.append("  No restrictions configured.")
    return "\n".join(lines)
