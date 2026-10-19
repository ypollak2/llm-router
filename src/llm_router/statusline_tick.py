"""The status line's per-tick renderer. STDLIB ONLY, and it must stay that way.

Claude Code runs the ``statusLine`` command about once a second. This file is
what that command executes in debug mode (``LLM_ROUTER_STATUSLINE=fast``;
``hooks/statusline-command.sh`` execs it; the installer copies it next to the
hooks as ``llm_router_statusline_tick.py``), so everything here is on a
one-second loop:

* it never imports ``llm_router`` (the package import alone costs more than the
  whole tick budget) and never computes a KPI. It reads ONE small cache file,
  ``statusline_cache.json``, that :mod:`llm_router.statusline_refresh` writes,
  and the Claude quota snapshot ``usage.json`` (session 5h / weekly / Sonnet %,
  the numbers ``llm-router status`` prints), which the hooks keep fresh. Quota
  is never fetched from here;
* when that cache is missing or older than :data:`REFRESH_AFTER_S` it starts the
  refresher DETACHED and does not wait for it. At most one start per
  :data:`REFRESH_AFTER_S` (a timestamp file, not a lock: a hung refresher holds
  nothing a tick waits on), so a stuck refresh cannot delay a tick or pile up;
* every :data:`HISTORY_INTERVAL_S` (300 s) it appends one row of the quota it
  just read to ``quota_history.jsonl`` (GE6 / S3: the 5-minute series; one
  ``stat`` of a stamp file per tick otherwise). Stale snapshots are recorded as
  ``stale``, unknown values as null;
* a value it does not have is printed as ``n/a``, never as 0. A cache older than
  :data:`STALE_AFTER_S` is not data: every cached field reads ``n/a``. A quota
  snapshot older than ``LLM_ROUTER_USAGE_TTL_SEC`` (default 300 s, the same TTL
  as the full layout's ``°`` marker) is still shown, with ``(stale <age>)``;
* it reads nothing from stdin but drains it (Claude Code pipes the session JSON
  and times out a command that leaves the pipe full). The session JSON carries
  no prompt text today, and nothing from it is printed either way.

Output: one line, at most :data:`MAX_CHARS` characters (counted without the
colour codes), e.g.::

    llm-router · smart · NS 0.0% n=3599 · Claude 5h 12% wk 41% sonnet 3% · Codex 7/15 ↻21:14 · ⚠ hooks p95 3.0s auto-route
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

# subprocess / shlex are imported only on the rare path that needs them: on this
# machine's python3 they add ~15 ms to a tick, and the bar is 100 ms.

CACHE_NAME = "statusline_cache.json"
#: The Claude quota snapshot the hooks write (``llm-router status`` reads it too).
USAGE_NAME = "usage.json"
#: Past this age a quota snapshot is shown with a stale marker (the full
#: layout's ``LLM_ROUTER_USAGE_TTL_SEC`` default).
DEFAULT_USAGE_TTL_S = 300.0
#: (usage.json key, label), in the order ``llm-router status`` lists them.
_QUOTA_FIELDS = (("session_pct", "5h"), ("weekly_pct", "wk"), ("sonnet_pct", "sonnet"))
REFRESH_STAMP_NAME = ".statusline-refresh.last"
#: The cache is refreshed in the background at most this often.
REFRESH_AFTER_S = 60.0
#: ... and, when a ledger changed since the cache was written, at most this often (P35-2).
LEDGER_SPAWN_MIN_S = 10.0
_SESSION_RE = re.compile(r'"session_id"\s*:\s*"([A-Za-z0-9._-]{1,128})"')
#: Past this age the cached values are not shown at all ("n/a").
STALE_AFTER_S = 15 * 60.0
MAX_CHARS = 200
#: GE6 / S3: the 5-minute quota series (``llm_router.quota_samples`` reads it).
HISTORY_NAME = "quota_history.jsonl"
HISTORY_STAMP_NAME = ".quota_history.last"
#: One history row at most this often (the quota TTL above).
HISTORY_INTERVAL_S = 300.0
#: F107: history rows older than this are pruned. The longest reader window is 14 days
#: (``quota_rate``, ``statusline_snapshot.BURN_DAYS``); 30 days is that plus a margin for the
#: ``kpi --quota-burn`` coverage report. Docs: ``docs/RETENTION.md``.
HISTORY_RETAIN_S = 30 * 86400.0
#: ... and never more than this many rows (30 d at 288/day is 8,640; a clock glitch cannot grow it).
HISTORY_MAX_ROWS = 20_000
HISTORY_PRUNE_STAMP_NAME = ".quota_history.pruned"
HISTORY_PRUNE_EVERY_S = 86400.0
#: A usage.json older than this is recorded ``stale``, not ``measured``.
QUOTA_STALE_AFTER_S = 30 * 60.0
#: Longest model / hook name the line will carry before it is cut.
_NAME_MAX = 24

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SAFE = re.compile(r"[^A-Za-z0-9._:/+-]")


def router_home() -> str:
    """``LLM_ROUTER_HOME`` or ``~/.llm-router`` (mirrors ``llm_router.paths``)."""
    override = os.environ.get("LLM_ROUTER_HOME", "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".llm-router")


def read_cache(home: str) -> dict | None:
    try:
        with open(os.path.join(home, CACHE_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_usage(home: str) -> dict | None:
    try:
        with open(os.path.join(home, USAGE_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
            mtime = os.fstat(fh.fileno()).st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if _num(data.get("updated_at")) in (None, 0.0):
        data["updated_at"] = mtime  # older snapshots lack it: file mtime, as the full layout does
    return data


def usage_ttl() -> float:
    raw = os.environ.get("LLM_ROUTER_USAGE_TTL_SEC", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_USAGE_TTL_S
    return value if value > 0 else DEFAULT_USAGE_TTL_S


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _quota(usage: dict | None, now: float, ttl: float) -> str:
    """``Claude 5h 12% wk 41% sonnet 3%`` from a ``usage.json`` snapshot.

    Unknown is ``n/a``, never 0: no file, the install placeholder (``pending``),
    the failed-fetch snapshot (``is_fallback``: invented 50s), or a field that is
    not a number. A snapshot past ``ttl`` is still shown, marked ``(stale <age>)``;
    one with no ``updated_at`` has an unknown age and is marked ``(stale)``.
    """
    if not isinstance(usage, dict) or usage.get("pending") or usage.get("is_fallback"):
        return "Claude n/a"
    values = [(label, _num(usage.get(key))) for key, label in _QUOTA_FIELDS]
    if all(v is None for _, v in values):
        return "Claude n/a"
    seg = "Claude " + " ".join(f"{label} {v:.0f}%" if v is not None else f"{label} n/a"
                               for label, v in values)
    updated = _num(usage.get("updated_at"))
    if updated is None or updated <= 0:
        return seg + " (stale)"
    if now - updated > ttl:
        seg += f" (stale {_age(now - updated)})"
    return seg


def _num(value) -> float | None:
    """A real number, or None. ``True`` is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


def _name(value) -> str:
    """A model / hook name made safe for a terminal line: no escapes, bounded."""
    text = _SAFE.sub("", str(value))[:_NAME_MAX]
    return text or "?"


def _mode_label(raw: str | None) -> str:
    if not raw:
        return "n/a"
    mode = raw.strip().lower()
    if mode in ("off", "shadow", "observe"):
        return "off"
    return _name(mode)


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def render(cache: dict | None, *, now: float, env_mode: str | None = None, color: bool = False,
           usage: dict | None = None, usage_ttl_s: float = DEFAULT_USAGE_TTL_S) -> str:
    """The status line for ``cache`` and the quota snapshot ``usage`` at ``now``. Pure: no I/O.

    ``env_mode`` is ``LLM_ROUTER_ENFORCE`` from this process's environment; it
    wins over the cached mode exactly as ``enforce_config`` lets it win.
    """
    fresh = None
    if isinstance(cache, dict):
        written = _num(cache.get("written_at"))
        if written is not None and 0 <= now - written <= STALE_AFTER_S:
            fresh = cache

    parts: list[str] = ["llm-router"]

    parts.append(_mode_label(env_mode or (fresh or {}).get("mode")))

    ns = (fresh or {}).get("ns") if fresh else None
    ns_pct = _num(ns.get("pct")) if isinstance(ns, dict) else None
    ns_n = _num(ns.get("n")) if isinstance(ns, dict) else None
    if ns_pct is not None and ns_n is not None and ns_n > 0:
        parts.append(f"NS {ns_pct:.1f}% n={int(ns_n)}")
    elif ns_n is not None and ns_n > 0:
        parts.append(f"NS n/a n={int(ns_n)}")  # counted, too few to state a share
    else:
        parts.append("NS n/a")

    parts.append(_quota(usage, now, usage_ttl_s))

    codex = (fresh or {}).get("codex") if fresh else None
    used = _num(codex.get("used")) if isinstance(codex, dict) else None
    limit = _num(codex.get("budget")) if isinstance(codex, dict) else None
    if used is not None and limit is not None:
        seg = f"Codex {int(used)}/{int(limit)}"
        resets = _num(codex.get("resets_at"))
        if resets is not None and resets > now:
            seg += f" ↻{_clock(resets)}"
        parts.append(seg)
    else:
        parts.append("Codex n/a")

    slow = (fresh or {}).get("hooks_slow") if fresh else None
    if isinstance(slow, dict):
        p95 = _num(slow.get("p95_ms"))
        if p95 is not None:
            mark = f"⚠ hooks p95 {p95 / 1000:.1f}s {_name(slow.get('hook', '?'))}"
            parts.append(f"\x1b[33m{mark}\x1b[0m" if color else mark)

    line = " · ".join(parts)
    return _fit(line)


def _fit(line: str) -> str:
    """Cut to :data:`MAX_CHARS` visible characters, never mid-escape."""
    if len(_ANSI.sub("", line)) <= MAX_CHARS:
        return line
    plain = _ANSI.sub("", line)
    return plain[: MAX_CHARS - 1] + "…"


def _which(name: str) -> str | None:
    """``shutil.which`` without importing shutil (~10 ms on this python3)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(d, name)
        if d and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _refresh_argv() -> list[str] | None:
    """The refresher's command line. ``LLM_ROUTER_STATUSLINE_REFRESH_CMD`` (a
    shell-quoted string) overrides it, for tests and unusual installs; otherwise
    ``llm-router statusline --refresh`` found on PATH, or nothing at all."""
    override = os.environ.get("LLM_ROUTER_STATUSLINE_REFRESH_CMD", "").strip()
    if override:
        import shlex

        try:
            argv = shlex.split(override)
        except ValueError:
            return None
        if argv and os.sep not in argv[0]:
            argv[0] = _which(argv[0]) or argv[0]
        return argv or None
    exe = _which("llm-router")
    return [exe, "statusline", "--refresh"] if exe else None


def _spawn_detached(argv: list[str]) -> int | None:
    """Start ``argv`` in its own session with no stdio, and do not wait for it.

    Returns the child pid (callers that need to reap or stop it, e.g. the G1 evidence
    script, use it; the hooks ignore it).

    ``os.fork`` + ``execv`` where it exists: importing ``subprocess`` costs ~15 ms
    a tick on this machine's python3, which is the whole margin of a 100 ms bar."""
    if hasattr(os, "fork") and hasattr(os, "setsid"):
        pid = os.fork()
        if pid == 0:  # child: never returns into the caller
            try:
                os.setsid()
                null = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                os.execv(argv[0], argv)
            finally:
                os._exit(127)
        return pid
    import subprocess  # Windows: no fork

    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                            stderr=subprocess.DEVNULL, close_fds=True).pid


def ledger_changed_since(home: str, stamp: float) -> bool:
    """True when a ledger was written after ``stamp`` (P35-2): two ``stat`` calls, no read. The first
    that exists decides for the database (``usage.db-wal`` takes every write until a checkpoint, so it
    is the live mtime; ``usage.db`` when there is no WAL), then ``proxy_calls.jsonl``."""
    for names in (("usage.db-wal", "usage.db"), ("proxy_calls.jsonl",)):
        for name in names:
            try:
                mtime = os.path.getmtime(os.path.join(home, name))
            except OSError:
                continue
            if mtime > stamp:
                return True
            break
    return False


def _snapshot_mtime(home: str, session: str | None) -> float | None:
    """mtime of ``snapshots/claude_code-<session>.json`` (the name ``statusline_snapshot.snapshot_path``
    gives it, same character rule), or None when there is none."""
    if not session:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)
    try:
        return os.path.getmtime(os.path.join(home, "snapshots", f"claude_code-{safe}.json"))
    except OSError:
        return None                               # no snapshot yet: the caller falls back to the cache stamp


def session_of(raw: bytes) -> str | None:
    """The ``session_id`` in the session JSON Claude Code pipes in, if it is a plain id."""
    m = _SESSION_RE.search(raw.decode("utf-8", "replace"))
    return m.group(1) if m else None


def maybe_refresh(home: str, cache: dict | None, now: float, session: str | None = None) -> bool:
    """Start one detached refresh when the cache is due, or when a ledger changed since it was
    written (at most one start per :data:`LEDGER_SPAWN_MIN_S` then). Never waits, never raises.

    With a ``session`` the refresher also writes that session's snapshot (``statusline --refresh
    --host claude_code --session S``), and a ledger change is judged against THAT snapshot's mtime
    (one more ``stat``): a snapshot-only run does not move the global cache's ``written_at``, so
    comparing with it re-spawned every 10 s until the next 60 s refresh (rv-632). With no snapshot
    yet, the cache stamp decides. Returns True when a refresher was started."""
    try:
        written = _num((cache or {}).get("written_at"))
        age_due = written is None or not 0 <= now - written < REFRESH_AFTER_S
        if not age_due and not ledger_changed_since(home, _snapshot_mtime(home, session) or written):
            return False
        stamp = os.path.join(home, REFRESH_STAMP_NAME)
        try:
            if now - os.path.getmtime(stamp) < (REFRESH_AFTER_S if age_due else LEDGER_SPAWN_MIN_S):
                return False  # one was started recently; it may still be running
        except OSError:
            pass
        argv = _refresh_argv()
        if not argv:
            return False
        if session and not os.environ.get("LLM_ROUTER_STATUSLINE_REFRESH_CMD", "").strip():
            argv += ["--host", "claude_code", "--session", session]
        os.makedirs(home, mode=0o700, exist_ok=True)
        fd = os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.utime(stamp, (now, now))
        _spawn_detached(argv)
        return True
    except Exception:  # noqa: BLE001 -- a status line never fails over a refresh
        return False


def _epoch(value) -> float | None:
    """Copy of ``quota_samples._epoch``: ``session_resets_at`` as epoch seconds."""
    if isinstance(value, str) and value:
        # Imported here, not at the top: ``datetime`` costs ~5-8 ms of start-up and
        # only the once-per-5-min history row reads an ISO reset time (P0.9-c).
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    v = _num(value)
    return v if v is not None and 0 < v < float("inf") else None


def quota_sample(usage: dict | None, now: float) -> dict:
    """``{five_hour_pct, weekly_pct, updated_at, five_hour_resets_at, source}`` from a usage.json dict.

    The tick cannot import ``llm_router``, so this is a copy of
    ``quota_samples.sample_from_usage``; tests/test_quota_samples.py pins both to
    the same answers. ``measured`` needs real numbers no older than
    :data:`QUOTA_STALE_AFTER_S`; anything else is ``stale``, unknown values null."""
    resets_at = _epoch(usage.get("session_resets_at")) if isinstance(usage, dict) else None
    if not isinstance(usage, dict) or usage.get("pending") or usage.get("is_fallback"):
        return {"five_hour_pct": None, "weekly_pct": None,
                "updated_at": _num(usage.get("updated_at")) if isinstance(usage, dict) else None,
                "five_hour_resets_at": resets_at, "source": "stale"}
    h5, wk = _num(usage.get("session_pct")), _num(usage.get("weekly_pct"))
    updated = _num(usage.get("updated_at"))
    fresh = updated is not None and updated > 0 and 0 <= now - updated <= QUOTA_STALE_AFTER_S
    measured = fresh and h5 is not None and wk is not None
    return {"five_hour_pct": h5, "weekly_pct": wk, "updated_at": updated,
            "five_hour_resets_at": resets_at,
            "source": "measured" if measured else "stale"}


def maybe_append_history(home: str, usage: dict | None, now: float) -> bool:
    """Append one ``quota_history.jsonl`` row when :data:`HISTORY_INTERVAL_S` has
    passed since the last one (a stamp file's mtime, one ``stat`` a tick).
    Append-only, 0600. Never raises; True when a row was written.

    Several Claude Code windows tick at once: the slot is claimed under a
    non-blocking ``flock`` on the stamp, and the stamp's content (the last row's
    ``ts``) is re-checked under it, so one window writes a slot and the rest skip."""
    try:
        stamp = os.path.join(home, HISTORY_STAMP_NAME)
        try:
            if 0 <= now - os.path.getmtime(stamp) < HISTORY_INTERVAL_S:
                return False
        except OSError:
            pass
        os.makedirs(home, mode=0o700, exist_ok=True)
        fd = os.open(stamp, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl  # only on this once-per-slot path; absent on Windows
            except ImportError:
                fcntl = None
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return False  # another window is writing this slot
            last = _num(_float_or_none(os.read(fd, 64)))
            # Both directions: a window whose ``now`` was read a moment before the
            # winner's sees a small negative gap, and that slot is still taken.
            if last is not None and abs(now - last) < HISTORY_INTERVAL_S:
                return False  # another window wrote this slot since our stat
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, repr(float(now)).encode("ascii"))
            os.utime(stamp, (now, now))
            row = {"ts": now}
            row.update(quota_sample(usage, now))
            hfd = os.open(os.path.join(home, HISTORY_NAME), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(hfd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                os.close(hfd)
            return True
        finally:
            os.close(fd)  # also releases the flock
    except Exception:  # noqa: BLE001 -- a status line never fails over its history
        return False


def prune_history(home: str, now: float, *, retain_s: float = HISTORY_RETAIN_S,
                  max_rows: int = HISTORY_MAX_ROWS) -> bool:
    """Keep only the rows of ``quota_history.jsonl`` newer than ``retain_s`` (and at most
    ``max_rows``, newest first); rewrite the file atomically. True when the file was rewritten.

    At most once a day (the mtime of :data:`HISTORY_PRUNE_STAMP_NAME`). Never call this from the
    synchronous tick: only the background refresher does. It takes the writer's slot lock
    (``flock`` on the slot stamp) so no append lands between the read and the replace; while it
    holds the lock the writers skip (the slot stamp is not advanced, so the next tick writes the
    row). If a writer holds the lock, the prune is skipped and retried on the next call. Lines that
    are not JSON objects with a numeric ``ts`` are dropped. Raises OSError on a filesystem failure:
    the caller records it (nothing is swallowed here)."""
    prune_stamp = os.path.join(home, HISTORY_PRUNE_STAMP_NAME)
    if os.path.exists(prune_stamp) and 0 <= now - os.path.getmtime(prune_stamp) < HISTORY_PRUNE_EVERY_S:
        return False
    hist = os.path.join(home, HISTORY_NAME)
    if not os.path.exists(hist):
        return False
    try:
        import fcntl
    except ImportError:
        return False                       # no flock (Windows): never rewrite under a live writer
    fd = os.open(os.path.join(home, HISTORY_STAMP_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False                   # a writer holds the slot: try again on the next call
        cutoff = now - retain_s
        kept: list[bytes] = []
        total = 0
        with open(hist, "rb") as fh:
            for ln in fh:
                total += 1
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                ts = _num(r.get("ts")) if isinstance(r, dict) else None
                if ts is not None and ts >= cutoff:
                    kept.append(ln if ln.endswith(b"\n") else ln + b"\n")
        kept = kept[-max_rows:]
        rewritten = len(kept) != total
        if rewritten:
            tmp = f"{hist}.{os.getpid()}.tmp"
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(tfd, "wb") as out:
                    out.writelines(kept)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, hist)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        pfd = os.open(prune_stamp, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(pfd)
        os.utime(prune_stamp, (now, now))
        return rewritten
    finally:
        os.close(fd)                       # also releases the flock


def _float_or_none(raw: bytes) -> float | None:
    try:
        return float(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None


def main() -> int:
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        pass
    now = time.time()
    home = router_home()
    cache = read_cache(home)
    maybe_refresh(home, cache, now, session_of(raw))
    usage = read_usage(home)
    maybe_append_history(home, usage, now)
    color = not os.environ.get("NO_COLOR")
    sys.stdout.write(render(cache, now=now, env_mode=os.environ.get("LLM_ROUTER_ENFORCE"), color=color,
                            usage=usage, usage_ttl_s=usage_ttl()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
