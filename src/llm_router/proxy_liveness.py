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
* :func:`ledger_silence` -- the alert (P0.14-d, 2026-10-08 outage: the ledger was silent
  from 16:55:02Z for two hours while the 24 h count still held rows, so nothing warned).
  SILENT when, in the last ``LIVENESS_WINDOW_MIN`` minutes, the proxy ledger has 0 rows,
  the hooks recorded at least one ORGANIC CLAUDE CODE turn, and the proxy-default
  sentinel is enabled and not opted out. A Codex or Gemini turn never goes through this
  proxy and does not count; nor does a turn from a session whose project settings, or
  whose own launch environment, legitimately point ``ANTHROPIC_BASE_URL`` elsewhere. Shown by ``kpi``, ``doctor`` and
  SessionStart (never the statusline).
* :func:`ledger_gaps` -- every interval longer than that window with no proxy row and at
  least one such turn, for a gate file (``kpi --json --since --until``).
* :func:`doctor_findings` -- when the user settings make a localhost proxy the default,
  (a) every project-level ``.claude/settings.local.json`` / ``.claude/settings.json``
  under the current directory that overrides ``ANTHROPIC_BASE_URL`` (path and the
  overriding value's HOST only: never the full URL, never a header, never a key, and a value
  that is not a real hostname or IP prints as ``(unparseable)``), and (b) the ledger being
  silent for 24 h (or, earlier, for 2 h) while turns happened.

Read-only: nothing here writes a settings file or the ledger.
"""

from __future__ import annotations

from llm_router.provider_classes import real_decision_sql

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

__all__ = ["WINDOW_HOURS", "SHORT_WINDOW_HOURS", "SHORT_MIN_TURNS", "LIVENESS_WINDOW_MIN", "liveness",
           "short_silence", "ledger_silence", "ledger_gaps", "user_proxy_default", "find_overrides",
           "doctor_findings"]

WINDOW_HOURS = 24.0
#: P0.14-b early warning: 0 proxy rows in this window while >= SHORT_MIN_TURNS hook turns
#: happened in it. Constants, not env keys: nobody needs to tune an alarm threshold.
SHORT_WINDOW_HOURS = 2.0
SHORT_MIN_TURNS = 3
#: P0.14-d alert window (R8 ``liveness_window_min``): 0 proxy rows in this many minutes while
#: an organic Claude Code turn happened in them.
LIVENESS_WINDOW_MIN = 30.0
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
                     "AND COALESCE(reason_code,'') != 'sidecar_backfill' AND " + real_decision_sql(conn))
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


# -- P0.14-d: the 30 min ledger-silence alert ----------------------------------------


_TAIL_CHUNK = 65536
_REORDER_SLACK_S = 120.0  # rows are appended at write time; concurrent writers reorder by seconds


def _tail_dicts(path: Path, since: float) -> tuple[list[dict], bool]:
    """Rows of a JSONL file from the end back to the first one older than ``since``.

    Reads backwards in growing chunks, so SessionStart does not parse a 35 MB ledger to
    learn whether its last row is recent. Returns (rows, reached): ``reached`` is True when
    a row older than ``since`` (or the start of the file) was reached."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], False
    chunk = _TAIL_CHUNK
    while True:
        start = max(0, size - chunk)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return [], False
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line is cut by the seek
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        oldest = min((t for r in rows if (t := _ts(r)) is not None), default=None)
        if start == 0 or (oldest is not None and oldest < since - _REORDER_SLACK_S):
            return rows, True
        chunk *= 4


def _recent_hook_rows(since: float, until: float) -> list[dict]:
    from llm_router import hook_latency as hl

    path = hl.store_path()
    rows, reached = _tail_dicts(path, since)
    if not reached or not rows or min(_ts(r) or until for r in rows) >= since:
        older, _ = _tail_dicts(path.with_name(path.name + ".1"), since)  # rotated just now
        rows = older + rows
    return [r for r in rows if (t := _ts(r)) is not None and since <= t <= until]


def _sentinel() -> dict | None:
    try:
        data = json.loads(paths.state_path("proxy_default.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sentinel_ports(sentinel: dict | None) -> list[int]:
    ports = []
    for key in ("port", "upstream_port"):
        try:
            if sentinel and sentinel.get(key) is not None:
                ports.append(int(sentinel[key]))
        except (TypeError, ValueError):
            continue
    return ports or [8787]


def _routes_to(value: object, ports: list[int]) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value.strip() if "//" in value else "//" + value.strip())
        return parts.hostname in ("127.0.0.1", "localhost", "::1") and parts.port in ports
    except ValueError:
        return False


def project_override(project: str | None, ports: list[int]) -> str | None:
    """The project settings file that makes a session in ``project`` bypass the proxy on
    purpose, else None. Claude Code's precedence: ``.claude/settings.local.json``, then
    ``.claude/settings.json``; the first that sets a non-empty ``ANTHROPIC_BASE_URL`` wins,
    and it is an override only when it does not point at the proxy's own ports."""
    if not project:
        return None
    for name in _SETTINGS_FILES:
        f = Path(project) / ".claude" / name
        present, value = _env_base_url(f)
        if present and isinstance(value, str) and value.strip():
            return None if _routes_to(value, ports) else str(f)
    return None


def _env_overrides(base_url: object, ports: list[int]) -> bool:
    """True when a hook row's ``base_url`` (``loopback:<port>`` / ``loopback`` / ``other``)
    says the session ran with a base URL that is not the proxy's."""
    if not isinstance(base_url, str) or not base_url:
        return False
    head, _, port = base_url.partition(":")
    return not (head == "loopback" and port.isdigit() and int(port) in ports)


def _session_record(session_id: str) -> dict:
    """The SessionStart tag file (``session_kind_<sid>.json``): kind, cwd, entrypoint."""
    from llm_router import session_kind as sk

    try:
        data = json.loads(sk._tag_path(session_id).read_text(encoding="utf-8"))
        rec = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        rec = {}
    return {"kind": sk.kind_of(session_id), "cwd": rec.get("cwd"), "entrypoint": rec.get("entrypoint")}


def _classify_turns(rows: list[dict], ports: list[int]) -> tuple[list[dict], dict[str, int]]:
    """(organic Claude Code turns as ``{ts, session_id}``, excluded counts by reason).

    Host: the row's ``host`` (written only on positive evidence, ``hook_latency.detect_host``).
    A row without one counts as Claude Code only when its session's tag has an
    ``entrypoint`` (Claude Code exports ``CLAUDE_CODE_ENTRYPOINT``; a Codex hook process
    does not). Override, in Claude Code's precedence (the same order as session-start's
    ``_effective_base_url``): the base URL the hook process inherited (``base_url`` on the
    row: the launching shell or the merged settings ``env``), else the session's project
    settings (:func:`project_override`). Either one pointing away from the proxy's ports is
    a deliberate bypass. A missing user-level key is NOT one: that was the 2026-10-08 fault."""
    cache: dict[str, dict] = {}
    kept: list[dict] = []
    excluded = {"other_host": 0, "not_organic": 0, "env_override": 0, "project_override": 0,
                "no_session": 0}
    for r in rows:
        if r.get("hook") != _TURN_HOOK or r.get("event") != _TURN_EVENT:
            continue
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:
            excluded["no_session"] += 1
            continue
        rec = cache.get(sid)
        if rec is None:
            rec = cache[sid] = _session_record(sid)
            rec["override"] = project_override(rec["cwd"], ports)
        host = r.get("host") or ("claude_code" if rec["entrypoint"] else None)
        if host != "claude_code":
            excluded["other_host"] += 1
        elif rec["kind"] != "organic":
            excluded["not_organic"] += 1
        elif _env_overrides(r.get("base_url"), ports):
            excluded["env_override"] += 1
        elif rec["override"]:
            excluded["project_override"] += 1
        else:
            kept.append({"ts": _ts(r), "session_id": sid})
    return kept, excluded


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def ledger_silence(*, now: float | None = None, proxy_rows: list[dict] | None = None,
                   hook_rows: list[dict] | None = None,
                   window_min: float | None = None) -> dict[str, Any]:
    """P0.14-d. ``state``: ``SILENT``; ``ok`` (proxy rows in the window); ``quiet`` (no organic
    Claude Code turn in it); ``off`` (no sentinel, ``enabled: false`` or ``routing_opt_out``).

    ``proxy_rows`` / ``hook_rows`` are ledgers the caller already read (``None`` reads only
    the tail each needs). Read-only."""
    now_ts = time.time() if now is None else now
    minutes = LIVENESS_WINDOW_MIN if window_min is None else window_min
    since = now_ts - minutes * 60.0
    out: dict[str, Any] = {"window_min": minutes, "state": "off", "silent": False, "proxy_rows": None,
                           "organic_cc_turns": None, "sessions": 0, "excluded": None,
                           "newest_proxy_ts": None, "message": None}
    sentinel = _sentinel()
    if not sentinel or sentinel.get("enabled") is False or sentinel.get("routing_opt_out") is True:
        return out
    ports = _sentinel_ports(sentinel)
    if proxy_rows is None:
        from llm_router.proxy import ledger as pl

        proxy_rows, _ = _tail_dicts(pl.ledger_path(), since)
    stamps = [t for r in proxy_rows if (t := _ts(r)) is not None and t <= now_ts]
    n = sum(1 for t in stamps if t >= since)
    newest = max(stamps) if stamps else None
    if hook_rows is None:
        hook_rows = _recent_hook_rows(since, now_ts)
    turns, excluded = _classify_turns(
        [r for r in hook_rows if (t := _ts(r)) is not None and since <= t <= now_ts], ports)
    sessions = len({t["session_id"] for t in turns})
    out.update(proxy_rows=n, organic_cc_turns=len(turns), sessions=sessions, excluded=excluded,
               newest_proxy_ts=newest)
    if n:
        out["state"] = "ok"
    elif not turns:
        out["state"] = "quiet"
    else:
        last = f"last proxy row {_iso(newest)}" if newest is not None else "no proxy row on record"
        out.update(state="SILENT", silent=True, message=(
            f"SILENT proxy ledger: 0 rows in the last {minutes:g} min while {len(turns)} organic "
            f"Claude Code turn(s) in {sessions} session(s) were recorded and proxy-default is on "
            f"(port {ports[0]}); {last}. Routing is probably OFF: check ~/.claude/settings.json "
            "env.ANTHROPIC_BASE_URL and run `llm-router doctor`"))
    return out


def ledger_gaps(*, since: float, until: float, proxy_rows: list[dict],
                hook_rows: list[dict] | None = None,
                window_min: float | None = None) -> list[dict[str, Any]]:
    """Every interval inside ``[since, until]`` longer than the alert window with no proxy row
    and at least one organic Claude Code turn (the alert's own filters), oldest first.

    ``start``/``end`` are the proxy rows that bound the gap, or the window edge
    (``open_start`` / ``open_end``). The sentinel is not consulted: its past state is not
    recorded, and a gate needs the gap either way."""
    minutes = LIVENESS_WINDOW_MIN if window_min is None else window_min
    if hook_rows is None:
        from llm_router import hook_latency as hl

        hook_rows = hl.read_rows(since=since, until=until)
    turns, _ = _classify_turns([r for r in hook_rows if (t := _ts(r)) is not None and since <= t <= until],
                               _sentinel_ports(_sentinel()))
    turn_ts = sorted(t["ts"] for t in turns)
    stamps = sorted(t for r in proxy_rows if (t := _ts(r)) is not None and since <= t <= until)
    bounds = [since, *stamps, until]
    gaps = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        if b - a <= minutes * 60.0:
            continue
        inside = sum(1 for t in turn_ts if a < t < b)
        if inside:
            gaps.append({"start": _iso(a), "end": _iso(b), "start_ts": a, "end_ts": b,
                         "minutes": round((b - a) / 60.0, 1), "organic_cc_turns": inside,
                         "open_start": i == 0, "open_end": i == len(bounds) - 2})
    return gaps


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
    """Plain-text findings for ``doctor``; empty when all is well.

    The P0.14-d SILENT alert comes first and does not need the user settings to name the
    proxy: in the 2026-10-08 outage the missing user key WAS the fault."""
    try:  # its own guard: a failure here must not drop the override findings below
        silence = ledger_silence(now=now)
    except Exception:  # noqa: BLE001 -- an unreadable ledger is not a silent one
        silence = {"silent": False, "message": None}
    out = [silence["message"]] if silence["silent"] else []
    default = user_proxy_default(home)
    if default is None:
        return out
    out += [f"{o['path']} overrides {ENV_KEY} (user default {default}) with host {o['host']}: "
            "sessions started in this tree bypass the proxy"
            for o in find_overrides(Path(cwd) if cwd is not None else Path.cwd(), home)]
    from llm_router.proxy import ledger as pl

    rows = pl.read_rows()
    live = liveness(now=now, proxy_rows=rows)
    if live["warn"]:
        out.append(f"proxy is the configured default ({default}) but {live['message']}")
    elif not silence["silent"]:  # the 24 h warning (or SILENT) already covers a 2 h silence
        short = short_silence(now=now, proxy_rows=rows)
        if short["warn"]:
            out.append(f"proxy is the configured default ({default}) but {short['message']}")
    return out
