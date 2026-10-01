"""Plan 3.3: pre-write lint verification for scoped zero-Claude edits.

``verify_changed_files`` is the gate ``zero_claude_edit.maybe_replace`` runs
between validation (exact-once match + syntax check) and the write. These
tests exercise it directly, behaviourally: real ruff, real pass/fail
outcomes, real temp files — never a source-text assertion.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from llm_router.local_agent import verify

CLEAN_PY = "def add(a, b):\n    return a + b\n"
# F401 (unused import) is in this repo's selected ruff rules (E4, E7, E9, F —
# see pyproject.toml [tool.ruff.lint]).
LINT_DIRTY_PY = "import os\n\n\ndef add(a, b):\n    return a + b\n"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = [\"E4\", \"E7\", \"E9\", \"F\"]\n"
    )
    return root


@pytest.fixture(autouse=True)
def _ruff_required():
    if shutil.which("ruff") is None and shutil.which("uvx") is None:
        pytest.skip("neither ruff nor uv/uvx on PATH — cannot exercise the real linter")


# ── enabled/disabled gate ────────────────────────────────────────────────────

def test_disabled_skips_verification_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "0")
    repo = _repo(tmp_path)
    result = verify.verify_changed_files(
        {"bad.py": LINT_DIRTY_PY}, ["bad.py"], repo, time.monotonic() + 30,
    )
    assert result.ok
    assert result.ran == ()


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_truthy_spellings_enable_it(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", flag)
    assert verify.verify_enabled()


@pytest.mark.parametrize("flag", ["0", "false", "NO", "off"])
def test_falsy_spellings_disable_it(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", flag)
    assert not verify.verify_enabled()


def test_unset_defaults_on(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", raising=False)
    assert verify.verify_enabled()


# ── pass / fail on real content ──────────────────────────────────────────────

def test_clean_python_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    result = verify.verify_changed_files(
        {"good.py": CLEAN_PY}, ["good.py"], repo, time.monotonic() + 30,
    )
    assert result.ok, result.reason
    assert result.ran == ("ruff",)


def test_lint_dirty_python_fails_and_names_ruff(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    result = verify.verify_changed_files(
        {"bad.py": LINT_DIRTY_PY}, ["bad.py"], repo, time.monotonic() + 30,
    )
    assert not result.ok
    assert "ruff" in result.reason
    # cmd_check() (agentic/acceptance.py) keeps only the exit code and the
    # LAST output line for every acceptance check it backs, not the full
    # report — this module inherits that terseness rather than working
    # around it, per "reuse, don't duplicate" (see module docstring).
    assert "exit 1" in result.reason


def test_unchanged_files_are_not_in_changed_files_are_ignored(tmp_path, monkeypatch):
    """Only files actually in ``changed_files`` are linted — a dirty file
    present in ``new_contents`` but not reported as changed must not block
    the write (mirrors how ``maybe_replace`` computes ``changed_files``)."""
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    result = verify.verify_changed_files(
        {"good.py": CLEAN_PY, "untouched_but_dirty.py": LINT_DIRTY_PY},
        ["good.py"],
        repo,
        time.monotonic() + 30,
    )
    assert result.ok, result.reason


def test_non_python_changed_files_are_not_linted(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    result = verify.verify_changed_files(
        {"config.json": "{not valid json but not this module's job}"},
        ["config.json"],
        repo,
        time.monotonic() + 30,
    )
    assert result.ok
    assert result.ran == ()


def test_no_changed_files_is_a_trivial_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    result = verify.verify_changed_files({}, [], repo, time.monotonic() + 30)
    assert result.ok


# ── budget: timeout escalates, it never passes silently ─────────────────────

def test_deadline_already_passed_is_unverified_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    expired = time.monotonic() - 1.0
    result = verify.verify_changed_files(
        {"bad.py": LINT_DIRTY_PY}, ["bad.py"], repo, expired,
    )
    assert not result.ok
    assert "time budget" in result.reason
    # Never ran anything — there was nothing left to run with.
    assert result.ran == ()


def test_deadline_already_passed_blocks_even_clean_content(tmp_path, monkeypatch):
    """A timeout is unverified, not verified-clean — clean content must be
    blocked exactly like dirty content when the budget is already gone,
    because this function never claims to have checked what it didn't."""
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    repo = _repo(tmp_path)
    expired = time.monotonic() - 1.0
    result = verify.verify_changed_files(
        {"good.py": CLEAN_PY}, ["good.py"], repo, expired,
    )
    assert not result.ok


# ── no linter reachable: documented degrade, not an escalation ──────────────

def test_no_linter_available_degrades_to_pass(tmp_path, monkeypatch):
    """Neither `ruff` nor `uv`/`uvx` on PATH: this is the pre-3.3 behaviour
    (syntax check only, already performed before this module runs), not a
    verification failure — see module docstring, 'BUDGET'."""
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")
    monkeypatch.setattr(verify, "_ruff_argv", lambda: None)
    repo = _repo(tmp_path)
    result = verify.verify_changed_files(
        {"bad.py": LINT_DIRTY_PY}, ["bad.py"], repo, time.monotonic() + 30,
    )
    assert result.ok
    assert result.ran == ()
    assert "no linter" in result.reason
