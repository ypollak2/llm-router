"""The text sidecar of the classifier shadow: the typed prompt of a SAMPLED turn, for a labeller.

``llm-router kpi`` scores the live classifier against truth labels
(``LLM_ROUTER_SHADOW_LABELS``). A label needs the prompt, and every other store here
keeps hashes only. This module is the one place that keeps text, and only for the few
turns a deterministic sample picks, so an offline judge can label them (v16 P1.7-d,
owner decision 2026-10-08, option B).

Off by default. ``LLM_ROUTER_SHADOW_TEXT_SAMPLE``:

* unset, empty, ``off``, ``0``, a non-number, or a number outside (0, 1]: off. Nothing is
  hashed, read or written;
* ``on``: sample :data:`RATE_ON` of the turns;
* a number in (0, 1]: that rate.

It acts only where ``llm_shadow`` appends a ``classifier_shadow`` record, so each sidecar
entry has a matching shadow record to join on ``(session_id, text_sha)``.

The sample is a pure function of ``text_sha`` and the UTC day, so a restart or a retry
decides the same way. At most :data:`CAP_PER_DAY` entries are kept per UTC day (counted
from the file). An entry is ``{text_sha, session_id, ts, context, prompt}``; the file is
mode 0600. A turn whose text matches a pattern of ``secret_scrubber.SECRET_PATTERNS`` is
skipped whole, and router banners are cut out of the text before it is stored. The text
goes nowhere else: no log line, no failopen detail, no exception message carries it.

Deleting entries is the labeller's job (older than 7 days, or labelled/skipped and from
before today), under the same lock as the append.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only; without it the append is unlocked (still O_APPEND, one write)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from llm_router import prompt_key
from llm_router.proxy.steps import newest_human_text
from llm_router.secret_scrubber import SECRET_PATTERNS

ENV = "LLM_ROUTER_SHADOW_TEXT_SAMPLE"
SIDECAR_NAME = "shadow_text.jsonl"
LOCK_SUFFIX = ".lock"
RATE_ON = 0.3
CAP_PER_DAY = 20
PROMPT_STORE_CAP = 20000
OMITTED = "\n[... middle omitted ...]\n"
KEYS = ("text_sha", "session_id", "ts", "context", "prompt")

# Router banners that could carry the rules' pick into a judge's input (blindness).
MARKERS = ("⚡", "ROUTE:", "[llm_router]", "[llm-router]", "📊",
           "Routing context for this agent", "SUBSCRIPTION OVERRIDE")
SCRUB_SUFFIX = " [router text removed]"

# What maybe_record returns (tests and counters read it; the proxy ignores it).
OFF = "off"
NOT_SAMPLED = "not_sampled"
WRITTEN = "written"
SKIP_EMPTY = "skipped_empty"
SKIP_MISMATCH = "skipped_key_mismatch"
SKIP_SECRET = "skipped_secret"
SKIP_CAP = "skipped_cap"
SKIP_DUP = "skipped_duplicate"
SKIP_LOCKED = "skipped_locked"


def rate(raw: str | None = None) -> float:
    """The sampling rate from the env (0.0 = off)."""
    value = (os.environ.get("LLM_ROUTER_SHADOW_TEXT_SAMPLE", "") if raw is None else raw).strip().lower()
    if value in ("", "off", "0"):
        return 0.0
    if value == "on":
        return RATE_ON
    try:
        r = float(value)
    except ValueError:
        return 0.0
    return r if 0.0 < r <= 1.0 else 0.0


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def sampled(text_sha: str, day: str, r: float) -> bool:
    """Deterministic: the same ``(text_sha, day)`` always gives the same answer."""
    if r <= 0.0 or not text_sha:
        return False
    digest = hashlib.sha256(f"shadowtext:{day}:{text_sha}".encode()).hexdigest()
    return int(digest[:8], 16) / 2**32 < r


def wanted(text_sha: str | None, ts: float | None) -> bool:
    """The cheap, inline test: is this turn to be recorded? (A sha256 at most, only when on.)"""
    r = rate()
    if r <= 0.0 or not text_sha:
        return False
    return sampled(text_sha, day_of(ts if isinstance(ts, (int, float)) else time.time()), r)


def scrub(text: str) -> str:
    """Cut every line at its earliest router marker and mark the cut. A marker never survives."""
    out = []
    for line in text.split("\n"):
        cuts = [i for i in (line.find(m) for m in MARKERS) if i >= 0]
        out.append(line[:min(cuts)].rstrip() + SCRUB_SUFFIX if cuts else line)
    return "\n".join(out)


def secret_hit(*texts: str) -> bool:
    return any(p.search(t) for t in texts for p in SECRET_PATTERNS.values())


def sidecar_path(records_path: Path | None) -> Path:
    """Next to the shadow record file (the state dir by default)."""
    if records_path is not None:
        return records_path.parent / SIDECAR_NAME
    from llm_router import paths

    return paths.state_path(SIDECAR_NAME)


def _store(prompt: str) -> str:
    if len(prompt) <= PROMPT_STORE_CAP:
        return prompt
    half = PROMPT_STORE_CAP // 2
    return prompt[:half] + OMITTED + prompt[-half:]


def _existing(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def maybe_record(path: Path, body: dict, context: str, fields: dict) -> str:
    """Append the sidecar entry for this turn if it is sampled and passes every guard.

    ``body`` is the request snapshot (``messages``), ``context`` the classifier's context,
    ``fields`` the shadow record's identity (``ts``, ``session_id``, ``text_sha``).
    Raises only on a bug; the caller fails open. Never logs text."""
    ts = fields.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) else time.time()
    text_sha, session_id = fields.get("text_sha"), fields.get("session_id")
    r = rate()
    if r <= 0.0:
        return OFF
    day = day_of(ts)
    if not text_sha or not isinstance(session_id, str) or not session_id:
        return NOT_SAMPLED
    if not sampled(text_sha, day, r):
        return NOT_SAMPLED
    prompt = newest_human_text(body)
    if not prompt.strip():
        return SKIP_EMPTY
    if prompt_key.key(prompt) != text_sha:
        return SKIP_MISMATCH  # what we hold is not the text the key names: never store it
    if secret_hit(prompt, context):
        return SKIP_SECRET
    entry = {"text_sha": text_sha, "session_id": session_id, "ts": ts,
             "context": scrub(context), "prompt": scrub(_store(prompt))}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(path) + LOCK_SUFFIX, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return SKIP_LOCKED  # the labeller is pruning: drop this sample, never wait
        have = _existing(path)
        if any(e.get("text_sha") == text_sha and e.get("session_id") == session_id for e in have):
            return SKIP_DUP
        today = sum(1 for e in have if isinstance(e.get("ts"), (int, float)) and day_of(e["ts"]) == day)
        if today >= CAP_PER_DAY:
            return SKIP_CAP
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return WRITTEN
    finally:
        with contextlib.suppress(OSError):
            os.close(lock_fd)  # closing the descriptor releases the flock
