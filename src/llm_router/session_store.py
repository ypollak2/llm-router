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

import contextlib
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

# S2-3: there are TWO injectors, and this guard only knew about its own.
# `okf.inject_context` wraps retrieved documents in `<knowledge_context>`, and
# `router.py` rebinds `prompt = _okf.inject_context(prompt, concepts)` BEFORE
# dispatch — so `router.py:1977` recorded the rebound value as the user's turn.
# Every routed call with retrieval active wrote the retrieved documents into the
# conversation history as though the user had said them, and build_session_context
# served them back on later turns. Found in this machine's live shard as:
#
#     [user] <knowledge_context> ## [ModelCapability] gemini-2.5-pro ...
#
# Guarding here rather than at each call site: there are six writers, and a
# seventh must not be able to reopen the loop by not knowing about it.
_INJECTED_CTX_RE = re.compile(
    "(?:"
    + re.escape(SENTINEL_OPEN) + r".*?" + re.escape(SENTINEL_CLOSE)
    + r"|<knowledge_context>.*?</knowledge_context>"
    + ")",
    re.DOTALL | re.IGNORECASE,
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
        from llm_router.config_lite import config_value
        persist_raw = bool(config_value("llm_router_persist_raw"))
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


def _project_id(project_root: str | None = None, session_id: str | None = None) -> str:
    """Stable identifier for the current project scope.

    CTX-PROJ-1: with a *session_id* (and no explicit root/override) the project is
    the session's, not the writing process's: see ``_session_project_root``.

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
    if session_id:
        # CTX-PROJ-1: the session's pin outranks a caller-derived root. MCP/gateway callers pass the
        # session's LATEST cwd (mcp_roots._root_from_session_pointer); honouring it would put readers
        # at hash(worktree) while the writers stay at the pinned hash(repo).
        pinned = _session_project_root(session_id)
        if pinned:
            project_root = pinned
    if project_root:
        # An explicit root from the CALLER. The MCP server's cwd is $HOME, a
        # directory no session ever writes to, so deriving the bucket from the
        # server's own cwd looked up the wrong project every time — identity
        # resolved and the events were somewhere else. This does NOT merge
        # buckets: the caller declares which project it is asking about, which is
        # the isolation CHZ-AUD-024 pins, not a way around it.
        cwd = project_root
    else:
        try:
            cwd = os.getcwd()
        except Exception:
            cwd = os.path.expanduser("~")
    # S2-1: the REPO ROOT of the cwd, not the raw cwd. Hashing the raw cwd made
    # `repo/`, `repo/src/` and `repo/tests/` three different projects, and the
    # PostToolUse hook runs with whatever cwd the last tool call left behind — so a
    # session that touched several directories scattered its events across several
    # buckets while build_session_context read exactly one. Measured on this
    # machine: one session across 7 buckets, 299 events recorded, 176 readable.
    cwd = _repo_root_of(cwd)
    # Namespacing key, not a security hash (usedforsecurity=False → bandit B324).
    return hashlib.sha1(
        cwd.encode("utf-8", errors="ignore"), usedforsecurity=False
    ).hexdigest()[:16]


def _session_project_root(session_id: str) -> str | None:
    """The repo root this session is pinned to, or None (caller falls back to its cwd).

    CTX-PROJ-1 (D-57): hashing the writing process's cwd scattered one session id
    across several ``projects/<hash>/`` shards (Bash-spawned test processes that
    inherit the session id, a ``cd`` into a worktree). The hooks record the
    session's cwd in ``session_cwd_<sid>.json``; the first time that record exists
    its repo root is pinned in ``session_project_<sid>.json`` (O_EXCL, first wins),
    so later ``cd``s and the record's rewrites cannot move the session. The pin is
    deliberately not age-limited (``read_session_cwd`` is, by 12 h): a session
    resumed a day later must find its own file. First-write race: before the hook
    has written a record nothing is pinned and the writer's cwd is used (the old
    behaviour); events written in that window stay in that shard.
    """
    try:
        pin = _state_dir() / f"session_project_{_sanitize(session_id)}.json"
        try:
            root = json.loads(pin.read_text(encoding="utf-8")).get("project_root")
            if isinstance(root, str) and root:
                return root
        except Exception:  # noqa: BLE001 — no usable pin yet
            pass
        rec = json.loads(_session_cwd_path(session_id).read_text(encoding="utf-8"))
        cwd = str(rec.get("cwd") or "")
        if not cwd:
            return None
        root = _repo_root_of(cwd)
        try:
            pin.parent.mkdir(parents=True, exist_ok=True)
            # Atomic publish: write a private temp file, then os.link it to the pin path. link()
            # fails with FileExistsError if a winner exists, and a reader never sees a partial file.
            fd, tmp = tempfile.mkstemp(dir=str(pin.parent), prefix=".pin_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"project_root": root, "ts": time.time()}, fh)
                os.chmod(tmp, 0o600)
                os.link(tmp, pin)
                created = True
            except FileExistsError:
                created = False
            finally:
                with contextlib.suppress(OSError):  # our own temp file; the link (if any) is the pin
                    os.unlink(tmp)
            if not created:
                # Lost a race: the winner's pin is the truth (one retry covers a slow filesystem).
                for _ in range(2):
                    try:
                        won = json.loads(pin.read_text(encoding="utf-8")).get("project_root")
                        if isinstance(won, str) and won:
                            return won
                    except Exception:  # noqa: BLE001
                        time.sleep(0.02)
            else:
                _adopt_foreign_shards(session_id, root)
        except OSError:
            pass
        return root
    except Exception:  # noqa: BLE001 — absent/unreadable record: old behaviour
        return None


def _adopt_foreign_shards(session_id: str, root: str) -> None:
    """Pin just created for *session_id*: fold its pre-pin shards into the pinned project.

    A session already running at deploy wrote under hash(cwd-at-the-time); without this its older
    events (live and archived) would sit in other project dirs and never be archived or restored
    with the session. Reuses ``merge_session_shards`` (order by ts, de-duplicated, sources kept as
    ``*.premerge``) under the target's lock. Best-effort: a failure leaves the shards in place for
    ``llm-router sessions merge``.
    """
    try:
        pdir = _state_dir() / "projects" / hashlib.sha1(
            root.encode("utf-8", errors="ignore"), usedforsecurity=False
        ).hexdigest()[:16]
        name = f"session_context_{_sanitize(session_id)}.jsonl"
        projects = _state_dir() / "projects"
        live = sorted(p for p in projects.glob(f"*/{name}") if p.is_file())
        arch = sorted(p for p in projects.glob(f"*/archive/{name}") if p.is_file())
        target, atarget = pdir / name, pdir / "archive" / name
        if not any(p != target for p in live) and not any(p != atarget for p in arch):
            return
        with exclusive_lock(_lock_path(target)) as locked:
            if not locked:
                _note_lock_timeout("session adopt")
                return
            if any(p != target for p in live):
                merge_session_shards(session_id, apply=True, paths=live, target=target)
            if any(p != atarget for p in arch):
                merge_session_shards(session_id, apply=True, paths=arch, target=atarget)
    except Exception:  # noqa: BLE001
        pass


def _repo_root_of(start: str) -> str:
    """Nearest ancestor of *start* containing ``.git``, else *start* unchanged.

    Cross-project isolation is preserved: two different repos still hash to two
    different ids. Only subdirectories of the SAME repo are merged, which is what
    "project scope" was always supposed to mean.
    """
    try:
        here = Path(start).resolve()
    except Exception:
        return start
    for candidate in (here, *here.parents):
        try:
            if (candidate / ".git").exists():
                return str(candidate)
        except OSError:
            break
    return str(here)


def _project_dir(project_root: str | None = None, session_id: str | None = None) -> Path:
    """Project-scoped state dir: ``~/.llm-router/projects/<project_id>``."""
    return _state_dir() / "projects" / _project_id(project_root, session_id)


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"


def _session_path(session_id: str, project_root: str | None = None) -> Path:
    """Where this session's log lives, within the CURRENT project scope.

    Resolution deliberately stays scope-local. Following a session id across
    buckets was tried — a session id is arguably a stronger identity than a
    directory, and it would have consolidated the remaining fragmentation — but it
    breaks the isolation guarantee CHZ-AUD-024 pins: a project must not be able to
    read a session it does not own, even knowing its id. Reassembling more context
    is not worth handing one project a read path into another's conversation.

    What remains is bounded and by design: events recorded while the cwd was in a
    genuinely different project stay with that project. See ``_repo_root_of`` for
    the part that IS fixed — subdirectories of one repo no longer split.
    """
    return _project_dir(project_root, session_id) / f"session_context_{_sanitize(session_id)}.jsonl"


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
        # Project pointer first, then the machine-wide one. Both are needed:
        # the project pointer is keyed on cwd, and the MCP server's cwd is $HOME,
        # which is a directory no session ever writes to. Measured 2026-09-14 —
        # 30 pointers existed on this machine and the MCP server resolved NONE of
        # them, so every routed model ran with no session context at all.
        for ptr in (_pointer_path(), _global_pointer_path()):
            sid = _read_pointer(ptr)
            if sid:
                return sid
    except Exception:
        pass
    return None


def _global_pointer_path() -> Path:
    """Pointer that does not depend on the reader's working directory."""
    return _state_dir() / "current_session.json"


def _read_pointer(ptr: Path) -> str | None:
    """Session id from *ptr*, or None when missing, malformed or stale.

    Stale must mean None, never the old id: a pointer that outlives its session
    hands the next reader somebody else's conversation, which is a worse failure
    than having no context at all.
    """
    try:
        if not ptr.exists():
            return None
        data = json.loads(ptr.read_text(encoding="utf-8"))
        sid = data.get("session_id")
        ts = data.get("ts")
        if sid and isinstance(ts, (int, float)):
            if time.time() - ts <= _POINTER_MAX_AGE_SECONDS:
                return sid
    except Exception:                                        # noqa: BLE001
        pass
    return None


def write_pointer(session_id: str | None, cwd: str | None = None) -> None:
    """Write the ``current_session.json`` pointer file (best-effort).

    A defensive fallback only — distinct from and independent of LLM Router's
    legacy ``session_id.txt`` quota-bookkeeping marker, which this module
    never touches.
    """
    try:
        if not session_id:
            return
        payload = {"session_id": session_id, "ts": time.time()}
        # Both pointers, every time. The project one is keyed on cwd and a
        # session's cwd changes as work moves between repos; the machine-wide one
        # is what a reader with a different cwd — notably the MCP server, whose
        # cwd is $HOME — can actually find.
        _write_json_atomic(_pointer_path(), payload)
        _write_json_atomic(_global_pointer_path(), payload)
        # X4: the session's cwd, keyed by session id. The MCP server's own cwd
        # is $HOME (or /), and Claude Code sends no MCP roots, so this is how an
        # llm(...) call finds the project the caller is working in.
        if cwd:
            _write_json_atomic(_session_cwd_path(session_id), {"cwd": cwd, "ts": time.time()})
    except Exception:
        pass


_SESSION_CWD_MAX_AGE_S = 12 * 3600


def _session_cwd_path(session_id: str) -> Path:
    return _global_pointer_path().parent / f"session_cwd_{_sanitize(session_id)}.json"


def read_session_cwd(session_id: str | None) -> str | None:
    """The cwd the hook last saw for *session_id*, if recent and still a dir."""
    if not session_id:
        return None
    try:
        data = json.loads(_session_cwd_path(session_id).read_text(encoding="utf-8"))
        ts = data.get("ts")
        # A missing timestamp is unknown, not zero — and unknown is not fresh.
        if not isinstance(ts, (int, float)) or time.time() - ts > _SESSION_CWD_MAX_AGE_S:
            return None
        cwd = str(data.get("cwd") or "")
        return cwd if cwd and Path(cwd).is_dir() else None
    except Exception:  # noqa: BLE001 — absent or unreadable: no answer
        return None


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
        from llm_router.config_lite import config_value
        if not config_value("session_context_enabled"):
            return "off"
        share_external = config_value("session_context_share_external")
        return "all" if share_external else "local"
    except Exception:
        return "all"


#: Providers that keep session content on this machine (privacy mode `local`).
#: D-91/D-95: "codex" and "gemini_cli" are NOT here -- both are cloud services; it still receives the
#: request, but not session content under `local`. This is a privacy list only;
#: cost classes (_FREE_PROVIDERS etc.) are separate and still count codex as free.
LOCAL_PROVIDERS = ("local", "ollama")


def allows_session_content(target_provider: str | None) -> bool:
    """Whether session content may go to *target_provider* under :func:`get_mode`.

    The one rule for every path that sends session content to a model
    (``build_session_context`` here, ``context_pack.build_pack`` for its
    ``recent`` section): ``off`` allows nothing; ``local`` allows only
    :data:`LOCAL_PROVIDERS`.

    RED2-04: block context egress to ANY non-free-local provider under
    `local` (was a two-provider allowlist that let Perplexity through).

    S2-5: "local" itself belongs in the allowlist. The hook passes
    `target_provider="local"` — a category, not a provider name, meaning "the
    free-local draft chain", which the free-tier-drafts filter has already
    guaranteed. It was not in the list, so setting
    LLM_ROUTER_SESSION_CONTEXT=local silently returned "" for every draft: the
    privacy setting most likely to be chosen by someone who wants context to
    stay on the machine was the one that switched context off entirely, with
    no error. Masked until now only because the default mode is `all`.
    """
    mode = get_mode()
    if mode == "off":
        return False
    return not (mode == "local" and target_provider not in LOCAL_PROVIDERS)


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
        from llm_router.config_lite import config_value
        days = float(config_value("llm_router_persist_ttl_days"))
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
        for attempt in (0, 1):
            if attempt == 0:
                # CTX-PROJ-2: resolve once more BEFORE taking the lock. If the hook's cwd record
                # appeared since the first resolve, this call creates the pin and adopts the old
                # shards; doing that while holding the old shard's lock made adoption time out on
                # our own lock and strand the early events. Under the lock the pin almost always
                # exists, so the re-resolve below is a read; if the record lands in the tiny window
                # before the lock, adoption can still skip this shard (counted by
                # _note_lock_timeout; `llm-router sessions merge` recovers it).
                path = _session_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_lock(_lock_path(path)) as locked:
                if not locked:
                    _note_lock_timeout("session append")
                    return
                # NEVER create a pin / adopt from inside this lock: adoption locks the source shards
                # (0.5 s) and this lock file may be one of them, so it would time out on ourselves.
                # A pin created/merge applied while we waited can move the session's path: a stale
                # `open("a")` would recreate the OLD shard. Re-resolve under the lock, retry once.
                if attempt == 0 and _session_path(session_id) != path:
                    path = _session_path(session_id)
                    continue
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
                # S-09: create at 0600, not 0644-then-chmod. `open` creates with
                # `0666 & ~umask`, so on FIRST creation this file — which holds
                # scrubbed prompt transcripts — is world-readable for the whole
                # write, and anything that opens it inside that window keeps a
                # readable handle after the chmod. The chmod below stays: an opener
                # only sets the mode when it CREATES, so it is not a repair for a
                # file an older version already wrote at 0644.
                # NB: the BUILTIN open, not Path.open — Path.open takes no opener.
                from llm_router.paths import private_opener
                with open(path, "a", encoding="utf-8", opener=private_opener) as fh:
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
            return
    except Exception:
        pass


def _spill_to_archive(path: Path, lines: list[str]) -> None:
    """Append *lines* (original order) to the archive file of the session log at *path*, fsynced."""
    dest = path.parent / "archive" / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    from llm_router.paths import private_opener
    with open(dest, "ab", opener=private_opener) as out:
        if out.tell() > 0:
            with open(dest, "rb") as rf:
                rf.seek(-1, os.SEEK_END)
                if rf.read(1) != b"\n":
                    out.write(b"\n")
        out.write(("\n".join(lines) + "\n").encode("utf-8"))
        out.flush()
        os.fsync(out.fileno())


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

        # (line, parsed_ok) in file order. TTL-expired records are left out on purpose: B-02
        # retention is a physical delete, so they are NOT spilled to the archive.
        entries: list[tuple[str, bool]] = []
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)  # validate; keep raw line to avoid re-encoding cost
                except Exception:
                    entries.append((line, False))  # torn/corrupt: leaves live, spilled raw
                    continue
                if ttl_cutoff is not None:
                    try:
                        if float(parsed.get("ts", 0)) < ttl_cutoff:
                            continue  # B-02: physically deleted
                    except Exception:
                        pass
                entries.append((line, True))

        parsed_idx = [i for i, (_, ok) in enumerate(entries) if ok]
        cut = parsed_idx[-_COMPACT_TO] if len(parsed_idx) > _COMPACT_TO else 0
        newest = [ln for i, (ln, ok) in enumerate(entries) if ok and i >= cut]
        # CTX-COMPACT-1: everything else that leaves the live file goes to the archive, in order.
        dropped = [ln for i, (ln, ok) in enumerate(entries) if not (ok and i >= cut)]
        # CTX-COMPACT-1 (R-CTX-4): append the dropped lines to the archive and fsync BEFORE the live
        # file is replaced. A crash in between leaves them in both places (a duplicate that restore
        # removes); it can never leave them in neither. If the spill fails, do not compact at all.
        if dropped:
            try:
                _spill_to_archive(path, dropped)
            except Exception:
                return
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

def load_events(session_id: str | None, *, limit: int = 200,
                project_root: str | None = None) -> list[dict[str, Any]]:
    """Load up to ``limit`` newest events for *session_id*, oldest first.

    Tolerates torn/unparseable trailing lines. Fails open to ``[]``.
    """
    try:
        if not session_id:
            return []
        path = _session_path(session_id, project_root)
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
    project_root: str | None = None,
) -> str:
    """Assemble a compact, sentinel-wrapped context block for *session_id*.

    `project_root` names the project the CALLER is asking about. Without it the
    bucket is derived from this process's cwd, which for the MCP server is $HOME
    — a directory no session ever writes to. Measured 2026-09-15: the session id
    resolved correctly and the context came back EMPTY, because the events were
    in the writing project's bucket. Passing the caller's root is not a way around
    the isolation CHZ-AUD-024 pins; it is the caller declaring its own scope.

    Applies the privacy mode from :func:`get_mode` (returns ``""`` for
    ``off``, and for ``local`` when *target_provider* is an external paid
    API such as ``openai``/``gemini``). Keeps the 3 newest events
    unconditionally plus any older event matching *task_type* or sharing a
    keyword with *query*, formats them, dedupes repeated blocks, and
    truncates to *max_tokens*, keeping the newest events. Fails open to ``""``.
    """
    try:
        if not allows_session_content(target_provider):
            return ""

        records = load_events(session_id, limit=200, project_root=project_root)
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
        # Records are oldest -> newest, so keep the tail: the newest event is the
        # one a routed model most needs (P0.1; head-keeping dropped it first).
        # Imported here: token_budget pulls tiktoken (~10 ms), and the UserPromptSubmit
        # hook imports this module on every prompt but only renders context on some (D-171 A3).
        from llm_router.token_budget import truncate_to_budget

        text = truncate_to_budget(text, max_tokens, keep="tail")
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
        ) + list(state_dir.glob("projects/*/archive/session_context_*.jsonl"))
        # B-02: merge leftovers hold the same plaintext and age out on the same TTL.
        for pat in ("projects/*/session_context_*.jsonl.premerge",
                    "projects/*/archive/session_context_*.jsonl.premerge",
                    "projects/*/session_context_*.jsonl.merging",
                    "projects/*/archive/session_context_*.jsonl.merging"):
            stale += list(state_dir.glob(pat))
        for p in stale:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
        _sweep_orphan_pins(state_dir, cutoff)
    except Exception:
        pass


def _sweep_orphan_pins(state_dir: Path, cutoff: float) -> None:
    """Delete ``session_project_<sid>.json`` and ``session_cwd_<sid>.json`` older than *cutoff*
    whose session has no ``session_context_<sid>.jsonl`` anywhere (live or archive, any project)."""
    have: set[str] = set()
    for pat in ("session_context_*.jsonl", "projects/*/session_context_*.jsonl",
                "projects/*/archive/session_context_*.jsonl"):
        for p in state_dir.glob(pat):
            have.add(p.name[len("session_context_"):-len(".jsonl")])
    for prefix in ("session_project_", "session_cwd_"):
        for p in state_dir.glob(f"{prefix}*.json"):
            try:
                if p.name[len(prefix):-len(".json")] not in have and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError as exc:
                logging.getLogger(__name__).debug("pin sweep skipped %s: %s", p, exc)


# ---------------------------------------------------------------------------
# Shard recovery (S4)
# ---------------------------------------------------------------------------
# S2-1 stopped NEW fragmentation but deliberately left existing shards alone:
# consolidating them at runtime would be a cross-project read, which CHZ-AUD-024
# forbids. What makes recovery legitimate is who asks and when — the audit forbids
# a running project silently reading another project's session by id; it does not
# forbid the owner of the machine explicitly migrating their own store, which is
# what `okf adopt` and `okf gc` already do for the knowledge store.
#
# Hence: explicit CLI only, never a runtime path; dry-run by default; and the
# originating shards are left on disk, so a merge that turns out wrong costs disk
# rather than data.

def _shard_paths(session_id: str) -> list[Path]:
    """Every bucket holding a log for *session_id*, within this machine's store."""
    name = f"session_context_{_sanitize(session_id)}.jsonl"
    try:
        return sorted(
            p for p in (_state_dir() / "projects").glob(f"*/{name}")
            if p.is_file()
        )
    except OSError:
        return []


def _read_shard(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn trailing line — same tolerance as load_events
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def fragmented_sessions() -> dict[str, dict[str, Any]]:
    """Sessions whose events are split across more than one project bucket."""
    found: dict[str, dict[str, Any]] = {}
    try:
        root = _state_dir() / "projects"
        by_session: dict[str, list[Path]] = {}
        for p in root.glob("*/session_context_*.jsonl"):
            sid = p.name[len("session_context_"):-len(".jsonl")]
            by_session.setdefault(sid, []).append(p)
    except OSError:
        return found
    for sid, paths in by_session.items():
        counts = {p.parent.name: len(_read_shard(p)) for p in paths}
        counts = {k: v for k, v in counts.items() if v}
        if len(counts) > 1:
            total = sum(counts.values())
            found[sid] = {
                "shards": len(counts),
                "events": total,
                "readable": max(counts.values()),
                "stranded": total - max(counts.values()),
                "buckets": counts,
            }
    return found


def merge_session_shards(
    session_id: str,
    apply: bool = False,
    *,
    paths: list[Path] | None = None,
    target: Path | None = None,
) -> dict[str, Any]:
    """Consolidate every shard of *session_id* into the current project's log.

    Ordering is by recorded timestamp where events carry one, because a merged
    conversation with interleaved turns in the wrong order is worse than one that is
    merely split: it reads as a coherent history that never happened.

    Returns a report dict. With ``apply=False`` (the default) nothing is written —
    a merge that turns out to be wrong should be discovered before it is applied.
    """
    adopt = target is not None  # internal: fold foreign shards into an explicit target
    if paths is None:
        paths = _shard_paths(session_id)
    report: dict[str, Any] = {
        "session_id": session_id,
        "shards": len(paths),
        "events": 0,
        "merged": 0,
        "applied": False,
        "target": None,
    }
    if not paths:
        return report

    def _ts(rec: dict[str, Any]) -> float:
        raw = rec.get("ts", rec.get("timestamp", 0))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _collect(ps: list[Path]) -> list[str]:
        """Merged raw lines: de-duplicated, ts-ordered. Lines are kept byte-for-byte (so restore's
        byte-identical dedupe still matches); an unparseable line stays raw, in place (after the
        record that precedes it in its own shard), as ``_maybe_compact`` does."""
        seen: set[tuple[str, str, Any]] = set()
        items: list[tuple[float, int, str]] = []
        for p in ps:
            last_ts = 0.0
            try:
                raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in raw_lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    rec = None
                if isinstance(rec, dict):
                    # Shards overlap when the same turn was recorded from two cwds.
                    key = (rec.get("role", ""), (rec.get("content") or "").strip(), rec.get("ts"))
                    if key in seen:
                        continue
                    seen.add(key)
                    last_ts = _ts(rec)
                items.append((last_ts, len(items), line))
        items.sort(key=lambda t: (t[0], t[1]))
        return [t[2] for t in items]

    if target is None:
        target = _session_path(session_id)
    report["target"] = str(target)
    merged = _collect(paths)
    report["events"] = len(merged)
    report["merged"] = max(0, len(merged) - max(
        (len(_read_shard(p)) for p in paths), default=0
    ))
    if (len(paths) < 2 and not (adopt and paths and paths[0] != target)) or not apply:
        return report

    try:
        import contextlib as _cl
        with _cl.ExitStack() as stack:
            if not adopt:
                # CTX-PROJ-2: the CLI path replaces the target file, so a concurrent writer's
                # append (made under the target's lock) must not land between our read and the
                # replace. Adoption (target= given) is called with the target lock already held.
                if not stack.enter_context(exclusive_lock(_lock_path(target), timeout=5.0)):
                    _note_lock_timeout("session merge target")
                    return report
            # Writers append under the SOURCE's own lock. Hold it across read+rename, or an append
            # landing in between is never merged. Sorted order; a source we cannot lock quickly is
            # skipped (left in place) so adoption never blocks a hook.
            usable: list[Path] = []
            for p in sorted(paths):
                if p == target:
                    usable.append(p)
                    continue
                live = p.parent.parent / p.name if p.parent.name == "archive" else p
                if stack.enter_context(exclusive_lock(_lock_path(live), timeout=0.5)):
                    usable.append(p)
                else:
                    _note_lock_timeout("session merge source")
            if not any(p != target for p in usable):
                return report
            merged = _collect(usable)
            report["events"] = len(merged)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".jsonl.merging")
            with tmp.open("w", encoding="utf-8") as fh:
                for line in merged:
                    fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            # Target first, sources second: a crash between the two leaves duplicates (which the
            # next merge de-duplicates) rather than data only in *.premerge / *.merging.
            os.replace(str(tmp), str(target))
            # Originating shards are NOT removed (a bad merge costs disk, not the only copy);
            # cleanup_old_sessions ages the *.premerge files out on the retention TTL.
            for p in usable:
                if p != target:
                    try:
                        p.rename(p.with_suffix(".jsonl.premerge"))
                    except OSError:
                        pass
            report["applied"] = True
    except OSError:
        pass
    return report


def _archive_path(session_id: str, project_root: str | None = None) -> Path:
    """Where SessionEnd parks *session_id*'s log until the session resumes or ages out."""
    return _project_dir(project_root, session_id) / "archive" / f"session_context_{_sanitize(session_id)}.jsonl"


def archive_session(session_id: str | None) -> None:
    """Take *session_id*'s log out of the live store at session end (best-effort).

    CONTEXT-RESUME-1: every ``claude --resume`` / ``--continue`` is its own SessionStart..SessionEnd
    lifecycle under the SAME session id, so deleting here left the resumed session an empty store.
    The log is now moved to ``projects/<id>/archive/`` (merged after any earlier archive of the same
    id) and ``restore_session`` brings it back on ``SessionStart source=resume``. A session that is
    never resumed leaves one file that ``cleanup_old_sessions`` removes after ``_TTL_DAYS``. With the
    session-context kill switch off nothing is retained: the live log is deleted as before.
    """
    try:
        if not session_id:
            return
        path = _session_path(session_id)
        if not path.exists():
            return
        if get_mode() == "off":
            path.unlink()
            return
        dest = _archive_path(session_id)
        with exclusive_lock(_lock_path(path)) as locked:
            if not locked:
                _note_lock_timeout("session archive")
                return
            if not path.exists():
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                with open(dest, "ab") as out, open(path, "rb") as src:
                    if out.tell() > 0:
                        with open(dest, "rb") as rf:
                            rf.seek(-1, os.SEEK_END)
                            if rf.read(1) != b"\n":
                                out.write(b"\n")
                    out.write(src.read())
                    out.flush()
                    os.fsync(out.fileno())  # CTX-COMPACT-1: durable before the live copy goes
                path.unlink()
            else:
                os.replace(path, dest)
            os.utime(dest, None)  # the retention clock starts at archive time, not last write
    except Exception:
        pass


def restore_session(session_id: str | None) -> bool:
    """Bring *session_id*'s archived log back as the live log (SessionStart source=resume).

    The archive goes first, then anything already recorded live under this id, so the line count
    never drops across a resume. The archive is consumed. Returns True if something was restored.
    """
    try:
        if not session_id:
            return False
        arch = _archive_path(session_id)
        if not arch.exists() or get_mode() == "off":
            return False
        path = _session_path(session_id)
        with exclusive_lock(_lock_path(path)) as locked:
            if not locked:
                _note_lock_timeout("session restore")
                return False
            if not arch.exists():
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            data = arch.read_bytes()
            if data and not data.endswith(b"\n"):
                data += b"\n"
            if path.exists():
                # CTX-COMPACT-1: a crash mid-spill / mid-restore can leave an event in both files;
                # drop live lines that are byte-identical to an archive line (same ts + hash).
                seen = set(data.split(b"\n"))
                live = path.read_bytes()
                if live and not live.endswith(b"\n"):
                    live += b"\n"
                data += b"".join(
                    ln + b"\n" for ln in live.split(b"\n") if ln and ln not in seen
                )
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".restore_", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            arch.unlink()
        return True
    except Exception:
        return False
