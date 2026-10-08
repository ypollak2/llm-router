"""``llm-router install --proxy-default[=off]`` — make the per-call proxy the
DEFAULT ``ANTHROPIC_BASE_URL`` for every Claude Code session on this machine.

`docs/proxy.md` describes the opt-in, one-session-at-a-time proxy: nothing
installs it and nothing changes ``settings.json``. This is the opposite risk
profile — every session on the machine depends on it being up — so every step
here is ordered around one rule: **never point ``settings.json`` at a proxy
that has not already proven it answers.**

    1. If a proxy already answers on the target port (the owner's own
       ``com.ypollak2.llm-router-proxy`` LaunchAgent, or a prior install),
       reuse it — never install a second service that would fight it for the
       same port.
    2. Otherwise write and start a supervised service
       (``llm_router.proxy_default``), then poll its health before touching
       anything else.
    3. Only once the proxy is confirmed answering does this module back up
       and edit ``~/.claude/settings.json``, recording the previous
       ``env`` block in the install manifest (``install_manifest``) so
       uninstall restores it exactly rather than just deleting the keys —
       other tools may already keep entries there.
    4. If step 2 never becomes healthy, this refuses: no settings.json write,
       clear error, whatever was written in step 2 stays (so `llm-router
       doctor` can show the operator the log path) but is never activated.

See ``proxy_default.py`` for the service-file rendering and health probe, and
``docs/proxy.md``'s "Proxy-default" section for the fail-safe design this
implements (SessionStart hook check, doctor check, statusline warning).
"""

from __future__ import annotations

import copy
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from llm_router import install_manifest, proxy_default as pd


# ── ANSI helpers (respect NO_COLOR / non-tty) — same small block every
# commands/*.py module in this package carries its own copy of. ────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


ENV_ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_TOOL_SEARCH = "ENABLE_TOOL_SEARCH"


def _wire_settings_env(port: int) -> str | None:
    """Set ``env.ANTHROPIC_BASE_URL`` / ``env.ENABLE_TOOL_SEARCH`` in
    ``~/.claude/settings.json``. Backs up first, records the WHOLE previous
    ``env`` value in the install manifest (never a blind key delete — other
    tools, or the user, may already keep entries there) so uninstall restores
    it exactly. Returns an error string, or ``None`` on success."""
    from llm_router.install_hooks import _backup_before_overwrite, _load_settings, _save_settings, settings_path

    path = settings_path()
    _backup_before_overwrite(path)  # no-op (returns None) when path doesn't exist yet
    data = _load_settings()
    if install_manifest.find("json_key", path, key="env") is None:
        had_key = "env" in data
        previous = copy.deepcopy(data.get("env")) if had_key else None
        install_manifest.record("json_key", path, key="env", had_key=had_key, previous=previous)
    env = data.setdefault("env", {})
    env[ENV_ANTHROPIC_BASE_URL] = f"http://127.0.0.1:{port}"
    env[ENV_TOOL_SEARCH] = "true"
    try:
        _save_settings(data)
    except OSError as exc:
        return str(exc)
    return None


def _start_and_wait(dest: Path, activate_cmd: str, port: int, what: str, *, runner, home: Path,
                    log: str, health_retries: int, health_interval_s: float) -> str | None:
    """Start one written service and poll its port. Returns an error or None."""
    ok, detail = pd.activate_service(dest, activate_cmd, runner=runner)
    if not ok:
        return f"could not start the {what} service (`{activate_cmd}`): {detail or 'unknown error'}"
    for _ in range(max(1, health_retries)):
        if pd.proxy_health("127.0.0.1", port, timeout=1.0):
            return None
        time.sleep(health_interval_s)
    return (
        f"the {what} service was started but did not answer on 127.0.0.1:{port} "
        f"within {health_retries * health_interval_s:.0f}s — refusing to change "
        f"ANTHROPIC_BASE_URL. Check {home}/.llm-router/logs/{log}, then retry."
    )


def install_proxy_default(
    *,
    port: int = pd.DEFAULT_PORT,
    upstream_port: int = pd.DEFAULT_UPSTREAM_PORT,
    shim: bool = True,
    steps: str = pd.DEFAULT_STEPS,
    tiers: str = pd.DEFAULT_TIERS,
    home: Path | None = None,
    system: str | None = None,
    runner=None,
    health_retries: int = 10,
    health_interval_s: float = 1.0,
) -> dict:
    """Returns ``{"ok": bool, "actions": [...], "error": str | None, "reused": bool}``.

    Never writes ``settings.json`` unless the proxy is confirmed answering
    first (either an already-running one, or one this call just started).

    ``port`` is the port settings.json will name. With ``shim`` (the default,
    P0.10 / D-17 = A) the fail-open shim listens there and the main proxy
    listens on ``upstream_port``; both must answer before settings.json is
    touched. ``shim=False`` keeps the pre-P0.10 layout (main proxy on ``port``).
    """
    runner = runner or subprocess.run
    system = system or platform.system()
    home = home or Path.home()
    actions: list[str] = []
    shim = shim and upstream_port != port
    main_port = upstream_port if shim else port
    wait = {"runner": runner, "home": home, "health_retries": health_retries,
            "health_interval_s": health_interval_s}

    if pd.proxy_health("127.0.0.1", port, timeout=1.0):
        actions.append(
            f"Found a proxy already answering on 127.0.0.1:{port} — reusing it "
            f"(no second service installed)."
        )
        if shim:
            actions.append(
                "No fail-open shim installed: the port is already taken. If that process is "
                "the main proxy, a dead proxy still breaks new sessions; see docs/proxy.md "
                "'Fail-open shim' to move it behind the shim."
            )
        shim = False
        main_port = port
        reused = True
    else:
        try:
            dest, activate_cmd = pd.install_service(
                system=system, home=home, port=main_port, steps=steps, tiers=tiers,
            )
        except RuntimeError as exc:
            return {"ok": False, "actions": actions, "error": str(exc), "reused": False}
        # Deliberately NOT recorded in install_manifest as a generic "file":
        # the manifest replay (commands/uninstall.py) removes "file" records
        # by unlinking, with no unload/stop step — for a hook script copy
        # that's correct, but this file backs a LIVE supervised process, and
        # deleting it without unloading first leaves the process orphaned and
        # still running. `uninstall_proxy_default()` (driven by the sentinel
        # this function writes below) is the sole owner of this file's full
        # teardown: stop, THEN delete, in that order. The shim file below
        # follows the same rule.
        actions.append(f"Wrote {dest}")
        err = _start_and_wait(dest, activate_cmd, main_port, "proxy", log="proxy.err.log", **wait)
        if err is not None:
            return {"ok": False, "actions": actions, "reused": False, "error": err}
        actions.append(f"Started via `{activate_cmd}`")
        if shim:
            sdest, sactivate = pd.install_shim_service(
                system=system, home=home, port=port, upstream_port=upstream_port,
            )
            actions.append(f"Wrote {sdest}")
            err = _start_and_wait(sdest, sactivate, port, "fail-open shim",
                                  log="proxy-shim.err.log", **wait)
            if err is not None:
                return {"ok": False, "actions": actions, "reused": False, "error": err}
            actions.append(
                f"Started the fail-open shim via `{sactivate}` (127.0.0.1:{port} -> main proxy "
                f"on :{upstream_port}, or api.anthropic.com when the main proxy is down)"
            )
        reused = False

    err = _wire_settings_env(port)
    if err is not None:
        return {"ok": False, "actions": actions, "reused": reused, "error": f"could not update settings.json: {err}"}
    actions.append(
        f"Set ANTHROPIC_BASE_URL=http://127.0.0.1:{port} and ENABLE_TOOL_SEARCH=true "
        f"in ~/.claude/settings.json"
    )

    pd.write_sentinel(
        port=port, steps=steps, tiers=tiers, label=pd.LABEL, system=system,
        upstream_port=main_port if shim else None, shim_label=pd.SHIM_LABEL if shim else None,
    )
    actions.append(f"Recorded proxy-default state in {pd.sentinel_path()}")
    return {"ok": True, "actions": actions, "error": None, "reused": reused}


def uninstall_proxy_default(*, home: Path | None = None, system: str | None = None, runner=None) -> list[str]:
    """Stops and removes the service this module installed, and the sentinel.

    Does NOT touch ``settings.json`` directly — that restore is handled by
    the install manifest replay already run by ``commands/uninstall.py``
    (``install_manifest.apply_uninstall()``), which puts the whole previous
    ``env`` block back exactly, including keys this module never touched.
    """
    runner = runner or subprocess.run
    system = system or platform.system()
    home = home or Path.home()
    actions: list[str] = []

    sentinel = pd.read_sentinel()
    if sentinel is None:
        return actions  # never installed, or already removed — nothing to do

    services = [(pd.LABEL, "proxy service")]
    if sentinel.get("shim_label"):
        services.insert(0, (sentinel["shim_label"], "fail-open shim service"))
    for label, what in services:
        try:
            dest, _ = pd.service_target(system, home, label=label)
        except (RuntimeError, KeyError):
            continue
        ok, detail = pd.deactivate_service(system, dest, runner=runner, label=label)
        actions.append(f"Stopped {what}{'' if ok else f' (best-effort: {detail})'}")
        if dest.exists():
            try:
                dest.unlink()
                actions.append(f"Removed {dest}")
            except OSError as exc:
                actions.append(f"WARN could not remove {dest}: {exc}")

    pd.remove_sentinel()
    actions.append("Removed proxy-default state sentinel")
    return actions


# ── CLI entry point (dispatched from commands/install.py's `--proxy-default`) ──

def cmd_proxy_default(sub: str) -> int:
    if sub == "off":
        print(f"\n{_bold('Proxy-default: reverting...')}\n")
        actions = uninstall_proxy_default()
        if not actions:
            print(f"  {_yellow('Not installed — nothing to do.')}\n")
            return 0
        for a in actions:
            print(f"  {a}")
        print(
            f"\n{_green('Done.')} `llm-router uninstall` also reverts settings.json's "
            f"ANTHROPIC_BASE_URL/ENABLE_TOOL_SEARCH. Restart Claude Code to pick it up.\n"
        )
        return 0

    print(f"\n{_bold('Proxy-default: installing...')}\n")
    result = install_proxy_default()
    for a in result["actions"]:
        print(f"  {_green('✓')}  {a}")
    if not result["ok"]:
        print(f"\n  {_red('✗')}  {result['error']}")
        print(f"  {_yellow('Nothing in ~/.claude/settings.json was changed.')}\n")
        return 1

    print(f"\n{_green('✓')} {_bold('Every new Claude Code session now routes through the proxy by default.')}")
    print("  Restart Claude Code to pick it up (settings.json is read at startup).")
    print("  `opus:` at the start of a prompt pins that conversation to Opus.")
    print("  A contradiction, a `claude:` re-ask, or repeated tool failures escalate automatically.")
    print("  `llm-router doctor` checks proxy health any time.")
    print("  `llm-router install --proxy-default off` reverts.\n")
    return 0
