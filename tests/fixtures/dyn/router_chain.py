"""Drive ``router._build_and_filter_chain`` with no ambient state (usable as a script and in tests).

Works unchanged on a tree without the P0.15 gate, so the same fixtures produce the
recorded "today" chains (``p015_main_chains.json``) from origin/main.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

PROFILES = ("BUDGET", "BALANCED", "PREMIUM")
TASKS = ("QUERY", "GENERATE", "ANALYZE", "CODE")
COMPLEXITY = {"BUDGET": "SIMPLE", "BALANCED": "MODERATE", "PREMIUM": "COMPLEX"}


class FakeConfig:
    """The attributes ``_build_and_filter_chain`` reads."""

    def __init__(self, *, ollama=(), compat=(), configured_ollama="", configured_compat="",
                 routing_policy="balanced", providers=("anthropic", "openai", "deepseek", "gemini")):
        self.available_providers = set(providers)
        self._ollama, self._compat = list(ollama), list(compat)
        self.ollama_budget_models = configured_ollama
        self.openai_compat_models = configured_compat
        self.llm_router_claude_subscription = False
        self.llm_router_gemini_subscription = False
        self.llm_router_claw_code = False
        self.llm_router_routing_policy = routing_policy
        self.llm_router_agentic_model = ""

    def all_ollama_models(self):
        return list(self._ollama)

    def all_openai_compat_models(self):
        return list(self._compat)


@contextmanager
def isolated(repo_cfg=None):
    from llm_router.repo_config import RepoConfig

    with ExitStack() as st:
        for target, value in (
            ("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *a, **k: None),
            ("llm_router.cost.get_model_failure_rates", AsyncMock(return_value={})),
            ("llm_router.cost.get_model_latency_stats", AsyncMock(return_value={})),
            ("llm_router.cost.get_model_acceptance_scores", AsyncMock(return_value={})),
            ("llm_router.claude_usage.get_claude_pressure", lambda: 0.0),
            ("llm_router.policy.load_org_policy", lambda *a, **k: None),
            ("llm_router.router.get_repo_config", lambda *a, **k: repo_cfg or RepoConfig()),
            ("llm_router.router.is_codex_available", lambda: False),
            ("llm_router.router.is_gemini_cli_available", lambda: False),
            ("llm_router.router.get_active_agent", lambda: None),
            ("llm_router.quota_balance.get_provider_pressures",
             AsyncMock(return_value={"claude": 0.5, "gemini_cli": 0.5, "codex": 0.5})),
        ):
            st.enter_context(patch(target, value))
        yield


def build(profile: str, task: str, config: FakeConfig, repo_cfg=None) -> list[str]:
    from llm_router.router import _build_and_filter_chain
    from llm_router.types import Complexity, RoutingProfile, TaskType

    c = Complexity[COMPLEXITY[profile]]
    with isolated(repo_cfg):
        return asyncio.run(_build_and_filter_chain(
            TaskType[task], RoutingProfile[profile], None, c, c, config))


def all_chains(config: FakeConfig, repo_cfg=None) -> dict[str, list[str]]:
    """The 12 chains: 3 profiles x 4 task types."""
    return {f"{p}/{t}": build(p, t, config, repo_cfg) for p in PROFILES for t in TASKS}


INJECTED = (
    "ollama/old-a:7b", "ollama/new-b:7b", "ollama/new-c:70b", "ollama/cfg-d:8b",
    "openai_compat/new-x", "openai_compat/cfg-y",
)

FIXTURES = {
    "ollama_ABCD": dict(ollama=[INJECTED[0], INJECTED[1], INJECTED[2], INJECTED[3]], configured_ollama="cfg-d:8b"),
    "ollama_B_only": dict(ollama=[INJECTED[1]]),
    "compat_X_only": dict(compat=[INJECTED[4]]),
    "mixed_all": dict(ollama=list(INJECTED[:4]), compat=list(INJECTED[4:]),
                      configured_ollama="cfg-d:8b", configured_compat="cfg-y"),
    "ollama_ABCD_cost_policy": dict(ollama=list(INJECTED[:4]), configured_ollama="cfg-d:8b", routing_policy="cost"),
    "none": dict(),
}


def pattern(chain: list[str]) -> list[str]:
    """The chain with every entry that is not one of the fixture's models collapsed to ``.``.

    Keeps what the escape-valve test is about (where the injected models sit) and
    ignores the static YAML chains, which other changes may legitimately edit.
    """
    out: list[str] = []
    for m in chain:
        token = m if m in INJECTED else "."
        if token == "." and out and out[-1] == ".":
            continue
        out.append(token)
    return out
