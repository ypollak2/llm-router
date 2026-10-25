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


# Functions in user_routing_policy.py that are NOT quality/cost scoring. Every other function
# (including any helper added later) gets the shape rules, so a new scorer cannot escape the
# scan by using a literal other than 0.5 / 0.4 / 0.001. Each entry needs a one-line reason.
NON_SCORING_FUNCTIONS = {
    "_round_robin_pick": "round-robin counter: _rr_counters.get(key, 0) is an index, not a score",
    "_policy_quota_exhaustion": "quota pressure: pressure.get(provider, 0.0) means no pressure, not a score",
    "_policy_dynamic": "quota pressure lookup in the sort key; orders by pressure, not quality/price",
}


def find_invented_defaults(src: str, allowlist: frozenset[str] | set[str] | dict = frozenset()) -> list[str]:
    """Scan a whole module for invented-default shapes; return one description per hit.

    Shapes: the guessed literals 0.5 / 0.4 / 0.001 anywhere (this also catches a named
    constant such as ``_FALLBACK = 0.5``), a numeric default in ``.get(k, <num>)``, a
    numeric branch of ``<num> if v is None else ...`` / ``... if v is None else <num>``,
    and ``v or <num>``.

    The literal rule covers the whole module. The shape rules cover every function and all
    module-level code except the functions named in ``allowlist``.
    """
    hits: list[str] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float) \
                and node.value in _DEFAULT_VALUES:
            hits.append(f"literal {node.value} at line {node.lineno}")
    skipped: set[int] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in allowlist:
            skipped.update(id(n) for n in ast.walk(fn))
    for node in (n for n in ast.walk(tree) if id(n) not in skipped):
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
    hits = find_invented_defaults(OLD_POLICY.read_text(), NON_SCORING_FUNCTIONS)
    assert len([h for h in hits if h.startswith("literal")]) == 3, hits  # 0.001, 0.5, 0.4
    assert any(".get numeric default" in h for h in hits), hits


@pytest.mark.parametrize("mutant", [
    "_FALLBACK = 0.5\ndef f(v):\n    return _FALLBACK if v is None else v\n",
    "def f(v):\n    return 7 if v is None else v\n",
    "def f(d):\n    return d.get('k', 3)\n",
    "def f(v):\n    return v or 2\n",
    # a NEW scoring helper with a literal other than 0.5/0.4/0.001 must not escape (mutant m7)
    "def _policy_bonus(m):\n    return m.get('bonus', 0.3)\n",
])
def test_scan_catches_mutant_shapes(mutant):
    assert find_invented_defaults(mutant), mutant


def test_non_scoring_allowlist_has_no_stale_entries():
    names = {n.name for n in ast.walk(ast.parse(POLICY.read_text()))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert set(NON_SCORING_FUNCTIONS) <= names, set(NON_SCORING_FUNCTIONS) - names


def test_no_invented_defaults_in_whole_policy_module():
    assert find_invented_defaults(POLICY.read_text(), NON_SCORING_FUNCTIONS) == []


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
    "weakvendor/weak-model",
]
# weak-model has a MEASURED score below the neutral 0.5 that unknown models get from the
# composite scorer today. It must stay BELOW the unknowns: a scorer that gave unknowns 0.0
# (mutant m5b) would lift it above them while unknowns still sorted after the strong models.
_CHAIN_BENCH = {"task_scores": {"code": {
    "openai/gpt-4o": 0.85, "gemini/gemini-2.5-flash": 0.7, "claude-sonnet-4-6": 0.9,
    "weakvendor/weak-model": 0.2}}}
_UNKNOWN = {"ollama/mystery-7b", "newvendor/unbenchmarked"}
# Score groups, best first, computed on origin/main (9f4171ca) with HOME isolated; hard-coded
# here. Only the two unknowns tie, so they are the only multi-member group.
_PINNED_GROUPS = [
    {"claude-sonnet-4-6"}, {"openai/gpt-4o"}, {"ollama/qwen3-coder:30b"},
    {"gemini/gemini-2.5-flash"}, _UNKNOWN, {"weakvendor/weak-model"},
]
# Composite score of an unknown-quality model (and of the weak model), per complexity.
_PINNED_UNKNOWN_SCORE = {"simple": 0.9, "moderate": 0.825, "complex": 0.725}
_PINNED_WEAK_SCORE = {"simple": 0.84, "moderate": 0.72, "complex": 0.56}


@pytest.mark.parametrize("complexity", sorted(_PINNED_UNKNOWN_SCORE))
def test_default_chain_scoring_order_unchanged_vs_main(complexity, tmp_path, monkeypatch):
    """score_all_models feeds chain_builder._build_dynamic_chain (always on). Pins the score
    groups exactly (a tied group cannot swap members with a neighbour) and the numeric score
    of unknown vs weak-known models, so an unknown scored 0.0 goes red."""
    import asyncio
    from llm_router.scorer import score_all_models
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("llm_router.benchmarks.get_benchmark_data", return_value=_CHAIN_BENCH):
        scored = asyncio.run(score_all_models(list(reversed(_CHAIN_MODELS)), "code", complexity))
    assert len(scored) == len(_CHAIN_MODELS)
    groups: list[set[str]] = []
    last = None
    for s in scored:
        if last is None or abs(s.score - last) > 1e-9:
            groups.append(set())
        groups[-1].add(s.model_id)
        last = s.score
    assert groups == _PINNED_GROUPS
    by_id = {s.model_id: s.score for s in scored}
    for m in _UNKNOWN:
        assert by_id[m] == pytest.approx(_PINNED_UNKNOWN_SCORE[complexity], abs=1e-6)
    assert by_id["weakvendor/weak-model"] == pytest.approx(_PINNED_WEAK_SCORE[complexity], abs=1e-6)
    assert by_id["ollama/qwen3-coder:30b"] > by_id["ollama/mystery-7b"]
    assert by_id["ollama/mystery-7b"] > by_id["weakvendor/weak-model"]
