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


def test_classify_rejects_prompt_with_no_named_file_or_identifier():
    """No literal file AND no code-shaped identifier -> nothing to resolve."""
    result = zce.classify_edit_prompt("fix the redis checkpoint first")
    assert not result.is_edit
    assert result.files == ()
    assert result.symbols == ()


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("add a docstring to bar()", "bar"),
        ("count_vowels ignores uppercase, fix it", "count_vowels"),
        ("fix `normalize` so it strips whitespace", "normalize"),
        ("the ScopedEditOutcome dataclass should be frozen, change it", "ScopedEditOutcome"),
    ],
)
def test_classify_extracts_identifiers_when_no_file_is_named(prompt, expected):
    """Real users name a function/class more often than a path: the
    classifier hands those identifiers on for repo resolution."""
    result = zce.classify_edit_prompt(prompt)
    assert result.is_edit, result.reason
    assert result.files == ()
    assert expected in result.symbols


def test_classify_ignores_plain_english_words_as_identifiers():
    """Only code-shaped names (snake_case, camelCase, call-shaped, backticked)
    are candidates — a plain word like 'checkpoint' is not."""
    result = zce.classify_edit_prompt("fix the checkpoint logic please")
    assert result.symbols == ()


@pytest.mark.parametrize(
    "prompt",
    [
        "what does count_vowels do?",
        "why is count_vowels failing on uppercase input? should we fix it",
        "I think count_vowels might be wrong - could you fix it or is it fine?",
    ],
)
def test_classify_question_mentioning_function_is_not_edit(prompt):
    """A question that mentions a function is a question, not a change
    request — never resolved to a file and edited."""
    result = zce.classify_edit_prompt(prompt)
    assert not result.is_edit, result.reason


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


# ── unit tests: symbol / bare-filename resolution ────────────────────────────


def _symbol_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _commit_all(repo, "init")
    return repo


def test_resolve_symbol_unique_definition(tmp_path):
    """The defining file wins; a test file that only CALLS the function is
    not a definition and does not make it ambiguous."""
    repo = _symbol_repo(tmp_path, {
        "pkg/mod07.py": "def count_vowels(s):\n    return 0\n",
        "tests/test_mod07.py": "from pkg.mod07 import count_vowels\nassert count_vowels('a') == 1\n",
    })
    files, reason = zce.resolve_symbol_targets(("count_vowels",), repo)
    assert files == ["pkg/mod07.py"], reason
    assert "count_vowels" in reason


def test_resolve_symbol_class_definition(tmp_path):
    repo = _symbol_repo(tmp_path, {"a/models.py": "class Widget:\n    pass\n"})
    files, _ = zce.resolve_symbol_targets(("Widget",), repo)
    assert files == ["a/models.py"]


def test_resolve_symbol_ambiguous_falls_through(tmp_path):
    """Defined in two files -> refuse to guess. A wrong-file edit is worse
    than a fallthrough."""
    repo = _symbol_repo(tmp_path, {
        "a.py": "def normalize(x):\n    return x\n",
        "b.py": "def normalize(y):\n    return y\n",
    })
    files, reason = zce.resolve_symbol_targets(("normalize",), repo)
    assert files == []
    assert "ambiguous" in reason


def test_resolve_symbol_one_ambiguous_name_poisons_the_set(tmp_path):
    """Even when another name is unique, one ambiguous name means we do not
    know which file the user meant."""
    repo = _symbol_repo(tmp_path, {
        "a.py": "def normalize(x):\n    return x\n\ndef only_here():\n    pass\n",
        "b.py": "def normalize(y):\n    return y\n",
    })
    files, reason = zce.resolve_symbol_targets(("only_here", "normalize"), repo)
    assert files == []
    assert "ambiguous" in reason


def test_resolve_symbol_more_than_three_files_falls_through(tmp_path):
    repo = _symbol_repo(tmp_path, {
        f"m{i}.py": f"def fn_{i}():\n    pass\n" for i in range(4)
    })
    files, reason = zce.resolve_symbol_targets(tuple(f"fn_{i}" for i in range(4)), repo)
    assert files == []
    assert "too broad" in reason


def test_resolve_symbol_not_defined_anywhere(tmp_path):
    repo = _symbol_repo(tmp_path, {"a.py": "x = 1\n"})
    files, reason = zce.resolve_symbol_targets(("count_vowels",), repo)
    assert files == []
    assert "no definition" in reason


def test_resolve_symbol_ignores_untracked_files(tmp_path):
    """git grep over TRACKED files only — a scratch file is not a target."""
    repo = _symbol_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "scratch.py").write_text("def count_vowels(s):\n    return 0\n")
    files, _ = zce.resolve_symbol_targets(("count_vowels",), repo)
    assert files == []


def test_resolve_bare_filename_to_unique_tracked_path(tmp_path):
    repo = _symbol_repo(tmp_path, {"src/pkg/helpers.py": "x = 1\n"})
    resolved, problems = zce.resolve_target_files(("helpers.py",), repo)
    assert resolved == ["src/pkg/helpers.py"]
    assert problems == []


def test_resolve_bare_filename_ambiguous_is_dropped(tmp_path):
    repo = _symbol_repo(tmp_path, {"a/helpers.py": "x = 1\n", "b/helpers.py": "y = 2\n"})
    resolved, problems = zce.resolve_target_files(("helpers.py",), repo)
    assert resolved == []
    assert problems == []


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


def test_symbol_only_prompt_is_resolved_applied_and_names_the_target(tmp_path, repo, stub_ollama):
    """No path in the prompt: the function name resolves to foo.py (its unique
    definition), the edit is applied, and the block message says which file
    was inferred from which name."""
    out = _run(
        "old_name is a bad name, rename it to new_name", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert out is not None and out.get("decision") == "block"
    reason = out.get("reason", "")
    assert "ZERO_CLAUDE_EDIT APPLIED" in reason
    assert "old_name -> foo.py" in reason
    assert (repo / "foo.py").read_text() == "def new_name():\n    pass\n"


def test_symbol_only_prompt_ambiguous_falls_through(tmp_path, repo, stub_ollama):
    (repo / "bar.py").write_text("def old_name():\n    return 1\n")
    _commit_all(repo, "second definition")
    before = (repo / "foo.py").read_text()
    out = _run(
        "old_name is a bad name, rename it to new_name", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text() == before
    assert not _StubOllama.calls
    logs = list((tmp_path / ".llm-router").glob("auto-route-debug*.log"))
    assert logs and "ambiguous" in "".join(p.read_text() for p in logs)


def test_symbol_only_prompt_dirty_target_falls_through(tmp_path, repo, stub_ollama):
    (repo / "foo.py").write_text("def old_name():\n    pass\n# wip\n")
    out = _run(
        "old_name is a bad name, rename it to new_name", tmp_path, repo, stub_ollama,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE_SCOPE": "edit"},
    )
    assert "ZERO_CLAUDE_EDIT" not in json.dumps(out)
    assert (repo / "foo.py").read_text().endswith("# wip\n")


def test_classify_long_prompt_does_not_infer_from_identifiers():
    """A pasted spec/log that happens to contain a class name and an edit
    verb is not a request to edit that class's file."""
    prompt = "implement this, see ModelCapability notes. " + "context line. " * 30
    result = zce.classify_edit_prompt(prompt)
    assert not result.is_edit
    assert "too long" in result.reason


def test_resolve_bare_filename_not_inferred_when_disabled(tmp_path):
    repo = _symbol_repo(tmp_path, {"src/pkg/helpers.py": "x = 1\n"})
    resolved, _ = zce.resolve_target_files(("helpers.py",), repo, infer_bare=False)
    assert resolved == []
