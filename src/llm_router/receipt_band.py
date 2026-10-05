"""The receipt band's host side: what the Claude Code mod asks ``llm-router`` for.

The mod (``src/llm_router/mods/llm-router-receipt``) runs inside Claude Code with
no file system or Python of its own, so it calls ``llm-router mod <verb>``:

* ``receipt --since TS [--session ID]``  the turn that started at ``TS``: was any
  of it actually served by a non-Claude model? Read from the proxy ledger
  (``proxy_calls.jsonl``, ``decision == "served"``), the one record that says a
  reply was produced off Claude. Prints ``{"routed": false}`` or
  ``{"routed": true, "key", "model", "cost_usd", "saved_usd"}``. ``cost_usd`` is
  what the served steps cost on Anthropic (0.0 for a locally served step, the
  ledger's own rule) and ``saved_usd`` the ledger's net-avoided ESTIMATE for the
  turn's served runs, an upper bound (``proxy.ledger`` module docstring);
  either is ``null`` when unknown, never 0.
* ``signal --key K --signal kept|redone --surface S``  one ``user_signal`` row
  (:mod:`llm_router.user_signal`).
* ``feed [--session ID] [--limit N]``  the last N routing decisions for the
  ``/router`` pane: time, model, why, outcome.
* ``install`` / ``uninstall``  opt-in: copy the mod under the router home and
  add that folder to ``CLAUDE_CODE_PLUGIN_DIRS`` in Claude Code's user settings
  (the ``env`` block, which Claude Code reads for that variable). Idempotent;
  the settings file is backed up before the first change and restored byte for
  byte on uninstall when nothing else in it changed meanwhile.

Turns routed through the MCP tools (Claude calls ``llm``, then answers itself)
are not receipts: Claude produced the reply. Only proxy-served replies are.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

from llm_router import paths

MOD_NAME = "llm-router-receipt"
PLUGIN_DIRS_VAR = "CLAUDE_CODE_PLUGIN_DIRS"
_INSTALL_RECORD = "mod_install.json"
_SETTINGS_BACKUP_SUFFIX = ".llm-router-mod.bak"


# ── receipt ──────────────────────────────────────────────────────────────────

def _session_rows(session_id: str | None, days: float = 2.0) -> list[dict]:
    from llm_router.proxy import ledger

    rows = ledger.read_rows(days=days)
    if session_id:
        rows = [r for r in rows if r.get("session_id") == session_id]
    return sorted(rows, key=lambda r: r.get("ts") or 0)


def last_routed_turn(since: float, session_id: str | None = None,
                     *, until: float | None = None) -> dict:
    """The receipt for the turn that began at ``since`` (see module docstring)."""
    from llm_router.proxy import cost_accounting as ca
    from llm_router.proxy import ledger

    until_ts = time.time() if until is None else until
    rows = _session_rows(session_id)
    turn = [r for r in rows if isinstance(r.get("ts"), (int, float)) and since <= r["ts"] <= until_ts]
    served = [r for r in turn if r.get("decision") == ledger.DECISION_SERVED]
    if not served:
        return {"routed": False}
    last = served[-1]
    key = last.get("msg_id")
    if not isinstance(key, str) or not key:
        key = f"proxy-{last['ts']:.3f}"
    cost: float | None = 0.0
    for r in served:
        if ca.served_by(r) != ca.SERVED_BY_LOCAL:
            cost = None
            break
    # Context for the estimate: the last real Anthropic call before the turn
    # (the cache the served steps would have read), then the turn's own rows.
    before = [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] < since
              and r.get("decision") in (ledger.DECISION_FORWARDED, ledger.DECISION_FALLBACK)]
    an = ledger.stats(before[-1:] + turn).get("anthropic") or {}
    saved = an.get("net_avoided_usd") if an.get("net_avoided_n") else None
    model = last.get("model") if isinstance(last.get("model"), str) else None
    return {"routed": True, "key": key, "model": model, "cost_usd": cost, "saved_usd": saved}


# ── feed (the /router pane) ──────────────────────────────────────────────────

def feed(session_id: str | None = None, limit: int = 10) -> list[dict]:
    """The last ``limit`` routing decisions, newest first, with the person's own
    keep / redo where they pressed one. No prompt text: the ledger holds none."""
    from llm_router import user_signal

    rows = _session_rows(session_id)[-max(1, limit):]
    signals = user_signal.latest_by_key()
    out = []
    for r in reversed(rows):
        decision = r.get("decision") or "?"
        served = decision == "served"
        model = (r.get("model") if served else None) or r.get("requested_model") or "?"
        why = r.get("task_type") if served else r.get("reason")
        outcome = decision
        sig = signals.get(r.get("msg_id") or "")
        if sig is not None:
            outcome = f"{decision}, {sig['signal']}"
        out.append({"ts": r.get("ts"), "model": model, "why": why or "-", "outcome": outcome})
    return out


# ── install / uninstall ──────────────────────────────────────────────────────

def mod_source() -> Path:
    return Path(__file__).resolve().parent / "mods" / MOD_NAME


def mod_dest() -> Path:
    return paths.state_path("mods", MOD_NAME)


def _settings_path() -> Path:
    from llm_router.install_hooks import settings_path

    return settings_path()


def _read_settings(path: Path) -> tuple[dict, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    try:
        data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); not touching it") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; not touching it")
    return data, raw


def _write_settings(path: Path, data: dict) -> None:
    """Atomic replace that keeps the file's mode (a 0600 settings file holding
    secrets in ``env`` stays 0600) and writes through a symlink to its target."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".llm-router-mod.tmp")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _record_path() -> Path:
    return paths.state_path(_INSTALL_RECORD)


def install() -> list[str]:
    """Copy the mod and point Claude Code at it. Safe to run twice."""
    actions: list[str] = []
    src, dest = mod_source(), mod_dest()
    if not (src / ".claude-plugin" / "plugin.json").exists():
        raise FileNotFoundError(f"mod source missing: {src}")
    _read_settings(_settings_path())  # refuse a malformed settings file before touching anything
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("*.test.ts", "*.test.tsx"))
    exe = shutil.which("llm-router")
    if exe:
        # Claude Code's PATH is not always the shell's: name the executable.
        (dest / "hooks" / "router-cmd.mjs").write_text(
            f"export const ROUTER_ARGV = {json.dumps([exe])}\n", encoding="utf-8")
    actions.append(f"copied the mod to {dest}" + ("" if exe else " (llm-router not on PATH: the mod will look it up at run time)"))

    spath = _settings_path()
    settings, raw = _read_settings(spath)
    env = settings.get("env")
    current = env.get(PLUGIN_DIRS_VAR) if isinstance(env, dict) else None
    entries = [p for p in (current or "").split(os.pathsep) if p]
    if str(dest) in entries:
        actions.append(f"{PLUGIN_DIRS_VAR} already names {dest}; settings unchanged")
        return actions

    record = {"had_env": isinstance(env, dict), "had_var": current is not None,
              "had_file": raw is not None, "dest": str(dest), "backup": None}
    if raw is not None:
        backup = spath.with_name(spath.name + _SETTINGS_BACKUP_SUFFIX)
        with open(backup, "wb", opener=paths.private_opener) as fh:
            fh.write(raw)
        record["backup"] = str(backup)
        actions.append(f"backed up {spath} to {backup}")
    new_env = dict(env) if isinstance(env, dict) else {}
    new_env[PLUGIN_DIRS_VAR] = os.pathsep.join(entries + [str(dest)])
    settings["env"] = new_env
    _write_settings(spath, settings)
    rpath = _record_path()
    rpath.parent.mkdir(parents=True, exist_ok=True)
    with open(rpath, "w", encoding="utf-8", opener=paths.private_opener) as fh:
        json.dump(record, fh)
    actions.append(f"added {dest} to {PLUGIN_DIRS_VAR} in {spath} (takes effect in the next session)")
    return actions


def uninstall() -> list[str]:
    """Undo :func:`install`. Safe to run twice, and when never installed."""
    actions: list[str] = []
    dest = mod_dest()
    rpath = _record_path()
    try:
        record = json.loads(rpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No record: never invent a key the person did not have.
        record = {"had_env": True, "had_var": False, "had_file": True, "backup": None}

    spath = _settings_path()
    settings, raw = _read_settings(spath)
    env = settings.get("env")
    current = env.get(PLUGIN_DIRS_VAR) if isinstance(env, dict) else None
    entries = [p for p in (current or "").split(os.pathsep) if p]
    if str(dest) in entries:
        entries = [p for p in entries if p != str(dest)]
        new_env = dict(env)
        if entries:
            new_env[PLUGIN_DIRS_VAR] = os.pathsep.join(entries)
        elif record.get("had_var"):
            new_env[PLUGIN_DIRS_VAR] = ""
        else:
            new_env.pop(PLUGIN_DIRS_VAR, None)
        if new_env or record.get("had_env"):
            settings["env"] = new_env
        else:
            settings.pop("env", None)
        backup = Path(record["backup"]) if record.get("backup") else None
        restored = False
        if backup is not None and backup.exists():
            # Byte-for-byte when the only difference is ours: the person's own
            # formatting comes back exactly. Any other change made since install
            # is kept, and the file is written structurally instead.
            original, _ = _read_settings(backup)
            if original == settings:
                spath.write_bytes(backup.read_bytes())  # keeps the file's own mode
                backup.unlink(missing_ok=True)
                restored = True
                actions.append(f"restored {spath} from {backup}")
        if not restored:
            if not settings and not record.get("had_file", True):
                spath.unlink(missing_ok=True)
                actions.append(f"removed {spath} (it did not exist before install)")
            else:
                _write_settings(spath, settings)
                actions.append(f"removed {dest} from {PLUGIN_DIRS_VAR} in {spath}")
            if backup is not None:
                backup.unlink(missing_ok=True)
    else:
        actions.append(f"{PLUGIN_DIRS_VAR} does not name {dest}; settings unchanged")
    rpath.unlink(missing_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
        actions.append(f"removed {dest}")
    return actions


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_mod(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="llm-router mod",
                                 description="The Claude Code receipt band mod (opt-in).")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("install", help="install the mod for your next Claude Code session")
    sub.add_parser("uninstall", help="remove the mod and its settings entry")
    p = sub.add_parser("receipt", help="JSON: was the turn since --since served off Claude?")
    p.add_argument("--since", type=float, required=True)
    p.add_argument("--session", default=None)
    p = sub.add_parser("signal", help="record one keep / redo press")
    p.add_argument("--key", required=True)
    p.add_argument("--signal", required=True, choices=("kept", "redone"))
    p.add_argument("--surface", required=True)
    p = sub.add_parser("feed", help="JSON: the last routing decisions")
    p.add_argument("--session", default=None)
    p.add_argument("--limit", type=int, default=10)
    parsed = ap.parse_args(args)

    if parsed.verb in ("install", "uninstall"):
        try:
            lines = install() if parsed.verb == "install" else uninstall()
        except (OSError, ValueError) as exc:
            print(f"llm-router mod {parsed.verb}: {exc}", file=sys.stderr)
            return 1
        for line in lines:
            print(line)
        return 0
    if parsed.verb == "receipt":
        print(json.dumps(last_routed_turn(parsed.since, parsed.session)))
        return 0
    if parsed.verb == "signal":
        from llm_router import user_signal

        try:
            user_signal.record(parsed.key, parsed.signal, parsed.surface)
        except (ValueError, OSError) as exc:
            print(f"llm-router mod signal: not recorded: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"recorded": True}))
        return 0
    print(json.dumps(feed(parsed.session, parsed.limit)))
    return 0
