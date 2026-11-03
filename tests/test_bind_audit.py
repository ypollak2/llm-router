"""BIND W3: the outcome audit measures routed work correctly (audit v2).

The 16.0.0 audit read 0 used of 564 routed units. BIND-R1 and BIND-R2 each followed
units through the transcripts (one model per report, unblinded, 193 of the 564 units)
and found the audit itself undercounting and mis-pairing; the fixes are in-sample with
respect to those rows. One test per defect, each on synthetic text (no owner prompt or
reply text is in this file):

* Codex units were paired by "the latest unclaimed Agent call within 900 s" (31% wrong)
  and nested spawns were invisible (8 of the 9 unknowns).
* ``session_kind_overrides.json`` was ignored.
* A verbatim relay of an MCP answer scored ``used`` only 9 of 113 times (the JSON
  envelope, header and footer diluted it); tool inputs and ``llm_local_task`` file
  effects were not counted; probes sat in the denominator; replayed rows counted
  twice; a backgrounded call was scored on its stub.

``tests/test_outcome_audit.py`` still pins the rules that did not change.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "bind_w3"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load("_bind_audit_under_test", "scripts/release/outcome_audit.py")

from tests.bind_corpus import (  # noqa: E402
    ANSWER, COMMIT, CWD, NOW, OLD_REASON, OTHER, PROJ, SID, SID2, SID3, W1_ESCAPE, W1_LEAD, W1_SOURCE, Transcript, _iso,
    agent_prompt, codex_row, envelope, hook_stdout, launch_text, w1_wrapper, write_state,
)


def audit_units(root: pathlib.Path, state: pathlib.Path, overrides=None):
    units, ctx = A._collect(30, root / "projects", state, NOW, overrides=overrides)
    return units, ctx


def by_kind(units, kind):
    return [u for u in units if u.kind == kind]


# ── envelope, probes, short bodies ───────────────────────────────────────────

def test_answer_body_strips_envelope_header_and_footer():
    assert A.answer_body(envelope(ANSWER)) == ANSWER
    cache = json.dumps({"result": f"> 🤖 **⬜ cache/ollama/x** · $0.000000 · 0ms\n\n{ANSWER}\n\n─────\n→ cache hit (100%) · x · $0"})
    assert A.answer_body(cache) == ANSWER
    route = json.dumps({"result": f"[S] simple (100%) | task: query | via heuristic ($0.000000, 0ms)\n"
                                  f"> 🤖 **🦙 ollama/x** · 9 tokens · $0 · 5ms\n> **Total cost: $0.000000**\n\n{ANSWER}\n\n"
                                  f"---\n🔀 **Routed to:** x (query/simple) · Cost: $0.0000"})
    assert A.answer_body(route) == ANSWER
    job = json.dumps({"result": f"> 🤖 **🦙 ollama/x** · 9 tokens · $0 · 5ms\n\n_validation: status=accepted, attempts=1_\n\n{COMMIT}"})
    assert A.answer_body(job) == COMMIT
    assert A.answer_body(ANSWER) == ANSWER  # plain text is left alone
    assert A.answer_body(None) == ""
    # a bare JSON string: the escaped newlines become newlines, not "nhttp"-style glue
    bare = json.dumps("> 🤖 **🦙 ollama/x** · 9 tokens · $0 · 5ms\n\nfirst line\nsecond line\n\n─────\n→ x · simple · $0")
    assert A.answer_body(bare) == "first line\nsecond line"
    assert A._WORD_RE.findall(A.answer_body(bare)) == ["first", "line", "second", "line"]


def test_the_raw_envelope_never_reached_the_used_threshold_but_the_body_does():
    """BIND-R2 section 8: scoring the raw tool_result (envelope, header, footer, JSON \\n glued
    onto the next word) put a verbatim relay under 60%; the body is what the host relays."""
    raw = envelope(ANSWER)
    host = "Here is what the router said: " + ANSWER
    assert A.reuse_fraction(raw, host) < A.USED_THRESHOLD       # the defect
    assert A.label_routed_mcp(raw, False, host, True, None)[:2] == ("used", "tool_result_reused")


@pytest.mark.parametrize("prompt,expected", [
    ("Reply OK", True),
    ("Reply with the single word: ready", True),
    ("  reply \"ok\"", True),
    ("Owner instruction: use the newer local model for the benchmark. Reply OK.", True),
    ("Reply with one word: does appending a line with echo count as writing code? (yes/no)", False),  # a real question
    ("Reply with the migration plan for the schema change in full detail please " + "x" * 120, False),  # too long to be a probe
    ("Reply with a one-line commit message for this change:\nfix the retry backoff", False),   # context on the next line
    ("What does git rev-parse --short HEAD print", False),
    ("", False),
    (None, False),
])
def test_probe_prompt_rule(prompt, expected):
    assert A.is_probe_prompt(prompt) is expected


@pytest.mark.parametrize("words,size", [(8, 4), (19, 4), (20, 8), (60, 8)])
def test_shingle_size_follows_the_body(words, size):
    assert A.shingle_size(words) == size


def test_bodies_under_eight_words_cannot_be_attributed():
    """3 to 7 word bodies matched another unit's host text by chance in 13 of 157 draws (8.3%): the PR #816 review."""
    for body in ("No, it does not", "git log --oneline -5", "Hash.", "seven words are not enough to attribute"):
        assert A.body_reuse(body, "The test says " + body.lower() + " here")[0] is None
    got = A.label_routed_mcp("No, it does not", False, "The test says no, it does not pass", True, None)
    assert got[:2] == ("unknown", "body_too_short_to_attribute")
    eight = "one two three four five six seven eight"
    assert A.body_reuse(eight, "x " + eight)[0] == 1.0                                  # 8 words is the first length scored
    assert A.body_reuse("one two three four five six seven", "x one two three four five six seven")[0] is None
    assert A.label_routed_mcp("", False, "x", True, None)[0] == "unused"


def test_medium_body_uses_four_word_shingles():
    body = "run git log with the format flag then pipe it through head to keep five lines only"  # 17 words
    host = "$ run git log with the format flag then pipe it through head to keep five lines"
    frac, words = A.body_reuse(body, host)
    assert words == 17 and frac >= A.USED_THRESHOLD
    assert A.label_routed_mcp(body, False, host, True, None)[:2] == ("used", "tool_result_reused")


# ── host output includes tool inputs ─────────────────────────────────────────

def test_a_commit_message_relayed_into_bash_is_use():
    s = A.Session(SID, [
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b", "name": "Bash",
                                                        "input": {"command": f"git commit -m '{COMMIT}'"}}]}},
    ], {})
    text_only, _, _ = s.reply_after(0)
    with_inputs, _, _ = s.reply_after(0, inputs=True)
    assert COMMIT not in text_only and COMMIT in with_inputs
    assert A.label_routed_mcp(COMMIT, False, with_inputs, True, None)[:2] == ("used", "tool_result_reused")


def test_inputs_of_router_tools_and_read_tools_are_not_host_output():
    assert A.input_strings({"name": "mcp__llm_router__llm", "input": {"prompt": ANSWER}}) == []
    assert A.input_strings({"name": "Read", "input": {"file_path": "/a/b"}}) == []
    assert A.input_strings({"name": "Bash", "input": {"command": "ls", "description": "list"}}) == ["ls", "list"]
    assert A.input_strings({"name": "SendMessage", "input": {"to": "a", "message": {"text": "hi"}}}) == ["a", "hi"]


# ── local_task: judged by file effects ───────────────────────────────────────

def _lt(**kw):
    base = {"status": "verified_complete", "changed_files": ["src/a.py"], "writes_applied": True, "error": None}
    base.update(kw)
    return base


@pytest.mark.parametrize("res,follow,expected", [
    (_lt(), "kept", ("used", "local_task_effect_kept")),
    (_lt(status="failed_check"), "kept", ("used", "local_task_effect_kept")),    # a malformed check; the edit was kept
    (_lt(changed_files=[]), "kept", ("used", "local_task_effect_kept")),
    (_lt(), "amended", ("corrected", "local_task_effect_amended")),
    (_lt(), "undone", ("unused", "local_task_effect_undone")),
    (_lt(), None, ("unknown", "local_task_effect_unconfirmed")),
    (_lt(status="incomplete", changed_files=[]), "kept", ("unused", "local_task_no_effect")),  # max iterations
    (_lt(status="failed", error="boom"), None, ("unused", "local_task_failed")),
    (_lt(writes_applied=False), "kept", ("unused", "local_task_not_applied")),
    (None, None, ("unknown", "tool_result_unreadable")),
])
def test_local_task_label(res, follow, expected):
    assert A.label_local_task(res, follow)[:2] == expected


WD = "/work/wt-a"
CHANGED = ["src/a.py"]


def _fu(*cmds, changed=CHANGED, wd=WD, edits=()):
    return A.local_task_follow_up([("edit", e) for e in edits] + [("bash", c) for c in cmds], wd, changed)


def test_local_task_is_kept_when_a_commit_stages_what_the_tool_changed():
    assert _fu(f"cd {WD} && git add -A && git commit -m x") == "kept"                    # add -A, no pathspec
    assert _fu(f"cd {WD} && git commit -am x") == "kept"                                 # commit -a
    assert _fu(f"cd {WD} && git commit -qam 'x -a y'") == "kept"
    assert _fu(f"cd {WD} && git add src/a.py && git commit -m x") == "kept"              # the changed file
    assert _fu(f"cd {WD} && git add -A src && git commit -m x") == "kept"                # add -A over a directory holding it
    assert _fu(f"cd {WD} && git add src/a.py", f"cd {WD} && git commit -m x") == "kept"  # add and commit in two calls
    assert _fu(f"cd {WD} && git -c user.name=t -c user.email=t@t commit -am x") == "kept"
    assert _fu(f"cd {WD}/ && git add . && git commit -m x") == "kept"
    assert _fu(f"cd {WD} && git add -A; git commit -q -F - <<'EOF'\nmsg\nEOF") == "kept"
    assert _fu(f"cd {WD} && git add -A && git commit -m x", changed=[]) == "kept"          # nothing listed: only "everything" counts


def test_local_task_is_not_kept_by_a_commit_that_is_about_something_else():
    """The two over-count channels of the PR #816 review: the workdir matched as a substring, and any commit counted."""
    assert _fu("git commit -am x") is None                                                # names no directory
    assert _fu("cd /work/wt-ab && git add -A && git commit -m x") is None                # /work/wt-a is a prefix of /work/wt-ab
    assert _fu("cd /tmp/work/wt-a && git add -A && git commit -m x") is None             # another tree with the same tail
    assert _fu(f"cd {WD} && git add docs/x.md && git commit -m x") is None               # an unrelated file
    assert _fu(f"cd {WD} && git add -A docs && git commit -m x") is None                 # add -A over an unrelated directory
    assert _fu(f"cd {WD} && git commit -m 'fix a -a b'") is None                         # "-a" inside a message is not a flag
    assert _fu(f"cd {WD} && pytest -q") is None
    assert _fu(f"cd {WD} && git add src/a.py && git commit -m x", changed=[]) is None    # nothing listed: a named file proves nothing
    assert _fu(f"cd {WD} && git add -A && git commit -m x", wd="") is None
    assert _fu(f"cd {WD} && git stash && pytest -q && git stash pop") is None            # a baseline check is not a commit or an undo


def test_local_task_is_undone_by_a_restore_of_its_file_and_amended_by_a_host_edit():
    assert _fu(f"cd {WD} && git checkout -- src/a.py", f"cd {WD} && git commit -am x") == "undone"
    assert _fu(f"cd {WD} && git restore src/a.py", f"cd {WD} && git commit -am x") == "undone"
    assert _fu(f"cd {WD} && git checkout -b topic", f"cd {WD} && git commit -am x") == "kept"       # a branch is not an undo
    assert _fu(f"cd {WD} && git checkout docs/other.md", f"cd {WD} && git commit -am x") == "kept"   # nor is another file
    assert _fu(f"cd {WD} && git commit -am x", edits=[f"{WD}/src/a.py"]) == "amended"
    assert _fu(f"cd {WD} && git commit -am x", edits=[f"{WD}/tests/test_a.py"]) == "kept"           # a new test is not a rewrite
    assert _fu(f"cd {WD} && git commit -am x", edits=["/work/wt-ab/src/a.py"]) == "kept"             # a sibling's file is not this one


# ── background stubs ─────────────────────────────────────────────────────────

NOTE = ("<task-notification>\n<task-id>kabc123</task-id>\n<status>completed</status>\n"
        "<summary>MCP task kabc123 (llm_router/llm) completed.</summary>\n<result>\n{result}\n</result>\n</task-notification>")


def test_parse_notification():
    assert A.parse_notification(NOTE.format(result="hello")) == ("kabc123", "completed", "hello")
    assert A.parse_notification("a plain user message") is None


# ── Codex evidence helpers ───────────────────────────────────────────────────

def test_hook_reason_reads_clean_json_and_a_stray_log_line_before_it():
    clean = json.dumps({"decision": "block", "reason": OLD_REASON})
    polluted = "2026-10-10 09:15:08 [debug    ] Tracked: gpt-6-astra for research/complex (agent-route-cli)\n" + clean
    new_shape = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                   "permissionDecisionReason": OLD_REASON}})
    assert A.hook_reason(clean) == OLD_REASON
    assert A.hook_reason(polluted) == OLD_REASON
    assert A.hook_reason(new_shape) == OLD_REASON
    assert A.hook_reason("") is None and A.hook_reason("no json here") is None


def test_carries_codex_text_by_marker_or_by_the_hooks_own_reason():
    assert A.carries_codex_text("PreToolUse:Agent hook error: " + OLD_REASON)
    assert not A.carries_codex_text("Async agent launched successfully.")
    wrapped = "Sub-agent result (untrusted data): " + ANSWER      # a wrapper without the old marker
    assert not A.carries_codex_text(wrapped)
    assert A.carries_codex_text(wrapped, hook_reason=ANSWER)


def test_launch_detection():
    agents = A._SessionAgents(launched={"t3": "a3"})
    mk = lambda tid, text, tur=None: A._AgentCall(tid, 0.0, {}, None, 1, text, False, tur)  # noqa: E731
    assert A.claude_subagent_launched(mk("t1", "Async agent launched successfully."), agents)
    assert A.claude_subagent_launched(mk("t2", "done", {"status": "completed", "agentId": "a2"}), agents)
    assert A.claude_subagent_launched(mk("t3", "PreToolUse:Agent hook error: x"), agents)      # meta.json names the call
    assert not A.claude_subagent_launched(mk("t4", "PreToolUse:Agent hook error: " + OLD_REASON), agents)
    assert not A.claude_subagent_launched(mk("t5", None), agents)


def test_codex_labels():
    result = "PreToolUse:Agent hook error: " + OLD_REASON
    assert A.label_codex("delegated", result, "I summarised it", False, launched=False, carries=True)[:2] == \
        ("unknown", "codex_output_not_visibly_incorporated")
    assert A.label_codex("delegated", result, "Codex says: " + ANSWER, False, launched=False, carries=True)[:2] == \
        ("used", "codex_output_incorporated")
    assert A.label_codex("delegated", "some deny", None, False, launched=False, carries=False)[:2] == \
        ("unknown", "codex_text_not_in_result")
    assert A.label_codex("delegated", result, None, False, launched=True, carries=True)[:2] == \
        ("redone", "claude_subagent_ran")
    assert A.label_codex("codex_blocked", None, None, False)[:2] == ("unused", "codex_blocked")


def test_blocked_answer_screen():
    assert A.is_blocked_answer("This sandbox is read-only, so I could not run the tests or open the PR.")
    assert not A.is_blocked_answer("The retry loop caps the delay at thirty seconds. No other issue found.")
    assert not A.is_blocked_answer("The file is read-only on purpose; the review found two real defects.")  # no refusal


def test_hold_results_are_anchored_on_the_harness_prefix():
    deny = "PreToolUse:Bash hook error: [llm_router] Routing directive BLOCKED.\nViolation 1/4"
    assert A.is_hold_result(deny)
    assert not A.is_hold_result("grep hit: raise RuntimeError('Routing directive BLOCKED')")   # a file that quotes it
    assert not A.is_hold_result(None)


# ── session-kind overrides ───────────────────────────────────────────────────

def test_override_file_reader_ignores_bad_entries(tmp_path):
    p = tmp_path / "o.json"
    p.write_text(json.dumps({SID: {"kind": "research", "reason": "r"}, SID2: {"kind": "nonsense"},
                             SID3: "organic", "x": None}))
    assert A.read_session_overrides(p) == {SID: "research"}
    assert A.read_session_overrides(tmp_path / "missing.json") == {}
    p.write_text("not json")
    assert A.read_session_overrides(p) == {}


@pytest.mark.parametrize("meta,override,expected", [
    ({"entrypoint": "cli", "cwd": CWD, "project_dir": PROJ, "session_id": SID}, None, True),
    ({"entrypoint": "cli", "cwd": CWD, "project_dir": PROJ, "session_id": SID}, "research", False),
    ({"entrypoint": "cli", "cwd": CWD, "project_dir": PROJ, "session_id": SID}, "organic", True),
    # "organic" lifts the cwd/worktree classification but not the structural exclusions:
    ({"entrypoint": "cli", "cwd": "/private/tmp/x", "project_dir": "-private-tmp-x", "session_id": SID}, "organic", True),
    ({"entrypoint": "sdk-cli", "cwd": CWD, "project_dir": PROJ, "session_id": SID}, "organic", False),
    ({"entrypoint": "cli", "cwd": CWD, "project_dir": PROJ, "session_id": "modetest"}, "organic", False),
])
def test_is_organic_override_precedence(meta, override, expected):
    assert A.is_organic(meta, override) is expected


def test_overridden_session_is_not_read_and_its_units_are_gone(tmp_path):
    for sid, tid in ((SID, "t_keep"), (SID2, "t_drop")):
        t = Transcript(tmp_path, sid)
        t.human(NOW - 3000, "please run the next step of the plan now")
        t.call(NOW - 2990, tid, "mcp__llm_router__llm", {"prompt": "what is the retry policy", "task": "query"})
        t.result(NOW - 2985, tid, envelope(ANSWER))
        t.say(NOW - 2980, OTHER)
        t.write()
    state = write_state(tmp_path, overrides={SID2: {"kind": "research", "reason": "coordinator"}})
    units, ctx = audit_units(tmp_path, state)
    assert [u.session_id for u in by_kind(units, "routed_mcp")] == [SID]
    assert ctx["overrides"]["excluded_sessions"] == [SID2]
    # an empty override set (the validation run) brings it back
    units, _ = audit_units(tmp_path, state, overrides={})
    assert sorted(u.session_id for u in by_kind(units, "routed_mcp")) == sorted([SID, SID2])


# ── MCP units end to end ─────────────────────────────────────────────────────

def mcp_corpus(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 20000
    t.human(t0, "please look at the retry policy and tidy the sync worker")
    # 1. a verbatim relay of the answer body in prose
    t.call(t0 + 10, "u_text", "mcp__llm_router__llm", {"prompt": "what should the retry policy be", "task": "query"})
    t.result(t0 + 12, "u_text", envelope(ANSWER))
    t.say(t0 + 14, "The router says: " + ANSWER)
    # 2. a commit message relayed into Bash
    t.human(t0 + 100, "now commit the sync worker change with a good message")
    t.call(t0 + 110, "u_job", "mcp__llm_router__llm_text_job", {"job": "commit_message", "input_text": "diff", "hint": "retry"})
    t.result(t0 + 112, "u_job", json.dumps({"result": f"> 🤖 **🦙 ollama/x** · 9 tokens · $0 · 5ms\n\n_validation: status=accepted, attempts=1_\n\n{COMMIT}"}))
    t.call(t0 + 114, "b_commit", "Bash", {"command": f"git commit -m \"{COMMIT}\""})
    t.result(t0 + 115, "b_commit", "[main abc123] done")
    # 3. a probe, plain
    t.human(t0 + 200, "keep going with the next task in the list please")
    t.call(t0 + 210, "u_probe", "mcp__llm_router__llm", {"prompt": "Reply OK", "task": "query"})
    t.result(t0 + 212, "u_probe", envelope("OK"))
    t.say(t0 + 214, "Continuing.")
    # 4. a hold, then a probe that releases it
    t.human(t0 + 300, "deploy the main branch now to the machine")
    t.call(t0 + 310, "b_held", "Bash", {"command": "deploy.sh main"})
    t.result(t0 + 311, "b_held", "PreToolUse:Bash hook error: [llm_router] Routing directive BLOCKED.\nViolation 1/4", is_error=True)
    t.call(t0 + 312, "u_release", "mcp__llm_router__llm", {"prompt": "Reply OK", "task": "query"})
    t.result(t0 + 314, "u_release", envelope("OK"))
    t.call(t0 + 316, "b_deploy", "Bash", {"command": "deploy.sh main"})
    t.result(t0 + 317, "b_deploy", "deployed")
    # 5. a status view
    t.human(t0 + 400, "show me the router status and the savings so far")
    t.call(t0 + 410, "u_status", "mcp__llm_router__llm_router_status", {})
    t.result(t0 + 412, "u_status", json.dumps({"result": "## Provider Health\nollama: healthy"}))
    t.say(t0 + 414, "Status looks fine, nothing to change.")
    # 6. an answer the host ignored
    t.human(t0 + 500, "what is the name of the file that holds the settings")
    t.call(t0 + 510, "u_ignored", "mcp__llm_router__llm", {"prompt": "name the settings file", "task": "query"})
    t.result(t0 + 512, "u_ignored", envelope(ANSWER))
    t.say(t0 + 514, OTHER)
    # 7. a local task whose edit the host committed
    t.human(t0 + 600, "have the local model fix the flaky test then commit it")
    t.call(t0 + 610, "u_lt", "mcp__llm_router__llm_local_task",
           {"objective": "fix the flaky test", "workdir": "/work/wt-a", "acceptance_check": "pytest -q", "apply_writes": True})
    t.result(t0 + 650, "u_lt", json.dumps({"result": json.dumps(_lt(changed_files=["tests/test_a.py"]))}))
    t.call(t0 + 660, "b_lt", "Bash", {"command": "cd /work/wt-a && git add -A && git commit -m fix"})
    t.result(t0 + 661, "b_lt", "[x] ok")
    # 8. a local task that ran out of iterations
    t.human(t0 + 700, "try the local model on the other flaky test as well")
    t.call(t0 + 710, "u_lt2", "mcp__llm_router__llm_local_task",
           {"objective": "fix the other test", "workdir": "/work/wt-b", "apply_writes": True})
    t.result(t0 + 750, "u_lt2", json.dumps({"result": json.dumps(_lt(status="incomplete", changed_files=[]))}))
    t.say(t0 + 755, OTHER)
    t.write()
    return t0


def test_mcp_units_end_to_end(tmp_path):
    mcp_corpus(tmp_path)
    units, ctx = audit_units(tmp_path, write_state(tmp_path))
    got = {u.extra["tool_use_id"]: (u.label, u.signal) for u in by_kind(units, "routed_mcp")}
    assert got == {
        "u_text": ("used", "tool_result_reused"),
        "u_job": ("used", "tool_result_reused"),
        "u_probe": ("not_applicable", "probe_fixed_reply"),
        "u_release": ("not_applicable", "hold_release_probe"),
        "u_status": ("not_applicable", "status_view"),
        "u_ignored": ("unused", "tool_result_not_reused"),
        "u_lt": ("used", "local_task_effect_kept"),
        "u_lt2": ("unused", "local_task_no_effect"),
    }
    assert ctx["holds"]["n"] == 1 and ctx["holds"]["n_prompts"] == 8


def test_summary_excludes_not_applicable_from_the_denominator(tmp_path):
    mcp_corpus(tmp_path)
    rows, summary = A.run_audit(30, tmp_path / "projects", write_state(tmp_path), NOW)
    h = summary["headline"]
    assert (h["used"], h["attempted"], h["not_applicable"]) == (3, 5, 3)
    assert summary["audit_version"] == A.AUDIT_VERSION == 2
    assert summary["labels"]["not_applicable"] == 3
    assert summary["not_applicable"]["by_signal"] == {"hold_release_probe": 1, "probe_fixed_reply": 1, "status_view": 1}
    assert summary["not_applicable"]["used_rate_if_included"] == pytest.approx(3 / 8)   # the exclusion can be undone
    assert summary["by_kind"]["routed_mcp"]["n"] == 5 and summary["by_kind"]["routed_mcp"]["n_total"] == 8
    assert summary["holds"]["n"] == 1
    for r in rows:
        assert tuple(r) == A.ROW_KEYS


def test_replayed_tool_uses_count_once_and_belong_to_the_older_session(tmp_path):
    for sid, start in ((SID, NOW - 9000), (SID2, NOW - 4000)):    # SID2 is the later session
        t = Transcript(tmp_path, sid)
        t.human(start, "please look at the retry policy again now")
        t.call(NOW - 3000, "shared", "mcp__llm_router__llm", {"prompt": "what is the policy", "task": "query"})  # same id, same ts
        t.result(NOW - 2990, "shared", envelope(ANSWER))
        t.say(NOW - 2980, OTHER)
        t.write()
    units, ctx = audit_units(tmp_path, write_state(tmp_path))
    mcp = by_kind(units, "routed_mcp")
    assert [u.session_id for u in mcp] == [SID]
    assert ctx["dedup"]["replayed_tool_uses"] == 1


def test_background_stub_is_followed_to_its_notification(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 5000
    t.human(t0, "please run the local model on the slow benchmark")
    stub = ('MCP tool "llm_router/llm_local_task" is still running after 120s. It was moved to the background as '
            "task kabc123 and keeps running; you'll receive a notification with the result when it completes.")
    t.call(t0 + 5, "u_bg", "mcp__llm_router__llm", {"prompt": "summarise the slow benchmark run", "task": "query"})
    t.result(t0 + 125, "u_bg", [{"type": "text", "text": stub}])
    t.raw_user(t0 + 300, NOTE.format(result=envelope(ANSWER)))
    t.say(t0 + 310, "The benchmark summary: " + ANSWER)
    t.call(t0 + 400, "u_bg2", "mcp__llm_router__llm", {"prompt": "summarise the other benchmark run", "task": "query"})
    t.result(t0 + 520, "u_bg2", [{"type": "text", "text": stub.replace("kabc123", "kmissing")}])
    t.say(t0 + 530, OTHER)
    t.write()
    units, _ = audit_units(tmp_path, write_state(tmp_path))
    got = {u.extra["tool_use_id"]: (u.label, u.signal) for u in by_kind(units, "routed_mcp")}
    assert got["u_bg"] == ("used", "tool_result_reused")            # scored on the real result, not on the stub
    assert got["u_bg2"] == ("unknown", "background_result_missing")


def test_error_on_a_probe_is_not_applicable_and_error_on_a_task_is_unused(tmp_path):
    t = Transcript(tmp_path, SID)
    t.human(NOW - 900, "please check the retry policy for the worker")
    t.call(NOW - 890, "e1", "mcp__llm_router__llm", {"prompt": "Reply OK", "task": "query"})
    t.result(NOW - 888, "e1", "Error executing tool llm", is_error=True)
    t.call(NOW - 880, "e2", "mcp__llm_router__llm", {"prompt": "research the retry policy", "task": "research"})
    t.result(NOW - 878, "e2", "Error executing tool llm", is_error=True)
    t.write()
    units, _ = audit_units(tmp_path, write_state(tmp_path))
    got = {u.extra["tool_use_id"]: (u.label, u.signal) for u in by_kind(units, "routed_mcp")}
    assert got == {"e1": ("not_applicable", "probe_fixed_reply"), "e2": ("unused", "tool_result_error")}


# ── Codex units end to end ───────────────────────────────────────────────────

def test_old_rows_are_paired_by_their_hook_run_not_by_the_latest_call(tmp_path):
    """Two Agent calls issued 1 s apart; the hooks (and ledger rows) finish in the order
    1, 2. The 900 s rule paired each row with the LATEST earlier call: row 1 with call 2."""
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please review both of the changes in parallel now")
    t.call(t0 + 1, "tA", "Agent", {"prompt": agent_prompt("A"), "subagent_type": "task-reviewer"})
    t.call(t0 + 2, "tB", "Agent", {"prompt": agent_prompt("B"), "subagent_type": "general-purpose"})
    t.hook(t0 + 20.3, "tA", hook_stdout())
    t.result(t0 + 20.4, "tA", launch_text("aaaa"), tur={"isAsync": True, "status": "async_launched", "agentId": "aaaa",
                                                       "resolvedModel": "claude-opus-5-5"})
    t.hook(t0 + 40.3, "tB", hook_stdout())
    t.result(t0 + 40.4, "tB", launch_text("bbbb"), tur={"isAsync": True, "status": "async_launched", "agentId": "bbbb"})
    t.say(t0 + 50, OTHER)
    t.write()
    rows = [codex_row(t0 + 20.0, subagent_type="task-reviewer"), codex_row(t0 + 40.0, subagent_type="general-purpose")]
    units, _ = audit_units(tmp_path, write_state(tmp_path, rows))
    codex = sorted(by_kind(units, "agent_route_codex"), key=lambda u: u.ts)
    assert [u.extra["tool_use_id"] for u in codex] == ["tA", "tB"]
    assert [u.extra["pairing"] for u in codex] == ["hook_run", "hook_run"]
    assert all((u.label, u.signal) == ("redone", "claude_subagent_ran") for u in codex)
    assert codex[0].extra["resolved_model"] == "claude-opus-5-5" and codex[0].extra["call_subagent_type"] == "task-reviewer"
    # the rule it replaces really did mis-pair (so this test would have failed before):
    old_pick = max((c for c in (("tA", t0 + 1), ("tB", t0 + 2)) if c[1] <= t0 + 20.0 + 2), key=lambda c: c[1])
    assert old_pick[0] == "tB"


def test_new_rows_are_paired_by_tool_use_id(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please review the change and the other change")
    t.call(t0 + 1, "tOLD", "Agent", {"prompt": agent_prompt("A")})
    t.result(t0 + 3, "tOLD", launch_text("aaaa"))
    t.call(t0 + 100, "tNEW", "Agent", {"prompt": agent_prompt("B"), "subagent_type": "task-reviewer"})
    t.result(t0 + 130, "tNEW", "PreToolUse:Agent hook error: " + OLD_REASON, is_error=True,
             tur="Error: PreToolUse:Agent hook error")
    t.say(t0 + 140, "The reviewer found: " + ANSWER)
    t.write()
    # the ledger row names the OLDER call although the newer one is nearer in time
    rows = [codex_row(t0 + 129.0, tool_use_id="tOLD")]
    units, _ = audit_units(tmp_path, write_state(tmp_path, rows))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["pairing"] == "tool_use_id" and u.extra["tool_use_id"] == "tOLD"
    assert (u.label, u.signal) == ("redone", "claude_subagent_ran")
    rows = [codex_row(t0 + 129.0, tool_use_id="tNEW")]
    units, _ = audit_units(tmp_path, write_state(tmp_path, rows))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["tool_use_id"] == "tNEW" and u.extra["bound"] is True and u.extra["launched"] is False
    assert (u.label, u.signal) == ("used", "codex_output_incorporated")       # the host relayed the answer


def test_bound_unit_the_host_only_summarised_is_bound_but_not_used(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": agent_prompt("B"), "subagent_type": "task-reviewer"})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + OLD_REASON, is_error=True)
    t.say(t0 + 40, "The review is back and it was short; one thing to check.")
    t.write()
    units, ctx = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["bound"] is True
    assert (u.label, u.signal) == ("unknown", "codex_output_not_visibly_incorporated")   # binding is reported apart from `used`
    assert ctx["binding"]["bound"] == ctx["binding"]["delivered"] == ctx["binding"]["delegated"] == 1
    assert ctx["binding"]["void_claude_subagent_ran"] == ctx["binding"]["respawned_after_delivery"] == 0


def test_blocked_codex_text_is_flagged_among_bound(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the reviewer run the tests for the change")
    blocked = OLD_REASON.replace(ANSWER, "The sandbox is read-only so I could not run the tests or open the pull request.")
    t.call(t0 + 1, "tB", "Agent", {"prompt": agent_prompt("B")})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + blocked, is_error=True)
    t.say(t0 + 40, "That did not work.")
    t.write()
    units, ctx = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    assert ctx["binding"]["blocked_screen_among_bound"] == 1


def test_a_sub_agent_launch_is_found_in_meta_json_even_when_the_result_is_a_deny(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": agent_prompt("B")})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + OLD_REASON, is_error=True)
    t.say(t0 + 40, OTHER)
    t.subagent("zzzz", [], tool_use_id="tB")
    t.write()
    units, ctx = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["launched"] is True and u.extra["bound"] is False
    assert (u.label, u.signal) == ("redone", "claude_subagent_ran")
    assert ctx["binding"]["void_claude_subagent_ran"] == 1


def test_a_nested_spawn_lives_in_the_sub_agent_transcript(tmp_path):
    """8 of the 9 unknowns in the 16.0.0 audit: the Agent call and its hook run are in
    <session>/subagents/agent-*.jsonl, which the audit never read."""
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the lead reviewer split the work")
    t.call(t0 + 1, "tTOP", "Agent", {"prompt": agent_prompt("top"), "subagent_type": "general-purpose"})
    t.result(t0 + 2, "tTOP", launch_text("lead"))
    t.write()
    nested = []
    for rec in (
        {"type": "assistant", "timestamp": _iso(t0 + 10), "isSidechain": True, "sessionId": SID,
         "message": {"content": [{"type": "tool_use", "id": "tNEST", "name": "Agent",
                                  "input": {"prompt": agent_prompt("nested"), "subagent_type": "task-reviewer"}}]}},
        {"type": "attachment", "timestamp": _iso(t0 + 40.3), "isSidechain": True, "sessionId": SID,
         "attachment": {"type": "hook_success", "hookName": "PreToolUse:Agent", "toolUseID": "tNEST", "stdout": hook_stdout()}},
        {"type": "user", "timestamp": _iso(t0 + 40.4), "isSidechain": True, "sessionId": SID,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "tNEST", "content": launch_text("child")}]}},
    ):
        nested.append(rec)
    t.subagent("lead", nested, tool_use_id="tTOP", agent_type="general-purpose")
    units, ctx = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 40.0)]))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["tool_use_id"] == "tNEST" and u.extra["pairing"] == "hook_run"
    assert (u.label, u.signal) == ("redone", "claude_subagent_ran")
    assert ctx["binding"]["no_agent_call_found"] == 0


def test_unpaired_delegation_is_unknown_and_counted(tmp_path):
    t = Transcript(tmp_path, SID)
    t.human(NOW - 6000, "please do something unrelated to the reviewer")
    t.say(NOW - 5990, OTHER)
    t.write()
    units, ctx = audit_units(tmp_path, write_state(tmp_path, [codex_row(NOW - 5000)]))
    (u,) = by_kind(units, "agent_route_codex")
    assert (u.label, u.signal) == ("unknown", "no_agent_call_found")
    assert ctx["binding"]["no_agent_call_found"] == 1


def test_failed_and_blocked_dispatches_are_unused_and_not_in_the_binding_denominator(tmp_path):
    t = Transcript(tmp_path, SID)
    t.human(NOW - 6000, "please do something unrelated to the reviewer")
    t.write()
    rows = [codex_row(NOW - 5000, outcome="codex_failed"), codex_row(NOW - 4000, outcome="codex_blocked")]
    units, ctx = audit_units(tmp_path, write_state(tmp_path, rows))
    assert sorted((u.label, u.signal) for u in by_kind(units, "agent_route_codex")) == \
        [("unused", "codex_blocked"), ("unused", "codex_failed")]
    b = ctx["binding"]
    assert (b["delegated"], b["codex_blocked"], b["codex_failed"], b["codex_runs"]) == (0, 1, 1, 2)


def _delivered_then_asked_again(tmp_path, gap):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 9000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": agent_prompt("B")})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), is_error=True)
    t.call(t0 + 30 + gap, "tB2", "Agent", {"prompt": agent_prompt("B")})      # the same task, again: the escape hatch
    t.result(t0 + 32 + gap, "tB2", launch_text("aaaa"))
    t.write()
    rows = [codex_row(t0 + 29.5, tool_use_id="tB"), codex_row(t0 + 30 + gap, tool_use_id="tB2", outcome="codex_reissue")]
    units, ctx = audit_units(tmp_path, write_state(tmp_path, rows))
    (u,) = by_kind(units, "agent_route_codex")
    return u, ctx["binding"]


def test_a_reissue_after_a_delivered_result_is_not_bound(tmp_path):
    u, b = _delivered_then_asked_again(tmp_path, 50)
    assert u.extra["delivered"] is True and u.extra["respawned"] is True and u.extra["bound"] is False
    assert (u.label, u.signal) == ("redone", "respawned_after_codex")
    assert (b["delegated"], b["delivered"], b["bound"], b["respawned_after_delivery"]) == (1, 1, 0, 1)
    assert b["codex_reissue_rows"] == 1                                         # the router's own ledger row, counted beside


@pytest.mark.parametrize("gap,reissued", [(3599.0, True), (3601.0, False)])
def test_the_reissue_window_is_the_routers_memory_of_a_delivered_prompt(tmp_path, gap, reissued):
    u, b = _delivered_then_asked_again(tmp_path, gap)
    assert u.extra["respawned"] is reissued and u.extra["bound"] is (not reissued)


def test_a_different_task_after_a_delivered_result_leaves_it_bound(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": agent_prompt("B")})
    t.result(t0 + 30, "tB", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), is_error=True)
    t.call(t0 + 60, "tC", "Agent", {"prompt": "inspect the migration script for the ledger table and list every unsafe statement in it"})
    t.result(t0 + 62, "tC", launch_text("cccc"))
    t.write()
    units, _ = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    (u,) = by_kind(units, "agent_route_codex")
    assert u.extra["bound"] is True and u.extra["respawned"] is False


# ── bound needs the deny SHAPE and the wrapper, not a marker alone ───────────

def _one_unit(tmp_path, result_text, is_error, stdout=None, tur=None, launch_meta=False):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the reviewer look at the change")
    t.call(t0 + 1, "tB", "Agent", {"prompt": agent_prompt("B")})
    if stdout is not None:
        t.hook(t0 + 29.8, "tB", stdout)
    t.result(t0 + 30, "tB", result_text, is_error=is_error, tur=tur)
    t.say(t0 + 40, "ok")
    if launch_meta:
        t.subagent("zzzz", [], tool_use_id="tB")
    t.write()
    units, ctx = audit_units(tmp_path, write_state(tmp_path, [codex_row(t0 + 29.5, tool_use_id="tB")]))
    (u,) = by_kind(units, "agent_route_codex")
    return u, ctx["binding"]


CURRENT_SHAPE = json.dumps({"decision": "block", "reason": "r", "hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "r"}})
LEGACY_SHAPE = json.dumps({"decision": "block", "reason": "r"})


def test_a_rendered_deny_with_the_wrapper_is_bound(tmp_path):
    u, b = _one_unit(tmp_path, "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), True)
    assert (u.extra["marker"], u.extra["deny_shape"], u.extra["delivered"], u.extra["bound"]) == (True, True, True, True)
    assert b["bound"] == 1


def test_the_wrapper_text_in_an_ordinary_result_is_not_a_deny(tmp_path):
    """A sub-agent report that quotes the wrapper (a review of this very feature) is not a delivered deny."""
    u, b = _one_unit(tmp_path, "The router said: " + w1_wrapper(ANSWER), False)
    assert (u.extra["marker"], u.extra["deny_shape"], u.extra["bound"]) == (True, False, False)
    assert (u.label, u.signal) == ("unknown", "codex_text_not_in_result")
    assert b["bound"] == 0 and b["delivered_without_deny_shape"] == 1


def test_the_hooks_own_stdout_must_hold_the_current_deny_shape(tmp_path):
    ok, _ = _one_unit(tmp_path / "a", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), True, stdout=CURRENT_SHAPE)
    assert ok.extra["deny_shape"] is True and ok.extra["bound"] is True
    legacy, b = _one_unit(tmp_path / "b", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), True, stdout=LEGACY_SHAPE)
    assert legacy.extra["deny_shape"] is False and legacy.extra["bound"] is False       # decision/reason alone: the 16.0.0 defect
    assert b["delivered_without_deny_shape"] == 1


def test_a_launched_sub_agent_is_never_bound_whatever_the_text_says(tmp_path):
    u, b = _one_unit(tmp_path, "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), True, launch_meta=True)
    assert u.extra["launched"] is True and u.extra["bound"] is False and b["void_claude_subagent_ran"] == 1


def test_a_deny_with_no_codex_wrapper_is_counted_apart_so_wording_drift_is_visible(tmp_path):
    u, b = _one_unit(tmp_path, "PreToolUse:Agent hook error: [llm-router] This sub-agent task was run by a routed model ...", True)
    assert u.extra["marker"] is False and u.extra["bound"] is False
    assert b["result_without_codex_marker"] == 1 and b["bound"] == 0


# ── the audit still writes only the six keys and no text ─────────────────────

def test_artifacts_carry_no_text_and_exactly_six_keys(tmp_path):
    mcp_corpus(tmp_path)
    rows, summary = A.run_audit(30, tmp_path / "projects", write_state(tmp_path), NOW)
    jsonl, summ = A.write_artifacts(rows, summary, tmp_path / "out", "9.9.9")
    blob = jsonl.read_text() + summ.read_text()
    for secret in (ANSWER[:40], COMMIT[:40], OTHER[:40]):
        assert secret not in blob
    assert all(tuple(json.loads(line)) == A.ROW_KEYS for line in jsonl.read_text().splitlines())


# ── the hand labels, as text-free fixtures ───────────────────────────────────

def _uid_hash(unit_id: str) -> str:
    return hashlib.sha256(unit_id.encode()).hexdigest()[:16]


R2_FIX = json.loads((FIXTURES / "r2_mcp_hand_labels.json").read_text())
R1_FIX = json.loads((FIXTURES / "r1_codex_sample.json").read_text())
ALLOWED_KEYS_R2 = {"id", "r2_row", "tool", "class", "subclass", "quality", "hold", "replay_duplicate",
                   "hand_label_any_of", "basis"}
ALLOWED_KEYS_R1 = {"id", "r1_row", "spawned_type", "task_cx", "hook_stdout_opens_with", "agent_tool_result",
                   "claude_agent_model", "old_audit_label", "old_audit_paired_its_own_call", "hand_label",
                   "hand_signal", "hand_bound"}


def test_fixtures_are_text_free_and_match_the_reports_totals():
    assert len(R2_FIX["units"]) == 158 and len({u["id"] for u in R2_FIX["units"]}) == 158
    assert len(R1_FIX["units"]) == 35 and len({u["id"] for u in R1_FIX["units"]}) == 35
    for u in R2_FIX["units"]:
        assert set(u) == ALLOWED_KEYS_R2 and len(u["id"]) == 16 and int(u["id"], 16) >= 0
    for u in R1_FIX["units"]:
        assert set(u) == ALLOWED_KEYS_R1 and len(u["id"]) == 16
    blob = json.dumps(R2_FIX) + json.dumps(R1_FIX)
    for marker in ("llm_router", "/Users/", "ollama", "Reply", "http"):    # no session path, prompt or reply text
        assert marker not in blob
    # R2 section 2/3 totals, recomputed from the fixture
    cls = {}
    for u in R2_FIX["units"]:
        cls[u["class"]] = cls.get(u["class"], 0) + 1
    assert sum(u["replay_duplicate"] for u in R2_FIX["units"]) == 8
    assert (cls["err"], cls["f"], cls["unk"], cls["c"], cls["a"]) == (16, 2, 1, 3, 8)
    assert sum(1 for u in R1_FIX["units"] if not u["old_audit_paired_its_own_call"]) == 9      # R1: 26 of 35 paired


def test_r2_agreement_arithmetic():
    sys.path.insert(0, str(REPO / "scripts" / "release"))
    try:
        ag = _load("_audit_agreement_under_test", "scripts/release/audit_agreement.py")
    finally:
        sys.path.pop(0)
    perfect = {}
    for u in R2_FIX["units"]:
        if not u["replay_duplicate"]:
            perfect[u["id"]] = u["hand_label_any_of"][0]
    r = ag.r2_agreement(perfect, R2_FIX)
    assert r["disagree"] == 0 and r["agreement"] == 1.0 and r["absent"] == 0
    assert r["used"]["hand"] == r["used"]["audit"] == r["used"]["both"] == 10
    flipped = dict(perfect)
    victim = next(u["id"] for u in R2_FIX["units"] if u["hand_label_any_of"] == ["used"])
    flipped[victim] = "unused"
    r = ag.r2_agreement(flipped, R2_FIX)
    assert r["disagree"] == 1 and r["used"]["both"] == 9
    dup = next(u["id"] for u in R2_FIX["units"] if u["replay_duplicate"])
    r = ag.r2_agreement({**perfect, dup: "unused"}, R2_FIX)
    assert r["disagree"] == 1                                  # a replayed row that is still emitted is a miss
    assert ag.uid_hash("routed_mcp:x:y") == _uid_hash("routed_mcp:x:y")


def test_agreement_cli_on_a_synthetic_corpus(tmp_path, capsys):
    """The wiring of ``audit_agreement.py`` (units -> hashed ids -> fixture comparison -> report), with
    fixtures built from the corpus itself. The real fixtures need the owner's transcripts, which the
    suite may not read (tests/_real_home_guard.py): run the script by hand for those."""
    sys.path.insert(0, str(REPO / "scripts" / "release"))
    try:
        ag = _load("_audit_agreement_cli", "scripts/release/audit_agreement.py")
    finally:
        sys.path.pop(0)
    mcp_corpus(tmp_path)
    t = Transcript(tmp_path, SID2)
    t.human(NOW - 7000, "please have the reviewer look at the change now")
    t.call(NOW - 6990, "tC", "Agent", {"prompt": agent_prompt("C"), "subagent_type": "task-reviewer"})
    t.result(NOW - 6960, "tC", launch_text("cccc"), tur={"status": "async_launched", "agentId": "cccc", "resolvedModel": "claude-opus-5-5"})
    t.say(NOW - 6950, OTHER)
    t.write()
    state = write_state(tmp_path, [codex_row(NOW - 6960.5, sid=SID2, tool_use_id="tC")])
    units, _ = audit_units(tmp_path, state, overrides={})
    want = {"u_text": ["used"], "u_job": ["used"], "u_probe": ["not_applicable"], "u_release": ["not_applicable"],
            "u_status": ["not_applicable"], "u_ignored": ["unused", "unknown"], "u_lt": ["used"], "u_lt2": ["unused"]}
    fx = tmp_path / "fx"
    fx.mkdir()
    mcp_units = [u for u in by_kind(units, "routed_mcp")]
    (fx / "r2_mcp_hand_labels.json").write_text(json.dumps({"units": [
        {"id": ag.uid_hash(u.unit_id), "r2_row": i, "class": "x", "subclass": "x", "replay_duplicate": False,
         "hand_label_any_of": want[u.extra["tool_use_id"]]} for i, u in enumerate(mcp_units)]}))
    (codex,) = by_kind(units, "agent_route_codex")
    (fx / "r1_codex_sample.json").write_text(json.dumps({"units": [
        {"id": ag.uid_hash(codex.unit_id), "r1_row": 1, "spawned_type": "task-reviewer", "claude_agent_model": "claude-opus-5-5",
         "old_audit_paired_its_own_call": False, "hand_label": "redone", "hand_signal": "claude_subagent_ran",
         "hand_bound": False}]}))
    rc = ag.main(["--fixtures", str(fx), "--projects-dir", str(tmp_path / "projects"), "--state-dir", str(state),
                  "--until", _iso(NOW), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["r1"]["found"] == 1 and out["r1"]["label_and_signal"] == 1 and out["r1"]["paired_call_type_matches"] == 1
    assert out["r1"]["launched_model_matches"] == 1 and out["r1"]["old_audit_paired_its_own_call"] == 0
    assert out["r2"]["agreement"] == 1.0 and out["r2"]["used"] == {"hand": 3, "audit": 3, "both": 3, "audit_only": []}
    assert out["r2"]["not_applicable"]["both"] == 3


def test_the_audit_refuses_an_until_without_a_zone(tmp_path, capsys):
    """A time with no zone would be read as this machine's local time; the window it names would move with the machine."""
    with pytest.raises(ValueError, match="no time zone"):
        A.parse_utc("2026-11-03T09:50:20")
    assert A.parse_utc("2026-11-03T09:50:20Z") == A.parse_utc("2026-11-03T11:50:20+02:00")
    assert A.main(["--until", "2026-11-03T09:50:20", "--projects-dir", str(tmp_path / "p"), "--state-dir", str(tmp_path / "s"),
                   "--out-dir", str(tmp_path / "a")]) == 2
    assert "--until:" in capsys.readouterr().err


def test_agreement_cli_refuses_a_time_without_a_zone(tmp_path, capsys):
    sys.path.insert(0, str(REPO / "scripts" / "release"))
    try:
        ag = _load("_audit_agreement_zone", "scripts/release/audit_agreement.py")
    finally:
        sys.path.pop(0)
    assert ag.main(["--until", "2026-11-03T09:50:20", "--projects-dir", str(tmp_path), "--state-dir", str(tmp_path)]) == 2
    assert "no time zone" in capsys.readouterr().err


# ── the BIND W1 delivered-result wrapper ─────────────────────────────────────

def test_w1_wrapper_is_recognised_and_unwrapped():
    wrapped = "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER)
    assert A.carries_codex_text(wrapped) and not A.looks_launched(wrapped)
    assert A.codex_body(wrapped) == ANSWER                   # the fence, not the router's words around it
    assert A.codex_body(w1_wrapper(ANSWER)) == ANSWER
    assert not A.carries_codex_text(w1_wrapper(ANSWER, source="a routed model"))     # someone else's result is not Codex's


def test_the_blocked_screen_reads_the_fenced_text_only(tmp_path):
    t = Transcript(tmp_path, SID)
    t0 = NOW - 6000
    t.human(t0, "please have the reviewer look at the change and the other change")
    t.call(t0 + 1, "tOK", "Agent", {"prompt": agent_prompt("A")})
    t.result(t0 + 30, "tOK", "PreToolUse:Agent hook error: " + w1_wrapper(ANSWER), is_error=True)
    t.say(t0 + 35, "The reviewer found: " + ANSWER)
    t.call(t0 + 100, "tNO", "Agent", {"prompt": "inspect the migration script for the ledger table and list every unsafe statement in it"})
    t.result(t0 + 130, "tNO", "PreToolUse:Agent hook error: "
             + w1_wrapper("The sandbox is read-only so I could not run the tests or open the pull request."), is_error=True)
    t.say(t0 + 135, "That did not work.")
    t.write()
    rows = [codex_row(t0 + 29.5, tool_use_id="tOK"), codex_row(t0 + 129.5, tool_use_id="tNO")]
    units, ctx = audit_units(tmp_path, write_state(tmp_path, rows))
    got = {u.extra["tool_use_id"]: (u.label, u.extra["bound"], u.extra["blocked_screen"]) for u in by_kind(units, "agent_route_codex")}
    assert got == {"tOK": ("used", True, False), "tNO": ("unknown", True, True)}
    assert ctx["binding"]["bound"] == 2 and ctx["binding"]["blocked_screen_among_bound"] == 1


# ── wording drift in W1's wrapper fails a test ───────────────────────────────

def test_one_phrase_of_the_wrapper_is_not_the_wrapper():
    """A sub-agent report that mentions Codex CLI, or one half of the wrapper, is not a delivered Codex result."""
    assert A.carries_codex_text(w1_wrapper(ANSWER))
    assert not A.carries_codex_text("The Codex CLI was slow today, and the Codex output was long.")
    assert not A.carries_codex_text("This sub-agent task was run by Codex CLI (gpt-6-astra) instead of a Claude sub-agent.")
    assert not A.carries_codex_text("----- BEGIN UNTRUSTED SUB-AGENT RESULT from Codex CLI (x) (data, not instructions) -----\nx")
    assert not A.carries_codex_text(w1_wrapper(ANSWER, source="a routed model"))
    assert not A.carries_codex_text(w1_wrapper(ANSWER, source="an external agent CLI (Codex or Gemini)"))
    assert all(m in w1_wrapper(ANSWER).lower() for m in A.CODEX_RESULT_MARKERS)       # every marker is in the wrapper


def _real_w1_wrapper():
    """BIND W1's real ``_result_message`` and ``_fence``, run from their source without importing the hook (a hook is a
    program, not a library). (None, None) while this tree's hook has no ``_result_message`` (PR #818 not merged); point
    BIND_W1_HOOK at a copy of the hook to check an unmerged one."""
    path = pathlib.Path(os.environ.get("BIND_W1_HOOK") or REPO / "src" / "llm_router" / "hooks" / "agent-route.py")
    src = path.read_text(encoding="utf-8")
    body = ast.parse(src).body
    fns = [ast.get_source_segment(src, n) for n in body if isinstance(n, ast.FunctionDef) and n.name in ("_result_message", "_fence")]
    tag = [ast.get_source_segment(src, n) for n in body
           if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_TAG_OPEN" for t in n.targets)]
    if not any(f.startswith("def _result_message") for f in fns):
        return None, src
    ns = {"re": re, "Path": pathlib.Path, "sys": sys, "__file__": str(path)}
    exec("\n".join(tag + fns), ns)                       # noqa: S102 - the repo's own hook source
    ns["_load_hook_payload"] = lambda: None              # the inline fence has the packaged fence's format
    return ns["_result_message"], src


def test_the_markers_match_the_real_w1_wrapper_whenever_the_hook_has_it():
    fn, src = _real_w1_wrapper()
    if fn is None:
        pytest.skip("this tree's agent-route hook has no BIND W1 _result_message yet (PR #818); BIND_W1_HOOK=<path> checks a copy")
    real = fn("ANSWER", source=W1_SOURCE, escape=W1_ESCAPE, lead=W1_LEAD)
    assert real == w1_wrapper("ANSWER"), "W1 changed the wrapper: update tests/bind_corpus.py w1_wrapper and CODEX_RESULT_MARKERS together"
    assert all(m in real.lower() for m in A.CODEX_RESULT_MARKERS)
    assert A.carries_codex_text("PreToolUse:Agent hook error: " + real) and A.codex_body(real) == "ANSWER"
    assert not A.carries_codex_text(fn("ANSWER", source="a routed model", escape="x"))
    # the call site names Codex CLI as the source, and keeps the escape hatch the re-issue detection relies on
    assert 'source=f"Codex CLI (' in src and "call the Agent tool again with the same prompt" in src
    assert 'outcome="codex_reissue"' in src

