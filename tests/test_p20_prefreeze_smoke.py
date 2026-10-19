from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("p20_prefreeze_smoke_t", ROOT / "scripts/p20_prefreeze_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def stub(directory: Path, name: str, code: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text(f"#!/bin/sh\nexit {code}\n")
    exe.chmod(0o755)


def test_smoke_prints_pass_fail_only(tmp_path, monkeypatch, capsys):
    clean = tmp_path / "clean"
    stub(clean, "python3", 0)
    stub(clean, "node", 0)
    token = tmp_path / "tok"
    token.write_text("SECRET-VALUE")
    token.chmod(0o600)
    monkeypatch.setenv("PATH", str(clean))
    monkeypatch.setattr(smoke.h, "agent_path", lambda p: p)
    monkeypatch.setenv(smoke.h.CLAUDE_CREDENTIALS_ENV, str(token))
    assert smoke.main(["--scratch", str(tmp_path / "scr")]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["PASS import_guard", "PASS node_on_agent_path", "PASS claude_token"]
    assert "SECRET" not in out and str(token) not in out


def test_smoke_fails_on_importable_python_and_loose_token(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad"
    stub(bad, "python3", 1)
    token = tmp_path / "tok"
    token.write_text("SECRET-VALUE")
    token.chmod(0o644)
    monkeypatch.setenv("PATH", str(bad))
    monkeypatch.setattr(smoke.h, "agent_path", lambda p: p)
    monkeypatch.setenv(smoke.h.CLAUDE_CREDENTIALS_ENV, str(token))
    assert smoke.main(["--scratch", str(tmp_path / "scr")]) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == ["FAIL import_guard", "FAIL node_on_agent_path", "FAIL claude_token"]
    assert "SECRET" not in out


def test_smoke_empty_python_set_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(smoke.h, "agent_path", lambda p: p)
    assert smoke.check_import_guard({"PATH": str(tmp_path)}) is False
    assert os.environ["PATH"] == str(tmp_path)
