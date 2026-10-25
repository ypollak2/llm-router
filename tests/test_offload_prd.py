"""PLAN v16 P1.9: offload per action, the 15-case membership fixture, lanes, pricing, the kpi block.

Each fixture case lives in its own organic session (``case-NN``) so a case is looked up by session id and
asserts BOTH sides: is it in the denominator, is it in the numerator, is it ``spread``. Every numerator case
has a quality-sampled lane (``lane_quality.jsonl``, PLAN section 1.1); the lane rule is tested on its own.
No prompt text anywhere: rows hold ids, models and sizes."""
from __future__ import annotations

import json

import pytest

from llm_router import lane_quality, northstar as ns, offload_share as osh, paths, pricing, session_kind
from llm_router.commands import kpi

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

SINCE, UNTIL = NOW - 86400.0, NOW
T = NOW - 3000


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _mcp(sid, *, model, provider, task_type="code", reason_code=None, ts=T + 50, tool="tu-"):
    """A row as ``northstar.local_shadow_units(all_providers=True)`` yields it."""
    return {"session_id": sid, "ts": _iso(ts), "task_type": task_type, "model": model, "provider": provider,
            "reason_code": reason_code, "tool_use_id": tool + sid, "success": 1, "complexity": "simple"}


def _techop(sid, *, served_by="local_model", bytes_in=8192, re_read=False, ts=T + 60):
    return {"ts": ts, "session_id": sid, "tool": "Bash", "op_class": "grep", "bytes_in": bytes_in,
            "bytes_out": 900, "served_by": served_by, "re_read": re_read}


def _audit(door, cls, group, *, n=30, ok=28, ts=NOW - 86400.0):
    return {"ts": ts, "door": door, "served_class": cls, "task_group": group, "kind": "audit", "n": n,
            "acceptable": ok, "source": "fixture"}


#: lane -> sampled. Case 15 sits in (proxy, local, code), which has no passing batch.
QUALITY = [_audit("proxy", "haiku", "qa"), _audit("mcp", "local", "code"), _audit("proxy", "local", "qa"),
           _audit("mcp", "very_cheap", "qa"), _audit("techop", "local", "tech_op"),
           _audit("direct", "local", "code"), _audit("subagent", "haiku", "qa"),
           _audit("proxy", "very_cheap", "qa")]       # so case 7 (Sonnet) is decided by model class alone


def _case_sid(n: int) -> str:
    return f"case-{n:02d}"


def _fixture():
    """The 15 cases of PLAN P1.9 task 4. Returns (proxy_rows, mcp_rows, techops)."""
    c = _case_sid
    proxy, mcp, tech = [], [], []
    # 1  Haiku turn-first, not redone
    proxy.append(proxy_row(1, sid=c(1), ts=T, tier="haiku", kind="organic", msg_id="m1", **{"tier_task_type": "query"}))
    # 2  Haiku turn-first, then a `claude:` re-ask (an escalation row in the next turn)
    proxy.append(proxy_row(2, sid=c(2), ts=T, tier="haiku", kind="organic", msg_id="m2", **{"tier_task_type": "query"}))
    proxy.append(proxy_row(3, sid=c(2), ts=T + 20, tier="opus", kind="organic", msg_id="m2b", reason="escalation"))
    # 3  local MCP llm() code call
    mcp.append(_mcp(c(3), model="ollama/qwen3-coder:30b", provider="ollama"))
    # 4  Ollama continuation step served by the proxy's local mode
    proxy.append(proxy_row(4, sid=c(4), ts=T, tier="sonnet", kind="organic", msg_id="m4a", **{"tier_task_type": "query"}))
    proxy.append(proxy_row(5, sid=c(4), ts=T + 5, kind="organic", msg_id="m4", step="continuation",
                           decision="served", model="ollama/qwen3.5:latest", served_by="local",
                           **{"tier_task_type": "query"}))
    # 5  Codex gpt-5.5 sub-agent: another Frontier subscription
    mcp.append(_mcp(c(5), model="gpt-5.5", provider="codex"))
    # 6  Gemini Flash call (very cheap by price)
    mcp.append(_mcp(c(6), model="gemini/gemini-2.5-flash", provider="gemini", task_type="query"))
    # 7  Sonnet continuation
    proxy.append(proxy_row(6, sid=c(7), ts=T, tier="sonnet", kind="organic", msg_id="m7", step="continuation"))
    # 8  technical op served by a local model, not re-read
    tech.append(_techop(c(8)))
    # 9  technical op handled by the deterministic filter
    tech.append(_techop(c(9), served_by="local_filter"))
    # 10 technical op served locally, then re-read
    tech.append(_techop(c(10), re_read=True))
    # 11 technical op below the 4 KB threshold
    tech.append(_techop(c(11), bytes_in=1024))
    # 12 DIRECT local draft
    mcp.append(_mcp(c(12), model="ollama/qwen3.8:latest", provider="ollama", reason_code="direct"))
    # 13 sub-agent served on Haiku
    proxy.append(proxy_row(7, sid=c(13), ts=T, tier="haiku", kind="organic", msg_id="m13", step="subagent_first",
                           **{"tier_task_type": "query"}))
    # 14 executor (research) session row
    proxy.append(proxy_row(8, sid=c(14), ts=T, tier="haiku", kind="research", msg_id="m14",
                           **{"tier_task_type": "query"}))
    # 15 local code call, lane (proxy, local, code) not quality-sampled
    proxy.append(proxy_row(9, sid=c(15), ts=T, kind="organic", msg_id="m15", decision="served",
                           model="ollama/qwen3-coder:30b", served_by="local", **{"tier_task_type": "code"}))
    return proxy, mcp, tech


def _kind_of(sid, stamp):
    return "research" if sid == _case_sid(14) else "organic"


@pytest.fixture()
def res():
    proxy, mcp, tech = _fixture()
    return osh.action_units(SINCE, UNTIL, proxy_rows=proxy, mcp_rows=mcp, techops=tech, edit_rows=[],
                            kind_of=_kind_of, quality_rows=QUALITY, outcome_redos=[])


def _of(res, n):
    return [a for a in res["actions"] if a["session_id"] == _case_sid(n)]


# (case, in denominator, in numerator, spread)
CASES = [
    (1, True, True, False),
    (2, True, False, False),
    (3, True, True, False),
    (4, True, True, False),
    (5, True, False, True),
    (6, True, True, False),
    (7, True, False, False),
    (8, True, True, False),
    (9, False, False, False),
    (10, True, False, False),
    (11, False, False, False),
    (12, True, True, False),
    (13, True, True, False),
    (14, False, False, False),
    (15, True, False, False),
]


@pytest.mark.parametrize("n,in_den,in_num,spread", CASES, ids=[f"case{c[0]:02d}" for c in CASES])
def test_fixture_case_membership(res, n, in_den, in_num, spread):
    acts = _of(res, n)
    if n == 2:                                   # the re-ask is its own (claude) action in the same session
        acts = [a for a in acts if a["served_class"] == "haiku"]
    assert bool(acts) is in_den, f"case {n}: denominator membership"
    assert any(a["numerator"] for a in acts) is in_num, f"case {n}: numerator membership"
    assert any(a["spread"] for a in acts) is spread, f"case {n}: spread membership"
    if in_num:                                   # every numerator case is in a quality-sampled lane
        assert all(a["sampled"] for a in acts if a["numerator"])


def test_fixture_is_15_cases_and_the_totals_follow(res):
    assert len(CASES) == 15 and len({c[0] for c in CASES}) == 15
    s = osh.summarize_actions(res["actions"])
    in_den = [c for c in CASES if c[1]]
    # two helper rows are extra claude actions in the denominator: case 2's re-ask and case 4's turn-first
    assert s["action"]["n"] == len(in_den) + 2
    assert s["action"]["k"] == sum(1 for c in CASES if c[2]) == 7
    assert s["spread"]["k"] == 1 and s["spread"]["n"] == s["action"]["n"]
    ex = res["excluded"]
    assert ex["techop_deterministic"] == 1 and ex["techop_below_4kb"] == 1 and ex["other_kind"] >= 1


def test_orphan_proxy_rows_are_flagged_and_counted_in_a_caveat(_scorecard_env, monkeypatch):
    from llm_router import o3_transcripts

    proxy, mcp, _ = _fixture()
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), proxy)
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None, **kw: iter(()))
    monkeypatch.setattr(o3_transcripts, "thread_lookup", lambda **kw: (lambda sid, mid: "orphan"))
    off = kpi.compute_scorecard(since=SINCE, until=UNTIL)["offload"]
    assert any("in no message of their session's transcript" in c for c in off["caveats"])


def test_case_2_haiku_row_is_redone_by_escalation(res):
    haiku = [a for a in _of(res, 2) if a["served_class"] == "haiku"][0]
    assert haiku["redone"] is True and haiku["numerator"] is False


def test_case_10_is_not_redone_but_reread(res):
    a = _of(res, 10)[0]
    assert a["redone"] is False and a["reread"] is True and a["numerator"] is False


def test_case_15_would_count_if_its_lane_were_sampled():
    proxy, mcp, tech = _fixture()
    q = QUALITY + [_audit("proxy", "local", "code")]
    r = osh.action_units(SINCE, UNTIL, proxy_rows=proxy, mcp_rows=mcp, techops=tech, edit_rows=[],
                         kind_of=_kind_of, quality_rows=q, outcome_redos=[])
    assert [a["numerator"] for a in _of(r, 15)] == [True]


def test_no_quality_rows_means_numerator_zero_and_denominator_unchanged():
    proxy, mcp, tech = _fixture()
    r = osh.action_units(SINCE, UNTIL, proxy_rows=proxy, mcp_rows=mcp, techops=tech, edit_rows=[],
                         kind_of=_kind_of, quality_rows=[], outcome_redos=[])
    s = osh.summarize_actions(r["actions"])
    assert s["action"]["k"] == 0 and s["action"]["n"] == 14 and r["sampled"] == {}


# ── the lane rule (PLAN section 1.1) ───────────────────────────────────────────────────────────────────

LANE = ("proxy", "haiku", "qa")


@pytest.mark.parametrize("row,expect", [
    (dict(kind="audit", n=30, acceptable=26), True),
    (dict(kind="audit", n=30, acceptable=25), False),
    (dict(kind="audit", n=29, acceptable=29), False),         # n < 30
    (dict(kind="audit", n=60, acceptable=52), True),          # 52/60 = 0.8667 = 26/30
    (dict(kind="audit", n=60, acceptable=51), False),
    (dict(kind="shadow_pairs", n=60, gap_pts=3.0, newcombe_lb=-10.0), True),
    (dict(kind="shadow_pairs", n=59, gap_pts=0.0, newcombe_lb=0.0), False),
    (dict(kind="shadow_pairs", n=80, gap_pts=3.1, newcombe_lb=-5.0), False),
    (dict(kind="shadow_pairs", n=80, gap_pts=1.0, newcombe_lb=-10.1), False),
    (dict(kind="audit", n=30), None),                          # unreadable: neither pass nor fail
    (dict(kind="nope", n=30), None),
])
def test_batch_passed(row, expect):
    assert lane_quality.batch_passed(row) is expect


def _lane_row(ts, ok, **kw):
    return {"ts": ts, "door": LANE[0], "served_class": LANE[1], "task_group": LANE[2], "kind": "audit",
            "n": 30, "acceptable": ok, **kw}


def test_latest_batch_in_the_trailing_28_days_decides():
    now = NOW
    day = 86400.0
    assert LANE in lane_quality.sampled_lanes([_lane_row(now - 3 * day, 28)], now)
    assert lane_quality.sampled_lanes([_lane_row(now - 29 * day, 30)], now) == {}          # too old
    assert lane_quality.sampled_lanes([_lane_row(now + 60, 30)], now) == {}                 # not yet
    # an older pass is undone by a newer failing batch, and the reverse
    assert lane_quality.sampled_lanes([_lane_row(now - 10 * day, 30), _lane_row(now - day, 20)], now) == {}
    assert LANE in lane_quality.sampled_lanes([_lane_row(now - 10 * day, 20), _lane_row(now - day, 30)], now)
    # other lanes are independent
    other = {**_lane_row(now - day, 30), "door": "mcp"}
    assert set(lane_quality.sampled_lanes([other], now)) == {("mcp", "haiku", "qa")}


def test_lane_quality_file_is_read_defensively(tmp_path):
    p = tmp_path / "lane_quality.jsonl"
    assert lane_quality.read_rows(p) == []
    p.write_text("not json\n" + json.dumps(_lane_row(NOW - 5, 30)) + "\n[1]\n")
    assert len(lane_quality.read_rows(p)) == 1


# ── pricing.is_very_cheap ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,cheap", [
    ("ollama/qwen3:8b", True), ("cache/ollama/qwen3-coder:30b", True), ("claude-haiku-4-5-20251001", True),
    ("claude-haiku-5-5", True), ("gemini/gemini-2.5-flash", True), ("gpt-4o-mini", True),
    ("claude-sonnet-5-5", False), ("claude-opus-5-5", False), ("gpt-5.5", False), ("gpt-4o", False),
    ("gemini-2.5-pro", False), ("a-model-nobody-priced", False), ("", False),
])
def test_is_very_cheap(model, cheap):
    assert pricing.is_very_cheap(model) is cheap


def test_very_cheap_reference_is_read_from_the_table_not_written_down():
    haiku = [p.input for k, p in pricing._PRICES.items() if "haiku" in k]
    assert pricing.haiku_input_rate() == max(haiku) and len(haiku) >= 2


# ── served class ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,provider,local,want", [
    ("ollama/x:7b", "ollama", False, "local"), ("claude-sonnet-5-5", None, True, "local"),
    ("claude-haiku-5-5", None, False, "haiku"), ("gemini/gemini-2.5-flash", "gemini", False, "very_cheap"),
    ("gpt-5.5", "codex", False, "spread"), ("codex/gpt-5.5", None, False, "spread"),
    ("gpt-6-astra", "codex", False, "spread"), ("gpt-5.1-codex-mini", None, False, "spread"),
    ("gpt-5-mini", None, False, "very_cheap"), ("gpt-5.4-mini", None, False, "paid"), ("gemini-2.5-pro", "gemini", False, "spread"),
    ("claude-sonnet-5-5", None, False, "paid"), ("claude-opus-5-5", None, False, "paid"),
])
def test_served_class(model, provider, local, want):
    assert osh.served_class(model, provider=provider, local=local) == want


# ── the typed-prompt and turn repairs (task 1) ─────────────────────────────────────────────────────────

def test_task_notification_and_peer_message_are_not_typed_prompts():
    from llm_router import o3_transcripts as t

    def entry(**kw):
        return {"type": "user", "message": {"role": "user", "content": "do the thing"}, **kw}

    assert t.is_typed_prompt(entry(promptSource="typed", origin={"kind": "human"}))
    assert t.is_typed_prompt(entry(promptSource="suggestion_accepted", origin={"kind": "human"}))
    assert t.is_typed_prompt(entry())                                   # older layout: no source, no origin
    assert not t.is_typed_prompt(entry(promptSource="system", origin={"kind": "task-notification"}))
    assert not t.is_typed_prompt(entry(promptSource="system", isMeta=True, origin={"kind": "peer"}))
    assert not t.is_typed_prompt(entry(origin={"kind": "task-notification"}))


def test_interrupt_marker_and_compaction_summary_are_not_typed_prompts():
    """Found by the synthetic P1.9-a run (D-99): each one added a "prompt" with no turn."""
    from llm_router import o3_transcripts as t

    def entry(content, **kw):
        return {"type": "user", "message": {"role": "user", "content": content}, **kw}

    assert not t.is_typed_prompt(entry([{"type": "text", "text": "[Request interrupted by user]"}]))
    assert not t.is_typed_prompt(entry("[Request interrupted by user for tool use]"))
    assert not t.is_typed_prompt(entry("This session is being continued from a previous conversation.",
                                       isCompactSummary=True, isVisibleInTranscriptOnly=True))
    assert t.is_typed_prompt(entry("why was the request interrupted?"))
    assert t.is_typed_prompt(entry("see the [Request interrupted by user] line above"))  # anchored


def test_an_answer_after_a_compaction_summary_or_interrupt_with_no_prompt_is_not_a_turn(tmp_path):
    from llm_router import o3_transcripts as t

    def line(d):
        return json.dumps(d) + "\n"

    user = lambda content, **kw: line({"type": "user", "message": {"role": "user", "content": content}, **kw})  # noqa: E731
    asst = lambda mid: line({"type": "assistant", "message": {"id": mid, "role": "assistant", "content": []}})  # noqa: E731
    proj = tmp_path / "-p"
    proj.mkdir()
    (proj / "sess.jsonl").write_text(
        user("first real prompt") + asst("m1")
        + user("This session is being continued.", isCompactSummary=True) + asst("m2")
        + user([{"type": "text", "text": "[Request interrupted by user]"}]) + asst("m3")
        + user("second real prompt") + user("This session is being continued.", isCompactSummary=True)
        + asst("m4"))
    roles = t.thread_index("sess", projects_dir=tmp_path)
    assert roles == {"m1": t.ROLE_TURN, "m2": t.ROLE_META, "m3": t.ROLE_META,
                     "m4": t.ROLE_TURN}  # a summary never demotes the prompt it follows


def test_transcript_role_of_a_task_notification_answer_is_meta(tmp_path):
    from llm_router import o3_transcripts as t

    def line(d):
        return json.dumps(d) + "\n"

    user = lambda text, **kw: line({"type": "user", "message": {"role": "user", "content": text}, **kw})  # noqa: E731
    asst = lambda mid: line({"type": "assistant", "message": {"id": mid, "role": "assistant", "content": []}})  # noqa: E731
    proj = tmp_path / "-p"
    proj.mkdir()
    (proj / "sess.jsonl").write_text(
        user("ask", promptSource="typed", origin={"kind": "human"}) + asst("a1")
        + user("<task-notification>x</task-notification>", promptSource="system",
               origin={"kind": "task-notification"}) + asst("a2"))
    idx = t.thread_index("sess", projects_dir=tmp_path)
    assert idx == {"a1": "turn", "a2": "meta"}


# ── the kpi block ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def _scorecard_env(monkeypatch, tmp_path):
    proj = tmp_path / "claude-projects" / "-Users-x-app"
    proj.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(proj.parent))
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    session_kind._FOUND.clear()
    for n in range(1, 16):     # case 14 is the executor (research) session: its cwd has a scratchpad part
        session_kind.tag_session(_case_sid(n), "/Users/someone/scratchpad/x" if n == 14
                                 else "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


def test_scorecard_emits_offload_with_k_n_wilson_sessions_and_the_o3_warning(_scorecard_env, monkeypatch):
    proxy, mcp, tech = _fixture()
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), proxy)
    fx.write_jsonl(paths.state_path("technical_ops.jsonl"), tech)
    fx.write_jsonl(paths.state_path("lane_quality.jsonl"), QUALITY)
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None, **kw: iter(
        mcp if kw.get("all_providers") else [u for u in mcp if u["provider"] == "ollama"]))
    data = kpi.compute_scorecard(since=SINCE, until=UNTIL)
    off = data["offload"]
    keys = {"k", "n", "value", "wilson95", "n_sessions", "largest_session_share", "window"}
    for name in ("action", "call", "turn", "spread"):
        assert keys <= set(off[name]), name
        assert off[name]["window"] == {"since": kpi._iso(SINCE), "until": kpi._iso(UNTIL)}
    assert {"by_lane", "by_task_type", "caveats"} <= set(off)
    # the proxy-door rows: 1, 2 (+ its re-ask), 4 (2 rows), 7, 13, 15 -> the research row (14) is out
    assert off["call"]["n"] == 8
    assert off["action"]["n"] == 14 and off["action"]["k"] == 7 and off["spread"]["k"] == 1
    lo, hi = off["action"]["wilson95"]
    assert 0.0 < lo < off["action"]["value"] < hi < 1.0
    # turn reuses O3: same numerator, same denominator, and its integrity warning
    o3 = data["o3"]["breakdown"]
    assert (off["turn"]["k"], off["turn"]["n"]) == (o3["offload_kept"], o3["n"])
    assert off["turn"]["caveats"] == [data["o3"]["caveat"], kpi.P19A_POPULATION_NOTE] and "D-58" in off["turn"]["caveats"][1] and "unvalidated" in off["turn"]["caveats"][0]
    assert {r["task_group"] for r in off["by_lane"]} == {"qa", "code", "tech_op"}
    assert set(off["by_task_type"]) == {"qa", "code", "tech_op"}
    assert not any("no quality-sampled lane" in c for c in off["caveats"])
    assert len(off["quality_sampled_lanes"]) == len(QUALITY)


def test_scorecard_without_lane_quality_says_so_and_counts_zero(_scorecard_env, monkeypatch):
    proxy, mcp, _ = _fixture()
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), proxy)
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None, **kw: iter(mcp))
    off = kpi.compute_scorecard(since=SINCE, until=UNTIL)["offload"]
    assert off["action"]["k"] == 0 and off["action"]["n"] > 0
    assert any("no quality-sampled lane" in c for c in off["caveats"])
    assert any("technical_ops.jsonl" in c for c in off["caveats"])


def test_scorecard_offload_renders_and_survives_an_empty_ledger(_scorecard_env, monkeypatch):
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None, **kw: iter(()))
    data = kpi.compute_scorecard(since=SINCE, until=UNTIL)
    assert data["offload"]["action"]["n"] == 0 and data["offload"]["action"]["value"] is None
    text = "\n".join(kpi._offload_render_lines(data["offload"]))
    assert "Offload share, per action (S1)" in text and "not measurable: n=0" in text
    assert "WARNING: O3 turn count unvalidated" in text


def test_offload_failure_is_reported_not_raised(_scorecard_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(osh, "action_units", boom)
    off = kpi.compute_scorecard(since=SINCE, until=UNTIL)["offload"]
    assert off["measurable"] is False and "RuntimeError" in off["reason"]
    assert "not measurable" in kpi._offload_render_lines(off)[0]


def test_s1_report_prints_the_age_of_each_final_lane_a_row_and_reports_an_invalidated_one(_scorecard_env, monkeypatch):
    """D-302: a final shadow-pair row is exempt from the 28-day read cap; S1 prints its age in days, and a row whose
    stamped identities no longer match is reported as invalidated, not counted."""
    from llm_router import lane_a_shadow

    ident = lane_a_shadow.current_identities()
    assert ident is not None
    final = {"ts": UNTIL - 40 * 86400.0, "door": "proxy", "served_class": "haiku", "task_group": "tech_op",
             "kind": "shadow_pairs", "n": 64, "gap_pts": 1.0, "newcombe_lb": -5.0, "source": "fixture",
             "window_complete": True, "informative": True, "identity": ident}
    stale = dict(final, task_group="qa", identity={**ident, "breaker_sha": "old"}, ts=UNTIL - 50 * 86400.0)
    fx.write_jsonl(paths.state_path("lane_quality.jsonl"), [final, stale])
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None, **kw: iter(()))
    off = kpi.compute_scorecard(since=SINCE, until=UNTIL)["offload"]
    (live,) = off["quality_sampled_lanes"]
    assert (live["task_group"], live["age_days"]) == ("tech_op", 40.0)
    (dead,) = off["invalidated_lanes"]
    assert (dead["task_group"], dead["mismatch"], dead["age_days"]) == ("qa", ["breaker_sha"], 50.0)
    text = "\n".join(kpi._offload_render_lines(off))
    assert "final window proxy/haiku/tech_op: n=64, age 40.0 d" in text
    assert "INVALIDATED window proxy/haiku/qa: age 50.0 d, changed breaker_sha" in text
    assert any("INVALIDATED" in c and "prereg amendment" in c for c in off["caveats"])
