"""Verify the RouterArena LLMRouterRouter selects sensibly on representative prompts.

Two things this guards against:

1. The inlined heuristic regexes drift away from
   ``src/llm_router/hooks/auto-route.py`` and stop matching prompts that
   production would classify as code/math/reasoning.
2. The override rules (code → coder, reasoning → deepseek) silently get
   reordered so that the simpler tier rule fires first.

We mock ``BaseRouter`` so the test can run without checking out
RouterArena's repo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _install_fake_base_router() -> ModuleType:
    """Inject a minimal ``router_inference.router.base_router`` shim.

    The real BaseRouter loads config from disk; for the test we let the
    subclass populate ``self.models`` directly via the constructor.
    """
    base_pkg = ModuleType("router_inference")
    base_pkg.__path__ = []
    inner_pkg = ModuleType("router_inference.router")
    inner_pkg.__path__ = []
    module = ModuleType("router_inference.router.base_router")

    class BaseRouter:
        def __init__(self, router_name: str = "llm-routing") -> None:
            self.router_name = router_name
            self.models = list(_DEFAULT_POOL)

        def get_prediction(self, query: str) -> str:
            picked = self._get_prediction(query)
            assert picked in self.models, (
                f"router returned {picked!r} not in pool {self.models}"
            )
            return picked

        def _get_prediction(self, query: str) -> str:  # pragma: no cover
            raise NotImplementedError

    module.BaseRouter = BaseRouter
    sys.modules["router_inference"] = base_pkg
    sys.modules["router_inference.router"] = inner_pkg
    sys.modules["router_inference.router.base_router"] = module
    return module


_DEFAULT_POOL = (
    "qwen/qwen3-235b-a22b-2507",
    "google/gemini-3.1-flash-lite",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v3.2",
    "qwen/qwen3-next-80b-a3b-instruct",
    "Qwen/Qwen3-Coder-Next",
    "gemini-2.5-flash",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "gpt-4o-mini",
    "claude-3-haiku-20240307",
)


@pytest.fixture(scope="module")
def llm_router_router():
    _install_fake_base_router()
    spec = importlib.util.spec_from_file_location(
        "routerarena_submission_llm_router_router",
        Path(__file__).resolve().parents[1]
        / "bench" / "routerarena" / "submission"
        / "router"
        / "llm_router_router.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.LLMRouterRouter()


# ── Override rules win over tier rules ─────────────────────────────────


def test_code_prompt_picks_coder_specialist(llm_router_router):
    prompt = "Write a Python function that reverses a linked list in place."
    assert llm_router_router.get_prediction(prompt) == "Qwen/Qwen3-Coder-Next"


def test_math_complex_picks_deepseek(llm_router_router):
    prompt = (
        "Analyze the convergence of the series sum 1/n^p for p > 1 and "
        "derive the value when p = 2 using the Basel problem."
    )
    pick = llm_router_router.get_prediction(prompt)
    assert pick == "deepseek/deepseek-v4-flash", (
        f"math+complex should route to deepseek, got {pick}"
    )


# ── Tier rules apply when no override fires ────────────────────────────


def test_simple_question_picks_flash_lite(llm_router_router):
    pick = llm_router_router.get_prediction("What is the capital of France?")
    assert pick == "google/gemini-3.1-flash-lite"


def test_moderate_question_picks_gpt4o_mini(llm_router_router):
    # 200+ chars puts it firmly in "moderate" by the length rule.
    prompt = (
        "Describe the differences between supervised and unsupervised "
        "learning, including a concrete example application of each "
        "approach in a domain other than image recognition. Keep it brief."
    )
    pick = llm_router_router.get_prediction(prompt)
    assert pick == "gpt-4o-mini", f"moderate length should hit gpt-4o-mini, got {pick}"


def test_complex_analyze_picks_frontier(llm_router_router):
    prompt = (
        "Analyze the architectural tradeoffs of using a B-tree versus a "
        "LSM-tree for a write-heavy time-series database. Compare write "
        "amplification, read latency, and space efficiency under bursty "
        "workloads. Cite specific failure modes for each."
    )
    pick = llm_router_router.get_prediction(prompt)
    assert pick == "qwen/qwen3-235b-a22b-2507", (
        f"complex analyze should route to frontier, got {pick}"
    )


# ── Pool guarantees ────────────────────────────────────────────────────


def test_returned_model_is_always_in_pool(llm_router_router):
    """Sample several prompts and confirm every pick is in the pool.

    BaseRouter.get_prediction asserts the same thing — this test makes
    sure the assertion would catch a drift rather than silently allowing
    an off-pool name through.
    """
    prompts = [
        "What is 2 + 2?",
        "def fizzbuzz(n): ?",
        "Compare the impact of monetary policy versus fiscal stimulus.",
        "Explain why entropy increases in an isolated system.",
        "",  # empty edge case
    ]
    for p in prompts:
        pick = llm_router_router.get_prediction(p)
        assert pick in _DEFAULT_POOL, f"off-pool pick for {p!r}: {pick}"
