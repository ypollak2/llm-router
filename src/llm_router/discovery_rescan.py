"""Refresh discovery on first use, periodically, and when key names change."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from llm_router import model_discovery, paths, seats
from llm_router.lazy_urllib import urllib

DEFAULT_INTERVAL_S = 3600
KEY_CHANGE_MIN_GAP_S = 60
_SEAT_NAMES = ("claude", "codex", "gemini", "ollama", "copilot")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _interval(environ=None) -> float:
    raw = (os.environ.get("LLM_ROUTER_RESCAN_INTERVAL_S") if environ is None
           else environ.get("LLM_ROUTER_RESCAN_INTERVAL_S"))
    try:
        value = float(DEFAULT_INTERVAL_S if raw is None else raw)
        return max(300.0, value) if math.isfinite(value) else DEFAULT_INTERVAL_S
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


def probe_tags(timeout=2.0) -> list[dict] | None:
    """Return tags, distinguishing an empty inventory from an unreachable server."""
    try:
        req = urllib.request.Request(
            f"{model_discovery._ollama_url().rstrip('/')}/api/tags"
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            models = json.loads(response.read())["models"]
        if not isinstance(models, list) or any(
            not isinstance(m, dict) or not isinstance(m.get("name"), str)
            or not m["name"] or not isinstance(m.get("digest", ""), str)
            for m in models
        ):
            return None
        return models
    except Exception:  # Network, decoding and malformed responses all mean unavailable.
        return None


def ollama_fingerprint(models: list[dict]) -> str:
    lines = sorted(f"{m['name']}@{m.get('digest', '')}" for m in models)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def key_names_present(environ=None, env_files=None) -> list[str]:
    """Inspect presence only; do not retain or expose credential values."""
    env = os.environ if environ is None else environ

    def eligible(name):
        return name.endswith("_API_KEY") or name in seats._API_KEY_VARS

    names = {name for name in env if eligible(name) and env[name].strip()}
    files = (
        [paths.state_path(".env"), Path.home() / ".env"]
        if env_files is None else env_files
    )
    for candidate in files:
        path = paths.regular_file_or_none(candidate)
        if path is None:
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    match = _ENV_LINE.match(line)
                    del line
                    if match is None:
                        continue
                    name = match.group(1)
                    # Inspect the value once and discard the match immediately.
                    present = bool(match.group(2).strip().strip("\"'").strip())
                    del match
                    if present and eligible(name):
                        names.add(name)
        except (OSError, UnicodeError):
            continue
    if env_files is None:
        # `<provider>.key` files count too: discovery.keys reads them for the map.
        from llm_router.discovery.keys import _keyfile_values
        names.update(_keyfile_values())
    return sorted(names)


def _seat_state(value) -> dict:
    return {
        name: [getattr(value, name).kind, getattr(value, name).status]
        for name in _SEAT_NAMES
    }


def seat_fingerprint(value) -> str:
    payload = json.dumps(_seat_state(value), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class RescanResult:
    ran: bool = False
    changed: bool = False
    ollama_added: list[str] = field(default_factory=list)
    ollama_removed: list[str] = field(default_factory=list)
    ollama_modified: list[str] = field(default_factory=list)
    keys_added: list[str] = field(default_factory=list)
    keys_removed: list[str] = field(default_factory=list)
    seats_changed: list[str] = field(default_factory=list)
    ollama_reachable: bool = False
    first_scan: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _read_state() -> dict | None:
    try:
        state = json.loads(paths.state_path("discovery.rescan.json").read_text())
        if not isinstance(state, dict):
            return None
        checked = state["checked_at"]
        if not isinstance(checked, (int, float)) or not math.isfinite(checked):
            return None
        for name in ("ollama_fp", "seat_fp"):
            if state[name] is not None and not isinstance(state[name], str):
                return None
        keys = state["key_names"]
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            return None
        models = state["ollama_models"]
        if not isinstance(models, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in models.items()
        ):
            return None
        summary = state.get("seat_state")
        if summary is not None and (
            not isinstance(summary, dict)
            or set(summary) != set(_SEAT_NAMES)
            or any(
                not isinstance(v, list) or len(v) != 2
                or any(x is not None and not isinstance(x, str) for x in v)
                for v in summary.values()
            )
        ):
            return None
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


def due(now=None, *, environ=None) -> tuple[bool, str]:
    """Cheap scheduling check: no network or subprocess work."""
    state = _read_state()
    if state is None:
        return True, "first_use"
    now = time.time() if now is None else now
    if now - state["checked_at"] >= _interval(environ):
        return True, "interval"
    if key_names_present(environ) != sorted(state["key_names"]):
        return True, "keys_changed"
    return False, "not_due"


def _write_state(state: dict) -> None:
    path = paths.state_path("discovery.rescan.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def rescan(force=False, *, probe=probe_tags, detect=None, now=None,
           environ=None) -> RescanResult:
    now = time.time() if now is None else now
    if not force and not due(now, environ=environ)[0]:
        return RescanResult()
    previous = _read_state()
    old = previous or {}
    result = RescanResult(ran=True, first_scan=previous is None)
    tags = probe()
    result.ollama_reachable = tags is not None
    old_models = old.get("ollama_models", {})
    models = old_models
    fingerprint = old.get("ollama_fp")
    if tags is not None:
        models = {m["name"]: m.get("digest", "") for m in tags}
        fingerprint = ollama_fingerprint(tags)
        result.ollama_added = sorted(models.keys() - old_models.keys())
        result.ollama_removed = sorted(old_models.keys() - models.keys())
        result.ollama_modified = sorted(
            name for name in models.keys() & old_models.keys()
            if models[name] != old_models[name]
        )
        if fingerprint != old.get("ollama_fp") or not model_discovery._cache_path().is_file():
            # LC-12f: avoid a fresh cached_at trusting the old digest and missing remote flag after a digest change.
            model_discovery.remember_tags_rows(tags)
            model_discovery._write_cache([
                name for name in models if model_discovery._is_completion_model(name)
            ])

    raw = detect() if detect is not None else seats.detect_seats(timeout=2.0)
    cached_seats = seats.load_seats()
    # A process that cannot see a CLI must not undo another's recent "connected".
    # A carried result keeps seats.json's detected_at (the sighting), and is saved
    # only when another seat changed, so the carry ends CARRY_MAX_AGE_S after the
    # first sighting however often blind processes rescan.
    detected = seats.carry_for_save(raw, cached_seats, now)
    summary = _seat_state(detected)
    seat_fp = seat_fingerprint(detected)
    old_summary = old.get("seat_state")
    if old_summary is not None:
        result.seats_changed = sorted(
            name for name in _SEAT_NAMES if summary[name] != old_summary[name]
        )
    if detected is raw and (seat_fp != old.get("seat_fp") or cached_seats is None
            or cached_seats.is_stale(now)
            or seat_fingerprint(cached_seats) != seat_fp):
        seats.save_seats(detected)
    elif detected is not raw and seat_fingerprint(cached_seats) != seat_fp:
        # A seat was carried, but another one changed: persist the merged result
        # (it carries the OLD detected_at, so the carry still ends on time).
        seats.save_seats(detected)
    keys = key_names_present(environ)
    result.keys_added = sorted(set(keys) - set(old.get("key_names", [])))
    result.keys_removed = sorted(set(old.get("key_names", [])) - set(keys))
    result.changed = bool(
        result.ollama_added or result.ollama_removed or result.ollama_modified
        or result.keys_added or result.keys_removed or result.seats_changed
        or (previous is not None and seat_fp != old.get("seat_fp"))
    )
    _write_state({
        "checked_at": now, "ollama_fp": fingerprint, "ollama_models": models,
        "key_names": keys, "seat_fp": seat_fp, "seat_state": summary,
    })
    return result


def _claim_attempt(now: float, gap: float) -> bool:
    # A nonblocking advisory lock makes checking and updating one atomic claim.
    import fcntl

    path = paths.state_path("discovery.rescan_attempt")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="ascii") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            stream.seek(0)
            try:
                last = float(stream.read().strip())
            except ValueError:
                last = None
            if last is not None and math.isfinite(last) and now - last < gap:
                return False
            stream.seek(0)
            stream.truncate()
            stream.write(str(now))
            stream.flush()
            return True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def maybe_rescan_async() -> bool:
    """Start at most one detached refresh per retry window."""
    try:
        needed, reason = due()
        if not needed:
            return False
        flag = os.environ.get("LLM_ROUTER_NO_DETACHED_SPAWN", "")
        if flag and flag.lower() not in ("0", "off", "false", "no"):
            return False
        gap = (
            KEY_CHANGE_MIN_GAP_S if reason == "keys_changed"
            else max(60.0, _interval() / 2)
        )
        if not _claim_attempt(time.time(), gap):
            return False
        return model_discovery._spawn_probe_child(module="llm_router.discovery_rescan")
    except Exception as exc:
        from llm_router import failopen

        failopen.record("CHZ-FO-DISCOVERY-RESCAN-SPAWN", exc)
        return False


def rescan_if_due_blocking() -> RescanResult:
    return rescan()


async def rescan_loop(check_every_s: float = 900.0) -> None:
    """For long-running processes (MCP server, proxy): rescan when due, off the event loop.

    Wakes every ``check_every_s`` (default 15 min); ``rescan()`` itself returns at once
    unless the interval elapsed or a provider-key name changed, so a quiet machine pays a
    stat and a small JSON read per wake. Never raises; cancel it on shutdown.
    """
    import asyncio

    while True:
        try:
            await asyncio.to_thread(rescan_if_due_blocking)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from llm_router import failopen

            failopen.record("CHZ-FO-DISCOVERY-RESCAN-LOOP", exc)
        await asyncio.sleep(check_every_s)


def _main() -> None:
    try:
        rescan()
    except Exception as exc:
        from llm_router import failopen

        failopen.record("CHZ-FO-DISCOVERY-RESCAN", exc)


if __name__ == "__main__":
    _main()
