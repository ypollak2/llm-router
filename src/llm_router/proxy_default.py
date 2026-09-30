# SPDX-License-Identifier: MIT
"""Make the per-call proxy (`llm_router.proxy`) the DEFAULT `ANTHROPIC_BASE_URL`
for every Claude Code session on this machine, supervised so it survives a
crash and reboot.

`docs/proxy.md` documents the OPT-IN, one-session-at-a-time form: nothing
installs it, nothing changes settings.json, and closing the terminal stops
using it. This module is the opposite risk profile: once installed, EVERY
Claude Code session on the machine points at this proxy, so if the proxy is
down, every session's first API call fails — not just the one that opted in.
That is why this module exists separately from `docs/proxy.md`'s opt-in path
and why almost everything here is about the failure mode, not the happy path:

  * the service is supervised (`KeepAlive`/`Restart=on-failure`) so a crash
    restarts it in place;
  * `install_proxy_default()` (in `commands/proxy_default.py`) verifies the
    proxy actually answers *before* it ever writes `ANTHROPIC_BASE_URL`, and
    refuses (leaving settings.json untouched) if it does not;
  * `proxy_health()` is the one health probe every other surface (doctor,
    the SessionStart hook, the statusline) reads from, so "is the default
    proxy up" is answered the same way everywhere.

Evidence: `~/.rsi/research/llm-router-cursor-parity/trial-real-prompts.md`
(n=6, real prompts, 2026-09-30) found 72-80% lower cost and every conversation
landing on Sonnet, with 1/6 routed answers unacceptable (a moderate
investigation-shaped prompt Sonnet answered shallowly) — see
`proxy/escalation.py` for the mitigations that trial motivated.
"""

from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

from llm_router import paths

LABEL = "com.llm_router.proxy"
DEFAULT_PORT = 8787
DEFAULT_STEPS = "off"
DEFAULT_TIERS = "conversation"
_SENTINEL_NAME = "proxy_default.json"


# ── sentinel: "is proxy-default installed, and with what config" ───────────
#
# A separate file rather than re-deriving from settings.json, because
# settings.json's `env.ANTHROPIC_BASE_URL` alone cannot tell "llm-router set
# this" apart from "the user has a corporate proxy" or pxpipe's own sync
# (hooks/session-start.py `_sync_pxpipe_anthropic_base_url`) — and every
# caller that needs to know "is proxy-default mode on, and which port" (the
# SessionStart hook, doctor, the statusline) would otherwise have to guess.

def sentinel_path() -> Path:
    return paths.state_path(_SENTINEL_NAME)


def read_sentinel() -> dict | None:
    p = sentinel_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_sentinel(*, port: int, steps: str, tiers: str, label: str, system: str) -> None:
    p = sentinel_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {
            "enabled": True,
            "port": port,
            "steps": steps,
            "tiers": tiers,
            "label": label,
            "system": system,
            "installed_at": time.time(),
        },
        indent=2,
    ) + "\n")


def remove_sentinel() -> None:
    try:
        sentinel_path().unlink(missing_ok=True)
    except OSError:
        pass


# ── health ───────────────────────────────────────────────────────────────

def proxy_health(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 1.0) -> bool:
    """Is something listening on `host:port` at all.

    Deliberately a raw TCP connect, not an HTTP GET. The proxy's only route is
    the catch-all `/{path:path}` handler (`proxy/server.py`): anything other
    than a well-formed `POST /v1/messages` is forwarded byte-for-byte to
    `https://api.anthropic.com` (`forward()` never inspects it for non-message
    paths either). An HTTP health probe would therefore make a real network
    call to Anthropic on every doctor run, every statusline render and every
    session start — a needless network dependency (and a needless, if cheap,
    real request) for a question that a TCP connect already answers: is the
    process up and accepting connections.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── service file rendering ──────────────────────────────────────────────────
#
# Same shape as `gateway_service.py`'s launchd/systemd renderers (that module
# is the existing per-user background-service pattern in this codebase), with
# one deliberate difference: `home` is an explicit parameter here rather than
# `Path.home()` read inline, so a test can install and ACTIVATE a real service
# file under a temp HOME without ever touching `~/Library/LaunchAgents`. The
# task this module ships for requires testing the installer end-to-end
# (including the real "proxy is down" failure mode) without risking the
# owner's live `com.ypollak2.llm-router-proxy` LaunchAgent already running on
# :8787 — gateway_service.py's tests only ever call it with `write=False`
# against the real home, which is not enough coverage for a feature whose job
# is exactly "make this safe to install for real."

def render_launchd_plist(
    python: str, home: Path, *, port: int, steps: str, tiers: str, label: str = LABEL,
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>llm_router.cli</string>
        <string>proxy</string>
        <string>--port</string><string>{port}</string>
        <string>--steps</string><string>{steps}</string>
        <string>--tiers</string><string>{tiers}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{home}/.llm-router/logs/proxy.out.log</string>
    <key>StandardErrorPath</key><string>{home}/.llm-router/logs/proxy.err.log</string>
</dict>
</plist>
"""


def render_systemd_user_unit(python: str, *, port: int, steps: str, tiers: str) -> str:
    # No `label` parameter: unlike a launchd plist, a systemd unit carries no
    # label field of its own -- `service_target` below is what maps `LABEL`
    # to the actual unit NAME (llm_router-proxy.service).
    return f"""[Unit]
Description=LLM Router default Claude Code proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} -m llm_router.cli proxy --port {port} --steps {steps} --tiers {tiers}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def service_target(system: str | None = None, home: Path | None = None) -> tuple[Path, str]:
    """(destination file, activation command) for the current platform."""
    system = system or platform.system()
    home = home or Path.home()
    if system == "Darwin":
        dest = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        return dest, f"launchctl load {dest}"
    if system == "Linux":
        dest = home / ".config" / "systemd" / "user" / "llm_router-proxy.service"
        return dest, "systemctl --user daemon-reload && systemctl --user enable --now llm_router-proxy"
    raise RuntimeError(
        f"Automatic proxy-default install is not supported on {system!r}; "
        "run `llm-router proxy` under your own supervisor and set "
        "ANTHROPIC_BASE_URL yourself."
    )


def deactivation_command(system: str, dest: Path) -> str | None:
    if system == "Darwin":
        return f"launchctl unload {dest}" if dest.exists() else None
    if system == "Linux":
        return "systemctl --user disable --now llm_router-proxy"
    return None


def install_service(
    python: str | None = None,
    *,
    system: str | None = None,
    home: Path | None = None,
    port: int = DEFAULT_PORT,
    steps: str = DEFAULT_STEPS,
    tiers: str = DEFAULT_TIERS,
    write: bool = True,
) -> tuple[Path, str]:
    """Render and (by default) write the per-user proxy service file.

    Returns (path, activation_command). Does NOT load/start the service —
    writing the file is reversible; starting it is the caller's explicit next
    step (`activate_service` below), so a refused install never leaves a
    running process behind it didn't mean to start.
    """
    python = python or sys.executable
    system = system or platform.system()
    home = home or Path.home()
    dest, activate = service_target(system, home)
    content = (
        render_launchd_plist(python, home, port=port, steps=steps, tiers=tiers)
        if system == "Darwin"
        else render_systemd_user_unit(python, port=port, steps=steps, tiers=tiers)
    )
    if write:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    return dest, activate


def activate_service(dest: Path, activate_cmd: str, *, runner=subprocess.run) -> tuple[bool, str]:
    """Best-effort load/enable of the just-written service file.

    `runner` is injectable so a test never actually invokes `launchctl`/
    `systemctl` — the real activation command talks to a system service
    manager, and a unit test has no business starting or stopping one.
    """
    try:
        result = runner(activate_cmd, shell=True, capture_output=True, text=True, timeout=15)
        ok = result.returncode == 0
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        return ok, detail
    except Exception as exc:  # noqa: BLE001 — report, never raise into the installer
        return False, str(exc)


def deactivate_service(system: str, dest: Path, *, runner=subprocess.run) -> tuple[bool, str]:
    cmd = deactivation_command(system, dest)
    if cmd is None:
        return True, ""
    try:
        result = runner(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
