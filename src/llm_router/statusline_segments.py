"""Off-the-hot-path computation of the FULL status line's segments (PLAN v16 P0.9-c).

``hooks/statusline-command.sh`` is the status line Claude Code runs about once a
second, and its bar is p95 <= 100 ms (NFR-LAT, R-VAL-1). Measured on 2026-10-09
(30 runs, 11 MB transcript, load1 3-4) it spent ~540 ms in sixteen sequential
subprocesses: twelve ``python3 -c`` one-liners (~20 ms start-up each), a scan of
the whole transcript for the context figure (~65 ms and growing with the
session), an ``import llm_router`` plus the ``dashboard_data`` union for the
money figure (~170 ms), a ``sqlite3`` process, an interpreter probe loop, and a
ledger tail for the last route. Nothing in them needs to be fresh to the second.

This file does all of that work in ONE process, started detached by the script
when its cache is older than :data:`CACHE_TTL_S` (or, for the context figure, when
the transcript is newer than the cache), and writes a small flat ``key=value``
file the script reads with the shell's own ``read`` (no process, no JSON, no
``eval``). The script's hot path is then: parse the session JSON with bash regexes,
read that file, print.

STDLIB ONLY at import time; ``llm_router`` is imported lazily for the one figure
that needs it (money) and its absence just omits that figure, as before.

CACHE FILE ``<state>/statusline_seg_<session>.kv``, one ``key=value`` per line,
values stripped of control characters (a value is data to print, never to run)::

    v=1  written=<epoch s>
    usage=none|fallback|ok  session_pct  weekly_pct  usage_stale=0|1  reset
    ctx_human  ctx_pct
    money  mix_local  mix_paid
    proxy_down
    health=ok|degraded|idle|down
    last  last_stale=0|1  last_tok

A key that is absent or empty is a segment that is not shown, exactly as before;
nothing is ever written as 0 for "unknown". STALENESS: the script compares
``written`` with the clock and, past :data:`STALE_MARK_S`, appends a visible
``cached <age>`` marker to the line, so a refresher that has died cannot pass its
last answer off as live.

    python3 llm_router_statusline_segments.py --state DIR --session ID \\
        [--transcript PATH] [--model ID]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import sys
import time

SCHEMA = 1
#: Seconds before the script starts a refresh; also the staleness unit below.
CACHE_TTL_S = 30
#: A cache older than this is rendered with an explicit "cached <age>" marker.
STALE_MARK_S = 90
#: Hard stop for a stuck refresh (SIGALRM): the lock is freed, the line keeps drawing.
RUN_TIMEOUT_S = 30
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: object) -> str:
    """One line, no control characters: a cache value can never carry an escape or a newline."""
    return _CTRL.sub(" ", str(value)).strip()


# ── segments (each a port of the script's former inline ``python3 -c``) ──────


def _age_s(now: float, stamp: object) -> float:
    """Seconds since ``stamp``; a missing or unreadable stamp is 99999 (unknown, so stale)."""
    try:
        return now - float(stamp) if stamp is not None else 99999.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 99999.0


def _refresh_script() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "hooks", "llm_router-usage-refresh.py")


def usage_segment(state: str, now: float, env: dict[str, str]) -> dict[str, str]:
    path = os.path.join(state, "usage.json")
    if not os.path.isfile(path):
        return {"usage": "none"}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError
    except Exception:  # noqa: BLE001 -- unreadable is not measured either
        return {"usage": "fallback"}
    # is_fallback: a snapshot session-start wrote when the OAuth fetch FAILED
    # (three invented 50s). Absence of the key means measured.
    if data.get("is_fallback"):
        return {"usage": "fallback"}
    out = {"usage": "ok"}
    try:
        out["session_pct"] = "%.0f" % float(data.get("session_pct", 0))
        out["weekly_pct"] = "%.0f" % float(data.get("weekly_pct", 0))
    except (TypeError, ValueError):
        return {"usage": "fallback"}
    try:
        ttl = float(env.get("LLM_ROUTER_USAGE_TTL_SEC", "300"))
    except ValueError:
        ttl = 300.0
    # An absent or unreadable updated_at is "age unknown", which reads as stale (never fresh).
    age = _age_s(now, data.get("updated_at"))
    # Byte-for-byte with the shell it replaces (test_statusline_default_full): the
    # old script computed the age only when the refresh script exists, so the
    # degree sign never rendered without it. Kept; changing it is its own change.
    out["usage_stale"] = "1" if (age > ttl and os.access(_refresh_script(), os.X_OK)) else "0"
    out["usage_age"] = str(int(age))
    try:
        import datetime

        raw = str(data.get("session_resets_at", "") or "")
        if raw:
            dt = datetime.datetime.fromisoformat((raw[:-1] + "+00:00" if raw.endswith("Z") else raw)).astimezone()
            if dt >= datetime.datetime.now(datetime.timezone.utc).astimezone():
                out["reset"] = dt.strftime("%-I:%M%p").lower()
    except Exception:  # noqa: BLE001
        pass
    return out


def maybe_refresh_usage(state: str, now: float, env: dict[str, str]) -> None:
    """Start the quota refresher (detached) when usage.json is older than the TTL.

    Throttled by a timestamp file, not flock (absent on macOS): at most one launch
    per ``LLM_ROUTER_REFRESH_THROTTLE_SEC`` (default 60 s). Moved here from the
    script so the hot path never reads usage.json's age.
    """
    script = _refresh_script()
    path = os.path.join(state, "usage.json")
    if not (os.path.isfile(path) and os.access(script, os.X_OK)):
        return
    try:
        ttl = float(env.get("LLM_ROUTER_USAGE_TTL_SEC", "300"))
        throttle = float(env.get("LLM_ROUTER_REFRESH_THROTTLE_SEC", "60"))
    except ValueError:
        ttl, throttle = 300.0, 60.0
    try:
        with open(path, encoding="utf-8") as fh:
            age = _age_s(now, json.load(fh).get("updated_at"))
    except Exception:  # noqa: BLE001
        age = 99999.0
    if age <= ttl:
        return
    last_try = os.path.join(state, ".usage-refresh.last")
    try:
        if now - os.path.getmtime(last_try) < throttle:
            return
    except OSError:
        pass
    try:
        open(last_try, "w").close()
        _spawn_detached(script)
    except Exception:  # noqa: BLE001 -- a refresh failure never reaches the line
        pass


def _spawn_detached(script: str) -> None:
    """fork + setsid + execv, the POSIX path of ``statusline_tick._spawn_detached``:
    no new ``subprocess`` site (tests/test_r4_subprocess_env_allowlist counts them)."""
    if not hasattr(os, "fork"):
        return
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)  # the first child exits at once; the grandchild runs on, parentless
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.execv(script, [script])
    finally:
        os._exit(127)


def context_segment(transcript: str, model_id: str, env: dict[str, str]) -> dict[str, str]:
    """Tokens of the LAST message with a ``usage`` block, read from the tail.

    The old one-liner parsed the whole file on every render (cost grows with the
    session: 65 ms at 11 MB, hundreds of ms at 50 MB). The answer is the last
    qualifying line, so reading backwards from the end gives the same value; the
    window doubles from 256 KiB until a qualifying line is found or the file is
    covered.
    """
    if not transcript or not os.path.isfile(transcript):
        return {}
    total = _last_usage_tokens(transcript)
    if not total:
        return {}
    try:
        limit = int(env.get("CC_CONTEXT_LIMIT", "200000"))
    except ValueError:
        limit = 200000
    if "[1m]" in model_id or "1m" in model_id:
        limit = 1_000_000
    pct = min(100, total * 100 // limit) if limit > 0 else 0
    if total >= 1_000_000:
        human = "%.1fM" % (total / 1_000_000)
    elif total >= 1_000:
        human = "%.1fk" % (total / 1_000)
    else:
        human = str(total)
    return {"ctx_human": human, "ctx_pct": str(pct)}


def _tokens_of(line: bytes) -> int:
    try:
        d = json.loads(line)
    except Exception:  # noqa: BLE001
        return 0
    msg = d.get("message") if isinstance(d, dict) else None
    u = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(u, dict):
        return 0
    try:
        return int(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                   + u.get("cache_read_input_tokens", 0))
    except (TypeError, ValueError):
        return 0


def _last_usage_tokens(path: str) -> int:
    try:
        size = os.path.getsize(path)
        window = 256 * 1024
        with open(path, "rb") as fh:
            while True:
                start = max(0, size - window)
                fh.seek(start)
                blob = fh.read(size - start)
                lines = blob.split(b"\n")
                if start > 0:
                    lines = lines[1:]  # first line is cut mid-way
                for line in reversed(lines):
                    if line.strip():
                        t = _tokens_of(line)
                        if t > 0:
                            return t
                if start == 0:
                    return 0
                window *= 2
    except OSError:
        return 0


def money_segment(state: str) -> str:
    db = os.path.join(state, "usage.db")
    if not os.path.isfile(db):
        return ""
    try:
        import pathlib

        from llm_router.dashboard_data import SPEND_FLOOR_USD, summary

        s = summary("today", db_path=pathlib.Path(db))
        return s.compact() + "." if s.estimated_usd >= SPEND_FLOOR_USD else ""
    except Exception:  # noqa: BLE001 -- never break the line over a reporting figure
        return ""


def mix_segment(state: str) -> dict[str, str]:
    """Local vs paid calls over the last 6 h (the former ``sqlite3`` process)."""
    db = os.path.join(state, "usage.db")
    if not os.path.isfile(db):
        return {}
    try:
        import sqlite3

        con = sqlite3.connect(db, timeout=2)
        try:
            row = con.execute(
                "SELECT SUM(CASE WHEN model LIKE 'ollama/%' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN model NOT LIKE 'ollama/%' THEN 1 ELSE 0 END)"
                " FROM usage WHERE timestamp >= datetime('now', '-6 hours')"
                " AND COALESCE(provider, '') != 'cache'").fetchone()  # cache hits are not calls (provider_classes)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return {}
    local, paid = (row[0] or 0), (row[1] or 0)
    if local + paid <= 0:
        return {}
    return {"mix_local": str(local), "mix_paid": str(paid)}


def proxy_segment(state: str) -> dict[str, str]:
    sentinel = os.path.join(state, "proxy_default.json")
    if not os.path.isfile(sentinel):
        return {}
    try:
        with open(sentinel, encoding="utf-8") as fh:
            d = json.load(fh)
        port = int(d.get("port", 8787))
        up = d.get("upstream_port")
        up = int(up) if up is not None else None
    except Exception:  # noqa: BLE001 -- unreadable sentinel is not evidence of a dead proxy
        return {}
    import socket

    def answers(p: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.3):
                return True
        except OSError:
            return False

    if not answers(port):
        return {"proxy_down": str(port)}
    if up is not None and not answers(up):
        return {"proxy_down": f"{up} (bypassed)"}
    return {}


def health_segment(state: str, now: float, env: dict[str, str]) -> str:
    """ok / degraded / idle / down -- the former inline health one-liner, unchanged."""
    savings = os.path.join(state, "savings_log.jsonl")
    keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY")
    subscription_on = env.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("1", "true", "yes")
    providers = any(env.get(k) for k in keys) or subscription_on
    ollama_recent = False
    try:
        from datetime import datetime

        with open(savings, encoding="utf-8") as fh:
            lines = fh.readlines()[-200:]
        for line in reversed(lines):
            r = json.loads(line)
            m = r.get("model", "")
            if isinstance(m, str) and m.startswith("ollama/"):
                if now - datetime.fromisoformat(r["timestamp"]).timestamp() <= 1800:
                    ollama_recent = True
                    break
    except Exception:  # noqa: BLE001
        pass
    stale, fallback = True, False
    try:
        p = os.path.join(state, "usage.json")
        with open(p, encoding="utf-8") as fh:
            u = json.load(fh)
        fallback = bool(u.get("is_fallback"))
        ts = u.get("updated_at")
        age = (now - float(ts)) if ts else (now - os.path.getmtime(p))
        stale = age > 1800
    except Exception:  # noqa: BLE001
        pass
    if providers or ollama_recent:
        return "degraded" if (stale or fallback) else "ok"
    reachable = False
    try:
        import urllib.request

        url = env.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/tags"
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=0.3):
            reachable = True
    except Exception:  # noqa: BLE001
        reachable = False
    return "idle" if reachable else "down"


def last_route_segment(state: str, now: float) -> dict[str, str]:
    out: dict[str, str] = {}
    files = glob.glob(os.path.join(state, "last_route_*.json"))
    if files:
        try:
            newest = max(files, key=os.path.getmtime)
            with open(newest, encoding="utf-8") as fh:
                d = json.load(fh)
            tool = str(d.get("tool", "?")).replace("llm_", "")
            task = d.get("task_type", tool)
            out["last"] = (task + ">" + tool) if task != tool else tool
            out["last_stale"] = "1" if _age_s(now, d.get("saved_at")) >= 300 else "0"
        except Exception:  # noqa: BLE001
            out.pop("last", None)
    if out.get("last"):
        try:
            with open(os.path.join(state, "savings_log.jsonl"), encoding="utf-8") as fh:
                lines = fh.readlines()[-200:]
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                n = sum(v for v in (d.get("input_tokens"), d.get("output_tokens"))
                        if isinstance(v, (int, float)))
                if n > 0:
                    out["last_tok"] = ("%.1fk tok" % (n / 1000)) if n >= 1000 else ("%d tok" % n)
                break
        except Exception:  # noqa: BLE001
            pass
    return out


# ── assembly ─────────────────────────────────────────────────────────────────


def compute(state: str, transcript: str, model_id: str, env: dict[str, str] | None = None,
            now: float | None = None) -> dict[str, str]:
    env = dict(os.environ) if env is None else env
    now = time.time() if now is None else now
    seg: dict[str, str] = {"v": str(SCHEMA)}
    parts = (
        lambda: usage_segment(state, now, env),
        lambda: context_segment(transcript, model_id, env),
        lambda: {"money": money_segment(state)},
        lambda: mix_segment(state),
        lambda: proxy_segment(state),
        lambda: {"health": health_segment(state, now, env)},
        lambda: last_route_segment(state, now),
    )
    for part in parts:
        try:
            seg.update(part())
        except Exception:  # noqa: BLE001 -- a part that fails is simply absent (unknown), never 0
            continue
    seg["written"] = str(int(time.time()))
    return {k: _clean(v) for k, v in seg.items()}


def cache_file(state: str, session: str) -> str:
    return os.path.join(state, f"statusline_seg_{session}.kv")


def write_cache(state: str, session: str, seg: dict[str, str]) -> str:
    path = cache_file(state, session)
    tmp = f"{path}.{os.getpid()}.tmp"
    body = "".join(f"{k}={v}\n" for k, v in seg.items() if v != "")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, path)
    return path


def _prune(state: str, keep_s: float = 2 * 86400) -> None:
    """Drop session caches nobody has read for two days (one file per session)."""
    cutoff = time.time() - keep_s
    names = ("statusline_seg_*.kv", ".statusline-seg-*.lock", ".statusline_seg_spawn_*",
             ".statusline_seg_sync_*")
    for p in (q for pat in names for q in glob.glob(os.path.join(state, pat))):
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            continue  # raced with another refresher, or not ours to remove: leave it


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", required=True)
    ap.add_argument("--session", default="default")
    ap.add_argument("--transcript", default="")
    ap.add_argument("--model", default="")
    ns = ap.parse_args(argv)
    session = re.sub(r"[^A-Za-z0-9._-]", "_", ns.session) or "default"
    state = ns.state
    os.makedirs(state, exist_ok=True)
    # One refresher per session at a time; a second exits at once.
    try:
        import fcntl

        lock = open(os.path.join(state, f".statusline-seg-{session}.lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except OSError:
        return 0
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(1))
        signal.alarm(RUN_TIMEOUT_S)
    now = time.time()
    maybe_refresh_usage(state, now, dict(os.environ))
    write_cache(state, session, compute(state, ns.transcript, ns.model, dict(os.environ), now))
    _prune(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
