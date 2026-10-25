"""W1-3 / P1.11-c (INVENTED-DEFAULTS-1): unknown scores stay unknown.

An invented quality (0.5 / 0.4), price (0.001) or fallback (0.50) made an unmeasured
model look measured. These tests pin that the constants are gone and that consumers
receive ``None`` and sort unknown last.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_router.user_routing_policy import apply_routing_policy

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "src/llm_router/user_routing_policy.py"
OLD_POLICY = ROOT / "tests/fixtures/w13_old_user_routing_policy.txt"  # policy source at 6bb16aeb
_DEFAULT_VALUES = {0.5, 0.4, 0.001}


def _is_num(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
        and not isinstance(node.value, bool)


# Functions in user_routing_policy.py that turn quality/price into an order (the shape rules
# apply here; the literal rule applies to the whole module).
SCORING_FUNCTIONS = frozenset({"_load_quality_scores", "_policy_cost", "_sort_unknown_last", "_policy_quality"})


def find_invented_defaults(src: str, scoring_functions: frozenset[str] | None = None) -> list[str]:
    """Scan a whole module for invented-default shapes; return one description per hit.

    Shapes: the guessed literals 0.5 / 0.4 / 0.001 anywhere (this also catches a named
    constant such as ``_FALLBACK = 0.5``), a numeric default in ``.get(k, <num>)``, a
    numeric branch of ``<num> if v is None else ...`` / ``... if v is None else <num>``,
    and ``v or <num>``.

    The literal rule covers the whole module. The shape rules cover ``scoring_functions``
    only (every function when None): a round-robin counter's ``.get(key, 0)`` or a quota
    pressure ``.get(p, 0.0)`` is not a quality/cost score.
    """
    hits: list[str] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float) \
                and node.value in _DEFAULT_VALUES:
            hits.append(f"literal {node.value} at line {node.lineno}")
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and (scoring_functions is None or n.name in scoring_functions)]
    if scoring_functions is None:
        scopes = [tree]
    for node in (n for scope in scopes for n in ast.walk(scope)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) == 2 and _is_num(node.args[1]):
            hits.append(f".get numeric default at line {node.lineno}")
        if isinstance(node, ast.IfExp) and (_is_num(node.body) or _is_num(node.orelse)):
            hits.append(f"numeric ternary branch at line {node.lineno}")
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) \
                and any(_is_num(v) for v in node.values[1:]):
            hits.append(f"`or <num>` default at line {node.lineno}")
    return hits


def test_scan_finds_defaults_in_old_policy_fixture():
    # The fixture is a committed copy of the pre-change source: no git history needed.
    hits = find_invented_defaults(OLD_POLICY.read_text(), SCORING_FUNCTIONS)
    assert len([h for h in hits if h.startswith("literal")]) == 3, hits  # 0.001, 0.5, 0.4
    assert any(".get numeric default" in h for h in hits), hits


@pytest.mark.parametrize("mutant", [
    "_FALLBACK = 0.5\ndef f(v):\n    return _FALLBACK if v is None else v\n",
    "def f(v):\n    return 7 if v is None else v\n",
    "def f(d):\n    return d.get('k', 3)\n",
    "def f(v):\n    return v or 2\n",
])
def test_scan_catches_mutant_shapes(mutant):
    assert find_invented_defaults(mutant), mutant


def test_no_invented_defaults_in_whole_policy_module():
    assert find_invented_defaults(POLICY.read_text(), SCORING_FUNCTIONS) == []


# -- consumers: unknown -> None -------------------------------------------------

def test_policy_reads_measured_scores_only():
    """The policy loads the benchmark file directly, never benchmarks.get_quality_score,
    so the composite scorer's residual local table / 0.50 default cannot reach it."""
    src = POLICY.read_text()
    assert "get_quality_score" not in src and "_LOCAL_QUALITY_SCORES" not in src


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


# -- the default chain must not move (D-296: policy path only) -------------------

_CHAIN_MODELS = [
    "ollama/mystery-7b", "newvendor/unbenchmarked", "claude-sonnet-4-6",
    "ollama/qwen3-coder:30b", "openai/gpt-4o", "gemini/gemini-2.5-flash",
]
_CHAIN_BENCH = {"task_scores": {"code": {
    "openai/gpt-4o": 0.85, "gemini/gemini-2.5-flash": 0.7, "claude-sonnet-4-6": 0.9}}}
# Computed on origin/main (2c5a876e) with HOME isolated; hard-coded here.
_PINNED_ORDER = {
    "simple": ["claude-sonnet-4-6", "openai/gpt-4o", "ollama/qwen3-coder:30b",
               "gemini/gemini-2.5-flash", "ollama/mystery-7b", "newvendor/unbenchmarked"],
    "moderate": ["claude-sonnet-4-6", "openai/gpt-4o", "ollama/qwen3-coder:30b",
                 "gemini/gemini-2.5-flash", "ollama/mystery-7b", "newvendor/unbenchmarked"],
    "complex": ["claude-sonnet-4-6", "openai/gpt-4o", "ollama/qwen3-coder:30b",
                "gemini/gemini-2.5-flash", "ollama/mystery-7b", "newvendor/unbenchmarked"],
}


@pytest.mark.parametrize("complexity", sorted(_PINNED_ORDER))
def test_default_chain_scoring_order_unchanged_vs_main(complexity, tmp_path, monkeypatch):
    """score_all_models feeds chain_builder._build_dynamic_chain (always on). A known local
    model (qwen3-coder) must still outrank an unknown local one, as on origin/main."""
    import asyncio
    from llm_router.scorer import score_all_models
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("llm_router.benchmarks.get_benchmark_data", return_value=_CHAIN_BENCH):
        scored = asyncio.run(score_all_models(list(reversed(_CHAIN_MODELS)), "code", complexity))
    # Models with equal scores (the two unknowns tie) keep input order, so compare score groups:
    # the order of distinct scores is pinned, members of a tie are compared as a set.
    groups: list[set[str]] = []
    last = None
    for s in scored:
        if last is None or abs(s.score - last) > 1e-9:
            groups.append(set())
        groups[-1].add(s.model_id)
        last = s.score
    pinned = _PINNED_ORDER[complexity]
    assert [m for g in groups for m in sorted(g, key=pinned.index)] == pinned
    qwen = next(s for s in scored if s.model_id == "ollama/qwen3-coder:30b")
    mystery = next(s for s in scored if s.model_id == "ollama/mystery-7b")
    assert qwen.score > mystery.score
