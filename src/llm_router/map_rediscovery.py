"""Map re-discovery: rebuild the resource map when its inputs change (P1.8 tasks 8, 10).

Change signal, cheapest first:
  * mtimes of the files the map is built from (env files, the Codex auth file,
    usage.json, codex_quota.json) -- stat only;
  * a signature of Ollama's ``/api/tags``: sha256 of the sorted
    ``(name, digest, modified_at)`` triples, so a re-pull under the same name
    (new digest) is a change and a rename-free no-op is not (D-60a);
  * the map's own age (``REFRESH_TTL_S``).

Who polls: the MCP server and the proxy run :func:`maybe_start_poller` (30 s).
Hooks never poll. A hook calls :func:`pending_notice` (reads two small files)
and :func:`spawn_refresh_if_needed` (stat only, then a debounced detached
``python -m llm_router.map_rediscovery --once``), so a hook adds no network or
build latency. :func:`poll` is the single writer; it takes a non-blocking file
lock so only one process refreshes at a time.

Diffs go to ``map_changes.jsonl`` (append only). The next hook prints one line
such as ``map: + qwen3.6 local``; a cursor file records what was announced.

Known limits: the cwd ``.env`` is not a watch key, so a change to a project's
``.env`` is seen only by the 300 s TTL, and rebuilds from different cwds can
show key add/remove diffs because ``build()`` reads the cwd ``.env``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

CHANGES_FILE = "map_changes.jsonl"
CHANGES_MAX_BYTES = 256 * 1024
CHANGES_KEEP_LINES = 1000
WATCH_FILE = "map_watch.json"
CURSOR_FILE = "map_notice_cursor.json"
LOCK_FILE = "map_refresh.lock"
SPAWN_MARKER = "map_refresh.spawn"
POLL_INTERVAL_S = 30.0
NOTICE_MAX_AGE_S = 3600
SPAWN_COOLDOWN_S = 60
NOTICE_MAX_PARTS = 4
ENV_SWITCH = "LLM_ROUTER_MAP_REDISCOVERY"
_OFF = ("0", "off", "false", "no")


_ENTRY: str | None = None   # set by the entry point that owns this process's poller


def writer_identity() -> dict:
    """Who wrote a map or a change line: pid, entry point, and a short hash of
    PATH (two writers with different PATHs see different CLIs; MAP-FIX)."""
    path = os.environ.get("PATH", "")
    return {"pid": os.getpid(), "entry": _ENTRY or Path(sys.argv[0]).name or "python",
            "path_sig": hashlib.sha256(path.encode()).hexdigest()[:12]}


def _enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() not in _OFF


def _home() -> Path:
    from llm_router import paths
    return paths.llm_router_home()


def _lost(code: str, exc: BaseException) -> None:
    from llm_router import failopen
    failopen.record(code, exc)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):   # temp-file cleanup; the write error is re-raised
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ── signatures ──────────────────────────────────────────────────────────────

def tags_signature(tags: Any) -> str:
    """sha256 of sorted ``(name, digest, modified_at)``; ``"unreachable"`` if no list."""
    if not isinstance(tags, dict) or "models" not in tags:
        return "unreachable"
    seen: dict[str, list] = {}
    for m in tags.get("models") or []:
        name = m.get("name") if isinstance(m, dict) else None
        if name and name not in seen:
            seen[name] = [name, str(m.get("digest") or ""), str(m.get("modified_at") or "")]
    blob = json.dumps(sorted(seen.values()), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def watched_paths(env: dict | None = None, home: Path | str | None = None) -> list[Path]:
    """Files whose mtime change means the map may differ. Stat only, never read."""
    from llm_router import env_loader, paths
    # [0] is Path.cwd()/.env. The watch state (map_watch.json) is global, so a
    # cwd-relative key would flip between projects and force a rebuild on every
    # poll that alternates cwds. Only the user-level env files are watched.
    return [
        *env_loader.candidate_env_paths()[1:],
        Path(home) / ".codex" / "auth.json" if home else Path.home() / ".codex" / "auth.json",
        paths.state_path("usage.json"),
        paths.state_path("codex_quota.json"),
    ]


def input_mtimes(paths: list[Path]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for p in paths:
        try:
            out[str(p)] = p.stat().st_mtime_ns
        except OSError:
            out[str(p)] = None
    return out


def ollama_base(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("OLLAMA_URL") or env.get("OLLAMA_API_BASE") or "http://localhost:11434").rstrip("/")


# ── diff and notice ─────────────────────────────────────────────────────────

def _short(resource: str) -> str:
    return resource[len("ollama/"):] if resource.startswith("ollama/") else resource


def _pressure(row: dict) -> float | None:
    v = ((row.get("quota") or {}).get("pressure") or {}).get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ollama_unreachable(m: dict) -> bool:
    return any(r.get("resource") == "ollama" and "no models installed" not in (r.get("reason") or "")
               for r in m.get("resources", []))


def diff_maps(old: dict | None, new: dict) -> list[dict]:
    """Events between two maps. ``old`` None is the baseline: nothing to announce."""
    if old is None:
        return []
    o = {r["resource"]: r for r in old.get("resources", [])}
    n = {r["resource"]: r for r in new.get("resources", [])}
    ev: list[dict] = []
    # A momentary Ollama outage replaces every local row with one placeholder. That
    # is "no information", not "all removed" then "all added": local add/remove is
    # not announced across such a map (health with debounce is P1.8 task 14).
    skip_local = _ollama_unreachable(old) or _ollama_unreachable(new)

    def add(event, res, before, after, notice=""):
        ev.append({"event": event, "resource": res, "before": before, "after": after, "notice": notice})

    for res in sorted(n.keys() - o.keys()):
        r = n[res]
        if skip_local and r["kind"] == "local":
            continue
        add("added", res, None, r["status"],
            f"+ {_short(res)} {r['kind']}" if r["status"] == "connected" else "")
    for res in sorted(o.keys() - n.keys()):
        r = o[res]
        if skip_local and r["kind"] == "local":
            continue
        add("removed", res, r["status"], None,
            f"- {_short(res)} {r['kind']}" if r["status"] == "connected" else "")
    for res in sorted(o.keys() & n.keys()):
        a, b = o[res], n[res]
        if a["status"] != b["status"]:
            add("status", res, a["status"], b["status"], f"~ {_short(res)} {b['status']}")
        da, db = a.get("digest"), b.get("digest")
        if b.get("kind") == "local" and da and db and da != db:
            add("digest", res, (da or "")[:12], db[:12], f"~ {_short(res)} new digest")
        pa, pb = _pressure(a), _pressure(b)
        if pa is not None and pb is not None:
            if pa < 1.0 <= pb:
                add("quota", res, pa, pb, f"! {_short(res)} quota exhausted")
            elif pb < 1.0 <= pa:
                add("quota", res, pa, pb, f"~ {_short(res)} quota available")
    return ev


def notice_text(events: list[dict]) -> str:
    parts: list[str] = []
    for e in events:
        t = e.get("notice") or ""
        if t and t not in parts:
            parts.append(t)
    if not parts:
        return ""
    shown = parts[:NOTICE_MAX_PARTS]
    if len(parts) > len(shown):
        shown.append(f"+{len(parts) - len(shown)} more")
    return "map: " + "; ".join(shown)


def record_changes(old: dict | None, new: dict, *, source: str, now: float,
                   path: Path | None = None) -> list[dict]:
    """Append the diff of two maps to ``map_changes.jsonl``. No events, no write."""
    events = diff_maps(old, new)
    if not events:
        return events
    path = path or _home() / CHANGES_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = writer_identity()
        block = "".join(json.dumps({"ts": _iso(now), "source": source, "writer": writer, **e},
                                   sort_keys=True) + "\n"
                        for e in events)
        with open(path, "a") as f:
            f.write(block)           # one write: a poll's events are never split
        _cap_changes(path)
    except OSError as exc:
        _lost("CHZ-FO-MAP-CHANGES-WRITE", exc)
    return events


def _cap_changes(path: Path) -> None:
    """Keep the last CHANGES_KEEP_LINES lines once the file passes CHANGES_MAX_BYTES.
    Callers hold the refresh lock. The notice cursor is a timestamp, so a trim is safe."""
    if path.stat().st_size <= CHANGES_MAX_BYTES:
        return
    lines = path.read_text(errors="replace").splitlines(keepends=True)[-CHANGES_KEEP_LINES:]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):   # temp-file cleanup; the write error is re-raised
            os.unlink(tmp)
        raise


# ── the single writer ───────────────────────────────────────────────────────

def poll(*, inputs: Any = None, now: float | None = None, http_get: Callable | None = None,
         paths: list[Path] | None = None, map_path: Path | None = None,
         state_dir: Path | None = None, force: bool = False) -> list[dict]:
    """One check; rebuild + diff + log when something changed. May raise: the poller
    thread and the ``--once`` entry point catch."""
    from llm_router import resource_map as rm
    from llm_router.file_lock import exclusive_lock

    now = time.time() if now is None else now
    sdir = state_dir or _home()
    get = http_get or (inputs.http_get if inputs is not None else rm._http_get)
    env = inputs.env if inputs is not None else os.environ
    base = (getattr(inputs, "ollama_url", "") or "").rstrip("/") or ollama_base(env)
    with exclusive_lock(sdir / LOCK_FILE, timeout=0) as got:
        if not got:
            return []
        mtimes = input_mtimes(paths if paths is not None else watched_paths(env))
        sig = tags_signature(get(f"{base}/api/tags"))
        prior = _read_json(sdir / WATCH_FILE)
        prior = prior if isinstance(prior, dict) else None
        cur = rm.load_map(map_path)
        reasons: list[str] = []
        if prior is not None:
            reasons += [f"mtime:{Path(k).name}" for k, v in mtimes.items()
                        if (prior.get("mtimes") or {}).get(k) != v]
            if prior.get("tags_sig") != sig:
                reasons.append("ollama_signature")
        elif cur is not None:
            reasons.append("first_run")
        age = rm.age_seconds(cur, now) if cur is not None else None
        if cur is None or age is None or age > rm.REFRESH_TTL_S or age < 0:
            reasons.append("stale")
        if force:
            reasons.append("forced")
        state = {"mtimes": mtimes, "tags_sig": sig, "checked_at": now}
        if not reasons:
            _atomic_write_json(sdir / WATCH_FILE, state)
            return []
        if inputs is not None:
            inputs.now = now
        data = rm.carry_connected(cur, rm.build(inputs), now)
        rm.write_map(data, map_path)
        events = record_changes(cur, data, source=",".join(reasons), now=now,
                                path=(map_path.parent if map_path is not None else sdir) / CHANGES_FILE)
        _atomic_write_json(sdir / WATCH_FILE, state)
        return events


# ── hook side: read only ────────────────────────────────────────────────────

def pending_notice(*, now: float | None = None, consume: bool = True,
                   path_dir: Path | None = None) -> str:
    """The one-line notice for changes not yet announced. No network, no build."""
    d = path_dir or _home()
    now = time.time() if now is None else now
    try:
        raw = (d / CHANGES_FILE).read_bytes()
    except OSError:
        return ""
    cur = _read_json(d / CURSOR_FILE)
    last = cur.get("ts") if isinstance(cur, dict) else None
    last = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else 0.0
    parsed: list[tuple[float, dict]] = []
    tail_bad = False
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(e["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            tail_bad = True       # only the LAST line matters: a half-written tail
            continue
        tail_bad = False
        if isinstance(e, dict):
            parsed.append((ts, e))
    if tail_bad and parsed:
        # Events of one batch share a timestamp. The batch is still being written,
        # so hold it back whole and read it again next time; advancing the cursor
        # to its timestamp would skip the unwritten rest of it for good.
        open_ts = parsed[-1][0]
        parsed = [(t, e) for t, e in parsed if t < open_ts]
    kept = []
    newest = last
    for ts, e in parsed:
        if ts <= last:
            continue
        newest = max(newest, ts)
        if 0 <= now - ts <= NOTICE_MAX_AGE_S:
            kept.append(e)
    if consume and newest > last:
        try:
            _atomic_write_json(d / CURSOR_FILE, {"ts": newest})
        except OSError as exc:
            _lost("CHZ-FO-MAP-NOTICE-CURSOR", exc)
    return notice_text(kept)


def needs_refresh(*, now: float | None = None, paths: list[Path] | None = None,
                  state_dir: Path | None = None, map_path: Path | None = None) -> bool:
    """Stat-only: have inputs changed since the last poll, or is the map stale?"""
    from llm_router import resource_map as rm
    sdir = state_dir or _home()
    prior = _read_json(sdir / WATCH_FILE)
    if not isinstance(prior, dict):
        return True
    if input_mtimes(paths if paths is not None else watched_paths()) != (prior.get("mtimes") or {}):
        return True
    cur = rm.load_map(map_path)
    age = rm.age_seconds(cur, now) if cur is not None else None
    return age is None or age > rm.REFRESH_TTL_S or age < 0


def _default_spawner(argv: list[str]) -> None:
    kw: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kw)


def spawn_refresh_if_needed(*, now: float | None = None,
                            spawner: Callable[[list[str]], Any] | None = None) -> bool:
    """Start one detached refresh when needed; debounced; never waits, never raises."""
    try:
        if not _enabled():
            return False
        now = time.time() if now is None else now
        if not needs_refresh(now=now):
            return False
        marker = _home() / SPAWN_MARKER
        try:
            if 0 <= now - marker.stat().st_mtime < SPAWN_COOLDOWN_S:
                return False
        except OSError:
            pass
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        os.utime(marker, (now, now))
        (spawner or _default_spawner)([sys.executable, "-m", "llm_router.map_rediscovery", "--once"])
        return True
    except Exception:  # noqa: BLE001 - a hook must never fail on this
        return False


# ── long-running processes ──────────────────────────────────────────────────

class Poller:
    def __init__(self, interval_s: float, poll_fn: Callable, wait: Callable | None, kwargs: dict):
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._thread = threading.Thread(target=self._run, args=(interval_s, poll_fn, kwargs),
                                        name="map-rediscovery", daemon=True)

    def _run(self, interval_s: float, poll_fn: Callable, kwargs: dict) -> None:
        while not self._stop.is_set():
            try:
                poll_fn(**kwargs)
            except Exception as exc:  # noqa: BLE001
                _lost("CHZ-FO-MAP-POLL", exc)
                log.debug("map re-discovery poll failed: %s", exc)
            self._wait(interval_s)

    def start(self) -> "Poller":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)


_default_poller: Poller | None = None


def start_poller(interval_s: float = POLL_INTERVAL_S, *, poll_fn: Callable = poll,
                 wait: Callable | None = None, **poll_kwargs: Any) -> Poller:
    """Start a daemon poll thread. The default poll_fn keeps one per process."""
    global _default_poller
    if poll_fn is poll:
        if _default_poller is not None:
            return _default_poller
        # Bind the state locations now: a process that later changes
        # LLM_ROUTER_HOME (a test worker) must not have this thread write into it.
        from llm_router import resource_map as rm
        poll_kwargs.setdefault("state_dir", _home())
        poll_kwargs.setdefault("map_path", rm.map_path())
        _default_poller = Poller(interval_s, poll_fn, wait, poll_kwargs).start()
        return _default_poller
    return Poller(interval_s, poll_fn, wait, poll_kwargs).start()


def stop_default_poller(timeout: float = 10.0) -> bool:
    """Stop and join this process's default poller (if any); True when none is left running.

    A process that must make no outbound connection (the synthetic-replay harness
    run in-process) calls this before installing its guard: a poller started by an
    entry point (``server.main``, ``main_sse*``, ``cmd_proxy``) would otherwise probe
    Ollama through the harness' network guard on its next tick."""
    global _default_poller
    p, _default_poller = _default_poller, None
    if p is None:
        return True
    p.stop()
    p.join(timeout)
    return not p._thread.is_alive()


def maybe_start_poller(entry: str | None = None) -> Poller | None:
    """*entry* names this process in ``writer`` fields (``mcp``, ``proxy``)."""
    global _ENTRY
    if entry:
        _ENTRY = entry
    try:
        return start_poller() if _enabled() else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    global _ENTRY
    # Under `python -m` this module is __main__; resource_map.build() reads the
    # writer from the imported llm_router.map_rediscovery, so name both.
    from llm_router import map_rediscovery as _canonical
    _ENTRY = _canonical._ENTRY = _ENTRY or _canonical._ENTRY or "hook-once"
    try:
        if "--once" in (sys.argv[1:] if argv is None else argv) and _enabled():
            poll()
    except Exception as exc:  # noqa: BLE001
        _lost("CHZ-FO-MAP-REFRESH-CHILD", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
