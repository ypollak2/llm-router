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


# Exact (top-level function, source segment) pairs that are allowed to look like a numeric
# default. Why pairs and not whole functions: skipping a function skipped its nested defs too,
# so `pressure.get(..., 0.3)` in _policy_dynamic's nested _quota, or `_rr_counters.get(key, 0)
# or 0`, passed. A pair allows only the expression that exists today; any other default in the
# same function is flagged, and a pair whose text no longer occurs fails the stale check.
# Each entry needs a one-line reason.
ALLOWED_DEFAULT_EXPRESSIONS = {
    ("_round_robin_pick", "_rr_counters.get(key, 0)"):
        "round-robin counter: 0 is a start index, not a score",
    ("_policy_quota_exhaustion", "pressure.get(_provider_from_model(m), 0.0)"):
        "quota pressure: 0.0 means no pressure, not a score",
    ("_policy_dynamic", "pressure.get(_provider_from_model(m), 0.0)"):
        "quota pressure lookup in the sort key; orders by pressure, not quality/price",
}
# Module-level numeric constants in (0, 1) read by a function. Empty: main has none.
ALLOWED_MODULE_CONSTANTS: dict[str, str] = {}


def _top_level_functions(tree: ast.Module):
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def find_invented_defaults(src: str, allowed: dict | frozenset | set = frozenset(),
                           allowed_constants: dict | frozenset | set = frozenset()) -> list[str]:
    """Scan a whole module for invented-default shapes; return one description per hit.

    Shapes: the guessed literals 0.5 / 0.4 / 0.001 anywhere, a numeric default in
    ``.get(k, <num>)``, a numeric branch of ``<num> if v is None else ...`` /
    ``... if v is None else <num>``, ``v or <num>``, and a module-level numeric constant
    in (0, 1) that any function reads (``_FALLBACK = 0.3`` used bare escapes the shapes).

    Every function, nested defs included, gets the shape rules. A shape hit is suppressed
    only when its (outermost function name, exact source text) is in ``allowed``.
    """
    hits: list[str] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float) \
                and node.value in _DEFAULT_VALUES:
            hits.append(f"literal {node.value} at line {node.lineno}")
    owner: dict[int, str] = {}
    for fn in _top_level_functions(tree):
        for n in ast.walk(fn):
            owner[id(n)] = fn.name
    for node in ast.walk(tree):
        shape = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) == 2 and _is_num(node.args[1]):
            shape = ".get numeric default"
        elif isinstance(node, ast.IfExp) and (_is_num(node.body) or _is_num(node.orelse)):
            shape = "numeric ternary branch"
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) \
                and any(_is_num(v) for v in node.values[1:]):
            shape = "`or <num>` default"
        if shape and (owner.get(id(node)), ast.get_source_segment(src, node)) not in allowed:
            hits.append(f"{shape} at line {node.lineno}: {ast.get_source_segment(src, node)!r}")
    consts = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and _is_num(n.value) and 0 < n.value.value < 1:
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id not in allowed_constants:
                    consts[t.id] = n.lineno
    for fn in _top_level_functions(tree):
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in consts:
                hits.append(f"module constant {n.id} (line {consts[n.id]}) read in {fn.name} "
                            f"at line {n.lineno}")
    return hits


def test_scan_finds_defaults_in_old_policy_fixture():
    # The fixture is a committed copy of the pre-change source: no git history needed.
    hits = find_invented_defaults(OLD_POLICY.read_text(), ALLOWED_DEFAULT_EXPRESSIONS)
    assert len([h for h in hits if h.startswith("literal")]) == 3, hits  # 0.001, 0.5, 0.4
    assert any(".get numeric default" in h for h in hits), hits


@pytest.mark.parametrize("mutant", [
    "_FALLBACK = 0.5\ndef f(v):\n    return _FALLBACK if v is None else v\n",
    "def f(v):\n    return 7 if v is None else v\n",
    "def f(d):\n    return d.get('k', 3)\n",
    "def f(v):\n    return v or 2\n",
    # a NEW scoring helper with a literal other than 0.5/0.4/0.001 must not escape (mutant m7)
    "def _policy_bonus(m):\n    return m.get('bonus', 0.3)\n",
    # module constant read bare: no shape matches, only the constant rule catches it
    "_FALLBACK = 0.3\ndef f(q):\n    return q if q is not None else _FALLBACK\n",
])
def test_scan_catches_mutant_shapes(mutant):
    assert find_invented_defaults(mutant), mutant


def test_allowlist_has_no_stale_entries():
    """Every allowed (function, expression) pair must still occur in that function."""
    src = POLICY.read_text()
    fns = {f.name: f for f in _top_level_functions(ast.parse(src))}
    stale = []
    for fname, seg in ALLOWED_DEFAULT_EXPRESSIONS:
        fn = fns.get(fname)
        segs = {ast.get_source_segment(src, n) for n in ast.walk(fn)} if fn else set()
        if seg not in segs:
            stale.append((fname, seg))
    assert not stale, stale


def test_scan_does_not_skip_nested_defs_of_allowed_functions():
    src = ("def _policy_dynamic(chain):\n    def _quota(m):\n"
           "        return pressure.get(m, 0.3)\n    return _quota\n")
    assert find_invented_defaults(src, ALLOWED_DEFAULT_EXPRESSIONS)
    ok = ("def _round_robin_pick(c):\n    return _rr_counters.get(key, 0)\n")
    assert find_invented_defaults(ok, ALLOWED_DEFAULT_EXPRESSIONS) == []
    assert find_invented_defaults("def _round_robin_pick(c):\n    return _rr_counters.get(key, 0) or 0\n",
                                  ALLOWED_DEFAULT_EXPRESSIONS)


def test_no_invented_defaults_in_whole_policy_module():
    assert find_invented_defaults(POLICY.read_text(), ALLOWED_DEFAULT_EXPRESSIONS,
                                  ALLOWED_MODULE_CONSTANTS) == []


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
    """DESIGN: this pin fails on any legitimate weight retune; update the pin with the retune.

    score_all_models feeds chain_builder._build_dynamic_chain (always on). Pins the score
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
