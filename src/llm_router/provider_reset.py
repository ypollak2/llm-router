"""Respect a provider's reported reset time.

A provider that says "usage limit reached, try again at 06:39 tomorrow" is not
having a 15-second blip. The rate-limit cooldown in ``health.py`` is 15s (or a
``Retry-After`` that only works inside one process), so the router kept
retrying an exhausted provider on every request. Observed 2026-10-03: Codex
(ChatGPT Plus) returned a usage-limit error with a reset the next morning.

This module does three small things:

1. ``parse_reset_epoch`` -- pull an absolute reset time out of an error message
   and/or response headers (ISO timestamp, "try again in 2h 15m", "try again at
   6:39 AM", ``Retry-After`` seconds or HTTP-date, Anthropic RFC3339
   ``anthropic-ratelimit-*-reset``).
2. ``record_provider_reset`` -- persist "provider X unavailable until T" under
   ``paths.state_path("provider_reset.json")`` so short-lived hook processes and
   the long-running MCP server see the same answer. (``provider_health.json`` is
   a write-only snapshot for ``doctor``; routing never reads it.)
3. ``get_provider_reset_until`` -- the read side, consulted by
   ``HealthTracker.is_healthy`` which is the single choke point for dispatch.

Rules: a reset in the past is expired; a reset further than 7 days away is
capped at 7 days; an unparseable message yields ``None`` and the caller keeps
its existing 15s cooldown; every read/write fails OPEN (a broken state file
never blocks routing) and is accounted via ``failopen.record``.

ASSUMPTION: the exact live Codex usage-limit wording was not capturable from
the repo or git history. The text patterns here are deliberately generic
("try again at/in ...", "resets at/in ...", any ISO timestamp) rather than a
copy of one string. If real wording differs, add a case to the tests.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from llm_router import failopen, paths
from llm_router.file_lock import exclusive_lock
from llm_router.logging import get_logger

log = get_logger("llm_router.provider_reset")

#: Longest we will ever skip a provider, however far away it claims the reset is.
MAX_SKIP_SECONDS = 7 * 24 * 3600

#: Resets nearer than this are left to the existing in-process rate-limit
#: cooldown (15s / Retry-After). Persisting a 20-second blip to disk would add
#: I/O to every dispatch for no benefit.
MIN_PERSIST_SECONDS = 60

_ANTHROPIC_RESET_HEADERS = (
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-reset",
)

#: A bare ISO timestamp (and only an ISO timestamp) appears anywhere in a log
#: line, a correlation id, or an unrelated stack trace -- far more often than
#: a real reset time. Both anchors below require the timestamp to directly
#: follow a reset-ish verb ("try again at/on ...", "resets on ...", "reset:
#: ..."), and the whole-message gate in `parse_reset_epoch` additionally
#: requires a limit/quota/rate word to be present at all, so an ordinary
#: "connection refused, please retry" with an incidental timestamp elsewhere
#: in the line does not get parsed as a reset.
_ISO_RE = re.compile(
    r"(?:try again|retry|resets?|reset)\s*(?:tomorrow\s+)?(?:at|on|:)\s*"
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)

#: Message must look like a limit/quota/rate-limit report at all before any
#: message-text parsing is attempted (headers bypass this -- they are
#: authoritative regardless of wording).
_LIMIT_SIGNAL_RE = re.compile(
    r"usage limit|rate.?limit|too many requests|quota|exhausted|\b429\b",
    re.IGNORECASE,
)

_REL_RE = re.compile(
    r"(?:try again|retry|resets?)\s+in\s+(?:about\s+|approximately\s+)?"
    r"(?:(?P<d>\d+)\s*d(?:ays?)?\b\s*)?"
    r"(?:(?P<h>\d+)\s*h(?:ours?|rs?)?\b\s*)?"
    r"(?:(?P<m>\d+)\s*m(?:in(?:ute)?s?)?\b\s*)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b)?",
    re.IGNORECASE,
)

_CLOCK_RE = re.compile(
    r"(?:try again|retry|resets?)\s+(?:tomorrow\s+)?at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?",
    re.IGNORECASE,
)


def _state_file() -> Path:
    """Where the per-provider unavailable-until record lives (read at call time)."""
    override = os.environ.get("LLM_ROUTER_PROVIDER_RESET_PATH", "").strip()
    if override:
        return Path(override)
    return paths.state_path("provider_reset.json")


# --------------------------------------------------------------------------- parse


def _from_headers(headers: Mapping[str, str], now: float) -> float | None:
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for key in _ANTHROPIC_RESET_HEADERS:
        raw = lowered.get(key)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    raw = lowered.get("retry-after")
    if raw:
        try:
            return now + float(raw)
        except (TypeError, ValueError):
            try:
                return email.utils.parsedate_to_datetime(str(raw)).timestamp()
            except (TypeError, ValueError, IndexError):
                return None
    return None


def _from_iso_text(text: str) -> float | None:
    m = _ISO_RE.search(text)
    if not m:
        return None
    try:
        # Aware stamps use their own offset; naive ones are read as local time,
        # which is how a CLI prints a reset to the person at the keyboard.
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _from_relative_text(text: str, now: float) -> float | None:
    m = _REL_RE.search(text)
    if not m:
        return None
    d, h, mi, s = (m.group(k) for k in ("d", "h", "m", "s"))
    if not (d or h or mi or s):
        return None
    delta = (
        float(d or 0) * 86400 + float(h or 0) * 3600 + float(mi or 0) * 60 + float(s or 0)
    )
    return now + delta if delta > 0 else None


def _from_clock_text(text: str, now: float) -> float | None:
    m = _CLOCK_RE.search(text)
    if not m:
        return None
    hour, minute = int(m.group("hour")), int(m.group("minute"))
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    candidate = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)  # a bare clock time means its next occurrence
    return candidate.timestamp()


def parse_reset_epoch(
    message: str | None,
    headers: Mapping[str, str] | None = None,
    now: float | None = None,
) -> float | None:
    """Epoch seconds at which the provider says it is usable again, or ``None``.

    Headers win over message text. A reset already in the past yields ``None``
    (expired); one beyond 7 days is capped at 7 days.
    """
    now = time.time() if now is None else now
    epoch: float | None = None
    if headers:
        epoch = _from_headers(headers, now)
    if epoch is None and message:
        text = str(message)
        # Headers are authoritative regardless of wording; message text is
        # not, so require it to actually look like a limit/quota report
        # before trying to extract a time from it at all (CHZ-RESET-R-01 —
        # otherwise an ordinary error whose text happens to mention a time
        # gets misread as a reset).
        if _LIMIT_SIGNAL_RE.search(text):
            epoch = (
                _from_iso_text(text)
                or _from_relative_text(text, now)
                or _from_clock_text(text, now)
            )
    if epoch is None or epoch <= now:
        return None
    return min(epoch, now + MAX_SKIP_SECONDS)


# ------------------------------------------------------------------------ persist


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    providers = data.get("providers") if isinstance(data, dict) else None
    return providers if isinstance(providers, dict) else {}


def record_provider_reset(
    provider: str,
    until_epoch: float,
    reason: str = "",
    now: float | None = None,
) -> bool:
    """Persist "``provider`` unavailable until ``until_epoch``". Never raises.

    Returns True when the record was written. Resets nearer than
    ``MIN_PERSIST_SECONDS`` are not persisted (the in-process cooldown covers
    them) and return False without it being a failure.
    """
    now = time.time() if now is None else now
    until_epoch = min(until_epoch, now + MAX_SKIP_SECONDS)
    if until_epoch - now < MIN_PERSIST_SECONDS:
        return False
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Two processes recording different providers at once must not clobber
        # each other's write with a stale read (CHZ-RESET-R-02): the read and
        # the replace both happen inside the same cross-process exclusive lock
        # that session_store.py uses for its own append/compact race.
        with exclusive_lock(path.with_suffix(path.suffix + ".lock")):
            try:
                providers = _load(path) if path.exists() else {}
            except (OSError, ValueError):
                providers = {}  # corrupt file: replace it rather than fail again
            providers[provider] = {"until": until_epoch, "set_at": now, "reason": reason[:200]}
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"providers": providers}), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 -- must never break routing
        failopen.record("CHZ-FO-PROVIDER-RESET-WRITE", exc, detail=provider)
        return False
    log.warning(
        "provider_unavailable_until",
        provider=provider,
        until=time.strftime("%Y-%m-%d %H:%M", time.localtime(until_epoch)),
        reason=reason[:120],
    )
    return True


def all_provider_resets(now: float | None = None) -> dict[str, float]:
    """Providers currently blocked -> reset epoch. Fails open to ``{}``."""
    now = time.time() if now is None else now
    try:
        path = _state_file()
        if not path.exists():
            return {}
        out: dict[str, float] = {}
        for name, entry in _load(path).items():
            until = entry.get("until") if isinstance(entry, dict) else None
            if isinstance(until, (int, float)) and now < until <= now + MAX_SKIP_SECONDS:
                out[str(name)] = float(until)
        return out
    except Exception as exc:  # noqa: BLE001 -- corrupt/unreadable state => not blocked
        failopen.record("CHZ-FO-PROVIDER-RESET-READ", exc)
        return {}


def get_provider_reset_until(provider: str, now: float | None = None) -> float | None:
    """Epoch when ``provider`` is usable again, or ``None`` if not blocked."""
    return all_provider_resets(now).get(provider)


def is_provider_reset_blocked(provider: str, now: float | None = None) -> bool:
    return get_provider_reset_until(provider, now) is not None


def note_provider_error(
    provider: str, exc: BaseException, headers: Mapping[str, str] | None = None
) -> float | None:
    """Parse ``exc``/``headers`` for a reset and persist it. Never raises.

    Returns the persisted reset epoch, or ``None`` when nothing actionable was
    found (the caller then keeps its existing 15s cooldown).
    """
    try:
        until = parse_reset_epoch(str(exc), headers)
        if until is None:
            return None
        if record_provider_reset(provider, until, reason=str(exc)):
            return until
    except Exception as err:  # noqa: BLE001
        failopen.record("CHZ-FO-PROVIDER-RESET-PARSE", err, detail=provider)
    return None
