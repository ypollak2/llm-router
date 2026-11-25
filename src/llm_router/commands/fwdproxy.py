"""`llm-router fwdproxy` -- OFF-by-default, beta: local forward proxy for Claude Code Remote Control.

Remote Control needs ANTHROPIC_BASE_URL to be api.anthropic.com, so routing cannot use the
base-URL redirect. The forward proxy (llm_router.fwdproxy) intercepts CONNECT api.anthropic.com
and relays the decrypted stream to the router proxy. Nothing here runs unless the user types
`llm-router fwdproxy install`; `llm-router install` and `--proxy-default` never touch it.
See docs/fwdproxy.md for the security model.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.llm_router.fwdproxy"
UNIT = "llm-router-fwdproxy.service"
_PKG = Path(__file__).resolve().parent.parent / "fwdproxy"

_HELP = """usage: llm-router fwdproxy <ca|install|uninstall|status>   (beta, opt-in)

  ca [--renew-leaf|--force]  create the name-constrained local CA + leaf for api.anthropic.com
                             (CA cert/leaf in ~/.llm-router/fwd-ca, CA key in ~/.llm-router/fwd-ca-key)
  install                    install ~/.llm-router/bin/claude-rc and start the forward proxy
                             (launchd on macOS, systemd --user on Linux); needs `ca` first
  uninstall                  stop it; rename (never delete) the service file, claude-rc and fwd-ca
  status                     print what is installed and whether the listener answers

Never edits settings.json or the macOS keychain. Run Claude Code through
~/.llm-router/bin/claude-rc to use it. Docs: docs/fwdproxy.md
"""


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path(os.path.expanduser("~"))


def _system(system: str | None) -> str:
    return system or sys.platform


def service_file(home: Path, system: str) -> Path:
    if system == "darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return home / ".config" / "systemd" / "user" / UNIT


def render_service(home: Path, system: str, python: str) -> str:
    log_dir = home / ".llm-router" / "logs"
    if system == "darwin":
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-I</string>
        <string>-m</string>
        <string>llm_router.fwdproxy</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/fwdproxy.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/fwdproxy.err.log</string>
</dict>
</plist>
"""
    return f"""[Unit]
Description=llm-router forward proxy (Remote Control, beta)

[Service]
ExecStart={shlex.quote(python)} -I -m llm_router.fwdproxy
Restart=always
RestartSec=10
StandardOutput=append:{log_dir}/fwdproxy.out.log
StandardError=append:{log_dir}/fwdproxy.err.log

[Install]
WantedBy=default.target
"""


def _run(runner, args: list[str], env: dict | None = None) -> int:
    r = runner(args, env=env, capture_output=True, text=True)
    return r.returncode


def install(*, home: Path | None = None, system: str | None = None, runner=None, python: str | None = None) -> tuple[int, list[str]]:
    home, system, runner = _home(home), _system(system), runner or subprocess.run
    out: list[str] = []
    ca_dir = home / ".llm-router" / "fwd-ca"
    if not (ca_dir / "leaf.crt").is_file() or not (ca_dir / "leaf.key").is_file():
        return 1, [f"no leaf in {ca_dir}: run `llm-router fwdproxy ca` first"]
    if system not in ("darwin", "linux"):
        return 1, [f"unsupported platform {system!r} (macOS and Linux only)"]
    bin_dir = home / ".llm-router" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (home / ".llm-router" / "logs").mkdir(parents=True, exist_ok=True)
    rc = bin_dir / "claude-rc"
    shutil.copyfile(_PKG / "claude-rc", rc)
    rc.chmod(0o755)
    out.append(f"installed launcher {rc}")
    dest = service_file(home, system)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_service(home, system, python or sys.executable))
    out.append(f"wrote {dest}")
    if system == "darwin":
        uid = str(os.getuid())
        _run(runner, ["launchctl", "bootout", f"gui/{uid}/{LABEL}"])  # reload an edited plist; ok if absent
        code = _run(runner, ["launchctl", "bootstrap", f"gui/{uid}", str(dest)])
    else:
        _run(runner, ["systemctl", "--user", "daemon-reload"])
        code = _run(runner, ["systemctl", "--user", "enable", "--now", UNIT])
    if code != 0:
        out.append(f"service start failed (rc={code}); the files are in place, nothing else was changed")
        return 1, out
    out.append("forward proxy started on 127.0.0.1:8789; run Claude Code via ~/.llm-router/bin/claude-rc")
    return 0, out


def uninstall(*, home: Path | None = None, system: str | None = None, runner=None) -> tuple[int, list[str]]:
    home, system, runner = _home(home), _system(system), runner or subprocess.run
    out: list[str] = []
    if system == "linux":
        dest = service_file(home, system)
        if dest.exists():
            _run(runner, ["systemctl", "--user", "disable", "--now", UNIT])
            moved = dest.with_name(dest.name + ".discard")
            dest.rename(moved)
            out.append(f"renamed {dest} -> {moved}")
    env = dict(os.environ, LLM_ROUTER_WRAPPER_HOME=str(home))
    r = runner(["/bin/sh", str(_PKG / "rollback.sh")], env=env, capture_output=True, text=True)
    out.extend(r.stdout.splitlines())
    return (0 if r.returncode == 0 else 1), out


def status(*, home: Path | None = None, system: str | None = None) -> list[str]:
    home, system = _home(home), _system(system)
    ca_dir = home / ".llm-router" / "fwd-ca"
    rows = [
        ("CA + leaf", (ca_dir / "ca.crt").is_file() and (ca_dir / "leaf.crt").is_file()),
        ("launcher claude-rc", (home / ".llm-router" / "bin" / "claude-rc").is_file()),
        ("service file", service_file(home, system).is_file()),
    ]
    return [f"{'yes' if ok else 'no ':3} {name}" for name, ok in rows]


def cmd_fwdproxy(args: list[str]) -> int:
    if not args or any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0 if args else 2
    sub, rest = args[0], args[1:]
    if sub == "ca":
        return subprocess.run(["/bin/sh", str(_PKG / "make-ca.sh"), *rest]).returncode
    if sub in ("install", "uninstall"):
        if rest:
            print(f"llm-router fwdproxy {sub}: takes no arguments", file=sys.stderr)
            return 2
        code, lines = (install if sub == "install" else uninstall)()
        for ln in lines:
            print(ln)
        return code
    if sub == "status":
        print("\n".join(status()))
        return 0
    print(f"llm-router fwdproxy: unknown subcommand {sub!r}", file=sys.stderr)
    print(_HELP, file=sys.stderr)
    return 2
