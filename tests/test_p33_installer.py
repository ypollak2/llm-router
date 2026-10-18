"""P33-3: installer / uninstaller / doctor wiring for the daily KPI scheduler entry (D-220: explicit consent).

Every test runs under a tmp HOME with a fake runner. The fixture asserts that Path.home() is under tmp, and no
test passes the real subprocess.run to anything, so nothing here can reach launchctl or the real home."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import daily_offload as do
from llm_router import daily_schedule as ds
from llm_router import host_detect, install_hooks, install_manifest, proxy_default as pd, seats
from llm_router.commands import doctor, install, uninstall
from llm_router.commands import proxy_default as pdc


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_SCHEDULED", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.chdir(h)
    assert str(Path.home()).startswith(str(tmp_path)), "fixture must never see the real home"
    detect = host_detect.detect_hosts
    monkeypatch.setattr(host_detect, "detect_hosts", lambda: detect(home=h, which=lambda b: None))
    monkeypatch.setattr(install_hooks, "install", lambda: ["Claude hooks installed"])
    monkeypatch.setattr(install_hooks, "claw_code_settings_path", lambda: None)
    monkeypatch.setattr(install_hooks, "check_api_keys", lambda: ["keys"])
    monkeypatch.setattr(seats, "refresh_seats",
                        lambda: SimpleNamespace(summary_line=lambda: "seats", free_bucket=lambda: []))
    monkeypatch.setattr(install, "_maybe_enable_claude_subscription", lambda found: None)
    return h


class FakeRunner:
    def __init__(self):
        self.cmds: list[str] = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _sched(home, runner):
    return {"home": home, "system": "Darwin", "runner": runner}


def _plist(home):
    return home / "Library" / "LaunchAgents" / f"{ds.LABEL}.plist"


def _daily_records():
    return [r for r in install_manifest.records() if "kpi-daily" in str(r.get("path", ""))]


def test_headless_with_flag_records_manifest_entries(home):
    fr = FakeRunner()
    install.run_full(interactive=False, flags=["--schedule-daily"], schedule=_sched(home, fr))
    assert _plist(home).exists()
    assert _daily_records(), "kpi-daily entry must be recorded in the install manifest"
    assert any("launchctl" in c for c in fr.cmds)


def test_headless_install_command_with_flag(home):
    fr = FakeRunner()
    install._run_install_headless(["--headless", "--schedule-daily"], schedule=_sched(home, fr))
    assert _plist(home).exists() and _daily_records()


@pytest.mark.parametrize("flags", [["--no-schedule"], ["--schedule-daily", "--no-schedule"], []])
def test_no_consent_writes_nothing(home, flags):
    fr = FakeRunner()
    install.run_full(interactive=False, flags=flags, schedule=_sched(home, fr))
    install._run_install_headless(flags, schedule=_sched(home, fr))
    assert not _plist(home).exists()
    assert _daily_records() == [] and fr.cmds == []


def test_interactive_without_terminal_writes_nothing(home):
    fr = FakeRunner()
    assert install._daily_schedule_step(interactive=True, flags=[], schedule=_sched(home, fr), stdin_tty=False) is False
    assert not _plist(home).exists() and fr.cmds == []


def test_interactive_prompt_states_what_is_written_and_default_yes(home, capsys):
    fr, asked = FakeRunner(), []
    ok = install._daily_schedule_step(interactive=True, flags=[], schedule=_sched(home, fr), stdin_tty=True,
                                      ask=lambda q: asked.append(q) or "")
    out = capsys.readouterr().out
    assert ok and _plist(home).exists() and _daily_records()
    assert str(_plist(home)) in out and "local ledgers" in out and asked == ["  Install the daily KPI job? [Y/n] "]


def test_interactive_no_answer_writes_nothing(home):
    fr = FakeRunner()
    ok = install._daily_schedule_step(interactive=True, flags=[], schedule=_sched(home, fr), stdin_tty=True,
                                      ask=lambda q: "n")
    assert ok is False and not _plist(home).exists() and fr.cmds == []


def _stub_other_removers(monkeypatch):
    monkeypatch.setattr(install_hooks, "uninstall", lambda: [])
    monkeypatch.setattr(install_hooks, "uninstall_claw_code", lambda: [])
    monkeypatch.setattr(install_hooks, "uninstall_ide_configs", lambda: [])


def test_uninstall_unloads_daily_job_first_and_removes_everything(home, monkeypatch):
    fr = FakeRunner()
    install.run_full(interactive=False, flags=["--schedule-daily"], schedule=_sched(home, fr))
    assert _plist(home).exists()
    fr.cmds.clear()
    _stub_other_removers(monkeypatch)
    order: list[str] = []
    real_apply = install_manifest.apply_uninstall

    def apply_spy():
        order.append("replay:plist_exists=%s" % _plist(home).exists())
        return real_apply()

    monkeypatch.setattr(install_manifest, "apply_uninstall", apply_spy)
    uninstall._run_uninstall([], schedule=_sched(home, fr))
    assert fr.cmds[0] == f"launchctl unload {_plist(home)}", fr.cmds
    assert order == ["replay:plist_exists=False"], "daily job must be gone before the generic replay"
    assert not _plist(home).exists() and _daily_records() == []


def test_uninstall_proxy_default_teardown_unchanged(home, monkeypatch):
    fr = FakeRunner()
    _stub_other_removers(monkeypatch)
    dest, _ = pd.service_target("Darwin", home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("<plist/>")
    pd.write_sentinel(port=8787, steps="s", tiers="t", label=pd.LABEL, system="Darwin")
    real = pdc.uninstall_proxy_default
    monkeypatch.setattr(pdc, "uninstall_proxy_default", lambda: real(home=home, system="Darwin", runner=fr))
    uninstall._run_uninstall([], schedule=_sched(home, fr))
    assert fr.cmds == [f"launchctl unload {dest}"], "only the proxy service is unloaded; no daily job exists"
    assert not dest.exists() and pd.read_sentinel() is None


def test_uninstall_without_daily_job_runs_nothing(home, monkeypatch):
    fr = FakeRunner()
    _stub_other_removers(monkeypatch)
    uninstall._run_uninstall([], schedule=_sched(home, fr))
    assert fr.cmds == []


def test_doctor_row_not_installed_never_zero(home, capsys):
    issues: list[str] = []
    doctor._daily_schedule_section(issues)
    out = capsys.readouterr().out
    assert "not installed" in out and "last run n/a" in out and "late_days n/a" in out
    assert "late_days 0" not in out and issues == []


def test_doctor_row_installed_with_last_run(home, capsys, monkeypatch):
    fr = FakeRunner()
    ds.install(system="Darwin", home=home, runner=fr)
    base = do.daily_dir()
    base.mkdir(parents=True)
    (base / do.LAST_RUN).write_text(json.dumps({"ts": 1_000_000.0, "exit": 0}))
    monkeypatch.setattr(do, "_now", lambda: 1_000_000.0 + 7200)
    doctor._daily_schedule_section([])
    out = capsys.readouterr().out
    assert "scheduler entry present" in out and "last run 2.0h ago" in out and "late_days n/a" in out
