from __future__ import annotations

import importlib.util
import json
import os
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
    assert out.splitlines() == ["PASS import_guard", "PASS grader_python", "PASS node_on_agent_path", "PASS claude_token"]
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
    assert out.splitlines() == ["FAIL import_guard", "FAIL grader_python", "FAIL node_on_agent_path", "FAIL claude_token"]
    assert "SECRET" not in out


def test_smoke_empty_python_set_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(smoke.h, "agent_path", lambda p: p)
    assert smoke.check_import_guard({"PATH": str(tmp_path)}) is False
    assert os.environ["PATH"] == str(tmp_path)


def test_smoke_grader_python_fails_without_pytest(tmp_path):
    no_pytest = tmp_path / "np"
    no_pytest.mkdir()
    exe = no_pytest / "python3"
    exe.write_text("#!/bin/sh\ncase \"$*\" in *'import pytest'*) exit 1;; esac\nexit 0\n")
    exe.chmod(0o755)
    env = {"PATH": str(no_pytest)}
    assert smoke.check_import_guard(env) is True
    assert smoke.check_grader_python(env) is False


def test_smoke_removes_its_own_scratch_dir(tmp_path, monkeypatch):
    made = []
    real = smoke.tempfile.mkdtemp

    def tracking(*a, **k):
        made.append(real(*a, dir=str(tmp_path), **k))
        return made[-1]

    monkeypatch.setattr(smoke.tempfile, "mkdtemp", tracking)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(smoke.h, "agent_path", lambda p: p)
    smoke.main([])
    assert len(made) == 1 and not Path(made[0]).exists()


def _fake_claude_call(monkeypatch, tmp_path, persist: bool):
    token = "FAKE-OAUTH-TOKEN-0123456789"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)

    def run(argv, **kw):
        if persist:  # Claude Code writing the auth value under its scratch HOME
            (Path(kw["env"]["HOME"]) / "state.json").write_text(token)
        return smoke.subprocess.CompletedProcess(
            argv, 0, json.dumps({"is_error": False, "result": "ok"}), "")

    monkeypatch.setattr(smoke.h, "assert_no_llm_router_import", lambda env: None)
    monkeypatch.setattr(smoke.subprocess, "run", run)
    return smoke.check_claude_call(tmp_path / "scr")


def test_smoke_claude_call_passes_when_auth_not_persisted(tmp_path, monkeypatch):
    assert _fake_claude_call(monkeypatch, tmp_path, persist=False) is True


def test_smoke_claude_call_fails_when_auth_persisted_under_scratch_home(tmp_path, monkeypatch):
    assert _fake_claude_call(monkeypatch, tmp_path, persist=True) is False
