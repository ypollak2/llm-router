"""scripts/g1_synthetic_evidence.py: synthetic G1 evidence (owner decision D-99).

Offline only: no model is called. The responders are fakes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "g1_synthetic_evidence.py"


@pytest.fixture(scope="module")
def g1():
    spec = importlib.util.spec_from_file_location("g1_synthetic_evidence_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_items_follow_the_preregistered_mix_and_are_deterministic(g1):
    first, second = g1.items(), g1.items()
    assert len(first) == g1.N_ITEMS == 60
    assert {c: sum(1 for i in first if i.category == c) for c, _ in g1.MIX} == dict(g1.MIX)
    assert [(i.item_id, i.token, json.dumps(i.entries)) for i in first] == \
        [(i.item_id, i.token, json.dumps(i.entries)) for i in second]


def test_every_item_plants_its_token_exactly_once(g1):
    for it in g1.items():
        assert g1.full_context(it).count(it.token) == 1, it.item_id


def test_early_tokens_sit_outside_the_last_seven_messages(g1):
    from llm_router.context_pack import extract_turn_text

    for it in g1.items():
        tail = " ".join(extract_turn_text(e["message"]["content"]) for e in it.entries[-7:])
        assert (it.token in tail) == (it.category == "recent"), it.item_id


def test_sessions_are_in_pack_mode_and_under_the_full_cap(g1, monkeypatch):
    from llm_router import context_summary
    from llm_router.token_budget import estimate_tokens

    with g1.scratch_env() as root:
        tdir = root / "t"
        tdir.mkdir()
        for it in g1.items()[::15]:
            path = g1.write_transcript(it, tdir)
            context_summary.maybe_update(it.item_id, path, model=None, free_pct=100.0)
            pack, mode = g1.pack_context(it, path, "anthropic")
            assert mode == "pack"
            assert 0.8 * g1.WINDOW < estimate_tokens(g1.full_context(it)) < 40_000
            assert estimate_tokens(pack) < g1.WINDOW


def test_pack_carries_the_rolling_summary(g1):
    from llm_router import context_summary

    it = next(i for i in g1.items() if i.category == "early_user")
    with g1.scratch_env() as root:
        path = g1.write_transcript(it, root)
        context_summary.maybe_update(it.item_id, path, model=None, free_pct=100.0)
        pack, _ = g1.pack_context(it, path, "anthropic")
    assert "<conversation-summary>" in pack and it.token in pack


def test_hook6_is_the_last_six_text_turns_without_tool_blocks(g1):
    it = g1.items()[0]
    ctx = g1.hook6_context(it)
    assert ctx.count("[assistant]") + ctx.count("[user]") == 7  # 6 turns + request
    assert "tool_result" not in ctx and "tool_use" not in ctx


def test_scratch_env_never_leaks_the_operators_home(g1, monkeypatch, tmp_path):
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "real"))
    monkeypatch.setenv("LLM_ROUTER_FOO", "keep")
    with g1.scratch_env() as root:
        assert os.environ["HOME"].startswith(str(root))
        assert "LLM_ROUTER_FOO" not in os.environ
    assert os.environ["HOME"] == str(tmp_path / "real") and os.environ["LLM_ROUTER_FOO"] == "keep"


@pytest.mark.parametrize("answer,token,ok", [
    ("The port is quokka-falcon-417.", "quokka-falcon-417", True),
    ("`QUOKKA-FALCON-417`", "quokka-falcon-417", True),
    ("quokka falcon 417", "quokka-falcon-417", False),
    ("", "quokka-falcon-417", False),
])
def test_grading_is_normalized_containment(g1, answer, token, ok):
    assert g1.passes(answer, token) is ok


def test_mcnemar_exact(g1):
    assert g1.mcnemar_exact(0, 0) == 1.0
    assert g1.mcnemar_exact(5, 5) == 1.0
    assert g1.mcnemar_exact(10, 0) == pytest.approx(2 / 1024)
    assert g1.mcnemar_exact(0, 10) == pytest.approx(2 / 1024)


def _rows(spec):
    return [{"category": c, "pack": p, "ref": r, "error": e} for c, p, r, e in spec]


def test_verdict_bar_and_error_rule(g1):
    ok = _rows([("recent", True, True, None)] * 59 + [("early_tool", False, True, None)])
    v = g1.verdict(ok, total=60)
    assert (v["worse"], v["better"], v["verdict"]) == (1, 0, "PASS")  # 1/60 <= 0.02
    bad = _rows([("recent", True, True, None)] * 57 + [("early_tool", False, True, None)] * 3)
    assert g1.verdict(bad, total=60)["verdict"] == "FAIL"             # 3/60 > 0.02
    errs = _rows([("recent", True, True, None)] * 53 + [("recent", False, False, "cli_error")] * 7)
    v = g1.verdict(errs, total=60)
    assert (v["n"], v["errors"], v["verdict"]) == (53, 7, "NOT INFORMATIVE")


def test_run_fidelity_with_fake_responders(g1):
    """End to end on 8 items: a responder that answers from whatever context it gets."""
    def echo(context: str):
        return g1.Answer(text=context[-4000:], model="fake")  # the tail holds what the arm kept

    res = g1.run_fidelity("sonnet", echo, provider="anthropic", limit=8)
    assert res["label"] == g1.LABEL and res["n"] == 8 and res["errors"] == 0
    assert res["prereg_sha256"] == g1.prereg_sha256()
    assert set(res["pack_modes"]) == {"pack"}


def test_errors_are_retried_once_then_counted(g1):
    calls = []

    def flaky(context: str):
        calls.append(1)
        return g1.Answer(error="cli_error")

    res = g1.run_fidelity("sonnet", flaky, provider="anthropic", limit=2)
    assert res["errors"] == 2 and res["n"] == 0 and res["verdict"] == "NOT INFORMATIVE"
    assert len(calls) == 2 * 2 * 2  # 2 items x 2 arms x (call + one retry)


def test_summary_coverage_on_synthetic_sessions(g1):
    res = g1.run_summary(n_sessions=20, spawns=20)
    assert res["label"] == g1.LABEL
    assert res["coverage"]["n_sessions"] == 20 and res["coverage"]["covered"] == 20
