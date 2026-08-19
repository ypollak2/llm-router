"""Agent registry — load AgentProfile definitions from YAML config.

Profiles live in `config/agents.yaml`. The registry is constructed once at
process start; lookups are O(1). Hot-reload is not supported — restart the
MCP server to pick up changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from llm_router.agents.base import AgentProfile


class AgentNotFound(KeyError):
    """Raised when an agent_id isn't in the registry."""


@dataclass
class AgentRegistry:
    """In-memory map of agent_id → AgentProfile.

    Constructed from a list of profiles (for tests) or from YAML (for
    production). Validation happens at load time, not at lookup time.
    """

    profiles: dict[str, AgentProfile]

    @classmethod
    def from_profiles(cls, profiles: Iterable[AgentProfile]) -> "AgentRegistry":
        out: dict[str, AgentProfile] = {}
        for p in profiles:
            if p.id in out:
                raise ValueError(f"duplicate agent profile id: {p.id}")
            out[p.id] = p
        return cls(profiles=out)

    @classmethod
    def from_yaml(cls, path: Path) -> "AgentRegistry":
        """Parse a YAML config like config/agents.yaml."""
        import yaml  # lazy: keep registry usable without yaml in dev/test

        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict) or "agents" not in raw:
            raise ValueError(f"{path}: expected top-level 'agents:' list")

        profiles: list[AgentProfile] = []
        for entry in raw["agents"]:
            profiles.append(_parse_profile(entry))
        return cls.from_profiles(profiles)

    def get(self, agent_id: str) -> AgentProfile:
        if agent_id not in self.profiles:
            raise AgentNotFound(agent_id)
        return self.profiles[agent_id]

    def list_ids(self) -> list[str]:
        return sorted(self.profiles.keys())

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self.profiles


def _parse_profile(entry: dict) -> AgentProfile:
    """Validate + build one AgentProfile from a YAML entry."""
    required = ("id", "description")
    missing = [k for k in required if k not in entry]
    if missing:
        raise ValueError(f"agent profile missing required keys: {missing}")

    routing = entry.get("routing_profile", {}) or {}
    budget = entry.get("budget", {}) or {}

    tier_pref = tuple(routing.get("tier_preference", ()))
    signal_boosts = dict(routing.get("signal_boosts", {}))
    preferred_chain = str(routing.get("preferred_chain", ""))

    default_usd = float(budget.get("default_usd", 0.50))
    hard_max_usd = float(budget.get("hard_max_usd", 2.00))

    if default_usd > hard_max_usd:
        raise ValueError(
            f"agent {entry['id']}: default_usd ({default_usd}) > hard_max_usd ({hard_max_usd})"
        )

    return AgentProfile(
        id=str(entry["id"]),
        description=str(entry["description"]),
        tier_preference=tier_pref,
        signal_boosts=signal_boosts,
        preferred_chain=preferred_chain,
        default_budget_usd=default_usd,
        hard_max_budget_usd=hard_max_usd,
    )
