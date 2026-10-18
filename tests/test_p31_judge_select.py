"""P3.1-3: canonical identity, strongest-other judge selection, Haiku only by config + flag."""

from __future__ import annotations

import ast
import pathlib
import random
from unittest.mock import patch

import pytest

from llm_router import failopen, judge_select
from llm_router.judge_select import LocalModel, canon, same_model, select_judge

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"
G = 1e9


def _inv(*names):
    return [LocalModel(name=f"ollama/{n}") for n in names]


# ── the proving test: fails on the pre-P3.1-3 selector ──────────────────────

def test_latest_alias_of_the_answerer_is_not_its_judge():
    sel = select_judge("ollama/foo:latest", _inv("foo", "bar"))
    assert sel.judge_model == "ollama/bar" and sel.judge_kind == "local"


def test_old_selector_path_via_judge_module():
    from llm_router.judge import _select_judge_model

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch("llm_router.discover.get_cached_ollama_models",
               return_value=["ollama/foo", "ollama/bar"]):
        assert _select_judge_model("ollama/foo:latest") == "ollama/bar"


# ── identity ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("foo", "foo:latest"), ("ollama/foo", "foo"), ("OLLAMA/Foo:latest", "foo"),
    ("anthropic/claude-haiku", "claude-haiku-4-5-20251001"),
    ("claude-haiku-4-5", "anthropic/claude-haiku-4-5-20251001"),
    ("", "foo"), ("foo", None),
])
def test_same_model_aliases_and_doubt(a, b):
    assert same_model(a, b)


@pytest.mark.parametrize("a,b", [("foo:7b", "foo:14b"), ("foo", "bar"), ("qwen3:8b", "qwen3-coder:8b")])
def test_distinct_models_are_distinct(a, b):
    assert not same_model(a, b)


def test_shared_digest_is_the_same_model():
    assert not same_model("alias-a", "alias-b")
    assert same_model("alias-a", "alias-b", {"alias-a": "d1", "alias-b": "d1"})
    assert not same_model("alias-a", "alias-b", {"alias-a": "d1", "alias-b": "d2"})


def test_parse_param_size_unknown_is_none():
    assert judge_select.parse_param_size("7.6B") == pytest.approx(7.6e9)
    assert judge_select.parse_param_size("494.03M") == pytest.approx(494.03e6)
    for bad in (None, "", "big", 0, "0"):
        assert judge_select.parse_param_size(bad) is None


# ── property: judge != answerer on 100% of generated rows ───────────────────

def _random_world(rng: random.Random):
    bases = ["m%d" % rng.randrange(6) for _ in range(rng.randrange(1, 7))]
    inv, seen = [], set()
    for b in bases:
        tag = rng.choice(["", ":latest", ":7b", ":14b", ":q4"])
        name = b + tag
        if name in seen:
            continue
        seen.add(name)
        inv.append(LocalModel(
            name=f"ollama/{name}",
            params=rng.choice([None, 1e9, 7e9, 14e9]),
            size=rng.choice([None, 10, 20, 30]),
            digest=rng.choice([None, "d0", "d1", "d2"]),
        ))
    answerer = rng.choice([
        "ollama/" + rng.choice(inv).name.split("/")[1],
        rng.choice(inv).name.split("/")[1], rng.choice(inv).name.split("/")[1] + ":latest",
        "ollama/m%d:7b" % rng.randrange(8), "openai/gpt-x", "claude-haiku-4-5-20251001",
    ])
    return answerer, inv


def test_property_judge_never_equals_answerer():
    rng = random.Random(20261018)
    n = judged = 0
    for _ in range(3000):
        answerer, inv = _random_world(rng)
        sel = select_judge(answerer, inv, setting=rng.choice(["", "m1:7b"]),
                           haiku_model="haiku-x", haiku_enabled=rng.random() < 0.5)
        n += 1
        if sel.judge_model is None:
            assert sel.status == "ungraded_no_judge"
            continue
        judged += 1
        digests = {canon(m.name): m.digest for m in inv if m.digest}
        assert not same_model(answerer, sel.judge_model, digests), (answerer, sel, inv)
        assert canon(answerer) != canon(sel.judge_model)
        assert judge_select.independent  # guard exists
    assert n == 3000 and judged > 1000, "property set must exercise real selections"


# ── strongest-other ─────────────────────────────────────────────────────────

def test_strongest_other_by_parameter_size_then_size_then_setting():
    inv = [
        LocalModel("ollama/small", 3 * G, 9), LocalModel("ollama/big", 30 * G, 1),
        LocalModel("ollama/mid", 8 * G, 99), LocalModel("ollama/ans", 70 * G, 1),
    ]
    assert select_judge("ollama/ans", inv).judge_model == "ollama/big"
    # equal parameters -> bigger on-disk size
    inv2 = [LocalModel("ollama/a", 8 * G, 5), LocalModel("ollama/b", 8 * G, 7), LocalModel("ollama/ans", None)]
    assert select_judge("ollama/ans", inv2).judge_model == "ollama/b"
    # all equal -> user's setting, else discovery order
    inv3 = [LocalModel("ollama/x"), LocalModel("ollama/y"), LocalModel("ollama/ans")]
    assert select_judge("ollama/ans", inv3, setting="y").judge_model == "ollama/y"
    assert select_judge("ollama/ans", inv3).judge_model == "ollama/x"


def test_unknown_parameters_rank_below_known():
    inv = [LocalModel("ollama/unk"), LocalModel("ollama/known", 1e6), LocalModel("ollama/ans")]
    assert select_judge("ollama/ans", inv).judge_model == "ollama/known"


def test_cloud_answer_gets_strongest_local_never_a_cloud_judge():
    inv = [LocalModel("ollama/a", 1 * G), LocalModel("ollama/b", 9 * G)]
    sel = select_judge("openai/gpt-x", inv, haiku_model="haiku-x", haiku_enabled=True)
    assert (sel.judge_model, sel.judge_kind) == ("ollama/b", "local")
    none = select_judge("openai/gpt-x", [], haiku_model="haiku-x", haiku_enabled=True)
    assert none.judge_model is None and none.status == "ungraded_no_judge"


# ── Haiku fallback: configured AND flag ─────────────────────────────────────

def test_local_answer_without_second_local_uses_haiku_only_when_configured_and_enabled():
    inv = [LocalModel("ollama/only")]
    kw = dict(haiku_model="cfg-haiku")
    on = select_judge("ollama/only", inv, haiku_enabled=True, **kw)
    assert (on.judge_model, on.judge_kind, on.status) == ("cfg-haiku", "subscription", "selected")
    assert select_judge("ollama/only", inv, haiku_enabled=False, **kw).status == "ungraded_no_judge"
    assert select_judge("ollama/only", inv, haiku_enabled=True, haiku_model=None).status == "ungraded_no_judge"


def test_haiku_that_is_the_answerer_family_is_refused():
    sel = select_judge("ollama/only", [LocalModel("ollama/only")], haiku_model="only:latest", haiku_enabled=True)
    assert sel.judge_model is None


def test_nothing_configured_is_ungraded_no_judge(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for var in ("LLM_ROUTER_JUDGE_MODEL", "LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "LLM_ROUTER_OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)
    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch("llm_router.discover.get_cached_ollama_models", return_value=["ollama/only"]), \
         patch("llm_router.proxy.tiers.tier_model", return_value=None):
        sel = judge_select.select_for("ollama/only")
    assert sel == judge_select.Selection(None, None, "ungraded_no_judge")


def test_flag_off_by_default_even_when_haiku_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_MODEL", "cfg-haiku")
    monkeypatch.delenv("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", raising=False)
    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch("llm_router.discover.get_cached_ollama_models", return_value=["ollama/only"]):
        assert judge_select.select_for("ollama/only").judge_model is None
        monkeypatch.setenv("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "1")
        sel = judge_select.select_for("ollama/only")
    assert (sel.judge_model, sel.judge_kind) == ("cfg-haiku", "subscription")


def test_haiku_id_sources_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_JUDGE_MODEL", raising=False)
    with patch("llm_router.proxy.tiers.tier_model", return_value="tier-haiku"):
        assert judge_select.configured_haiku_model() == "tier-haiku"
        (tmp_path / "routing.yaml").write_text("judge:\n  haiku_model: yaml-haiku\n")
        assert judge_select.configured_haiku_model() == "yaml-haiku"
        monkeypatch.setenv("LLM_ROUTER_JUDGE_MODEL", "env-haiku")
        assert judge_select.configured_haiku_model() == "env-haiku"


# ── drain-time guard and per-row record ─────────────────────────────────────

@pytest.mark.asyncio
async def test_grade_one_refuses_alias_and_records_row(tmp_path, monkeypatch):
    import json
    from llm_router import judge

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    failopen.reset_cache()
    monkeypatch.setattr(judge, "_select_judge_model", lambda m: "ollama/foo")
    with patch("llm_router.discover.is_ollama_available", return_value=False), \
         patch("llm_router.judge.call_llm") as call:
        out = await judge._grade_one({"answering_model": "ollama/foo:latest", "door": "mcp"})
    call.assert_not_called()
    assert out == "ungraded"
    row = json.loads((tmp_path / "judge_results.jsonl").read_text())
    assert row["status"] == "ungraded_no_judge" and "judge_kind" in row


# ── D-205 lint and unknown-as-number ────────────────────────────────────────

_MODEL_LITERAL = ("claude-", "gpt-", "gemini-", "haiku", "sonnet", "opus", "qwen", "llama", "mistral", "gemma", "deepseek")


@pytest.mark.parametrize("fname", ["judge_select.py"])
def test_no_model_literals_in_judge_selection_code(fname):
    tree = ast.parse((SRC / fname).read_text())
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}
    assert any(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in ast.walk(tree))
    bad = [n.value for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
           and not n.value.startswith("LLM_ROUTER_") and n.value != "haiku_model"  # env name, config key
           and any(m in n.value.lower() for m in _MODEL_LITERAL)]
    assert not bad, bad


def test_judge_py_has_no_default_model_literal():
    tree = ast.parse((SRC / "judge.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in fn.args.defaults + [x for x in fn.args.kw_defaults if x is not None]:
                assert not (isinstance(d, ast.Constant) and isinstance(d.value, str)
                            and "claude" in d.value.lower()), fn.name


@pytest.mark.asyncio
async def test_judge_scores_none_when_no_data(temp_db):
    from llm_router import judge

    scores = await judge.get_judge_scores_for_model("nobody/none", days=3)
    assert scores["avg_score"] is None and scores["sample_count"] == 0
    assert scores["min_score"] is None and scores["max_score"] is None
