"""BIND W3: every threshold of the D-358 bar, and of the audit rules it rests on, pinned on both sides.

The independent review of PR #816 mutated the code 47 times and 18 mutations survived, among them the 3-day
and the 50-prompt thresholds: the tests had checked that a bar passes and that it fails, never WHERE. Each
test here sits on the edge (the value itself and one step either side), so moving a constant by a step or
flipping ``>=`` to ``>`` fails one of them. ``tests/bind_mutations.py`` re-runs the mutations, one textual
change each, and prints which were killed; its last result is in the PR description.
"""
from __future__ import annotations

import json

import pytest

from tests.bind_corpus import (
    ANSWER, NOW, PROJ, SID, Transcript, _iso, agent_prompt, codex_row, launch_text, w1_wrapper, write_state,
)
from tests.test_bind_audit import A, _lt, audit_units, by_kind
from tests.test_bind_report import BIND, BLOCKED, BR, DAY, SINCE, TOLL, UNTIL, build
from tests.test_outcome_gate import _rows, _run, _summary


def W(n: int, start: int = 1) -> str:
    """n distinct one-token words, w001 w002 ...: every shingle is distinct, so fractions are exact."""
    return " ".join(f"w{i:03d}" for i in range(start, start + n))


def report(tmp_path, capsys, *extra, since: float = SINCE, reference: str = "measured", **corpus) -> dict:
    projects, state = build(tmp_path, start=since, **corpus)
    BR.main(["--since", BR.iso(since), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", reference, "--json", *extra])
    return json.loads(capsys.readouterr().out)


def lines_of(rep: dict) -> dict:
    return {x["criterion"]: x for x in rep["lines"]}


# ── line 1: the binding rate ─────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,status", [(259200, "PASS"), (258336, "INSUFFICIENT"), (260064, "PASS")])
def test_binding_needs_three_days_exactly(tmp_path, capsys, seconds, status):
    """3 days = 259200 s. 30 bound units in a window of 2.99 days read INSUFFICIENT; in 3.00 and 3.01 days they pass."""
    L = lines_of(report(tmp_path, capsys, since=UNTIL - seconds, bound=30))
    assert L[BIND]["status"] == status, L[BIND]


@pytest.mark.parametrize("bound,status", [(30, "PASS"), (29, "INSUFFICIENT")])
def test_binding_needs_thirty_units_exactly(tmp_path, capsys, bound, status):
    L = lines_of(report(tmp_path, capsys, bound=bound))
    assert L[BIND]["status"] == status and f"{bound}/{bound} = 100.0%" in L[BIND]["result"]


@pytest.mark.parametrize("bound,void,status", [(90, 10, "PASS"), (89, 11, "FAIL"), (27, 3, "PASS"), (26, 4, "FAIL")])
def test_binding_rate_bar_is_ninety_percent_exactly(tmp_path, capsys, bound, void, status):
    L = lines_of(report(tmp_path, capsys, bound=bound, void=void))
    assert L[BIND]["status"] == status, L[BIND]


def test_a_unit_at_either_edge_of_the_window_is_inside_and_one_second_beyond_is_outside(tmp_path, capsys):
    projects, state = build(tmp_path, bound=34)
    rows = [json.loads(ln) for ln in (state / "north_star_units.jsonl").read_text().splitlines()]
    for row, ts in zip(rows[:4], (SINCE, UNTIL, SINCE - 1, UNTIL + 1)):
        row["ts"] = ts                                    # two on the edges, two one second outside
    (state / "north_star_units.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "measured", "--json"])
    L = lines_of(json.loads(capsys.readouterr().out))
    assert L[BIND]["result"].startswith("32/32 = 100.0%"), L[BIND]["result"]                 # 34 rows - the 2 outside


# ── line 2: the hand check ───────────────────────────────────────────────────

@pytest.mark.parametrize("bound,status", [(19, "INSUFFICIENT"), (20, "PENDING"), (21, "PENDING")])
def test_the_hand_check_needs_twenty_bound_units_and_samples_twenty(tmp_path, capsys, bound, status):
    L = lines_of(report(tmp_path, capsys, bound=bound))
    assert L[BLOCKED]["status"] == status
    if status == "PENDING":
        assert "sample of 20" in L[BLOCKED]["result"]
        assert len(L[BLOCKED]["note"].split("Sample (unit ids): ")[1].split(", ")) == 20


def test_the_blocked_share_bar_sits_between_five_and_ten_percent(tmp_path, capsys):
    """With a sample of 20 the share moves in steps of 5%: 1 of 20 passes, 2 of 20 (10%) fails."""
    projects, state = build(tmp_path, bound=30)
    audit = BR._audit_modules()[1]
    units, _ = audit._collect(30, projects, state, UNTIL)
    sample = BR.hand_check_sample([u.unit_id for u in units if u.extra.get("bound")])
    for n_blocked, status in ((0, "PASS"), (1, "PASS"), (2, "FAIL"), (3, "FAIL")):
        hand = tmp_path / f"hand{n_blocked}.jsonl"
        hand.write_text("".join(json.dumps({"unit_id": i, "blocked": k < n_blocked}) + "\n" for k, i in enumerate(sample)))
        BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
                 "--history", str(tmp_path / "h.json"), "--reference", "measured", "--hand-checked", str(hand), "--json"])
        L = lines_of(json.loads(capsys.readouterr().out))
        assert L[BLOCKED]["status"] == status and f"{n_blocked}/20" in L[BLOCKED]["result"], L[BLOCKED]


# ── line 3: toll probes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prompts,status", [(49, "INSUFFICIENT"), (50, "PASS")])
def test_the_toll_floor_is_fifty_prompts_exactly(tmp_path, capsys, prompts, status):
    L = lines_of(report(tmp_path, capsys, prompts=prompts))
    assert L[TOLL]["status"] == status, L[TOLL]


@pytest.mark.parametrize("blocked,status", [(99, "PASS"), (100, "FAIL"), (101, "FAIL")])
def test_the_hold_bar_is_one_per_hundred_prompts_exactly(tmp_path, capsys, blocked, status):
    """10000 prompts on the router's ledger: 99 holds is 0.99 per 100 (under the bar), 100 holds is 1.00 (at it)."""
    ledger = tmp_path / "hold_events.jsonl"
    rows = [{"ts": SINCE + 1 + i, "event": "prompt", "session_id": SID} for i in range(10000)]
    rows += [{"ts": SINCE + 20000 + i, "event": "blocked", "session_id": SID, "tool": "Bash"} for i in range(blocked)]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    L = lines_of(report(tmp_path, capsys, "--holds-file", str(ledger), prompts=120))
    assert L[TOLL]["status"] == status, L[TOLL]["result"]


def test_the_toll_floor_follows_the_option_and_defaults_to_fifty():
    assert BR.MIN_PROMPTS == 50 and BR.HOLDS_PER_100_MAX == 1.0 and BR.HAND_CHECK_N == 20
    assert (BR.BIND_RATE_MIN, BR.BIND_MIN_UNITS, BR.BIND_MIN_DAYS, BR.BLOCKED_SHARE_MAX) == (0.90, 30, 3.0, 0.10)
    assert (BR.MIN_REFERENCE_UNITS, BR.GATE_DAYS) == (30, 30)


# ── the live reference of the savings estimate ───────────────────────────────

def _void_history(tmp_path, n: int, back_s: float):
    """n delegated units that each ran a Claude sub-agent, about `back_s` seconds before UNTIL, with its transcript."""
    t = Transcript(tmp_path, SID)
    rows = []
    sub = tmp_path / "projects" / PROJ / SID / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        at = UNTIL - back_s - i * 60
        tid = f"v{i}"
        t.call(at, tid, "Agent", {"prompt": agent_prompt(tid), "subagent_type": "task-reviewer"})
        t.result(at + 10, tid, launch_text(f"v{i}"))
        rows.append(codex_row(at + 9.5, tool_use_id=tid))
        rec = {"type": "assistant", "message": {"id": f"m{i}", "model": "claude-opus-5-5", "usage": {"output_tokens": 100 + i}}}
        (sub / f"agent-v{i}.jsonl").write_text(json.dumps(rec) + "\n")
    t.write()
    return tmp_path / "projects", write_state(tmp_path, rows)


def _reference_kind(tmp_path, capsys, n: int, back_s: float) -> str:
    projects, state = _void_history(tmp_path, n, back_s)
    BR.main(["--since", BR.iso(SINCE), "--until", BR.iso(UNTIL), "--projects-dir", str(projects), "--state-dir", str(state),
             "--history", str(tmp_path / "h.json"), "--reference", "live", "--json"])
    return json.loads(capsys.readouterr().out)["savings_estimate"]["reference"]


@pytest.mark.parametrize("n,kind", [(30, "live, organic sessions only"), (29, "measured 2026-11-03, all sessions")])
def test_a_live_reference_needs_thirty_void_units(tmp_path, capsys, n, kind):
    assert _reference_kind(tmp_path, capsys, n, 6 * DAY) == kind


@pytest.mark.parametrize("days_back,kind", [(29.5, "live, organic sessions only"), (30.5, "measured 2026-11-03, all sessions")])
def test_the_outcome_look_back_is_thirty_days(tmp_path, capsys, days_back, kind):
    """The look-back that feeds the live reference ends 30 days before --until: 29.5 days back is read, 30.5 is not."""
    assert _reference_kind(tmp_path, capsys, 30, days_back * DAY) == kind


# ── the audit's scoring thresholds ───────────────────────────────────────────

def test_used_needs_sixty_percent_of_the_shingles():
    body = W(107)                                           # 100 eight-word shingles, all distinct
    assert A.body_reuse(body, W(66)) == (0.59, 107)         # the first 66 words hold 59 of them
    assert A.body_reuse(body, W(67)) == (0.60, 107)
    assert A.label_routed_mcp(body, False, W(66), True, None)[:2] == ("unknown", "tool_result_ambiguous_overlap")
    assert A.label_routed_mcp(body, False, W(67), True, None)[:2] == ("used", "tool_result_reused")
    assert A.USED_THRESHOLD == 0.60


def test_ambiguous_starts_at_five_percent_of_the_shingles():
    one_in_twenty = W(27)                                   # 20 shingles; the host holds the last 8 words: 1 of 20 = 5%
    assert A.body_reuse(one_in_twenty, W(8, 20)) == (0.05, 27)
    assert A.label_routed_mcp(one_in_twenty, False, W(8, 20), True, None)[:2] == ("unknown", "tool_result_ambiguous_overlap")
    one_in_twenty_one = W(28)                               # 21 shingles: 1 of 21 = 4.76%
    assert A.body_reuse(one_in_twenty_one, W(8, 21))[0] == pytest.approx(1 / 21)
    assert A.label_routed_mcp(one_in_twenty_one, False, W(8, 21), True, None)[:2] == ("unused", "tool_result_not_reused")
    assert A.AMBIGUOUS_FLOOR == 0.05


@pytest.mark.parametrize("words,size", [(7, 7), (8, 4), (19, 4), (20, 8)])
def test_the_shingle_size_changes_at_eight_and_at_twenty_words(words, size):
    assert A.shingle_size(words) == size and A.MIN_ATTRIBUTABLE_WORDS == 8


def test_a_nineteen_word_body_uses_four_word_shingles_and_a_twenty_word_body_eight():
    """The first 13 words of a 19-word body are 10 of its 16 four-word shingles (62.5%: used); the first 14 words
    of a 20-word body are 7 of its 13 eight-word shingles (54%: not used), where four-word shingles would give 65%."""
    assert A.label_routed_mcp(W(19), False, W(13), True, None)[:2] == ("used", "tool_result_reused")
    assert A.label_routed_mcp(W(20), False, W(14), True, None)[:2] == ("unknown", "tool_result_ambiguous_overlap")


@pytest.mark.parametrize("prompt,expected", [
    ("Reply " + "x" * 143, True),                          # 149 characters
    ("Reply " + "x" * 144, False),                         # 150
    ("x" * 190 + " reply OK", True),                       # 199
    ("x" * 191 + " reply OK", False),                      # 200
    ("Reply OK?", False),                                  # a question mark makes it a question
    ("Reply OK\nand then continue", False),                # a second line carries context
])
def test_the_probe_rule_has_its_length_edges(prompt, expected):
    assert A.is_probe_prompt(prompt) is expected


@pytest.mark.parametrize("n,rc", [(49, 1), (50, 0)])
def test_the_gate_needs_fifty_attempted_units(tmp_path, capsys, n, rc):
    """Under 50 attempted units the rate is too few to tell and the gate fails without a stated reason; at 50 it records."""
    rows = _rows(2, n)
    got, _ = _run(tmp_path, rows, _summary(rows), "1.0.0")
    out = capsys.readouterr().out
    assert got == rc, out
    assert A.MIN_N == 50 and (("too few to tell" in out) is (n == 49))


# ── the pairing windows ──────────────────────────────────────────────────────

T = NOW - 5000


def _pairing(tmp_path, calls, hooks=(), row_type="task-reviewer"):
    """One ledger row at T with no tool_use_id. calls: (tid, ts, subagent_type). hooks: (tid, ts)."""
    t = Transcript(tmp_path, SID)
    t.human(T - 900, "please run the review")
    events = [(ts, 0, ("call", tid, typ)) for tid, ts, typ in calls] + [(ts, 1, ("hook", tid, None)) for tid, ts in hooks]
    for ts, _o, (kind, tid, typ) in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == "call":
            t.call(ts, tid, "Agent", {"prompt": agent_prompt(tid), "subagent_type": typ})
        else:
            t.hook(ts, tid, "x")
    t.write()
    units, _ = audit_units(tmp_path, write_state(tmp_path, [codex_row(T, subagent_type=row_type)]))
    (u,) = by_kind(units, "agent_route_codex")
    return u.extra["pairing"], u.extra["tool_use_id"] if u.extra["call_found"] else None


@pytest.mark.parametrize("hook_at,how", [
    (T - 2.0, "hook_run"), (T - 2.5, "call_time_window"),
    (T + 20.0, "hook_run"), (T + 20.5, "call_time_window"),
])
def test_a_hook_run_joins_a_row_from_two_seconds_before_to_twenty_seconds_after(tmp_path, hook_at, how):
    got, tid = _pairing(tmp_path, [("t1", T - 40, "task-reviewer")], [("t1", hook_at)])
    assert (got, tid) == (how, "t1")                       # the fallback finds the same call by time, so the HOW is what moves


@pytest.mark.parametrize("call_at,how", [
    (T - 330, "call_time_window"), (T - 331, "none"),
    (T + 2, "call_time_window"), (T + 3, "none"),
])
def test_the_last_resort_pairing_reaches_330_seconds_back_and_two_seconds_ahead(tmp_path, call_at, how):
    got, _ = _pairing(tmp_path, [("t1", call_at, "task-reviewer")])
    assert got == how


def test_the_last_resort_takes_the_latest_call_before_the_row(tmp_path):
    got, tid = _pairing(tmp_path, [("early", T - 100, "task-reviewer"), ("late", T - 50, "task-reviewer")])
    assert (got, tid) == ("call_time_window", "late")


def test_two_hook_runs_pair_by_subagent_type_before_distance(tmp_path):
    calls = [("tx", T - 30, "general-purpose"), ("ty", T - 29, "task-reviewer")]
    got, tid = _pairing(tmp_path, calls, [("tx", T + 0.1), ("ty", T + 1.0)])       # tx is nearer, ty has the row's type
    assert (got, tid) == ("hook_run", "ty")
    got, tid = _pairing(tmp_path / "same", [("tx", T - 30, "task-reviewer"), ("ty", T - 29, "task-reviewer")],
                        [("tx", T + 0.1), ("ty", T + 1.0)])                         # equal types: the nearer run
    assert (got, tid) == ("hook_run", "tx")


# ── the re-issue rule ────────────────────────────────────────────────────────

def _reissue(tmp_path, second_prompt: str, gap: float = 60.0, first: str | None = None):
    first = W(13) if first is None else first
    t = Transcript(tmp_path, SID)
    t0 = NOW - 9000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": first})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), is_error=True)
    t.call(t0 + 30 + gap, "tB2", "Agent", {"prompt": second_prompt})
    t.result(t0 + 32 + gap, "tB2", launch_text("aaaa"))
    t.write()
    units, _ = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    (u,) = by_kind(units, "agent_route_codex")
    return u.extra["respawned"]


def test_a_reissue_is_the_same_prompt_once_stripped_and_nothing_looser(tmp_path):
    """The router's escape hatch (BIND W1) matches sha256 of the stripped prompt. The same words with another word added,
    or a task asked in other words, are not a re-issue: the old 4-word-shingle rule read sibling tasks as each other's."""
    assert _reissue(tmp_path / "same", W(13)) is True
    assert _reissue(tmp_path / "spaces", "  " + W(13) + "\n") is True
    assert _reissue(tmp_path / "extra", W(13) + " plus") is False
    assert _reissue(tmp_path / "fewer", W(12)) is False
    assert _reissue(tmp_path / "other", W(13, 50)) is False


def test_two_empty_prompts_are_not_a_reissue(tmp_path):
    assert _reissue(tmp_path / "empty", "", first="") is False
    assert _reissue(tmp_path / "blank", "   ", first="  \n") is False


def test_the_same_prompt_asked_inside_a_sub_agent_is_not_the_parents_reissue(tmp_path):
    """The escape hatch is the parent asking again. A sub-agent that asks the same words in its own transcript is not."""
    t = Transcript(tmp_path, SID)
    t0 = NOW - 9000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": W(13)})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), is_error=True)
    t.call(t0 + 40, "tTOP", "Agent", {"prompt": W(13, 90), "subagent_type": "general-purpose"})
    t.result(t0 + 41, "tTOP", launch_text("lead"))
    t.write()
    t.subagent("lead", [{"type": "assistant", "timestamp": _iso(t0 + 60), "isSidechain": True, "sessionId": SID,
                         "message": {"content": [{"type": "tool_use", "id": "tNEST", "name": "Agent",
                                                  "input": {"prompt": W(13)}}]}}],
               tool_use_id="tTOP", agent_type="general-purpose")
    units, _ = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["respawned"] is False and u.extra["bound"] is True


def test_sibling_tasks_from_one_template_are_not_each_others_reissues(tmp_path, capsys):
    """100 delegations an hour apart whose prompts differ by one number: all 100 are bound. (The old similarity rule
    read 99 of them as a re-issue of their neighbour.)"""
    L = lines_of(report(tmp_path, capsys, bound=90, void=10))
    assert "0 re-issued" in L[BIND]["n"] and L[BIND]["result"].startswith("90/100 = 90.0%")


@pytest.mark.parametrize("gap,reissued", [(3599.5, True), (3599.6, False)])
def test_the_reissue_window_ends_3600_seconds_after_the_ledger_row(tmp_path, gap, reissued):
    """The router remembers a delivered prompt for 3600 s from the row it wrote (t0 + 29.5); the second call is at
    t0 + 30 + gap, so gap 3599.5 is exactly 3600 s after the row."""
    assert _reissue(tmp_path, W(13), gap) is reissued


# ── the local_task keep window ───────────────────────────────────────────────

@pytest.mark.parametrize("after,label", [(6 * 3600, "used"), (6 * 3600 + 1, "unknown")])
def test_a_local_task_commit_counts_for_six_hours(tmp_path, after, label):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 40000
    t.human(t0, "have the local model fix the flaky test then commit it")
    t.call(t0 + 10, "u_lt", "mcp__llm_router__llm_local_task",
           {"objective": "fix the flaky test", "workdir": "/work/wt-a", "apply_writes": True})
    t.result(t0 + 50, "u_lt", json.dumps({"result": json.dumps(_lt(changed_files=["tests/test_a.py"]))}))
    t.call(t0 + 10 + after, "b_lt", "Bash", {"command": "cd /work/wt-a && git add -A && git commit -m fix"})
    t.result(t0 + 11 + after, "b_lt", "[x] ok")
    t.write()
    units, _ = audit_units(tmp_path, write_state(tmp_path))
    (u,) = by_kind(units, "routed_mcp")
    assert u.label == label


def test_a_sibling_directory_with_a_hyphen_is_not_the_workdir():
    wd, changed = "/work/wt-a", ["src/a.py"]
    assert A.local_task_follow_up([("bash", "cd /work/wt-a-2 && git add -A && git commit -m x")], wd, changed) is None
    assert A.local_task_follow_up([("bash", "cd /work/wt-a && git add -A && git commit -m x")], wd, changed) == "kept"


@pytest.mark.parametrize("cmd,expected", [
    ("git add -A && git commit -m x", "kept"),
    ("git add -u && git commit -m x", "kept"),
    ("git add --all && git commit -m x", "kept"),
    ("git add --update && git commit -m x", "kept"),
    ("git add && git commit -m x", None),                  # nothing named: git adds nothing
    ("git add -v && git commit -m x", None),               # a flag that is neither -A nor -u names nothing either
    ("git add -N && git commit -m x", None),
])
def test_what_a_bare_git_add_stages(cmd, expected):
    assert A.local_task_follow_up([("bash", f"cd /work/wt-a && {cmd}")], "/work/wt-a", ["src/a.py"]) == expected


# ── bound needs the deny shape ───────────────────────────────────────────────

def test_the_hooks_own_reason_marks_a_result_only_when_most_of_it_is_there():
    reason = W(40)                                         # 33 eight-word shingles
    assert A.carries_codex_text(W(30), reason) is True     # 23 of 33 (70%) are in the result
    assert A.carries_codex_text(W(10), reason) is False    # 3 of 33 (9%): an overlap, not the text
    assert A.carries_codex_text(W(10), None) is False




def test_a_hook_that_allowed_the_call_is_not_a_deny(tmp_path):
    allow = json.dumps({"decision": "block", "reason": "r", "hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": "r"}})
    from tests.test_bind_audit import _one_unit
    u, b = _one_unit(tmp_path, "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), True, stdout=allow)
    assert u.extra["deny_shape"] is False and u.extra["bound"] is False and b["delivered_without_deny_shape"] == 1


def test_a_rendered_result_must_be_an_error_that_opens_with_the_hook_error_prefix(tmp_path):
    from tests.test_bind_audit import _one_unit
    not_error, _ = _one_unit(tmp_path / "a", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), False)
    assert not_error.extra["deny_shape"] is False and not_error.extra["bound"] is False
    other_prefix, _ = _one_unit(tmp_path / "b", "Agent finished: " + w1_wrapper(ANSWER), True)
    assert other_prefix.extra["deny_shape"] is False and other_prefix.extra["bound"] is False
    both_phrases = w1_wrapper(ANSWER)
    only_one, _ = _one_unit(tmp_path / "c", "PreToolUse:Agent hook error: " + both_phrases.split("\n\n")[0], True)
    assert only_one.extra["marker"] is False                  # one wrapper phrase is not the wrapper
