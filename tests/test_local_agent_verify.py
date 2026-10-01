"""Plan 3.3: pre-write lint verification for scoped zero-Claude edits.

``verify_changed_files`` is the gate ``zero_claude_edit.maybe_replace`` runs
between validation (exact-once match + syntax check) and the write. These
tests exercise it directly and behaviourally: real ruff, real pass/fail
outcomes, real temp trees — never a source-text assertion.

Contract: lint ORIGINAL and CANDIDATE with the project's own ruff config and
block only on violations NEW in the candidate; no project ruff config means
skip (syntax check only); a timeout/ruff error means unverified (blocked).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from llm_router.local_agent import verify

CLEAN_PY = "def add(a, b):\n    return a + b\n"
# F401 (unused import) is in the rule set the fixtures below opt into.
DEBT_PY = "import os\n\n\ndef add(a, b):\n    return a + b\n"
RUFF_F = '[tool.ruff.lint]\nselect = ["F"]\n'


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """A repo root; ``files`` maps repo-relative path -> text."""
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _verify(root, rel, original, candidate, deadline=30.0):
    return verify.verify_changed_files(
        {rel: candidate}, {rel: original}, [rel], root, time.monotonic() + deadline,
    )


@pytest.fixture(autouse=True)
def _ruff_required():
    if shutil.which("ruff") is None and shutil.which("uvx") is None:
        pytest.skip("neither ruff nor uv/uvx on PATH — cannot exercise the real linter")


@pytest.fixture(autouse=True)
def _verify_on(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "1")


# ── enabled/disabled gate ────────────────────────────────────────────────────

def test_disabled_skips_verification_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "0")
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = _verify(root, "bad.py", CLEAN_PY, DEBT_PY)
    assert result.ok
    assert result.ran == ()


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_truthy_spellings_enable_it(monkeypatch, flag):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", flag)
    assert verify.verify_enabled()


@pytest.mark.parametrize("flag", ["0", "false", "NO", "off"])
def test_falsy_spellings_disable_it(monkeypatch, flag):
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", flag)
    assert not verify.verify_enabled()


def test_unset_defaults_on(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ZERO_CLAUDE_VERIFY", raising=False)
    assert verify.verify_enabled()


# ── pass / fail with a baseline ──────────────────────────────────────────────

def test_clean_candidate_passes(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = _verify(root, "good.py", CLEAN_PY, CLEAN_PY + "\n\ndef sub(a, b):\n    return a - b\n")
    assert result.ok, result.reason
    assert result.ran == ("ruff",)


def test_new_violation_blocks_and_names_code_message_and_line(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = _verify(root, "bad.py", CLEAN_PY, DEBT_PY)
    assert not result.ok
    assert "F401" in result.reason
    assert "`os` imported but unused" in result.reason
    assert "line 1" in result.reason
    assert "bad.py" in result.reason


def test_preexisting_violation_untouched_by_edit_passes(tmp_path):
    """Blocker 1: lint debt that was already there must not block every edit."""
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    edited = DEBT_PY.replace("return a + b", "return a + b + 0")
    result = _verify(root, "debt.py", DEBT_PY, edited)
    assert result.ok, result.reason


def test_line_shift_does_not_turn_old_debt_into_a_new_violation(tmp_path):
    """Compared by rule code, not line number: prepending lines moves the old
    violation without adding one."""
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    shifted = '"""Doc."""\n\n# a\n# b\n' + DEBT_PY
    result = _verify(root, "debt.py", DEBT_PY, shifted)
    assert result.ok, result.reason


def test_renaming_already_unused_import_is_not_blamed_on_the_edit(tmp_path):
    """The message embeds the name, so a (code, message) key would call an
    unused `os` swapped for an unused `sys` a new F401."""
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    swapped = DEBT_PY.replace("import os", "import sys")
    assert swapped != DEBT_PY
    result = _verify(root, "debt.py", DEBT_PY, swapped)
    assert result.ok, result.reason


def test_new_violation_in_file_with_existing_debt_blocks_naming_new_code(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    candidate = DEBT_PY + "\n\ndef f():\n    unused = 1\n"  # adds F841, F401 unchanged
    result = _verify(root, "debt.py", DEBT_PY, candidate)
    assert not result.ok
    assert "F841" in result.reason
    assert "F401" not in result.reason  # the old debt is not blamed on the edit


def test_named_violations_are_capped(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    candidate = "".join(f"import mod{i}\n" for i in range(5)) + CLEAN_PY
    result = _verify(root, "many.py", CLEAN_PY, candidate)
    assert not result.ok
    assert "5 new ruff violation(s)" in result.reason
    assert "(+2 more)" in result.reason
    assert result.reason.count("F401") == 3


def test_unchanged_files_not_in_changed_files_are_ignored(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = verify.verify_changed_files(
        {"good.py": CLEAN_PY, "other.py": DEBT_PY}, {"good.py": CLEAN_PY, "other.py": CLEAN_PY},
        ["good.py"], root, time.monotonic() + 30,
    )
    assert result.ok, result.reason


def test_non_python_changed_files_are_not_linted(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = _verify(root, "config.json", "{}", "{not json but not this module's job}")
    assert result.ok
    assert result.ran == ()


def test_no_changed_files_is_a_trivial_pass(tmp_path):
    root = _repo(tmp_path)
    assert verify.verify_changed_files({}, {}, [], root, time.monotonic() + 30).ok


# ── blocker 2: only a config the repo opted into counts ─────────────────────

def test_no_ruff_config_skips_lint_syntax_check_only(tmp_path):
    root = _repo(tmp_path)  # no config anywhere
    result = _verify(root, "bad.py", CLEAN_PY, DEBT_PY)
    assert result.ok
    assert result.ran == ()
    assert "no ruff config" in result.reason


def test_pyproject_without_tool_ruff_does_not_count(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname = "x"\nversion = "0"\n'})
    result = _verify(root, "bad.py", CLEAN_PY, DEBT_PY)
    assert result.ok
    assert result.ran == ()
    assert "no ruff config" in result.reason


@pytest.mark.parametrize("name", ["ruff.toml", ".ruff.toml"])
def test_standalone_ruff_toml_counts(tmp_path, name):
    root = _repo(tmp_path, {name: 'lint.select = ["F"]\n'})
    result = _verify(root, "bad.py", CLEAN_PY, DEBT_PY)
    assert not result.ok
    assert "F401" in result.reason


def test_nested_package_config_is_found_walking_up(tmp_path):
    """No root config; the package's own config governs files under it."""
    root = _repo(tmp_path, {"pkg/pyproject.toml": RUFF_F, "pkg/sub/__init__.py": ""})
    result = _verify(root, "pkg/sub/mod.py", CLEAN_PY, DEBT_PY)
    assert not result.ok
    assert "F401" in result.reason


def test_nearest_config_wins_over_root_config(tmp_path):
    root = _repo(tmp_path, {
        "pyproject.toml": '[tool.ruff.lint]\nselect = ["E9"]\n',  # root would not flag F401
        "pkg/ruff.toml": 'lint.select = ["F"]\n',
    })
    assert not _verify(root, "pkg/mod.py", CLEAN_PY, DEBT_PY).ok
    assert _verify(root, "top.py", CLEAN_PY, DEBT_PY).ok  # root config: E9 only


def test_file_outside_nested_config_is_not_governed_by_it(tmp_path):
    root = _repo(tmp_path, {"pkg/pyproject.toml": RUFF_F})
    result = _verify(root, "other/mod.py", CLEAN_PY, DEBT_PY)
    assert result.ok
    assert "no ruff config" in result.reason


# ── should-fix 3: per-file-ignores keyed on repo-relative paths ─────────────

PFI = '[tool.ruff.lint]\nselect = ["F"]\nper-file-ignores = {"tests/*.py" = ["F401"]}\n'


def test_per_file_ignores_on_tests_glob_is_honoured(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": PFI})
    in_tests = _verify(root, "tests/test_x.py", CLEAN_PY, DEBT_PY)
    assert in_tests.ok, in_tests.reason
    elsewhere = _verify(root, "src/x.py", CLEAN_PY, DEBT_PY)
    assert not elsewhere.ok
    assert "F401" in elsewhere.reason


def test_per_file_ignores_in_nested_config_are_relative_to_that_config(tmp_path):
    root = _repo(tmp_path, {"pkg/pyproject.toml": PFI})
    assert _verify(root, "pkg/tests/test_x.py", CLEAN_PY, DEBT_PY).ok
    assert not _verify(root, "pkg/src/x.py", CLEAN_PY, DEBT_PY).ok


# ── budget and errors: unverified escalates, never passes ────────────────────

def test_deadline_already_passed_is_unverified_not_a_pass(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = _verify(root, "good.py", CLEAN_PY, CLEAN_PY, deadline=-1.0)
    assert not result.ok
    assert "time budget" in result.reason
    assert result.ran == ()


def test_ruff_failing_outright_is_unverified_not_a_pass(tmp_path):
    root = _repo(tmp_path, {"pyproject.toml": '[tool.ruff.lint]\nselect = ["NOT-A-RULE"]\n'})
    result = _verify(root, "good.py", CLEAN_PY, CLEAN_PY)
    assert not result.ok
    assert "unverified" in result.reason


def test_unlintable_original_is_unverified_not_a_pass(tmp_path, monkeypatch):
    """If the baseline run cannot complete, a candidate with no visible
    violations must still not pass."""
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    calls = []

    def flaky(argv, config, cwd, rels, remaining):
        calls.append(cwd.name)
        raise verify._Unverified("ruff check timed out after 1.0s")

    monkeypatch.setattr(verify, "_run_ruff", flaky)
    result = _verify(root, "good.py", CLEAN_PY, CLEAN_PY)
    assert not result.ok
    assert "unverified" in result.reason
    assert calls == ["original"]  # the baseline is linted first, and its failure is final


def test_no_linter_available_degrades_to_pass(tmp_path, monkeypatch):
    """Neither `ruff` nor `uv`/`uvx` on PATH: the pre-3.3 behaviour (syntax
    check only, already done earlier), not a verification failure."""
    monkeypatch.setattr(verify, "_ruff_argv", lambda: None)
    root = _repo(tmp_path, {"pyproject.toml": RUFF_F})
    result = _verify(root, "bad.py", CLEAN_PY, DEBT_PY)
    assert result.ok
    assert result.ran == ()
    assert "no linter" in result.reason
