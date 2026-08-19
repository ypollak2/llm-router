"""T3-M4: idempotency-key dedupe for ``route_and_call``.

Prevents an agent that retries a logical "turn" from producing
duplicate provider costs OR duplicate side effects when the turn's
output drives tool calls downstream.

Contract:

* The caller supplies ``idempotency_key="<opaque-string>"`` on
  ``route_and_call``. When the same key is presented again within the
  TTL window, the original ``LLMResponse`` is returned without
  contacting any provider — no cost incurred, an audit row written
  with ``outcome="idempotency_dedupe"``.
* The store is SQLite-backed at ``~/.llm-router/idempotency.db`` (override
  with ``LLM_ROUTER_IDEMPOTENCY_PATH``). Single-process; multi-process
  coordination lands in T2-XL1.
* Expired rows are swept lazily on access — no background thread,
  no scheduler.
* No auto-generation. The default ``idempotency_key=None`` preserves
  pre-T3-M4 routing behaviour. Agent platforms opt in deliberately
  by passing the key.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-008 (idempotency
slice of the runaway-protection cluster).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from llm_router.logging import get_logger
from llm_router.types import LLMResponse

log = get_logger("llm_router.idempotency")


def _secure_perms(path: Path) -> None:
    """Ensure *path* is mode 0600, repairing looser existing perms.

    CHZ-AUD-D-02 (sibling): mirrors result_cache._secure_perms — sensitive DB
    files must never be world/group-readable.
    """
    import stat as _stat
    try:
        if _stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


# Default TTL — 1 hour. Long enough to cover most agent-workflow
# retry windows; short enough to prevent unbounded growth without a
# vacuum job.
_DEFAULT_TTL_SECONDS = 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_entries (
    key TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_entries(expires_at);
"""


class IdempotencyStore:
    """SQLite-backed dedupe table for ``route_and_call`` responses.

    Thread-safety: a single connection per instance. SQLite's
    per-connection serialisation is sufficient for in-process use; a
    future T2-XL1 distributed backend will replace this for shared
    deployments.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or Path(
            os.environ.get("LLM_ROUTER_IDEMPOTENCY_PATH")
            or (Path.home() / ".llm-router" / "idempotency.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # CHZ-AUD-D-02 (sibling): the dedupe DB persists LLM responses to disk —
        # create it 0600 (owner-only) and repair looser perms on an existing file
        # opened by an older version, so it is never world/group-readable.
        if not self.db_path.exists():
            self.db_path.touch(mode=0o600)
        else:
            _secure_perms(self.db_path)
        self._conn = sqlite3.connect(str(self.db_path))
        # CHZ-AUD-D-04 (sibling): zero freed pages on DELETE so the TTL sweep
        # physically removes response bytes rather than leaving them in
        # unallocated pages.
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        for suffix in ("-wal", "-shm"):
            _sidecar = self.db_path.with_name(self.db_path.name + suffix)
            if _sidecar.exists():
                _secure_perms(_sidecar)

    # ── Lookup + insert ───────────────────────────────────────────────────

    def lookup(self, key: str) -> LLMResponse | None:
        """Return the cached ``LLMResponse`` for ``key`` if present and
        not expired; otherwise None. Expired rows are deleted on miss
        as part of lazy sweeping.
        """
        if not key:
            return None
        now = time.time()
        row = self._conn.execute(
            "SELECT payload_json, expires_at FROM idempotency_entries "
            "WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        payload_json, expires_at = row
        if expires_at <= now:
            self._conn.execute(
                "DELETE FROM idempotency_entries WHERE key = ?", (key,)
            )
            self._conn.commit()
            return None
        try:
            data = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            log.warning("idempotency_payload_corrupt", key=key, error=str(exc))
            return None
        return _payload_to_response(data)

    def store(
        self,
        key: str,
        response: LLMResponse,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        """Persist ``response`` under ``key`` with the given TTL."""
        if not key:
            return
        now = time.time()
        try:
            payload_json = json.dumps(_response_to_payload(response))
        except (TypeError, ValueError) as exc:
            log.warning("idempotency_payload_serialize_failed", error=str(exc))
            return
        # TTL is allowed to be sub-second so tests can exercise the
        # expiry path quickly. Non-positive values would write an
        # already-expired row — reject them with a warning rather than
        # silently dropping the entry.
        ttl = float(ttl_seconds)
        if ttl <= 0:
            log.warning("idempotency_non_positive_ttl_ignored", ttl=ttl)
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO idempotency_entries "
            "(key, created_at, expires_at, payload_json) VALUES (?, ?, ?, ?)",
            (key, now, now + ttl, payload_json),
        )
        self._conn.commit()

    def sweep_expired(self) -> int:
        """Remove all expired rows; return the count deleted.

        Production callers don't need to invoke this — ``lookup``
        sweeps the row it's asked about on miss. Tests and operators
        running a maintenance job may call this for bulk cleanup.
        """
        now = time.time()
        cursor = self._conn.execute(
            "DELETE FROM idempotency_entries WHERE expires_at <= ?",
            (now,),
        )
        deleted = cursor.rowcount
        self._conn.commit()
        return int(deleted or 0)


# ── Module-level singleton (lazy, thread-safe-on-first-call) ───────────────

_store: IdempotencyStore | None = None


def get_store() -> IdempotencyStore:
    """Return the process-wide ``IdempotencyStore``.

    Constructed on first call so import-time doesn't touch the DB.
    """
    global _store
    if _store is None:
        _store = IdempotencyStore()
    return _store


def reset_store_for_tests() -> None:
    """Drop the cached singleton so the next ``get_store`` picks up a
    fresh ``LLM_ROUTER_IDEMPOTENCY_PATH`` (typically a per-test tmp_path).
    Production never calls this.
    """
    global _store
    _store = None


# ── LLMResponse <-> dict conversion ────────────────────────────────────────


def _response_to_payload(response: LLMResponse) -> dict[str, Any]:
    """Serialise the fields we round-trip. Avoids ``__dict__`` because
    ``LLMResponse`` is a frozen dataclass and may carry non-JSON-able
    extras in future revisions; pinning the fields keeps the format
    stable across upgrades.

    CHZ-AUD-D-01 (sibling): the response ``content`` can carry secrets/PII and
    was previously persisted 100% raw. Route it through the same shared
    ``persist_redact()`` every other on-disk sink uses — gated by
    ``LLM_ROUTER_PERSIST_RAW`` / ``LLM_ROUTER_PERSIST_REDACTION`` so a caller who needs
    byte-exact dedupe replay can still opt into raw storage explicitly. Default
    is redact-on-write, consistent with result_cache/semantic_cache/session_store.
    """
    try:
        from llm_router.enterprise.redaction import persist_redact
        safe_content = persist_redact(response.content)
    except Exception as _redact_err:  # noqa: BLE001 — never persist raw on failure
        log.warning("idempotency_content_redaction_failed", error=str(_redact_err))
        safe_content = "[REDACTION_ERROR: content withheld]"
    return {
        "content": safe_content,
        "model": response.model,
        "provider": response.provider,
        "input_tokens": int(response.input_tokens or 0),
        "output_tokens": int(response.output_tokens or 0),
        "cost_usd": float(response.cost_usd or 0.0),
        "latency_ms": float(response.latency_ms or 0.0),
    }


def _payload_to_response(data: dict[str, Any]) -> LLMResponse | None:
    """Rebuild an ``LLMResponse`` from the stored payload. Returns None
    on shape mismatch — corrupt rows should not propagate as exceptions
    out of ``lookup``.
    """
    try:
        return LLMResponse(
            content=data["content"],
            model=data["model"],
            provider=data["provider"],
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            latency_ms=float(data.get("latency_ms", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("idempotency_payload_rebuild_failed", error=str(exc))
        return None


__all__ = [
    "IdempotencyStore",
    "get_store",
    "reset_store_for_tests",
]
