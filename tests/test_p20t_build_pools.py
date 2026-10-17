"""P2.0-T pool builder: synthetic transcripts and ledger rows only."""
import hashlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import p20t_build_pools as b  # noqa: E402

MARKER = "unique-private-prompt-marker-p20t"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put(path, value, lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in value) if lines else value)


def entry(role, content, identity=None, **extra):
    return dict(type=role, message=dict(role=role, content=content, id=identity), **extra)


def use(name, **inp):
    return dict(type="tool_use", name=name, input=inp)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    from llm_router.proxy import tool_classes
    from llm_router.secret_scrubber import SECRET_PATTERNS

    monkeypatch.setitem(SECRET_PATTERNS, "test_p20t", re.compile("TEST_SECRET_P20T"))
    fake = types.ModuleType("_p20t_fake_census")
    fake.BASH_UNRESOLVED = "unresolved-bash"
    fake.step_kind_of = lambda row: (row.get("kind", "continuation"), True)
    fake.offline_tool_class = lambda tools: tool_classes.TECHNICAL_OP
    monkeypatch.setitem(sys.modules, "_p20t_fake_census", fake)
    census = tmp_path / "census.py"
    put(census, "from _p20t_fake_census import BASH_UNRESOLVED, step_kind_of, offline_tool_class\n")
    monkeypatch.setitem(b.harness.CONSTANTS_T, "CENSUS_PY_SHA256", sha(census.read_bytes()))
    put(tmp_path / "exec.txt", "exec\n")
    root = tmp_path / "transcripts"
    args = ["--out", str(tmp_path / "out"), "--census-py", str(census),
            "--exec-sessions", str(tmp_path / "exec.txt"), "--proxy-calls", str(tmp_path / "calls.jsonl"),
            "--transcripts-root", str(root), "--until", "2026-10-02T00:00:00Z"]

    def calls(rows):
        put(tmp_path / "calls.jsonl", rows, True)

    def session(name, entries, sub=None):
        put(root / f"project/{name}.jsonl", entries, True)
        for k, v in (sub or {}).items():
            put(root / f"project/{name}/subagents/{k}.jsonl", v, True)

    def row(session_id, msg_id, **kw):
        return dict(ts="2026-10-01T00:00:00Z", session_id=session_id, msg_id=msg_id,
                    prev_tool_class=tool_classes.TECHNICAL_OP, is_main_thread=True, **kw)

    return types.SimpleNamespace(args=args, out=tmp_path / "out", calls=calls, session=session,
                                 row=row, root=root, tmp=tmp_path)


def pool(case):
    return [json.loads(x) for x in (case.out / "pools/tech_op.jsonl").read_text().splitlines()]


def manifest(case):
    return json.loads((case.out / "pools/build_manifest.json").read_text())


def basic(case, **kw):
    case.session("good", [
        entry("user", "ask " + MARKER),
        entry("assistant", [use("Read", file_path="/a/b.py")], "m0"),
        entry("user", [dict(type="tool_result", content="out")]),
        entry("assistant", [dict(type="text", text="thinking")], "m1"),
        entry("assistant", [use("Grep", pattern="  foo  ")], "m1"),
        entry("user", "AFTER"),
    ])


def test_build_strata_no_tier_and_organic_action(case, capsys):
    basic(case)
    case.session("rs", [entry("user", "ask2"), entry("assistant", "stop", "m9")])
    case.session("untag", [entry("user", "ask3"), entry("assistant", "stop", "m8")])
    case.calls([
        case.row("good", "m1", session_kind="organic", served_model="opus"),
        case.row("rs", "m9", session_kind="research"),
        case.row("untag", "m8"),
    ])
    assert b.main(case.args) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and MARKER not in captured.out
    rows = {r["session"]: r for r in pool(case)}
    assert rows["good"]["stratum"] == "org" and rows["rs"]["stratum"] == "ho"
    assert rows["untag"]["stratum"] == "ho"  # untagged is ho (census-exact org only)
    assert all(r["tier"] == "unlabelled" for r in rows.values())  # no tier filter (C1)
    # the assistant message m1 spans two transcript lines: the first tool_use is the action
    assert rows["good"]["organic_action"] == {"tool": "Grep", "target": "foo"}
    assert rows["rs"]["organic_action"] == {"final": True}
    assert rows["good"]["served_model"] == "opus" and rows["good"]["row_stamp"] == "organic"
    assert rows["good"]["messages"][0]["content"] == "ask " + MARKER
    m = manifest(case)
    assert m["counts"]["by_stratum"] == {"org": 1, "ho": 2}
    assert m["counts"]["rowstamp_organic"] == {"org": 1}
    assert m["pools"]["tech_op"] == sha((case.out / "pools/tech_op.jsonl").read_bytes())
    assert set(m["sources"]) == {"census_py", "proxy_calls_copy", "exec_sessions", "secret_patterns"}


def test_rowstamp_organic_resolved_to_research_by_tag_is_ho(case, monkeypatch):
    basic(case)
    from llm_router import session_kind

    monkeypatch.setattr(session_kind, "tag_kind_of",
                        lambda sid: "research" if sid == "good" else None)
    case.calls([case.row("good", "m1", session_kind="organic")])
    assert b.main(case.args) == 0
    only, = pool(case)
    assert only["stratum"] == "ho" and only["row_stamp"] == "organic"
    assert manifest(case)["counts"]["rowstamp_organic"] == {"ho": 1}


def test_sidechain_entries_accepted_only_in_subagent_file(case):
    # main transcript: a sidechain entry is skipped; the sub-agent file is all sidechain
    case.session("good", [entry("user", "main ask"), entry("user", "SIDE", isSidechain=True),
                          entry("assistant", "x", "other")],
                 sub={"agent-1": [
                     entry("user", "sub ask " + MARKER, isSidechain=True),
                     entry("assistant", [use("Read", file_path="/s.py")], "target", isSidechain=True),
                 ]})
    case.calls([case.row("good", "target", session_kind="organic")])
    assert b.main(case.args) == 0
    only, = pool(case)
    assert only["request"] == "sub ask " + MARKER and only["from_subagent_file"] is True
    assert only["organic_action"] == {"tool": "Read", "target": "/s.py"}
    assert manifest(case)["counts"]["from_subagent_file"] == {"org": 1}
    assert "project/good/subagents/agent-1.jsonl" in manifest(case)["transcripts"]


def test_main_transcript_still_skips_sidechain(case):
    case.session("good", [entry("user", "ask"), entry("user", "SIDE", isSidechain=True),
                          entry("assistant", "stop", "target")])
    case.calls([case.row("good", "target", session_kind="organic")])
    assert b.main(case.args) == 0
    only, = pool(case)
    assert [m["content"] for m in only["messages"]] == ["ask"] and only["from_subagent_file"] is False


def test_drops_are_counted(case):
    for name in ("a", "exec", "main", "secret", "big", "agent"):
        case.session(name, [entry("user", "ask " + ("TEST_SECRET_P20T" if name == "secret" else "z")),
                            entry("assistant", [use("Read", file_path="/a")], "t")])
    case.session("a", [entry("user", "ask"), entry("assistant", "s", "t")])
    case.session("agent", [entry("user", "ask"),
                           entry("assistant", [use("SubagentHandback", message="x")], "pre"),
                           entry("assistant", "s", "t")])
    case.session("big", [entry("user", "q" * 4000), entry("assistant", "s", "t")])
    case.session("noask", [entry("assistant", [use("Read", file_path="/a")], "pre"),
                           entry("assistant", "s", "t")])
    rows = [case.row(n, "t", session_kind="organic") for n in ("a", "exec", "main", "secret", "big")]
    rows[2]["is_main_thread"] = False
    rows += [case.row("agent", "t", session_kind="organic"),
             case.row("ghost", "t", session_kind="organic"),
             case.row("noask", "t", session_kind="organic"),
             dict(case.row("a", "t", session_kind="organic"), prev_tool_class="unresolved-bash"),
             case.row("a", "missing", session_kind="organic"),
             dict(case.row("a", "t", session_kind="organic"), kind="side_call"),
             dict(case.row("a", "t", session_kind="organic"), prev_tool_class="web"),
             dict(case.row("a", "t", session_kind="organic"), ts="2020-01-01T00:00:00Z")]
    case.calls(rows)
    args = case.args + ["--subagent-branch", "A"]
    original = b.default_fits
    try:
        b.build.__defaults__ = (lambda m, w: sum(len(x["content"]) for x in m) < 1000,)
        assert b.main(args) == 0
    finally:
        b.build.__defaults__ = (original,)
    excluded = manifest(case)["counts"]["excluded"]
    assert excluded == {"exec_session": 1, "not_main_thread": 1, "secret_pattern": 1,
                        "pack_over_window": 1, "subagent_step": 1, "transcript_missing": 1,
                        "msg_not_found": 1, "not_continuation": 1, "not_technical_op": 1,
                        "bash_unresolved": 1, "no_human_ask": 1}
    assert [r["session"] for r in pool(case)] == ["a"]


def test_pack_over_window_with_the_real_default_fits(case):
    # no stand-in estimator: the gate's own estimate (capability_filter) drops the long item
    from llm_router import capability_filter  # noqa: F401  (the real path, not the fallback)

    case.session("short", [entry("user", "ask"), entry("assistant", "s", "t")])
    case.session("long", [entry("user", "q" * 4 * 40000), entry("assistant", "s", "t")])
    case.calls([case.row("short", "t", session_kind="organic"),
                case.row("long", "t", session_kind="organic")])
    assert b.main(case.args) == 0
    assert [r["session"] for r in pool(case)] == ["short"]
    assert manifest(case)["counts"]["excluded"] == {"pack_over_window": 1}


def test_branch_b_does_not_drop_subagent_tool_evidence(case):
    case.session("agent", [entry("assistant", [use("SubagentHandback", message="x")], "pre"),
                           entry("user", "ask"), entry("assistant", "s", "t")])
    case.calls([case.row("agent", "t", session_kind="organic")])
    assert b.main(case.args) == 0 and len(pool(case)) == 1


def test_branch_a_tests_only_tool_evidence(case):
    # FIX 4: branch A is the gate's tool-name test over the history, nothing about file or
    # parent session; a sub-agent step with no SubagentHandback in its history is NOT dropped.
    case.session("file", [entry("user", "ask"), entry("assistant", "s", "t")])
    case.calls([case.row("file", "t", session_kind="organic")])
    assert b.main(case.args + ["--subagent-branch", "A"]) == 0
    assert len(pool(case)) == 1 and manifest(case)["counts"]["excluded"] == {}


def test_secret_in_organic_action_excludes_item(case):
    case.session("s", [entry("user", "ask"),
                       entry("assistant", [use("Bash", command="echo TEST_SECRET_P20T")], "t")])
    case.calls([case.row("s", "t", session_kind="organic")])
    assert b.main(case.args) == 0
    assert pool(case) == [] and manifest(case)["counts"]["excluded"] == {"secret_pattern": 1}


def test_pin_mismatch_existing_output_and_safe_errors(case, capsys, monkeypatch):
    basic(case)
    case.calls([case.row("good", "m1", session_kind="organic")])
    monkeypatch.setitem(b.harness.CONSTANTS_T, "CENSUS_PY_SHA256", "0" * 64)
    assert b.main(case.args) == 2
    err = capsys.readouterr().err
    assert "pin mismatch" in err and MARKER not in err
    monkeypatch.undo()


def test_output_never_overwritten(case):
    basic(case)
    case.calls([case.row("good", "m1", session_kind="organic")])
    assert b.main(case.args) == 0
    assert b.main(case.args) == 2
    assert oct((case.out / "pools/tech_op.jsonl").stat().st_mode & 0o777) == "0o600"


def test_default_fits_fallback_estimator():
    short = [dict(role="user", content="x" * 100)]
    long = [dict(role="user", content="x" * 4 * 40000)]
    assert b.default_fits(short, 32768) is True
    assert b.default_fits(long, 32768) is False
