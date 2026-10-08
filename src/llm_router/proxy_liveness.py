"""P0.14-a: is the per-call proxy's ledger alive, and is something routing around it?

Incident, 2026-10-07 12:47 to 2026-10-08 14:24: ``proxy_calls.jsonl`` wrote 0 rows for
25 hours because every Claude Code session ran with a project-level
``ANTHROPIC_BASE_URL`` that pointed straight at ``api.anthropic.com``, overriding the
user-level proxy default. Nothing flagged it: ``kpi`` rendered "not measurable" and
``doctor`` only asked whether the proxy port answered (it did).

Two readers, one module, so ``kpi`` and ``doctor`` cannot disagree:

* :func:`liveness` -- ``proxy_rows_24h`` (with its n), the user turns the hooks and
  ``routing_decisions`` recorded in the same window, and a WARN when the first is 0
  while either of the others is not. An empty set must not raise an alarm: no recorded
  turn means no warning, and a turn count that could not be read is ``None``, not 0.
* :func:`short_silence` -- the earlier warning (P0.14-b): no proxy row in the last 2 h while
  the hooks recorded at least 3 user turns in those 2 h. An empty set (0 turns) reports
  nothing; a turn count that cannot be read is ``None`` and reports nothing.
* :func:`doctor_findings` -- when the user settings make a localhost proxy the default,
  (a) every project-level ``.claude/settings.local.json`` / ``.claude/settings.json``
  under the current directory that overrides ``ANTHROPIC_BASE_URL`` (path and the
  overriding value's HOST only: never the full URL, never a header, never a key, and a value
  that is not a real hostname or IP prints as ``(unparseable)``), and (b) the ledger being
  silent for 24 h (or, earlier, for 2 h) while turns happened.

Read-only: nothing here writes a settings file or the ledger.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from llm_router import paths

__all__ = ["WINDOW_HOURS", "SHORT_WINDOW_HOURS", "SHORT_MIN_TURNS", "liveness", "short_silence", "user_proxy_default", "find_overrides", "doctor_findings"]

WINDOW_HOURS = 24.0
#: P0.14-b early warning: 0 proxy rows in this window while >= SHORT_MIN_TURNS hook turns
#: happened in it. Constants, not env keys: nobody needs to tune an alarm threshold.
SHORT_WINDOW_HOURS = 2.0
SHORT_MIN_TURNS = 3
ENV_KEY = "ANTHROPIC_BASE_URL"

#: The one hook whose invocation is a user turn (``status-bar`` is also UserPromptSubmit;
#: counting both would double every turn).
_TURN_HOOK = "auto-route"
_TURN_EVENT = "UserPromptSubmit"

_SETTINGS_FILES = ("settings.local.json", "settings.json")
_MAX_DEPTH = 3
_MAX_DIRS = 3000
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages", "Library", "target", "dist", "build"})


# -- the ledger -----------------------------------------------------------------


def _ts(row: dict) -> float | None:
    t = row.get("ts")
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def _hook_turns(since: float, until: float) -> int | None:
    try:
        from llm_router import hook_latency as hl

        return sum(1 for r in hl.read_rows(since=since, until=until)
                   if r.get("hook") == _TURN_HOOK and r.get("event") == _TURN_EVENT)
    except Exception:  # noqa: BLE001 -- unreadable is not zero
        return None


def _decision_turns(since: float, until: float) -> int | None:
    """Rows in ``routing_decisions`` inside the window; None when the table cannot be read."""
    db = paths.state_path("usage.db")
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            window = ("timestamp >= datetime(?, 'unixepoch') AND timestamp <= datetime(?, 'unixepoch')")
            try:  # backfilled sidecar rows are not turns; an older schema has no reason_code
                q = (f"SELECT COUNT(*) FROM routing_decisions WHERE {window} "
                     "AND COALESCE(reason_code,'') != 'sidecar_backfill'")
                return int(conn.execute(q, (since, until)).fetchone()[0])
            except sqlite3.OperationalError:
                return int(conn.execute(f"SELECT COUNT(*) FROM routing_decisions WHERE {window}",
                                        (since, until)).fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def liveness(*, now: float | None = None, proxy_rows: list[dict] | None = None,
             hours: float = WINDOW_HOURS) -> dict[str, Any]:
    """Ledger liveness over the ``hours`` ending at ``now``.

    ``proxy_rows`` is the ledger already read by the caller (``None`` reads it)."""
    now_ts = time.time() if now is None else now
    since = now_ts - hours * 3600.0
    if proxy_rows is None:
        from llm_router.proxy import ledger as pl

        proxy_rows = pl.read_rows()
    stamps = [t for r in proxy_rows if (t := _ts(r)) is not None]
    n = sum(1 for t in stamps if since <= t <= now_ts)
    newest = min(max(stamps), now_ts) if stamps else None   # a future-dated row is not 'newest'
    hook_turns = _hook_turns(since, now_ts)
    decisions = _decision_turns(since, now_ts)
    seen = [x for x in (hook_turns, decisions) if x]
    warn = n == 0 and bool(seen)
    message = None
    if warn:
        evidence = ", ".join(f"{label} {v}" for label, v in
                             (("hook turns", hook_turns), ("routing decisions", decisions)) if v)
        message = (f"proxy ledger wrote 0 rows in the last {hours:g} h while {evidence} were recorded: "
                   "sessions are probably bypassing the proxy (run `llm-router doctor`; if the proxy is "
                   "not meant to be in use, ignore this)")
    return {"window_hours": hours, "proxy_rows_24h": n, "newest_proxy_ts": newest,
            "hook_turns_24h": hook_turns, "routing_decisions_24h": decisions,
            "warn": warn, "message": message}


def short_silence(*, now: float | None = None, proxy_rows: list[dict] | None = None) -> dict[str, Any]:
    """P0.14-b: ``SHORT_WINDOW_HOURS`` with 0 proxy rows and ``SHORT_MIN_TURNS``+ hook turns.

    ``hook_turns`` is ``None`` when the hook ledger cannot be read, and that never warns."""
    now_ts = time.time() if now is None else now
    since = now_ts - SHORT_WINDOW_HOURS * 3600.0
    if proxy_rows is None:
        from llm_router.proxy import ledger as pl

        proxy_rows = pl.read_rows()
    n = sum(1 for r in proxy_rows if (t := _ts(r)) is not None and since <= t <= now_ts)
    turns = _hook_turns(since, now_ts)
    warn = n == 0 and turns is not None and turns >= SHORT_MIN_TURNS
    message = None
    if warn:
        message = (f"proxy ledger wrote 0 rows in the last {SHORT_WINDOW_HOURS:g} h while {turns} hook turns "
                   "were recorded: sessions started recently are probably bypassing the proxy "
                   "(if the proxy is not meant to be in use, ignore this)")
    return {"window_hours": SHORT_WINDOW_HOURS, "proxy_rows": n, "hook_turns": turns,
            "warn": warn, "message": message}


# -- settings ---------------------------------------------------------------------


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _is_hostname(host: str) -> bool:
    """RFC 1123 name with at least one dot, or exactly ``localhost``; or an IP address.

    Anything else (a bare token, a key, a name with an underscore) is not a host and must
    never be echoed: it may be a secret that was pasted into the wrong field."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    name = host[:-1] if host.endswith(".") else host
    if len(name) > 253:
        return False
    labels = name.split(".")
    if not all(_LABEL.fullmatch(lb) for lb in labels):
        return False
    if name == "localhost":
        return True
    return len(labels) >= 2 and not labels[-1].isdigit()


def _host_of(value: object) -> str:
    """``host[:port]`` of a URL-ish value, never userinfo, path or query.

    Prints only a real hostname or IP; every other string is ``(unparseable)`` (P0.14-b)."""
    if not isinstance(value, str) or not value.strip():
        return "(empty)"
    raw = value.strip()
    try:
        parts = urlsplit(raw if "//" in raw else "//" + raw)
        host = parts.hostname
        if not host or not _is_hostname(host):
            return "(unparseable)"
        port = parts.port
    except ValueError:
        return "(unparseable)"
    host = f"[{host}]" if ":" in host else host
    return f"{host}:{port}" if port else host


def _is_local(host_port: str) -> bool:
    m = re.fullmatch(r"(\[.*\]|[^:]*)(?::\d+)?", host_port)
    host = m.group(1).strip("[]") if m else host_port
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _canon(host_port: str) -> str:
    """localhost / 127.0.0.1 / ::1 are one host: the same proxy, spelled differently."""
    m = re.fullmatch(r"(\[.*\]|[^:]*)(?::(\d+))?", host_port)
    if not m:
        return host_port
    host = m.group(1).strip("[]")
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        host = "localhost"
    return f"{host}:{m.group(2)}" if m.group(2) else host


def _env_base_url(path: Path) -> tuple[bool, object]:
    """(key present, value) from a settings file's ``env`` block; unreadable is (False, None)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    env = data.get("env") if isinstance(data, dict) else None
    if isinstance(env, dict) and ENV_KEY in env:
        return True, env[ENV_KEY]
    return False, None


def _user_settings(home: Path | None) -> Path:
    return (home if home is not None else Path.home()) / ".claude" / "settings.json"


def user_proxy_default(home: Path | None = None) -> str | None:
    """``host[:port]`` when the USER settings make a localhost proxy the default, else None."""
    present, value = _env_base_url(_user_settings(home))
    if not present:
        return None
    hp = _host_of(value)
    return hp if _is_local(hp) else None


def find_overrides(cwd: Path, home: Path | None = None) -> list[dict[str, str]]:
    """Project-level settings under ``cwd`` whose ``env.ANTHROPIC_BASE_URL`` differs from the
    user's localhost default. Each: ``{"path", "host"}``. Bounded walk (depth 3, 3000 dirs)."""
    default = user_proxy_default(home)
    if default is None:
        return []
    user_file = _user_settings(home)
    try:
        user_resolved = user_file.resolve()
    except OSError:
        user_resolved = user_file
    found: list[dict[str, str]] = []
    seen_dirs = 0
    root_depth = len(Path(cwd).parts)
    for dirpath, dirnames, _files in os.walk(cwd):
        seen_dirs += 1
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = [] if (depth >= _MAX_DEPTH or seen_dirs >= _MAX_DIRS) else sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
        for name in _SETTINGS_FILES:
            f = Path(dirpath) / ".claude" / name
            if not f.is_file():
                continue
            try:
                if f.resolve() == user_resolved:
                    continue
            except OSError:
                continue
            present, value = _env_base_url(f)
            if not present:
                continue
            hp = _host_of(value)
            if _canon(hp) != _canon(default):
                found.append({"path": str(f), "host": hp})
    return found


def doctor_findings(cwd: Path | None = None, home: Path | None = None, *,
                    now: float | None = None) -> list[str]:
    """Plain-text findings for ``doctor``; empty when the proxy is not the default or all is well."""
    default = user_proxy_default(home)
    if default is None:
        return []
    out = [f"{o['path']} overrides {ENV_KEY} (user default {default}) with host {o['host']}: "
           "sessions started in this tree bypass the proxy"
           for o in find_overrides(Path(cwd) if cwd is not None else Path.cwd(), home)]
    from llm_router.proxy import ledger as pl

    rows = pl.read_rows()
    live = liveness(now=now, proxy_rows=rows)
    if live["warn"]:
        out.append(f"proxy is the configured default ({default}) but {live['message']}")
    else:  # the 24 h warning already covers a ledger that has been silent for 2 h
        short = short_silence(now=now, proxy_rows=rows)
        if short["warn"]:
            out.append(f"proxy is the configured default ({default}) but {short['message']}")
    return out
