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


def test_fewer_than_60_paired_items_is_never_a_verdict(g1):
    """PLAN P1.1-b: 'Until n >= 60 paired the result is not informative' (review 509, finding 1)."""
    six_errors = _rows([("recent", True, True, None)] * 54 + [("recent", False, False, "cli_error")] * 6)
    assert g1.verdict(six_errors, total=60)["verdict"] == "NOT INFORMATIVE"   # 10% errors, n=54
    assert g1.verdict(_rows([("recent", True, True, None)] * 5), total=5)["verdict"] == "NOT INFORMATIVE"
    assert g1.verdict(_rows([("recent", True, True, None)] * 60), total=60)["verdict"] == "PASS"


def test_run_fidelity_with_fake_responders(g1):
    """End to end on 8 items: a responder that answers from whatever context it gets."""
    def echo(context: str):
        return g1.Answer(text=context[-4000:], model="fake")  # the tail holds what the arm kept

    res = g1.run_fidelity("sonnet", echo, provider="anthropic", limit=8)
    assert res["label"] == g1.LABEL and res["n"] == 8 and res["errors"] == 0
    assert res["verdict"] == "SMOKE (not scored)"                 # a --limit run is never a result
    assert res["prereg_sha256"] == g1.prereg_sha256()
    assert set(res["pack_modes"]) == {"pack"}


def test_errors_are_retried_once_then_counted(g1):
    calls = []

    def flaky(context: str):
        calls.append(1)
        return g1.Answer(error="cli_error")

    res = g1.run_fidelity("sonnet", flaky, provider="anthropic", limit=2)
    assert res["errors"] == 2 and res["n"] == 0 and res["verdict"] == "SMOKE (not scored)"
    assert len(calls) == 2 * 2 * 2  # 2 items x 2 arms x (call + one retry)


def test_summary_coverage_comes_from_the_real_detached_workers(g1, monkeypatch):
    res = g1.run_summary(n_sessions=20, wait_s=120)
    assert res["label"] == g1.LABEL and res["workers_started"] == 20
    assert res["coverage"]["n_sessions"] == 20 and res["coverage"]["covered"] == 20
    assert res["verdict"] == "NOT INFORMATIVE"                    # 20 spawns < the 100 the bar needs
    from llm_router import context_summary as cs

    real = cs.spawn
    monkeypatch.setattr(cs, "spawn", lambda sid, p, argv=None: real(sid, p, argv=[sys.executable, "-c", "pass"]))
    res = g1.run_summary(n_sessions=20, wait_s=2)
    assert res["coverage"]["covered"] == 0                        # no worker, no summary


def test_summary_verdict_bars(g1):
    cov = {"n_sessions": 20, "covered": 20, "pct": 100.0, "informative": True}
    assert g1.summary_verdict(cov, [10.0] * 100)["verdict"] == "PASS"
    assert g1.summary_verdict(cov, [10.0] * 94 + [60.0] * 6)["verdict"] == "FAIL"   # p95 > 50 ms
    assert g1.summary_verdict(dict(cov, covered=18, pct=90.0), [10.0] * 100)["verdict"] == "FAIL"
    assert g1.summary_verdict(dict(cov, informative=False), [10.0] * 100)["verdict"] == "NOT INFORMATIVE"
    assert g1.summary_verdict(cov, [10.0] * 99)["verdict"] == "NOT INFORMATIVE"


def test_a_loopback_base_url_is_refused(g1):
    assert g1.loopback_base_url({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})
    assert g1.loopback_base_url({"ANTHROPIC_BASE_URL": "http://localhost:8787/v1"})
    assert g1.loopback_base_url({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None
    assert g1.loopback_base_url({}) is None


# ── P1.9-a: O3 integrity on synthetic transcripts ─────────────────────────────


def test_o3_counts_one_turn_per_typed_prompt_and_applies_the_d58_rules(g1):
    res = g1.run_o3()
    s = res["sessions"]
    assert res["label"] == g1.LABEL and res["verdict"] == "PASS" and res["measurable"] == 3
    for sid in ("synth-o3-a", "synth-o3-b", "synth-o3-c"):
        assert s[sid]["state"] == "PASS" and s[sid]["ratio"] == 1.0, sid
    assert s["synth-o3-b"]["coverage"] < 1.0                     # lost rows, still >= 95%
    assert s["synth-o3-d"]["state"] == "UNMEASURABLE"            # proxy down: coverage ~50%
    assert s["synth-o3-e"]["state"] == "NOT A GATE UNIT"         # < 50 typed prompts
    ex = res["excluded"]                                          # every M0.3 cause is exercised
    assert ex["side_call_excluded"] and ex["unjoined"] and ex["meta_first"] and ex["subagent_first"]


def test_o3_check_fails_without_the_transcript_join(g1, tmp_path, monkeypatch):
    """The measurement can fail: with no transcript the proxy-only rule keeps the orphan calls."""
    main, subs, rows = g1.build_o3_session("synth-o3-x", 60)
    g1.write_o3_session(tmp_path / "with", "synth-o3-x", main, subs)
    until = max(r["ts"] for r in rows) + 60
    kinds = frozenset({"synthetic"})
    ok = g1.o3_integrity(tmp_path / "with", rows, since=g1._EPOCH - 60, until=until, kinds=kinds)
    assert ok["sessions"]["synth-o3-x"]["ratio"] == 1.0

    import llm_router.o3_transcripts as o3t

    monkeypatch.setattr(o3t, "thread_lookup", lambda **k: (lambda sid, mid: None))
    bad = g1.o3_integrity(tmp_path / "with", rows, since=g1._EPOCH - 60, until=until, kinds=kinds)
    assert bad["sessions"]["synth-o3-x"]["ratio"] > 1.10 and bad["sessions"]["synth-o3-x"]["state"] == "FAIL"


# ── P1.10-a / P0.9: replay corpus and readout ─────────────────────────────────


def test_replay_corpus_is_valid_for_the_replay_harness(g1, tmp_path):
    spec = importlib.util.spec_from_file_location("synthetic_replay_under_test", ROOT / "scripts" / "synthetic_replay.py")
    sr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sr
    try:
        spec.loader.exec_module(sr)
        recs = g1.replay_corpus(n_sessions=3, turns=5)
        p = tmp_path / "c.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in recs))
        loaded = sr.load_corpus(p)
    finally:
        sys.modules.pop(spec.name, None)
    steps = [r["step"] for r in loaded]
    assert len(loaded) == 3 * (5 * 2 + 4 * 3)
    assert steps.count("turn_first") == 15 and steps.count("subagent") == 12
    assert g1.replay_corpus(3) == g1.replay_corpus(3)


def test_identity_readout_uses_the_kpi_rule(g1, tmp_path):
    import sqlite3

    state = tmp_path / "state"
    state.mkdir()
    proxy = [{"session_id": "synth-replay-1", "task_id": "0123456789abcdef"}] * 198 + \
            [{"session_id": "synth-replay-1", "task_id": "gen-0123456789ab"},       # generated: missing
             {"session_id": "unknown:hook", "task_id": "0123456789abcdef"}]          # unknown: missing
    (state / "proxy_calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in proxy))
    con = sqlite3.connect(state / "usage.db")
    con.execute("CREATE TABLE usage (session_id TEXT, task_id TEXT)")
    con.executemany("INSERT INTO usage VALUES (?, ?)", [("synth-replay-1", "0123456789abcdef")] * 50)
    con.commit()
    con.close()
    summary = {"timing": {"lifecycle": {"session-start:SessionStart": {"n": 60, "p50_ms": 300.0, "p95_ms": 900.0}},
                          "wall_s": 1.0},
               "deterministic": {"errors": [], "network": {"violations": 0}, "corpus": {}},
               "run": {"git_sha": "x"}}
    res = g1.identity_from_replay(state, summary)
    assert res["paths"]["proxy"]["n"] == 200 and res["paths"]["proxy"]["share"] == 0.99
    assert res["paths"]["proxy"]["verdict"] == "PASS" and res["paths"]["usage"]["verdict"] == "n<200"
    assert res["verdict_p110a"] == "NOT INFORMATIVE"
    assert res["latency"]["P0.9-a session-start"]["verdict"] == "PASS"


def test_identity_readout_reports_replay_probe_envelopes_apart(g1, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    host = [{"session_id": "synth-replay-1", "task_id": "0123456789abcdef"}] * 200
    probes = [{"session_id": f"synth-replay-1-{v}", "task_id": "gen-0123456789ab"}
              for v in ("nosys", "bare", "reminder")] * 50
    (state / "proxy_calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in host + probes))
    summary = {"timing": {"lifecycle": {}, "wall_s": 1.0},
               "deterministic": {"errors": [], "network": {"violations": 0}, "corpus": {}},
               "run": {"git_sha": "x"}}
    res = g1.identity_from_replay(state, summary)
    assert res["paths"]["proxy"]["n"] == 200 and res["paths"]["proxy"]["verdict"] == "PASS"
    assert res["paths"]["proxy_probe_envelopes"]["n"] == 150
    assert res["paths"]["proxy_probe_envelopes"]["share"] == 0.0
    assert res["verdict_p110a"] == "PASS"


# ── P1.1-b Sonnet stratum after repair 1 (PREREG-P11B-synthetic-R2) ───────────


def test_equivalence_holds_at_the_d101_window_and_fails_at_the_pre_d101_one(g1):
    it = g1.items()[40]                                   # an early_tool item: the category PACK lost
    with g1.scratch_env() as root:
        path = g1.write_transcript(it, root)
        new = g1.equivalence_item(it, path, 65_536)
        old = g1.equivalence_item(it, path, 8_192)       # the MCP door's pre-D-101 cloud window
    assert new["equivalent"] and all(new[k] for k in ("full_mode", "no_summary", "same_messages",
                                                       "token_present"))
    assert not old["equivalent"] and not old["full_mode"] and not old["same_messages"]
    assert not old["token_present"]                      # the token the original stratum lost


def test_equivalence_is_repair_attributable_and_never_fail(g1, monkeypatch):
    res = g1.run_equivalence()
    assert res["model"] == "anthropic/claude-sonnet-4-6" and res["door"] == "mcp"   # the routed Sonnet
    assert res["windows"] == {"new": 65_536, "old": 8_192}
    assert res["equivalent"] == {"new": 60, "old": 0}
    assert res["verdict"] == "PASS by construction (repair-attributable)"
    from llm_router import router

    monkeypatch.setattr(router, "_pack_window", lambda model, door: router._MCP_NON_LOCAL_WINDOW)
    assert g1.run_equivalence()["verdict"] == "NOT INFORMATIVE"   # no repair: nothing to attribute


def test_equivalence_is_not_attributable_when_the_old_window_already_sent_items_whole(g1, monkeypatch):
    """Review 514 (D3): if the pre-D-101 window already fit the items (as R1's doors did),
    60/60 at the new window proves nothing about the repair."""
    from llm_router import router

    monkeypatch.setattr(router, "_MCP_NON_LOCAL_WINDOW", 32_768)
    res = g1.run_equivalence()
    assert res["equivalent"] == {"new": 60, "old": 60} and res["verdict"] == "NOT INFORMATIVE"
