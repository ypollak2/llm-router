"""P33-2: the daily scheduler entry. Renderers are parsed with the real parsers (plistlib, configparser,
ElementTree); install/uninstall run under a tmp home with a fake runner, so no test touches launchctl,
systemctl or schtasks. The Linux and Windows entries are NOT verified on a real machine."""
from __future__ import annotations

import configparser
import json
import shlex
import shutil
import subprocess
import types
import plistlib
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_router import daily_offload as do
from llm_router import daily_schedule as ds
from llm_router import failopen, file_lock, install_manifest
from llm_router.commands import kpi

PY = "/opt/venv/bin/python3"
ARGV = [PY, "-m", "llm_router.cli", "kpi", "--daily", "--backfill", "7"]
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp()
LAST = date(2026, 10, 19)
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
FORBIDDEN = ("/users/", "claude", "codex", "haiku", "sonnet", "opus", "gpt", "gemini", "qwen", "ollama")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_SCHEDULED", raising=False)
    monkeypatch.setattr(do, "_now", lambda: NOW)
    return h


class FakeRunner:
    def __init__(self, rc_for=None):
        self.cmds: list[str] = []
        self.rc_for = rc_for or (lambda cmd: 0)

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc_for(cmd), stdout="", stderr="")


def _tz(h, m=0):
    return timezone(timedelta(hours=h, minutes=m))


def _renders(env=None):
    env = env or ds.scheduled_env()
    return {
        "plist": ds.render_plist(PY, Path("/var/state/logs"), 3, 15, env),
        "service": ds.render_systemd_service(PY, env),
        "timer": ds.render_systemd_timer(),
        "xml": ds.render_task_xml(PY, env, 3, 15, "alice", pythonw="C:\\py\\pythonw.exe"),
    }


# ------------------------------------------------------------------ renderers

def test_command_argv_exact():
    assert ds.daily_command(PY) == ARGV


def test_plist_parses_with_the_required_keys():
    d = plistlib.loads(_renders()["plist"].encode())
    assert d["Label"] == "com.llm_router.kpi-daily"
    assert d["ProgramArguments"] == ARGV
    assert d["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert d["RunAtLoad"] is False and d["ProcessType"] == "Background"
    assert d["LowPriorityIO"] is True and d["Nice"] == 10
    assert d["EnvironmentVariables"] == {"LLM_ROUTER_SCHEDULED": "1"}
    assert d["StandardOutPath"] == "/var/state/logs/kpi-daily.out.log"


def test_home_in_env_only_when_given():
    assert ds.scheduled_env() == {"LLM_ROUTER_SCHEDULED": "1"}
    assert ds.scheduled_env("/x y/state") == {"LLM_ROUTER_SCHEDULED": "1", "LLM_ROUTER_HOME": "/x y/state"}
    d = plistlib.loads(_renders(ds.scheduled_env("/s"))["plist"].encode())
    assert d["EnvironmentVariables"]["LLM_ROUTER_HOME"] == "/s"


def test_systemd_units_parse():
    r = _renders(ds.scheduled_env("/x y/state"))
    svc = configparser.ConfigParser(interpolation=None, strict=True)
    svc.optionxform = str
    svc.read_string(r["service"])
    s = svc["Service"]
    assert s["Type"] == "oneshot" and s["Nice"] == "10"
    assert s["IOSchedulingClass"] == "idle" and s["TimeoutStartSec"] == "20min"
    assert shlex.split(s["ExecStart"]) == ARGV
    assert shlex.split(s["Environment"]) == ["LLM_ROUTER_SCHEDULED=1", "LLM_ROUTER_HOME=/x y/state"]
    tim = configparser.ConfigParser(interpolation=None, strict=True)
    tim.optionxform = str
    tim.read_string(r["timer"])
    t = tim["Timer"]
    assert t["OnCalendar"] == "*-*-* 01:15:00 UTC"
    assert t["Persistent"] == "true" and t["RandomizedDelaySec"] == "300"
    assert tim["Install"]["WantedBy"] == "timers.target"


def test_task_xml_structure():
    text = _renders()["xml"]
    root = ET.fromstring(text.encode("utf-16"))          # what the installer writes: UTF-16 with BOM
    assert root.tag == NS + "Task"
    trig = root.find(f"{NS}Triggers/{NS}CalendarTrigger")
    assert trig.find(NS + "StartBoundary").text.endswith("T03:15:00")
    assert trig.find(f"{NS}ScheduleByDay/{NS}DaysInterval").text == "1"
    st = root.find(NS + "Settings")
    want = {"StartWhenAvailable": "true", "DisallowStartIfOnBatteries": "false", "ExecutionTimeLimit": "PT30M",
            "Priority": "7"}
    assert {k: st.find(NS + k).text for k in want} == want
    assert root.find(f"{NS}Principals/{NS}Principal/{NS}UserId").text == "alice"
    ex = root.find(f"{NS}Actions/{NS}Exec")
    assert ex.find(NS + "Command").text == "cmd.exe"
    args = ex.find(NS + "Arguments").text
    assert "pythonw.exe" in args and "-m llm_router.cli kpi --daily --backfill 7" in args
    assert 'set "LLM_ROUTER_SCHEDULED=1"' in args


def test_task_xml_without_pythonw_uses_python():
    x = ds.render_task_xml(PY, ds.scheduled_env(), 3, 15, "alice")
    assert "start " not in x and PY in x


def test_renders_carry_no_user_path_model_or_agent_name():
    for name, text in _renders(ds.scheduled_env("/srv/state")).items():
        low = text.lower()
        for bad in FORBIDDEN:
            assert bad not in low, (name, bad)


@pytest.mark.parametrize("tz,want", [(_tz(-8), (17, 15)), (_tz(5, 30), (6, 45)), (_tz(13), (14, 15)),
                                     (timezone.utc, (1, 15))])
def test_local_run_time_fixed_offsets(tz, want):
    assert ds.local_run_time(NOW, tz) == want


def test_local_run_time_across_dst():
    zi = pytest.importorskip("zoneinfo")
    try:
        ny = zi.ZoneInfo("America/New_York")
    except zi.ZoneInfoNotFoundError:
        pytest.skip("no tz database")
    winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).timestamp()
    assert ds.local_run_time(winter, ny) == (20, 15)
    assert ds.local_run_time(summer, ny) == (21, 15)


def test_home_override_only_when_set_and_not_default(home, monkeypatch):
    assert ds.install_home_override(home) is None                      # fixture sets the default location
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / "elsewhere"))
    assert ds.install_home_override(home) == str(home / "elsewhere")
    monkeypatch.delenv("LLM_ROUTER_HOME")
    assert ds.install_home_override(home) is None


# ------------------------------------------------------------------ install / uninstall (fake runner)

def test_install_uninstall_darwin(home):
    r = FakeRunner()
    out = ds.install(system="Darwin", home=home, python=PY, runner=r, now=NOW, tz=_tz(-8))
    plist = home / "Library" / "LaunchAgents" / "com.llm_router.kpi-daily.plist"
    assert out["status"] == "installed" and plist.is_file()
    d = plistlib.loads(plist.read_bytes())
    assert d["StartCalendarInterval"] == {"Hour": 17, "Minute": 15}
    assert d["ProgramArguments"] == ARGV
    assert (home / ".llm-router" / "logs").is_dir()
    assert len(r.cmds) == 1 and "launchctl kickstart -k gui/$(id -u)/com.llm_router.kpi-daily" in r.cmds[0]
    assert "launchctl load" in r.cmds[0]
    assert do.scheduler_entry() is True
    assert ds.status(system="Darwin", home=home)["in_manifest"] is True
    u = ds.uninstall(system="Darwin", home=home, runner=r)
    assert u["status"] == "removed" and not plist.exists()
    assert r.cmds[1].startswith("launchctl unload ") and "kpi-daily.plist" in r.cmds[1]
    assert do.scheduler_entry() is False
    assert ds.uninstall(system="Darwin", home=home, runner=r)["status"] == "not_installed"


def test_install_uninstall_linux(home):
    r = FakeRunner()
    out = ds.install(system="Linux", home=home, python=PY, runner=r, now=NOW, tz=_tz(0))
    d = home / ".config" / "systemd" / "user"
    assert out["status"] == "installed"
    assert (d / "llm_router-kpi-daily.service").is_file() and (d / "llm_router-kpi-daily.timer").is_file()
    assert r.cmds[0] == "systemctl --user show-environment"
    assert r.cmds[1] == "systemctl --user daemon-reload && systemctl --user enable --now llm_router-kpi-daily.timer"
    assert sum("kpi-daily" in str(x.get("path")) for x in install_manifest.records()) == 2
    assert do.scheduler_entry() is True
    ds.uninstall(system="Linux", home=home, runner=r)
    assert "systemctl --user disable --now llm_router-kpi-daily.timer" in r.cmds
    assert not list(d.glob("*kpi-daily*")) and do.scheduler_entry() is False


def test_linux_without_user_systemd_installs_nothing(home):
    r = FakeRunner(rc_for=lambda cmd: 1)
    out = ds.install(system="Linux", home=home, python=PY, runner=r, now=NOW, tz=_tz(0))
    assert out["status"] == "no_user_systemd" and out["files"] == []
    assert out["manual"].endswith("kpi --daily --backfill 7") and "LLM_ROUTER_SCHEDULED=1" in out["manual"]
    assert not (home / ".config").exists() and install_manifest.records() == []
    assert r.cmds == ["systemctl --user show-environment"]


def test_install_uninstall_windows(home):
    py = home / "py" / "python.exe"
    py.parent.mkdir()
    py.write_text("")
    (py.parent / "pythonw.exe").write_text("")
    r = FakeRunner()
    out = ds.install(system="Windows", home=home, python=str(py), runner=r, now=NOW, tz=_tz(13), user="alice")
    xml = home / ".llm-router" / "schedule" / "llm_router-kpi-daily.xml"
    assert out["status"] == "installed" and xml.is_file()
    raw = xml.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")                         # UTF-16 BOM
    root = ET.fromstring(raw)
    assert root.find(f"{NS}Triggers/{NS}CalendarTrigger/{NS}StartBoundary").text.endswith("T14:15:00")
    assert "pythonw.exe" in root.find(f"{NS}Actions/{NS}Exec/{NS}Arguments").text
    assert r.cmds == [f'schtasks /Create /TN llm_router-kpi-daily /XML "{xml}" /F']
    assert do.scheduler_entry() is True
    ds.uninstall(system="Windows", home=home, runner=r)
    assert r.cmds[-1] == "schtasks /Delete /TN llm_router-kpi-daily /F" and not xml.exists()
    assert do.scheduler_entry() is False


def test_failed_activation_is_reported_and_files_stay_recorded(home):
    out = ds.install(system="Darwin", home=home, python=PY, runner=FakeRunner(rc_for=lambda c: 5), now=NOW,
                     tz=_tz(0))
    assert out["status"] == "activation_failed" and do.scheduler_entry() is True


def test_unsupported_system_prints_manual_and_writes_nothing(home):
    out = ds.install(system="Plan9", home=home, python=PY, runner=FakeRunner(), now=NOW)
    assert out["status"] == "unsupported" and out["manual"] and install_manifest.records() == []


def test_render_plist_flag_prints_the_installed_text(home, capsys, monkeypatch):
    monkeypatch.setattr(ds, "time", types.SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(ds, "install_home_override", lambda h: None)
    ds.install(system="Darwin", home=home, python=ds.sys.executable, runner=FakeRunner(), now=NOW, home_override=None)
    written = (home / "Library" / "LaunchAgents" / "com.llm_router.kpi-daily.plist").read_text()
    assert kpi.cmd_kpi(["--daily-render-plist"]) == 0
    assert capsys.readouterr().out == written


def test_cli_install_needs_yes_without_a_terminal_and_yes_alone_is_refused(home, capsys):
    assert ds.run_install(yes=False, runner=FakeRunner(), system="Darwin", home=home, stdin_tty=False) == 2
    assert not (home / "Library").exists()
    with pytest.raises(SystemExit):
        kpi.cmd_kpi(["--yes"])
    r = FakeRunner()
    assert ds.run_install(yes=True, runner=r, system="Darwin", home=home) == 0
    assert "installed" in capsys.readouterr().out and len(r.cmds) == 1
    assert ds.run_uninstall(runner=r, system="Darwin", home=home) == 0


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not present")
def test_systemd_analyze_verify(tmp_path):
    svc, tim = tmp_path / "llm_router-kpi-daily.service", tmp_path / "llm_router-kpi-daily.timer"
    svc.write_text(ds.render_systemd_service(ds.sys.executable, ds.scheduled_env()))
    tim.write_text(ds.render_systemd_timer())
    p = subprocess.run(["systemd-analyze", "verify", str(svc), str(tim)], capture_output=True, text=True)
    bad = [ln for ln in (p.stdout + p.stderr).splitlines()
           if any(w in ln for w in ("Unknown key", "Unknown section", "Invalid", "Failed to parse"))]
    assert not bad, bad


# ------------------------------------------------------------------ run_daily: bench guard, lock, bookkeeping

def _run(**kw):
    args = dict(date=LAST.isoformat(), backfill=0, force=False, partial=False, manifest=None,
                inputs=do.Inputs([], [], [], [], [], lambda sid, stamp: None), now=NOW)
    args.update(kw)
    return do.run_daily(**args)


def test_scheduled_run_defers_while_a_bench_marker_exists(home, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_SCHEDULED", "1")
    do.bench_marker().parent.mkdir(parents=True, exist_ok=True)
    do.bench_marker().write_text("")
    assert _run() == 0
    assert "deferred_busy" in capsys.readouterr().out
    assert json.loads((do.daily_dir() / do.LAST_RUN).read_text())["status"] == "deferred_busy"
    assert do.day_file(LAST.isoformat()) is None                         # nothing computed or written
    do.bench_marker().unlink()
    assert _run() == 0 and do.day_file(LAST.isoformat()) is not None     # marker gone: runs


def test_manual_run_ignores_the_bench_marker(home, capsys):
    do.bench_marker().parent.mkdir(parents=True, exist_ok=True)
    do.bench_marker().write_text("")
    assert _run() == 0
    assert "deferred_busy" not in capsys.readouterr().out and do.day_file(LAST.isoformat()) is not None


def test_second_run_while_locked_exits_zero_busy(home, capsys):
    with file_lock.exclusive_lock(do.daily_dir() / ".lock", timeout=0) as got:
        assert got is True
        assert _run() == 0
    assert "busy" in capsys.readouterr().out
    assert not (do.daily_dir() / do.LAST_RUN).exists() and do.day_file(LAST.isoformat()) is None
    assert _run() == 0 and do.day_file(LAST.isoformat()) is not None     # released: runs


@pytest.mark.parametrize("exc", [OSError("disk full"), RuntimeError("boom")])
def test_last_run_write_failure_never_changes_the_exit_code(home, monkeypatch, capsys, exc):
    real = do._atomic_write

    def flaky(path, row):
        if path.name == do.LAST_RUN:
            raise exc
        return real(path, row)

    seen = []
    monkeypatch.setattr(do, "_atomic_write", flaky)
    monkeypatch.setattr(failopen, "record", lambda code, e=None, **kw: seen.append((code, type(e).__name__)))
    assert _run() == 0
    assert "could not write .last_run.json" in capsys.readouterr().err
    assert seen == [("CHZ-FO-DAILY-LAST-RUN", type(exc).__name__)]
    assert do.day_file(LAST.isoformat()) is not None                     # the row itself was written


def test_last_run_records_a_refused_run(home):
    assert _run(date="2026-10-20") == 2                                  # open day: refused
    assert json.loads((do.daily_dir() / do.LAST_RUN).read_text())["exit"] == 2


def test_wilson_literal_20_of_40():
    lo, hi = do.wilson_ci(20, 40)
    assert (round(lo, 3), round(hi, 3)) == (0.352, 0.648)


def test_provenance_counts_only_rows_carrying_the_keys(home):
    base = do.daily_dir()
    base.mkdir(parents=True)
    for i, extra in enumerate(({}, {"run_by": "scheduler", "lateness_h": 3.0})):      # pre- and post-P33-1 row
        d = LAST - timedelta(days=i)
        (base / f"{d}.json").write_text(json.dumps({"schema": do.SCHEMA, "date": str(d), **extra}))
    assert do.provenance_counts(LAST, base=base) == {"scheduled_days": 1, "late_days": 0}
