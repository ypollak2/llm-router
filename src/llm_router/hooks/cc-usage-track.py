# llm_router-hook-version: 9
"""PostToolUse[Agent] hook — track Claude Code subscription model calls.

Fires after every Agent subagent completes. Writes an estimated usage record
to the canonical llm_router SQLite database (~/.llm-router/usage.db) so the
dashboard shows CC subscription Agent calls alongside external API calls and
credits them against the right counterfactual baseline.

v9.4.0: switched from the stub ``llm_usage.db`` to the canonical ``usage.db``
and populates baseline_model + potential_cost_usd + saved_usd. The old DB is
now orphaned and may be removed manually.

Token estimation: input from prompt length, output from result length (chars/4).
Cost: 0.0 — CC subscription is flat-rate, not per-call billed.

Model: the one the sub-agent actually ran on. subagent_type does NOT choose it. Claude
Code runs a sub-agent on, in this order, and so does ``_resolve_model``:
  1. the model the host reports in ``tool_response`` (``resolvedModel``, else ``model``).
     ASSUMED, not observed: ``resolvedModel`` is proven only in the transcript's
     toolUseResult (3,132 of 3,132 records in a local scan), and no recorded PostToolUse
     Agent payload carrying it exists (the repo fixture has none). Absent -> next step;
  2. the Agent call's ``tool_input.model`` (alias sonnet/opus/haiku/fable, or a full id);
     an explicit ``inherit`` goes straight to step 4, an absent value goes to step 3;
  3. the ``model:`` frontmatter of the agent definition for subagent_type
     (``<cwd>/.claude/agents/<type>.md``, then ``~/.claude/agents/<type>.md``);
     no definition, no ``model:`` or ``inherit`` -> step 4;
  4. the parent session's model: the latest real (not sidechain, not ``<synthetic>``)
     assistant record in the tail of ``transcript_path``.
A value that is present but not a Claude model id or alias ends the search as UNKNOWN: it
is not "absent", and an older source must not stand in for it. Aliases come from
``llm_router.pricing.claude_code_model``; prices from ``llm_router.pricing`` only.

UNKNOWN is recorded, never guessed and never a zero. A sub-agent whose model is unknown,
or has no price (today ``claude-fable-5-2``), is written as a row with the model ("unknown"
when none), 0 tokens, NULL potential_cost_usd and NULL saved_usd, and reason
``claude_code_subscription:unknown_model:<why>``. Every money reader sums saved_usd (NULL
adds nothing) or prices tokens (0 adds nothing), so it cannot count as a saving, yet it
stays in the call count. Rows already in the ledger are not rewritten.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path

# -- KPI G1: record how long this invocation ran (llm_router.hook_latency) -----
# The clock starts BEFORE the first llm_router import, so the package import is
# inside the measurement. Armed only when run as a script: a test that imports
# this file must not register an exit-time write. Fail-open: no llm_router on
# the path means no row for this run; any other error is reported on stderr
# (never stdout, which the host parses) and the hook carries on.
import time as _hl_time

_HOOK_T0 = _hl_time.monotonic()
if __name__ == "__main__":
    try:
        from llm_router import hook_latency as _hook_latency

        _hook_latency.begin("cc-usage-track", "PostToolUse", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

# .env -> os.environ for this process (llm_router.env_loader). The real
# environment wins; without the package this is a no-op, as it always was.
try:
    from llm_router.env_loader import load_dotenv_files as _apply_dotenv
    _apply_dotenv()
except Exception:
    pass

try:
    from llm_router import pricing as _pricing
except ImportError:  # pragma: no cover — llm_router not importable from this interpreter
    # This file is COPIED to ~/.claude/hooks/ and run standalone, so unlike the
    # rest of the package its imports can genuinely fail — a half-removed
    # install, or a hook still registered against a deleted venv. Before WP-03
    # it imported nothing from llm_router, so this degradation path is new and has
    # to be explicit: with no price source the hook records NOTHING rather than
    # recording a zero. A zero here does not mean "no cost", it means "could not
    # price this", and writing it to usage.db would understate every cost and
    # inflate every savings figure derived from the row afterwards.
    _pricing = None  # type: ignore[assignment]

# The baseline column names the family the row was priced at.
_FAMILIES = ("haiku", "sonnet", "opus", "fable")

# No price table here: every price comes from llm_router.pricing at runtime.
# The old _PRICED_MODELS / _PRICES table was a second opinion that the audit
# flagged as a defect (WP-03).  Only the hook's guarded import path
# (_pricing) is used to fetch prices, so a host without llm_router still
# falls back to "no price → no row" rather than writing a stale rate.


def _is_synthetic_run() -> bool:
    """C-02. Is this a test or benchmark process? Explicit signals only.

    Inlined rather than imported: hooks are standalone scripts that must keep
    working when llm_router is not importable. Mirrors
    `routing_quality.detect_synthetic` exactly; a test pins them together.
    """
    import os as _os

    if _os.environ.get("LLM_ROUTER_SYNTHETIC", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return "PYTEST_CURRENT_TEST" in _os.environ


def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def _db_path() -> Path:
    """Resolve the canonical usage DB lazily so tests that patch Path.home() work."""
    return _router_home() / "usage.db"


_MAX_DEFINITION_BYTES = 8192
_MAX_TRANSCRIPT_TAIL_BYTES = 262144
_MODEL_ID = re.compile(r"claude-[a-z0-9][a-z0-9._-]{0,70}(\[1m\])?")

# A model value is one of: ABSENT (no value), INHERIT (the word), a MODEL (an alias or a
# full Claude id) or UNRECOGNIZED (present, but neither). The first two let the search go
# on; the last stops it.
_ABSENT, _INHERIT, _MODEL, _UNRECOGNIZED = "absent", "inherit", "model", "unrecognized"


def _classify(value: object) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value.strip():
        return _ABSENT, None
    v = value.strip().lower()
    if v == "inherit":
        return _INHERIT, None
    alias = getattr(_pricing, "claude_code_model", None)
    if alias is not None and (mapped := alias(v)) is not None:
        return _MODEL, mapped
    if _MODEL_ID.fullmatch(v):
        return _MODEL, v
    return _UNRECOGNIZED, None


def _read_regular(path: str, limit: int, *, tail: bool = False) -> str:
    """At most ``limit`` bytes of a REGULAR file (its end when ``tail``). Raises OSError or
    ValueError (NUL in the path) for anything else; the caller turns that into "no value"."""
    # Read-only (no O_CREAT); the 0o600 mode is inert here and tells the W0-4 state-write
    # ratchet (tests/test_w04_state_file_modes.py) this call cannot create a wide file.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        if tail and st.st_size > limit:
            os.lseek(fd, st.st_size - limit, os.SEEK_SET)
        return os.read(fd, limit).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _definition_model(subagent_type: object, cwd: object) -> tuple[str, str | None, str | None]:
    """``(kind, model, note)`` from the ``model:`` frontmatter of the agent definition.

    ``kind`` is absent when there is no usable definition or it sets no model (the agent
    then inherits). The type is matched against a strict name pattern before it touches a
    path; the file must resolve inside the agents directory (no symlink escape); the
    project definition wins over the user one. Any read problem is absent plus a note.
    """
    if not isinstance(subagent_type, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", subagent_type):
        return _ABSENT, None, None
    dirs = []
    if isinstance(cwd, str) and cwd.strip():
        dirs.append(Path(cwd) / ".claude" / "agents")
    dirs.append(Path.home() / ".claude" / "agents")
    note = None
    for d in dirs:
        try:
            root = os.path.realpath(d)
            real = os.path.realpath(d / f"{subagent_type}.md")
            if not os.path.exists(real):
                continue
            if os.path.commonpath([root, real]) != root:
                note = "definition_outside_agents_dir"
                continue
            text = _read_regular(real, _MAX_DEFINITION_BYTES)
        except (OSError, ValueError):
            note = "definition_unreadable"
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return _ABSENT, None, note
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = re.match(r"model\s*:\s*[\"']?([^\"'#\s]+)", line)
            if m:
                kind, model = _classify(m.group(1))
                return kind, model, note
        return _ABSENT, None, note
    return _ABSENT, None, note


def _parent_model(transcript_path: object) -> tuple[str, str | None, str | None]:
    """``(kind, model, note)`` of the latest real main-chain assistant record in the tail of
    the parent transcript. Sidechain (sub-agent) records and ``<synthetic>`` ones are skipped;
    if the latest real record names a model we do not recognise the answer is that, never an
    older record's model."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return _ABSENT, None, "no_transcript_path"
    try:
        tail = _read_regular(transcript_path, _MAX_TRANSCRIPT_TAIL_BYTES, tail=True)
    except (OSError, ValueError):
        return _ABSENT, None, "transcript_unreadable"
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        if not isinstance(msg, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        raw = msg.get("model")
        if not isinstance(raw, str) or raw.strip().lower() in ("", "<synthetic>"):
            continue
        kind, model = _classify(raw)
        return (kind, model, None) if kind in (_MODEL, _UNRECOGNIZED) else (_ABSENT, None, None)
    return _ABSENT, None, "no_main_chain_assistant_record_in_tail"


def _resolve_model(data: dict) -> tuple[str | None, str, str]:
    """``(model, source, why)``: the model the sub-agent ran on and where it came from, or
    ``(None, "none", <why unknown>)``. Never raises."""
    try:
        return _resolve_model_unsafe(data)
    except Exception as exc:  # noqa: BLE001 -- the hook must not break Claude Code
        return None, "none", f"resolver_error_{type(exc).__name__}"


def _resolve_model_unsafe(data: dict) -> tuple[str | None, str, str]:
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_result = data.get("tool_response", data.get("tool_result"))
    if isinstance(tool_result, dict):
        for key in ("resolvedModel", "model"):
            kind, model = _classify(tool_result.get(key))
            if kind == _MODEL:
                return model, "tool_response", ""
            if kind == _UNRECOGNIZED:
                return None, "tool_response", "unrecognized_model_in_tool_response"
    kind, model = _classify(tool_input.get("model"))
    if kind == _MODEL:
        return model, "tool_input", ""
    if kind == _UNRECOGNIZED:
        return None, "tool_input", "unrecognized_model_in_tool_input"
    notes: list[str] = []
    if kind == _ABSENT:  # explicit inherit skips the definition: the call said "use the parent's"
        kind, model, note = _definition_model(tool_input.get("subagent_type"), data.get("cwd"))
        if note:
            notes.append(note)
        if kind == _MODEL:
            return model, "definition", ""
        if kind == _UNRECOGNIZED:
            return None, "definition", "unrecognized_model_in_definition"
    kind, model, note = _parent_model(data.get("transcript_path"))
    if note:
        notes.append(note)
    if kind == _MODEL:
        return model, "parent", ""
    if kind == _UNRECOGNIZED:
        return None, "parent", "unrecognized_parent_model"
    return None, "none", "no_model_source" + "".join(f"_{n}" for n in notes)


def _infer_baseline(model: str) -> str | None:
    """Family the row is priced at: haiku, sonnet, opus or fable; None for any other id."""
    return next((f for f in _FAMILIES if f in model), None)


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimate counterfactual API cost — actual billing is $0 via subscription.

    Returns ``None`` when no price is available, which the caller must treat as
    "do not record", not as zero. See the guarded import at the top of the file.
    A model with no price is NOT priced as some other model.
    """
    if _pricing is None:
        return None
    p = _pricing.price_for(model, prompt_tokens=input_tokens)
    if p is None:
        return None
    return round((p.input * input_tokens + p.output * output_tokens) / 1_000_000, 8)


def _ensure_table(db: sqlite3.Connection) -> None:
    """Create the usage table only if it doesn't exist yet.

    The full schema lives in cost.py — it adds baseline_model, potential_cost_usd,
    saved_usd, and several other columns via migrations. To keep this hook
    forward-compatible when run before cost.py has touched the DB, we mirror
    those columns here. SQLite's ``IF NOT EXISTS`` makes this a no-op when the
    canonical schema is already present.
    """
    db.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            model TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'cc',
            task_type TEXT DEFAULT 'code',
            profile TEXT DEFAULT 'balanced',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            latency_ms REAL DEFAULT 0.0,
            success INTEGER DEFAULT 1,
            baseline_model TEXT,
            potential_cost_usd REAL DEFAULT 0.0,
            saved_usd REAL DEFAULT 0.0,
            complexity TEXT DEFAULT 'moderate',
            -- C-02: this hook creates its own compatible subset of the schema,
            -- so the column has to exist here too or the stamped INSERT fails
            -- against a table this hook created first.
            is_simulated INTEGER DEFAULT 0
        )"""
    )
    # P0.8: the session column cost.py adds by migration. This hook can be the first
    # writer on a database cost.py has not opened since the column was introduced.
    try:
        db.execute("ALTER TABLE usage ADD COLUMN session_id TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
    # P0.8-d: why the row was made (cost.MIGRATE_USAGE_ADD_REASON); same reason as above.
    # P1.10: the task and trace ids (cost.MIGRATE_ADD_TASK_IDENTITY); same reason again.
    for _col in ("reason", "task_id", "trace_id"):
        try:
            db.execute(f"ALTER TABLE usage ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _task_ids(session_id: str | None) -> tuple[str | None, str | None]:
    """``(task_id, trace_id)`` for a row written now (``call_identity.row_ids``): the session's
    current human turn, else a generated id. ``(None, None)`` when llm_router is not
    importable: the hook must still run, and NULL says "unknown" where a guess would not."""
    try:
        from llm_router import call_identity as _ci

        return _ci.row_ids(session_id)
    except Exception:  # noqa: BLE001
        return None, None


def _ledger_session_id(value: object) -> str | None:
    """The payload session id if it has the shape of an id, else None.

    Inlined, like ``_is_synthetic_run``: the hook must run when llm_router is not
    importable. Same rule as ``call_identity.ledger_session_id``; a test pins them.
    """
    import re as _re

    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return None
    return None if value.lower() in {"sdk", "unknown", "none", "null", "default"} else value


def _log_to_db(
    model: str | None,
    why_unknown: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    success: bool,
    session_id: str | None = None,
) -> None:
    db_path = _db_path()
    if not db_path.parent.exists():
        return
    try:
        with sqlite3.connect(str(db_path), timeout=5) as db:
            # M-06: busy_timeout BEFORE journal_mode -- it governs how long the
            # journal_mode PRAGMA waits for its own exclusive lock.
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("PRAGMA journal_mode=WAL")
            _ensure_table(db)
            potential = _estimate_cost(model, input_tokens, output_tokens) if model else None
            reason = "claude_code_subscription"
            baseline_model = _infer_baseline(model) if model else None
            if potential is None:
                # Unknown model, no price source (see the guarded import) or a model the
                # registry does not price (claude-fable-5-2): an explicit UNKNOWN row. 0
                # tokens and NULL money, so no reader can count it as a saving or as a
                # measured zero cost; the call itself stays visible (see the docstring).
                why = why_unknown or ("unpriced_model" if model else "no_model_source")
                reason = "claude_code_subscription:unknown_model:" + re.sub(r"[^A-Za-z0-9_.:-]", "_", why)[:80]
                baseline_model, input_tokens, output_tokens = None, 0, 0
            # cost_usd = 0 (subscription), so saved_usd = potential − 0 = potential
            # (NULL when unknown).
            saved = potential
            db.execute(
                # C-02: the SECOND writer to `usage`. The audit named only
                # cost.py's insert as "the single INSERT"; this one omitted
                # `is_simulated` too, so a benchmark run through the Claude Code
                # tracker was recorded as production spend.
                """INSERT INTO usage
                   (model, provider, task_type, profile,
                    input_tokens, output_tokens, cost_usd, latency_ms, success,
                    baseline_model, potential_cost_usd, saved_usd, is_simulated,
                    session_id, reason, task_id, trace_id)
                   VALUES (?, 'cc', 'code', 'balanced', ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?)""",
                (
                    model or "unknown",
                    input_tokens,
                    output_tokens,
                    latency_ms,
                    1 if success else 0,
                    baseline_model,
                    potential,
                    saved,
                    1 if _is_synthetic_run() else 0,
                    session_id or "unknown:hook",  # P0.8: never NULL; G3 counts unknown:* as missing
                    reason,
                    *_task_ids(session_id),
                ),
            )
            db.commit()
    except Exception as _exc:  # noqa: BLE001 — never let tracking break Claude Code
        # T-14: still fail-open, but no longer SILENT. A failed write here loses
        # a Claude Code usage row and reported nothing: no error, no log, no counter. 92
        # persistence sites had this shape; this is one of the ones that loses
        # data a user would notice missing.
        try:
            from llm_router import failopen as _fo
            _fo.record("CHZ-FO-HOOK-CC-USAGE-WRITE", _exc)
        except Exception:  # noqa: BLE001
            pass


# -- stdout is the host's JSON channel: library logging goes to stderr (docs/BUGS.md BIND-1) --------
# structlog's default sink is STDOUT. A log line in front of (or after) this hook's JSON makes Claude Code read
# the whole output as plain text and drop the decision or context it carried; agent-route.py lost 317 of 317
# Codex handoffs that way. configure_logging_lazily() points structlog at stdlib logging (stderr) the moment it
# is first imported, as auto-route.py does. The fallback covers a host without the llm_router package.
def _init_hook_logging() -> None:
    # LiteLLM keeps its own handler, which writes every record below WARNING to sys.stdout (looked up when the record
    # is emitted) and takes its level, DEBUG by default, from LITELLM_LOG when it is imported. ERROR keeps it off the
    # decision channel, and the hook FORCES it: an explicit LITELLM_LOG=DEBUG in the user's environment used to win
    # here, put LiteLLM's records in front of this hook's JSON, and the host dropped the whole output
    # (docs/bugs/BIND-10.md). The variable is set before the import, and the loggers on their own in case LiteLLM is
    # already imported. Only the script entry changes the process: a test that calls main() must not.
    if __name__ == "__main__":
        import logging as _ll_logging
        import os as _ll_os

        _ll_os.environ.update(LITELLM_LOG="ERROR")
        for _ll_name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            _ll_logging.getLogger(_ll_name).setLevel(_ll_logging.ERROR)
    try:
        from llm_router.logging import configure_logging_lazily

        configure_logging_lazily()
    except Exception:  # noqa: BLE001 -- logging setup must never abort the hook
        import logging as _logging

        _root = _logging.getLogger()
        if not _root.handlers:
            _root.addHandler(_logging.StreamHandler(sys.stderr))


def main() -> None:
    _init_hook_logging()
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    try:  # PG9: session id on this run's hook_latency row (from the parsed payload; never invented)
        from llm_router.hook_latency import set_session as _hl_set_session

        _hl_set_session(data.get("session_id") if isinstance(data, dict) else None)
    except Exception:  # noqa: BLE001 -- llm_router without set_session: no session on the row
        pass

    if not isinstance(data, dict):  # HOOKS-FAILOPEN-1: `[]`, `"x"`, `123` -> fail open like an empty payload
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name != "Agent":
        sys.exit(0)

    tool_input  = data.get("tool_input", {})
    tool_result = data.get("tool_response", data.get("tool_result", {}))

    prompt        = tool_input.get("prompt", "")
    result_text   = ""
    if isinstance(tool_result, dict):
        result_text = tool_result.get("output", tool_result.get("content", ""))
    elif isinstance(tool_result, str):
        result_text = tool_result

    model, _source, why_unknown = _resolve_model(data)
    if model is None:
        print(f"llm-router: cc-usage-track model unknown ({why_unknown})", file=sys.stderr)
    input_tokens  = max(1, len(prompt) // 4)
    output_tokens = max(1, len(result_text) // 4)
    latency_ms    = data.get("duration_ms", 0.0)

    _log_to_db(model, why_unknown, input_tokens, output_tokens, float(latency_ms), success=True,
               session_id=_ledger_session_id(data.get("session_id")))
    sys.exit(0)


if __name__ == "__main__":
    main()
