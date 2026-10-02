"""scripts/measure_zce_warm.py: pytest-availability gate and per-fixture test classification.

The bug: every "served" edit's `fixture_test_passes` came from running
`sys.executable -m pytest ...`. When invoked via an interpreter with no pytest
(e.g. the uv tool env `~/.local/share/uv/tools/llm-routing/bin/python`), that
subprocess can't even start pytest -- but the old code collapsed "could not
run" into `fixture_test_passes: False`, reporting a false "the edit broke the
fixture" for every single row (a false 0/11 in practice).

These tests cover the two pure functions the fix introduces:
  - `_pytest_available` / `_require_pytest_or_die`: fail loudly, before any
    Ollama/fixture work, if the interpreter can't run pytest at all.
  - `_run_fixture_test`: never returns `(False, ...)` for a run that could not
    happen -- timeout, missing binary, or a "no module named pytest" pytest
    output are all `(None, <reason>)`; only an actual pass/fail run returns
    `(True/False, "")`.
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_zce_warm.py"
_spec = importlib.util.spec_from_file_location("measure_zce_warm", _PATH)
mzw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mzw)


# ── _pytest_available / _require_pytest_or_die ──────────────────────────────

def test_pytest_available_true_for_current_interpreter():
    # The interpreter running this test suite has pytest importable by definition.
    assert mzw._pytest_available(sys.executable) is True


def test_pytest_available_false_for_nonexistent_binary():
    assert mzw._pytest_available("/no/such/python-binary-xyz") is False


def test_pytest_available_false_for_a_python_with_no_pytest(tmp_path):
    # A real python3 that errors out of `-m pytest` looks like "unavailable",
    # not a crash -- simulate via a tiny wrapper script that always exits 1.
    fake = tmp_path / "fake-python"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    assert mzw._pytest_available(str(fake)) is False


def test_require_pytest_or_die_passes_silently_when_available():
    mzw._require_pytest_or_die(sys.executable)  # must not raise


def test_require_pytest_or_die_exits_loudly_when_unavailable(capsys):
    with pytest.raises(SystemExit) as exc_info:
        mzw._require_pytest_or_die("/no/such/python-binary-xyz")
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "pytest is not importable" in err
    assert "/no/such/python-binary-xyz" in err
    assert "--with pytest" in err  # names the fix, not just the symptom


# ── _run_fixture_test ────────────────────────────────────────────────────────

def _write_passing_test(tmp_path: Path) -> Path:
    t = tmp_path / "test_ok.py"
    t.write_text("def test_ok():\n    assert True\n")
    return t


def _write_failing_test(tmp_path: Path) -> Path:
    t = tmp_path / "test_fail.py"
    t.write_text("def test_fail():\n    assert False\n")
    return t


def test_run_fixture_test_reports_true_for_a_real_pass(tmp_path):
    test_ok, reason = mzw._run_fixture_test(sys.executable, _write_passing_test(tmp_path), tmp_path)
    assert test_ok is True
    assert reason == ""


def test_run_fixture_test_reports_false_for_a_real_failure(tmp_path):
    test_ok, reason = mzw._run_fixture_test(sys.executable, _write_failing_test(tmp_path), tmp_path)
    assert test_ok is False
    assert reason == ""


def test_run_fixture_test_reports_none_not_false_when_binary_missing(tmp_path):
    test_ok, reason = mzw._run_fixture_test(
        "/no/such/python-binary-xyz", _write_passing_test(tmp_path), tmp_path,
    )
    assert test_ok is None
    assert "could not start" in reason


def test_run_fixture_test_reports_none_not_false_on_timeout(tmp_path, monkeypatch):
    import subprocess as sp

    def _raise_timeout(*a, **k):
        raise sp.TimeoutExpired(cmd=a[0], timeout=60)

    monkeypatch.setattr(mzw.subprocess, "run", _raise_timeout)
    test_ok, reason = mzw._run_fixture_test(sys.executable, _write_passing_test(tmp_path), tmp_path)
    assert test_ok is None
    assert "timed out" in reason


def test_run_fixture_test_reports_none_not_false_when_pytest_missing(tmp_path, monkeypatch):
    # Simulate the exact original bug: the subprocess runs, but the interpreter
    # has no pytest, so it exits non-zero with "No module named pytest" on
    # stderr. This must come back as (None, reason), never (False, "").
    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = textwrap.dedent("""\
            /usr/bin/python3: No module named pytest
            """)

    monkeypatch.setattr(mzw.subprocess, "run", lambda *a, **k: _FakeResult())
    test_ok, reason = mzw._run_fixture_test(sys.executable, _write_passing_test(tmp_path), tmp_path)
    assert test_ok is None
    assert "no pytest" in reason
