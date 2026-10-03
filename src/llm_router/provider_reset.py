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
   ``HealthTracker.is_healthy`` (router.py) and by ``execute_chain`` in
   ``hooks/direct_executor.py``, which dispatches gemini/openai/ollama without
   going through HealthTracker, so it checks this module directly.

Rules: a reset in the past is expired; a reset further than 7 days away is
capped at 7 days; an unparseable message yields ``None`` and the caller keeps
its existing 15s cooldown; every read/write fails OPEN (a broken state file
never blocks routing) and is accounted via ``failopen.record``.

How far a reset is trusted depends on where it came from (``source``):

* ``header``   -- ``Retry-After`` / ``anthropic-ratelimit-*-reset``: up to 7 days.
* ``absolute`` -- a dated timestamp directly after a reset verb (ISO, or Codex's
  "at Oct 6th, 2026 10:34 PM"): up to 7 days.
* ``text``     -- a relative duration ("try again in 3 days") or a bare clock
  time ("try again at 6:39 AM", "tomorrow at 06:39"): capped at 24h, because
  prose is the least reliable source.

Message text is only read where a reset verb sits at the start of its own clause
right after a limit phrase ("usage limit ... try again in X"). "You should retry
in 3 days" inside generic advice is NOT a reset report and keeps the 15s
cooldown. Anthropic is never benched from text (headers only), and the last
provider left in a chain is never benched beyond the 15s cooldown, so a
misparse cannot become a multi-day outage. ``llm-router provider unban <name>``
clears a bench by hand.

UNVERIFIED ASSUMPTIONS: the exact live Codex usage-limit wording and the
timezone of a bare clock time ("try again at 6:39 AM") were not capturable from
the repo or git history. The text patterns are deliberately generic rather than
a copy of one string, and a bare clock time is read as LOCAL time (how a CLI
prints it to the person at the keyboard); if Codex prints UTC the bench is off
by the UTC offset, bounded by the 24h text cap. If real wording differs, add a
case to the tests.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping, Sequence

from llm_router import failopen, paths
from llm_router.file_lock import exclusive_lock
from llm_router.logging import get_logger

log = get_logger("llm_router.provider_reset")

#: Longest we will ever skip a provider, however far away it claims the reset is.
MAX_SKIP_SECONDS = 7 * 24 * 3600

#: Longest a bench derived from message text (not a header, not an absolute
#: timestamp) may last. Prose is the least reliable source.
MAX_TEXT_SKIP_SECONDS = 24 * 3600

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

#: A limit/quota/rate-limit phrase. Message text is only read AFTER one of
#: these (headers bypass this -- they are authoritative regardless of wording).
_LIMIT_SIGNAL_RE = re.compile(
    r"usage limit|rate.?limit|too many requests|quota|exhausted|\b429\b",
    re.IGNORECASE,
)

#: How far after the limit phrase the reset sentence may start.
_VERB_WINDOW = 160

#: The reset verb. ``retry``/``try again`` take "in/after/at", ``resets`` takes
#: "in/at/on".
_VERB_RE = re.compile(r"(?:try again|retry|resets?)\b", re.IGNORECASE)

#: Words that may sit between the start of a clause and the reset verb. Anything
#: else ("you should", "in general you may", "see docs and") makes it advice,
#: not a report. A lead-in is the text after the last clause boundary between
#: the limit phrase and the verb.
_CLAUSE_LEADS = frozenset({
    "", "or", "and", "then", "so", "please", "wait and",
    "will", "it will", "which will", "will be", "it will be",
})

#: A bare "or" lead (see ``_from_message``) is trusted only across a span that
#: names the kind of clause a real usage-limit report uses to offer a way out.
_MONETIZATION_RE = re.compile(
    r"upgrade|purchase|credits?|subscri|billing|\bplans?\b", re.IGNORECASE
)

#: ... and only when that same span carries none of these -- each one marks a
#: suggestion as hedged advice, not a report of what the provider will do.
_HEDGE_RE = re.compile(
    r"\bcould\b|\bmight\b|\bmay\b|people say|if you prefer|\busually\b|"
    r"\bprobably\b|nobody (?:really )?knows|\bI think\b|\bgenerally\b",
    re.IGNORECASE,
)

_DURATION_RE = re.compile(
    r"\s*(?:in|after)\s+(?:about\s+|approximately\s+)?"
    r"(?:(?P<d>\d+)\s*d(?:ays?)?(?![a-z])\s*)?"
    r"(?:(?P<h>\d+)\s*h(?:ours?|rs?)?(?![a-z])\s*)?"
    r"(?:(?P<m>\d+)\s*m(?:in(?:ute)?s?)?(?![a-z])\s*)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?(?![a-z]))?",
    re.IGNORECASE,
)

_CLOCK_RE = re.compile(
    r"\s*(?P<tom1>tomorrow\s+)?at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?P<tom2>\s+tomorrow)?",
    re.IGNORECASE,
)

_ISO_AFTER_VERB_RE = re.compile(
    r"\s*(?:at|on|:)\s*"
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)

#: Codex prints the reset as a dated clock time: "try again at Oct 6th, 2026
#: 10:34 PM." (wording reported publicly, not captured from a live run here).
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE_NAME_RE = re.compile(
    r"\s*(?:at|on)\s+(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4}),?\s+)?"
    r"(?:at\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?",
    re.IGNORECASE,
)

_CLAUSE_SPLIT_RE = re.compile(r"[.;!?:,]\s+|[,)(]")


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


def _duration_seconds(m: re.Match[str]) -> float | None:
    d, h, mi, s = (m.group(k) for k in ("d", "h", "m", "s"))
    if not (d or h or mi or s):
        return None
    # A component the message did not mention is simply not added.
    delta = sum(float(v) * k for v, k in zip((d, h, mi, s), (86400, 3600, 60, 1)) if v)
    return delta if delta > 0 else None


def _clock_epoch(m: re.Match[str], now: float) -> float | None:
    hour, minute = int(m.group("hour")), int(m.group("minute"))
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    # Bare clock time: LOCAL time, unverified (see module docstring).
    candidate = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if m.group("tom1") or m.group("tom2"):
        # An explicit "tomorrow" is the next calendar day, even when that clock
        # time has not yet passed today.
        candidate += timedelta(days=1)
    elif candidate.timestamp() <= now:
        candidate += timedelta(days=1)  # a bare clock time means its next occurrence
    return candidate.timestamp()


def _iso_epoch(stamp: str) -> float | None:
    """Aware stamps use their own offset; naive ones are read as local time, as a
    CLI prints it to the person at the keyboard."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _dated_epoch(m: re.Match[str], now: float) -> float | None:
    mon = m.group("mon").lower()
    if mon not in _MONTHS:
        return None
    hour, minute = int(m.group("hour")), int(m.group("minute"))
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    year = int(m.group("year")) if m.group("year") else datetime.fromtimestamp(now).year
    try:
        # Local time, unverified (see module docstring).
        epoch = datetime(year, _MONTHS.index(mon) + 1, int(m.group("day")), hour, minute).timestamp()
        if not m.group("year") and epoch <= now:
            epoch = datetime(year + 1, _MONTHS.index(mon) + 1, int(m.group("day")), hour, minute).timestamp()
        return epoch
    except ValueError:
        return None


def _from_message(text: str, now: float) -> tuple[float, str] | None:
    """A reset from limit-report text, as ``(epoch, source)``; ``source`` is
    ``"absolute"`` for an ISO timestamp and ``"text"`` otherwise."""
    for limit in _LIMIT_SIGNAL_RE.finditer(text):
        tail = text[limit.end(): limit.end() + _VERB_WINDOW]
        for verb in _VERB_RE.finditer(tail):
            lead = _CLAUSE_SPLIT_RE.split(tail[: verb.start()])[-1].strip().lower()
            # A real Codex message (2026-10-03 capture) joins the credits-purchase
            # clause to the retry clause with a bare "or" and no comma before it:
            # "...to purchase more credits or try again at 11:33 PM." — the clause
            # boundary regex only splits on punctuation, so `lead` here is the
            # whole "...or" clause, not the bare connector "or" already allowed
            # above. Accepting any lead that ENDS IN " or" (rather than requiring
            # the full lead to equal "or") covers this without weakening the
            # advice/report distinction: every INCIDENTAL advice fixture's lead
            # ends in a different word ("should", "may", "can", "and", ...), so
            # none of them newly match.
            is_bare_or = lead == "or" or lead.endswith(" or")
            if lead not in _CLAUSE_LEADS and not is_bare_or:
                continue
            if is_bare_or:
                # "or" alone (bare, or at the end of a longer lead) is also how
                # hedged ADVICE joins two suggestions: "you could wait a bit, or
                # try again in 3 days if you prefer" and "people say wait or try
                # again in 3 days but nobody really knows" both produce an "or"
                # lead, exactly like the real Codex report's "...purchase more
                # credits or try again at 11:33 PM." A bare "or" is trusted only
                # when the span it joins (limit phrase .. verb) both names the
                # kind of clause an actual usage-limit report uses -- upgrade,
                # purchase, credits, a plan/subscription -- AND carries none of
                # the hedge words that mark advice instead of a report.
                span = tail[: verb.start()]
                if not _MONETIZATION_RE.search(span) or _HEDGE_RE.search(span):
                    continue
            rest = tail[verb.end():]
            m = _ISO_AFTER_VERB_RE.match(rest)
            if m:
                epoch = _iso_epoch(m.group("ts"))
                if epoch is not None:
                    return epoch, "absolute"
            m = _DATE_NAME_RE.match(rest)
            if m:
                epoch = _dated_epoch(m, now)
                if epoch is not None:
                    return epoch, "absolute"
            m = _CLOCK_RE.match(rest)
            if m:
                epoch = _clock_epoch(m, now)
                if epoch is not None:
                    return epoch, "text"
            m = _DURATION_RE.match(rest)
            if m:
                secs = _duration_seconds(m)
                if secs is not None:
                    return now + secs, "text"
    return None


def parse_reset(
    message: str | None,
    headers: Mapping[str, str] | None = None,
    now: float | None = None,
) -> tuple[float | None, str]:
    """``(epoch, source)`` with the per-source cap applied; ``(None, "")`` when
    there is nothing actionable. ``source`` is ``header``, ``absolute`` or
    ``text``. A reset already in the past is expired (``None``)."""
    now = time.time() if now is None else now
    epoch: float | None = None
    source = ""
    if headers:
        epoch = _from_headers(headers, now)
        source = "header"
    if epoch is None and message:
        found = _from_message(str(message), now)
        if found is not None:
            epoch, source = found
    if epoch is None or epoch <= now:
        return None, ""
    cap = MAX_TEXT_SKIP_SECONDS if source == "text" else MAX_SKIP_SECONDS
    return min(epoch, now + cap), source


def parse_reset_epoch(
    message: str | None,
    headers: Mapping[str, str] | None = None,
    now: float | None = None,
) -> float | None:
    """Epoch seconds at which the provider says it is usable again, or ``None``.

    Headers win over message text. A reset already in the past yields ``None``
    (expired); one beyond its source's cap (7 days; 24h for plain text) is capped.
    """
    return parse_reset(message, headers, now)[0]


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


def clear_provider_reset(provider: str | None = None) -> list[str]:
    """Manually lift a bench: one provider, or every provider when ``None``.

    Returns the names that were cleared. Never raises (fails open to ``[]``).
    """
    try:
        path = _state_file()
        if not path.exists():
            return []
        with exclusive_lock(path.with_suffix(path.suffix + ".lock")):
            try:
                providers = _load(path)
            except (OSError, ValueError):
                providers = {}
            names = [str(n) for n in providers] if provider is None else (
                [provider] if provider in providers else []
            )
            for n in names:
                providers.pop(n, None)
            if names:
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"providers": providers}), encoding="utf-8")
                tmp.replace(path)
        return names
    except Exception as exc:  # noqa: BLE001 -- must never break the caller
        failopen.record("CHZ-FO-PROVIDER-RESET-WRITE", exc, detail=provider or "*")
        return []


#: Marks an exception whose reset has already been looked at, so the CLI
#: branches (which parse the full output) and the generic handler (which sees
#: the truncated message) record a failure once, not twice.
_NOTED_ATTR = "_llm_router_reset_noted"


def note_provider_error(
    provider: str,
    exc: BaseException,
    headers: Mapping[str, str] | None = None,
    *,
    text: str | None = None,
    alternatives: Sequence[str] | None = None,
) -> float | None:
    """Parse ``exc``/``headers`` for a reset and persist it. Never raises.

    ``text`` overrides ``str(exc)`` when the caller holds the full output and
    ``exc`` carries only a truncated copy. ``alternatives`` are the providers
    still AHEAD of this request in its chain (not the ones that already ran and
    failed); when none of them is currently usable, ``provider`` is the last one
    standing and is not benched beyond the caller's 15s cooldown (a misparse
    must not become a multi-day outage). ``None`` skips that check.

    Returns the persisted reset epoch, or ``None`` when nothing actionable was
    found (the caller then keeps its existing 15s cooldown).
    """
    try:
        if getattr(exc, _NOTED_ATTR, False):
            return None
        try:
            setattr(exc, _NOTED_ATTR, True)
        except Exception:  # noqa: BLE001 -- exceptions with __slots__
            pass
        until, source = parse_reset(text if text is not None else str(exc), headers)
        if until is None:
            return None
        if provider == "anthropic" and source != "header":
            return None  # subscription/API Anthropic: headers only, never prose
        if alternatives is not None and not any(
            a != provider and not is_provider_reset_blocked(a) for a in alternatives
        ):
            return None
        reason = text if text is not None else str(exc)
        if record_provider_reset(provider, until, reason=reason):
            return until
    except Exception as err:  # noqa: BLE001
        failopen.record("CHZ-FO-PROVIDER-RESET-PARSE", err, detail=provider)
    return None
