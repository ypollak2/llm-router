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
Code runs a sub-agent on, in this order, and so does ``_infer_model``:
  1. the model the host reports in ``tool_response`` (``resolvedModel``, else ``model``);
  2. the Agent call's ``tool_input.model`` (alias sonnet/opus/haiku/fable, or a full id);
  3. the ``model:`` frontmatter of the agent definition for subagent_type
     (``<cwd>/.claude/agents/<type>.md``, then ``~/.claude/agents/<type>.md``);
  4. the parent session's model (last assistant message in ``transcript_path``).
``inherit`` (or no value) at a step falls through to the next. When none of them names
a model the model is UNKNOWN and NO ROW IS WRITTEN: guessing a model would credit a
made-up counterfactual as savings. The same holds for a model with no price in
``llm_router.pricing`` (today ``claude-fable-5-2``): no price, no row, never a 0.00.
Aliases map to the current ids in ``_CURRENT_ALIASES``; prices come from
``llm_router.pricing`` only. Rows already in the ledger are not rewritten.
"""

from __future__ import annotations

import json
import sqlite3
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

# Claude Code's model aliases -> the current model ids (the Claude 5 family). The
# registry's own aliases ("sonnet" -> claude-sonnet-5, "haiku" -> claude-haiku-4-5)
# lag the current models, so they are not used here. An id with no price in
# llm_router.pricing (claude-fable-5-2 today) is still named, and the row is then
# skipped by _estimate_cost: the id is not invented away to a cheaper stand-in.
_CURRENT_ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-2",
}

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


def _as_model(value: object) -> str | None:
    """A model id from an alias ("sonnet") or a full id; None for ``inherit``/unknown text."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or v == "inherit":
        return None
    if v in _CURRENT_ALIASES:
        return _CURRENT_ALIASES[v]
    if v.startswith("claude-") and len(v) <= 80:
        return v
    return None


def _definition_model(subagent_type: object, cwd: object) -> str | None:
    """``model:`` frontmatter of the sub-agent's definition file, or None.

    The type is matched against a strict name pattern before it touches a path.
    The project definition wins over the user one, as in Claude Code.
    """
    import re as _re

    if not isinstance(subagent_type, str) or not _re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", subagent_type):
        return None
    dirs = []
    if isinstance(cwd, str) and cwd.strip():
        dirs.append(Path(cwd) / ".claude" / "agents")
    dirs.append(Path.home() / ".claude" / "agents")
    for d in dirs:
        try:
            text = (d / f"{subagent_type}.md").read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = _re.match(r"model\s*:\s*[\"']?([^\"'#\s]+)", line)
            if m:
                return _as_model(m.group(1))
        return None  # the definition exists and sets no model: it inherits
    return None


def _parent_model(transcript_path: object) -> str | None:
    """Model of the last assistant message in the parent session transcript, or None."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 262144))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        if isinstance(msg, dict) and rec.get("type") == "assistant":
            model = _as_model(msg.get("model"))
            if model:
                return model
    return None


def _infer_model(data: dict) -> str | None:
    """The model the sub-agent ran on, or None when nothing in hand names it."""
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_result = data.get("tool_response", data.get("tool_result"))
    if isinstance(tool_result, dict):
        for key in ("resolvedModel", "model"):
            model = _as_model(tool_result.get(key))
            if model:
                return model
    return (
        _as_model(tool_input.get("model"))
        or _definition_model(tool_input.get("subagent_type"), data.get("cwd"))
        or _parent_model(data.get("transcript_path"))
    )


def _infer_baseline(model: str) -> str:
    """Family the row is priced at: haiku, sonnet, opus or fable."""
    return next((f for f in _FAMILIES if f in model), "sonnet")


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
    model: str,
    baseline_model: str,
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
            potential = _estimate_cost(model, input_tokens, output_tokens)
            if potential is None:
                # No price: no price source (see the guarded import) or a model the
                # registry does not price (claude-fable-5-2). Skip the row rather
                # than persist a 0.00 that later reads as a measured cost.
                return
            # cost_usd = 0 (subscription), so saved_usd = potential − 0 = potential.
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
                           'claude_code_subscription', ?, ?)""",
                (
                    model,
                    input_tokens,
                    output_tokens,
                    latency_ms,
                    1 if success else 0,
                    baseline_model,
                    potential,
                    saved,
                    1 if _is_synthetic_run() else 0,
                    session_id or "unknown:hook",  # P0.8: never NULL; G3 counts unknown:* as missing
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

    model         = _infer_model(data)
    if model is None:
        sys.exit(0)  # unknown model: no row (see the module docstring)
    baseline      = _infer_baseline(model)
    input_tokens  = max(1, len(prompt) // 4)
    output_tokens = max(1, len(result_text) // 4)
    latency_ms    = data.get("duration_ms", 0.0)

    _log_to_db(model, baseline, input_tokens, output_tokens, float(latency_ms), success=True,
               session_id=_ledger_session_id(data.get("session_id")))
    sys.exit(0)


if __name__ == "__main__":
    main()
