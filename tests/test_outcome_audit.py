"""Phase 0.1 — the RELEASE-TIME outcome audit (`scripts/release/outcome_audit.py`).

The audit labels every routed unit in the owner's organic sessions as
used / corrected / redone / unused / unknown. Its one hard rule: no `used` or
`corrected` label without a CONTENT-BEARING terminal event. A dispatch flag, a
PreToolUse door-call, or a flat per-turn credit never counts.

The two cases the 2026-09-29 audit got wrong are pinned here as fixtures:

* `agent_route_codex` "used" at dispatch time (northstar.py's
  `_CODEX_ATTEMPT_OUTCOMES["delegated"]`). On this machine every one of those
  PreToolUse blocks was ignored and Claude's own sub-agent ran the task.
* `direct` units that are really drafts: northstar tags a DIRECT SUCCESS as
  `direct` when the invocation's log contains any "ZERO_CLAUDE" substring, and
  "ZERO_CLAUDE_EDIT: not edit-class" is the zero-Claude edit path DECLINING. The
  draft then went to Claude as advisory context, and Claude answered itself.

Loaded by path: the audit lives under scripts/, never under src/ (see
tests/test_outcome_audit_import_boundary.py).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import sqlite3
from datetime import datetime, timezone

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str = "_outcome_audit_under_test"):
    path = REPO / "scripts" / "release" / "outcome_audit.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


A = _load()

LONG_ANSWER = (
    "The foreign key constraint makes sure every value in the child column "
    "already exists in the parent table so orphaned rows cannot be inserted "
    "and cascading deletes remove dependent rows when the parent is deleted"
)
UNRELATED = (
    "I checked the repository and the migration in slice nine already adds "
    "the licence file so nothing else is needed before the next commit lands"
)


# ── Wilson interval ──────────────────────────────────────────────────────────

def test_wilson_matches_the_audit_figure():
    # routing.md: 9/1081 -> Wilson 95% CI [0.44%, 1.58%] (quoted to 2 dp; the
    # exact upper bound is 1.5747%, so compare within a rounding step)
    lo, hi = A.wilson(9, 1081)
    assert abs(lo * 100 - 0.44) < 0.01
    assert abs(hi * 100 - 1.58) < 0.01


def test_wilson_zero_n_is_none_not_zero():
    assert A.wilson(0, 0) is None


# ── drafts / direct ──────────────────────────────────────────────────────────

def test_draft_relayed_with_marker_is_used():
    host = "🎯 LLM Router routed → ollama/qwen3 · query/simple\n\n" + LONG_ANSWER
    assert A.label_draft(LONG_ANSWER, host, None, None)[0] == "used"


def test_draft_copied_verbatim_without_marker_is_used():
    assert A.label_draft(LONG_ANSWER, "Sure. " + LONG_ANSWER, None, None)[0] == "used"


def test_draft_relayed_and_rewritten_is_corrected():
    half = " ".join(LONG_ANSWER.split()[:20])
    host = ("🎯 LLM Router routed → x\n\n" + half
            + " but that is only true when the constraint is enforced by the engine itself here")
    label, signal, _ = A.label_draft(LONG_ANSWER, host, None, None)
    assert label == "corrected", signal


def test_unmarked_partial_overlap_is_ambiguous_not_corrected():
    """Hand check 2026-09-29: the one unmarked 40%-overlap draft was the host
    restating URLs both had seen, not a relay."""
    half = " ".join(LONG_ANSWER.split()[:20])
    host = half + " but that is only true when the constraint is enforced by the engine itself here"
    label, signal, _ = A.label_draft(LONG_ANSWER, host, None, None)
    assert (label, signal) == ("unknown", "draft_partial_overlap_ambiguous")


def test_mcp_partial_overlap_is_ambiguous_not_corrected():
    """Hand check 2026-09-29: a 25% overlap was the host quoting the result to
    critique it."""
    half = " ".join(LONG_ANSWER.split()[:20])
    label, signal, _ = A.label_routed_mcp(LONG_ANSWER, False, "It said: " + half + " which is wrong", True, None)
    assert (label, signal) == ("unknown", "tool_result_ambiguous_overlap")


def test_mcp_result_written_into_a_file_counts_as_host_output(tmp_path):
    """Routed content the host writes with Write/Edit is used content, even
    when the host says nothing in prose."""
    s = A.Session("10ea9ab2-8cdc-4058-b9f8-3a6543727978", [
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w", "name": "Write",
                                                        "input": {"file_path": "/a", "content": LONG_ANSWER}}]}},
    ], {})
    host, uses, _ = s.reply_after(0)
    assert A.label_routed_mcp(LONG_ANSWER, False, host, True, None)[0] == "used"


def test_draft_the_host_ignored_is_unused_not_used():
    """The 2026-09-29 finding: 8 of 9 'used' direct units were drafts the
    host never relayed. Claude answering on its own is `unused`."""
    label, signal, _ = A.label_draft(LONG_ANSWER, UNRELATED, "next thing please do it", None)
    assert label == "unused"
    assert signal == "draft_not_relayed"


def test_draft_with_no_host_reply_is_unknown():
    assert A.label_draft(LONG_ANSWER, None, None, None)[0] == "unknown"


def test_draft_relayed_then_reasked_is_redone():
    host = "🎯 LLM Router routed → x\n\n" + LONG_ANSWER
    assert A.label_draft(LONG_ANSWER, host, "claude: answer it properly", None)[0] == "redone"


def test_draft_hook_verdict_unused_wins_over_missing_text():
    assert A.label_draft(None, None, None, "unused")[0] == "unused"


def test_draft_without_text_or_verdict_is_unknown():
    assert A.label_draft(None, UNRELATED, None, None)[0] == "unknown"


# ── routed_mcp ───────────────────────────────────────────────────────────────

def test_mcp_error_result_is_unused():
    assert A.label_routed_mcp("boom", True, LONG_ANSWER, True, None)[0] == "unused"


def test_mcp_result_reused_is_used():
    assert A.label_routed_mcp(LONG_ANSWER, False, "Here: " + LONG_ANSWER, True, None)[0] == "used"


def test_mcp_result_ignored_is_unused():
    """The door-call shape: llm() called only to release a hold, answer ignored."""
    label, signal, _ = A.label_routed_mcp(LONG_ANSWER, False, UNRELATED, True, None)
    assert label == "unused"
    assert signal == "tool_result_not_reused"


def test_mcp_missing_result_is_unknown():
    assert A.label_routed_mcp(None, False, UNRELATED, True, None)[0] == "unknown"


def test_mcp_followed_by_reask_is_redone():
    assert A.label_routed_mcp(LONG_ANSWER, False, UNRELATED, True, "claude: do it yourself")[0] == "redone"


# ── agent_route_codex ────────────────────────────────────────────────────────

def test_codex_delegated_but_claude_subagent_ran_is_redone():
    """What actually happened to all delegated rows on this machine: the
    PreToolUse block was ignored and Claude's own agent launched."""
    label, signal, _ = A.label_codex(
        "delegated", "Async agent launched successfully. agentId: abc", UNRELATED, False
    )
    assert label == "redone"
    assert signal == "claude_subagent_ran"


def test_codex_dispatch_alone_is_never_used():
    label, _, _ = A.label_codex("delegated", None, None, False)
    assert label == "unknown"


def test_codex_failed_is_unused():
    assert A.label_codex("codex_failed", None, None, False)[0] == "unused"


def test_codex_output_incorporated_is_used():
    result = "[llm_router] Subagent task was delegated to Codex CLI. Output:\n" + LONG_ANSWER
    label, signal, _ = A.label_codex("delegated", result, "Codex says: " + LONG_ANSWER, False)
    assert label == "used"
    assert signal == "codex_output_incorporated"


def test_codex_output_then_respawn_is_redone():
    result = "[llm_router] Subagent task was delegated to Codex CLI. Output:\n" + LONG_ANSWER
    assert A.label_codex("delegated", result, LONG_ANSWER, True)[0] == "redone"


# ── routed_edit ──────────────────────────────────────────────────────────────

def test_edit_applied_verbatim_is_used():
    new = "def f():\n    return 42\n"
    assert A.label_edit(True, [new], [new], True, True)[0] == "used"


def test_edit_applied_with_changes_is_corrected():
    new = "def compute_total(items):\n    return sum(item.price for item in items if item.active)\n"
    tweaked = new.replace("item.active", "item.is_active")
    assert A.label_edit(True, [new], [tweaked], True, True)[0] == "corrected"


def test_edit_replaced_by_different_edit_is_redone():
    assert A.label_edit(True, ["return 42 from the helper function body"],
                        ["completely unrelated rewrite of another thing"], True, True)[0] == "redone"


def test_edit_never_applied_by_host_is_unused():
    assert A.label_edit(True, ["x = 1"], [], True, True)[0] == "unused"


def test_edit_not_validated_is_unused():
    assert A.label_edit(False, [], [], True, False)[0] == "unused"


def test_edit_without_result_text_is_unknown_even_if_git_says_survived():
    """git survival alone is not content-bearing: an untouched file does not
    prove the edit was ever applied."""
    assert A.label_edit(True, [], [], True, False)[0] == "unknown"


# ── delegate / bounded_operational ───────────────────────────────────────────

def test_delegate_success_without_host_evidence_is_unknown():
    assert A.label_delegate({"route_outcome": "success", "route_succeeded": True})[0] == "unknown"


def test_delegate_failed_is_unused():
    assert A.label_delegate({"route_outcome": "failed", "route_succeeded": False})[0] == "unused"


# ── organic scope ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("meta,expected", [
    ({"entrypoint": "cli", "cwd": "/Users/u/Projects/app", "project_dir": "-Users-u-Projects-app",
      "session_id": "10ea9ab2-8cdc-4058-b9f8-3a6543727978"}, True),
    ({"entrypoint": "sdk-cli", "cwd": "/Users/u/Projects/app", "project_dir": "-Users-u-Projects-app",
      "session_id": "10ea9ab2-8cdc-4058-b9f8-3a6543727978"}, False),
    ({"entrypoint": "cli", "cwd": "/private/tmp/bq_x", "project_dir": "-private-tmp-bq-x",
      "session_id": "10ea9ab2-8cdc-4058-b9f8-3a6543727978"}, False),
    ({"entrypoint": "cli", "cwd": "/Users/u/Projects/app/.claude/worktrees/a",
      "project_dir": "-Users-u-Projects-app--claude-worktrees-a",
      "session_id": "10ea9ab2-8cdc-4058-b9f8-3a6543727978"}, False),
    ({"entrypoint": "cli", "cwd": "/Users/u/Projects/app", "project_dir": "-Users-u-Projects-app",
      "session_id": "modetest"}, False),
    ({"entrypoint": "cli", "cwd": "/Users/u/Projects/llm-router-wt-p01",
      "project_dir": "-Users-u-Projects-llm-router-wt-p01",
      "session_id": "10ea9ab2-8cdc-4058-b9f8-3a6543727978"}, False),
])
def test_organic_scope(meta, expected):
    assert A.is_organic(meta) is expected


# ── end to end on a synthetic corpus ─────────────────────────────────────────

SID = "10ea9ab2-8cdc-4058-b9f8-3a6543727978"
SUB = "20fb0bb3-0000-4000-8000-000000000001"
PROMPT_TEXT = "please explain what a foreign key is in postgres"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _rec(ts, typ, content=None, **extra):
    r = {"type": typ, "timestamp": _iso(ts), "sessionId": SID, "entrypoint": "cli",
         "cwd": "/Users/u/Projects/app"}
    if content is not None:
        r["message"] = {"role": "user" if typ == "user" else "assistant", "content": content}
    r.update(extra)
    return r


def _draft_attachment(ts, body):
    text = ("ROUTING NOTICE — this prompt was classified as query/simple.\n"
            "───── UNVERIFIED DRAFT (no context — verify or discard) ─────\n"
            f"{body}\n───── END UNVERIFIED DRAFT ─────")
    r = _rec(ts, "attachment")
    r["attachment"] = {"type": "hook_additional_context", "content": [text]}
    return r


@pytest.fixture
def corpus(tmp_path):
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp()
    projects = tmp_path / "projects"
    state = tmp_path / "state"
    state.mkdir()
    proj = projects / "-Users-u-Projects-app"
    proj.mkdir(parents=True)
    t0 = now - 3600
    t1 = now - 1800
    recs = [
        _rec(t0, "user", PROMPT_TEXT),
        _draft_attachment(t0 + 7, LONG_ANSWER),
        _rec(t0 + 10, "assistant", [{"type": "text", "text": UNRELATED}]),
        _rec(t1, "user", "now spawn the reviewer agent for this change"),
        _rec(t1 + 1, "assistant", [{"type": "tool_use", "id": "tu_agent", "name": "Agent",
                                    "input": {"prompt": "review it", "subagent_type": "general-purpose"}}]),
        _rec(t1 + 2, "user", [{"type": "tool_result", "tool_use_id": "tu_agent",
                               "content": [{"type": "text", "text": "Async agent launched successfully."}]}]),
        _rec(t1 + 5, "assistant", [{"type": "tool_use", "id": "tu_llm", "name": "mcp__llm_router__llm",
                                    "input": {"task": "query", "prompt": "x"}}]),
        _rec(t1 + 6, "user", [{"type": "tool_result", "tool_use_id": "tu_llm",
                               "content": [{"type": "text", "text": LONG_ANSWER}]}]),
        _rec(t1 + 8, "assistant", [{"type": "text", "text": UNRELATED}]),
    ]
    (proj / f"{SID}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    # A sub-agent session (sdk-cli) and a stale unit OUTSIDE the window in an
    # organic file whose mtime is fresh: neither may appear.
    sub = [dict(_rec(t0, "user", PROMPT_TEXT), entrypoint="sdk-cli", sessionId=SUB),
           dict(_rec(t0 + 3, "assistant", [{"type": "tool_use", "id": "s1", "name": "mcp__llm_router__llm",
                                             "input": {"task": "query"}}]), entrypoint="sdk-cli", sessionId=SUB)]
    (proj / f"{SUB}.jsonl").write_text("\n".join(json.dumps(r) for r in sub) + "\n")

    day = datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    log = [
        f"[{day}] [INVOCATION {t0:.3f}] prompt_len=48 session_id={SID[:8]}",
        f"[{day}] [INVOCATION {t0:.3f}] ZERO_CLAUDE_EDIT: not edit-class — no imperative edit verb found",
        f"[{day}] [INVOCATION {t0:.3f}] DIRECT SUCCESS: model=ollama/qwen3-coder:30b latency=6855ms files_read=0",
    ]
    (state / "auto-route-debug.log").write_text("\n".join(log) + "\n")
    (state / "north_star_units.jsonl").write_text(json.dumps({
        "ts": t1 + 1.5, "session_id": SID, "lever": "agent_route_codex", "outcome": "delegated",
        "task_type": "code", "model": "gpt-5.5", "subagent_type": "general-purpose"}) + "\n")
    db = sqlite3.connect(state / "usage.db")
    db.execute("CREATE TABLE execution_events (ts REAL, session_id TEXT, event_type TEXT, "
               "realization_status TEXT, adoption_method TEXT)")
    db.execute("INSERT INTO execution_events VALUES (?,?,?,?,?)",
               (t1 + 5.5, SID, "route_realized", "verified_used", "door_call"))
    db.execute("CREATE TABLE savings_stats (timestamp TEXT, session_id TEXT, model_used TEXT, "
               "estimated_claude_cost_saved REAL)")
    db.execute("INSERT INTO savings_stats VALUES (?,?,?,?)",
               (_iso(t1), SID, "llm_router-agentic-router", 0.2))
    db.commit()
    db.close()
    return projects, state, now


def test_end_to_end_reproduces_zero_truly_used(corpus):
    projects, state, now = corpus
    rows, summary = A.run_audit(days=30, projects_dir=projects, state_dir=state, now=now)
    by_kind = {r["kind"]: r for r in rows}
    # northstar calls this `direct` (ZERO_CLAUDE substring); the host ignored it.
    assert by_kind["direct"]["label"] == "unused"
    assert by_kind["agent_route_codex"]["label"] == "redone"
    assert by_kind["routed_mcp"]["label"] == "unused"
    assert summary["headline"]["used"] == 0
    assert summary["headline"]["attempted"] == 3
    assert summary["headline"]["too_few_to_tell"] is True
    # the sdk-cli sub-agent session is out of scope
    assert summary["scope"]["n_sessions"] == 1
    # inflated sources are counted but never credited
    ex = summary["excluded_inflated_sources"]
    assert ex["door_call_verified_used"]["n"] == 1
    assert ex["agentic_flat_credits"]["n"] == 1
    assert ex["codex_dispatch_flags"]["n"] == 1
    assert all(v["counted_as_used"] == 0 for v in ex.values())


def test_rows_have_exactly_the_six_keys_and_no_prompt_text(corpus, tmp_path):
    projects, state, now = corpus
    rows, summary = A.run_audit(days=30, projects_dir=projects, state_dir=state, now=now)
    assert rows
    for r in rows:
        assert tuple(r) == A.ROW_KEYS
        assert r["human_reviewed"] is False
    out = tmp_path / "out"
    jsonl, summ = A.write_artifacts(rows, summary, out, "9.9.9")
    assert jsonl.name == "outcome_audit_9.9.9.jsonl"
    blob = jsonl.read_text() + summ.read_text()
    for secret in (PROMPT_TEXT, LONG_ANSWER[:40], UNRELATED[:40]):
        assert secret not in blob


def test_window_is_by_unit_timestamp_not_file_mtime(corpus):
    projects, state, now = corpus
    # 1-day window from a "now" 2 days later: the file is fresh on disk, but
    # every unit is older than the window.
    rows, summary = A.run_audit(days=1, projects_dir=projects, state_dir=state, now=now + 2 * 86400)
    assert rows == []
    assert summary["headline"]["attempted"] == 0


def test_default_artifact_dir_is_not_under_llm_router_home():
    d = A.default_artifact_dir()
    assert ".llm-router" not in str(d)
    assert str(d).startswith(str(REPO))


# ── human labels and the optional judge ─────────────────────────────────────

def test_human_labels_override_and_are_marked_reviewed(corpus, tmp_path):
    projects, state, now = corpus
    rows, _ = A.run_audit(days=30, projects_dir=projects, state_dir=state, now=now)
    target = next(r for r in rows if r["kind"] == "routed_mcp")
    hl = tmp_path / "hand.jsonl"
    hl.write_text(json.dumps({"unit_id": target["unit_id"], "label": "corrected"}) + "\n")
    rows2, summary = A.run_audit(days=30, projects_dir=projects, state_dir=state, now=now, human_labels=hl)
    got = next(r for r in rows2 if r["unit_id"] == target["unit_id"])
    assert (got["label"], got["signal"], got["human_reviewed"]) == ("corrected", "human_review", True)
    assert summary["human_reviewed"] == 1


def _ambiguous_units():
    return [A.Unit("routed_mcp", "s", 1.0, "unknown", "tool_result_ambiguous_overlap", "low",
                   evidence={"routed": "r", "host": "h"}),
            A.Unit("draft", "s", 2.0, "unused", "draft_not_relayed", "high",
                   evidence={"routed": "r", "host": "h"})]


def test_judge_is_off_by_default():
    assert A._apply_judge(_ambiguous_units(), None, None) == {"enabled": False, "status": "OFF"}


def test_proposed_judge_is_shadow_only(monkeypatch):
    monkeypatch.setattr(A, "judge_label", lambda *a, **k: "used")
    units = _ambiguous_units()
    info = A._apply_judge(units, "ollama/x", {"status": "PROPOSED"})
    assert [u.label for u in units] == ["unknown", "unused"]
    assert info["judge_labels"]["used"] == 1 and info["applied_to_headline"] is False


def test_active_judge_touches_only_the_ambiguous_remainder(monkeypatch):
    monkeypatch.setattr(A, "judge_label", lambda *a, **k: "used")
    units = _ambiguous_units()
    A._apply_judge(units, "ollama/x", {"status": "ACTIVE"})
    assert (units[0].label, units[0].signal) == ("used", "judge:ollama/x")
    assert units[1].label == "unused"  # a decided row is never re-judged


def test_calibration_needs_n_and_agreement_to_activate(monkeypatch, tmp_path):
    units = [A.Unit("draft", "s", 1000.0 + i, "unused", "x", "low", evidence={"routed": "r", "host": "h"})
             for i in range(45)]
    sample = tmp_path / "s.jsonl"
    sample.write_text("".join(json.dumps({
        "session_id": "s", "kind": "draft",
        "ts": datetime.fromtimestamp(1000.0 + i, tz=timezone.utc).isoformat(),
        "northstar_outcome": "discarded", "hand_check_verdict": "agree"}) + "\n" for i in range(45)))
    monkeypatch.setattr(A, "judge_label", lambda *a, **k: "unused")
    good = A.calibrate_judge("ollama/x", sample, units)
    assert (good["n"], good["agreement"], good["status"]) == (45, 1.0, "ACTIVE")
    monkeypatch.setattr(A, "judge_label", lambda *a, **k: "used")
    bad = A.calibrate_judge("ollama/x", sample, units)
    assert (bad["agreement"], bad["status"]) == (0.0, "PROPOSED")


def test_ledgerless_llm_edit_call_is_judged_by_its_edits():
    new = "- appended status line for the morning run"
    result = json.dumps({"result": "**1 edit(s) to apply:**\n\n### Edit 1: /no/such/file.md\n_x_\n\n"
                                   "**Replace:**\n```\nold\n```\n**With:**\n```\n" + new + "\n```\n"})
    assert A._edit_files_in(result) == ["/no/such/file.md"]
    assert A._with_blocks_for(result, "/no/such/file.md") == [new]
