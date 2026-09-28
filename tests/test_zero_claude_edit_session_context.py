"""Session-context resolution for anaphoric edit-class prompts.

Real edit-like prompts rarely name a path or a code identifier — most short
ones refer back to the conversation ("fix F-1", "apply that", "fix it").
Before this feature, ``classify_edit_prompt`` dropped every one of these
(measured 2026-09-28: only 6/122 real edit-like prompts on this machine
entered the scope). This module resolves them against the CC transcript
named by the hook payload's ``transcript_path`` — recently Read/Edited/
Written files, plus files named in Claude's own last reply — and ONLY when
the result is small (<=2 files) and unambiguous.

Contract under test:
  * classify_edit_prompt marks the anaphoric shape (pronoun / "F-1"-style
    label, no literal target) instead of just dropping it.
  * resolve_session_targets resolves a label to the one file named next to
    it in the last assistant message; a bare pronoun to the small set of
    recently touched/mentioned files; and refuses (returns []) on ambiguity,
    a stale transcript, or no transcript at all.
  * maybe_replace wires this into the same safety pipeline as every other
    target (containment, dirty-tree, breaker, all-or-nothing apply) — it
    only changes WHAT the target is, never HOW it is handled.
  * a question is still never edit-class, session context or not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

sys.path.insert(0, str(ROOT / "src"))

from llm_router import zero_claude_edit as zce  # noqa: E402


# ── unit tests: classify_edit_prompt anaphoric flag ──────────────────────────


@pytest.mark.parametrize("prompt", ["fix it", "apply that", "update this", "remove them"])
def test_classify_marks_bare_pronoun_as_anaphoric(prompt):
    result = zce.classify_edit_prompt(prompt)
    assert not result.is_edit
    assert result.anaphoric, result.reason
    assert result.labels == ()


def test_classify_marks_label_reference_as_anaphoric():
    result = zce.classify_edit_prompt("fix F-1")
    assert not result.is_edit
    assert result.anaphoric
    assert result.labels == ("F-1",)


@pytest.mark.parametrize("prompt", ["Finding 2 is wrong, fix Finding 2", "fix issue 3", "fix #7"])
def test_classify_recognises_label_shapes(prompt):
    result = zce.classify_edit_prompt(prompt)
    assert result.anaphoric
    assert result.labels


def test_classify_plain_no_target_prompt_is_not_anaphoric():
    """No pronoun and no label — the pre-existing 'nothing to resolve' case,
    unaffected by this feature."""
    result = zce.classify_edit_prompt("fix the redis checkpoint first")
    assert not result.is_edit
    assert not result.anaphoric
    assert result.reason == "no named file or code identifier in the prompt"


def test_classify_git_verb_with_pronoun_is_not_anaphoric():
    """'merge it into main' is a git operation, not a scoped file rewrite —
    measured 2026-09-28: this exact prompt false-positived under the naive
    rule (any edit verb + any pronoun) before 'merge' was excluded."""
    result = zce.classify_edit_prompt("merge it into main")
    assert not result.anaphoric


@pytest.mark.parametrize("verb", ["implement", "split", "extract"])
def test_classify_ambiguous_abstract_verbs_are_not_anaphoric(verb):
    """These read as 'implement the plan' / 'split the PR' / 'extract this
    into a ticket' far more often than a file edit, with no path or
    identifier to confirm otherwise."""
    result = zce.classify_edit_prompt(f"{verb} it")
    assert not result.anaphoric


def test_classify_long_conversational_prompt_is_not_anaphoric():
    """Measured 2026-09-28 on this machine's real prompts: long, non-edit
    conversational asks routinely contain a stray edit verb and pronoun
    ('...have this as a platform to add more agents and tools') without
    being an edit request at all. The length cap catches what the verb/
    pronoun check alone does not."""
    prompt = (
        "create an architectural audit and pros and cons of different components "
        "of the architecture and what you could have replaced/changed to have "
        "this as a platform to add more agents and tools"
    )
    assert len(prompt) > zce._MAX_ANAPHORIC_PROMPT_CHARS
    result = zce.classify_edit_prompt(prompt)
    assert not result.anaphoric


@pytest.mark.parametrize(
    "prompt",
    [
        "did that fix it?",
        "is F-1 still broken?",
        "what should I fix?",
    ],
)
def test_classify_question_is_never_anaphoric_edit(prompt):
    """A question about a prior finding is a question, not a change request
    — session context must never be consulted for it."""
    result = zce.classify_edit_prompt(prompt)
    assert not result.is_edit
    assert not result.anaphoric, result.reason


# ── unit tests: resolve_session_targets ──────────────────────────────────────


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _tool_use_entry(tool: str, file_path: str) -> dict:
    return {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": tool, "input": {"file_path": file_path}}],
        },
    }


def _assistant_text_entry(text: str) -> dict:
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_resolve_session_targets_no_transcript_path():
    files, reason = zce.resolve_session_targets(
        zce.EditClassification(False, (), "x", anaphoric=True), "",
    )
    assert files == []
    assert "no transcript_path" in reason


def test_resolve_session_targets_bare_pronoun_resolves_to_recent_file(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _tool_use_entry("Read", "pkg/mod07.py"),
        _assistant_text_entry("mod07.py has an off-by-one bug in the loop bound."),
    ])
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True)
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == ["pkg/mod07.py"]
    assert "pkg/mod07.py" in reason


def test_resolve_session_targets_bare_pronoun_ignores_prose_mention(tmp_path):
    """Regression for a real miss (2026-09-28): 'add a link to it from the
    README' resolved to the file 'it' REFERRED TO (named only in prose) —
    exactly backwards, since the untouched README was the real edit target.
    A bare pronoun must resolve ONLY against files Claude itself touched via
    a tool call, never a file merely named in its prose reply."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _assistant_text_entry("docs/BUILD-PROMPT.md is the file we were discussing."),
    ])
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True)
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == []
    assert "no recently touched files" in reason


def test_resolve_session_targets_label_resolves_to_named_file(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _assistant_text_entry(
            "Findings:\nF-1: off-by-one bug in pkg/mod07.py (line 12)\n"
            "F-2: missing null check in pkg/mod09.py",
        ),
    ])
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True, labels=("F-1",))
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == ["pkg/mod07.py"]
    assert "F-1" in reason


def test_resolve_session_targets_label_ambiguous_falls_through(tmp_path):
    """Same label mentioned next to two different files -> refuse."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _assistant_text_entry(
            "F-1: bug in pkg/mod07.py\nAlso see F-1 again in pkg/mod09.py for the same pattern.",
        ),
    ])
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True, labels=("F-1",))
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == []
    assert "F-1" in reason


def test_resolve_session_targets_label_not_found_falls_through(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [_assistant_text_entry("Everything looks fine, no findings.")])
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True, labels=("F-1",))
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == []


def test_resolve_session_targets_ambiguous_recent_files_falls_through(tmp_path):
    """More than _SESSION_CONTEXT_MAX_FILES recently touched files -> 'which
    one?', not a resolvable reference."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _tool_use_entry("Read", "a.py"),
        _tool_use_entry("Edit", "b.py"),
        _tool_use_entry("Read", "c.py"),
    ])
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True)
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == []
    assert "ambiguous" in reason


def test_resolve_session_targets_stale_transcript_falls_through(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [_tool_use_entry("Read", "pkg/mod07.py")])
    stale_time = time.time() - zce._STALE_TRANSCRIPT_S - 60
    os.utime(transcript, (stale_time, stale_time))
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True)
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == []
    assert "stale" in reason


def test_resolve_session_targets_empty_transcript_falls_through(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    classification = zce.EditClassification(False, (), "anaphoric", anaphoric=True)
    files, reason = zce.resolve_session_targets(classification, str(transcript))
    assert files == []


def test_last_assistant_message_returns_most_recent_text(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _assistant_text_entry("first reply"),
        {"message": {"role": "user", "content": "ok now what"}},
        _assistant_text_entry("second reply, the real one"),
    ])
    assert zce.last_assistant_message(str(transcript)) == "second reply, the real one"


def test_recent_session_files_dedupes_and_orders_most_recent_first(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        _tool_use_entry("Read", "a.py"),
        _tool_use_entry("Edit", "a.py"),
        _tool_use_entry("Read", "b.py"),
    ])
    assert zce.recent_session_files(str(transcript)) == ["b.py", "a.py"]


# ── end-to-end hook scenarios ────────────────────────────────────────────────

STUB_MODEL = "scoped-edit-model:latest"

_VALID_EDIT_RESPONSE = json.dumps([
    {
        "file": "pkg/mod07.py",
        "old_string": "def count_vowels(s):\n    return 0\n",
        "new_string": "def count_vowels(s):\n    return sum(1 for c in s if c in 'aeiou')\n",
        "description": "fix count_vowels",
    }
])


class _StubOllama(BaseHTTPRequestHandler):
    response_content = _VALID_EDIT_RESPONSE
    calls: list[dict] = []

    def log_message(self, *_a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": STUB_MODEL}]})
        else:
            self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        type(self).calls.append(payload)
        self._send({
            "model": payload.get("model"), "done": True,
            "prompt_eval_count": 20, "eval_count": 15,
            "message": {"role": "assistant", "content": type(self).response_content},
        })


@pytest.fixture
def stub_ollama():
    _StubOllama.response_content = _VALID_EDIT_RESPONSE
    _StubOllama.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    (root / "pkg").mkdir()
    (root / "pkg" / "mod07.py").write_text("def count_vowels(s):\n    return 0\n")
    _commit_all(root, "init")
    return root


def _run(
    prompt: str, home: Path, repo_dir: Path, ollama_url: str | None,
    transcript_path: str | None = None, extra_env=None,
) -> dict | None:
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    env["HOME"] = str(home)
    env["LLM_ROUTER_HOME"] = str(home) + "/.llm-router"
    if ollama_url:
        env["LLM_ROUTER_OLLAMA_URL"] = ollama_url
        env["LLM_ROUTER_OLLAMA_MODEL"] = STUB_MODEL
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_API_KEY"] = ""
    if extra_env:
        env.update(extra_env)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt, "session_id": "zce-ctx",
        "cwd": str(repo_dir),
    }
    if transcript_path:
        payload["transcript_path"] = transcript_path
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env,
        cwd=str(home),
        timeout=60,
    )
    out = result.stdout.strip()
    return json.loads(out) if out else None


def test_anaphoric_prompt_with_no_transcript_falls_through(tmp_path, repo, stub_ollama):
    before = (repo / "pkg" / "mod07.py").read_text()
    out = _run(
        "fix it", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT APPLIED" not in json.dumps(out)
    assert (repo / "pkg" / "mod07.py").read_text() == before
    assert not _StubOllama.calls


def test_anaphoric_pronoun_resolves_via_recently_read_file_and_is_applied(tmp_path, repo, stub_ollama):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [
        _tool_use_entry("Read", str(repo / "pkg" / "mod07.py")),
        _assistant_text_entry("count_vowels always returns 0 — it never checks the characters."),
    ])
    out = _run(
        "fix it", tmp_path, repo, stub_ollama, transcript_path=str(transcript),
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert out is not None and out.get("decision") == "block"
    reason = out.get("reason", "")
    assert "ZERO_CLAUDE_EDIT APPLIED" in reason
    assert "pkg/mod07.py" in reason
    assert "session context" in reason
    assert (repo / "pkg" / "mod07.py").read_text() != "def count_vowels(s):\n    return 0\n"
    # the last assistant message was handed to the local model as context
    assert _StubOllama.calls
    sent = json.dumps(_StubOllama.calls[-1])
    assert "never checks the characters" in sent


def test_labelled_reference_resolves_via_last_assistant_message(tmp_path, repo, stub_ollama):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [
        _assistant_text_entry("F-1: count_vowels always returns 0, in pkg/mod07.py"),
    ])
    out = _run(
        "fix F-1", tmp_path, repo, stub_ollama, transcript_path=str(transcript),
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert out is not None and out.get("decision") == "block"
    reason = out.get("reason", "")
    assert "ZERO_CLAUDE_EDIT APPLIED" in reason
    assert "F-1" in reason


def test_anaphoric_prompt_ambiguous_recent_files_falls_through(tmp_path, repo, stub_ollama):
    (repo / "pkg" / "mod09.py").write_text("x = 1\n")
    (repo / "pkg" / "mod11.py").write_text("y = 2\n")
    _commit_all(repo, "more files")
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [
        _tool_use_entry("Read", str(repo / "pkg" / "mod07.py")),
        _tool_use_entry("Read", str(repo / "pkg" / "mod09.py")),
        _tool_use_entry("Read", str(repo / "pkg" / "mod11.py")),
    ])
    before = (repo / "pkg" / "mod07.py").read_text()
    out = _run(
        "fix it", tmp_path, repo, stub_ollama, transcript_path=str(transcript),
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT APPLIED" not in json.dumps(out)
    assert (repo / "pkg" / "mod07.py").read_text() == before
    assert not _StubOllama.calls


def test_anaphoric_prompt_stale_transcript_falls_through(tmp_path, repo, stub_ollama):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [_tool_use_entry("Read", str(repo / "pkg" / "mod07.py"))])
    stale_time = time.time() - zce._STALE_TRANSCRIPT_S - 60
    os.utime(transcript, (stale_time, stale_time))
    before = (repo / "pkg" / "mod07.py").read_text()
    out = _run(
        "fix it", tmp_path, repo, stub_ollama, transcript_path=str(transcript),
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT APPLIED" not in json.dumps(out)
    assert (repo / "pkg" / "mod07.py").read_text() == before
    assert not _StubOllama.calls


def test_question_never_triggers_session_resolution(tmp_path, repo, stub_ollama):
    """'did that fix it?' is a question about the conversation, not a change
    request — session context must never even be consulted."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [_tool_use_entry("Read", str(repo / "pkg" / "mod07.py"))])
    before = (repo / "pkg" / "mod07.py").read_text()
    out = _run(
        "did that fix it?", tmp_path, repo, stub_ollama, transcript_path=str(transcript),
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "pkg" / "mod07.py").read_text() == before
    assert not _StubOllama.calls


def test_anaphoric_prompt_dirty_target_falls_through(tmp_path, repo, stub_ollama):
    (repo / "pkg" / "mod07.py").write_text("def count_vowels(s):\n    return 0  # wip\n")
    dirty_content = (repo / "pkg" / "mod07.py").read_text()
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [_tool_use_entry("Read", str(repo / "pkg" / "mod07.py"))])
    out = _run(
        "fix it", tmp_path, repo, stub_ollama, transcript_path=str(transcript),
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT APPLIED" not in json.dumps(out)
    assert (repo / "pkg" / "mod07.py").read_text() == dirty_content
