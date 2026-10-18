"""D-171 L1: the auto-route hook's per-prompt cost no longer grows with the session.

Measured before the change (origin/main b81e82b3, synthetic Claude Code-shaped transcripts,
cold hook runs): the hook read and JSON-parsed the WHOLE transcript twice per prompt
(``session_io`` 22 ms at 1 MB, 167 ms at 50 MB), and every 10th prompt scanned the whole,
unbounded ``auto-route-debug.log`` (81 ms at 50 MB). Live rows carried no ``router_added_ms``.

These tests count what the hook reads instead of timing it (wall-clock on a shared
machine is noise): bytes read from the transcript, opens of the transcript, offsets the
draft counter scans from. Fixtures are generated here, made-up text only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from llm_router import capped_log, hook_latency, routing_report, session_store

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
HOOK = SRC / "llm_router" / "hooks" / "auto-route.py"

_WORDS = ("alpha beta gamma delta parser config widget handler cache index route token "
          "model session buffer stream queue worker retry timeout schema column table").split()


def _words(n: int, seed: int) -> str:
    return " ".join(_WORDS[(seed * 7 + i * 13) % len(_WORDS)] for i in range(n))


def _line(role: str, content) -> str:
    return json.dumps({"type": role, "sessionId": "l1-fixture", "message": {"role": role, "content": content}})


def _write_transcript(path: Path, megabytes: float, tail_turns=()) -> Path:
    """A Claude Code-shaped JSONL of about ``megabytes`` MB (prose turns, tool_use, big
    tool_result lines), then ``tail_turns`` = [(role, text), ...] as the last lines."""
    target = int(megabytes * 1024 * 1024)
    size = 0
    i = 0
    with path.open("w", encoding="utf-8") as fh:
        while size < target:
            rows = [
                _line("user", _words(40, i)),
                _line("assistant", [{"type": "text", "text": _words(80, i + 1)},
                                    {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}]),
                _line("user", [{"type": "tool_result", "tool_use_id": f"t{i}", "content": _words(1500, i + 2)}]),
                _line("assistant", [{"type": "text", "text": f"answer {i} " + _words(60, i + 3)}]),
            ]
            for r in rows:
                fh.write(r + "\n")
                size += len(r) + 1
            i += 1
        for role, text in tail_turns:
            fh.write(_line(role, [{"type": "text", "text": text}] if role == "assistant" else text) + "\n")
    return path


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("l1_auto_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["l1_auto_route"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    """The 20 MB and 50 MB fixtures, generated once per run."""
    d = tmp_path_factory.mktemp("l1-big")
    tail = [("user", "previous question"), ("assistant", "FINAL-ANSWER the previous reply"),
            ("user", "current prompt text")]
    return {mb: _write_transcript(d / f"t{mb}.jsonl", mb, tail) for mb in (20, 50)}


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "l1-scope")
    yield


# ── the reference: the code this change replaced, verbatim in behaviour ──────────────

def _old_full_read_turns(transcript_path: str) -> list[dict]:
    """What ``_load_conversation_history`` did before: readlines() + json.loads of every line."""
    turns = []
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
        else:
            text = ""
        text = text.strip()
        if text:
            turns.append({"role": role, "content": text})
    return turns


class _CountingOpen:
    """Replaces the hook module's ``open``: counts opens of one path and the bytes read."""

    def __init__(self, target: str):
        self.target = str(target)
        self.opens = 0
        self.bytes = 0

    def __call__(self, file, *a, **k):
        fh = open(file, *a, **k)  # the real builtin: this module's own `open` is not patched
        if str(file) != self.target:
            return fh
        self.opens += 1
        outer = self

        class _P:
            def __enter__(self_):
                fh.__enter__()
                return self_

            def __exit__(self_, *e):
                return fh.__exit__(*e)

            def read(self_, *n):
                data = fh.read(*n)
                outer.bytes += len(data)
                return data

            def readlines(self_, *n):
                data = fh.readlines(*n)
                outer.bytes += sum(len(x) for x in data)
                return data

            def __iter__(self_):
                for x in fh:
                    outer.bytes += len(x)
                    yield x

            def __getattr__(self_, name):
                return getattr(fh, name)

        return _P()


# ── A1: tail read, one parse ───────────────────────────────────────────────────────

@pytest.mark.parametrize("mb", [20, 50])
def test_bytes_read_from_a_big_transcript_are_bounded(hook, big, monkeypatch, mb):
    counter = _CountingOpen(big[mb])
    monkeypatch.setattr(hook, "open", counter, raising=False)
    turns = hook._read_cc_turns(str(big[mb]))
    size = big[mb].stat().st_size
    assert size > mb * 1024 * 1024 * 0.99, "fixture is not the size the test names"
    assert counter.opens == 1
    # prose-sized tail: one window (256 KB) is enough; allow the doubling twice
    assert 0 < counter.bytes <= 4 * hook._TRANSCRIPT_TAIL_BYTES, (counter.bytes, size)
    assert counter.bytes < size / 20
    assert turns[-1] == {"role": "user", "content": "current prompt text"}
    assert len(turns) == hook._CC_TURNS_NEEDED


def test_bytes_read_do_not_grow_with_the_file(hook, big, monkeypatch):
    got = {}
    for mb in (20, 50):
        counter = _CountingOpen(big[mb])
        monkeypatch.setattr(hook, "open", counter, raising=False)
        hook._read_cc_turns(str(big[mb]))
        got[mb] = counter.bytes
    assert got[50] <= got[20] * 1.5, got  # relative, and deterministic: bytes, not time


@pytest.mark.parametrize("mb", [20, 50])
def test_tail_turns_equal_the_full_parse_on_big_fixtures(hook, big, mb):
    full = _old_full_read_turns(str(big[mb]))
    for need in (1, 2, 7, 12):
        assert hook._read_cc_turns(str(big[mb]), need) == full[-need:], need


def test_a_tool_heavy_tail_grows_the_window_and_still_matches(hook, tmp_path, monkeypatch):
    """No prose turn inside the first window: the window must double until it finds some."""
    p = tmp_path / "heavy.jsonl"
    with p.open("w") as fh:
        for i in range(6):
            fh.write(_line("user", f"early question {i}") + "\n")
            fh.write(_line("assistant", [{"type": "text", "text": f"early answer {i}"}]) + "\n")
        for i in range(8):  # ~1.3 MB of tool results with no text turn in them
            fh.write(_line("user", [{"type": "tool_result", "tool_use_id": f"x{i}",
                                     "content": _words(30000, i)}]) + "\n")
    counter = _CountingOpen(p)
    monkeypatch.setattr(hook, "open", counter, raising=False)
    got = hook._read_cc_turns(str(p), 7)
    assert got == _old_full_read_turns(str(p))[-7:]
    assert counter.bytes > hook._TRANSCRIPT_TAIL_BYTES  # the window did grow


def test_a_tool_heavy_tail_scans_each_byte_once_and_stops_at_the_cap(hook, tmp_path, monkeypatch):
    """F64: ~100 MB of tool results after the last prose turns used to be re-read and re-parsed at
    every window doubling (247 ms session_io at a 100 MB tool-heavy transcript, measured). Now the
    scan is one backward pass over at most ``_TRANSCRIPT_SCAN_CAP_BYTES``."""
    monkeypatch.setattr(hook, "_TRANSCRIPT_SCAN_CAP_BYTES", 8 * 1024 * 1024)  # small cap keeps it fast
    p = tmp_path / "huge-tools.jsonl"
    with p.open("w") as fh:
        for i in range(6):
            fh.write(_line("user", f"early question {i}") + "\n")
            fh.write(_line("assistant", [{"type": "text", "text": f"early answer {i}"}]) + "\n")
        row = _line("user", [{"type": "tool_result", "tool_use_id": "x", "content": "z" * 60_000}]) + "\n"
        for _ in range(500):  # ~30 MB, far more than the cap
            fh.write(row)
    assert p.stat().st_size > 3 * hook._TRANSCRIPT_SCAN_CAP_BYTES
    counter = _CountingOpen(p)
    monkeypatch.setattr(hook, "open", counter, raising=False)
    assert hook._read_cc_turns(str(p), 7) == []  # the prose is beyond the cap: dropped, not waited for
    assert counter.opens == 1
    assert counter.bytes <= hook._TRANSCRIPT_SCAN_CAP_BYTES, counter.bytes


def test_each_byte_is_read_once_when_the_window_grows(hook, tmp_path, monkeypatch):
    p = tmp_path / "grow.jsonl"
    with p.open("w") as fh:
        for i in range(6):
            fh.write(_line("user", f"early question {i}") + "\n")
            fh.write(_line("assistant", [{"type": "text", "text": f"early answer {i}"}]) + "\n")
        for i in range(8):
            fh.write(_line("user", [{"type": "tool_result", "tool_use_id": f"x{i}",
                                     "content": _words(30000, i)}]) + "\n")
    counter = _CountingOpen(p)
    monkeypatch.setattr(hook, "open", counter, raising=False)
    got = hook._read_cc_turns(str(p), 7)
    assert got == _old_full_read_turns(str(p))[-7:]
    assert counter.bytes <= p.stat().st_size  # the old restart read the first windows again


def _fuzz_line(rng: random.Random, i: int) -> str:
    """Every shape the skip-before-parse filter must not get wrong, in either JSON spacing."""
    kind = rng.randrange(9)
    if kind == 0:
        c = f"plain user text {i}"
    elif kind == 1:
        c = f'user text quoting "tool_result" and "type": "tool_use" and "image" {i}'
    elif kind == 2:
        c = [{"type": "text", "text": f"assistant text {i}"}]
    elif kind == 3:
        c = [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "ls"}}]
    elif kind == 4:
        c = [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "r" * rng.randrange(1, 400)}]
    elif kind == 5:  # text and tool blocks in ONE message: the text must survive
        c = [{"type": "tool_result", "tool_use_id": "q", "content": "r"}, {"type": "text", "text": f"mixed {i}"}]
    elif kind == 6:
        c = [{"type": "image", "source": {"type": "base64", "data": "A" * rng.randrange(1, 300)}}]
    elif kind == 7:  # tool_result whose own content holds a text block
        c = [{"type": "tool_result", "tool_use_id": "q", "content": [{"type": "text", "text": f"inner {i}"}]}]
    else:
        c = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": f"after thinking {i}"}]
    role = rng.choice(("user", "assistant"))
    sep = rng.choice(((",", ":"), (", ", ": ")))
    return json.dumps({"type": role, "message": {"role": role, "content": c}}, separators=sep)


@pytest.mark.parametrize("seed", range(6))
def test_the_skip_before_parse_filter_and_chunk_joins_match_the_full_parse(hook, tmp_path, monkeypatch, seed):
    """Tiny chunks (so lines straddle many chunk edges) over fuzzed lines equal the old full read."""
    rng = random.Random(seed)
    p = tmp_path / f"fuzz{seed}.jsonl"
    lines = [_fuzz_line(rng, i) for i in range(rng.randrange(1, 400))]
    p.write_text("\n".join(lines) + rng.choice(("", "\n")), encoding="utf-8")
    monkeypatch.setattr(hook, "_TRANSCRIPT_TAIL_BYTES", rng.choice((64, 200, 1000)))
    full = _old_full_read_turns(str(p))
    for need in (1, 3, 7, 50):
        assert hook._read_cc_turns(str(p), need) == full[-need:], (seed, need)


@pytest.mark.parametrize("case", ["empty", "short", "no_trailing_newline", "bad_json", "crlf", "missing"])
def test_edge_shapes_match_the_old_full_read(hook, tmp_path, case):
    p = tmp_path / "e.jsonl"
    if case == "missing":
        assert hook._read_cc_turns(str(p), 7) == [] == _old_full_read_turns(str(p))
        return
    rows = {
        "empty": "",
        "short": _line("user", "hi") + "\n" + _line("assistant", [{"type": "text", "text": "yo"}]) + "\n",
        "no_trailing_newline": _line("user", "hi") + "\n" + _line("user", "again"),
        "bad_json": _line("user", "hi") + "\n{not json\n\n" + _line("user", "after") + "\n",
        "crlf": _line("user", "hi") + "\r\n" + _line("assistant", [{"type": "text", "text": "yo"}]) + "\r\n",
    }[case]
    p.write_bytes(rows.encode())
    assert hook._read_cc_turns(str(p), 7) == _old_full_read_turns(str(p))[-7:]


def test_load_conversation_history_matches_the_old_result(hook, big, tmp_path):
    """Whole function, with the trailing-prompt pop, the session shard and the char budget."""
    sid = "l1-history"
    shard = hook._transcript_shard_path(sid)
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text(json.dumps({"role": "assistant", "content": "routed answer from the shard"}) + "\n")
    p = str(big[50])
    for prompt in ("current prompt text", "something else"):
        for max_turns in (2, 6):
            new = hook._load_conversation_history(p, prompt, max_turns=max_turns, session_id=sid)
            old_turns = _old_full_read_turns(p)
            if old_turns and old_turns[-1]["role"] == "user" and old_turns[-1]["content"] == prompt:
                old_turns.pop()
            seen = {(t["role"], t["content"]) for t in old_turns}
            shard_row = {"role": "assistant", "content": "routed answer from the shard"}
            if ("assistant", shard_row["content"]) not in seen:
                old_turns.append(shard_row)
            recent = old_turns[-max_turns:]
            budget, kept = 16000, []
            for t in reversed(recent):
                budget -= len(t["content"])
                if budget < 0 and kept:
                    break
                kept.append(t)
            kept.reverse()
            assert new == kept, (prompt, max_turns)


def test_the_shared_list_is_not_mutated(hook, big):
    turns = hook._read_cc_turns(str(big[20]))
    snapshot = [dict(t) for t in turns]
    hook._load_conversation_history(str(big[20]), "current prompt text", max_turns=2, cc_turns=turns)
    hook._last_assistant_text(str(big[20]), cc_turns=turns)
    assert turns == snapshot  # the trailing-prompt pop worked on a copy


def test_last_assistant_text_is_the_same_shared_or_not(hook, big):
    p = str(big[50])
    shared = hook._read_cc_turns(p)
    assert hook._last_assistant_text(p) == hook._last_assistant_text(p, cc_turns=shared) \
        == "FINAL-ANSWER the previous reply"


def _persisted(sid: str) -> list[tuple[str, str]]:
    return [(e.get("role"), e.get("content")) for e in session_store.load_events(sid, limit=200)
            if e.get("role") == "assistant"]


def test_persisted_turns_are_identical_to_the_old_full_read(hook, big, monkeypatch):
    """The session store ends up with the same events whether the turns came from the old
    full read or the tail read."""
    p = str(big[50])
    hook._persist_assistant_turns(p, "l1-new", current_prompt="current prompt text")
    new = _persisted("l1-new")

    def full(path, need=hook._CC_TURNS_NEEDED):
        return _old_full_read_turns(path)[-need:]

    monkeypatch.setattr(hook, "_read_cc_turns", full)
    hook._persist_assistant_turns(p, "l1-old", current_prompt="current prompt text")
    old = _persisted("l1-old")
    assert new and new == old
    assert any("FINAL-ANSWER" in c for _r, c in new)


_PARSE_DRIVER = textwrap.dedent(
    """
    import builtins, json, runpy, sys
    target = sys.argv[2]
    stat = {"opens": 0, "bytes": 0}
    real_open = builtins.open
    class P:
        def __init__(s, fh): s.fh = fh
        def __enter__(s): s.fh.__enter__(); return s
        def __exit__(s, *e): return s.fh.__exit__(*e)
        def read(s, *n):
            d = s.fh.read(*n); stat["bytes"] += len(d); return d
        def readlines(s, *n):
            d = s.fh.readlines(*n); stat["bytes"] += sum(len(x) for x in d); return d
        def __iter__(s):
            for x in s.fh:
                stat["bytes"] += len(x); yield x
        def __getattr__(s, n): return getattr(s.fh, n)
    def spy(file, *a, **k):
        fh = real_open(file, *a, **k)
        if str(file) == target:
            stat["opens"] += 1
            return P(fh)
        return fh
    builtins.open = spy
    sys.argv = [sys.argv[1]]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit:
        pass
    sys.stderr.write("\\nL1:" + json.dumps({**stat, "tiktoken": "tiktoken" in sys.modules,
                                          "okf": "llm_router.okf" in sys.modules}) + "\\n")
    """
)


def _run_hook(tmp_path: Path, transcript: Path, session_id="l1-e2e", prompt="please summarise the design",
              extra_env=None):
    home = tmp_path / "e2e-home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home), "LLM_ROUTER_HOME": str(home / "state"), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(SRC), "LLM_ROUTER_SYNTHETIC": "1", "LANG": "en_US.UTF-8",
           "OLLAMA_HOST": "http://127.0.0.1:9"}
    env.update(extra_env or {})
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "cwd": str(tmp_path),
               "transcript_path": str(transcript), "prompt": prompt}
    proc = subprocess.run([sys.executable, "-c", _PARSE_DRIVER, str(HOOK), str(transcript)],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=180)
    line = next((x for x in proc.stderr.splitlines() if x.startswith("L1:")), None)
    assert line, f"driver reported nothing (rc={proc.returncode}): {proc.stderr[-600:]}"
    assert proc.stdout.strip(), proc.stderr[-400:]
    return json.loads(line[3:]), home / "state"


def test_the_hook_opens_and_reads_the_transcript_once_per_prompt(big, tmp_path):
    """End to end, the hook run as __main__ the way the host does: ONE open of the transcript
    (persist, draft audit and draft history share one parse) and a bounded read of a 50 MB file."""
    report, state = _run_hook(tmp_path, big[50])
    size = big[50].stat().st_size
    assert report["opens"] == 1, report
    assert 0 < report["bytes"] <= 4 * 256 * 1024 < size, (report, size)


def test_the_hook_does_not_import_tiktoken_or_okf_on_a_routed_prompt(big, tmp_path):
    report, _state = _run_hook(tmp_path, big[20])
    assert report["tiktoken"] is False, "tiktoken (~10 ms) was imported on the per-prompt path"
    assert report["okf"] is False, "llm_router.okf (asyncio, ~9 ms) was imported on the per-prompt path"


def test_the_hook_persisted_claudes_previous_answer_end_to_end(big, tmp_path):
    _report, state = _run_hook(tmp_path, big[20], session_id="l1-e2e-persist")
    env_home = state.parent
    code = ("import json,sys\nfrom llm_router import session_store as s\n"
            "ev=s.load_events('l1-e2e-persist', limit=200)\n"
            "print(json.dumps([e.get('content') for e in ev if e.get('role')=='assistant']))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={"HOME": str(env_home), "LLM_ROUTER_HOME": str(state), "PATH": "/usr/bin:/bin",
                              "PYTHONPATH": str(SRC)})
    contents = json.loads(out.stdout.strip().splitlines()[-1])
    assert any("FINAL-ANSWER" in c for c in contents), contents


# ── A2: O(1) draft acceptance, rotation ─────────────────────────────────────────────

def _old_full_scan(log: Path) -> tuple[int, int]:
    if not log.exists():
        return (0, 0)
    used = offered = 0
    with log.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if "DRAFT UNUSED:" in line:
                offered += 1
            elif "DRAFT USED:" in line or "relayed as the answer" in line:
                used += 1
                offered += 1
    return (used, offered)


def _log_lines(n: int, start: int = 0) -> str:
    out = []
    for i in range(start, start + n):
        out.append(f"[2026-10-17 10:00:{i % 60:02d}] [INVOCATION {i}.1] prompt_len={i} session_id=s{i % 5}")
        if i % 7 == 0:
            out.append(f"[2026-10-17 10:00:{i % 60:02d}] [INVOCATION {i}.1] DRAFT USED: the draft was relayed as the answer")
        if i % 11 == 0:
            out.append(f"[2026-10-17 10:00:{i % 60:02d}] [INVOCATION {i}.1] DRAFT UNUSED: discarded; Claude answered instead")
        if i % 13 == 0:
            out.append(f"[2026-10-17 10:00:{i % 60:02d}] [INVOCATION {i}.1] note relayed as the answer")
    return "\n".join(out) + "\n"


def test_draft_acceptance_equals_the_old_full_scan(tmp_path):
    log = tmp_path / "auto-route-debug.log"
    log.write_text(_log_lines(3000), encoding="utf-8")
    want = _old_full_scan(log)
    assert want[1] > 100, "fixture has too few drafts to be a test"
    assert routing_report.draft_acceptance(log) == want  # cold: full scan, writes the sidecar
    assert routing_report.draft_acceptance(log) == want  # warm


def test_draft_acceptance_stays_equal_as_the_log_grows(tmp_path):
    log = tmp_path / "auto-route-debug.log"
    log.write_text(_log_lines(500), encoding="utf-8")
    assert routing_report.draft_acceptance(log) == _old_full_scan(log)
    for start in (500, 700, 1200):
        with log.open("a", encoding="utf-8") as fh:
            fh.write(_log_lines(150, start))
        assert routing_report.draft_acceptance(log) == _old_full_scan(log), start


def test_an_unterminated_last_line_is_counted_once(tmp_path):
    log = tmp_path / "auto-route-debug.log"
    log.write_text(_log_lines(50) + "[x] DRAFT USED: half a line", encoding="utf-8")
    assert routing_report.draft_acceptance(log) == _old_full_scan(log)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(" now finished\n")
    assert routing_report.draft_acceptance(log) == _old_full_scan(log)


def test_a_warm_call_scans_only_what_was_appended(tmp_path, monkeypatch):
    """The O(1) claim, counted: the second call starts past the first call's end of file."""
    log = tmp_path / "auto-route-debug.log"
    log.write_text(_log_lines(2000), encoding="utf-8")
    routing_report.draft_acceptance(log)
    end = log.stat().st_size
    offsets = []
    real = routing_report._scan_from

    def spy(path, offset):
        offsets.append((path.name, offset))
        return real(path, offset)

    monkeypatch.setattr(routing_report, "_scan_from", spy)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_log_lines(3, 9000))
    routing_report.draft_acceptance(log)
    assert offsets == [(log.name, end)], offsets


def test_a_corrupt_or_stale_sidecar_costs_a_rescan_never_a_wrong_count(tmp_path):
    log = tmp_path / "auto-route-debug.log"
    side = tmp_path / "auto-route-debug.log.draft_counts.json"
    log.write_text(_log_lines(400), encoding="utf-8")
    want = _old_full_scan(log)
    for junk in ("{not json", "[]", json.dumps({"v": 1, "offset": 10**9, "sig_len": 0, "sig": "x",
                                                "used": 5, "offered": 9})):
        side.write_text(junk)
        assert routing_report.draft_acceptance(log) == want, junk
    routing_report.draft_acceptance(log)
    # a log replaced by a different file (head differs) is not extended from the old count
    log.write_text("[x] DRAFT USED: a different log\n" + _log_lines(30, 5000), encoding="utf-8")
    assert routing_report.draft_acceptance(log) == _old_full_scan(log)


def test_counts_survive_rotation(tmp_path):
    log = tmp_path / "auto-route-debug.log"
    log.write_text(_log_lines(600), encoding="utf-8")
    routing_report.draft_acceptance(log)
    with log.open("a", encoding="utf-8") as fh:  # written after the last call, before the rotation
        fh.write(_log_lines(100, 600))
    capped_log.rotate_if_full(log, 1, backups=3)
    assert not log.exists() and (tmp_path / "auto-route-debug.log.1").exists()
    log.write_text(_log_lines(50, 700), encoding="utf-8")
    total = _old_full_scan(tmp_path / "auto-route-debug.log.1")
    cur = _old_full_scan(log)
    assert routing_report.draft_acceptance(log) == (total[0] + cur[0], total[1] + cur[1])
    all_in_one = tmp_path / "all.txt"
    all_in_one.write_text(_log_lines(750), encoding="utf-8")
    assert routing_report.draft_acceptance(log) == _old_full_scan(all_in_one)


def test_no_log_is_zero_zero(tmp_path):
    assert routing_report.draft_acceptance(tmp_path / "nope.log") == (0, 0)


def test_capped_log_keeps_the_requested_number_of_backups(tmp_path):
    log = tmp_path / "x.log"
    for gen in range(6):
        log.write_text(f"gen {gen}\n" * 10)
        capped_log.rotate_if_full(log, 5, backups=3)
    names = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("x.log") and not p.name.endswith(".lock"))
    assert names == ["x.log.1", "x.log.2", "x.log.3"]
    assert (tmp_path / "x.log.1").read_text().startswith("gen 5")
    assert (tmp_path / "x.log.3").read_text().startswith("gen 3")


def test_the_default_capped_log_append_is_still_two_generations(tmp_path):
    p = tmp_path / "y.jsonl"
    for _ in range(40):
        capped_log.append(p, b"x" * 100 + b"\n", 1000)
    assert (tmp_path / "y.jsonl.1").exists() and not (tmp_path / "y.jsonl.2").exists()


def test_the_hook_debug_log_rotates_at_its_cap(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_DEBUG_LOG_MAX_BYTES", 2000)
    monkeypatch.setattr(hook, "_DEBUG_LOG_BACKUPS", 2)
    path = hook._debug_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(400):
        hook._debug_log(f"[INVOCATION {i}.0] line {i} " + "x" * 40)
    siblings = sorted(p.name for p in path.parent.iterdir()
                      if p.name.startswith(path.name) and not p.name.endswith(".lock"))
    assert siblings == [path.name, path.name + ".1", path.name + ".2"], siblings
    assert path.stat().st_size <= 2000 + 120  # the cap plus the one line that crossed it
    assert (path.parent / (path.name + ".1")).stat().st_size > 1000  # a real generation, not an empty rename


def test_the_debug_log_cap_is_bounded_by_default(hook):
    assert hook._DEBUG_LOG_MAX_BYTES <= 16_000_000 and 1 <= hook._DEBUG_LOG_BACKUPS <= 5


# ── A3: policy loader, single policy load, lazy imports ───────────────────────────────

def test_policy_yaml_loads_with_the_c_loader_when_available():
    from llm_router import policy

    assert policy._SafeLoader is getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def test_the_c_loader_parses_every_shipped_policy_like_safe_load():
    loader = getattr(yaml, "CSafeLoader", None)
    if loader is None:
        pytest.skip("this PyYAML has no libyaml")
    files = sorted((SRC / "llm_router" / "policies").rglob("*.yaml"))
    assert len(files) >= 2, "no policy files found: the comparison would be vacuous"
    for f in files:
        text = f.read_text()
        assert yaml.load(text, Loader=loader) == yaml.safe_load(text), f.name


def test_resolve_loads_the_active_policy_once(monkeypatch):
    """research is unservable here, so resolve tries research, query and analyze: the policy is
    read once for all three, not once per task."""
    from llm_router import map_reader, policy, task_availability as ta

    monkeypatch.setattr(map_reader, "load_data", lambda: {"resources": [
        {"status": "connected", "chain_id": "xyz/model", "kind": "api"}]})
    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k)
    calls = []
    real = policy.get_active_policy
    monkeypatch.setattr(policy, "get_active_policy", lambda: calls.append(1) or real())
    task, _reason = ta.resolve("research")
    assert len(calls) == 1, f"policy loaded {len(calls)} times"
    assert task is None or isinstance(task, str)


def test_resolve_result_is_unchanged_by_the_hoisted_policy_read(monkeypatch):
    from llm_router import map_reader, task_availability as ta

    monkeypatch.setattr(map_reader, "load_data", lambda: {"resources": [
        {"status": "connected", "chain_id": "ollama/qwen", "kind": "local"}]})
    for k in list(os.environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k)
    assert ta.resolve("query") == ("query", ta.SERVABLE)
    assert ta.resolve("research")[0] in ("query", "analyze", "research", None)


def test_knowledge_pattern_has_one_definition():
    from llm_router import knowledge_ctx, okf

    assert okf._KNOWLEDGE_CTX_RE is knowledge_ctx.KNOWLEDGE_CTX_RE


def test_session_store_import_does_not_load_tiktoken():
    code = "import sys, llm_router.session_store; print('tiktoken' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC), "HOME": "/nonexistent-l1"})
    assert out.stdout.strip() == "False", out.stderr[-300:]


# ── router_added_ms on auto-route rows ──────────────────────────────────────────────

def _fake_clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(hook_latency, "_monotonic", lambda: t["now"])
    return t


def _rows(tmp_path):
    p = hook_latency.store_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def _clean_recorder(monkeypatch):
    monkeypatch.setattr(hook_latency, "_pending", None)
    monkeypatch.setattr(hook_latency, "_phases", {})
    monkeypatch.setattr(hook_latency, "_model_depth", 0)
    monkeypatch.setattr(hook_latency, "_nested_model_ms", 0.0)
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)


def test_an_auto_route_row_without_a_model_phase_carries_router_added_ms(monkeypatch, tmp_path, _clean_recorder):
    t = _fake_clock(monkeypatch)
    hook_latency.begin("auto-route", "UserPromptSubmit", t0=1000.0)
    with hook_latency.phase("session_io"):
        t["now"] += 0.03
    with hook_latency.phase("availability"):
        t["now"] += 0.01
    t["now"] += 0.02
    hook_latency._finish()
    (row,) = _rows(tmp_path)
    assert row["router_added_ms"] == pytest.approx(60.0)
    assert row["router_added_ms"] == row["elapsed_ms"]  # no model phase ran: nothing to subtract
    assert {"session_io", "availability"} <= set(row["phases_ms"])


def test_an_auto_route_row_with_a_model_phase_subtracts_it(monkeypatch, tmp_path, _clean_recorder):
    t = _fake_clock(monkeypatch)
    hook_latency.begin("auto-route", "UserPromptSubmit", t0=1000.0)
    with hook_latency.phase("zce"):
        with hook_latency.phase("zce_model"):
            t["now"] += 4.0
        t["now"] += 0.01
    hook_latency._finish()
    (row,) = _rows(tmp_path)
    assert row["router_added_ms"] == pytest.approx(10.0)


def test_other_hooks_keep_the_old_row_shape(monkeypatch, tmp_path, _clean_recorder):
    t = _fake_clock(monkeypatch)
    hook_latency.begin("status-bar", "UserPromptSubmit", t0=1000.0)
    with hook_latency.phase("read_cache"):
        t["now"] += 0.02
    hook_latency._finish()
    (row,) = _rows(tmp_path)
    assert "router_added_ms" not in row


def test_a_real_hook_run_writes_router_added_ms_and_the_new_phase_names(big, tmp_path):
    _report, state = _run_hook(tmp_path, big[20], session_id="l1-row",
                               extra_env={"LLM_ROUTER_MINI_SUMMARY_EVERY": "1"})
    rows = [json.loads(x) for x in (state / "hook_latency.jsonl").read_text().splitlines()]
    row = [r for r in rows if r["hook"] == "auto-route"][-1]
    assert "router_added_ms" in row, row
    assert row["router_added_ms"] <= row["elapsed_ms"] + 0.1
    phases = set(row["phases_ms"])
    assert {"import", "session_io", "engine", "mini_summary", "trend"} <= phases, phases


# ── readers of the rotated debug log see every generation ────────────────────────────

def _inv(i: int, day: str = "2026-10-17") -> str:
    return (f"[{day} 10:00:00] [INVOCATION START] ID={i}.5\n"
            f"[{day} 10:00:00] [INVOCATION {i}.5] prompt_len=40 session_id=abcdef12\n"
            f"[{day} 10:00:01] [INVOCATION {i}.5] DIRECT SUCCESS\n")


def _rotated(tmp_path: Path) -> Path:
    """invocations 1-2 in the .2 generation, 3-4 in .1, 5-6 in the live file."""
    log = tmp_path / "auto-route-debug.log"
    (tmp_path / "auto-route-debug.log.2").write_text(_inv(1) + _inv(2))
    (tmp_path / "auto-route-debug.log.1").write_text(_inv(3) + _inv(4))
    log.write_text(_inv(5) + _inv(6))
    return log


def test_the_hook_and_the_readers_agree_on_how_many_generations_exist(hook):
    from llm_router import routing_log

    assert hook._DEBUG_LOG_BACKUPS == routing_log.BACKUPS == routing_report._DRAFT_BACKUPS


def test_routing_log_load_reads_every_generation(tmp_path):
    from llm_router import routing_log

    log = _rotated(tmp_path)
    assert [p.name for p in routing_log.generations(log)] == [
        "auto-route-debug.log.2", "auto-route-debug.log.1", "auto-route-debug.log"]
    assert len(routing_log.load(log)) == 6


def test_routing_report_counts_every_generation(tmp_path):
    log = _rotated(tmp_path)
    assert routing_report._outcome_counts(log)["DIRECT SUCCESS"] == 6
    assert routing_report.unterminated_invocations(log) == (0, 6)


def test_a_log_whose_live_file_was_just_rotated_away_is_still_read(tmp_path):
    from llm_router import routing_log

    (tmp_path / "auto-route-debug.log.1").write_text(_inv(3) + _inv(4))
    log = tmp_path / "auto-route-debug.log"  # absent: the next write has not happened yet
    assert len(routing_log.load(log)) == 2
    assert routing_report._outcome_counts(log)["DIRECT SUCCESS"] == 2


def test_routing_health_and_northstar_read_every_generation(tmp_path):
    import datetime as dt

    from llm_router import northstar, routing_health

    log = _rotated(tmp_path)
    assert len(northstar._parse_debug_log(log)) == 6
    days = routing_health.summarize(log, days=3, today=dt.date(2026, 10, 17))
    assert sum(d["prompts"] for d in days.values()) == 6


def test_an_unwritable_counter_sidecar_is_counted_and_the_answer_is_still_right(tmp_path, monkeypatch):
    from llm_router import failopen

    log = tmp_path / "auto-route-debug.log"
    log.write_text(_log_lines(200), encoding="utf-8")
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith(".draft_counts.json"):
            raise OSError("read-only")
        return real_replace(src, dst)

    monkeypatch.setattr(routing_report.os, "replace", boom)
    assert routing_report.draft_acceptance(log) == _old_full_scan(log)
    assert seen == ["CHZ-FO-DRAFT-COUNTS-WRITE"]


# F64 review (owner 2026-10-18): the scan cap defaults to 32 MiB, is configurable, and a hit
# is marked on the latency row so truncated context stays measurable.
def test_scan_cap_default_and_override(hook, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_TRANSCRIPT_SCAN_CAP_MB", raising=False)
    assert hook._scan_cap_bytes() == 32 * 1024 * 1024
    for raw, mb in (("64", 64), ("0", 1), ("5000", 1024), ("junk", 32), ("", 32)):
        monkeypatch.setenv("LLM_ROUTER_TRANSCRIPT_SCAN_CAP_MB", raw)
        assert hook._scan_cap_bytes() == mb * 1024 * 1024, raw


def test_a_cap_hit_is_marked_on_the_latency_row(hook, tmp_path, monkeypatch):
    marks = []
    monkeypatch.setattr(hook_latency, "add_phase", lambda name, ms: marks.append((name, ms)))
    monkeypatch.setattr(hook, "_TRANSCRIPT_SCAN_CAP_BYTES", 1024 * 1024)
    p = tmp_path / "capped.jsonl"
    with p.open("w") as fh:
        fh.write(_line("user", "old question") + "\n")
        row = _line("user", [{"type": "tool_result", "tool_use_id": "x", "content": "z" * 60_000}]) + "\n"
        for _ in range(40):  # ~2.4 MB of tool output, more than the 1 MiB cap
            fh.write(row)
    assert hook._read_cc_turns(str(p), 7) == []
    assert ("cc_scan_capped", 0.0) in marks
    marks.clear()
    q = tmp_path / "small.jsonl"
    q.write_text(_line("user", "q") + "\n" + _line("assistant", [{"type": "text", "text": "a"}]) + "\n")
    hook._read_cc_turns(str(q), 7)  # whole file read, cap not hit
    assert marks == []
