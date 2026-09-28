"""Scoped zero-Claude for edit-class prompts (LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit).

Unit tests for the pure classifier/safety helpers in zero_claude_edit.py, plus
end-to-end hook scenarios mirroring tests/test_zero_claude_bypass.py and
tests/test_edit.py's subprocess-against-a-stub-Ollama pattern.

Contract under test:
  * scope unset (default)         -> unchanged: no file touched, no
                                      ZERO_CLAUDE_EDIT decision emitted.
  * scope=edit + non-edit prompt  -> falls through to Claude, unchanged.
  * scope=edit + edit prompt      -> replaced: applied, blocked with a diff.
  * scope=edit + generation fails -> blocked with the reason, nothing changed.
  * scope=edit + dirty target     -> falls through to Claude, unchanged.
  * scope=edit + open breaker     -> falls through to Claude, unchanged.
  * scope=edit + out-of-repo path -> refused (blocked), unchanged.
  * `claude:` prefix              -> bypasses, falls through to Claude.
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


# ── unit tests: classify_edit_prompt ─────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "rename old_name to new_name in foo.py",
        "fix the off-by-one bug in src/router.py",
        "add type hints to src/foo.py",
        "refactor the retry loop in retry.py",
    ],
)
def test_classify_recognises_edit_shaped_prompts(prompt):
    result = zce.classify_edit_prompt(prompt)
    assert result.is_edit, result.reason
    assert result.files


def test_classify_rejects_prompt_with_no_named_file():
    """'add a docstring to bar()' names a symbol, not a file — conservative
    by design: without a file this module has nothing safe to resolve."""
    result = zce.classify_edit_prompt("add a docstring to bar()")
    assert not result.is_edit
    assert result.files == ()


def test_classify_rejects_questions():
    result = zce.classify_edit_prompt("what does router.py do?")
    assert not result.is_edit
    assert "question" in result.reason


def test_classify_rejects_too_many_files():
    prompt = "add type hints to a.py and b.py and c.py and d.py"
    result = zce.classify_edit_prompt(prompt)
    assert not result.is_edit
    assert "too broad" in result.reason


def test_classify_allows_tests_directory_file_when_named_explicitly():
    """Frozen-path caution (tests/, .github/) only ever applies to paths the
    classifier would infer on its own — it never infers, it only extracts
    literal names from the prompt, so an explicitly named tests/ file is
    treated the same as any other named file."""
    result = zce.classify_edit_prompt("fix the fixture path in tests/test_foo.py")
    assert result.is_edit
    assert "tests/test_foo.py" in result.files


def test_classify_empty_prompt():
    result = zce.classify_edit_prompt("")
    assert not result.is_edit


# ── unit tests: resolve_target_files / dirty_files ───────────────────────────


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


def test_resolve_target_files_accepts_existing_in_repo_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "foo.py").write_text("x = 1\n")
    _commit_all(repo, "init")

    resolved, problems = zce.resolve_target_files(("foo.py",), repo)
    assert resolved == ["foo.py"]
    assert problems == []


def test_resolve_target_files_drops_nonexistent_silently(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    resolved, problems = zce.resolve_target_files(("nope.py",), repo)
    assert resolved == []
    assert problems == []  # not found is not a security problem


def test_resolve_target_files_refuses_path_escaping_repo_root(tmp_path):
    """A traversal sequence embedded in an otherwise normal-looking relative
    name (the shape a real prompt could contain) must be caught — this is the
    'out-of-repo paths refused' safety requirement."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")

    resolved, problems = zce.resolve_target_files(("a/../../outside.py",), repo)
    assert resolved == []
    assert problems and "outside the repo root" in problems[0]


def test_dirty_files_flags_uncommitted_modification(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "foo.py").write_text("x = 1\n")
    _commit_all(repo, "init")
    (repo / "foo.py").write_text("x = 2\n")  # uncommitted

    assert zce.dirty_files(repo, ["foo.py"]) == ["foo.py"]


def test_dirty_files_empty_for_clean_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "foo.py").write_text("x = 1\n")
    _commit_all(repo, "init")

    assert zce.dirty_files(repo, ["foo.py"]) == []


# ── end-to-end hook scenarios ────────────────────────────────────────────────

STUB_MODEL = "scoped-edit-model:latest"

_VALID_EDIT_RESPONSE = json.dumps([
    {
        "file": "foo.py",
        "old_string": "def old_name():\n    pass\n",
        "new_string": "def new_name():\n    pass\n",
        "description": "rename old_name to new_name",
    }
])


class _StubOllama(BaseHTTPRequestHandler):
    #: Overridable per-test: the /api/chat response content.
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


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    (root / "foo.py").write_text("def old_name():\n    pass\n")
    _commit_all(root, "init")
    return root


def _run(prompt: str, home: Path, repo_dir: Path, ollama_url: str | None, extra_env=None) -> dict | None:
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
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt, "session_id": "zce",
            "cwd": str(repo_dir),
        }),
        capture_output=True, text=True, env=env,
        cwd=str(home),
        timeout=60,
    )
    out = result.stdout.strip()
    return json.loads(out) if out else None


def test_scope_off_is_unchanged(tmp_path, repo, stub_ollama):
    """Default (scope unset): behaviour and files are exactly as before this
    feature existed — no ZERO_CLAUDE_EDIT decision, file untouched."""
    before = (repo / "foo.py").read_text()
    out = _run("rename old_name to new_name in foo.py", tmp_path, repo, stub_ollama)
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text() == before


def test_non_edit_prompt_goes_to_claude(tmp_path, repo, stub_ollama):
    before = (repo / "foo.py").read_text()
    out = _run(
        "what does foo.py do?", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text() == before


def test_edit_prompt_is_replaced_applied_and_blocked_with_diff(tmp_path, repo, stub_ollama):
    out = _run(
        "rename old_name to new_name in foo.py", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert out is not None
    assert out.get("decision") == "block"
    reason = out.get("reason", "")
    assert "ZERO_CLAUDE_EDIT APPLIED" in reason
    assert STUB_MODEL in reason
    assert "foo.py" in reason
    assert "new_name" in reason  # short diff shown
    assert "claude:" in reason   # redo hint
    assert (repo / "foo.py").read_text() == "def new_name():\n    pass\n"


def test_failed_edit_blocks_with_reason_and_changes_nothing(tmp_path, repo, stub_ollama):
    _StubOllama.response_content = "I cannot help with that."
    before = (repo / "foo.py").read_text()
    out = _run(
        "rename old_name to new_name in foo.py", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert out is not None
    assert out.get("decision") == "block"
    assert "ZERO_CLAUDE_EDIT BLOCKED" in out.get("reason", "")
    assert "claude:" in out.get("reason", "")
    assert (repo / "foo.py").read_text() == before


def test_dirty_target_file_falls_through_to_claude(tmp_path, repo, stub_ollama):
    (repo / "foo.py").write_text("def old_name():\n    pass  # local edit\n")
    dirty_content = (repo / "foo.py").read_text()
    out = _run(
        "rename old_name to new_name in foo.py", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text() == dirty_content


def test_open_breaker_falls_through_to_claude(tmp_path, repo, stub_ollama):
    state_dir = tmp_path / ".llm-router"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "quality_breaker.json").write_text(json.dumps({
        "version": 1,
        "classes": {
            "zero_claude_edit:code": {"state": "open", "opened_at": time.time()},
        },
    }))
    before = (repo / "foo.py").read_text()
    out = _run(
        "rename old_name to new_name in foo.py", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text() == before


def test_out_of_repo_path_is_refused(tmp_path, repo, stub_ollama):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    before = outside.read_text()
    out = _run(
        "rename secret to hidden in a/../../outside.py", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert out is not None
    assert out.get("decision") == "block"
    assert "outside the repo root" in out.get("reason", "")
    assert outside.read_text() == before  # never touched
    assert not _StubOllama.calls  # refused before any model call


def test_claude_prefix_bypasses(tmp_path, repo, stub_ollama):
    before = (repo / "foo.py").read_text()
    out = _run(
        "claude: rename old_name to new_name in foo.py", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text() == before
