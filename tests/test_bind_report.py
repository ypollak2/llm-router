"""BIND W3: ``scripts/release/bind_report.py`` prints the D-358 success bar.

Each criterion gets a status: PASS, FAIL, INSUFFICIENT (the window has not produced the
n the bar needs), PENDING (the hand check has not been entered) or REPORTED (the outcome
rate, which owner decision D-359 turned from a gate into a figure). The corpora below are
synthetic; the arithmetic of the bar is what is pinned. The independent review of PR #816
(rv-PR816-20261103.md) added: the binding denominator and its disclosures (S2), the fail-closed
toll line (S6), the seeded hand-check sample (S7) and zone-required times (S9).
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
          before_since_void=0, start=SINCE, reissued=0, codex_blocked=0, codex_failed=0, reissue_rows=0):
    """Delegations spread from `start` to the end of the window, plus prompts, holds, probes and MCP units.
    `reissued` bound units are followed by the same task again (the escape hatch: a Claude sub-agent runs it);
    `codex_blocked`/`codex_failed` add ledger rows of runs that delivered nothing; `reissue_rows` adds the
    router's own codex_reissue rows."""
    t = Transcript(tmp_path, SID)
    rows = []
    step = (UNTIL - start - 3600) / max(bound + void + blocked, 1)
    clock = start + 600
    for i in range(prompts):
        t.human(SINCE + 60 + i * 30, f"please carry on with step number {i} of the plan now")
    n = 0
    reissue_left = reissued
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
            if kind == "bound" and reissue_left > 0:
                reissue_left -= 1
                t.call(at + 400, f"{tid}r", "Agent", {"prompt": agent_prompt(str(n)), "subagent_type": "task-reviewer"})
                t.result(at + 410, f"{tid}r", "Async agent launched successfully.\nagentId: r" + str(n))
    for i in range(codex_blocked):
        rows.append(codex_row(SINCE + 7000 + i, outcome="codex_blocked", tool_use_id=f"cb{i}"))
    for i in range(codex_failed):
        rows.append(codex_row(SINCE + 8000 + i, outcome="codex_failed", tool_use_id=f"cf{i}"))
    for i in range(reissue_rows):
        rows.append(codex_row(SINCE + 9000 + i, outcome="codex_reissue", tool_use_id=f"cr{i}"))
    for i in range(before_since_void):                       # the pre-fix reference population
        tid = f"old{i}"
        at = SINCE - (i + 1) * 3600
        t.call(at, tid, "Agent", {"prompt": agent_prompt(f"old{i}")})
        t.result(at + 10, tid, "Async agent launched successfully.\nagentId: o" + str(i))
        rows.append(codex_row(at + 9.5, tool_use_id=tid))
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
                             "Outcome rate under the fixed audit (reported, not gated; D-359)")


def test_binding_passes_at_ninety_percent_of_thirty_units(tmp_path, capsys):
    rc, rep, L = run(tmp_path, capsys, bound=27, void=3)
    assert L[BIND]["status"] == "PASS"
    assert L[BIND]["result"].startswith("27/30 = 90.0%")
    assert "30 delegated units: 27 bound, 3 void (a Claude sub-agent ran)" in L[BIND]["n"]
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


def test_a_ledger_id_with_no_call_in_the_transcript_counts_as_unpaired_not_bound(tmp_path, capsys):
    projects, state = build(tmp_path, bound=3)
    rows = [json.loads(ln) for ln in (state / "north_star_units.jsonl").read_text().splitlines()]
    rows.append(codex_row(SINCE + 7000, tool_use_id="not-in-any-transcript"))
    (state / "north_star_units.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured", "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert L[BIND]["result"].startswith("3/4 = 75.0%")                       # an unpaired unit is not bound
    assert "1 with no Agent call found" in L[BIND]["n"] and "0 void" in L[BIND]["n"]


def test_blocked_share_needs_the_hand_check(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, bound=28, blocked=2)
    assert L[BLOCKED]["status"] == "PENDING" and "hand check of the sample of 20 not complete (0 entered)" in L[BLOCKED]["result"]
    assert "automated screen" in L[BLOCKED]["note"] and "2/30" in L[BLOCKED]["note"]
    sample = L[BLOCKED]["note"].split("Sample (unit ids): ")[1].split(", ")
    assert len(sample) == 20 and len(set(sample)) == 20
    assert f"sha256('{BR.HAND_CHECK_SEED}:<unit_id>')" in L[BLOCKED]["note"]          # the rule is printed with the sample


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
    assert L[TOLL]["status"] == "INSUFFICIENT" and "needs a source with >= 50 organic prompts" in L[TOLL]["note"]
    _, _, L = run(tmp_path / "ok", capsys, "--min-prompts", "30", prompts=40)         # the floor is this script's, so it moves
    assert L[TOLL]["status"] == "PASS" and "0 holds in 40 prompts = 0.00 per 100 [Wilson 95% 0.00..8.76]" in L[TOLL]["result"]


def _ledger(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_worse_of_the_ledger_and_the_transcripts_decides_and_both_are_shown(tmp_path, capsys):
    ledger = tmp_path / "hold_events.jsonl"
    rows = [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(200)]
    rows += [{"ts": SINCE + 5000 + i, "event": "blocked", "session_id": SID, "tool": "Bash"} for i in range(3)]
    rows += [{"ts": SINCE - 5, "event": "blocked", "session_id": SID},                    # before the window
             {"ts": SINCE + 99, "event": "blocked", "session_id": "ffffffff-0000-4000-8000-000000000000"}]   # not an organic session here
    _ledger(ledger, rows)
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120)             # the transcripts show 0 holds
    assert L[TOLL]["status"] == "FAIL"                                                    # the ledger's 3 in 200 decides
    assert "hold ledger: 3 holds in 200 prompts = 1.50 per 100" in L[TOLL]["result"]
    assert "transcripts: 0 holds in 120 prompts = 0.00 per 100" in L[TOLL]["result"]       # and the other source is not hidden
    assert "hold ledger" in L[TOLL]["source"]


def test_a_transcript_hold_fails_the_line_even_when_the_ledger_is_clean(tmp_path, capsys):
    """Fail closed: the old rule let the ledger win, so a ledger that missed a hold hid it."""
    ledger = tmp_path / "hold_events.jsonl"
    _ledger(ledger, [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(200)])   # 0 blocked in 200
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120, holds=2)                    # 2 in 120 = 1.67 per 100
    assert L[TOLL]["status"] == "FAIL"
    assert "hold ledger: 0 holds in 200 prompts" in L[TOLL]["result"] and "transcripts: 2 holds in 120 prompts" in L[TOLL]["result"]


def test_an_empty_hold_ledger_is_unknown_and_the_transcripts_are_used(tmp_path, capsys):
    ledger = tmp_path / "hold_events.jsonl"
    _ledger(ledger, [{"ts": SINCE + 1, "event": "blocked", "session_id": SID}])           # a block but no prompt row
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120, holds=2)
    assert "transcripts: 2 holds in 120 prompts" in L[TOLL]["result"] and "hold ledger:" not in L[TOLL]["result"]
    assert "no prompt rows in the window, so it gives no rate" in L[TOLL]["source"]
    assert BR.read_hold_events(tmp_path / "missing.jsonl", SINCE, UNTIL, {SID}) is None
    assert BR.read_hold_events(None, SINCE, UNTIL, {SID}) is None
    _ledger(ledger, [{"ts": SINCE - 99, "event": "prompt", "session_id": SID}])           # no row inside the window
    assert BR.read_hold_events(ledger, SINCE, UNTIL, {SID}) is None


def test_a_probe_still_fails_the_line_below_the_prompt_floor(tmp_path, capsys):
    """The old order tested the floor first, so one 'Reply OK' probe in 10 prompts read INSUFFICIENT."""
    _, _, L = run(tmp_path, capsys, prompts=10, probes=1)
    assert L[TOLL]["status"] == "FAIL" and "1 fixed-reply probes" in L[TOLL]["result"] and L[TOLL]["note"] == ""


def test_a_rate_demonstrably_over_the_bar_fails_below_the_floor(tmp_path, capsys):
    """5 holds in 10 prompts: the Wilson lower bound is 23.7 per 100, so no floor is needed to see the bar is broken."""
    _, _, L = run(tmp_path, capsys, prompts=10, holds=5)
    assert L[TOLL]["status"] == "FAIL" and "5 holds in 10 prompts = 50.00 per 100" in L[TOLL]["result"]


def test_one_hold_in_forty_prompts_is_insufficient_and_never_a_pass(tmp_path, capsys):
    """2.5 per 100 on 40 prompts: the Wilson lower bound (0.44) is under the bar, so it is not proven broken, and not clear."""
    _, _, L = run(tmp_path, capsys, prompts=40, holds=1)
    assert L[TOLL]["status"] == "INSUFFICIENT"
    assert "needs a source with >= 50 organic prompts" in L[TOLL]["note"]


def test_a_small_source_over_the_bar_blocks_a_pass(tmp_path, capsys):
    ledger = tmp_path / "hold_events.jsonl"
    rows = [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(40)]
    rows.append({"ts": SINCE + 5000, "event": "blocked", "session_id": SID, "tool": "Bash"})      # 1 in 40 on the ledger
    _ledger(ledger, rows)
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120)                    # the transcripts: 0 in 120 (enough)
    assert L[TOLL]["status"] == "INSUFFICIENT"
    assert "hold ledger reads at or above 1 per 100 on fewer than 50 prompts" in L[TOLL]["note"]
    assert L[TOLL]["status"] != "PASS"


def test_ledger_probe_events_fail_the_line_when_no_transcript_shows_a_probe(tmp_path, capsys):
    ledger = tmp_path / "hold_events.jsonl"
    rows = [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(150)]
    rows += [{"ts": SINCE + 7000, "event": "probe", "session_id": SID, "tool": "mcp__llm_router__llm"}]
    _ledger(ledger, rows)
    _, _, L = run(tmp_path, capsys, "--holds-file", str(ledger), prompts=120)
    assert L[TOLL]["status"] == "FAIL" and "1 fixed-reply probes" in L[TOLL]["result"]
    assert "(transcripts 0, hold ledger probe events 1)" in L[TOLL]["n"]
    _ledger(ledger, [{"ts": SINCE + 7000, "event": "probe", "session_id": SID, "tool": "x"}])    # a ledger with no prompt row
    _, _, L = run(tmp_path / "noprompt", capsys, "--holds-file", str(ledger), prompts=120)
    assert L[TOLL]["status"] == "FAIL" and "1 fixed-reply probes" in L[TOLL]["result"]           # its probe row still counts


def test_no_prompts_at_all_is_insufficient_and_does_not_crash_with_a_zero_floor(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, "--min-prompts", "0", bound=3)                                # 0 prompts, floor 0
    assert L[TOLL]["status"] == "INSUFFICIENT" and "0 holds in 0 prompts = n/a per 100" in L[TOLL]["result"]
    _, _, L = run(tmp_path / "default", capsys, bound=3)
    assert L[TOLL]["status"] == "INSUFFICIENT"


def test_the_hold_ledger_is_found_in_the_state_dir_by_default(tmp_path, capsys):
    projects, state = build(tmp_path, prompts=120)
    _ledger(state / "hold_events.jsonl", [{"ts": SINCE + 10 * i, "event": "prompt", "session_id": SID} for i in range(150)])
    hist = tmp_path / "h.json"
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(hist), "--reference", "measured", "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert "0 holds in 150 prompts" in L[TOLL]["result"] and L[TOLL]["status"] == "PASS"


def test_outcome_line_is_reported_not_gated_and_cites_d359(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, void=40, used_mcp=5, extra_unused=20)
    g = L[GATE]
    assert g["status"] == "REPORTED"                                   # a figure, never PASS or FAIL
    assert g["result"].startswith("5/65 = 7.7% [Wilson 95%")
    assert all("D-359" in g[k] for k in ("criterion", "target", "note"))
    assert "0.229" not in json.dumps(g) and " vs " not in g["result"]  # no comparison with 15.4.0 (a different method)
    assert "not compared with 15.4.0" in g["note"]


def test_outcome_line_is_reported_at_a_rate_of_zero_and_on_a_small_sample(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, void=60)                           # 0 used of 60: the old line failed here
    assert L[GATE]["status"] == "REPORTED" and L[GATE]["result"].startswith("0/60 = 0.000%")
    _, _, L = run(tmp_path / "small", capsys, bound=10)
    assert L[GATE]["status"] == "REPORTED" and "too few to tell" in L[GATE]["result"]


def test_outcome_line_quotes_the_recorded_baseline_of_its_own_audit_version(tmp_path, capsys):
    projects, state = build(tmp_path, void=40, used_mcp=5, extra_unused=20)
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps({"releases": [HISTORY["releases"][0],
                                             {"release": "16.0.0", "audit_version": 2, "used_rate": 0.5, "used": 50, "attempted": 100}]}))
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(hist), "--reference", "measured", "--json"])
    g = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}[GATE]
    assert "Recorded v2 baseline: 16.0.0 50/100 = 50.0% (same audit version, like for like)" in g["note"]
    assert "No v2 baseline" not in g["note"]
    _, _, L = run(tmp_path / "v1only", capsys, void=40, used_mcp=5, extra_unused=20)          # history holds only 15.4.0 (v1)
    assert "No v2 baseline is recorded yet" in L[GATE]["note"]
    assert "pre-release-verify.sh --outcome-cross-version rebaseline" in L[GATE]["note"]


def test_the_verdict_counts_the_gated_lines_only(tmp_path, capsys):
    """Lines 1 to 3 pass and the outcome line reads 0 used of 40: the bar is met, because that line is reported."""
    projects, state = build(tmp_path, bound=40, prompts=150)
    sample = BR.hand_check_sample(_bound_ids(projects, state))
    hand = tmp_path / "hand.jsonl"
    hand.write_text("".join(json.dumps({"unit_id": i, "blocked": False}) + "\n" for i in sample))
    rc = BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
                  "--history", str(tmp_path / "h.json"), "--reference", "measured", "--hand-checked", str(hand), "--json"])
    rep = json.loads(capsys.readouterr().out)
    L = {x["criterion"]: x for x in rep["lines"]}
    assert [L[k]["status"] for k in (BIND, BLOCKED, TOLL, GATE)] == ["PASS", "PASS", "PASS", "REPORTED"]
    assert rep["verdict"] == "MET" and rc == 0


def test_the_bar_is_met_only_when_every_gated_line_passes(tmp_path, capsys):
    rc, rep, L = run(tmp_path, capsys, bound=40, void=0, prompts=150, used_mcp=3, extra_unused=10)
    assert L[BIND]["status"] == "PASS" and L[TOLL]["status"] == "PASS" and L[GATE]["status"] == "REPORTED"
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
        assert any(f"[{s}]" in status_line for s in ("PASS", "FAIL", "INSUFFICIENT", "PENDING", "REPORTED"))
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


# ── S2: the binding denominator and what is printed beside it ────────────────

def test_the_denominator_is_every_deny_that_carried_a_codex_result_and_the_runs_that_did_not_are_printed(tmp_path, capsys):
    """codex_blocked and codex_failed delivered nothing, so they are not in the 27/30; they are printed beside it."""
    rc, rep, L = run(tmp_path, capsys, bound=27, void=3, codex_blocked=4, codex_failed=1)
    b = L[BIND]
    assert b["result"].startswith("27/30 = 90.0%") and b["status"] == "PASS"
    assert "codex_blocked: 4 of 35 Codex runs (11.4%) answered 'blocked' and were not delivered" in b["disclosed"][0]
    assert "codex_failed: 1 of 35 Codex runs (2.9%) failed" in b["disclosed"][1]
    assert "re-issued after delivery: 0 of 27 delivered results (0.000%)" in b["disclosed"][2]
    out = BR.render(rep)
    assert out.count("     disclosed: ") == 3 and "disclosed: codex_blocked: 4 of 35" in out


def test_a_reissue_after_a_delivered_result_counts_as_not_bound_and_is_printed(tmp_path, capsys):
    """The escape hatch: Claude calls Agent again with the same prompt and a Claude sub-agent redoes the work."""
    _, _, L = run(tmp_path, capsys, bound=30, reissued=4, reissue_rows=4)
    b = L[BIND]
    assert b["status"] == "FAIL" and b["result"].startswith("26/30 = 86.7%")                  # 4 of 30 do not bind
    assert "4 re-issued" in b["n"] and "26 bound" in b["n"]
    assert "re-issued after delivery: 4 of 30 delivered results (13.3%)" in b["disclosed"][2]
    assert "codex_reissue ledger rows in the window: 4" in b["disclosed"][2]


def test_a_reissue_outside_the_window_is_not_counted_in_the_ledger_line(tmp_path, capsys):
    projects, state = build(tmp_path, bound=3)
    rows = [json.loads(ln) for ln in (state / "north_star_units.jsonl").read_text().splitlines()]
    rows += [codex_row(SINCE - 10, outcome="codex_reissue", tool_use_id="before"),
             codex_row(UNTIL + 10, outcome="codex_reissue", tool_use_id="after")]
    (state / "north_star_units.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured", "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert "codex_reissue ledger rows in the window: 0" in L[BIND]["disclosed"][2]


def test_a_deny_without_the_codex_wrapper_is_named_as_wording_drift_not_hidden(tmp_path, capsys):
    """A deny whose text matches no known Codex wrapper is not bound, and the line says why."""
    projects, state = build(tmp_path, bound=2)
    for f in projects.rglob("*.jsonl"):
        f.write_text(f.read_text().replace("Subagent task was delegated to Codex CLI", "Some future wording of the handoff"))
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured", "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert L[BIND]["result"].startswith("0/2 = 0.000%")
    assert "2 deny without the Codex wrapper (wording drift or a different deny)" in L[BIND]["n"]


# ── S7: the hand-check sample is pre-registered ──────────────────────────────

def test_the_sample_is_fixed_by_a_pinned_seed_and_changes_with_it():
    ids = [f"agent_route_codex:s:{i}" for i in range(50)]
    assert BR.HAND_CHECK_SEED == "D-358/hand-check/v1" and BR.HAND_CHECK_N == 20
    assert BR.hand_check_sample(ids)[:5] == ["agent_route_codex:s:20", "agent_route_codex:s:4", "agent_route_codex:s:23",
                                             "agent_route_codex:s:38", "agent_route_codex:s:7"]
    assert BR.hand_check_sample(ids, seed="another seed")[:3] != BR.hand_check_sample(ids)[:3]


def test_a_hand_check_of_units_outside_the_sample_does_not_count(tmp_path, capsys):
    """Cherry-picking: twenty favourable entries for units the seed did not pick leave the line PENDING."""
    projects, state = build(tmp_path, bound=30)
    ids = _bound_ids(projects, state)
    sample = BR.hand_check_sample(ids)
    others = [i for i in ids if i not in sample]
    pick = tmp_path / "picked.jsonl"
    pick.write_text("".join(json.dumps({"unit_id": i, "blocked": False}) + "\n" for i in others))   # 10 hand-picked, all clean
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured", "--hand-checked", str(pick), "--json"])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert L[BLOCKED]["status"] == "PENDING" and "(0 entered)" in L[BLOCKED]["result"]
    assert "(10 ignored)" in L[BLOCKED]["note"]


def test_a_partial_sample_is_pending_and_extra_entries_cannot_change_a_complete_one(tmp_path, capsys):
    projects, state = build(tmp_path, bound=30)
    ids = _bound_ids(projects, state)
    sample = BR.hand_check_sample(ids)
    args = ["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
            "--history", str(tmp_path / "h.json"), "--reference", "measured", "--json"]
    part = tmp_path / "part.jsonl"
    part.write_text("".join(json.dumps({"unit_id": i, "blocked": False}) + "\n" for i in sample[:19]))
    BR.main([*args, "--hand-checked", str(part)])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert L[BLOCKED]["status"] == "PENDING" and "(19 entered)" in L[BLOCKED]["result"]
    full = tmp_path / "full.jsonl"                                       # the whole sample is clean; 10 other units say blocked
    full.write_text("".join(json.dumps({"unit_id": i, "blocked": False}) + "\n" for i in sample)
                    + "".join(json.dumps({"unit_id": i, "blocked": True}) + "\n" for i in ids if i not in sample))
    BR.main([*args, "--hand-checked", str(full)])
    L = {x["criterion"]: x for x in json.loads(capsys.readouterr().out)["lines"]}
    assert L[BLOCKED]["status"] == "PASS" and "0/20 = 0.000% by hand check" in L[BLOCKED]["result"]
    assert "(10 ignored)" in L[BLOCKED]["note"]


def test_the_blocked_share_is_insufficient_with_fewer_than_twenty_bound_units(tmp_path, capsys):
    _, _, L = run(tmp_path, capsys, bound=19)
    assert L[BLOCKED]["status"] == "INSUFFICIENT" and "19 bound units" in L[BLOCKED]["result"]
    _, _, L = run(tmp_path / "twenty", capsys, bound=20)
    assert L[BLOCKED]["status"] == "PENDING" and "sample of 20" in L[BLOCKED]["result"]


# ── S9: a time with no zone is refused ───────────────────────────────────────

def test_a_time_without_a_zone_is_refused_rather_than_read_as_local_time(tmp_path, capsys):
    with pytest.raises(ValueError, match="no time zone"):
        BR.parse_utc("2026-09-25T12:00:00")
    assert BR.parse_utc("2026-09-25T12:00:00Z") == BR.parse_utc("2026-09-25T14:00:00+02:00")
    for flag in ("--since", "--until"):
        argv = ["--since", "2026-09-25T12:00:00Z", "--until", "2026-09-29T12:00:00Z", "--projects-dir", str(tmp_path),
                "--state-dir", str(tmp_path)]
        argv[argv.index(flag) + 1] = "2026-09-26T12:00:00"
        assert BR.main(argv) == 2
        assert "no time zone" in capsys.readouterr().err
