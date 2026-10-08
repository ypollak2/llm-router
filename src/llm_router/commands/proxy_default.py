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
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

from llm_router import install_manifest, paths, proxy_default as pd


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


def _wire_settings_env(port: int, *, base_url_only: bool = False) -> tuple[str | None, Path | None]:
    """Set ``env.ANTHROPIC_BASE_URL`` / ``env.ENABLE_TOOL_SEARCH`` in
    ``~/.claude/settings.json``. Backs up first, records the WHOLE previous
    ``env`` value in the install manifest (never a blind key delete — other
    tools, or the user, may already keep entries there) so uninstall restores
    it exactly. Returns ``(error, backup_path)``: error is ``None`` on success,
    backup_path is ``None`` when there was no file to back up.

    ``base_url_only`` is the repair path (``doctor --fix-routing``): it sets
    only ``ANTHROPIC_BASE_URL`` (one-key diff) and refuses to write an existing
    file whose backup could not be taken."""
    from llm_router.install_hooks import _backup_before_overwrite, _load_settings, _save_settings, settings_path

    path = settings_path()
    existed = path.exists()
    backup = _backup_before_overwrite(path)  # no-op (returns None) when path doesn't exist yet
    if base_url_only and existed and backup is None:
        return f"could not back up {path}; nothing written", None
    data = _load_settings()
    if install_manifest.find("json_key", path, key="env") is None:
        had_key = "env" in data
        previous = copy.deepcopy(data.get("env")) if had_key else None
        install_manifest.record("json_key", path, key="env", had_key=had_key, previous=previous)
    env = data.setdefault("env", {})
    env[ENV_ANTHROPIC_BASE_URL] = f"http://127.0.0.1:{port}"
    if not base_url_only:
        env[ENV_TOOL_SEARCH] = "true"
    try:
        _save_settings(data)
    except OSError as exc:
        return str(exc), backup
    return None, backup


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


def _stale_plist_note(dest: Path, before: str | None, label: str) -> str | None:
    """`kickstart -k` does not re-read an edited plist: say so, never unload."""
    if before is None or not dest.exists() or dest.read_text() == before:
        return None
    return (
        f"NOTE {label}: the plist changed but launchd keeps the old one loaded, so this "
        f"restart does not apply it. Owner step (docs/proxy.md, 'Moving an existing install "
        f"behind the shim'): `launchctl unload {shlex.quote(str(dest))}` "
        f"then `launchctl load {shlex.quote(str(dest))}`."
    )


def _read_or_none(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


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

    prior = pd.read_sentinel() or {}
    own_shim_layout = (
        shim and prior.get("shim_label") == pd.SHIM_LABEL
        and prior.get("port") == port and prior.get("upstream_port") == main_port
    )
    if own_shim_layout and pd.proxy_health("127.0.0.1", port, timeout=1.0) \
            and pd.proxy_health("127.0.0.1", main_port, timeout=1.0):
        # A re-run on a finished install: nothing to write or restart, and the
        # sentinel must keep naming the shim so uninstall still removes it.
        actions.append(
            f"Already installed: shim on :{port} in front of the main proxy on :{main_port} — "
            f"nothing written or restarted."
        )
        reused = True
    elif not own_shim_layout and pd.proxy_health("127.0.0.1", port, timeout=1.0):
        actions.append(
            f"Found a proxy already answering on 127.0.0.1:{port} — reusing it "
            f"(no second service installed)."
        )
        if shim:
            actions.append(
                "No fail-open shim installed: the port is already taken. If that process is "
                "the main proxy, a dead proxy still breaks new sessions; see docs/proxy.md "
                "'Fail-open shim' to move it behind the shim (not done here: launchd does not "
                "re-read an edited plist on `kickstart`, and bootout + bootstrap is what cut "
                "8787 on 2026-10-08)."
            )
        shim = False
        main_port = port
        reused = True
    else:
        # Own shim layout, main proxy answering, shim down: the main service is fine and
        # `kickstart -k` on its loaded job would restart it and drop in-flight requests.
        # Leave it alone and go straight to the shim.
        main_healthy = own_shim_layout and pd.proxy_health("127.0.0.1", main_port, timeout=1.0)
        if main_healthy:
            actions.append(
                f"Main proxy on :{main_port} is healthy — not restarting it; starting only the shim. Any change to the main proxy's plist is not applied while it keeps running — owner step (docs/proxy.md): unload then load the main job."
            )
        else:
            try:
                _d, _ = pd.service_target(system, home)
                before = _read_or_none(_d)
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
            note = _stale_plist_note(dest, before, pd.LABEL)
            if note:
                actions.append(note)
            err = _start_and_wait(dest, activate_cmd, main_port, "proxy", log="proxy.err.log", **wait)
            if err is not None:
                return {"ok": False, "actions": actions, "reused": False, "error": err}
            actions.append(f"Started via `{activate_cmd}`")
        if shim:
            sbefore = _read_or_none(pd.service_target(system, home, label=pd.SHIM_LABEL)[0])
            sdest, sactivate = pd.install_shim_service(
                system=system, home=home, port=port, upstream_port=upstream_port,
            )
            actions.append(f"Wrote {sdest}")
            snote = _stale_plist_note(sdest, sbefore, pd.SHIM_LABEL)
            if snote:
                actions.append(snote)
            err = _start_and_wait(sdest, sactivate, port, "fail-open shim",
                                  log="proxy-shim.err.log", **wait)
            if err is not None:
                return {"ok": False, "actions": actions, "reused": False, "error": err}
            actions.append(
                f"Started the fail-open shim via `{sactivate}` (127.0.0.1:{port} -> main proxy "
                f"on :{upstream_port}, or api.anthropic.com when the main proxy is down)"
            )
        reused = False

    err, _backup = _wire_settings_env(port)
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
    try:
        shim_file, _ = pd.service_target(system, home, label=pd.SHIM_LABEL)
    except RuntimeError:
        shim_file = None
    # A shim plist nobody recorded (hand-written, or left by an earlier run)
    # is removed as well, with or without a sentinel.
    shim_label = (sentinel or {}).get("shim_label") or (
        pd.SHIM_LABEL if shim_file is not None and shim_file.exists() else None)
    if sentinel is None and shim_label is None:
        return actions  # never installed, or already removed — nothing to do

    services = [(pd.LABEL, "proxy service")] if sentinel is not None else []
    if shim_label:
        services.insert(0, (shim_label, "fail-open shim service"))
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

    if sentinel is not None:
        pd.remove_sentinel()
        actions.append("Removed proxy-default state sentinel")
    return actions


# ── `llm-router doctor --fix-routing` (P0.14-c; owner decision D-R8-4 = explicit only) ──
#
# 2026-10-08: settings.json lost env.ANTHROPIC_BASE_URL in an unrecorded rewrite
# while the sentinel still said enabled, and nothing put it back until a hand
# restore three hours later. This is the explicit repair: no hook and no plain
# `install` / `doctor` reaches it (tests/test_doctor_fix_routing.py and
# tests/test_failopen_never_writes_settings.py pin that). It writes the one key
# only when every condition below holds, and otherwise refuses with the reason.

SETTINGS_WRITES_NAME = "settings_writes.jsonl"


def env_block_sha256(env) -> str:
    """sha256 of settings.json's ``env`` block. hooks/session-start.py
    ``_env_block_sha256`` hashes the same bytes (stdlib-only copy, pinned by a
    parity test), so its observe rows and the write rows below compare."""
    blob = json.dumps(env if isinstance(env, dict) else None, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def routes_to_local_port(value: str | None, ports) -> bool:
    """Same rule as hooks/session-start.py ``_routes_to_local_port`` (parity test)."""
    from urllib.parse import urlsplit

    if not value:
        return False
    try:
        parts = urlsplit(value if "//" in value else "//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1") and port in ports


def _project_override(cwd: str) -> tuple[str | None, str]:
    """(value, file) of the first project settings file under ``cwd`` that sets
    ANTHROPIC_BASE_URL, local before shared (Claude Code's precedence, and the two
    project files the SessionStart hook's ``_effective_base_url`` reads). Only
    consulted once the user file is known not to set the key, so cwd == $HOME
    (where the "project" file is the user file) finds nothing there."""
    for name in ("settings.local.json", "settings.json"):
        path = Path(cwd) / ".claude" / name
        try:
            env = json.loads(path.read_text()).get("env")
        except (OSError, ValueError, AttributeError):
            continue
        v = env.get(ENV_ANTHROPIC_BASE_URL) if isinstance(env, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip(), str(path)
    return None, ""


def _append_settings_write(row: dict) -> None:
    p = paths.state_path(SETTINGS_WRITES_NAME)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _result(status: str, reason: str, diff: list[str] | None = None, backup: Path | None = None) -> dict:
    return {"status": status, "reason": reason, "diff": diff or [],
            "backup": str(backup) if backup is not None else None}


def fix_routing(*, yes: bool = False, confirm=None, cwd: str | None = None) -> dict:
    """Write ``env.ANTHROPIC_BASE_URL`` into ``~/.claude/settings.json`` only when
    all hold: the sentinel is enabled and has no ``routing_opt_out``; the key is
    absent there; no project settings file or environment variable overrides it;
    the port settings.json would name AND the main proxy behind the shim answer.

    ``confirm(diff_lines) -> bool`` asks the owner (interactive); without it,
    ``yes`` must be True or nothing is written. Returns ``{"status": "written" |
    "noop" | "refused" | "cancelled", "reason", "diff", "backup"}``."""
    from llm_router.install_hooks import settings_path
    from llm_router.proxy_liveness import _host_of

    sentinel = pd.read_sentinel()
    if sentinel is None:
        return _result("refused", "proxy-default is not installed (no sentinel); "
                                  "`llm-router install --proxy-default` installs it")
    if sentinel.get("enabled") is not True:
        return _result("refused", "the proxy-default sentinel is not enabled")
    if sentinel.get("routing_opt_out"):
        return _result("refused", "routing_opt_out is set: the key was removed on purpose "
                                  "(`--decline`). `llm-router install --proxy-default` opts back in")
    try:
        port = int(sentinel.get("port", pd.DEFAULT_PORT))
        up = sentinel.get("upstream_port")
        main_port = int(up) if up is not None else port
    except (TypeError, ValueError):
        return _result("refused", f"the port in {pd.sentinel_path()} is unreadable")
    ports = [port, main_port]

    path = settings_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return _result("refused", f"{path} does not parse as JSON; not touching it")
    env = data.get("env", {}) if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return _result("refused", f"{path} has no usable `env` object; not touching it")
    current = env.get(ENV_ANTHROPIC_BASE_URL)
    if current is not None and not isinstance(current, str):
        return _result("refused", f"{path} env.ANTHROPIC_BASE_URL is not a string; not touching it")
    if current is not None and current.strip():
        if not routes_to_local_port(current, ports):
            return _result("refused", f"{path} already sets ANTHROPIC_BASE_URL to {_host_of(current)}, "
                                      f"not this proxy (:{port}); not ours to change")
        dead = [p for p in dict.fromkeys(ports) if not pd.proxy_health("127.0.0.1", p, timeout=1.0)]
        if dead:
            return _result("refused", f"the key is present and points at this proxy, but nothing answers "
                                      f"on 127.0.0.1:{', :'.join(map(str, dead))}. A settings write cannot "
                                      f"fix that; `llm-router doctor` shows the restart command")
        return _result("noop", f"{path} already routes through 127.0.0.1:{port} and the proxy answers; "
                               f"nothing written")

    pval, pwhere = _project_override(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if pval is not None:
        return _result("refused", f"project override: {pwhere} sets ANTHROPIC_BASE_URL to {_host_of(pval)} "
                                  f"and takes precedence over {path}; change that file instead")
    ev = (os.environ.get(ENV_ANTHROPIC_BASE_URL) or "").strip()
    if ev and not routes_to_local_port(ev, ports):
        return _result("refused", f"the environment sets ANTHROPIC_BASE_URL to {_host_of(ev)}; "
                                  f"unset it first")
    if not pd.proxy_health("127.0.0.1", port, timeout=1.0):
        return _result("refused", f"nothing answers on 127.0.0.1:{port}; pointing settings.json at it "
                                  f"would fail every call")
    if main_port != port and not pd.proxy_health("127.0.0.1", main_port, timeout=1.0):
        return _result("refused", f"the shim answers on :{port} but the main proxy on :{main_port} does "
                                  f"not, so every call would bypass routing; restart it first")

    new = f"http://127.0.0.1:{port}"
    diff = [f"--- {path}", f"+++ {path}"]
    if current is not None:
        diff.append(f"-  env.{ENV_ANTHROPIC_BASE_URL}: {json.dumps(current)}")
    diff.append(f"+  env.{ENV_ANTHROPIC_BASE_URL}: {json.dumps(new)}")
    if not yes:
        if confirm is None:
            return _result("refused", "not interactive: re-run with --yes to write", diff)
        if not confirm(diff):
            return _result("cancelled", "nothing written", diff)

    err, backup = _wire_settings_env(port, base_url_only=True)
    if err is not None:
        return _result("refused", f"could not update settings.json: {err}", diff, backup)
    try:
        after = json.loads(path.read_text()).get("env") or {}
    except (OSError, ValueError, AttributeError):
        after = {}
    changed = sorted(f"env.{k}" for k in set(env) | set(after) if env.get(k) != after.get(k))
    try:
        _append_settings_write({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "write",
            "writer": "doctor --fix-routing", "path": str(path), "keys_changed": changed,
            "backup": str(backup) if backup is not None else None,
            "env_sha256": env_block_sha256(after),
        })
    except OSError:
        pass  # the settings write itself succeeded; the record is best-effort
    return _result("written", f"wrote env.{ENV_ANTHROPIC_BASE_URL}={new} (keys changed: "
                              f"{', '.join(changed) or 'none'})", diff, backup)


def decline_routing() -> dict:
    """``doctor --fix-routing --decline``: set ``routing_opt_out`` so the key's
    absence is treated as deliberate (no repair, no SessionStart warning)."""
    if not pd.set_routing_opt_out():
        return _result("refused", "proxy-default is not installed (no sentinel); nothing to decline")
    return _result("declined", f"routing_opt_out set in {pd.sentinel_path()}: `--fix-routing` will not "
                               f"write the key and SessionStart will not warn that routing is off. "
                               f"`llm-router install --proxy-default` opts back in")


def cmd_fix_routing(args: list[str]) -> int:
    """``llm-router doctor --fix-routing [--yes] [--decline]``. Exit 0 when written,
    already correct or declined; 1 when refused or cancelled."""
    if "--decline" in args:
        r = decline_routing()
    else:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

        def _ask(diff: list[str]) -> bool:
            for line in diff:
                print(f"  {line}")
            return input("  Write this one key? [y/N] ").strip().lower() in ("y", "yes")

        r = fix_routing(yes="--yes" in args, confirm=_ask if interactive else None)
        if r["status"] in ("written", "refused") and r["diff"]:
            for line in r["diff"]:
                print(f"  {line}")
    ok = r["status"] in ("written", "noop", "declined")
    mark = _green("✓") if ok else _red("✗")
    print(f"  {mark}  fix-routing {r['status']}: {r['reason']}")
    if r["backup"]:
        print(f"     backup: {r['backup']}")
    if r["status"] == "written":
        print("     Takes effect in the next Claude Code session (settings.json is read at startup).")
    return 0 if ok else 1


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
