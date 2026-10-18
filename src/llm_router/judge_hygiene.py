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
