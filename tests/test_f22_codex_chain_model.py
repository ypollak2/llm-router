"""F22: a Codex chain entry is always a model the Codex CLI accepts.

Live ledger (execution_events, read 2026-10-17): most routed Codex calls first
tried ``codex/gpt-4o-mini``, which the Codex CLI rejects ("Model metadata ... not
found", HTTP 400), so each burned one failed attempt (~4-5 s) before the next model
answered (10-10: 14, 10-15: 8, 10-16: 15). The live config sets
``LLM_ROUTER_CODEX_MODELS=gpt-6-astra``, yet the chain still held gpt-4o-mini:
``policies/standard.yaml`` named ``codex/gpt-4o-mini`` (BUDGET) and ``codex/gpt-4o``
(BALANCED) literally, and the router's Codex injection only ADDED ``CODEX_MODELS``
entries, so the literal slot stayed in front of them. See docs/BUGS.md F22.

No network and no codex call: the env cases run in a subprocess that only imports
the routing tables.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from llm_router import codex_agent
from llm_router.codex_agent import _load_codex_models, accepted_codex_entry
from llm_router.policy import PolicyManager, _resolve_chain
from llm_router.types import Complexity, RoutingProfile, TaskType

POLICY_DIR = Path(codex_agent.__file__).parent / "policies"
REJECTED = ("codex/gpt-4o-mini", "codex/gpt-4o")

# The tiers that held a rejected Codex literal before F22.
AFFECTED = [
    (profile, task)
    for profile in ("budget", "balanced")
    for task in ("query", "generate", "analyze", "code")
]


def _repo_default(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.delenv("LLM_ROUTER_CODEX_MODELS", raising=False)
    return _load_codex_models()


# ── The data files ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", sorted(POLICY_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_policy_files_name_only_accepted_codex_models(path, monkeypatch):
    """Raw YAML, before any load-time rewrite: no rejected Codex model is listed."""
    default = set(_repo_default(monkeypatch))
    data = yaml.safe_load(path.read_text())
    lists = [data.get("workhorses") or [], data.get("fallback_chain_complex") or []]
    for tasks in (data.get("chains") or {}).values():
        lists.extend((tasks or {}).values())
    bad = [m for chain in lists for m in chain or []
           if m.startswith("codex/") and m[len("codex/"):] not in default]
    assert not bad, f"{path.name} names Codex models the CLI rejects: {bad}"


# ── The shared load path ─────────────────────────────────────────────────────


def test_accepted_codex_entry_maps_rejected_models_to_first_accepted():
    assert accepted_codex_entry("codex/gpt-4o-mini", ["gpt-6-astra"]) == "codex/gpt-6-astra"
    assert accepted_codex_entry("codex/gpt-4o", ["gpt-5.5", "gpt-5.4"]) == "codex/gpt-5.5"
    assert accepted_codex_entry("codex/gpt-5.4", ["gpt-5.5", "gpt-5.4"]) == "codex/gpt-5.4"
    assert accepted_codex_entry("codex/gpt-4o-mini", []) is None
    # Non-Codex entries are never touched, including openai/gpt-4o-mini.
    assert accepted_codex_entry("openai/gpt-4o-mini", ["gpt-6-astra"]) == "openai/gpt-4o-mini"


def test_resolve_chain_keeps_codex_slot_and_leaves_other_entries(monkeypatch):
    monkeypatch.setattr(codex_agent, "CODEX_MODELS", ["gpt-6-astra"])
    chain = ["ollama/a", "codex/gpt-4o-mini", "openai/gpt-4o-mini", "codex/o3",
             "openai/gpt-4o-mini"]
    # The Codex slot keeps its position; a second Codex entry that lands on the
    # same model is dropped; a repeated non-Codex entry is left as written.
    assert _resolve_chain(chain) == [
        "ollama/a", "codex/gpt-6-astra", "openai/gpt-4o-mini", "openai/gpt-4o-mini",
    ]


@pytest.mark.parametrize(("profile", "task"), AFFECTED)
def test_loaded_standard_policy_honours_configured_codex_models(profile, task, monkeypatch):
    monkeypatch.setattr(codex_agent, "CODEX_MODELS", ["gpt-6-astra"])
    chain = PolicyManager().load_policy("standard").chains[profile][task]
    codex = [m for m in chain if m.startswith("codex/")]
    assert codex == ["codex/gpt-6-astra"], chain


# ── The runtime tables, in a fresh interpreter per env ──────────────────────

_PROBE = r"""
import json
from llm_router.codex_agent import CODEX_MODELS
from llm_router.dynamic_routing import build_dynamic_routing_table
from llm_router.profiles import ROUTING_TABLE

dyn = build_dynamic_routing_table({"openai", "anthropic"})
print(json.dumps({
    "codex_models": CODEX_MODELS,
    "static": {f"{p.value}/{t.value}": c for (p, t), c in ROUTING_TABLE.items()},
    "dynamic": {f"{p.value}/{t.value}": c for (p, t), c in dyn.items()},
}))
"""


def _probe(tmp_path: Path, codex_models: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_CODEX_MODELS"}
    env["HOME"] = str(tmp_path)
    env["LLM_ROUTER_HOME"] = str(tmp_path / "llm-router")
    if codex_models is not None:
        env["LLM_ROUTER_CODEX_MODELS"] = codex_models
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True,
        timeout=60, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _codex_slots(table: dict) -> dict[str, list[str]]:
    return {k: [m for m in c if m.startswith("codex/")] for k, c in table.items()}


def test_env_override_sets_codex_slot_on_static_and_dynamic_tables(tmp_path):
    got = _probe(tmp_path, "gpt-6-astra")
    assert got["codex_models"] == ["gpt-6-astra"]
    for name in ("static", "dynamic"):
        slots = _codex_slots(got[name])
        for profile, task in AFFECTED:
            assert slots[f"{profile}/{task}"] == ["codex/gpt-6-astra"], (name, profile, task)
        everywhere = {m for s in slots.values() for m in s}
        assert everywhere <= {"codex/gpt-6-astra"}, (name, everywhere)


def test_env_unset_uses_repo_default_codex_model(tmp_path, monkeypatch):
    default = _repo_default(monkeypatch)
    got = _probe(tmp_path, None)
    assert got["codex_models"] == default
    for name in ("static", "dynamic"):
        slots = _codex_slots(got[name])
        for profile, task in AFFECTED:
            assert slots[f"{profile}/{task}"] == [f"codex/{default[0]}"], (name, profile, task)
        for chain in got[name].values():
            assert not set(REJECTED) & set(chain), (name, chain)


# ── The router path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("profile", "task"), AFFECTED)
async def test_router_chain_offers_no_rejected_codex_model(profile, task, mock_env, monkeypatch):
    from llm_router import router
    from llm_router.config import get_config

    monkeypatch.setattr(router, "is_codex_available", lambda: True)
    complexity = Complexity.SIMPLE if profile == "budget" else Complexity.MODERATE
    chain = await router._build_and_filter_chain(
        TaskType(task), RoutingProfile(profile), None, complexity.value, complexity,
        get_config(),
    )
    codex = [m for m in chain if m.startswith("codex/")]
    assert codex, f"Codex is available, so the chain must offer it: {chain}"
    assert not set(REJECTED) & set(chain), chain
    assert all(m[len("codex/"):] in codex_agent.CODEX_MODELS for m in codex), chain
