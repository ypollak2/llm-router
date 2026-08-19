"""Session Context Accumulator — durable, cross-process session event store.

LLM Router builds up session context (user prompts, tool calls, routed Q&A) in a
durable per-session JSONL log so that cheap routed models can answer with
real context instead of fabricating it. Events are written by hooks
(``session-start.py``, ``auto-route.py``, ``context-capture.py``,
``session-end.py``) and by the router (``router.py``) as routed calls
complete, then re-assembled into a compact context block that gets injected
into every routed model call — both the MCP server path
(``context.build_context_messages``) and the hook draft path
(``hooks/direct_executor.execute_chain``/``execute_agent``).

Storage: one JSONL file per session at
``~/.llm-router/session_context_{sanitized_session_id}.jsonl``. Appends are plain
``open(path, "a")`` writes (POSIX near-atomic for small writes); readers
tolerate a torn/unparseable trailing line. Compaction and the current-session
pointer file use an atomic same-directory-temp-file + ``os.replace()`` write
(the ``_write_json_atomic`` pattern already used by ``enforce-route.py`` and
``auto-route.py``).

Every public function in this module is fail-open: any error is caught and
the function degrades to a no-op / empty result rather than raising, so a
storage problem here can never block routing, hooks, or Claude Code itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from llm_router.compaction import collapse_whitespace, dedup_sections
from llm_router.file_lock import exclusive_lock
from llm_router.paths import llm_router_home
from llm_router.token_budget import truncate_to_budget

_log = logging.getLogger("llm_router.session_store")

#: RED5-03: lock acquisitions that timed out and were therefore declined.
#: Counted rather than merely logged, so "did we lose writes under load?" has an
#: answer that does not require grepping logs nobody kept.
_lock_timeouts = 0


def lock_timeout_count() -> int:
    return _lock_timeouts


def _note_lock_timeout(what: str) -> None:
    global _lock_timeouts
    _lock_timeouts += 1
    _log.warning(
        "SESSION_LOCK_TIMEOUT (%s): declined rather than proceeding unlocked "
        "(timeouts this process: %d)",
        what,
        _lock_timeouts,
    )

# ── Self-injection guard ─────────────────────────────────────────────────────
# Context blocks we inject are wrapped in this sentinel. When recording new
# events (e.g. a routed model's own reply, or a tool result that happens to
# echo back a prior context block) we strip anything sentinel-wrapped first,
# so injected context never gets re-captured and re-injected into itself.
SENTINEL_OPEN = "[llm_router-session-context]"
SENTINEL_CLOSE = "[/llm_router-session-context]"
_INJECTED_CTX_RE = re.compile(
    re.escape(SENTINEL_OPEN) + r".*?" + re.escape(SENTINEL_CLOSE),
    re.DOTALL,
)

# ── Tunables ──────────────────────────────────────────────────────────────
_MAX_RECORD_CHARS = 2000       # per-event content cap before writing
_MAX_FILE_BYTES = 256 * 1024   # compact once the JSONL file exceeds this
_MAX_RECORDS = 300             # ...or once it holds more than this many lines
_COMPACT_TO = 150              # ...keep only the newest N records
_TTL_DAYS = 7                  # cleanup_old_sessions() default max age
_POINTER_MAX_AGE_SECONDS = 6 * 3600  # ignore current_session.json if stale

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "for", "and", "or", "with", "this", "that", "it", "as", "at",
    "by", "from", "i", "you", "my", "me", "do", "does", "did", "can", "will",
    "would", "should", "what", "how", "why", "not", "if", "then", "so",
    "just", "have", "has", "had",
}

_WORD_RE = re.compile(r"[a-z0-9]+")

# Credential patterns scrubbed from event content before it is persisted or
# later injected into routed (incl. external) model calls. Inline substitution
# preserves the surrounding prompt/response for context while stripping
# secrets. Mirrors llm_router.library.store.scrub_secrets.
_SECRET_PATTERNS = [
    re.compile(r"\b[A-Z][A-Z0-9_]*_(?:API_)?KEY\s*[=:]\s*\S+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
]


def _scrub_secrets(text: str) -> str:
    """Redact credential patterns from event content before persistence.

    D-01/B-03: routes through the shared ``persist_redact()`` (layers the
    structured PII patterns, the canonical ``secret_scrubber.scrub_text``,
    and a broadened unanchored "prose secret" pass, e.g. "the launch code is
    ORANGE-742"), gated by ``LLM_ROUTER_PERSIST_RAW`` / ``LLM_ROUTER_PERSIST_REDACTION``.
    Falls back to calling ``secret_scrubber.scrub_text`` directly if the
    redaction module can't be imported (config unavailable, etc).

    The local ``_SECRET_PATTERNS`` belt-and-suspenders pass still runs
    afterward — UNLESS ``LLM_ROUTER_PERSIST_RAW=1``, in which case skipping it is
    required for the raw opt-in escape hatch to actually mean raw.
    """
    try:
        from llm_router.persist_redaction import persist_redact
        text = persist_redact(text)
    except Exception:
        try:
            from llm_router.secret_scrubber import scrub_text
            text = scrub_text(text)
        except Exception:
            pass

    try:
        from llm_router.config import get_config
        persist_raw = bool(getattr(get_config(), "llm_router_persist_raw", False))
    except Exception:
        persist_raw = False

    if not persist_raw:
        # Belt-and-suspenders: local patterns (PEM etc.) as a fallback / extra pass.
        for pat in _SECRET_PATTERNS:
            text = pat.sub("[REDACTED]", text)
    return text


# ── Paths ─────────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    """The llm_router state directory, resolved at call time.

    Delegates to :func:`llm_router.paths.llm_router_home` so there is ONE answer to "where
    does state live". This used to be ``os.path.expanduser("~") / ".llm-router"``, which
    honoured only the ``HOME`` environment variable — and therefore neither of the two
    sandbox mechanisms actually in use:

    * ``LLM_ROUTER_HOME`` (the canonical one, which :func:`llm_router.paths.is_isolated`
      reports on) was ignored outright, so ``is_isolated()`` returned True while
      session events were written to and read from the real home;
    * replacing the ``pathlib.Path.home`` METHOD — what this repo's conftest does, and
      88 test files rely on — does not change ``os.path.expanduser``, so that was
      ignored too.

    The consequence was not theoretical: a full-suite run read the developer's live
    ``session_context_*.jsonl`` and injected real prompt and model-output text into a
    test's messages. See ``tests/test_p0_session_store_isolation.py``.
    """
    return llm_router_home()


def _project_id() -> str:
    """Stable identifier for the current project scope.

    Precedence: ``$LLM_ROUTER_PROJECT_ID`` (explicit override) → a short hash of
    the current working directory. Cross-project isolation depends on this:
    session-context files for one project live under a different subdirectory
    than another's, so Project B cannot enumerate or load Project A's context
    without knowing the exact session id *and* sharing its project scope.
    """
    try:
        explicit = os.environ.get("LLM_ROUTER_PROJECT_ID", "").strip()
        if explicit:
            return re.sub(r"[^A-Za-z0-9._-]", "_", explicit)[:64] or "default"
    except Exception:
        pass
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = os.path.expanduser("~")
    # Namespacing key, not a security hash (usedforsecurity=False → bandit B324).
    return hashlib.sha1(
        cwd.encode("utf-8", errors="ignore"), usedforsecurity=False
    ).hexdigest()[:16]


def _project_dir() -> Path:
    """Project-scoped state dir: ``~/.llm-router/projects/<project_id>``."""
    return _state_dir() / "projects" / _project_id()


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"


def _session_path(session_id: str) -> Path:
    return _project_dir() / f"session_context_{_sanitize(session_id)}.jsonl"


def _lock_path(path: Path) -> Path:
    """Sibling lock file for *path* (a JSONL data file).

    Deliberately a *different* file from the data file itself: compaction
    swaps the data file's inode via ``os.replace()``, and locking that inode
    directly would leave a stale/orphaned lock on the replaced-away copy.
    The lock file's own identity is never swapped, so it stays a stable
    synchronization point across the whole append-then-maybe-compact
    critical section (CHZ-AUD-C-01).
    """
    return path.with_name(path.name + ".lock")


def _pointer_path() -> Path:
    return _project_dir() / "current_session.json"


# ── Atomic JSON write (same pattern as enforce-route.py / auto-route.py) ───

def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON to *path* via a same-directory temp file + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Session id resolution ───────────────────────────────────────────────────

def resolve_session_id(explicit: str | None = None) -> str | None:
    """Resolve the current Claude Code session id.

    Precedence: explicit param → ``$CLAUDE_SESSION_ID`` env →
    ``$CLAUDE_CODE_SESSION_ID`` env → pointer file
    ``~/.llm-router/current_session.json`` (ignored if written more than 6h ago)
    → ``None``.
    """
    try:
        if explicit:
            return explicit
        env = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if env:
            return env
        env2 = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if env2:
            return env2
        ptr = _pointer_path()
        if ptr.exists():
            data = json.loads(ptr.read_text(encoding="utf-8"))
            sid = data.get("session_id")
            ts = data.get("ts")
            if sid and isinstance(ts, (int, float)):
                if time.time() - ts <= _POINTER_MAX_AGE_SECONDS:
                    return sid
    except Exception:
        pass
    return None


def write_pointer(session_id: str | None) -> None:
    """Write the ``current_session.json`` pointer file (best-effort).

    A defensive fallback only — distinct from and independent of LLM Router's
    legacy ``session_id.txt`` quota-bookkeeping marker, which this module
    never touches.
    """
    try:
        if not session_id:
            return
        _write_json_atomic(
            _pointer_path(), {"session_id": session_id, "ts": time.time()},
        )
    except Exception:
        pass


# ── Privacy mode ────────────────────────────────────────────────────────────

def get_mode() -> str:
    """Resolve the session-context privacy mode: 'all' | 'local' | 'off'.

    ``LLM_ROUTER_SESSION_CONTEXT`` env var wins if set (``on``/``all`` → all,
    ``local`` → local, ``off`` → off). Otherwise falls back to
    ``RouterConfig.session_context_enabled`` /
    ``session_context_share_external``. Fails open to ``"all"``.
    """
    try:
        env = os.environ.get("LLM_ROUTER_SESSION_CONTEXT", "").strip().lower()
        if env in ("on", "all"):
            return "all"
        if env == "local":
            return "local"
        if env == "off":
            return "off"
    except Exception:
        pass
    try:
        from llm_router.config import get_config
        config = get_config()
        if not getattr(config, "session_context_enabled", True):
            return "off"
        share_external = getattr(config, "session_context_share_external", True)
        return "all" if share_external else "local"
    except Exception:
        return "all"


# ── Recording ────────────────────────────────────────────────────────────────

def _content_hash(content: str) -> str:
    # Change-detection/dedup key over already-secret-scrubbed text. SHA-256 (not
    # SHA-1) so CodeQL py/weak-sensitive-data-hashing stays clean even though the
    # input is sanitised upstream by _scrub_secrets().
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _last_record(path: Path) -> dict[str, Any] | None:
    """Best-effort read of the last well-formed JSON line in *path*."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return None
            back = min(size, 8192)
            fh.seek(-back, os.SEEK_END)
            chunk = fh.read()
        for line in reversed(chunk.split(b"\n")):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
        return None
    except Exception:
        return None


def _first_record(path: Path) -> dict[str, Any] | None:
    """Best-effort read of the FIRST well-formed JSON line in *path*.

    Records are always appended in chronological order, so the first line's
    age is a cheap proxy for "does this file contain ANY TTL-expired record"
    — avoids a full-file scan on every append just to check retention.
    """
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except Exception:
                    continue
        return None
    except Exception:
        return None


def _persist_ttl_seconds() -> float:
    """Global physical-retention TTL (``LLM_ROUTER_PERSIST_TTL_DAYS``), in seconds.

    B-02: independent of ``cleanup_old_sessions()``'s whole-FILE mtime-based
    ``_TTL_DAYS`` sweep below — this governs per-RECORD physical deletion
    during compaction, so a long-lived session file can't accumulate records
    well past the retention window just because the session itself stays
    active. 0 (or lower) disables purging.
    """
    try:
        from llm_router.config import get_config
        days = float(getattr(get_config(), "llm_router_persist_ttl_days", 30))
    except Exception:
        days = 30.0
    return max(days, 0.0) * 86_400


def purge_expired(session_id: str | None) -> None:
    """Public helper: physically purge TTL-expired records for *session_id*.

    Runs the same per-record TTL check ``_maybe_compact()`` performs
    automatically after every ``record_event()`` call — exposed directly so
    tests / external maintenance passes can force a purge without waiting
    for (or synthesizing) another append.
    """
    try:
        if not session_id:
            return
        path = _session_path(session_id)
        if not path.exists():
            return
        # RED5-03: see the note at the append site. Compaction rewrites the file
        # from a snapshot; doing that without the lock is how a concurrent
        # append gets os.replace()'d out of existence.
        with exclusive_lock(_lock_path(path)) as locked:
            if not locked:
                _note_lock_timeout("session compaction")
                return
            _maybe_compact(path)
    except Exception:
        pass


def record_event(
    session_id: str | None,
    kind: str,
    content: str,
    *,
    role: str = "user",
    task_type: str = "",
    tool: str | None = None,
    model: str | None = None,
    max_chars: int = _MAX_RECORD_CHARS,
) -> None:
    """Append one event to the session's durable JSONL log.

    ``kind`` is a free-form label (``user_prompt``, ``tool_call``,
    ``routed_qa``, ``assistant``, ...) used later to format/filter events.
    Fails open: any error (bad session_id, unwritable disk, etc.) is a no-op.
    """
    try:
        if not session_id or not content:
            return
        # Privacy kill-switch: with LLM_ROUTER_SESSION_CONTEXT=off (or config
        # session_context_enabled=false) nothing is persisted to disk.
        if get_mode() == "off":
            return
        text = _INJECTED_CTX_RE.sub("", content).strip()
        if not text:
            return
        text = collapse_whitespace(text)
        if len(text) > max_chars:
            text = text[:max_chars]
        # Strip credentials before they hit disk / later external injection.
        text = _scrub_secrets(text)
        text = text.strip()
        if not text:
            return

        content_hash = _content_hash(text)
        path = _session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # CHZ-AUD-C-01: the append (dedupe-check + write) and any triggered
        # compaction must run as one atomic unit across processes. Without
        # this lock, a concurrent process's compaction can read a stale
        # snapshot of the file (taken before this process's append lands),
        # then os.replace() the file with that stale snapshot — silently
        # dropping this process's just-written record even though the write
        # itself succeeded. Held for the duration of both the append and any
        # compaction it triggers; a sibling `.lock` file is used rather than
        # locking the JSONL itself, so compaction's os.replace() swap of the
        # data file's inode never disturbs the lock's identity.
        # RED5-03: the yielded boolean is BOUND, not discarded. exclusive_lock
        # yields False when acquisition times out and degrades to "unlocked"
        # rather than raising — a sane default for best-effort callers, and the
        # wrong one here. Running this block unlocked is precisely the race the
        # comment above describes, so an unlocked run does not silently do the
        # dangerous thing: it declines, counts, and says so.
        with exclusive_lock(_lock_path(path)) as locked:
            if not locked:
                _note_lock_timeout("session append")
                return
            prev = _last_record(path)
            if prev and prev.get("h") == content_hash:
                return  # consecutive-duplicate dedupe

            record = {
                "ts": time.time(),
                "kind": kind,
                "role": role,
                "task_type": task_type or "",
                "tool": tool,
                "model": model,
                "content": text,
                "h": content_hash,
            }
            with path.open("a", encoding="utf-8") as fh:
                # NB: `text` is secret-scrubbed by _scrub_secrets() above
                # (~L309) and the file is chmod 0600 / local-only. Storing
                # scrubbed session context in clear text is the
                # accumulator's purpose (routed models read it back). The
                # CodeQL py/clear-text-storage-sensitive-data alert here is
                # a reviewed false positive (dismissed) — the regex
                # scrubber just isn't modelled as a sanitizer by CodeQL.
                fh.write(json.dumps(record) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

            _maybe_compact(path)
    except Exception:
        pass


def _maybe_compact(path: Path) -> None:
    """Rewrite *path* to keep only its newest ``_COMPACT_TO`` records once it
    exceeds ``_MAX_FILE_BYTES`` or ``_MAX_RECORDS`` lines, or once its oldest
    record exceeds the persistence TTL.

    B-02: TTL enforcement here is a PHYSICAL delete — expired lines are
    dropped from the rewrite and are gone from the on-disk bytes, not just
    filtered at read time. Survives a fresh process/store instance because
    it operates on the file itself, not in-memory state.
    """
    try:
        try:
            size = path.stat().st_size
        except OSError:
            return

        need_compact = size > _MAX_FILE_BYTES
        if not need_compact:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                count = sum(1 for _ in fh)
            need_compact = count > _MAX_RECORDS

        ttl_seconds = _persist_ttl_seconds()
        ttl_cutoff = time.time() - ttl_seconds if ttl_seconds > 0 else None
        if not need_compact and ttl_cutoff is not None:
            first = _first_record(path)
            if first is not None:
                try:
                    if float(first.get("ts", 0)) < ttl_cutoff:
                        need_compact = True
                except Exception:
                    pass
        if not need_compact:
            return

        records: list[str] = []
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)  # validate; keep raw line to avoid re-encoding cost
                except Exception:
                    continue
                if ttl_cutoff is not None:
                    try:
                        if float(parsed.get("ts", 0)) < ttl_cutoff:
                            continue  # B-02: physically drop expired record
                    except Exception:
                        pass
                records.append(line)

        newest = records[-_COMPACT_TO:]
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for line in newest:
                    handle.write(line + "\n")
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


# ── Reading ──────────────────────────────────────────────────────────────────

def load_events(session_id: str | None, *, limit: int = 200) -> list[dict[str, Any]]:
    """Load up to ``limit`` newest events for *session_id*, oldest first.

    Tolerates torn/unparseable trailing lines. Fails open to ``[]``.
    """
    try:
        if not session_id:
            return []
        path = _session_path(session_id)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
        if limit and len(records) > limit:
            records = records[-limit:]
        return records
    except Exception:
        return []


def _format_record(rec: dict[str, Any]) -> str:
    kind = rec.get("kind", "")
    content = rec.get("content", "")
    if kind == "user_prompt":
        return f"USER: {content}"
    if kind == "tool_call":
        tool = rec.get("tool") or "tool"
        return f"TOOL({tool}): {content}"
    if kind == "routed_qa":
        model = rec.get("model") or "model"
        return f"ROUTED({model}): {content}"
    if kind == "assistant":
        return f"ASSISTANT: {content}"
    return str(content)


def _keywords(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall(text.lower())
        if w not in _STOPWORDS and len(w) > 2
    }


def build_session_context(
    session_id: str | None,
    *,
    max_tokens: int = 1500,
    task_type: str | None = None,
    query: str | None = None,
    target_provider: str | None = None,
) -> str:
    """Assemble a compact, sentinel-wrapped context block for *session_id*.

    Applies the privacy mode from :func:`get_mode` (returns ``""`` for
    ``off``, and for ``local`` when *target_provider* is an external paid
    API such as ``openai``/``gemini``). Keeps the 3 newest events
    unconditionally plus any older event matching *task_type* or sharing a
    keyword with *query*, formats them, dedupes repeated blocks, and
    truncates to *max_tokens*. Fails open to ``""``.
    """
    try:
        mode = get_mode()
        if mode == "off":
            return ""
        # RED2-04: block context egress to ANY non-free-local provider under
        # `local` (was a two-provider allowlist that let Perplexity through).
        if mode == "local" and target_provider not in ("ollama", "codex", "gemini_cli"):
            return ""

        records = load_events(session_id, limit=200)
        if not records:
            return ""

        query_words = _keywords(query) if query else set()

        keep_ids: set[int] = set()
        newest_first = list(reversed(records))
        for idx, rec in enumerate(newest_first):
            if idx < 3:
                keep_ids.add(id(rec))
                continue
            if task_type and rec.get("task_type", "") == task_type:
                keep_ids.add(id(rec))
                continue
            if query_words:
                if query_words & _keywords(rec.get("content", "")):
                    keep_ids.add(id(rec))

        ordered = [rec for rec in records if id(rec) in keep_ids]
        lines = [_format_record(rec) for rec in ordered]
        text = "\n".join(ln for ln in lines if ln.strip())
        if not text:
            return ""

        text = dedup_sections(text)
        text = truncate_to_budget(text, max_tokens)
        if not text.strip():
            return ""

        return f"{SENTINEL_OPEN}\n{text}\n{SENTINEL_CLOSE}"
    except Exception:
        return ""


# ── Lifecycle ────────────────────────────────────────────────────────────────

def cleanup_old_sessions(max_age_days: int = _TTL_DAYS) -> None:
    """Delete session_context_*.jsonl files not modified in *max_age_days*."""
    try:
        state_dir = _state_dir()
        if not state_dir.exists():
            return
        cutoff = time.time() - max_age_days * 86400
        # Match both legacy flat files and project-scoped subdirs.
        stale = list(state_dir.glob("session_context_*.jsonl")) + list(
            state_dir.glob("projects/*/session_context_*.jsonl")
        )
        for p in stale:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def archive_session(session_id: str | None) -> None:
    """Remove *session_id*'s durable log at session end (best-effort)."""
    try:
        if not session_id:
            return
        path = _session_path(session_id)
        if path.exists():
            path.unlink()
    except Exception:
        pass
