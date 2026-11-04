"""The two routing numbers the status line shows for ONE session (owner goal 2026-11-04).

The North Star asks for more than 15% of LLM calls to be answered by a non-Claude or local model. This module
counts that for a session, in the detached refresher only (``statusline_snapshot.build``); the per-tick renderer
reads the result and computes nothing.

UNITS (one word per count; the status line prints the same word beside every number)

* a CALL is one model API response: a Claude ``message.id``, a Codex ``response_id``, or one router attempt that a
  non-Claude model answered and the router accepted;
* an ATTEMPT is one try of one model by the router's chain, whatever its outcome (a local model that is not
  installed is an attempt that failed). Attempts are counted only for the local-OK figure;
* a RUN is one delegated ``codex exec``; it is many calls.

DEFINITIONS (every one is checked by ``tests/test_session_routing.py``)

``routed X% (n/N calls, session)``

* N = every LLM call the session made = Claude calls + non-Claude calls.
* Claude calls = unique ``message.id`` over the session's transcript files: the main ``<sid>.jsonl``,
  ``<sid>/subagents/agent-*.jsonl`` AND ``<sid>/subagents/workflows/wf_*/agent-*.jsonl`` (workflow agents were 4,675
  of the 13,872 Claude calls of session 1a45a332 on 2026-11-04, and a one-level glob misses them; source:
  ``~/.rsi/research/primary-plan/v16/w9-evidence/``). One id is one call whichever file repeats it (a forked
  sub-agent replays its parent's messages). A ``<synthetic>`` record is no call. A message the per-call proxy
  served from a non-Claude model (``proxy_calls.jsonl``, ``decision: served``) moves to the non-Claude side.
  Router attempts accepted from a Claude model count here too.
* n = non-Claude calls whose answer was USED:
  - each model response of a Codex run agent-route DELEGATED for this session (``north_star_units.jsonl`` rows
    ``delegated``; ``codex_rollouts`` counts the responses in the rollout, the run's sub-agent threads included). A
    run with no countable response is counted as ONE and flagged. A ``codex_blocked`` / ``codex_failed`` run is
    DISCARDED (``northstar._CODEX_ATTEMPT_OUTCOMES``: Claude re-runs the work), so its responses are not in n;
    they are reported apart (``codex.discarded_responses``) and never inside routed%;
  - each router attempt a non-Claude provider answered and the router accepted (``execution_events``
    ``attempt_completed``): local (Ollama) attempts, Codex via the router's own chain, every MCP ``llm_*`` call
    answered by a non-Claude model. The attempt belongs to the session when its route is a ``routing_decisions`` row
    of the session or one named by an MCP ``tool_use`` id in the session's transcripts, so a call the MCP server
    logged under ``unknown:mcp`` still lands in the session that made it. The event names its route by
    ``correlation_id``, which is only 8 hex characters (``router.py``), so the join is constrained (:class:`_Routes`):
    the trace ids must agree, or, when either side has none, the event must have ended inside the window before the
    route was logged; a route that cannot be told from another is unplaceable. An attempt a text job's acceptance
    check rejected (``text_job_local_attempts.jsonl`` ``rejected``) is not a call used and is not in n; the row is
    matched to its attempt by job identity (:func:`_same_job`: the ``tool_use`` id of the ``llm_text_job`` call, else
    the session), never by model and time alone, and one function does it for the session and the 7-day population.
    A cache hit, a refused or breaker-held call is no attempt;
  - each ACCEPTED text-job-bind job of the session (``text_job_binds.jsonl``, ``bound`` / ``accepted``): the hook runs
    outside any MCP server, so its router rows are logged under ``unknown:non_mcp`` with no ``tool_use`` id and the
    attempt join above cannot reach them. One job is one call, whatever its tries; its model decides the side. A job
    that was handed back (rejected, fallback, timeout, error) is no call;
  - a Claude Code message served by a non-Claude model (proxy).

``local OK Y% (a/b, session)``

* b = LOCAL ATTEMPTS of the session: every ``execution_events`` attempt of a local provider (Ollama, LM Studio,
  vLLM, llama.cpp), whatever its outcome (``attempt_completed``, ``attempt_failed``, ``attempt_rejected``), so a
  local model that errored and was rescued by the next chain entry is a failed attempt in b;
* b also holds the local attempts of the session's text-job-bind jobs, accepted or handed back
  (``text_job_local_attempts.jsonl`` rows of the job's ``jid``: ``ok`` used, ``rejected`` thrown away by the
  acceptance check, ``error`` / ``infra`` failed; every attempt of a job that was handed back is not used, so ``a``
  gets nothing from it). The bind row names its ``jid`` (written since the hook recorded it) and the job is linked to
  its rows by that id and by nothing else: a row without a ``jid``, a ``jid`` with no rows, or rows that name
  another session link to nothing, and an accepted job then contributes only what its own row proves (the call, and
  one used attempt on its model). A router attempt is dropped for a job only when its route carries the bind row's
  ``tool_use`` id; never by model and time. The hook's own router rows are not in the session's attempts at all;
* a = those that were used: ``attempt_completed`` and not rejected afterwards by a text job's acceptance check
  (``text_job_local_attempts.jsonl`` ``rejected`` row of the same job or session, same model, that ended at most
  :data:`_GATE_WINDOW_S` seconds after the completion). Rows written before the ledger carried ``session_id`` and
  ``tool_use_id`` name no job and are matched to nothing: their attempts read as used.
* ``routing_decisions.success`` is NOT used anywhere here: it is "the response is usable" for the whole call
  (``router.py``), not "the local model worked", and a call rescued by a later chain entry has success 1 while
  its first local attempts failed.
* b = 0 prints ``local OK n/a (0 attempts, session)``, never 0%.

A ledger that exists and cannot be read (``LedgerUnreadable``: the bind ledger, the text-job attempt ledger, the
router database) makes the figures that need it unknown, printed n/a; a ledger that does not exist holds nothing.

WHAT IS NOT COUNTED (and why it is not a zero): a router call whose route has no ``execution_events`` row (the
agent-route ``direct`` delegations, counted as Codex runs above, and rows older than the event ledger); a call with
no session id on its route and no transcript ``tool_use`` to join through; a Codex run whose rollout was rotated
away (counted as one, flagged); a sub-agent transcript of the legacy top-level layout. The result carries
``complete: False`` while the transcript scan has not reached the end (a first scan of a 400 MB session takes
several runs); the renderers then print ``n/a``.

REPRODUCE (any machine with the same ledgers)::

    python -c "import json; from llm_router import session_routing as sr; print(json.dumps(sr.compute('<session id>'), indent=1))"

reading ``$LLM_ROUTER_HOME/usage.db`` (``execution_events``, ``routing_decisions``), ``north_star_units.jsonl``,
``text_job_local_attempts.jsonl``, ``proxy_calls.jsonl``, ``$CLAUDE_PROJECTS_DIR/*/<session>*`` and
``$CODEX_HOME/sessions``.

Refresher only: reads transcripts, the router database and rollout files. Never imported by the tick.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
from pathlib import Path

from llm_router import failopen, paths

SCHEMA = 1
INDEX_DIR = "session_routing"
#: A result younger than this is reused (the snapshot refresher runs at most every 10 s on a ledger change).
FRESH_S = 20.0
#: Seconds of transcript scanning one run may spend; the rest is resumed by the next run (offsets are persisted).
BUDGET_S = 4.0
KEEP_INDEXES = 20
CODEX_CACHE_NAME = "codex_counts.json"
_SAFE_SID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MCP_PREFIX = "mcp__llm_router__"
_CLAUDE_PROVIDERS = frozenset({"cc", "anthropic", "claude", "claude_code"})
#: ``execution_events.provider`` values that are no model attempt.
_NOT_A_MODEL = frozenset({"", "none", "cache", "test"})
_CODEX_OUTCOMES = ("delegated", "codex_blocked", "codex_failed")
#: Runs agent-route discarded (Claude re-runs the work): never in the routed share (``northstar``).
_DISCARDED_OUTCOMES = frozenset({"codex_blocked", "codex_failed"})
_ATTEMPT_EVENTS = {"attempt_completed": "ok", "attempt_failed": "error", "attempt_rejected": "rejected"}
#: A text job writes its acceptance row right after the route returns: the completed attempt it judged ended at
#: most this long before the row.
_GATE_WINDOW_S = 10.0
#: An attempt ends at most this long before its route is logged (a call can run for minutes) and at most this long
#: after (the clocks of the two writers).
_ROUTE_BEFORE_S = 900.0
_ROUTE_AFTER_S = 10.0
_IN_CHUNK = 400


def _is_claude(model: object) -> bool:
    from llm_router import northstar

    return northstar.claude_family(model) is not None


# ── the transcript scan: incremental, resumable, one index file per session ───────────────────────────────

def index_path(session_id: str) -> Path:
    return paths.state_path(INDEX_DIR, f"{session_id}.json")


def _load_index(session_id: str) -> dict:
    try:
        data = json.loads(index_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"v": SCHEMA, "files": {}, "proxy": {}, "models": []}
    if not isinstance(data, dict) or data.get("v") != SCHEMA or not isinstance(data.get("files"), dict):
        return {"v": SCHEMA, "files": {}, "proxy": {}, "models": []}
    data.setdefault("proxy", {})
    data.setdefault("models", [])
    return data


def result_path(session_id: str) -> Path:
    return paths.state_path(INDEX_DIR, f"{session_id}.result.json")


def _atomic_json(path: Path, data: dict) -> bool:
    """Write ``data`` at 0600 through a temp file. A failed write costs the next run its speed, never a number."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        failopen.record("CHZ-FO-ROUTING-WRITE", exc)
        tmp.unlink(missing_ok=True)
        return False


def _save_index(session_id: str, data: dict) -> None:
    path = index_path(session_id)
    if not _atomic_json(path, data):
        return
    try:                                        # bounded: the newest KEEP_INDEXES sessions (index + result each)
        files = sorted((p for p in path.parent.glob("*.json") if p.name != CODEX_CACHE_NAME),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[2 * KEEP_INDEXES:]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        failopen.record("CHZ-FO-ROUTING-PRUNE", exc)           # the directory grows until the next prune; no number is lost


def _load_result(session_id: str) -> dict | None:
    try:
        data = json.loads(result_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("v") == SCHEMA else None


def _progress(index: dict) -> tuple:
    """What a scan moves: the byte offsets of the files and of the proxy ledger, and the set of files."""
    return (tuple(sorted((k, e.get("offset", 0)) for k, e in index["files"].items())),
            index["proxy"].get("offset", 0), len(index["models"]))


def session_files(session_id: str, root: Path | None = None) -> list[Path]:
    """The main transcript, then every sub-agent and workflow-agent transcript of the session."""
    from llm_router import northstar

    root = root if root is not None else northstar.claude_projects_dir()
    main = sorted(glob.glob(str(root / "*" / f"{session_id}.jsonl")))
    subs = sorted(glob.glob(str(root / "*" / session_id / "subagents" / "**" / "agent-*.jsonl"), recursive=True))
    return [Path(p) for p in main + subs]


def _head(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read(256)).hexdigest()[:16]
    except OSError:
        return ""


def _model_slot(models: list[str], model: str | None) -> int:
    """Index of ``model`` in the index's model table (-1 = no model)."""
    if not model:
        return -1
    try:
        return models.index(model)
    except ValueError:
        models.append(model)
        return len(models) - 1


def _scan_file(path: Path, entry: dict | None, models: list[str], deadline: float) -> tuple[dict, bool]:
    """Read the lines appended to ``path`` since ``entry['offset']``. ``(entry, finished)``."""
    try:
        size = path.stat().st_size
    except OSError:
        return entry or {"offset": 0, "head": "", "msgs": {}, "mcp": []}, True
    head = _head(path)
    seen = entry.get("offset") if entry else None
    if not entry or entry.get("head") != head or not isinstance(seen, int) or size < seen:
        entry = {"offset": 0, "head": head, "msgs": {}, "mcp": []}
    msgs: dict[str, int] = entry["msgs"]
    mcp: list[str] = entry["mcp"]
    n = 0
    try:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            for line in fh:
                n += 1
                if (n & 2047) == 0 and time.monotonic() > deadline:
                    return entry, False                     # BEFORE this line is consumed: the next run reads it
                if not line.endswith(b"\n"):
                    break                                   # a partial last line: read whole next time
                entry["offset"] += len(line)
                if b'"type":"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") if isinstance(obj, dict) and obj.get("type") == "assistant" else None
                if not isinstance(message, dict):
                    continue
                model = message.get("model") if isinstance(message.get("model"), str) else None
                if (model or "").startswith("<"):           # "<synthetic>": not an API call
                    continue
                mid = message.get("id") if isinstance(message.get("id"), str) and message.get("id") else None
                key = mid or f"@{entry['offset']}"          # a record with no id is one call of its own
                msgs.setdefault(key, _model_slot(models, model))
                content = message.get("content")
                for block in content if isinstance(content, list) else ():
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and str(block.get("name") or "").startswith(_MCP_PREFIX) \
                            and isinstance(block.get("id"), str):
                        mcp.append(block["id"])
    except OSError:
        return entry, True
    return entry, True


def _scan_proxy(session_id: str, state: dict, deadline: float) -> bool:
    """Collect the message ids the proxy served for this session (appends only; offsets persisted)."""
    path = paths.state_path("proxy_calls.jsonl")
    try:
        size = path.stat().st_size
    except OSError:
        state.update(offset=0, served=[], missing=True)
        return True
    state.pop("missing", None)
    seen = state.get("offset")
    if not isinstance(seen, int) or size < seen:
        state.update(offset=0, served=[])
    served: list[str] = state.setdefault("served", [])
    needle = session_id.encode()
    n = 0
    with open(path, "rb") as fh:
        fh.seek(state.get("offset", 0))
        for line in fh:
            n += 1
            if (n & 8191) == 0 and time.monotonic() > deadline:
                return False                                # BEFORE this line is consumed: the next run reads it
            if not line.endswith(b"\n"):
                break
            state["offset"] = state.get("offset", 0) + len(line)
            if needle not in line or b'"served"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("session_id") == session_id and row.get("decision") == "served" \
                    and isinstance(row.get("msg_id"), str):
                served.append(row["msg_id"])
    return True


def transcript_figures(session_id: str, *, budget_s: float = BUDGET_S, root: Path | None = None) -> dict:
    """Claude / non-Claude message counts from the session's transcripts, and the MCP ``tool_use`` ids.

    ``complete`` is False while the scan is still resuming. Ids are unique across ALL the session's files."""
    deadline = time.monotonic() + budget_s
    index = _load_index(session_id)
    models: list[str] = index["models"]
    before = _progress(index)
    files = session_files(session_id, root)
    finished = True
    live = {str(p) for p in files}
    for stale in [k for k in index["files"] if k not in live]:
        del index["files"][stale]                           # a transcript the host removed
    for path in files:
        if time.monotonic() > deadline:
            finished = False
            break
        entry, done = _scan_file(path, index["files"].get(str(path)), models, deadline)
        index["files"][str(path)] = entry
        if not done:
            finished = False
            break
    if finished:
        finished = _scan_proxy(session_id, index["proxy"], deadline)
    seen: dict[str, str | None] = {}
    mcp_ids: list[str] = []
    for p in files:
        e = index["files"].get(str(p))
        if not e:
            continue
        for mid, slot in e["msgs"].items():
            seen.setdefault(mid, models[slot] if 0 <= slot < len(models) else None)
        mcp_ids.extend(e["mcp"])
    served = set(index["proxy"].get("served") or ())
    claude = proxy = other = unclassified = 0
    for mid, model in seen.items():
        if mid in served:
            proxy += 1
        elif model is None:
            unclassified += 1
        elif _is_claude(model):
            claude += 1
        else:
            other += 1
    out = {
        "complete": finished, "claude": claude, "proxy_served": proxy, "other_model": other,
        "unclassified": unclassified, "mcp_tool_use_ids": list(dict.fromkeys(mcp_ids)),
        "files": {"main": sum(1 for p in files if "subagents" not in p.parts),
                  "subagents": sum(1 for p in files if "subagents" in p.parts and "workflows" not in p.parts),
                  "workflows": sum(1 for p in files if "workflows" in p.parts)},
    }
    if _progress(index) != before:                # progress is kept even when the scan did not finish; an idle
        _save_index(session_id, index)            # refresh rewrites nothing
    return out


# ── Codex runs ────────────────────────────────────────────────────────────────────────────────────────────

def _ns_rows(session_id: str | None) -> list[dict]:
    from llm_router import northstar

    rows = [r for r in northstar._load_north_star_ledger()
            if r.get("lever") == "agent_route_codex" and r.get("outcome") in _CODEX_OUTCOMES
            and isinstance(r.get("ts"), (int, float)) and not isinstance(r.get("ts"), bool)]
    return rows if session_id is None else [r for r in rows if r.get("session_id") == session_id]


def _codex_cache_path() -> Path:
    return paths.state_path(INDEX_DIR, CODEX_CACHE_NAME)


def _load_codex_cache() -> dict:
    try:
        data = json.loads(_codex_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_codex_cache(cache: dict) -> None:
    _atomic_json(_codex_cache_path(), cache)


def run_key(row: dict) -> str:
    """The identity of one delegated run in ``north_star_units.jsonl``: session, time, thread and tool call. Two
    rows with the same ``ts`` are two runs, not one (the cache used to key on ``ts`` alone)."""
    return "|".join(str(row.get(k) or "") for k in ("session_id", "ts", "thread_id", "tool_use_id"))


def codex_run_responses(rows: list[dict], now: float) -> list[dict]:
    """``[{row, responses, flagged, how}]`` for each Codex row, in order. ``responses`` is what the run adds to the
    call count (see the module docstring). A finished run's count is cached by its identity (a rollout never
    changes)."""
    from llm_router import codex_rollouts as cr

    if not rows:
        return []
    cache = _load_codex_cache()
    dirty = False
    index = None
    out: list[dict] = []
    for row in rows:
        key = run_key(row)
        hit = cache.get(key)
        if isinstance(hit, dict) and isinstance(hit.get("responses"), int):
            responses, how = hit["responses"], hit.get("how", "rollout")
        else:
            if index is None:
                lo = min(r["ts"] - float(r.get("duration_sec") or 0.0) for r in rows) - 60.0
                index = cr.RolloutIndex(lo, max(r["ts"] for r in rows) + 10.0)
            tid = row.get("thread_id") if isinstance(row.get("thread_id"), str) else None
            how = "rollout"
            if not tid and row.get("outcome") != "codex_failed" and row.get("duration_sec"):
                tid = cr.thread_for_run(index, float(row["ts"]), float(row["duration_sec"]))
                how = "time-matched rollout"
            rc = cr.count_run(index, tid, float(row["ts"])) if tid else cr.RunCount(None)
            responses = rc.responses
            if responses is None:
                how = "none"
            elif now - float(row["ts"]) > 120.0:
                cache[key] = {"responses": responses, "how": how, "at": float(row["ts"])}
                dirty = True
        flagged = responses is None and row.get("outcome") == "delegated"      # only a counted run needs the flag
        if responses is None:
            responses = 1 if row.get("outcome") != "codex_failed" else 0
        out.append({"row": row, "responses": responses, "flagged": flagged, "how": how})
    if dirty:
        if len(cache) > 5000:                               # a bounded file: drop the oldest runs
            for k in sorted(cache, key=lambda x: (cache[x] or {}).get("at", 0.0))[:len(cache) - 5000]:
                cache.pop(k, None)
        _save_codex_cache(cache)
    return out


# ── router attempts (execution_events) and the text-job acceptance gate ───────────────────────────────────

def _ts(value: object) -> float | None:
    from datetime import datetime, timezone

    try:
        return datetime.strptime(str(value)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def classify_attempt(provider: object, model: object) -> str | None:
    """``local`` | ``claude`` | ``other`` (a non-Claude provider, Codex included) | None (not a model attempt)."""
    from llm_router import ledger_view as lv

    p = str(provider or "").strip().lower()
    if p in _NOT_A_MODEL:
        return None
    if p in _CLAUDE_PROVIDERS or _is_claude(model):
        return "claude"
    if p in lv.LOCAL_PROVIDERS or str(model or "").startswith(("ollama/", "ollama_chat/")):
        return "local"
    return "other"


def _event_attempt(row: tuple) -> dict | None:
    key, ts, provider, model, event_type, accepted = row
    outcome = _ATTEMPT_EVENTS.get(event_type)
    kind = classify_attempt(provider, model)
    if outcome is None or kind is None or not isinstance(ts, (int, float)):
        return None
    return {"id": key, "ts": float(ts), "provider": str(provider), "model": str(model or ""), "outcome": outcome,
            "kind": kind, "used": outcome == "ok" and accepted != 0, "gate_rejected": False}


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _real_session(sid: object) -> str | None:
    """``sid`` when it names a session (``unknown:mcp`` and the other ``unknown*`` placeholders do not)."""
    return sid if isinstance(sid, str) and sid and not sid.startswith("unknown") else None


class _Routes:
    """The ``routing_decisions`` rows an attempt can belong to, indexed by correlation id and by trace id.

    ``correlation_id`` is ``uuid4().hex[:8]`` (``router.py``): 32 bits, so two routes can share one. An event whose
    ``session_id`` column (its route's correlation id) names a route is therefore accepted only when the trace ids
    agree (32 hex characters: exact) or, when either side has none, when the event ended inside the window before
    the route was logged (:data:`_ROUTE_BEFORE_S` / :data:`_ROUTE_AFTER_S`). Two routes that both satisfy it with
    different identities make the event unplaceable (``None``), not a guess."""

    def __init__(self) -> None:
        self.by_corr: dict[str, list[tuple]] = {}
        self.by_trace: dict[str, tuple[str | None, str | None]] = {}

    def add(self, corr: object, trace: object, ts: object, sid: object, tool: object) -> None:
        ident = (sid if isinstance(sid, str) else None, tool if isinstance(tool, str) and tool else None)
        if isinstance(corr, str) and corr:
            self.by_corr.setdefault(corr, []).append((trace if isinstance(trace, str) and trace else None, _ts(ts)) + ident)
        if isinstance(trace, str) and trace:
            self.by_trace.setdefault(trace, ident)

    def place(self, corr: object, trace: object, ts: float) -> tuple[str | None, str | None] | None:
        """``(session_id, tool_use_id)`` of the route an event belongs to, or None."""
        if isinstance(trace, str) and trace and trace in self.by_trace:
            return self.by_trace[trace]
        found = set()
        for r_trace, r_ts, sid, tool in self.by_corr.get(corr, []) if isinstance(corr, str) else ():
            if r_trace and isinstance(trace, str) and trace:
                continue                                     # both carry a trace id and it is not the same route's
            if r_ts is not None and -_ROUTE_AFTER_S <= r_ts - ts <= _ROUTE_BEFORE_S:
                found.add((sid, tool))
        return next(iter(found)) if len(found) == 1 else None


def _attempt_query(ev: set[str], marks: str) -> str:
    return ("SELECT rowid, ts, provider, model, event_type, "
            f"{'accepted' if 'accepted' in ev else 'NULL'}, session_id, "
            f"{'trace_id' if 'trace_id' in ev else 'NULL'} FROM execution_events WHERE event_type IN ({marks}) AND ")


def session_attempts(session_id: str, tool_use_ids: list[str]) -> list[dict] | None:
    """Every router attempt of the session (see the module docstring), each with the ``tool_use`` id of its route
    (``tool``) and ``session``, or ``None`` when the ledger cannot be read: unknown, not zero."""
    from llm_router import ledger_view as lv

    con = lv._connect(None, 1.0)
    if con is None:
        return None
    try:
        rd, ev = lv._columns(con, "routing_decisions"), lv._columns(con, "execution_events")
        if not ({"correlation_id", "session_id"} <= rd and {"event_type", "provider", "model", "session_id", "ts"} <= ev):
            return None
        prov = " AND provenance = 'runtime'" if "provenance" in rd else ""
        base = ("SELECT correlation_id, " + ("trace_id" if "trace_id" in rd else "NULL") + ", "
                + ("timestamp" if "timestamp" in rd else "NULL") + ", session_id, "
                + ("tool_use_id" if "tool_use_id" in rd else "NULL") + " FROM routing_decisions WHERE ")
        routes = _Routes()
        for row in con.execute(base + "session_id = ?" + prov, (session_id,)):
            routes.add(*row)
        if "tool_use_id" in rd:
            for chunk in _chunks([t for t in tool_use_ids if t]):
                for row in con.execute(base + f"tool_use_id IN ({','.join('?' for _ in chunk)})" + prov, chunk):
                    routes.add(row[0], row[1], row[2], session_id, row[4])    # the MCP call is the session's
        marks = ",".join("?" for _ in _ATTEMPT_EVENTS)
        sel = _attempt_query(ev, marks)
        found: dict[int, dict] = {}
        for column, values in (("session_id", sorted(routes.by_corr)), ("trace_id", sorted(routes.by_trace) if "trace_id" in ev else [])):
            for chunk in _chunks(values):
                for row in con.execute(sel + f"{column} IN ({','.join('?' for _ in chunk)})", [*_ATTEMPT_EVENTS, *chunk]):
                    a = _event_attempt(row[:6])
                    placed = routes.place(row[6], row[7], row[1]) if a is not None else None
                    if a is not None and placed is not None:
                        a["session"], a["tool"] = session_id, placed[1]
                        found.setdefault(row[0], a)
    finally:
        con.close()
    return sorted(found.values(), key=lambda a: a["ts"])


def window_attempts(since: float, until: float, tool_sessions: dict[str, str]) -> list[dict] | None:
    """Every router attempt in ``[since, until]`` with the session it belongs to (``session``, None when it cannot
    be placed) and its route's ``tool_use`` id (``tool``). A route logged under ``unknown:mcp`` (the MCP server does
    not know the Claude session) is placed through the ``tool_use`` id its transcript block carries
    (``tool_sessions``, filled by the unit pass). ``None`` when the ledger cannot be read."""
    from llm_router import ledger_view as lv

    con = lv._connect(None, 1.0)
    if con is None:
        return None
    try:
        rd, ev = lv._columns(con, "routing_decisions"), lv._columns(con, "execution_events")
        if not ({"correlation_id", "session_id", "timestamp"} <= rd and {"event_type", "provider", "model", "session_id", "ts"} <= ev):
            return None
        prov = " AND provenance = 'runtime'" if "provenance" in rd else ""
        from datetime import datetime, timezone

        lo = datetime.fromtimestamp(since - 86400.0, timezone.utc).strftime("%Y-%m-%d")
        routes = _Routes()
        for row in con.execute(
                "SELECT correlation_id, " + ("trace_id" if "trace_id" in rd else "NULL") + ", timestamp, session_id, "
                + ("tool_use_id" if "tool_use_id" in rd else "NULL") + f" FROM routing_decisions WHERE timestamp >= ?{prov}",
                (lo,)):
            routes.add(*row)
        marks = ",".join("?" for _ in _ATTEMPT_EVENTS)
        out: list[dict] = []
        for row in con.execute(_attempt_query(ev, marks) + "ts >= ? AND ts <= ?", [*_ATTEMPT_EVENTS, since, until]):
            a = _event_attempt(row[:6])
            if a is None:
                continue
            sid, tool = routes.place(row[6], row[7], row[1]) or (None, None)
            a["session"] = _real_session(sid) or (tool_sessions.get(tool) if tool else None)
            a["tool"] = tool
            out.append(a)
    finally:
        con.close()
    return sorted(out, key=lambda a: a["ts"])


def _norm_model(model: str) -> str:
    return model.replace("ollama_chat/", "ollama/", 1)


def text_job_rejections(since: float, tool_sessions: dict[str, str] | None = None) -> list[dict]:
    """Every local text-job attempt the acceptance check rejected since ``since`` (the router answered; the job
    threw the answer away): ``{ts, model, session, tool}``. ``session`` is the row's session id when it names one,
    else the session its ``tool_use`` id belongs to (``tool_sessions``); ``tool`` is the ``tool_use`` id of the
    ``llm_text_job`` call. A row written before the ledger carried either (no ``session_id``, no ``tool_use_id``)
    names no session and is matched to nothing. Raises :class:`LedgerUnreadable` (the ledger exists and cannot be
    read: whether an attempt was rejected is unknown)."""
    from llm_router import text_job_ledger as tj

    out: list[dict] = []
    for line in _ledger_lines(paths.state_path(tj.LOCAL_ATTEMPTS_FILENAME)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = row.get("ts") if isinstance(row, dict) else None
        if not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= since
                and row.get("outcome") == tj.OUTCOME_REJECTED and isinstance(row.get("model"), str)):
            continue
        tool = row.get("tool_use_id") if isinstance(row.get("tool_use_id"), str) and row.get("tool_use_id") else None
        out.append({"ts": float(ts), "model": row["model"], "tool": tool,
                    "session": _real_session(row.get("session_id")) or (tool_sessions or {}).get(tool or "")})
    return sorted(out, key=lambda r: r["ts"])


def _same_job(attempt: dict, rejection: dict) -> bool:
    """Whether a rejection belongs to the call that made this attempt: the same ``tool_use`` id when both carry
    one (two ids are two jobs, whatever else matches), else the same session. Never by model and time alone."""
    if rejection["tool"] and attempt.get("tool"):
        return rejection["tool"] == attempt["tool"]
    return bool(rejection["session"]) and rejection["session"] == attempt.get("session")


def apply_text_job_gate(attempts: list[dict], rejections: list[dict]) -> int:
    """Mark the completed local attempt each text-job rejection judged as not used. ONE function for the session
    figure and the 7-day population. A rejection is matched to an attempt of the SAME job or session (:func:`_same_job`),
    of the same model, that ended up to :data:`_GATE_WINDOW_S` before the row, one-to-one (the latest unmarked one).
    Returns how many were marked."""
    marked = 0
    for r in rejections:
        best = None
        for a in attempts:
            if a["kind"] == "local" and a["used"] and _norm_model(a["model"]) == _norm_model(r["model"]) \
                    and 0.0 <= r["ts"] - a["ts"] <= _GATE_WINDOW_S and _same_job(a, r) \
                    and (best is None or a["ts"] > best["ts"]):
                best = a
        if best is not None:
            best["used"], best["gate_rejected"] = False, True
            marked += 1
    return marked


# ── text-job-bind jobs (the hook runs outside MCP: its router rows name no session) ─────────────────────────

class LedgerUnreadable(Exception):
    """A ledger exists and cannot be read: what it holds is unknown, which no renderer may print as zero."""


def _ledger_lines(path: Path) -> list[str]:
    """The lines of a ledger: ``[]`` when it does not exist (nothing was ever recorded), a raised
    :class:`LedgerUnreadable` when it exists and cannot be read (unknown, never an empty ledger)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LedgerUnreadable(str(path)) from exc


def bind_jobs(since: float = 0.0, until: float | None = None, *, session_id: str | None = None) -> list[dict]:
    """The jobs of ``text_job_binds.jsonl`` that ran the local text job (``bound`` and ``denied`` rows with a real
    session id): ``{ts, model, session, tool, jid, accepted, latency_s}``. ``accepted`` is ``bound`` / ``accepted``:
    the only rows that are a call. A job that was handed back (``rejected``, ``fallback``, ``timeout``, ``error``) still
    made local attempts; they are in b with ``a = 0`` and add nothing to n. ``handwritten`` rows ran nothing. ``jid``
    (written by the hook since this change) names the job's attempt rows; a row without one is linked to nothing.
    Raises :class:`LedgerUnreadable`."""
    from llm_router import text_job_ledger as tj
    from llm_router.text_job_bind import ledger as bl

    out: list[dict] = []
    for line in _ledger_lines(bl.ledger_path()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = row.get("ts") if isinstance(row, dict) else None
        sid = _real_session(row.get("session_id")) if isinstance(row, dict) else None
        if not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and sid and ts >= since
                and (until is None or ts <= until) and (session_id is None or sid == session_id)
                and row.get("kind") in (bl.KIND_BOUND, bl.KIND_DENIED) and isinstance(row.get("model"), str) and row["model"]):
            continue
        lat = row.get("latency_ms")
        jid = row.get("jid") if isinstance(row.get("jid"), str) and row["jid"] else None
        out.append({"ts": float(ts), "model": row["model"], "session": sid, "jid": jid,
                    "tool": row.get("tool_use_id") if isinstance(row.get("tool_use_id"), str) and row["tool_use_id"] else None,
                    "accepted": row.get("kind") == bl.KIND_BOUND and row.get("outcome") == tj.STATUS_ACCEPTED,
                    "latency_s": float(lat) / 1000.0 if isinstance(lat, (int, float)) and not isinstance(lat, bool) and lat >= 0 else 0.0})
    return sorted(out, key=lambda j: j["ts"])


def text_job_jid_groups() -> dict[str, list[dict]]:
    """Every row of ``text_job_local_attempts.jsonl`` that names a job (``jid``), by ``jid``, in time order.
    Raises :class:`LedgerUnreadable`."""
    from llm_router import text_job_ledger as tj

    groups: dict[str, list[dict]] = {}
    for line in _ledger_lines(paths.state_path(tj.LOCAL_ATTEMPTS_FILENAME)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = row.get("ts") if isinstance(row, dict) else None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and isinstance(row.get("jid"), str) \
                and isinstance(row.get("model"), str) and isinstance(row.get("outcome"), str):
            groups.setdefault(row["jid"], []).append({
                "ts": float(ts), "model": row["model"], "outcome": row["outcome"],
                "session": _real_session(row.get("session_id"))})
    for rows in groups.values():
        rows.sort(key=lambda r: r["ts"])
    return groups


def link_bind_job(job: dict, groups: dict[str, list[dict]]) -> list[dict] | None:
    """The attempt rows of one bind job: those of the ``jid`` its own row names, and nothing else. A job whose row
    names none (written before the hook recorded it), whose ``jid`` has no rows, or whose rows name another session
    is linked to nothing: it contributes only what its own row proves. Never by model and time."""
    rows = groups.get(job["jid"]) if job["jid"] else None
    if not rows or any(r["session"] and r["session"] != job["session"] for r in rows):
        return None
    return rows


def _provider_model(model: str) -> tuple[str, str]:
    head, sep, _rest = model.partition("/")
    return (head if sep else "other"), model


def fold_bind_jobs(attempts: list[dict], jobs: list[dict], groups: dict[str, list[dict]]) -> list[dict]:
    """``attempts`` with the attempts of each bind job added. Returns a new list.

    * a job's linked rows (:func:`link_bind_job`) are attempts of the job's session: ``ok`` is used when the job was
      accepted, ``rejected`` is thrown away by the acceptance check, ``error`` / ``infra`` failed; every row of a job
      that was handed back is not used (``a = 0``);
    * an ACCEPTED job with no used attempt among them (its row names no ``jid``, a served-by-Codex job, rows rotated
      away) adds the one used attempt its own row proves, on the model that served it (the call in n);
    * a router attempt is taken out only when it belongs to the job by identity: its route carries the ``tool_use`` id
      of the bind row. Never by model and time: another call of the session, or another job's attempts, on the same
      model in the same seconds are not this job's. (The hook's own route is logged under ``unknown:non_mcp`` and is
      not in ``attempts`` at all; a route that ever carries the bind row's ``tool_use`` id is the same work.)"""
    out = list(attempts)
    for job in jobs:
        if job["tool"]:
            out = [a for a in out if not (a.get("tool") == job["tool"] and not a.get("bind"))]
        rows = link_bind_job(job, groups) or []
        added = [{"id": ("bind", job["jid"], r["ts"]), "ts": r["ts"], "provider": "ollama", "model": r["model"],
                  "outcome": "ok" if r["outcome"] == "ok" else "rejected" if r["outcome"] == "rejected" else "error",
                  "kind": "local", "used": job["accepted"] and r["outcome"] == "ok",
                  "gate_rejected": r["outcome"] == "rejected", "session": job["session"], "tool": None, "bind": True}
                 for r in rows]
        kind = classify_attempt(*_provider_model(job["model"]))
        if job["accepted"] and kind is not None and not any(a["used"] for a in added):
            added.append({"id": ("bind", job["jid"], job["ts"]), "ts": job["ts"], "provider": _provider_model(job["model"])[0],
                          "model": job["model"], "outcome": "ok", "kind": kind, "used": True, "gate_rejected": False,
                          "session": job["session"], "tool": None, "bind": True})
        out += added
    return sorted(out, key=lambda a: a["ts"])


def local_figures(attempts: list[dict]) -> dict:
    """``{attempts, ok, gate_rejected}`` of the local attempts (see the module docstring)."""
    local = [a for a in attempts if a["kind"] == "local"]
    return {"attempts": len(local), "ok": sum(1 for a in local if a["used"]),
            "gate_rejected": sum(1 for a in local if a["gate_rejected"])}


# ── the session figure ────────────────────────────────────────────────────────────────────────────────────

def _part(code: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- one source failing must not blank the others
        failopen.record(code, exc)
        return None


def _router_attempts(session_id: str, mcp_ids: list[str]) -> list[dict] | None:
    """The session's router attempts with the text-job gate and the bind jobs applied, or None when any ledger it
    reads is unreadable (unknown, never an empty ledger)."""
    attempts = session_attempts(session_id, mcp_ids)
    if attempts is None:
        return None
    if attempts:
        apply_text_job_gate(attempts, text_job_rejections(attempts[0]["ts"], dict.fromkeys(mcp_ids, session_id)))
    jobs = bind_jobs(session_id=session_id)
    return fold_bind_jobs(attempts, jobs, text_job_jid_groups()) if jobs else attempts


def compute(session_id: str, now: float | None = None, *, budget_s: float = BUDGET_S, root: Path | None = None) -> dict | None:
    """The session's routing figure, or None for an id that is not a plain id. A source that cannot be read leaves
    its fields ``None`` (unknown); the renderers then print ``n/a``, never 0."""
    if not _SAFE_SID.fullmatch(session_id or ""):
        return None
    now = time.time() if now is None else now
    prior = _load_result(session_id)
    stamp = prior.get("computed_at") if prior else None
    if prior and prior.get("complete") and isinstance(stamp, (int, float)) and 0 <= now - stamp < FRESH_S:
        return prior
    t = _part("CHZ-FO-ROUTING-TRANSCRIPT", lambda: transcript_figures(session_id, budget_s=budget_s, root=root))
    mcp_ids = t["mcp_tool_use_ids"] if t else []
    runs = _part("CHZ-FO-ROUTING-CODEX", lambda: codex_run_responses(_ns_rows(session_id), now))
    attempts = _part("CHZ-FO-ROUTING-ROWS", lambda: _router_attempts(session_id, mcp_ids))

    used = {"local": 0, "other": 0, "claude": 0}
    for a in attempts or ():
        if a["used"]:
            used[a["kind"]] += 1
    delegated = [r for r in runs or () if r["row"].get("outcome") == "delegated"]
    discarded = [r for r in runs or () if r["row"].get("outcome") in _DISCARDED_OUTCOMES]
    codex_calls = sum(r["responses"] for r in delegated)

    complete = bool(t and t["complete"])
    # No main transcript on disk: the Claude side is unknown, and "0 Claude calls" would make every share 100%.
    known = t is not None and t["files"]["main"] > 0 and runs is not None and attempts is not None
    non_claude = (t["proxy_served"] + t["other_model"] + codex_calls + used["local"] + used["other"]) if known else None
    claude = (t["claude"] + used["claude"]) if known else None
    result = {
        "v": SCHEMA, "session_id": session_id, "computed_at": now, "complete": complete,
        "claude": claude, "non_claude": non_claude,
        "total": None if non_claude is None or claude is None else claude + non_claude,
        "unclassified": t["unclassified"] if t else None,
        "by_source": None if not known else {
            "codex_runs": codex_calls, "router_local": used["local"], "router_other": used["other"],
            "proxy": t["proxy_served"], "transcript_other": t["other_model"]},
        "codex": None if runs is None else {
            "runs": len(delegated), "responses": codex_calls, "flagged_runs": sum(1 for r in delegated if r["flagged"]),
            "discarded_runs": len(discarded), "discarded_responses": sum(r["responses"] for r in discarded)},
        "local": local_figures(attempts) if attempts is not None else None,
        "files": t["files"] if t else None,
    }
    if t is not None:
        _atomic_json(result_path(session_id), result)
    return result


# ── the 7-day organic window (the call mix's extra figures) ─────────────────────────────────────────────────

def window_extras(since: float, until: float, now: float, kind_of, tool_sessions: dict[str, str] | None = None) -> dict:
    """What the 7-day call mix adds on top of the NS unit list, over ``[since, until]`` and the organic population
    (``kind_of(session_id, stamp)`` -> true for a session of the population):

    * ``codex_extra``: the correction to the unit list's one-call-per-Codex-row count. A DELEGATED run counts its
      model responses (``responses - 1`` beyond the unit); a ``codex_blocked`` / ``codex_failed`` run is discarded
      work and leaves the count (``-1``: the list counts that unit, Claude re-ran the work). ``by_session`` is its
      per-session split, ``runs`` / ``flagged_runs`` the delegated runs and those counted as one;
    * ``bind_extra``: accepted text-job-bind jobs of the population, one call each (the unit list has none), also in
      ``by_session``;
    * ``local``: ``{attempts, ok}`` of the local router attempts of the same sessions, bind jobs folded in, by the same
      definition as the session figure (an attempt is OK when used). An attempt on a route logged under ``unknown:mcp`` is placed
      through its ``tool_use`` id (``tool_sessions``); ``unattributed_local`` counts the local attempts that no
      session could be found for (not in ``local``). ``local`` and ``unattributed_local`` are ``None`` when the attempt
      ledgers cannot be read: unknown, which every renderer prints as n/a. An unreadable bind ledger raises
      :class:`LedgerUnreadable` (the number of calls is then unknown, and the caller keeps the figures without
      the extras)."""
    rows = [r for r in _ns_rows(None) if since <= r["ts"] <= until and kind_of(r.get("session_id"), r.get("session_kind"))]
    runs = codex_run_responses(rows, now)
    extra = 0
    by_session: dict[str, int] = {}
    for r in runs:
        row = r["row"]
        if not row.get("model"):
            continue                                        # the unit list leaves a model-less row unclassified
        more = r["responses"] - 1 if row.get("outcome") == "delegated" else -1
        extra += more
        if more and isinstance(row.get("session_id"), str):
            by_session[row["session_id"]] = by_session.get(row["session_id"], 0) + more
    # text-job-bind jobs of the population (an unreadable bind ledger raises: the whole extras are then unknown and the
    # caller falls back to the figures without them). An accepted job is one call (the unit list has none), on the model
    # that served it; a job handed back adds attempts only.
    jobs = [j for j in bind_jobs(since, until) if kind_of(j["session"], None)]
    bind_extra = 0
    for j in jobs:
        if j["accepted"] and classify_attempt(*_provider_model(j["model"])) in ("local", "other"):
            bind_extra += 1
            by_session[j["session"]] = by_session.get(j["session"], 0) + 1
    attempts = window_attempts(since, until, tool_sessions or {})
    local = None                                            # the ledger could not be read: unknown, never "0 attempts"
    unattributed = None
    if attempts is not None:
        try:
            placed = [a for a in attempts if a["session"] and kind_of(a["session"], None)]
            apply_text_job_gate(placed, text_job_rejections(since, tool_sessions))
            folded = fold_bind_jobs(attempts, jobs, text_job_jid_groups()) if jobs else attempts
            placed = [a for a in folded if a["session"] and kind_of(a["session"], None)]
            local = local_figures(placed)
            unattributed = sum(1 for a in folded if a["kind"] == "local" and not a["session"])
        except LedgerUnreadable as exc:
            failopen.record("CHZ-FO-ROUTING-LEDGER", exc)     # whether an attempt was used is unknown: local stays None
    return {"codex_extra": extra, "bind_extra": bind_extra, "by_session": by_session,
            "runs": sum(1 for r in runs if r["row"].get("outcome") == "delegated"),
            "flagged_runs": sum(1 for r in runs if r["flagged"]), "local": local, "unattributed_local": unattributed}
