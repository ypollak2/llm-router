#!/usr/bin/env python3
# llm_router-hook-version: 4
"""Claude Code plugin SessionStart hook (P2.12, P212-3): keep the wired hosts on the plugin's version.

The plugin bundle (``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json``) can move ahead of what
``llm-router install`` last wired (``install_receipt.json``'s ``package_version``). On a mismatch:

* no consent yet: print ONE line pointing at ``/llm-router:setup`` and remember it was asked for
  this version, so the line is not repeated;
* consent given (``/llm-router:setup`` ran the installer, or an earlier ``--via-plugin`` run):
  start ``npx -y llm-routing@<plugin version> install --auto --via-plugin`` detached and return.
  The attempt (count, time) is recorded; a retry waits RETRY_AFTER_S and stops after MAX_ATTEMPTS
  per version. While a previous attempt has not produced a matching receipt the hook prints one
  line pointing at ``/llm-router:setup``. A matching receipt clears the record. The spawn runs in
  the state dir, never the project dir, so npx cannot resolve a project-local ``llm-routing``.

The plugin version and the package version move together: ``scripts/release/release.sh`` bumps
pyproject, ``.claude-plugin/plugin.json`` and ``npm/package.json`` in one release, and
``scripts/ci/verify-plugin-sync.py`` fails CI when they differ, so equality is the right test.

Nothing is written to a host config here; only the small ``plugin_install_state.json`` in the
state dir. Stdlib only: the plugin has no importable llm_router package. Never raises.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CONSENT = "plugin_install_consent.json"
STATE = "plugin_install_state.json"
RECEIPT = "install_receipt.json"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
RETRY_AFTER_S = 24 * 3600
MAX_ATTEMPTS = 3


def install_argv(version: str) -> list[str]:
    return ["npx", "-y", f"llm-routing@{version}", "install", "--auto", "--via-plugin"]


def _state_dir(env) -> Path:
    home = (env.get("LLM_ROUTER_HOME") or "").strip()
    return Path(home).expanduser() if home else Path.home() / ".llm-router"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _detached(argv: list[str], env, cwd) -> None:
    subprocess.Popen(argv, env=dict(env), cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _attempts(state: dict, plugin: str) -> tuple[int, float]:
    """(attempts, last spawn time) for this version. Planted or corrupt state (1e999, a negative
    count, nan, text) counts as attempts exhausted: no spawn, the setup line, never an exception."""
    if state.get("spawn_version") != plugin:
        return 0, 0.0
    try:
        n = state.get("spawn_attempts")
        ts = state.get("spawn_ts")
        if n is None and ts is None:  # no attempt recorded for this version: unknown, not zero
            return 0, 0.0
        if isinstance(n, bool) or isinstance(ts, bool) or not isinstance(n, int) or not isinstance(ts, (int, float)):
            raise ValueError("bad types")
        if n < 0 or not math.isfinite(ts):
            raise ValueError("out of range")
        return n, float(ts)
    except (ValueError, OverflowError, TypeError):
        return MAX_ATTEMPTS, 0.0


def run(env=os.environ, spawn=_detached, out=sys.stdout, now=time.time) -> int:
    root = Path(env.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    plugin = str(_read_json(root / ".claude-plugin" / "plugin.json").get("version") or "")
    state_dir = _state_dir(env)
    wired = str(_read_json(state_dir / RECEIPT).get("package_version") or "")
    state = _read_json(state_dir / STATE)
    try:
        if plugin and plugin == wired:
            if "spawn_version" in state:  # the install landed: drop the attempt record
                _write_state(state_dir / STATE, {k: v for k, v in state.items()
                                                 if k not in ("spawn_version", "spawn_attempts", "spawn_ts")})
            return 0
        if not plugin:
            return 0
        if not VERSION_RE.match(plugin):  # it goes into an npx argument: never pass anything else
            print(f"llm-router: plugin version {plugin[:40]!r} is not a plain version; "
                  "not starting an install. Run /llm-router:setup.", file=out)
            return 0
        pointer = ("Run /llm-router:setup to update them"
                   f" (plugin {plugin}, wired hosts {wired or 'none'}).")
        if _read_json(state_dir / CONSENT).get("consent") is not True:
            if state.get("asked_version") != plugin:
                state_dir.mkdir(parents=True, exist_ok=True)
                _write_state(state_dir / STATE, {**state, "asked_version": plugin})
                print(f"llm-router: plugin {plugin} is newer than the wired hosts ({wired or 'none'}). "
                      "Run /llm-router:setup to update them (asked once for this version).", file=out)
            return 0
        attempts, last = _attempts(state, plugin)
        if attempts:
            print("llm-router: the background update to plugin " + plugin + " has not taken effect. " + pointer, file=out)
        if attempts >= MAX_ATTEMPTS or (attempts and now() - last < RETRY_AFTER_S):
            return 0
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_state(state_dir / STATE, {**state, "spawn_version": plugin,
                                         "spawn_attempts": attempts + 1, "spawn_ts": now()})
        spawn(install_argv(plugin), {**env, "CLAUDE_PLUGIN_ROOT": str(root)}, state_dir)
    except (OSError, ValueError) as exc:
        # A SessionStart hook must not break the session; say why nothing happened.
        print(f"llm-router: plugin install check skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
