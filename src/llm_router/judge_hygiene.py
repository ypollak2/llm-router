"""Judge queue retention (P3.1-4): what text may be written, how long, and with what mode.

``judge_queue.jsonl`` is the only judge file that holds prompt and answer text.
Rules (the evidence is the audit in P31-PLAN.md "Retention": the queue held raw
prompt and answer, no redaction, mode 0644, no TTL):

  * An entry whose raw prompt or answer matches a ``secret_scrubber`` pattern is
    skipped whole; it is never written in a redacted form.
  * Surviving text passes ``shadow_text.scrub`` (router banners) and
    ``persist_redaction.persist_redact``, then is capped at 8000 characters each.
  * The file is created and rewritten with mode 0600.
  * Unclaimed entries older than ``min(persist TTL, queue max age)`` are physically
    purged. Claimed entries are deleted at claim (``judge._claim_queue``).

A skip records a failopen code with no text in it.

F97: a row carries ``"prepared": true`` once it has passed :func:`prepare`. A row without
the marker (queued before P3.1-4) is run through :func:`prepare` when it is claimed
(:func:`sanitize_claimed`), and dropped if prepare refuses it. Orphaned temp files
(``<queue>.rewrite.*``, ``<queue>.draining.*``) are physically purged
(:func:`purge_orphans`).
"""

from __future__ import annotations

import json
import os
import time

from llm_router import failopen

TEXT_CAP = 8000
_OMITTED = "\n[... omitted ...]\n"
DEFAULT_MAX_AGE_DAYS = 3.0
_PRIVATE = 0o600
PREPARED_KEY = "prepared"
#: A ``.rewrite.*`` temp file lives for microseconds; one this old was left by a crash.
REWRITE_ORPHAN_GRACE_S = 600.0


def _cap(text: str) -> str:
    if len(text) <= TEXT_CAP:
        return text
    half = (TEXT_CAP - len(_OMITTED)) // 2
    return text[:half] + _OMITTED + text[-half:]


def prepare(prompt: str, response: str) -> tuple[str, str] | None:
    """The (prompt, response) that may be stored, or None when the entry must be skipped."""
    from llm_router.secret_scrubber import SECRET_PATTERNS

    raw = (prompt or "", response or "")
    if any(p.search(t) for t in raw for p in SECRET_PATTERNS.values()):
        failopen.record("CHZ-FO-JUDGE-QUEUE-SECRET-SKIP", detail="secret pattern in prompt or answer; entry skipped")
        return None
    try:
        from llm_router.persist_redaction import persist_redact
        from llm_router.proxy.shadow_text import scrub

        out = tuple(_cap(persist_redact(scrub(t))) for t in raw)
    except Exception as exc:  # noqa: BLE001 — unredactable text is never written
        failopen.record("CHZ-FO-JUDGE-QUEUE-REDACT", exc)
        return None
    if any("[REDACTION-FAILED" in t for t in out):
        failopen.record("CHZ-FO-JUDGE-QUEUE-REDACT", None, detail="redaction withheld content; entry skipped")
        return None
    return out[0], out[1]


def append_private(path, lines: list[str]) -> None:
    """Append lines to ``path``, creating it 0600 and tightening an older, laxer file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _PRIVATE)
    try:
        os.fchmod(fd, _PRIVATE)
        os.write(fd, "".join(line if line.endswith("\n") else line + "\n" for line in lines).encode("utf-8"))
    finally:
        os.close(fd)


def rewrite_private(path, lines: list[str]) -> None:
    """Replace ``path`` atomically with ``lines``, mode 0600 from the first byte."""
    tmp = f"{path}.rewrite.{os.getpid()}.{time.monotonic_ns()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE)
    try:
        os.write(fd, "".join(line if line.endswith("\n") else line + "\n" for line in lines).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def max_age_seconds() -> float:
    """``min(persist TTL, queue max age)``; a persist TTL of 0 or less leaves the queue bound alone."""
    days = DEFAULT_MAX_AGE_DAYS
    raw = os.environ.get("LLM_ROUTER_JUDGE_QUEUE_MAX_AGE_DAYS", "").strip()
    if raw:
        try:
            days = float(raw)
        except ValueError as exc:
            failopen.record("CHZ-FO-JUDGE-QUEUE-TTL", exc)
    try:
        from llm_router.config_lite import config_value

        persist = float(config_value("llm_router_persist_ttl_days"))
        if persist > 0:
            days = min(days, persist)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-JUDGE-QUEUE-TTL", exc)
    return max(days, 0.0) * 86_400


def purge_expired(path, now: float | None = None) -> int:
    """Physically drop unclaimed entries older than the retention bound; returns how many.

    An unreadable line has no age and is dropped with the expired ones: text that
    cannot be aged must not outlive the bound. Never raises.
    """
    cutoff = (time.time() if now is None else now) - max_age_seconds()
    try:
        with open(path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return 0
    except OSError as exc:
        failopen.record("CHZ-FO-JUDGE-QUEUE-PURGE", exc)
        return 0
    keep = []
    for ln in lines:
        try:
            queued = float(json.loads(ln).get("queued_at"))
        except (ValueError, TypeError, AttributeError):
            continue
        if queued >= cutoff:
            keep.append(ln)
    dropped = len(lines) - len(keep)
    if dropped:
        try:
            rewrite_private(path, keep)
        except OSError as exc:
            failopen.record("CHZ-FO-JUDGE-QUEUE-PURGE", exc)
            return 0
        failopen.record("CHZ-FO-JUDGE-QUEUE-PURGED", detail=f"purged={dropped}")
    return dropped


def sanitize_claimed(items: list[dict]) -> tuple[list[dict], int]:
    """(rows safe to hand to a judge, how many were dropped) for a freshly claimed batch.

    A row already marked prepared is kept as is (not re-redacted). Any other row
    (queued before P3.1-4, raw text) goes through :func:`prepare`: it is rewritten with
    the prepared text and the marker, or dropped when prepare returns None. The claim
    already removed the raw row from disk; a kept row is only ever requeued prepared.
    """
    kept: list[dict] = []
    dropped = 0
    for item in items:
        if item.get(PREPARED_KEY) is True:
            kept.append(item)
            continue
        text = prepare(str(item.get("prompt") or ""), str(item.get("response") or ""))
        if text is None:
            dropped += 1
            continue
        kept.append({**item, "prompt": text[0], "response": text[1], PREPARED_KEY: True})
    if dropped:
        failopen.record("CHZ-FO-JUDGE-QUEUE-LEGACY-DROPPED", detail=f"dropped={dropped}")
    return kept, dropped


def purge_orphans(path, now: float | None = None) -> int:
    """Delete temp files a crash left beside the queue; returns how many. Never raises.

    ``<queue>.rewrite.*`` older than ``REWRITE_ORPHAN_GRACE_S``: the rows are still in the
    queue file (the replace never happened). ``<queue>.draining.*`` older than the retention
    bound (and the grace): younger ones are still recoverable by ``judge._recover_orphaned_claims``.
    """
    now = time.time() if now is None else now
    age_limit = {".rewrite.": REWRITE_ORPHAN_GRACE_S,
                 ".draining.": max(max_age_seconds(), REWRITE_ORPHAN_GRACE_S)}
    removed = 0
    try:
        parent = path.parent
        if not parent.is_dir():
            return 0
        for entry in parent.iterdir():
            for marker, limit in age_limit.items():
                if not entry.name.startswith(path.name + marker):
                    continue
                try:
                    if now - entry.stat().st_mtime <= limit:
                        break
                    entry.unlink()
                    removed += 1
                except OSError as exc:
                    failopen.record("CHZ-FO-JUDGE-QUEUE-ORPHAN-PURGE", exc)
                break
    except OSError as exc:
        failopen.record("CHZ-FO-JUDGE-QUEUE-ORPHAN-PURGE", exc)
        return removed
    if removed:
        failopen.record("CHZ-FO-JUDGE-QUEUE-ORPHAN-PURGED", detail=f"purged={removed}")
    return removed
