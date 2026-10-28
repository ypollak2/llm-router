"""P2.12 (P212-1): the install receipt, ``install --auto`` and the host matrix.

Every test runs the real installers against a tmp HOME (as test_p212_upgrade_from_15_4_0
does): HOME, ``Path.home``, ``LLM_ROUTER_HOME`` and the cwd point into tmp_path,
``LLM_ROUTER_CLAUDE_DIR`` and ``XDG_CONFIG_HOME`` (claw-code detection) are cleared, host
detection ignores the developer's PATH, and the seat probe is replaced.
"""
from __future__ import annotations

import builtins
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from llm_router import host_detect, seats
from llm_router import install_hooks as ih
from llm_router.commands import install

AUTO_WIRED = {"claude-code", "codex", "gemini-cli"}  # what run_full wires with hook scripts
MATRIX = sorted(host_detect._HOSTS)


def _point_at(home: Path, monkeypatch) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(home)
    detect = host_detect.detect_hosts
    monkeypatch.setattr(host_detect, "detect_hosts",
                        lambda **k: detect(**{"home": home, "which": lambda b: None, **k}))
    monkeypatch.setattr(seats, "refresh_seats",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no seat probe in tests")))
    return home


def _add_host(home: Path, host: str) -> None:
    """Make ``host`` present: its config dir holding a file of the host's own."""
    d = host_detect._config_dir(host_detect._HOSTS[host][1], home, sys.platform, os.environ)
    d.mkdir(parents=True, exist_ok=True)
    (d / "host-own-state.json").write_text("{}")


def _bundled(host: str) -> dict[str, int]:
    return {src: ih._hook_version(ih._HOOKS_SRC / src) for src in install._receipt_hook_files(host)}


@pytest.fixture
def home(tmp_path, monkeypatch):
    return _point_at(tmp_path / "home", monkeypatch)


def test_receipt_rows_carry_the_installed_hook_versions(home, capsys):
    for h in ("codex", "gemini-cli"):
        _add_host(home, h)

    result = install.run_full(interactive=False)

    path = install.receipt_path()
    on_disk = json.loads(path.read_text())
    assert on_disk == result.receipt
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rows = on_disk["hosts"]
    assert set(rows) == AUTO_WIRED
    for host in AUTO_WIRED:
        assert rows[host]["status"] == "wired", rows[host]
        assert rows[host]["hook_version"] == _bundled(host) and _bundled(host), host
    assert rows["claude-code"]["via"] == "settings" and rows["codex"]["via"] == "files"
    assert on_disk["digest"] == install.receipt_digest(on_disk)
    assert install.receipt_failures(on_disk) == []
    line = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("receipt:")]
    assert line == [f"receipt: {on_disk['digest'][:8]} hosts=3/3 version={on_disk['package_version']}"]


def test_auto_never_prompts_and_matches_run_full_on_a_twin_home(tmp_path, monkeypatch):
    def no_prompt(*_a, **_k):
        raise AssertionError("install --auto called input()")

    monkeypatch.setattr(builtins, "input", no_prompt)
    a = _point_at(tmp_path / "a", monkeypatch)
    for h in ("codex", "gemini-cli"):
        _add_host(a, h)
    assert install.cmd_install(["--auto"]) == 0
    auto = install.read_receipt()

    b = _point_at(tmp_path / "b", monkeypatch)
    for h in ("codex", "gemini-cli"):
        _add_host(b, h)
    direct = install.run_full(interactive=False).receipt

    assert auto is not None and auto["digest"] == direct["digest"]
    assert auto["ts"] and set(auto["hosts"]) == AUTO_WIRED


@pytest.mark.parametrize("flag", ["--host", "--project", "--check", "--headless"])
def test_auto_refuses_a_flag_that_selects_another_install(home, flag, capsys):
    assert install.cmd_install(["--auto", flag]) == 2
    assert "--auto" in capsys.readouterr().err
    assert install.read_receipt() is None


def test_matrix_has_one_cell_per_detectable_host():
    assert MATRIX == sorted(host_detect._HOSTS) and len(MATRIX) >= 13, MATRIX


@pytest.mark.parametrize("host", MATRIX)
def test_host_matrix(home, host):
    """P2.12-a2: a HOME where only ``host`` is present; 0 error rows, every not_wired row
    has a reason, and the host has its own row (it was detected, not silently dropped)."""
    _add_host(home, host)

    receipt = install.run_full(interactive=False).receipt

    assert install.receipt_failures(receipt) == [], receipt
    assert host in receipt["hosts"], f"{host} detected but has no receipt row"
    row = receipt["hosts"][host]
    assert row["status"] == ("wired" if host in AUTO_WIRED else "not_wired"), row
    assert install.read_receipt() == receipt


def test_negative_control_a_raising_installer_is_an_error_row(home, monkeypatch, capsys):
    """The matrix checker must go RED when a host fails: codex's installer raises, the
    install goes on to the other hosts, the codex row is ``error``, ``--auto`` exits 1."""
    for h in ("codex", "gemini-cli"):
        _add_host(home, h)

    def boom():
        raise RuntimeError("codex config.toml is read-only")

    monkeypatch.setattr(install, "_install_codex_files", boom)

    assert install.cmd_install(["--auto"]) == 1
    receipt = install.read_receipt()
    assert receipt["hosts"]["codex"] == {
        "status": "error", "reason": "RuntimeError: codex config.toml is read-only",
        "via": "files", "hook_version": None,
    }
    assert receipt["hosts"]["gemini-cli"]["status"] == "wired"  # one failure does not abort the rest
    failures = install.receipt_failures(receipt)
    assert failures == ["codex: error: RuntimeError: codex config.toml is read-only"]
    out = capsys.readouterr().out
    assert "install finished with errors" in out and "hosts=2/3" in out


def test_negative_control_a_missing_hook_file_is_an_error_row(home, monkeypatch):
    real = install._install_gemini_cli_files

    def drops_a_hook():
        actions = real()
        install._receipt_hook_files("gemini-cli")["gemini-cli-auto-route.py"].unlink()
        return actions

    _add_host(home, "gemini-cli")
    monkeypatch.setattr(install, "_install_gemini_cli_files", drops_a_hook)

    receipt = install.run_full(interactive=False).receipt

    assert receipt["hosts"]["gemini-cli"]["status"] == "error"
    assert install.receipt_failures(receipt) == [
        "gemini-cli: error: hook missing after install: gemini-cli-auto-route.py"]


def test_checker_is_red_on_a_missing_or_wrong_receipt(home):
    install.run_full(interactive=False)
    good = install.read_receipt()
    assert install.receipt_failures(good) == []

    assert install.receipt_failures(None) == ["no install receipt"]
    assert install.receipt_failures({**good, "hosts": {}}) == ["install receipt has no host rows"]
    unreasoned = json.loads(json.dumps(good))
    unreasoned["hosts"]["kimi"] = install.not_wired_row("")
    unreasoned["digest"] = install.receipt_digest(unreasoned)
    assert install.receipt_failures(unreasoned) == ["kimi: not_wired without a reason"]
    tampered = json.loads(json.dumps(good))
    tampered["hosts"]["claude-code"]["hook_version"]["auto-route.py"] = 1
    assert install.receipt_failures(tampered) == ["install receipt digest does not match its rows"]


def test_digest_ignores_ts_via_and_reason_but_not_versions():
    row = {"status": "wired", "reason": None, "via": "settings", "hook_version": {"auto-route.py": 78}}
    base = install.build_receipt({"claude-code": row}, package_version="1.0")
    plugin = install.build_receipt({"claude-code": {**row, "via": "plugin"}}, package_version="1.0")
    assert base["digest"] == plugin["digest"]
    bumped = install.build_receipt({"claude-code": {**row, "hook_version": {"auto-route.py": 79}}},
                                   package_version="1.0")
    assert bumped["digest"] != base["digest"]
    assert install.build_receipt({"claude-code": row}, package_version="1.1")["digest"] != base["digest"]
    assert re.fullmatch(r"[0-9a-f]{64}", base["digest"])
