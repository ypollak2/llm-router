"""LC-8: the per-(model, job) routing switch for passed local-check lanes, and the demotion monitor.

``local_routing.json`` (under the llm-router home) is the ONLY writer-owned state here::

    {"<model>": {"<job>": {"enabled": bool, "since": iso8601, "source": "local_check"|"prereg"|"manual",
                           "reason": str (only when disabled), "disabled_at": iso8601 (only when disabled)}}}

This is the shape ``local_check.display.routing_state`` reads (it accepts a bool or ``{"enabled": bool}``;
this module always writes the ``{"enabled": ...}`` form).

D-260: a recorded local-check PASS enables its lane by default (:func:`on_pass`). D-258: a pre-registered
lane (the owner's Q37) is record-only and is NEVER enabled automatically; only an explicit
:func:`enable` does it. A lane that was disabled (by the user, or by the demotion monitor) is never
re-enabled by :func:`on_pass`; only an explicit :func:`enable` flips it back.

Read-only on the routing path: :func:`is_enabled` and :func:`demotion_check` read local files only. No
network, no proxy modules, no authority.py (D-258: the owner's proxy authority stays off).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Iterable

from llm_router import paths
from llm_router.file_lock import exclusive_lock

ROUTING_FILE = "local_routing.json"
SOURCES = ("local_check", "prereg", "manual")
#: Demotion: more than this share of the last WINDOW calls for a (model, job) failed acceptance.
DEMOTE_RATE = 0.30
DEMOTE_WINDOW = 20
DEMOTE_REASON = "demoted: fallbacks"
_LEDGER_TAIL_BYTES = 512 * 1024


def routing_path() -> Path:
    return paths.state_path(ROUTING_FILE)


def norm_model(name: str) -> str:
    """``ollama/qwen3:8b`` -> ``qwen3:8b``; an untagged name gets ``:latest`` (as the F106 readers do)."""
    head, sep, rest = name.partition("/")
    if sep and head in ("ollama", "ollama_chat"):
        name = rest
    return name if ":" in name else f"{name}:latest"


def candidates(name: str) -> list[str]:
    """Spellings of one local model that may be the stored key, most specific first."""
    bare = name.partition("/")[2] if name.partition("/")[0] in ("ollama", "ollama_chat") and "/" in name else name
    out = [bare]
    if bare.endswith(":latest"):
        out.append(bare[: -len(":latest")])
    elif ":" not in bare:
        out.append(bare + ":latest")
    return out


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _epoch(iso: object) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        import calendar
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _read(path: Path | None = None) -> dict:
    try:
        data = json.loads(Path(path or routing_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path | None = None) -> None:
    target = Path(path or routing_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    # LC-8b: unique per process AND thread AND call, so two writers never share a temp file.
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _mutate(fn, path: Path | None = None):
    """LC-8b: read-modify-write ``local_routing.json`` under the shared advisory file lock.

    ``fn(data)`` edits ``data`` in place and returns the caller's result; the file is rewritten only
    when ``data`` changed. Raises ``OSError`` when the lock cannot be taken (never writes unlocked).
    """
    target = Path(path or routing_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)
    with exclusive_lock(lock_path) as locked:
        if not locked:
            raise OSError("local_routing lock unavailable")
        data = _read(target)
        before = json.dumps(data, sort_keys=True)
        result = fn(data)
        if json.dumps(data, sort_keys=True) != before:
            _write(data, target)
        return result


def entry(model: str, job: str, *, path: Path | None = None) -> dict | None:
    """The stored entry for (model, job) under any spelling of the model name, else None."""
    data = _read(path)
    for key in candidates(model):
        jobs = data.get(key)
        e = jobs.get(job) if isinstance(jobs, dict) else None
        if isinstance(e, bool):
            return {"enabled": e}
        if isinstance(e, dict):
            return e
    return None


def is_enabled(model: str, job: str, *, path: Path | None = None) -> bool:
    """True only when an entry exists and says enabled. Missing file / entry / bad shape -> False."""
    e = entry(model, job, path=path)
    return bool(e) and e.get("enabled") is True


def _store_key(data: dict, model: str) -> str:
    """The key an existing entry lives under, else the tagged inventory spelling (``x`` -> ``x:latest``)."""
    for key in candidates(model):
        if key in data:
            return key
    return norm_model(candidates(model)[0])


def _enable_in(data: dict, model: str, job: str, source: str) -> dict:
    key = _store_key(data, model)
    jobs = data.get(key) if isinstance(data.get(key), dict) else {}
    e = {"enabled": True, "since": _iso(), "source": source}
    jobs[job] = e
    data[key] = jobs
    return dict(e)


def _entry_in(data: dict, model: str, job: str) -> dict | None:
    for key in candidates(model):
        jobs = data.get(key)
        e = jobs.get(job) if isinstance(jobs, dict) else None
        if isinstance(e, bool):
            return {"enabled": e}
        if isinstance(e, dict):
            return e
    return None


def enable(model: str, job: str, source: str = "manual", *, path: Path | None = None) -> dict:
    """Explicitly enable routing for (model, job); clears any disable reason. Returns the stored entry.

    Pass the inventory name (with its tag); a name without one is stored as ``<name>:latest``.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    return _mutate(lambda data: _enable_in(data, model, job, source), path)


def disable(model: str, job: str, reason: str = "manual", *, path: Path | None = None) -> dict:
    """Disable routing for (model, job); keeps ``since``/``source`` for history. Returns the stored entry."""
    def _do(data: dict) -> dict:
        key = _store_key(data, model)
        jobs = data.get(key) if isinstance(data.get(key), dict) else {}
        old = jobs.get(job)
        old = old if isinstance(old, dict) else {}
        e = {"enabled": False, "since": old.get("since") or _iso(), "source": old.get("source") or "manual",
             "reason": str(reason)[:200], "disabled_at": _iso()}
        jobs[job] = e
        data[key] = jobs
        return dict(e)
    return _mutate(_do, path)


def on_pass(model: str, jobs: Iterable[str], *, path: Path | None = None) -> dict[str, str]:
    """D-260: a local-check PASS was just recorded for ``jobs``. Enable each lane by default.

    Returns ``{job: "enabled" | "already_on" | "kept_off"}``. ``kept_off`` means the lane was disabled
    earlier (by the user or by the demotion monitor) and a new pass does NOT turn it back on; the caller
    should tell the user to run ``llm-router local-check enable <lane>``. Only call this for lanes of a
    local-check run that passed (never for prereg-only lanes, D-258). One locked read-modify-write.
    """
    wanted = list(jobs)

    def _do(data: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for job in wanted:
            e = _entry_in(data, model, job)
            if e is None:
                _enable_in(data, model, job, "local_check")
                out[job] = "enabled"
            elif e.get("enabled") is True:
                out[job] = "already_on"
            else:
                out[job] = "kept_off"
        return out
    return _mutate(_do, path)


def demoted_entries(*, path: Path | None = None) -> list[tuple[str, str, dict]]:
    """(model, job, entry) for every lane the demotion monitor switched off."""
    out = []
    for model, jobs in sorted(_read(path).items()):
        if not isinstance(jobs, dict):
            continue
        for job, e in sorted(jobs.items()):
            if isinstance(e, dict) and e.get("enabled") is False and str(e.get("reason", "")).startswith("demoted"):
                out.append((model, job, e))
    return out


def doctor_lines(*, path: Path | None = None) -> list[str]:
    """One line per demoted lane, then one per passed lane the route-time settings keep from routing (LC-8b)."""
    try:
        from llm_router import eligibility_routing
        notices = eligibility_routing.settings_notices(routing=path)
    except Exception:  # noqa: BLE001
        notices = []
    return notices + [f"local-check: {m} — {j} routing demoted ({e.get('disabled_at', '?')}): more than "
            f"{int(DEMOTE_RATE * 100)}% of the last {DEMOTE_WINDOW} calls failed the acceptance check; "
            f"re-enable only with `llm-router local-check enable {j}`" for m, j, e in demoted_entries(path=path)]


def _recent_outcomes(model: str, job: str, since: float | None, ledger: Path | None) -> list[str]:
    """Outcomes of the last DEMOTE_WINDOW LOCAL attempts for (model, job) at or after ``since``.

    LC-8b: read from the per-attempt local sidecar (``text_job_ledger.record_local_attempts``), not the
    final-row ledger, so a local failure rescued by a later chain entry still counts.
    """
    from llm_router import text_job_ledger as L
    target = Path(ledger or paths.state_path(L.LOCAL_ATTEMPTS_FILENAME))
    want = norm_model(model)
    rows: deque[str] = deque(maxlen=DEMOTE_WINDOW)
    # Rotation can leave only a few rows (or no live file) in the new generation.
    for candidate in (target.with_name(target.name + ".1"), target):
        try:
            with open(candidate, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _LEDGER_TAIL_BYTES))
                raw = fh.read(_LEDGER_TAIL_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue  # includes the partial first line of a tail read
            if not isinstance(r, dict) or r.get("job") != job or not isinstance(r.get("model"), str):
                continue
            if norm_model(r["model"]) != want:
                continue
            ts = r.get("ts")
            if since is not None and isinstance(ts, (int, float)) and ts < since:
                continue
            rows.append(str(r.get("outcome")))
    return list(rows)


def demotion_check(model: str, job: str, *, path: Path | None = None, ledger: Path | None = None) -> bool:
    """Disable (model, job) when more than 30% of its last 20 LOCAL attempts since enabling failed.

    A failed attempt is a rejection by the acceptance check or a local error/timeout that a later chain
    entry rescued (LC-8b). Needs a full window of 20 attempts (so 7 rejected of 20 demotes, 6 does not). Only an enabled lane is
    considered. Never re-enables. Returns True when it demoted the lane. Best-effort: never raises.
    """
    try:
        e = entry(model, job, path=path)
        if not e or e.get("enabled") is not True:
            return False
        recent = _recent_outcomes(model, job, _epoch(e.get("since")), ledger)
        if len(recent) < DEMOTE_WINDOW:
            return False
        bad = sum(1 for s in recent if s != "ok")
        if bad / DEMOTE_WINDOW > DEMOTE_RATE:
            disable(model, job, DEMOTE_REASON, path=path)
            return True
    except Exception:  # noqa: BLE001 -- a monitor failure must never break the text job
        return False
    return False
