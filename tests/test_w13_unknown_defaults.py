"""W1-3 / P1.11-c (INVENTED-DEFAULTS-1): unknown scores stay unknown.

An invented quality (0.5 / 0.4), price (0.001) or fallback (0.50) made an unmeasured
model look measured. These tests pin that the constants are gone and that consumers
receive ``None`` and sort unknown last.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_router import benchmarks
from llm_router.user_routing_policy import apply_routing_policy

ROOT = Path(__file__).resolve().parents[1]
OLD_SHA = "6bb16aeb"  # main before this change: the constants exist here
POLICY = "src/llm_router/user_routing_policy.py"
BENCH = "src/llm_router/benchmarks.py"
_SCORING_FUNCS = {"_policy_cost", "_policy_quality"}
_DEFAULT_VALUES = {0.5, 0.4, 0.001}


def count_defaults(policy_src: str, bench_src: str) -> dict[str, int]:
    """Count the invented scoring defaults and the local quality table by AST."""
    out = {"0.5": 0, "0.4": 0, "0.001": 0, "0.50": 0, "_LOCAL_QUALITY_SCORES": 0}
    for fn in ast.walk(ast.parse(policy_src)):
        if isinstance(fn, ast.FunctionDef) and fn.name in _SCORING_FUNCS:
            for node in ast.walk(fn):
                if isinstance(node, ast.Constant) and isinstance(node.value, float) \
                        and node.value in _DEFAULT_VALUES:
                    out[repr(node.value)] += 1
    for node in ast.walk(ast.parse(bench_src)):
        if isinstance(node, ast.Name) and node.id == "_LOCAL_QUALITY_SCORES":
            out["_LOCAL_QUALITY_SCORES"] += 1
        if isinstance(node, ast.Name) and node.id == "_DEFAULT_QUALITY":
            out["0.50"] += 1  # the 0.50 default and every use of it
    return out


def _git_show(sha: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def test_defaults_found_on_old_commit():
    p, b = _git_show(OLD_SHA, POLICY), _git_show(OLD_SHA, BENCH)
    if p is None or b is None:
        pytest.skip(f"{OLD_SHA} not reachable in this checkout (shallow clone)")
    c = count_defaults(p, b)
    assert c["0.5"] == 1 and c["0.4"] == 1 and c["0.001"] == 1, c
    assert c["0.50"] >= 1 and c["_LOCAL_QUALITY_SCORES"] >= 1, c


def test_no_defaults_on_head():
    c = count_defaults((ROOT / POLICY).read_text(), (ROOT / BENCH).read_text())
    assert c == {"0.5": 0, "0.4": 0, "0.001": 0, "0.50": 0, "_LOCAL_QUALITY_SCORES": 0}


# -- consumers: unknown -> None -------------------------------------------------

def test_unknown_model_quality_is_none():
    with patch("llm_router.benchmarks.get_benchmark_data", return_value=None):
        assert benchmarks.get_quality_score("unknown/mystery-model", "code") is None
        assert benchmarks.get_quality_score("ollama/qwen3:32b", "code") is None


def test_benchmark_failure_is_none_not_a_number():
    with patch("llm_router.benchmarks.get_benchmark_data", side_effect=RuntimeError("x")):
        assert benchmarks.get_quality_score("openai/gpt-4o", "code") is None


def test_known_benchmark_score_still_returned():
    data = {"task_scores": {"code": {"openai/gpt-4o": 0.85}}}
    with patch("llm_router.benchmarks.get_benchmark_data", return_value=data):
        assert benchmarks.get_quality_score("openai/gpt-4o", "code") == pytest.approx(0.85)
        assert benchmarks.get_quality_score("openai/gpt-4o", "query") is None


def test_scorer_gives_unknown_quality_zero_contribution():
    from llm_router import scorer
    with patch.object(scorer, "get_quality_score", return_value=None):
        assert scorer._quality_score_adjusted("x/y", "code") == 0.0


# -- policy ordering ------------------------------------------------------------

def test_quality_policy_sorts_unknown_last_including_local(monkeypatch):
    monkeypatch.setattr("llm_router.user_routing_policy._load_quality_scores",
                        lambda: {"openai/gpt-4o": {"code": 0.30}, "ollama/known": {"code": 0.2}})
    chain = ["ollama/unknown", "openai/gpt-4o", "mystery/m", "ollama/known"]
    # Known scores below the old invented 0.5/0.4 still beat unknown; stable among unknowns.
    assert apply_routing_policy(chain, "quality", task_type="code") == [
        "openai/gpt-4o", "ollama/known", "ollama/unknown", "mystery/m"]


def test_cost_policy_sorts_unknown_price_last(monkeypatch):
    import litellm
    monkeypatch.setattr(litellm, "model_cost", {
        "gpt-4o": {"output_cost_per_token": 5.0},  # dearer than the old invented 0.001
        "cheap": {"output_cost_per_token": 0.0000002},
    })
    chain = ["mystery/m", "openai/gpt-4o", "ollama/x", "openai/cheap", "other/zzz"]
    assert apply_routing_policy(chain, "cost", task_type="query") == [
        "ollama/x", "openai/cheap", "openai/gpt-4o", "mystery/m", "other/zzz"]


def test_balanced_order_unchanged_pinned():
    chain = ["mystery/m", "ollama/x", "openai/gpt-4o", "anthropic/claude-sonnet-4-6"]
    assert apply_routing_policy(chain, "balanced", task_type="code") == chain


def test_local_first_untouched():
    chain = ["openai/gpt-4o", "ollama/x", "mystery/m"]
    assert apply_routing_policy(chain, "local-first") == ["ollama/x", "openai/gpt-4o", "mystery/m"]


# -- window: request size is not a reported window ------------------------------

def test_default_num_ctx_still_governs_request_size():
    from llm_router import local_models
    assert local_models.DEFAULT_NUM_CTX == 32768
    assert local_models.num_ctx("ollama/never-heard-of-it:7b") == 32768


def test_unlisted_model_context_window_is_unknown():
    from llm_router import resource_map
    assert resource_map.context_window_limit(None) == "unknown"
    assert resource_map.context_window_limit(131072) == 131072
