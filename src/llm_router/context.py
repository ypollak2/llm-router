"""Session context management — brings conversation history to routed LLM calls.

Two layers of context are maintained:

1. **Session buffer** (in-process, ephemeral) — a ring buffer of the last N
   message exchanges from the current MCP server session. Cleared on restart.

2. **Persistent summaries** (SQLite, cross-session) — compact summaries of
   prior sessions that survive restarts. Stored alongside usage data.

Context is injected into the LLM messages array as:
  [system_prompt?] → [previous_session_summary?] → [recent_messages...] → [user_prompt]

This gives external models awareness of both long-term history and
immediate conversational flow without exaggerating token usage.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3  # noqa: F401 — referenced by string annotations + local imports below
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from llm_router.compaction import compact_structural
from llm_router.context_optimizer import ContextOptimizationResult, optimize_context
from llm_router.okf import _KNOWLEDGE_CTX_RE

log = logging.getLogger("llm_router")

# Self-poisoning guard (same class of bug okf.py's enrich_from_response() already
# guards against — see its comment: "a setup.py doc captured a prompt + an echoed
# <knowledge_context> block and kept re-injecting it"). That fix only covers OKF's
# OWN re-capture path; it does NOT cover THIS module, which records every routed
# prompt/response verbatim into a session buffer (in-process, always on — not
# gated by LLM_ROUTER_OKF) and later replays it into unrelated future prompts. If any
# ONE exchange gets OKF-injected (or carries a previously-injected context block
# from this module itself), recording it here would re-inject it indefinitely —
# including after OKF is disabled, since the buffer/summary no longer knows where
# the content came from. Strip every known injected-context wrapper before it
# ever reaches storage.
_SELF_INJECTED_RE = re.compile(
    r"\[(?:Recent conversation context|Previous session context|Additional context)\]"
    r".*?(?=\n\n|\Z)",
    re.DOTALL,
)


def _strip_injected_context(text: str) -> str:
    """Remove OKF `<knowledge_context>` blocks and this module's own injected
    `[...]` blocks from `text` before it is ever persisted or re-injected."""
    if not text:
        return text
    text = _KNOWLEDGE_CTX_RE.sub("", text)
    text = _SELF_INJECTED_RE.sub("", text)
    # Also strip an echoed Session Context Accumulator block (session_store.py's
    # own sentinel wrapper) — same class of self-poisoning bug, different source.
    try:
        from llm_router.session_store import _INJECTED_CTX_RE as _sca_re
        text = _sca_re.sub("", text)
    except Exception:
        pass
    return text.strip()

# Module-level storage for last optimization result (for footer display)
_last_optimization: ContextOptimizationResult | None = None


def get_last_optimization() -> ContextOptimizationResult | None:
    """Return the result of the last context optimization pass."""
    return _last_optimization

# ── Session Buffer (in-process, ephemeral) ──────────────────────────────────


@dataclass(frozen=True)
class SessionMessage:
    """A single exchange recorded in the session buffer.

    Attributes:
        role: Message role — "user" or "assistant".
        content: The message text (may be truncated on storage).
        timestamp: Unix timestamp when the message was recorded.
        task_type: What kind of routed task produced this (e.g. "query", "code").
    """

    role: str
    content: str
    timestamp: float
    task_type: str = ""


class SessionBuffer:
    """Ring buffer holding recent exchanges from the current session.

    Thread-safe for single-writer async contexts (which is all MCP needs).
    Messages are stored as-is; compaction happens at retrieval time.
    """

    def __init__(self, max_messages: int = 10) -> None:
        self._buffer: deque[SessionMessage] = deque(maxlen=max_messages)
        self._session_start: float = time.time()
        # CHZ-AUD-B-04: tracked so the registry below can evict idle buffers.
        self.last_access: float = time.time()

    def record(self, role: str, content: str, task_type: str = "") -> None:
        """Add a message to the session buffer.

        Strips injected-context blocks before storage — see module-level
        _strip_injected_context() docstring. This is the single choke point
        every recorded exchange passes through, so guarding here (rather than
        at each call site) protects the buffer regardless of how a caller
        obtained `content`.
        """
        self._buffer.append(SessionMessage(
            role=role,
            content=_strip_injected_context(content)[:2000],  # truncate on write
            timestamp=time.time(),
            task_type=task_type,
        ))

    def get_recent(self, n: int = 5) -> list[SessionMessage]:
        """Return the last N messages, oldest first."""
        items = list(self._buffer)
        return items[-n:] if len(items) > n else items

    def clear(self) -> None:
        """Clear the buffer (e.g. on session end before summarizing)."""
        self._buffer.clear()

    @property
    def message_count(self) -> int:
        return len(self._buffer)

    def format_for_injection(self, n: int = 5) -> str:
        """Format recent messages as a compact context block for LLM injection.

        Returns:
            A formatted string summarizing recent exchanges, or empty string
            if no messages are buffered.
        """
        messages = self.get_recent(n)
        if not messages:
            return ""

        lines = ["[Recent conversation context]"]
        for msg in messages:
            prefix = "User" if msg.role == "user" else "Assistant"
            tag = f" ({msg.task_type})" if msg.task_type else ""
            # Truncate individual messages for context injection
            content = msg.content[:500]
            if len(msg.content) > 500:
                content += "..."
            lines.append(f"{prefix}{tag}: {content}")

        return "\n".join(lines)


# CHZ-AUD-B-04: bounded, evictable registry of per-(project, session)
# SessionBuffers.
#
# Previously this module kept ONE process-wide singleton SessionBuffer
# (`_session_buffer`), and `get_session_buffer()` took no arguments. In a
# long-lived MCP server process handling multiple projects/sessions, every
# caller shared that single buffer: recent conversation content from one
# project/session was injected into prompts for a completely different
# project/session (see build_context_messages()'s former Layer 2, which
# ignored its own `session_id` parameter entirely). This registry scopes each
# buffer to its (project_id, session_id) key, mirroring the identity model
# `session_store` already uses for its durable, cross-process session
# storage — so the in-process buffer and the durable accumulator are now
# consistently scoped to the same identity.
#
# `get_session_buffer()` REQUIRES an explicit `project_id` on purpose: this
# keeps the registry a pure function of its inputs, with no hidden coupling
# to env vars / cwd / the filesystem inside the buffer-access primitive
# itself. Callers that need "the current project/session" resolve that
# identity themselves (via `session_store._project_id()` /
# `resolve_session_id()`, both fail-open) and pass it in explicitly — see
# `build_context_messages()`, `auto_summarize_session()`, and
# `router.route_and_call()`'s primary success path.
_MAX_BUFFERS = 200
_BUFFER_IDLE_EVICT_SECONDS = 6 * 3600  # mirrors session_store's pointer TTL

_buffers: dict[tuple[str, str], SessionBuffer] = {}


def get_session_buffer(project_id: str, session_id: str | None = None) -> SessionBuffer:
    """Return the SessionBuffer scoped to (project_id, session_id).

    Creates a new, empty buffer on first access for a given key. Evicts
    idle-too-long buffers opportunistically, and — if the registry is at
    capacity — the least-recently-accessed buffer, so a long-lived process
    handling many short-lived projects/sessions cannot grow this registry
    without bound.
    """
    key = (project_id or "_no_project", session_id or "_no_session")
    now = time.time()

    if _buffers:
        stale = [
            k for k, buf in _buffers.items()
            if now - buf.last_access > _BUFFER_IDLE_EVICT_SECONDS
        ]
        for k in stale:
            del _buffers[k]

    buf = _buffers.get(key)
    if buf is None:
        if len(_buffers) >= _MAX_BUFFERS:
            lru_key = min(_buffers, key=lambda k: _buffers[k].last_access)
            del _buffers[lru_key]
        buf = SessionBuffer()
        _buffers[key] = buf
    buf.last_access = now
    return buf


def _reset_session_buffers_for_test() -> None:
    """Test-only: clear the entire buffer registry.

    Hermetic tests must call this (rather than poking a module-level
    singleton, which no longer exists) between test cases that populate
    SessionBuffers, to avoid cross-test leakage through the registry.
    """
    _buffers.clear()


def _resolve_context_identity(
    project_id: str | None, session_id: str | None,
) -> tuple[str, str | None]:
    """Resolve (project_id, session_id) identity for the SessionBuffer registry.

    An explicit `project_id` wins outright (there is no parameterized
    "resolve project id, but override with X" primitive in `session_store`,
    unlike session_id). `session_id` is always run through
    `session_store.resolve_session_id(session_id)` — matching the durable
    Session Context Accumulator's existing precedence (explicit → env →
    pointer file) — so the in-process buffer and the durable store stay
    consistently scoped to the same identity.

    Fails open: any error resolving project_id/session_id (missing
    session_store, corrupt env, etc.) degrades to a best-effort identity
    rather than raising — callers must never lose context injection or
    session recording entirely just because identity resolution hiccuped.
    """
    resolved_pid = project_id
    resolved_sid: str | None = None
    try:
        from llm_router import session_store
        if not resolved_pid:
            resolved_pid = session_store._project_id()
        resolved_sid = session_store.resolve_session_id(session_id)
    except Exception:
        pass
    return resolved_pid or "_unknown", resolved_sid


# ── Persistent Session Summaries (SQLite) ────────────────────────────────────


def _get_db_path() -> Path:
    """Resolve the database path for session summaries."""
    from llm_router.config import get_config
    return get_config().llm_router_db_path


def _secure_db_perms(path: Path) -> None:
    """Ensure *path* is mode 0600, repairing looser existing perms.

    CHZ-AUD-D-02 (sibling): the session-summary sink shares ``usage.db`` but
    opened it without securing perms, leaving it world-readable (0644). Mirrors
    result_cache._secure_perms.
    """
    import os
    import stat as _stat
    try:
        if _stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


def _open_session_db(db_path: Path) -> "sqlite3.Connection":
    """Open usage.db for the session-summary sink with 0600 perms +
    secure_delete so TTL purges physically remove secret bytes."""
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        db_path.touch(mode=0o600)
    else:
        _secure_db_perms(db_path)
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA busy_timeout=3000")
    # CHZ-AUD-D-04 (sibling): zero freed pages on DELETE so the TTL purge below
    # physically removes redacted-but-still-sensitive summary bytes.
    conn.execute("PRAGMA secure_delete=ON")
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            _secure_db_perms(sidecar)
    return conn


def _persist_ttl_seconds() -> float:
    """Physical-retention TTL for session summaries (LLM_ROUTER_PERSIST_TTL_DAYS)."""
    try:
        from llm_router.config import get_config
        days = float(getattr(get_config(), "llm_router_persist_ttl_days", 30))
    except Exception:
        days = 30.0
    return max(days, 0.0) * 86_400


def _ensure_session_table(db_path: Path) -> None:
    """Create the session_summaries table if it doesn't exist."""
    conn = _open_session_db(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_start TEXT NOT NULL,
                session_end TEXT NOT NULL,
                summary TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                task_types TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


async def save_session_summary(
    summary: str,
    message_count: int,
    task_types: list[str],
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Persist a session summary to SQLite for cross-session context.

    Called when a session ends (or periodically) to capture what happened.

    Args:
        summary: Compact text summary of the session's work.
        message_count: How many exchanges occurred in the session.
        task_types: List of task types used during the session.
        project_id: Scopes which SessionBuffer's `_session_start` is read
            (CHZ-AUD-B-04). Optional — resolved via `session_store` when
            omitted, so existing callers are unaffected.
        session_id: Same scoping, for session identity.
    """
    from datetime import datetime, timezone

    db_path = _get_db_path()
    _ensure_session_table(db_path)

    _pid, _sid = _resolve_context_identity(project_id, session_id)
    buf = get_session_buffer(_pid, _sid)

    session_start = datetime.fromtimestamp(
        buf._session_start, tz=timezone.utc,
    ).isoformat()
    session_end = datetime.now(timezone.utc).isoformat()

    # CHZ-AUD-D-01 (sibling): the session summary is LLM-generated from the
    # conversation and can carry secrets/PII. Scrub it through the same shared
    # persist_redact() the other persistence sinks use BEFORE it touches disk.
    # Safe-failure: on any redaction error, store a placeholder rather than the
    # raw text, so a scrubber bug can never leak the original.
    try:
        from llm_router.persist_redaction import persist_redact
        safe_summary = persist_redact(summary)
    except Exception as _redact_err:  # noqa: BLE001
        log.warning("session_summary_redaction_failed", error=str(_redact_err))
        safe_summary = "[REDACTION_ERROR: summary withheld]"

    import time as _time
    conn = _open_session_db(db_path)
    try:
        conn.execute(
            """INSERT INTO session_summaries
               (session_start, session_end, summary, message_count, task_types)
               VALUES (?, ?, ?, ?, ?)""",
            (session_start, session_end, safe_summary, message_count, json.dumps(task_types)),
        )
        # CHZ-AUD-D-04 (sibling): physically purge summaries older than the TTL
        # (secure_delete=ON zeroes the freed pages). 0 disables purging.
        _ttl = _persist_ttl_seconds()
        if _ttl > 0:
            _cutoff = datetime.fromtimestamp(
                _time.time() - _ttl, tz=timezone.utc,
            ).isoformat()
            conn.execute(
                "DELETE FROM session_summaries WHERE session_end < ?", (_cutoff,),
            )
        conn.commit()
    finally:
        conn.close()

    log.info("Saved session summary (%d messages, types: %s)", message_count, task_types)


async def auto_summarize_session(
    min_messages: int = 3,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Generate and persist a session summary using a cheap LLM.

    Collects the session buffer, sends it to the cheapest available model
    for summarization, and saves the result to SQLite. Returns None if
    the session has fewer than ``min_messages`` exchanges.

    Args:
        min_messages: Minimum number of messages before summarization triggers.
        project_id: Scopes which SessionBuffer is read (CHZ-AUD-B-04).
            Optional — resolved via `session_store` when omitted.
        session_id: Same scoping, for session identity. Resolved once here
            and forwarded to `save_session_summary()` so both reads/writes
            in a single summarization pass see the exact same identity,
            even if the ambient session pointer changes mid-call.

    Returns:
        The generated summary string, or None if skipped.
    """
    _pid, _sid = _resolve_context_identity(project_id, session_id)
    buf = get_session_buffer(_pid, _sid)
    messages = buf.get_recent(buf.message_count)

    if len(messages) < min_messages:
        log.info("Session too short (%d msgs) — skipping summary", len(messages))
        return None

    # Build the conversation transcript for summarization. Defense in depth:
    # msg.content is already stripped at record()-time, but never feed the
    # summarizer LLM anything that might still carry an injected-context marker
    # (e.g. buffered content written before this guard existed in a
    # long-running process).
    transcript_lines = []
    task_types_seen: set[str] = set()
    for msg in messages:
        prefix = "User" if msg.role == "user" else "Assistant"
        clean = _strip_injected_context(msg.content)
        content = clean[:300]
        if len(clean) > 300:
            content += "..."
        transcript_lines.append(f"{prefix}: {content}")
        if msg.task_type:
            task_types_seen.add(msg.task_type)

    transcript = "\n".join(transcript_lines)

    summarize_prompt = (
        "Summarize this conversation in 1-2 sentences. "
        "Focus on what was worked on, key decisions made, and outcomes. "
        "Be specific and concise.\n\n"
        f"{transcript}"
    )

    try:
        from llm_router.router import route_and_call
        from llm_router.types import RoutingProfile, TaskType

        resp = await route_and_call(
            TaskType.QUERY,
            summarize_prompt,
            profile=RoutingProfile.BUDGET,
            system_prompt="You are a concise session summarizer. Output only the summary, nothing else.",
            temperature=0.2,
            max_tokens=150,
        )
        summary = resp.content.strip()
    except Exception as e:
        # Fallback: concatenate user prompts if summarization fails
        log.warning("Auto-summarize failed (%s), using fallback", e)
        user_msgs = [m.content[:100] for m in messages if m.role == "user"]
        summary = "Topics: " + "; ".join(user_msgs[:5])

    await save_session_summary(
        summary=summary,
        message_count=len(messages),
        task_types=sorted(task_types_seen),
        project_id=_pid,
        session_id=_sid,
    )

    log.info("Auto-summarized session: %s", summary[:100])
    return summary


async def get_recent_session_summaries(limit: int = 3) -> list[dict]:
    """Load the most recent session summaries from SQLite.

    Args:
        limit: Maximum number of past sessions to retrieve.

    Returns:
        List of dicts with keys: summary, session_start, session_end,
        message_count, task_types. Ordered newest first.
    """
    import asyncio
    import sqlite3

    db_path = _get_db_path()
    # Offload synchronous Path.exists() to thread pool to avoid blocking event loop
    exists = await asyncio.to_thread(db_path.exists)
    if not exists:
        return []

    _ensure_session_table(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """SELECT summary, session_start, session_end, message_count, task_types
               FROM session_summaries
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            # Retroactive guard: strip on READ too, not just on write — a row
            # persisted before this fix existed (any process running an older
            # build of this module) could still carry an injected-context
            # block, and it would otherwise keep replaying into every future
            # session indefinitely, surviving restarts, forever.
            "summary": _strip_injected_context(row[0]),
            "session_start": row[1],
            "session_end": row[2],
            "message_count": row[3],
            "task_types": json.loads(row[4]),
        }
        for row in rows
    ]


def format_session_summaries(summaries: list[dict]) -> str:
    """Format persistent session summaries for LLM injection.

    Args:
        summaries: List of summary dicts from get_recent_session_summaries().

    Returns:
        Formatted context block, or empty string if no summaries exist.
    """
    if not summaries:
        return ""

    lines = ["[Previous session context]"]
    for s in reversed(summaries):  # oldest first for chronological order
        types = ", ".join(s["task_types"]) if s["task_types"] else "general"
        lines.append(f"- Session ({types}): {s['summary']}")

    return "\n".join(lines)


# ── Context Assembly ─────────────────────────────────────────────────────────


async def build_context_messages(
    *,
    caller_context: str | None = None,
    max_session_messages: int = 5,
    max_previous_sessions: int = 3,
    max_context_tokens: int = 1500,
    is_free_model: bool = False,
    session_id: str | None = None,
    project_id: str | None = None,
    target_provider: str | None = None,
) -> list[dict[str, str]]:
    """Assemble context messages for injection into LLM calls.

    Builds an ordered list of context messages:
      1. Previous session summaries (persistent, oldest→newest)
      2. Current session messages (ephemeral, last N)
      2b. Durable session context (Session Context Accumulator, cross-process)
      3. Caller-supplied context (if any)

    Context is optimized via the context_optimizer pipeline (v8.3.0),
    then compacted if it still exceeds the token budget.

    Args:
        caller_context: Optional explicit context from the MCP tool caller.
        max_session_messages: How many recent session messages to include.
        max_previous_sessions: How many past session summaries to load.
        max_context_tokens: Token budget for all context combined.
        is_free_model: If True, skip context optimization (no cost benefit).
        session_id: Optional explicit session id for the Session Context
            Accumulator (falls back to env/pointer-file resolution when
            omitted). When ``None`` and no session can be resolved, layer 2b
            contributes nothing — behavior is unchanged from before this
            layer existed.
        project_id: Optional explicit project id for scoping the in-process
            SessionBuffer (CHZ-AUD-B-04). Falls back to
            ``session_store._project_id()`` resolution when omitted — same
            fail-open precedence as ``session_id`` below.
        target_provider: Optional provider name (e.g. "openai", "gemini",
            "ollama") the assembled context is destined for. Used only to
            enforce the accumulator's privacy mode (``local`` blocks paid
            external APIs); has no effect otherwise.

    Returns:
        List of message dicts (role: "system") to insert between the
        system prompt and user prompt. May be empty if no context exists.
    """
    global _last_optimization
    parts: list[str] = []

    # CHZ-AUD-B-04: resolve (project_id, session_id) identity ONCE, up front,
    # so the in-process SessionBuffer (layer 2) and the durable Session
    # Context Accumulator (layer 2b) are scoped to the EXACT same identity.
    # Previously layer 2 called get_session_buffer() with no arguments at
    # all (a single process-wide singleton shared across every
    # project/session), while layer 2b independently resolved session_id via
    # session_store — so a long-lived process serving multiple
    # projects/sessions could inject one project's recent conversation
    # content into a completely different project's prompt.
    _ctx_project_id, _resolved_session_id = _resolve_context_identity(
        project_id, session_id,
    )

    # Resolve the session-context privacy mode once, up front, so EVERY context
    # layer (not just the Session Context Accumulator in layer 2b) honors it.
    # 'off' blocks all cross-session/session context; 'local' additionally
    # blocks context egress to any NON-free-local provider. Fails open to 'all'.
    #
    # RED2-04: this was a two-provider allowlist (`in ("openai","gemini")`), so
    # Perplexity — which every research-task prompt is routed to — bypassed the
    # gate entirely and received full session history in `local` mode. It is now
    # an inverted check against the known-free-local set, so ANY provider that is
    # not local/free (Perplexity, and any future paid provider) is blocked by
    # default under `local`, rather than requiring each new paid provider to be
    # added to a second allowlist. NOTE: `local` mode governs history/context
    # attachment only — it does not change routing destination (whether the
    # current prompt goes external is decided by the routing chain).
    _FREE_LOCAL_PROVIDERS = ("ollama", "codex", "gemini_cli")
    try:
        from llm_router import session_store
        privacy_mode = session_store.get_mode()
    except Exception:
        privacy_mode = "all"
    _blocks_external = (
        privacy_mode == "local" and target_provider not in _FREE_LOCAL_PROVIDERS
    )
    _context_suppressed = privacy_mode == "off" or _blocks_external

    # Layer 1: Previous session summaries. These are LLM-generated summaries of
    # prior sessions — under 'local' they must not reach external paid APIs, and
    # under 'off' they are disabled entirely. Previously this layer bypassed the
    # gate that build_session_context (layer 2b) already enforced (CHZ-AUD-023).
    if not _context_suppressed:
        summaries = await get_recent_session_summaries(limit=max_previous_sessions)
        session_context = format_session_summaries(summaries)
        if session_context:
            parts.append(session_context)

    # Layer 2: Current session messages. Same privacy gate as layer 1 — the
    # in-process buffer is verbatim session content and must not leak to
    # external paid APIs under 'local', nor be sent at all under 'off'.
    if not _context_suppressed:
        buf = get_session_buffer(_ctx_project_id, _resolved_session_id)
        current_context = buf.format_for_injection(n=max_session_messages)
        if current_context:
            parts.append(current_context)

    # Layer 2b: Durable session context (Session Context Accumulator) — user
    # prompts, tool calls, and routed Q&A recorded to a per-session JSONL
    # store outside this in-process buffer, so cheap routed models get real
    # context instead of fabricating. Downstream dedup_sections/optimize_context
    # collapse near-duplicates against layer 2's in-process buffer content, and
    # the existing token-budget compaction below still applies to the combined
    # total, so no additional cross-layer hash-dedupe is performed here.
    try:
        from llm_router import session_store
        from llm_router.config import get_config

        if _resolved_session_id:
            try:
                mcp_budget = get_config().session_context_max_tokens_mcp
            except Exception:
                mcp_budget = 1500
            durable_context = session_store.build_session_context(
                _resolved_session_id,
                max_tokens=mcp_budget,
                query=caller_context,
                target_provider=target_provider,
            )
            if durable_context:
                parts.append(durable_context)
    except Exception as e:
        log.debug("Session context accumulator unavailable (non-fatal): %s", e)

    # Layer 3: Caller-supplied context
    if caller_context:
        parts.append(f"[Additional context]\n{caller_context}")

    if not parts:
        _last_optimization = None
        return []

    combined = "\n\n".join(parts)

    # Context optimization (v8.3.0) — compress before sending to paid models
    try:
        import os
        optimizer_mode = os.getenv("LLM_ROUTER_CONTEXT_OPTIMIZER", "auto").lower()
        combined, opt_result = optimize_context(
            combined, mode=optimizer_mode, is_free_model=is_free_model,
        )
        _last_optimization = opt_result
        if opt_result.tokens_saved > 0:
            log.info(
                "Context optimized: %d → %d tokens (%.0f%% reduction, stages: %s)",
                opt_result.original_tokens, opt_result.compressed_tokens,
                opt_result.reduction_pct, ", ".join(opt_result.stages_applied),
            )
    except Exception as e:
        log.debug("Context optimization failed (non-fatal): %s", e)
        _last_optimization = None

    # Compact if over budget (existing behavior)
    combined, _ = await compact_structural(combined, threshold=max_context_tokens)

    # Final hard truncation safety net
    max_chars = max_context_tokens * 4  # rough tokens→chars
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n[... context truncated]"

    return [{"role": "system", "content": combined}]
