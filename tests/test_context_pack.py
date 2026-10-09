"""PLAN v16 P1.1 tasks 1, 2 and 4 (core): the context-pack builder.

Every fixture here is synthetic text. No real transcript is read.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import time
from pathlib import Path

import pytest

from llm_router import context_pack as cp
from llm_router.token_budget import estimate_tokens

GOLDEN = Path(__file__).parent / "fixtures" / "context_pack" / "golden_30.jsonl"
REQUEST = "synthetic final request"


def _build(**kw):
    kw.setdefault("target_window", 100)
    kw.setdefault("door", "hook")
    kw.setdefault("transcript_path", str(GOLDEN))
    return cp.build_pack(kw.pop("request", REQUEST), **kw)


# --------------------------------------------------------------------------- golden


def test_golden_fixture_has_30_messages_and_merges_split_assistant_entries():
    msgs = cp.messages_for("hook", transcript_path=str(GOLDEN))
    # 29 generated turns + the final request; the summary line, the isMeta
    # caveat and the thinking blocks are not conversation.
    assert len(msgs) == 30
    assert msgs[1] == {
        "role": "assistant",
        "content": 'synthetic answer 1\n[tool_use: Read] {"file_path": "/synthetic/f1.py"}',
    }
    assert all("synthetic thought" not in m["content"] for m in msgs)
    assert all("meta caveat" not in m["content"] for m in msgs)


def test_golden_recent_is_last_seven_verbatim_with_tool_blocks():
    pack = _build()
    elided = "[tool_result] " + "L" * 1500 + "[…truncated 3000 chars…]" + "L" * 1500
    assert pack.mode == "pack"
    assert pack.recent == [
        {"role": "user", "content": "[tool_result (error)] synthetic error 22\n[image]"},
        {"role": "assistant", "content": '[tool_use: Bash] {"command": "echo synthetic 23"}'},
        {"role": "user", "content": "synthetic question number 24"},
        {"role": "assistant",
         "content": 'synthetic answer 25\n[tool_use: Read] {"file_path": "/synthetic/f25.py"}'},
        {"role": "user", "content": elided},
        {"role": "assistant", "content": "synthetic reply 27"},
        {"role": "user", "content": "[tool_result (error)] synthetic error 28\n[image]"},
    ]
    assert pack.request == REQUEST
    assert pack.summary is None  # P1.2 not built


def test_trailing_copy_of_the_request_is_dropped():
    pack = _build(target_window=10**6)
    assert pack.mode == "full"
    assert len(pack.recent) == 29
    assert pack.recent[-1]["content"] != REQUEST


# --------------------------------------------------------------------------- full mode


def test_full_mode_carries_the_whole_conversation_unelided():
    pack = _build(target_window=10**6)
    assert pack.mode == "full"
    assert pack.recent[0] == {"role": "user", "content": "synthetic question number 0"}
    assert "[tool_result] " + "L" * 6000 in [m["content"] for m in pack.recent]
    assert not any("truncated" in m["content"] for m in pack.recent)


def test_full_mode_threshold_is_point_eight_of_the_window():
    msgs = [{"role": "user", "content": "u" * 400}, {"role": "assistant", "content": "a" * 400}]
    whole = 100 + 100 + estimate_tokens("r" * 40)  # conversation + request, no instructions
    window_at = int(whole / 0.8) + 1                 # whole <= 0.8 * window
    at = cp.build_pack("r" * 40, msgs, target_window=window_at, door="proxy")
    below = cp.build_pack("r" * 40, msgs, target_window=int(whole / 0.8) - 2, door="proxy")
    assert at.mode == "full" and below.mode == "pack"


def test_full_mode_boundary_is_inclusive_at_exactly_point_eight():
    # 400 + 360 chars -> 100 + 90 tokens, request 40 chars -> 10: whole = 200.
    msgs = [{"role": "user", "content": "u" * 400}, {"role": "assistant", "content": "a" * 360}]
    assert cp.build_pack("r" * 40, msgs, target_window=250, door="proxy").mode == "full"
    assert cp.build_pack("r" * 40, msgs, target_window=249, door="proxy").mode == "pack"


def test_instructions_count_toward_the_full_mode_check(tmp_path):
    msgs = [{"role": "user", "content": "u" * 400}, {"role": "assistant", "content": "a" * 360}]
    bare = cp.build_pack("r" * 40, msgs, project_root=str(tmp_path), target_window=250,
                         door="proxy")
    assert bare.mode == "full" and bare.instructions == ""
    (tmp_path / "CLAUDE.md").write_text("# Synthetic heading\n- synthetic rule\n")
    ruled = cp.build_pack("r" * 40, msgs, project_root=str(tmp_path), target_window=250,
                          door="proxy")
    assert ruled.instructions and ruled.mode == "pack"


def test_no_conversation_source_never_claims_full():
    for door in ("hook", "agent", "codex", "proxy"):
        assert cp.build_pack("q", target_window=10**6, door=door).mode == "pack"
    assert cp.build_pack("q", [], target_window=10**6, door="proxy").mode == "full"


def test_invalid_window_never_claims_full():
    for window in (0, -5, None, "x", float("inf"), float("nan")):
        pack = cp.build_pack("q", [{"role": "user", "content": "a"}], target_window=window,
                             door="proxy")
        assert pack.mode == "pack" and pack.request == "q"


# --------------------------------------------------------------------------- elision


def test_tool_block_elision_boundary():
    assert cp.elide("x" * 4000) == "x" * 4000
    out = cp.elide("h" * 1500 + "m" * 1001 + "t" * 1500)
    assert out == "h" * 1500 + "[…truncated 1001 chars…]" + "t" * 1500


def test_text_blocks_are_never_elided_in_pack_mode():
    long_text = "w" * 9000
    pack = cp.build_pack("q", [{"role": "user", "content": long_text}], target_window=10,
                         door="proxy")
    assert pack.mode == "pack"
    assert pack.recent == [{"role": "user", "content": long_text}]


def test_tool_use_input_over_cap_is_elided_in_pack_mode():
    msgs = [{"role": "assistant",
             "content": [{"type": "tool_use", "name": "Write", "input": "c" * 9000}]}]
    pack = cp.build_pack("q", msgs, target_window=10, door="proxy")
    assert pack.recent[0]["content"].startswith("[tool_use: Write] " + "c" * 1500 + "[…truncated")


# --------------------------------------------------------------------------- request


def test_bytes_request_is_decoded_not_reprd():
    assert cp.build_pack(b"synthetic", [], target_window=10, door="proxy").request == "synthetic"


def test_request_never_truncated():
    request = "  leading space\n" + "R" * 250_000 + "\ntrailing  "
    pack = cp.build_pack(request, transcript_path=str(GOLDEN), target_window=10, door="hook")
    assert pack.request == request
    assert pack.mode == "pack"
    assert pack.tokens >= estimate_tokens(request)


# --------------------------------------------------------------------------- 2 MB tail


def _write_transcript(path: Path, target_bytes: int) -> int:
    """Synthetic CC transcript of about ``target_bytes``; returns message count."""
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        size = 0
        while size < target_bytes:
            role = "user" if n % 2 == 0 else "assistant"
            line = json.dumps({"type": role, "message": {
                "role": role, "id": f"m{n}" if role == "assistant" else None,
                "content": f"synthetic turn {n} " + "p" * 900}}) + "\n"
            fh.write(line)
            size += len(line.encode())
            n += 1
    return n


@pytest.fixture(scope="module")
def big_transcript(tmp_path_factory):
    path = tmp_path_factory.mktemp("cp") / "big.jsonl"
    n = _write_transcript(path, 10 * 1024 * 1024)
    return path, n


class _CountingFile:
    def __init__(self, fh, counter):
        self._fh, self._counter = fh, counter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def seek(self, *a):
        return self._fh.seek(*a)

    def tell(self):
        return self._fh.tell()

    def read(self, *a):
        data = self._fh.read(*a)
        self._counter.append(len(data))
        return data


def test_only_the_last_2mb_of_a_transcript_is_read(big_transcript, monkeypatch):
    path, _ = big_transcript
    assert path.stat().st_size > 5 * cp.TAIL_BYTES
    reads: list[int] = []
    monkeypatch.setattr(cp, "_open", lambda p, mode: _CountingFile(open(p, mode), reads))
    pack = cp.build_pack("q", transcript_path=str(path), target_window=10**9, door="hook")
    assert reads and sum(reads) <= cp.TAIL_BYTES
    # An incomplete conversation can never be "full", whatever the window.
    assert pack.mode == "pack"


def test_large_transcript_recent_is_the_newest_seven(big_transcript):
    path, n = big_transcript
    pack = cp.build_pack("q", transcript_path=str(path), target_window=10, door="hook")
    assert [m["content"].split(" p")[0] for m in pack.recent] == [
        f"synthetic turn {i}" for i in range(n - 7, n)]


def _cc(role, content, mid=None):
    msg = {"role": role, "content": content}
    if mid:
        msg["id"] = mid
    return json.dumps({"type": role, "message": msg}) + "\n"


def test_reverse_tail_parse_merges_split_entries_like_the_forward_parse(tmp_path):
    tail = [_cc("user", "synthetic ask"),
            _cc("assistant", [{"type": "text", "text": "part one"}], "mX"),
            _cc("assistant", [{"type": "tool_use", "name": "A", "input": {}}], "mX"),
            _cc("assistant", [{"type": "tool_use", "name": "B", "input": {}}], "mX"),
            _cc("user", [{"type": "tool_result", "content": "ra"}]),
            _cc("user", [{"type": "tool_result", "content": "rb"}]),
            _cc("assistant", [{"type": "text", "text": "done"}], "mY")]
    small = tmp_path / "small.jsonl"
    small.write_text("".join(tail))
    big = tmp_path / "big.jsonl"
    _write_transcript(big, 3 * 1024 * 1024)
    with open(big, "a") as fh:
        fh.write("".join(tail))
    expected = [
        {"role": "user", "content": "synthetic ask"},
        {"role": "assistant",
         "content": "part one\n[tool_use: A] {}\n[tool_use: B] {}"},
        {"role": "user", "content": "[tool_result] ra\n[tool_result] rb"},
        {"role": "assistant", "content": "done"},
    ]
    assert cp.messages_for("hook", transcript_path=str(small)) == expected
    assert cp.messages_for("hook", transcript_path=str(big))[-4:] == expected


def test_tail_window_with_few_messages_drops_the_possibly_partial_oldest(tmp_path):
    path = tmp_path / "few.jsonl"
    with open(path, "w") as fh:
        fh.write(_cc("assistant", [{"type": "text", "text": "o" * (cp.TAIL_BYTES)}], "m0"))
        fh.write(_cc("assistant", [{"type": "text", "text": "tail of m0"}], "m0"))
        fh.write(_cc("user", "synthetic last ask"))
    assert cp.messages_for("hook", transcript_path=str(path)) == [
        {"role": "user", "content": "synthetic last ask"}]


def test_a_whole_oldest_message_at_the_window_start_is_kept(tmp_path):
    path = tmp_path / "huge_result.jsonl"
    with open(path, "w") as fh:
        fh.write(_cc("user", [{"type": "tool_result", "content": "r" * (cp.TAIL_BYTES + 200_000)}]))
        fh.write(_cc("user", "synthetic follow-up"))
        fh.write(_cc("assistant", [{"type": "text", "text": "synthetic reply"}], "mZ"))
    assert cp.messages_for("hook", transcript_path=str(path)) == [
        {"role": "user", "content": "synthetic follow-up"},
        {"role": "assistant", "content": "synthetic reply"}]


def test_reverse_path_drops_a_trailing_request_copy_and_keeps_seven(big_transcript, tmp_path):
    src, n = big_transcript
    path = tmp_path / "with_request.jsonl"
    path.write_bytes(src.read_bytes() + _cc("user", "synthetic current request").encode())
    pack = cp.build_pack("synthetic current request", transcript_path=str(path),
                         target_window=10, door="hook")
    assert [m["content"].split(" p")[0] for m in pack.recent] == [
        f"synthetic turn {i}" for i in range(n - 7, n)]


def test_partial_first_line_in_the_tail_window_is_dropped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"broken": "' + b"z" * 100 + b'"}\n{"ok": 1}\n')
    data, complete = cp._read_tail(path, limit=20)
    assert not complete and data == b'{"ok": 1}\n'


# --------------------------------------------------------------------------- instructions


def test_instructions_digest_keeps_headings_and_rule_lines(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Title\nprose that is not a rule\n- rule one\n1. numbered rule\n"
        "```\n- inside a fence\n```\n**Bold lead.** text\n## Section\n")
    pack = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert pack.instructions.splitlines() == [
        "[CLAUDE.md]", "# Title", "- rule one", "1. numbered rule",
        "**Bold lead.** text", "## Section"]


def test_instructions_digest_is_capped_at_1500_tokens(tmp_path):
    (tmp_path / "AGENTS.md").write_text("".join(f"- synthetic rule {i:05d}\n" for i in range(5000)))
    pack = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert pack.instructions.startswith("[AGENTS.md]\n- synthetic rule 00000")
    assert 1400 <= estimate_tokens(pack.instructions) <= cp.INSTRUCTIONS_MAX_TOKENS


def test_instructions_digest_is_cached_by_file_hash(tmp_path, monkeypatch):
    calls: list[int] = []
    real = cp._digest_text
    monkeypatch.setattr(cp, "_digest_text", lambda t: calls.append(1) or real(t))
    monkeypatch.setattr(cp, "_DIGEST_CACHE", {})
    f = tmp_path / "CLAUDE.md"
    f.write_text("# A\n- one\n")
    first = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    second = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert len(calls) == 1 and first.instructions == second.instructions
    f.write_text("# A\n- two\n")
    third = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert len(calls) == 2 and "- two" in third.instructions


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """HOME pointed at a fresh tmp dir, so no test can touch a real ~/.claude."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# Global\n- synthetic private rule\n")
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home


def test_private_global_claude_md_is_not_read(tmp_path, fake_home):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# Project\n")
    with_root = cp.build_pack("q", [], project_root=str(proj), target_window=10, door="proxy")
    without = cp.build_pack("q", [], target_window=10, door="proxy")
    assert with_root.instructions.splitlines() == ["[CLAUDE.md]", "# Project"]
    assert without.instructions == ""


def test_project_root_at_home_does_not_reach_the_private_file(fake_home):
    pack = cp.build_pack("q", [], project_root=str(fake_home), target_window=10, door="proxy")
    assert "synthetic private rule" not in pack.instructions
    assert pack.instructions == ""


def test_project_root_at_home_is_no_project(fake_home, monkeypatch):
    (fake_home / "AGENTS.md").write_text("# Copy\n- synthetic copied private rule\n")
    (fake_home / "CLAUDE.md").write_text("# Home\n- synthetic home rule\n")
    subprocess.run(["git", "init", "-q", str(fake_home)], check=True)
    called = []
    import llm_router.context_injection as ci
    monkeypatch.setattr(ci, "inject", lambda *a, **k: called.append(1) or "x\n\nq")
    for root in (str(fake_home), str(fake_home / "." / ".claude" / "..")):
        pack = cp.build_pack("q", [], project_root=root, target_window=10, door="proxy")
        assert pack.instructions == "" and pack.project == ""
    assert called == []  # the project section is not even gathered


def test_a_project_file_symlinked_to_the_private_file_is_not_read(tmp_path, fake_home):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").symlink_to(fake_home / ".claude" / "CLAUDE.md")
    (proj / "AGENTS.md").write_text("# Agents\n- synthetic project rule\n")
    pack = cp.build_pack("q", [], project_root=str(proj), target_window=10, door="proxy")
    assert "synthetic private rule" not in pack.instructions
    assert pack.instructions.splitlines() == ["[AGENTS.md]", "# Agents", "- synthetic project rule"]


# --------------------------------------------------------------------------- hash


def test_stable_hash_is_deterministic():
    a = _build()
    b = _build()
    assert a.stable_hash == b.stable_hash and len(a.stable_hash) == 64
    assert a == b
    assert _build(request="other").stable_hash != a.stable_hash
    assert _build(target_window=10**6).stable_hash != a.stable_hash  # full vs pack


def test_stable_hash_covers_project_and_instructions(tmp_path, monkeypatch):
    base = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    (tmp_path / "CLAUDE.md").write_text("# Synthetic\n")
    ruled = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert ruled.instructions and ruled.stable_hash != base.stable_hash
    monkeypatch.setattr(cp, "_project", lambda *a: "synthetic project A")
    a = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    monkeypatch.setattr(cp, "_project", lambda *a: "synthetic project B")
    b = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert a.project != b.project and a.stable_hash != b.stable_hash


# --------------------------------------------------------------------------- fail open


def test_project_and_instruction_errors_yield_empty_sections(tmp_path, monkeypatch):
    import llm_router.context_injection as ci

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    (tmp_path / "CLAUDE.md").write_text("# x\n")
    monkeypatch.setattr(ci, "inject", boom)
    monkeypatch.setattr(cp, "_digest_file", boom)
    pack = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert pack.project == "" and pack.instructions == "" and pack.request == "q"


def test_unreadable_transcript_yields_empty_conversation(tmp_path):
    pack = cp.build_pack("q", transcript_path=str(tmp_path / "missing.jsonl"),
                         target_window=10**6, door="hook")
    assert pack.recent == [] and pack.mode == "pack" and pack.request == "q"


def test_garbage_messages_do_not_raise():
    pack = cp.build_pack("q", [None, 3, "x", {"role": 5}, {"type": "unknown"}],
                         target_window=10, door="proxy")
    assert pack.recent == [] and pack.request == "q"


# --------------------------------------------------------------------------- project


def test_project_is_empty_without_a_root():
    assert _build().project == ""


def test_project_carries_repo_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_AUTOINDEX", "off")
    subprocess.run(["git", "init", "-q", "-b", "synthetic-branch", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    pack = cp.build_pack("q", [], project_root=str(tmp_path), target_window=10, door="proxy")
    assert "<repo_state>" in pack.project
    assert "branch: synthetic-branch" in pack.project
    assert "uncommitted: 2" in pack.project
    assert not pack.project.endswith("q")


# --------------------------------------------------------------------------- formats / doors


def test_openai_chat_messages_render_tool_calls_and_tool_results():
    msgs = [
        {"role": "system", "content": "host system prompt"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    assert cp.messages_for("gateway", messages=msgs) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": '[tool_use: f] {"a":1}'},
        {"role": "user", "content": "[tool_result] result"},
    ]


def test_responses_api_items_render():
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "name": "g", "arguments": "{}", "call_id": "x"},
        {"type": "function_call_output", "call_id": "x", "output": "done"},
        {"type": "reasoning", "summary": []},
    ]
    assert cp.messages_for("sdk", messages=items) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "[tool_use: g] {}"},
        {"role": "user", "content": "[tool_result] done"},
    ]


def test_codex_rollout_file(tmp_path):
    lines = [
        {"type": "session_meta", "payload": {"id": "s"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "synthetic codex ask"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                              "arguments": '{"cmd":"ls"}', "call_id": "1"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "1",
                                              "output": "a.txt"}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "ok"}]}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert cp.messages_for("codex", transcript_path=str(path)) == [
        {"role": "user", "content": "synthetic codex ask"},
        {"role": "assistant", "content": '[tool_use: shell] {"cmd":"ls"}'},
        {"role": "user", "content": "[tool_result] a.txt"},
        {"role": "assistant", "content": "ok"},
    ]


def test_mcp_resolves_the_session_transcript_then_falls_back(tmp_path):
    projects = tmp_path / "projects"
    (projects / "proj-a").mkdir(parents=True)
    (projects / "proj-a" / "sess-1.jsonl").write_text(GOLDEN.read_text())
    got = cp.messages_for("mcp", session_id="sess-1", projects_dir=projects)
    assert len(got) == 30
    assert cp.messages_for("mcp", session_id="sess-2", projects_dir=projects,
                           context="synthetic caller context") == [
        {"role": "user", "content": "synthetic caller context"}]
    assert cp.messages_for("mcp", session_id="../etc", projects_dir=projects) == []


def test_mcp_caller_context_never_claims_full(tmp_path):
    pack = cp.build_pack("q", session_id="no-such-session", context="synthetic caller context",
                         target_window=10**6, door="mcp")
    assert pack.mode == "pack"
    assert pack.recent == [{"role": "user", "content": "synthetic caller context"}]


def test_mcp_transcript_wins_over_caller_context(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    (projects / "p" / "sess-9.jsonl").write_text(GOLDEN.read_text())
    import llm_router.northstar as ns
    monkeypatch.setattr(ns, "claude_projects_dir", lambda: projects)
    pack = cp.build_pack(REQUEST, session_id="sess-9", context="synthetic caller context",
                         target_window=10**6, door="mcp")
    assert pack.mode == "full" and len(pack.recent) == 29


@pytest.mark.parametrize("mode,provider,kept", [
    ("all", "openai", True),
    ("local", "openai", False),
    ("local", "ollama", True),
    ("local", "local", True),
    ("off", "ollama", False),
])
def test_target_provider_applies_the_session_privacy_rule(monkeypatch, mode, provider, kept):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", mode)
    pack = cp.build_pack(REQUEST, transcript_path=str(GOLDEN), target_window=10**6,
                         door="hook", target_provider=provider)
    assert (len(pack.recent) == 29) is kept
    if not kept:
        assert pack.recent == [] and pack.mode == "pack" and pack.request == REQUEST


def test_no_target_provider_means_no_gate(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    assert len(cp.build_pack(REQUEST, transcript_path=str(GOLDEN), target_window=10**6,
                             door="hook").recent) == 29


def test_unknown_door_is_rejected():
    with pytest.raises(ValueError):
        cp.build_pack("q", [], target_window=10, door="hooks")
    with pytest.raises(ValueError):
        cp.messages_for("unknown", messages=[])


def test_every_door_name_is_accepted():
    for door in cp.DOORS:
        pack = cp.build_pack("q", [{"role": "user", "content": "a"}], target_window=10**4,
                             door=door)
        assert pack.request == "q"
    assert len(cp.DOORS) == 7


def test_generalized_extractor_matches_the_hook_when_tools_are_dropped():
    src = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
    spec = importlib.util.spec_from_file_location("auto_route_cp", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for line in GOLDEN.read_text().splitlines():
        content = (json.loads(line).get("message") or {}).get("content")
        assert cp.extract_turn_text(content, keep_tools=False) == mod._extract_turn_text(content)


# --------------------------------------------------------------------------- timing


@pytest.mark.timing
def test_build_p95_under_50ms_on_a_10mb_transcript(big_transcript):
    path, _ = big_transcript
    samples = []
    for _ in range(30):
        t0 = time.perf_counter()
        cp.build_pack("q", transcript_path=str(path), target_window=200_000, door="hook")
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[math.ceil(0.95 * len(samples)) - 1]
    assert p95 <= 50, f"p95={p95:.1f} ms over n={len(samples)}: {[round(x,1) for x in samples]}"


@pytest.mark.timing
def test_build_p95_under_50ms_in_the_shape_doors_use(big_transcript, tmp_path):
    """10 MB transcript + a real git project root + an instructions file, with
    the semantic layer at its shipped default (no env override)."""
    path, _ = big_transcript
    root = tmp_path / "proj"
    root.mkdir()
    git = ["git", "-c", "user.name=s", "-c", "user.email=s@s", "-C", str(root)]
    subprocess.run(["git", "init", "-q", "-b", "synthetic-branch", str(root)], check=True)
    (root / "CLAUDE.md").write_text("# Synthetic\n- synthetic rule\n")
    (root / "a.py").write_text("def synthetic_fn():\n    return 1\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "synthetic"], check=True)
    (root / "b.txt").write_text("x")
    # An established checkout, not one created this second: files written in
    # the same second as the index are "racily clean", so every `git status`
    # rewrites the index until that second has passed.
    old = time.time() - 60
    for f in ("CLAUDE.md", "a.py", "b.txt"):
        os.utime(root / f, (old, old))
    subprocess.run([*git, "status", "--porcelain"], check=True, capture_output=True)

    def once():
        t0 = time.perf_counter()
        pack = cp.build_pack("synthetic question", transcript_path=str(path),
                             project_root=str(root), target_window=200_000, door="hook")
        return (time.perf_counter() - t0) * 1000, pack

    _, pack = once()  # first build: imports and the first git read
    assert "branch: synthetic-branch" in pack.project and pack.instructions
    samples = sorted(once()[0] for _ in range(30))
    p95 = samples[math.ceil(0.95 * len(samples)) - 1]
    assert p95 <= 50, f"p95={p95:.1f} ms over n={len(samples)}: {[round(x,1) for x in samples]}"
