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
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from llm_router.compaction import compact_structural

log = logging.getLogger("llm_router")

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

    def record(self, role: str, content: str, task_type: str = "") -> None:
        """Add a message to the session buffer."""
        self._buffer.append(SessionMessage(
            role=role,
            content=content[:2000],  # truncate long content on write
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


# Module-level singleton
_session_buffer: SessionBuffer | None = None


def get_session_buffer() -> SessionBuffer:
    """Return the singleton session buffer."""
    global _session_buffer
    if _session_buffer is None:
        _session_buffer = SessionBuffer()
    return _session_buffer


# ── Persistent Session Summaries (SQLite) ────────────────────────────────────


def _get_db_path() -> Path:
    """Resolve the database path for session summaries."""
    from llm_router.config import get_config
    return get_config().llm_router_db_path


def _ensure_session_table(db_path: Path) -> None:
    """Create the session_summaries table if it doesn't exist."""
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
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


async def save_session_summary(summary: str, message_count: int, task_types: list[str]) -> None:
    """Persist a session summary to SQLite for cross-session context.

    Called when a session ends (or periodically) to capture what happened.

    Args:
        summary: Compact text summary of the session's work.
        message_count: How many exchanges occurred in the session.
        task_types: List of task types used during the session.
    """
    import sqlite3
    from datetime import datetime, timezone

    db_path = _get_db_path()
    _ensure_session_table(db_path)

    buf = get_session_buffer()

    session_start = datetime.fromtimestamp(
        buf._session_start, tz=timezone.utc,
    ).isoformat()
    session_end = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """INSERT INTO session_summaries
               (session_start, session_end, summary, message_count, task_types)
               VALUES (?, ?, ?, ?, ?)""",
            (session_start, session_end, summary, message_count, json.dumps(task_types)),
        )
        conn.commit()
    finally:
        conn.close()

    log.info("Saved session summary (%d messages, types: %s)", message_count, task_types)


async def auto_summarize_session(min_messages: int = 3) -> str | None:
    """Generate and persist a session summary using a cheap LLM.

    Collects the session buffer, sends it to the cheapest available model
    for summarization, and saves the result to SQLite. Returns None if
    the session has fewer than ``min_messages`` exchanges.

    Args:
        min_messages: Minimum number of messages before summarization triggers.

    Returns:
        The generated summary string, or None if skipped.
    """
    buf = get_session_buffer()
    messages = buf.get_recent(buf.message_count)

    if len(messages) < min_messages:
        log.info("Session too short (%d msgs) — skipping summary", len(messages))
        return None

    # Build the conversation transcript for summarization
    transcript_lines = []
    task_types_seen: set[str] = set()
    for msg in messages:
        prefix = "User" if msg.role == "user" else "Assistant"
        content = msg.content[:300]
        if len(msg.content) > 300:
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
    import sqlite3

    db_path = _get_db_path()
    if not db_path.exists():
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
            "summary": row[0],
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
) -> list[dict[str, str]]:
    """Assemble context messages for injection into LLM calls.

    Builds an ordered list of context messages:
      1. Previous session summaries (persistent, oldest→newest)
      2. Current session messages (ephemeral, last N)
      3. Caller-supplied context (if any)

    All context is compacted if it exceeds the token budget.

    Args:
        caller_context: Optional explicit context from the MCP tool caller.
        max_session_messages: How many recent session messages to include.
        max_previous_sessions: How many past session summaries to load.
        max_context_tokens: Token budget for all context combined.

    Returns:
        List of message dicts (role: "system") to insert between the
        system prompt and user prompt. May be empty if no context exists.
    """
    parts: list[str] = []

    # Layer 1: Previous session summaries
    summaries = await get_recent_session_summaries(limit=max_previous_sessions)
    session_context = format_session_summaries(summaries)
    if session_context:
        parts.append(session_context)

    # Layer 2: Current session messages
    buf = get_session_buffer()
    current_context = buf.format_for_injection(n=max_session_messages)
    if current_context:
        parts.append(current_context)

    # Layer 3: Caller-supplied context
    if caller_context:
        parts.append(f"[Additional context]\n{caller_context}")

    if not parts:
        return []

    combined = "\n\n".join(parts)

    # Compact if over budget
    combined, _ = await compact_structural(combined, threshold=max_context_tokens)

    # Final hard truncation safety net
    max_chars = max_context_tokens * 4  # rough tokens→chars
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n[... context truncated]"

    return [{"role": "system", "content": combined}]
