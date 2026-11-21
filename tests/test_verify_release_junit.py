"""verify-release.py junit gate: _junit_verdict and run_tests with junitxml.

Tests for the PR #942 rule: pass only when >=1 test ran, 0 failures, 0 errors.
A run that collected nothing is not a pass; a missing/unparseable junit file fails.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release" / "verify-release.py"


@pytest.fixture
def vr():
    spec = importlib.util.spec_from_file_location("verify_release_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _junit_verdict unit tests (direct)
# ---------------------------------------------------------------------------

def _write_junit(tmp_path: Path, xml: str) -> Path:
    p = tmp_path / "test.xml"
    p.write_text(xml)
    return p


def test_junit_clean_pass(vr, tmp_path):
    """Clean junit with tests -> pass."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" tests="3" failures="0" errors="0" skipped="0">'
        '<testcase name="a" classname="m" />'
        '<testcase name="b" classname="m" />'
        '<testcase name="c" classname="m" />'
        '</testsuite></testsuites>'
    )
    passed, reason = vr._junit_verdict(str(_write_junit(tmp_path, xml)))
    assert passed is True
    assert "3 tests" in reason
    assert "3 ran" in reason
    assert "0 failures" in reason
    assert "0 errors" in reason


def test_junit_with_failures_fails(vr, tmp_path):
    """exit 0 but junit shows a failure -> fail."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" tests="2" failures="1" errors="0" skipped="0">'
        '<testcase name="a" classname="m" />'
        '<testcase name="b" classname="m">'
        '<failure message="assert False"/>'
        '</testcase></testsuite></testsuites>'
    )
    passed, reason = vr._junit_verdict(str(_write_junit(tmp_path, xml)))
    assert passed is False
    assert "1 failures" in reason


def test_junit_with_errors_fails(vr, tmp_path):
    """exit 0 but junit shows errors -> fail."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" tests="2" failures="0" errors="1" skipped="0">'
        '<testcase name="a" classname="m" />'
        '<testcase name="b" classname="m">'
        '<error message="oops"/>'
        '</testcase></testsuite></testsuites>'
    )
    passed, reason = vr._junit_verdict(str(_write_junit(tmp_path, xml)))
    assert passed is False
    assert "1 errors" in reason


def test_junit_zero_collected_fails(vr, tmp_path):
    """exit 0 with 0 tests collected -> fail."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" tests="0" failures="0" errors="0" skipped="0"></testsuite></testsuites>'
    )
    passed, reason = vr._junit_verdict(str(_write_junit(tmp_path, xml)))
    assert passed is False
    assert "0 ran" in reason


def test_junit_missing_file_fails(vr):
    """Missing junit -> fail."""
    passed, reason = vr._junit_verdict("/nonexistent/path.xml")
    assert passed is False
    assert "no readable junit file" in reason


def test_junit_unparseable_fails(vr, tmp_path):
    """Unparseable junit -> fail."""
    p = _write_junit(tmp_path, "not xml at all <><>")
    passed, reason = vr._junit_verdict(str(p))
    assert passed is False
    assert "no readable junit file" in reason


def test_junit_skipped_not_ran(vr, tmp_path):
    """tests=5, skipped=5 -> ran=0 -> fail."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" tests="5" failures="0" errors="0" skipped="5"></testsuite></testsuites>'
    )
    passed, reason = vr._junit_verdict(str(_write_junit(tmp_path, xml)))
    assert passed is False
    assert "0 ran" in reason


# ---------------------------------------------------------------------------
# run_tests integration (subprocess.run faked; the fake writes the junit file
# at the --junitxml= path that run_tests passed, as pytest would)
# ---------------------------------------------------------------------------

CLEAN = (
    '<testsuites><testsuite name="t" tests="1" failures="0" errors="0" skipped="0">'
    '<testcase name="a" classname="m"/></testsuite></testsuites>'
)
FAILED = (
    '<testsuites><testsuite name="t" tests="2" failures="1" errors="0" skipped="0">'
    '<testcase name="a" classname="m"/>'
    '<testcase name="b" classname="m"><failure message="x"/></testcase>'
    '</testsuite></testsuites>'
)
EMPTY = '<testsuites><testsuite name="t" tests="0" failures="0" errors="0" skipped="0"/></testsuites>'


def _run_with_junit(vr, monkeypatch, xml, returncode=0):
    """Run vr.run_tests() with a fake pytest that exits `returncode` and writes `xml`
    (None = writes nothing, and removes the empty file run_tests pre-created)."""
    def fake_run(cmd, **kwargs):
        path = next(c for c in cmd if c.startswith("--junitxml=")).split("=", 1)[1]
        if xml is None:
            os.unlink(path)
        else:
            Path(path).write_text(xml)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")
    monkeypatch.setattr(vr.subprocess, "run", fake_run)
    return vr.run_tests()


def test_run_tests_clean_junit_passes(vr, monkeypatch):
    assert _run_with_junit(vr, monkeypatch, CLEAN) is True


def test_run_tests_exit_zero_but_junit_failure_fails(vr, monkeypatch, capsys):
    assert _run_with_junit(vr, monkeypatch, FAILED) is False
    assert "1 failures" in capsys.readouterr().out


def test_run_tests_exit_zero_but_nothing_collected_fails(vr, monkeypatch, capsys):
    assert _run_with_junit(vr, monkeypatch, EMPTY) is False
    assert "0 ran" in capsys.readouterr().out


def test_run_tests_exit_zero_but_missing_junit_fails(vr, monkeypatch, capsys):
    assert _run_with_junit(vr, monkeypatch, None) is False
    assert "no readable junit file" in capsys.readouterr().out


def test_run_tests_exit_zero_but_empty_junit_file_fails(vr, monkeypatch, capsys):
    assert _run_with_junit(vr, monkeypatch, "") is False
    assert "no readable junit file" in capsys.readouterr().out


def test_run_tests_nonzero_exit_fails_even_with_clean_junit(vr, monkeypatch):
    assert _run_with_junit(vr, monkeypatch, CLEAN, returncode=1) is False


def test_run_tests_removes_its_temp_junit_file(vr, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        path = next(c for c in cmd if c.startswith("--junitxml=")).split("=", 1)[1]
        Path(path).write_text(CLEAN)
        seen["path"] = path
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(vr.subprocess, "run", fake_run)
    assert vr.run_tests() is True
    assert not os.path.exists(seen["path"])


def test_run_tests_removes_its_temp_junit_file_after_a_timeout(vr, monkeypatch, capsys):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["path"] = next(c for c in cmd if c.startswith("--junitxml=")).split("=", 1)[1]
        assert os.path.exists(seen["path"])
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
    monkeypatch.setattr(vr.subprocess, "run", fake_run)
    assert vr.run_tests() is False
    assert "timed out" in capsys.readouterr().out
    assert not os.path.exists(seen["path"])


def test_a_failed_temp_file_removal_is_reported_not_swallowed(vr, monkeypatch, capsys):
    def fake_unlink(path):
        raise PermissionError("denied")
    monkeypatch.setattr(vr.os, "unlink", fake_unlink)
    _run_with_junit_nounlink = _run_with_junit  # fake pytest below writes via Path, not os.unlink
    assert _run_with_junit_nounlink(vr, monkeypatch, CLEAN) is True
    err = capsys.readouterr().err
    assert "could not remove temp junit file" in err
    assert "denied" in err
