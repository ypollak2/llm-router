"""Flexible routing policies — user-configurable routing behavior.

Policies control:
- Confidence threshold for routing decisions
- Which prompts to skip (e.g., acknowledgements, system commands)
- Whether to route coordination tasks (git, deploy, etc.)
- Task type routing preferences

Users can select from preset policies (aggressive, balanced, conservative)
or create custom policies via the wizard.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass(frozen=True)
class RoutingPolicy:
    """A routing policy defines how prompts are classified and routed.

    Attributes:
        name: Policy name (e.g., 'aggressive', 'balanced')
        description: Human-readable description
        confidence_threshold: Min heuristic score (0-10) to route without Ollama
        skip_patterns: Regex patterns for prompts to skip routing
        skip_acknowledgements: Skip routing for "yes", "ok", "thanks", etc.
        route_coordination: Route git/deploy/test/execution tasks
        prefer_ollama: Always try Ollama first before Claude (budget mode)
    """

    name: str
    description: str
    confidence_threshold: int = 4
    skip_patterns: List[str] = field(default_factory=list)
    skip_acknowledgements: bool = False
    route_coordination: bool = False
    prefer_ollama: bool = True

    def __post_init__(self):
        """Validate policy after creation."""
        if not 0 <= self.confidence_threshold <= 10:
            raise ValueError(
                f"confidence_threshold must be 0-10, got {self.confidence_threshold}"
            )
        if not self.name.isidentifier():
            raise ValueError(f"Policy name must be valid identifier, got '{self.name}'")

    def skip_prompt(self, text: str) -> bool:
        """Check if prompt should skip routing based on this policy.

        Args:
            text: User prompt text

        Returns:
            True if prompt should be skipped (not routed)
        """
        # Check skip patterns
        for pattern in self.skip_patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    return True
            except re.error as e:
                # Log but don't fail on invalid regex
                print(f"⚠️ Invalid regex in policy skip_patterns: {pattern}: {e}")

        # Check acknowledgements
        if self.skip_acknowledgements:
            ack_pattern = r'^\s*(yes|yeah|yep|y|no|nope|n|ok|okay|sure|thanks|thank you|cool|got it|good|sure thing)\s*$'
            if re.match(ack_pattern, text.strip(), re.IGNORECASE):
                return True

        return False


class PolicyManager:
    """Manages loading, switching, and persisting routing policies."""

    DEFAULT_POLICY_DIR = Path.home() / ".llm-router" / "policies"
    PRESET_POLICY_DIR = Path(__file__).parent / "policies"

    def __init__(self):
        """Initialize policy manager."""
        self._active_policy: Optional[RoutingPolicy] = None
        self._policy_cache: Dict[str, RoutingPolicy] = {}
        self._ensure_policy_dir()

    def _ensure_policy_dir(self) -> None:
        """Create policy directory if it doesn't exist."""
        self.DEFAULT_POLICY_DIR.mkdir(parents=True, exist_ok=True)

    def load_policy(self, name: str) -> RoutingPolicy:
        """Load a policy by name.

        Searches in order:
        1. User custom policies (~/.llm-router/policies/)
        2. Preset policies (bundled with llm-router)

        Args:
            name: Policy name (without .yaml extension)

        Returns:
            Loaded RoutingPolicy

        Raises:
            FileNotFoundError: If policy not found
            ValueError: If policy YAML is invalid
        """
        # Check cache first
        if name in self._policy_cache:
            return self._policy_cache[name]

        # Try user custom policies
        user_policy_path = self.DEFAULT_POLICY_DIR / f"{name}.yaml"
        if user_policy_path.exists():
            policy = self._load_yaml_policy(user_policy_path)
            self._policy_cache[name] = policy
            return policy

        # Try preset policies
        preset_path = self.PRESET_POLICY_DIR / f"{name}.yaml"
        if preset_path.exists():
            policy = self._load_yaml_policy(preset_path)
            self._policy_cache[name] = policy
            return policy

        raise FileNotFoundError(
            f"Policy '{name}' not found in {self.DEFAULT_POLICY_DIR} or presets"
        )

    def _load_yaml_policy(self, path: Path) -> RoutingPolicy:
        """Load policy from YAML file.

        Args:
            path: Path to policy YAML file

        Returns:
            Loaded RoutingPolicy

        Raises:
            ValueError: If YAML is invalid or missing required fields
        """
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in {path}: {e}")
        except OSError as e:
            raise FileNotFoundError(f"Cannot read {path}: {e}")

        if not isinstance(data, dict):
            raise ValueError(f"Policy YAML must be a dict, got {type(data)}")

        # Validate required fields
        required = {"name", "description"}
        if not required.issubset(data.keys()):
            missing = required - set(data.keys())
            raise ValueError(f"Missing required fields: {missing}")

        # Build policy with defaults
        return RoutingPolicy(
            name=data["name"],
            description=data["description"],
            confidence_threshold=int(data.get("confidence_threshold", 4)),
            skip_patterns=data.get("skip_patterns", []),
            skip_acknowledgements=bool(data.get("skip_acknowledgements", False)),
            route_coordination=bool(data.get("route_coordination", False)),
            prefer_ollama=bool(data.get("prefer_ollama", True)),
        )

    def set_active_policy(self, name: str) -> RoutingPolicy:
        """Set the active routing policy.

        Args:
            name: Policy name to activate

        Returns:
            The activated policy

        Raises:
            FileNotFoundError: If policy doesn't exist
        """
        policy = self.load_policy(name)
        self._active_policy = policy
        return policy

    def get_active_policy(self) -> RoutingPolicy:
        """Get the currently active policy.

        Loads from LLM_ROUTER_POLICY env var, or returns default (balanced).

        Returns:
            Active RoutingPolicy
        """
        if self._active_policy is not None:
            return self._active_policy

        policy_name = os.environ.get("LLM_ROUTER_POLICY", "balanced")
        try:
            return self.set_active_policy(policy_name)
        except FileNotFoundError:
            # Fallback to balanced if policy not found
            return self.set_active_policy("balanced")

    def save_custom_policy(self, policy: RoutingPolicy) -> Path:
        """Save a custom policy to ~/.llm-router/policies/.

        Args:
            policy: RoutingPolicy to save

        Returns:
            Path to saved policy file
        """
        self._ensure_policy_dir()
        path = self.DEFAULT_POLICY_DIR / f"{policy.name}.yaml"

        data = {
            "name": policy.name,
            "description": policy.description,
            "confidence_threshold": policy.confidence_threshold,
            "skip_patterns": policy.skip_patterns,
            "skip_acknowledgements": policy.skip_acknowledgements,
            "route_coordination": policy.route_coordination,
            "prefer_ollama": policy.prefer_ollama,
        }

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        # Clear cache so reload gets fresh copy
        if policy.name in self._policy_cache:
            del self._policy_cache[policy.name]

        return path

    def list_policies(self) -> Dict[str, str]:
        """List all available policies.

        Returns:
            Dict mapping policy name to description (preset + custom)
        """
        policies = {}

        # Preset policies
        for preset_path in self.PRESET_POLICY_DIR.glob("*.yaml"):
            try:
                policy = self._load_yaml_policy(preset_path)
                policies[policy.name] = policy.description
            except ValueError:
                pass  # Skip invalid policies

        # Custom policies
        for custom_path in self.DEFAULT_POLICY_DIR.glob("*.yaml"):
            try:
                policy = self._load_yaml_policy(custom_path)
                policies[policy.name] = policy.description
            except ValueError:
                pass

        return policies


# Global policy manager singleton
_policy_manager = PolicyManager()


def get_policy_manager() -> PolicyManager:
    """Get the global policy manager instance."""
    return _policy_manager


def get_active_policy() -> RoutingPolicy:
    """Get the currently active routing policy."""
    return _policy_manager.get_active_policy()


# ── Organization Policy (for org-level budget enforcement) ─────────────────────


@dataclass(frozen=True)
class OrgPolicy:
    """Organization-level policy for model/provider blocking and task caps.

    Attributes:
        block_providers: List of providers to block (e.g., ["openai"])
        block_models: List of model patterns to block (e.g., ["openai/gpt-4o", "openai/*"])
        allow_models: List of models to allow (blocklist → allowlist mode)
        task_caps: Dict mapping task type to daily spend cap in cents (e.g., {"code": 5000})
        source: Source of this policy ("default", "file", or "merged")
    """

    block_providers: list[str] = field(default_factory=list)
    block_models: list[str] = field(default_factory=list)
    allow_models: list[str] = field(default_factory=list)
    task_caps: dict[str, int] = field(default_factory=dict)
    source: str = "default"


def load_org_policy(path: Path | None = None) -> OrgPolicy | None:
    """Load organization policy from YAML file.

    Args:
        path: Path to org policy YAML file. Defaults to ~/.llm-router/org-policy.yaml

    Returns:
        OrgPolicy if file exists, else default permissive policy
    """
    if path is None:
        path = Path.home() / ".llm-router" / "org-policy.yaml"

    if not path.exists():
        return OrgPolicy(source="default")  # Return default permissive policy

    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return OrgPolicy(
            block_providers=data.get("block_providers", []),
            block_models=data.get("block_models", []),
            allow_models=data.get("allow_models", []),
            task_caps=data.get("task_caps", {}),
            source="file",
        )
    except (yaml.YAMLError, OSError):
        # On any error, return permissive default
        return OrgPolicy(source="default")


def get_task_cap(task_type: str, org_policy: OrgPolicy | None) -> int | None:
    """Get per-task daily spend cap for a task type.

    Args:
        task_type: Task type (e.g., "code", "analyze")
        org_policy: Organization policy (or None for no limit)

    Returns:
        Daily spend cap in cents, or None if no limit
    """
    if org_policy is None:
        return None
    return org_policy.task_caps.get(task_type)


def apply_policy(
    models: list[str],
    task_type: str,
    policy: OrgPolicy | None,
) -> tuple[list[str], list[str]]:
    """Filter a list of models based on org policy.

    Args:
        models: List of available models (e.g., ["ollama/qwen", "openai/gpt-4o"])
        task_type: Task type (for future per-task filtering)
        policy: Organization policy (or None for no filtering)

    Returns:
        Tuple of (allowed_models, blocked_models)
    """
    if policy is None:
        return models, []

    blocked = []
    allowed = []

    # If allowlist is configured, use it (default to empty = block all)
    if policy.allow_models:
        for model in models:
            if _matches_patterns(model, policy.allow_models):
                allowed.append(model)
            else:
                blocked.append(model)
        return allowed, blocked

    # Otherwise use blocklist
    for model in models:
        # Check if blocked by provider
        provider = model.split("/")[0] if "/" in model else model
        if provider in policy.block_providers:
            blocked.append(model)
            continue

        # Check if blocked by exact model or glob pattern
        if _matches_patterns(model, policy.block_models):
            blocked.append(model)
            continue

        allowed.append(model)

    return allowed, blocked


def _matches_patterns(text: str, patterns: list[str]) -> bool:
    """Check if text matches any pattern (supports glob wildcard *)."""
    for pattern in patterns:
        if "*" in pattern:
            # Convert glob to regex: "openai/*" → "^openai/.*$"
            regex = re.escape(pattern).replace(r"\*", ".*")
            if re.match(f"^{regex}$", text):
                return True
        elif text == pattern:
            return True
    return False


def policy_summary(policy: OrgPolicy) -> str:
    """Generate human-readable summary of an org policy.

    Args:
        policy: Organization policy

    Returns:
        Text summary of policy restrictions
    """
    if not any([policy.block_providers, policy.block_models, policy.allow_models, policy.task_caps]):
        return "No org policy configured. All providers available."

    lines = ["Organization Policy:"]

    if policy.allow_models:
        lines.append(f"  ✅ Allow only: {', '.join(policy.allow_models)}")

    if policy.block_providers:
        lines.append(f"  ❌ Block providers: {', '.join(policy.block_providers)}")

    if policy.block_models:
        lines.append(f"  ❌ Block models: {', '.join(policy.block_models)}")

    if policy.task_caps:
        cap_strs = [f"{k}=${v/100:.2f}" for k, v in policy.task_caps.items()]
        lines.append(f"  💰 Task caps: {', '.join(cap_strs)}")

    return "\n".join(lines)
