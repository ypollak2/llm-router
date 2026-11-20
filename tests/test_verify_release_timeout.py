"""verify-release.py run_tests(): a pytest timeout is a failure (F2), as in release.sh (#942).

The old rule passed a TimeoutExpired whenever "[100%]" was in the captured output, which
accepted runs whose results were never read. subprocess.run is replaced by a fake, so no
test suite, model or network runs here.
"""
from __future__ import annotations

import importlib.util
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


def _run_tests_with(vr, monkeypatch, effect):
    def fake_run(cmd, **kwargs):
        if isinstance(effect, BaseException):
            raise effect
        return effect
    monkeypatch.setattr(vr.subprocess, "run", fake_run)
    return vr.run_tests()


@pytest.mark.parametrize("stdout", [
    b"[ 99%]\n[100%]\n",   # the old pass: marker printed, then the run hung
    "[100%]\n",            # same, text mode
    b"[ 50%]\n",           # never reached the marker
    None,                  # no output at all
])
def test_a_timeout_is_a_failure_even_after_100_percent(vr, monkeypatch, capsys, stdout):
    exc = subprocess.TimeoutExpired(cmd=["uv"], timeout=600, output=stdout)
    assert _run_tests_with(vr, monkeypatch, exc) is False
    assert "timed out" in capsys.readouterr().out


def test_control_exit_zero_still_passes(vr, monkeypatch):
    done = subprocess.CompletedProcess(["uv"], 0, stdout="[100%]\n", stderr="")
    assert _run_tests_with(vr, monkeypatch, done) is True


def test_control_nonzero_exit_still_fails(vr, monkeypatch):
    done = subprocess.CompletedProcess(["uv"], 1, stdout="1 failed\n", stderr="")
    assert _run_tests_with(vr, monkeypatch, done) is False
