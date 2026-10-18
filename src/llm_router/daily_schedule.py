# SPDX-License-Identifier: MIT
"""P33-2: the opt-in OS scheduler entry that runs ``kpi --daily --backfill 7`` once a day.

Pure renderers (a launchd plist, a systemd user service + timer, a Windows Task Scheduler XML) plus an
injectable install / uninstall / status, in the shape of ``proxy_default.py``: ``home``, ``system`` and
``runner`` are parameters, so a test installs under a temp home with a fake runner and never calls the real
``launchctl`` / ``systemctl`` / ``schtasks``.

* The command is ``[python, -m, llm_router.cli, kpi, --daily, --backfill, 7]``. The environment carries
  ``LLM_ROUTER_SCHEDULED=1`` always (it stamps ``run_by: scheduler`` on the rows) and ``LLM_ROUTER_HOME`` only
  when it was set at install time and is not the default state directory.
* The run time is the local-clock equivalent of 01:15 UTC, computed at install (:func:`local_run_time`). A DST
  change later moves the job by an hour on launchd and Task Scheduler; systemd is told ``01:15:00 UTC``
  outright. ``--backfill 7`` makes a missed or shifted run heal itself.
* Every file written is recorded in the install manifest under a path containing ``kpi-daily``
  (``daily_offload.scheduler_entry`` reads that). Uninstall deactivates the job, removes the files and forgets
  the records.
* Linux without a user systemd manager: nothing is installed, the manual command is printed (no crontab).
* The Linux and Windows entries are rendered and unit-tested but NOT verified live on a real machine.

No prompt text, model name or user path is rendered except the paths passed in. Nothing here is wired into
the installer prompt (P33-3).
"""
from __future__ import annotations

import getpass
import os
import platform
import plistlib
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable

from llm_router import install_manifest, paths, proxy_default

LABEL = "com.llm_router.kpi-daily"
UNIT = "llm_router-kpi-daily"
TASK_NAME = "llm_router-kpi-daily"
UTC_HM = (1, 15)
BACKFILL = 7
_XMLNS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def daily_command(python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "llm_router.cli", "kpi", "--daily", "--backfill", str(BACKFILL)]


def scheduled_env(home_override: str | None = None) -> dict[str, str]:
    """``LLM_ROUTER_SCHEDULED=1`` always; ``LLM_ROUTER_HOME`` only when given (see :func:`install_home_override`)."""
    env = {"LLM_ROUTER_SCHEDULED": "1"}
    if home_override:
        env["LLM_ROUTER_HOME"] = home_override
    return env


def install_home_override(home: Path) -> str | None:
    """The ``LLM_ROUTER_HOME`` to bake into the job: set now AND not the default ``<home>/.llm-router``."""
    raw = os.environ.get(paths.ENV_VAR, "").strip()
    if not raw:
        return None
    return None if Path(raw).expanduser() == home / ".llm-router" else str(Path(raw).expanduser())


def local_run_time(now: float | None = None, tz: tzinfo | None = None) -> tuple[int, int]:
    """(hour, minute) on the local clock of 01:15 UTC on the date of ``now`` (``tz`` None = system zone)."""
    n = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    t = n.replace(hour=UTC_HM[0], minute=UTC_HM[1], second=0, microsecond=0)
    loc = t.astimezone(tz)
    return loc.hour, loc.minute


# ---------------------------------------------------------------- renderers (pure)

def render_plist(python: str, log_dir: Path, hour: int, minute: int, env: dict[str, str],
                 label: str = LABEL) -> str:
    d = {
        "Label": label,
        "ProgramArguments": daily_command(python),
        "EnvironmentVariables": dict(env),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(log_dir / "kpi-daily.out.log"),
        "StandardErrorPath": str(log_dir / "kpi-daily.err.log"),
    }
    return plistlib.dumps(d, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def _sd_quote(arg: str) -> str:
    arg = arg.replace("%", "%%")
    if not arg or any(c in arg for c in ' \t"\'\\'):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def render_systemd_service(python: str, env: dict[str, str]) -> str:
    environment = " ".join(_sd_quote(f"{k}={v}") for k, v in env.items())
    return f"""[Unit]
Description=LLM Router daily offload row (kpi --daily)

[Service]
Type=oneshot
ExecStart={" ".join(_sd_quote(a) for a in daily_command(python))}
Environment={environment}
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=20min
"""


def render_systemd_timer() -> str:
    return f"""[Unit]
Description=LLM Router daily offload row, once a day

[Timer]
OnCalendar=*-*-* {UTC_HM[0]:02d}:{UTC_HM[1]:02d}:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""


def render_task_xml(python: str, env: dict[str, str], hour: int, minute: int, user: str,
                    pythonw: str | None = None, start_date: str = "2026-01-01") -> str:
    """Task Scheduler definition for ``schtasks /Create /XML``. The environment is set through ``cmd.exe``
    because a task action has no environment of its own; ``start /b /wait`` keeps the windowless
    ``pythonw.exe`` (when it sits beside ``python``) a child cmd waits for."""
    sets = "".join(f'set "{k}={v}"&& ' for k, v in env.items())
    argv = daily_command(pythonw or python)
    exe = subprocess.list2cmdline(argv[:1])
    rest = subprocess.list2cmdline(argv[1:])
    inner = f'{sets}start "" /b /wait {exe} {rest}' if pythonw else f"{sets}{exe} {rest}"
    ET.register_namespace("", _XMLNS)
    q = lambda tag: f"{{{_XMLNS}}}{tag}"  # noqa: E731
    root = ET.Element(q("Task"), {"version": "1.2"})

    def sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
        e = ET.SubElement(parent, q(tag))
        if text is not None:
            e.text = text
        return e

    reg = sub(root, "RegistrationInfo")
    sub(reg, "Description", "LLM Router daily offload row (kpi --daily --backfill 7)")
    trig = sub(sub(root, "Triggers"), "CalendarTrigger")
    sub(trig, "StartBoundary", f"{start_date}T{hour:02d}:{minute:02d}:00")
    sub(trig, "Enabled", "true")
    sub(sub(trig, "ScheduleByDay"), "DaysInterval", "1")
    pr = sub(sub(root, "Principals"), "Principal", None)
    pr.set("id", "Author")
    sub(pr, "UserId", user)
    sub(pr, "LogonType", "InteractiveToken")
    sub(pr, "RunLevel", "LeastPrivilege")
    st = sub(root, "Settings")
    for tag, val in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                     ("StopIfGoingOnBatteries", "false"), ("AllowHardTerminate", "true"),
                     ("StartWhenAvailable", "true"), ("RunOnlyIfNetworkAvailable", "false"),
                     ("AllowStartOnDemand", "true"), ("Enabled", "true"), ("Hidden", "false"),
                     ("ExecutionTimeLimit", "PT30M"), ("Priority", "7")):
        sub(st, tag, val)
    act = sub(sub(root, "Actions"), "Exec")
    sub(act, "Command", "cmd.exe")
    sub(act, "Arguments", f'/d /s /c "{inner}"')
    root.find(q("Actions")).set("Context", "Author")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + body + "\n"


# ---------------------------------------------------------------- plan, install, uninstall, status

def manual_command(python: str, env: dict[str, str]) -> str:
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"{prefix} {shlex.join(daily_command(python))}"


def build_plan(*, system: str, home: Path, state_dir: Path, python: str, now: float | None = None,
               tz: tzinfo | None = None, home_override: str | None = None, user: str | None = None,
               pythonw: str | None = None) -> dict[str, Any]:
    """What an install would write and run. ``files`` is ``[(path, text)]``; ``activate`` / ``deactivate`` are
    shell command strings (None = nothing to run); ``manual`` is set when the platform has no usable entry."""
    hour, minute = local_run_time(now, tz)
    env = scheduled_env(home_override)
    plan: dict[str, Any] = {"system": system, "run_time_local": f"{hour:02d}:{minute:02d}",
                            "run_time_utc": f"{UTC_HM[0]:02d}:{UTC_HM[1]:02d}", "command": daily_command(python),
                            "env": env, "files": [], "activate": None, "deactivate": None, "manual": None}
    if system == "Darwin":
        dest = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        plan["files"] = [(dest, render_plist(python, state_dir / "logs", hour, minute, env))]
        plan["activate"] = proxy_default.launchd_activate_command(dest, LABEL)
        plan["deactivate"] = f"launchctl unload {shlex.quote(str(dest))}"
    elif system == "Linux":
        d = home / ".config" / "systemd" / "user"
        plan["files"] = [(d / f"{UNIT}.service", render_systemd_service(python, env)),
                         (d / f"{UNIT}.timer", render_systemd_timer())]
        plan["activate"] = f"systemctl --user daemon-reload && systemctl --user enable --now {UNIT}.timer"
        plan["deactivate"] = f"systemctl --user disable --now {UNIT}.timer"
    elif system == "Windows":
        dest = state_dir / "schedule" / f"{UNIT}.xml"
        plan["files"] = [(dest, render_task_xml(python, env, hour, minute, user or getpass.getuser(), pythonw))]
        plan["activate"] = f'schtasks /Create /TN {TASK_NAME} /XML "{dest}" /F'
        plan["deactivate"] = f"schtasks /Delete /TN {TASK_NAME} /F"
    else:
        plan["manual"] = manual_command(python, env)
    return plan


def _run(runner: Callable, cmd: str) -> tuple[bool, str]:
    return proxy_default.activate_service(Path("."), cmd, runner=runner)


def _user_systemd_ok(runner: Callable) -> bool:
    return _run(runner, "systemctl --user show-environment")[0]


def install(*, system: str | None = None, home: Path | None = None, state_dir: Path | None = None,
            python: str | None = None, runner: Callable = subprocess.run, now: float | None = None,
            tz: tzinfo | None = None, user: str | None = None, home_override: str | None = ...) -> dict[str, Any]:  # type: ignore[assignment]
    """Write the files, record them in the install manifest, activate. Returns the plan plus ``status``:
    ``installed``, ``activation_failed`` (files written and recorded; ``detail`` says why), ``no_user_systemd``
    or ``unsupported`` (nothing written; ``manual`` holds the command)."""
    system = system or platform.system()
    home = home or Path.home()
    state_dir = state_dir or paths.llm_router_home()
    python = python or sys.executable
    if home_override is ...:
        home_override = install_home_override(home)
    pythonw = None
    if system == "Windows":
        cand = Path(python).with_name("pythonw.exe")
        pythonw = str(cand) if cand.exists() else None
    plan = build_plan(system=system, home=home, state_dir=state_dir, python=python, now=now, tz=tz,
                      home_override=home_override, user=user, pythonw=pythonw)
    if plan["manual"] is not None:
        return {**plan, "status": "unsupported", "detail": f"no automatic entry on {system!r}"}
    if system == "Linux" and not _user_systemd_ok(runner):
        return {**plan, "files": [], "activate": None, "deactivate": None,
                "manual": manual_command(python, plan["env"]), "status": "no_user_systemd",
                "detail": "no user systemd manager; nothing installed"}
    if system == "Darwin":
        (state_dir / "logs").mkdir(parents=True, exist_ok=True)
    for dest, text in plan["files"]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if system == "Windows":
            dest.write_bytes(text.encode("utf-16"))
        else:
            dest.write_text(text, encoding="utf-8")
        install_manifest.record("created_file", dest)
    ok, detail = _run(runner, plan["activate"])
    return {**plan, "status": "installed" if ok else "activation_failed", "detail": detail}


def uninstall(*, system: str | None = None, home: Path | None = None, state_dir: Path | None = None,
              runner: Callable = subprocess.run) -> dict[str, Any]:
    """Deactivate, delete the files, drop their manifest records. ``status``: ``removed`` or ``not_installed``."""
    system = system or platform.system()
    home = home or Path.home()
    state_dir = state_dir or paths.llm_router_home()
    plan = build_plan(system=system, home=home, state_dir=state_dir, python=sys.executable)
    present = [d for d, _ in plan["files"] if d.exists()]
    keys = {k for d, _ in plan["files"] for k in (str(d.absolute()), str(d.resolve()))}
    recorded = any(isinstance(r, dict) and r.get("path") in keys for r in install_manifest.records())
    if plan["deactivate"] is None or not (present or recorded):
        return {"system": system, "status": "not_installed", "removed": [], "detail": ""}
    ok, detail = _run(runner, plan["deactivate"])
    removed = []
    for d in present:
        d.unlink()
        removed.append(str(d))
    for d, _ in plan["files"]:
        install_manifest.forget(d)
        install_manifest.forget(d.resolve())
    if system == "Linux":
        _run(runner, "systemctl --user daemon-reload")
    return {"system": system, "status": "removed", "removed": removed, "deactivated": ok, "detail": detail}


def status(*, system: str | None = None, home: Path | None = None, state_dir: Path | None = None) -> dict[str, Any]:
    """Which entry files exist and whether the manifest records them. The OS scheduler is never queried."""
    system = system or platform.system()
    plan = build_plan(system=system, home=home or Path.home(), state_dir=state_dir or paths.llm_router_home(),
                      python=sys.executable)
    recs = [r.get("path") for r in install_manifest.records() if isinstance(r, dict)]
    return {"system": system, "files": {str(d): d.exists() for d, _ in plan["files"]},
            "in_manifest": any("kpi-daily" in str(p) for p in recs), "run_time_local": plan["run_time_local"]}


# ---------------------------------------------------------------- CLI handlers

def render_default_plist() -> str:
    """``kpi --daily-render-plist``: the plist :func:`install` would write on this machine, now."""
    home, sd = Path.home(), paths.llm_router_home()
    h, m = local_run_time(time.time())
    return render_plist(sys.executable, sd / "logs", h, m, scheduled_env(install_home_override(home)))


def run_install(*, yes: bool, runner: Callable = subprocess.run, system: str | None = None,
                home: Path | None = None, stdin_tty: bool | None = None) -> int:
    """``kpi --daily-install [--yes]``. Shows what will be written and run, asks unless ``--yes``."""
    system = system or platform.system()
    home = home or Path.home()
    plan = build_plan(system=system, home=home, state_dir=paths.llm_router_home(), python=sys.executable,
                      now=time.time(), home_override=install_home_override(home))
    print(f"daily scheduler entry ({system}): runs {shlex.join(plan['command'])}")
    print(f"at {plan['run_time_local']} local ({plan['run_time_utc']} UTC), env {plan['env']}")
    for dest, _ in plan["files"]:
        print(f"writes {dest}")
    if plan["manual"] is None and plan["activate"]:
        print(f"activates with: {plan['activate']}")
    if not yes:
        tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
        if not tty:
            print("not a terminal: re-run with --yes to install", file=sys.stderr)
            return 2
        if input("Install? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not installed")
            return 1
    r = install(system=system, home=home, runner=runner)
    if r["manual"]:
        print(f"{r['detail']}. Run it from your own scheduler:\n  {r['manual']}")
        return 0 if r["status"] == "no_user_systemd" else 2
    print(f"{r['status']}" + (f": {r['detail']}" if r["detail"] else ""))
    return 0 if r["status"] == "installed" else 1


def run_uninstall(*, runner: Callable = subprocess.run, system: str | None = None, home: Path | None = None) -> int:
    r = uninstall(system=system, home=home, runner=runner)
    print(r["status"] + "".join(f"\nremoved {p}" for p in r["removed"]))
    return 0
