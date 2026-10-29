#!/usr/bin/env python3
# llm_router-hook-version: 1
"""Claude Code plugin SessionStart hook (P2.12, P212-3): keep the wired hosts on the plugin's version.

The plugin bundle (``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json``) can move ahead of what
``llm-router install`` last wired (``install_receipt.json``'s ``package_version``). On a mismatch:

* no consent yet: print ONE line pointing at ``/llm-router:setup`` and remember it was asked for
  this version, so the line is not repeated;
* consent given (``/llm-router:setup`` ran the installer, or an earlier ``--via-plugin`` run):
  start ``npx -y llm-routing install --auto --via-plugin`` detached, once per version, and return.

Nothing is written to a host config here; only the small ``plugin_install_state.json`` in the
state dir. Stdlib only: the plugin has no importable llm_router package. Never raises.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CONSENT = "plugin_install_consent.json"
STATE = "plugin_install_state.json"
RECEIPT = "install_receipt.json"
INSTALL_ARGV = ["npx", "-y", "llm-routing", "install", "--auto", "--via-plugin"]


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


def _detached(argv: list[str], env) -> None:
    subprocess.Popen(argv, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def run(env=os.environ, spawn=_detached, out=sys.stdout) -> int:
    root = Path(env.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    plugin = str(_read_json(root / ".claude-plugin" / "plugin.json").get("version") or "")
    state_dir = _state_dir(env)
    wired = str(_read_json(state_dir / RECEIPT).get("package_version") or "")
    if not plugin or plugin == wired:
        return 0
    state = _read_json(state_dir / STATE)
    consent = _read_json(state_dir / CONSENT).get("consent") is True
    try:
        if consent:
            if state.get("spawned_version") != plugin:
                state_dir.mkdir(parents=True, exist_ok=True)
                _write_state(state_dir / STATE, {**state, "spawned_version": plugin})
                spawn(INSTALL_ARGV, {**env, "CLAUDE_PLUGIN_ROOT": str(root)})
        elif state.get("asked_version") != plugin:
            state_dir.mkdir(parents=True, exist_ok=True)
            _write_state(state_dir / STATE, {**state, "asked_version": plugin})
            print(f"llm-router: plugin {plugin} is newer than the wired hosts ({wired or 'none'}). "
                  "Run /llm-router:setup to update them (asked once for this version).", file=out)
    except OSError as exc:
        # A SessionStart hook must not break the session; say why nothing happened.
        print(f"llm-router: plugin install check skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
