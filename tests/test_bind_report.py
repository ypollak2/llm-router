"""BIND W3: ``scripts/release/bind_report.py`` prints the D-358 success bar.

Each criterion gets a status: PASS, FAIL, INSUFFICIENT (the window has not produced the
n the bar needs) or PENDING (the hand check has not been entered). The corpora below are
synthetic; the arithmetic of the bar is what is pinned.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

from tests.bind_corpus import (
    ANSWER, NOW, OLD_REASON, SID, Transcript, agent_prompt, codex_row, write_state,
)

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


BR = _load("_bind_report_under_test", "scripts/release/bind_report.py")

UNTIL = NOW
DAY = 86400.0
SINCE = UNTIL - 4 * DAY                 # a 4-day success window: past the 3-day floor
BLOCKED_REASON = OLD_REASON.replace(ANSWER, "The sandbox is read-only so I could not run the tests or open the pull request.")
HISTORY = {"releases": [{"release": "15.4.0", "used_rate": 2 / 873, "used": 2, "attempted": 873,
                         "window_days": 30}]}                      # no audit_version: scored by audit v1


def build(tmp_path, bound=0, void=0, blocked=0, prompts=0, holds=0, probes=0, used_mcp=0, extra_unused=0,
          before_since_void=0, start=SINCE, pre_used_mcp=0):
    """Delegations spread from `start` to the end of the window, plus prompts, holds, probes and MCP units."""
    t = Transcript(tmp_path, SID)
    rows = []
    step = (UNTIL - start - 3600) / max(bound + void + blocked, 1)
    clock = start + 600
    for i in range(prompts):
        t.human(SINCE + 60 + i * 30, f"please carry on with step number {i} of the plan now")
    n = 0
    for kind, count in (("bound", bound), ("void", void), ("blocked", blocked)):
        for _ in range(count):
            tid = f"t{n}"
            n += 1
            at = clock + n * step
            t.call(at, tid, "Agent", {"prompt": agent_prompt(str(n)), "subagent_type": "task-reviewer"})
            if kind == "void":
                t.result(at + 10, tid, "Async agent launched successfully.\nagentId: a" + str(n))
            else:
                reason = BLOCKED_REASON if kind == "blocked" else OLD_REASON
                t.result(at + 10, tid, "PreToolUse:Agent hook error: " + reason, is_error=True)
            t.say(at + 20, "ok")
            rows.append(codex_row(at + 9.5, tool_use_id=tid))
    for i in range(before_since_void):                       # the pre-fix reference population
        tid = f"old{i}"
        at = SINCE - (i + 1) * 3600
        t.call(at, tid, "Agent", {"prompt": agent_prompt(f"old{i}")})
        t.result(at + 10, tid, "Async agent launched successfully.\nagentId: o" + str(i))
        rows.append(codex_row(at + 9.5, tool_use_id=tid))
    for i in range(pre_used_mcp):                               # used units before the fix went live
        at = SINCE - (i + 1) * 600
        t.call(at, f"pre{i}", "mcp__llm_router__llm", {"prompt": f"what is the old policy number {i}", "task": "query"})
        t.result(at + 1, f"pre{i}", json.dumps({"result": f"> 🤖 **x** · 9 tokens · $0 · 1ms\n\n{ANSWER}\n\n─────\n→ x"}))
        t.say(at + 2, "The router says " + ANSWER)
    for i in range(holds):
        t.call(SINCE + 100 + i, f"h{i}", "Bash", {"command": "x"})
        t.result(SINCE + 101 + i, f"h{i}", "PreToolUse:Bash hook error: [llm_router] Routing directive BLOCKED.\nViolation 1/4", is_error=True)
    for i in range(probes):
        t.call(SINCE + 200 + i, f"p{i}", "mcp__llm_router__llm", {"prompt": "Reply OK", "task": "query"})
        t.result(SINCE + 201 + i, f"p{i}", json.dumps({"result": "> 🤖 **x** · 1 tokens · $0 · 1ms\n\nOK\n\n─────\n→ x"}))
    for i in range(used_mcp):
        t.call(SINCE + 300 + i * 10, f"m{i}", "mcp__llm_router__llm", {"prompt": f"what is the policy number {i}", "task": "query"})
        t.result(SINCE + 301 + i * 10, f"m{i}", json.dumps({"result": f"> 🤖 **x** · 9 tokens · $0 · 1ms\n\n{ANSWER}\n\n─────\n→ x"}))
        t.say(SINCE + 302 + i * 10, "The router says " + ANSWER)
    for i in range(extra_unused):
        t.call(SINCE + 5000 + i * 10, f"x{i}", "mcp__llm_router__llm", {"prompt": f"what is the other number {i}", "task": "query"})
        t.result(SINCE + 5001 + i * 10, f"x{i}", json.dumps({"result": "> 🤖 **x** · 9 tokens · $0 · 1ms\n\n"
                                                              "an answer nobody used about something else entirely here today\n\n─────\n→ x"}))
        t.say(SINCE + 5002 + i * 10, "Moving on to the next thing in the plan without it.")
    t.write()
    state = write_state(tmp_path, rows)
    return tmp_path / "projects", state


def run(tmp_path, capsys, *extra, **corpus):
    projects, state = build(tmp_path, **corpus)
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps(HISTORY))
    rc = BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects),
                  "--state-dir", str(state), "--history", str(hist), "--reference", "measured", "--json", *extra])
    rep = json.loads(capsys.readouterr().out)
    return rc, rep, {x["criterion"]: x for x in rep["lines"]}


BIND, BLOCKED, TOLL, GATE = ("Codex binding rate", "Blocked-answer share among bound units", "Toll probes",
                             "Outcome gate under the fixed audit")


def test_binding_passes_at_ninety_percent_of_thirty_units(tmp_path, capsys):
    rc, rep, L = run(tmp_path, capsys, bound=27, void=3)
    assert L[BIND]["status"] == "PASS"
    assert L[BIND]["result"].startswith("27/30 = 90.0%")
    assert "30 delegated units (3 void" in L[BIND]["n"]
    assert BR.iso(SINCE) in L[BIND]["window"] and "north_star_units.jsonl" in L[BIND]["source"]
    assert rc == 1 and rep["verdict"] == "NOT MET"                  # the other lines are not yet satisfied


def test_binding_fails_below_ninety_percent(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, bound=26, void=4)
    assert L[BIND]["status"] == "FAIL" and L[BIND]["result"].startswith("26/30 = 86.7%")


def test_binding_is_insufficient_under_thirty_units_or_three_days(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, bound=29)
    assert L[BIND]["status"] == "INSUFFICIENT" and "has 29" in L[BIND]["note"]
    projects, state = build(tmp_path / "short", bound=30, start=UNTIL - 2 * DAY)       # all 30 units inside a 2-day window
    out = BR.main(["--since", BR.iso(UNTIL - 2 * DAY), "--until", BR.iso(UNTIL), "--projects-dir", str(projects),
                   "--state-dir", str(state), "--history", str(tmp_path / "none.json"), "--reference", "measured", "--json"])
    rep = json.loads(capsys.readouterr().out)
    line_ = {x["criterion"]: x for x in rep["lines"]}[BIND]
    assert line_["result"].startswith("30/30 = 100.0%")                # the rate is fine; the days are not
    assert line_["status"] == "INSUFFICIENT" and "over 2.0" in line_["note"]      # 2 days < 3
    assert out == 1


def test_blocked_share_needs_the_hand_check(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, bound=28, blocked=2)
    assert L[BLOCKED]["status"] == "PENDING" and "hand check of 20 not entered" in L[BLOCKED]["result"]
    assert "automated screen" in L[BLOCKED]["note"] and "2/30" in L[BLOCKED]["note"]
    sample = L[BLOCKED]["note"].split("stable order): ")[1].split(", ")
    assert len(sample) == 20 and len(set(sample)) == 20


def test_blocked_share_passes_or_fails_on_the_hand_check(tmp_path, capsys):
    projects, state = build(tmp_path, bound=30)
    ids = [u for u in _bound_ids(projects, state)]
    sample = BR.hand_check_sample(ids)
    ok = tmp_path / "hand_ok.jsonl"
    ok.write_text("".join(json.dumps({"unit_id": i, "blocked": n < 1}) + "\n" for n, i in enumerate(sample)))   # 1 of 20 = 5%
    bad = tmp_path / "hand_bad.jsonl"
    bad.write_text("".join(json.dumps({"unit_id": i, "blocked": n < 3}) + "\n" for n, i in enumerate(sample)))  # 3 of 20 = 15%
    for path, status in ((ok, "PASS"), (bad, "FAIL")):
        BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
                 "--history", str(tmp_path / "h.json"), "--reference", "measured", "--hand-checked", str(path), "--json"])
        L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
        assert L[BLOCKED]["status"] == status, L[BLOCKED]


def test_blocked_share_of_exactly_ten_percent_fails(tmp_path, capsys):
    projects, state = build(tmp_path, bound=30)
    sample = BR.hand_check_sample(_bound_ids(projects, state))
    ten = tmp_path / "hand_ten.jsonl"
    ten.write_text("".join(json.dumps({"unit_id": i, "blocked": n < 2}) + "\n" for n, i in enumerate(sample)))   # 2 of 20 = 10%
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured", "--hand-checked", str(ten), "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert L[BLOCKED]["status"] == "FAIL" and "2/20 = 10.0%" in L[BLOCKED]["result"]      # the bar is "under 10%"


def _bound_ids(projects, state):
    audit = BR._audit_modules()[1]
    units, _ = audit._collect(30, projects, state, UNTIL)
    return [u.unit_id for u in units if u.extra.get("bound")]


def test_hand_check_sample_is_stable_and_capped():
    ids = [f"agent_route_codex:s:{i}" for i in range(50)]
    assert BR.hand_check_sample(ids) == BR.hand_check_sample(list(reversed(ids)))
    assert len(BR.hand_check_sample(ids)) == 20 and len(BR.hand_check_sample(ids[:5])) == 5


def test_toll_probes_pass_with_a_hundred_prompts_and_no_holds_or_probes(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, prompts=120)
    assert L[TOLL]["status"] == "PASS" and "0 holds in 120 prompts" in L[TOLL]["result"]


def test_a_hold_rate_of_one_per_hundred_fails(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, prompts=100, holds=1)
    assert L[TOLL]["status"] == "FAIL" and "1.00 per 100" in L[TOLL]["result"]


def test_a_single_reply_ok_probe_fails_the_toll_line(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, prompts=150, probes=1)
    assert L[TOLL]["status"] == "FAIL" and "1 fixed-reply probes" in L[TOLL]["result"]


def test_toll_line_is_insufficient_under_the_prompt_floor(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, prompts=40)
    assert L[TOLL]["status"] == "INSUFFICIENT" and "needs >= 50" in L[TOLL]["note"]
    _, _, L = run(tmp_path / "ok", capsys, "--min-prompts", "30", prompts=40)         # the floor is this script's, so it moves
    assert L[TOLL]["status"] == "PASS" and "0 holds in 40 prompts = 0.00 per 100 [Wilson 95% 0.00..8.76]" in L[TOLL]["result"]


def _ledger(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_routers_hold_ledger_wins_over_the_derived_count(tmp_path, capsys):
    ledger = tmp_path / "hold_events.jsonl"
    rows = [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(200)]
    rows += [{"ts": SINCE + 5000 + i, "event": "blocked", "session_id": SID, "tool": "Bash"} for i in range(3)]
    rows += [{"ts": SINCE - 5, "event": "blocked", "session_id": SID},                    # before the window
             {"ts": SINCE + 99, "event": "blocked", "session_id": "ffffffff-0000-4000-8000-000000000000"}]   # not an organic session here
    _ledger(ledger, rows)
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120)
    assert "3 holds in 200 prompts = 1.50 per 100" in L[TOLL]["result"] and L[TOLL]["status"] == "FAIL"
    assert "hold ledger" in L[TOLL]["source"] and "cross-check from the transcripts: 0 holds in 120 prompts" in L[TOLL]["source"]


def test_an_empty_hold_ledger_is_unknown_and_the_transcripts_are_used(tmp_path, capsys):
    ledger = tmp_path / "hold_events.jsonl"
    _ledger(ledger, [{"ts": SINCE + 1, "event": "blocked", "session_id": SID}])           # a block but no prompt row
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120, holds=2)
    assert "2 holds in 120 prompts" in L[TOLL]["result"] and "derived from the transcripts" in L[TOLL]["source"]
    assert BR.read_hold_events(tmp_path / "missing.jsonl", SINCE, UNTIL, {SID}) is None
    assert BR.read_hold_events(None, SINCE, UNTIL, {SID}) is None


def test_the_hold_ledger_is_found_in_the_state_dir_by_default(tmp_path, capsys):
    projects, state = build(tmp_path, prompts=120)
    _ledger(state / "hold_events.jsonl", [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(150)])
    hist = tmp_path / "h.json"
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(hist), "--reference", "measured", "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert "0 holds in 150 prompts" in L[TOLL]["result"] and L[TOLL]["status"] == "PASS"


def test_outcome_line_is_insufficient_without_fifty_attempted_units(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, bound=10)
    assert L[GATE]["status"] == "INSUFFICIENT" and "too few to tell" in L[GATE]["result"]


def test_outcome_line_passes_above_the_recorded_rate_and_says_it_is_not_like_for_like(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, void=40, used_mcp=5, extra_unused=20)
    g = L[GATE]
    assert g["status"] == "PASS", g
    assert g["result"].startswith("5/65 = 7.7% vs 15.4.0's 0.229% (2/873)")
    assert "NOT LIKE FOR LIKE" in g["note"] and "audit v1" in g["note"] and "v2" in g["note"]
    assert "--cross-version" in g["note"]


def test_outcome_line_says_when_it_already_passes_before_the_fix(tmp_path, capsys):
    """The pre-registered line (used rate above 0.229%) is met by the fixed audit on data in which no
    handoff was bound, so a PASS there proves nothing about binding. The report says so."""
    _, _, L = run(tmp_path, capsys, void=45, before_since_void=45, pre_used_mcp=10)
    g = L[GATE]
    assert g["status"] == "PASS" and L[BIND]["status"] == "FAIL"        # 0 of 45 bound, and the line still passes
    assert "Before --since the same audit already reads 10/55 = 18.2%" in g["note"]
    assert "passes without a single bound handoff" in g["note"]
    _, _, L = run(tmp_path / "few", capsys, void=45, before_since_void=5, pre_used_mcp=2)     # 7 units: too few to say
    assert "Before --since" not in L[GATE]["note"]


def test_outcome_line_fails_at_or_below_the_recorded_rate(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, void=60)           # 0 used of 60
    assert L[GATE]["status"] == "FAIL"


def test_the_bar_is_met_only_when_every_line_passes(tmp_path, capsys):
    rc, rep, L = run(tmp_path, capsys, bound=40, void=0, prompts=150, used_mcp=3, extra_unused=10)
    assert L[BIND]["status"] == "PASS" and L[TOLL]["status"] == "PASS" and L[GATE]["status"] == "PASS"
    assert L[BLOCKED]["status"] == "PENDING"            # no hand check entered: not yet met
    assert rep["verdict"] == "NOT MET" and rc == 1


def test_savings_estimate_arithmetic():
    ref = {"source": "unit test", "n": 4, "output_tokens_total": 4000, "output_tokens_mean": 1000,
           "output_tokens_median": 500,
           "by_model": {"claude-opus-5-5": {"n": 2, "output_tokens": 3000}, "claude-sonnet-5-5": {"n": 2, "output_tokens": 1000}}}
    s = BR.savings_block(10, 7.0, ref, "test")
    assert s["output_tokens_avoided"] == 10_000 and s["output_tokens_avoided_per_week"] == 10_000
    # (3000 * $20 + 1000 * $10) / 1e6 / 4 units = $0.0175 per unit
    assert s["usd_avoided"] == pytest.approx(0.175, abs=0.005)
    assert "ESTIMATE" in s["label"] and "lower bound" in s["label"]
    assert BR.savings_block(10, 3.5, ref, "test")["output_tokens_avoided_per_week"] == 20_000      # a half-week window


def test_live_reference_reads_the_void_sub_agents_before_the_window(tmp_path):
    projects, state = build(tmp_path, before_since_void=2)
    d = projects / "-Users-u-Projects-app" / SID / "subagents"
    d.mkdir(parents=True)
    for aid, out in (("o0", 100), ("o1", 300)):
        rec = {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": out}}}
        (d / f"agent-{aid}.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")   # repeated message id counts once
    audit = BR._audit_modules()[1]
    units, _ = audit._collect(30, projects, state, UNTIL)
    ref = BR.live_reference(units, SINCE, projects)
    assert ref["n"] == 2 and ref["output_tokens_total"] == 400 and ref["output_tokens_mean"] == 200
    assert ref["by_model"] == {"claude-opus-5-5": {"n": 2, "output_tokens": 400}}


def test_price_table_matches_the_products_pricing_module():
    from llm_router import pricing
    for model, usd in BR.OUTPUT_PRICE_PER_MTOK.items():
        assert pricing._ANTHROPIC[model].output == usd, model


def test_render_has_a_status_n_window_and_source_on_every_line(tmp_path, capsys):
    projects, state = build(tmp_path, bound=27, void=3)
    audit = BR._audit_modules()[1]
    rc = BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
                  "--history", str(tmp_path / "h.json"), "--reference", "measured"])
    out = capsys.readouterr().out
    assert rc == 1
    for status_line in [ln for ln in out.splitlines() if ln[:2] in ("1.", "2.", "3.", "4.")]:
        assert any(f"[{s}]" in status_line for s in ("PASS", "FAIL", "INSUFFICIENT", "PENDING"))
    assert out.count("     n: ") == 4 and out.count("     window: ") == 4 and out.count("     source: ") == 4
    assert "ESTIMATE (not measured)" in out and "D-358 bar: NOT MET" in out
    assert audit.AUDIT_VERSION == 2


def test_report_reads_and_writes_nothing_outside_stdout(tmp_path, capsys):
    projects, state = build(tmp_path, bound=3)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured"])
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_until_must_follow_since(tmp_path, capsys):
    assert BR.main(["--since", BR.iso(UNTIL), "--until", BR.iso(SINCE), "--projects-dir", str(tmp_path), "--state-dir", str(tmp_path)]) == 2
