"""F10/D-164: scripts/check_live_matches_lock.py exit codes, using a fake interpreter."""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_matches_lock.py"

LOCK = """version = 1
[[package]]
name = "alpha"
version = "1.0.0"
[[package]]
name = "Beta_pkg"
version = "2.0.0"
[[package]]
name = "gamma"
version = "3.0.0"
[[package]]
name = "llm-routing"
version = "9.9.9"
source = { editable = "." }
"""


def _fake_python(tmp_path: Path, dists: dict[str, str]) -> str:
    exe = tmp_path / "fakepython"
    exe.write_text(f"#!/bin/sh\necho '{json.dumps(dists)}'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def _run(tmp_path: Path, dists: dict[str, str]):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK)
    return subprocess.run(
        [sys.executable, str(SCRIPT), _fake_python(tmp_path, dists), "--lock", str(lock)],
        capture_output=True, text=True,
    )


def test_match_exits_zero(tmp_path):
    r = _run(tmp_path, {"alpha": "1.0.0", "beta-pkg": "2.0.0", "extra": "5", "llm-routing": "1"})
    assert r.returncode == 0, r.stdout
    assert "overlap=2 mismatches=0" in r.stdout


def test_mismatch_exits_one_and_names_package(tmp_path):
    r = _run(tmp_path, {"alpha": "1.0.1", "beta-pkg": "2.0.0"})
    assert r.returncode == 1
    assert "MISMATCH alpha: live 1.0.1 != lock 1.0.0" in r.stdout


def test_empty_overlap_is_not_a_pass(tmp_path):
    assert _run(tmp_path, {"unrelated": "1"}).returncode == 2


def test_unqueryable_interpreter_exits_two(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK)
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "nope"), "--lock", str(lock)], capture_output=True, text=True)
    assert r.returncode == 2


def _run_lock_text(tmp_path: Path, text: str | None):
    lock = tmp_path / "uv.lock"
    if text is not None:
        lock.write_text(text)
    return subprocess.run(
        [sys.executable, str(SCRIPT), _fake_python(tmp_path, {"alpha": "1.0.0"}), "--lock", str(lock)],
        capture_output=True, text=True,
    )


def test_missing_lock_exits_two_without_traceback(tmp_path):
    r = _run_lock_text(tmp_path, None)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "cannot read lock" in r.stderr


def test_malformed_lock_exits_two_without_traceback(tmp_path):
    r = _run_lock_text(tmp_path, "x = [not toml")
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "cannot read lock" in r.stderr


def test_lock_without_packages_exits_two_without_traceback(tmp_path):
    r = _run_lock_text(tmp_path, "version = 1\n")
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "cannot read lock" in r.stderr


def test_old_python_without_tomllib_exits_two(tmp_path):
    # Simulate Python < 3.11: a sitecustomize-free shim that hides tomllib.
    shim = tmp_path / "hide_tomllib"
    shim.mkdir()
    (shim / "tomllib.py").write_text("raise ModuleNotFoundError(\"No module named 'tomllib'\")\n")
    import os
    env = dict(os.environ, PYTHONPATH=str(shim))
    r = subprocess.run([sys.executable, str(SCRIPT), "x"], capture_output=True, text=True, env=env)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "needs Python >= 3.11" in r.stderr
