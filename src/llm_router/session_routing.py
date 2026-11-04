"""The two routing numbers the status line shows for ONE session (owner goal 2026-11-04).

The North Star asks for more than 15% of LLM calls to be answered by a non-Claude or local model. This module
counts that for a session, in the detached refresher only (``statusline_snapshot.build``); the per-tick renderer
reads the result and computes nothing.

DEFINITIONS (every one is checked by ``tests/test_session_routing.py``)

``routed X% (n/N calls, session)``

* N = every LLM call the session made = Claude calls + non-Claude calls.
* Claude calls = unique ``message.id`` over the session's transcript files: the main ``<sid>.jsonl``,
  ``<sid>/subagents/agent-*.jsonl`` AND ``<sid>/subagents/workflows/wf_*/agent-*.jsonl`` (workflow agents are a
  third of a busy session and a one-level glob misses them). One id is one call whichever file repeats it (a forked
  sub-agent replays its parent's messages). A ``<synthetic>`` record is no call. A message the per-call proxy
  served from a non-Claude model (``proxy_calls.jsonl``, ``decision: served``) moves to the non-Claude side.
  Router calls answered by a Claude model count here too.
* n = non-Claude calls:
  - each MODEL RESPONSE of a Codex run agent-route delegated for this session (``north_star_units.jsonl`` rows
    ``delegated`` / ``codex_blocked`` / ``codex_failed``; ``codex_rollouts`` counts the responses in the rollout, the
    run's sub-agent threads included). A run with no countable response is counted as ONE and flagged (``flagged``);
    a ``codex_failed`` run with none counts zero (it may never have reached the model);
  - each router-executed call that a non-Claude provider answered (``routing_decisions``, ``success = 1``): local
    (Ollama) calls, Codex calls made by the router's own chain, and every MCP ``llm_*`` call whose answer came from
    a non-Claude model. An MCP call is attributed by the ``tool_use`` id its transcript block carries, so a call the
    MCP server logged under another session id still lands in the session that made it. A cache hit, a refused or
    breaker-held call and a caller-cancelled call are no call. The routing row of a Codex delegation (written by the
    same run) is the run itself and is not counted twice.
  - a Claude Code message served by a non-Claude model (proxy).

``local OK Y% (a/b, session)``

* b = local-model attempts: the session's ``routing_decisions`` rows whose provider is local (Ollama, LM Studio, vLLM,
  llama.cpp), failures included, cache hits / refusals / cancelled calls excluded;
* a = those that succeeded (the local route succeeded; a text job's result also needs the acceptance check, which
  the router records in the same row's ``success``).
* b = 0 prints ``local OK n/a (0 attempts)``, never 0%.

WHAT IS NOT COUNTED (and why it is not a zero): a call the router made with no session id on its row and no
transcript ``tool_use`` to join through; a Codex run whose rollout was rotated away (counted as one, flagged); a
sub-agent transcript of the legacy top-level layout. The result carries ``complete: False`` while the transcript scan
has not reached the end (a first scan of a 400 MB session takes several runs); the renderers then print ``n/a``.

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
from typing import Any

from llm_router import failopen, paths

SCHEMA = 1
INDEX_DIR = "session_routing"
#: A result younger than this is reused (the snapshot refresher runs at most every 10 s on a ledger change).
FRESH_S = 20.0
#: Seconds of transcript scanning one run may spend; the rest is resumed by the next run (offsets are persisted).
BUDGET_S = 6.0
KEEP_INDEXES = 20
CODEX_CACHE_NAME = "codex_counts.json"
_SAFE_SID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MCP_PREFIX = "mcp__llm_router__"
#: ``routing_decisions.reason_code`` of a row that is no model call.
_NOT_A_CALL = frozenset({"cache_hit", "breaker_open", "refused_no_research_provider", "error_cancelled"})
_CLAUDE_PROVIDERS = frozenset({"cc", "anthropic", "claude", "claude_code"})
_CODEX_OUTCOMES = ("delegated", "codex_blocked", "codex_failed")
#: A delegation's routing row is written this long before its north_star_units row (both are UTC).
_TWIN_BEFORE_S = 20.0
_TWIN_AFTER_S = 3.0
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
    if not entry or entry.get("head") != head or size < entry.get("offset", 0):
        entry = {"offset": 0, "head": head, "msgs": {}, "mcp": []}
    msgs: dict[str, int] = entry["msgs"]
    mcp: list[str] = entry["mcp"]
    n = 0
    try:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            for line in fh:
                if not line.endswith(b"\n"):
                    break                                   # a partial last line: read whole next time
                entry["offset"] += len(line)
                n += 1
                if (n & 2047) == 0 and time.monotonic() > deadline:
                    return entry, False
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
    if size < state.get("offset", 0):
        state.update(offset=0, served=[])
    served: list[str] = state.setdefault("served", [])
    needle = session_id.encode()
    n = 0
    with open(path, "rb") as fh:
        fh.seek(state.get("offset", 0))
        for line in fh:
            if not line.endswith(b"\n"):
                break
            state["offset"] = state.get("offset", 0) + len(line)
            n += 1
            if (n & 8191) == 0 and time.monotonic() > deadline:
                return False
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
    _save_index(session_id, index)               # progress is kept even when the scan did not finish
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


def codex_run_counts(rows: list[dict], now: float) -> list[dict]:
    """``[{row, responses, flagged, how}]`` for each Codex row, in order. ``responses`` is what the run adds to the
    call count (see the module docstring). A finished run's count is cached by its row (a rollout never changes)."""
    from llm_router import codex_rollouts as cr

    if not rows:
        return []
    cache = _load_codex_cache()
    dirty = False
    index = None
    out: list[dict] = []
    for row in rows:
        key = f"{row.get('ts')}"
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
                cache[key] = {"responses": responses, "how": how}
                dirty = True
        flagged = responses is None and row.get("outcome") != "codex_failed"
        if responses is None:
            responses = 1 if flagged else 0
        out.append({"row": row, "responses": responses, "flagged": flagged, "how": how})
    if dirty:
        if len(cache) > 5000:                               # a bounded file: drop the oldest keys
            for k in sorted(cache, key=lambda x: float(x) if re.fullmatch(r"[0-9.]+", x) else 0.0)[:len(cache) - 5000]:
                cache.pop(k, None)
        _save_codex_cache(cache)
    return out


# ── router-executed calls (routing_decisions) ──────────────────────────────────────────────────────────────

def _ts(value: object) -> float | None:
    from datetime import datetime, timezone

    try:
        return datetime.strptime(str(value)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def router_rows(session_id: str, tool_use_ids: list[str]) -> list[dict] | None:
    """The runtime ``routing_decisions`` rows of the session, plus those named by one of its MCP ``tool_use``
    ids. ``None`` when the database cannot be read (unknown, not zero)."""
    from llm_router import ledger_view as lv

    con = lv._connect(None, 1.0)
    if con is None:
        return None
    try:
        cols = lv._columns(con, "routing_decisions")
        if not {"final_provider", "success", "session_id"} <= cols:
            return None
        want = ["id", "timestamp", "final_provider", "final_model", "success",
                "reason_code" if "reason_code" in cols else "NULL", "session_id",
                "tool_use_id" if "tool_use_id" in cols else "NULL"]
        base = f"SELECT {', '.join(want)} FROM routing_decisions WHERE "
        prov = " AND provenance = 'runtime'" if "provenance" in cols else ""
        found: dict[Any, tuple] = {}
        for row in con.execute(base + "session_id = ?" + prov, (session_id,)):
            found[row[0]] = row
        if "tool_use_id" in cols:
            ids = [t for t in tool_use_ids if t]
            for i in range(0, len(ids), _IN_CHUNK):
                chunk = ids[i:i + _IN_CHUNK]
                marks = ",".join("?" for _ in chunk)
                for row in con.execute(base + f"tool_use_id IN ({marks})" + prov, chunk):
                    found.setdefault(row[0], row)
    finally:
        con.close()
    keys = ("id", "ts", "provider", "model", "success", "reason", "session_id", "tool_use_id")
    out = []
    for row in found.values():
        d = dict(zip(keys, row))
        d["ts"] = _ts(d["ts"])
        out.append(d)
    return out


def classify_router_row(row: dict) -> str | None:
    """``local`` | ``codex`` | ``other`` (a non-Claude provider) | ``claude`` | None (no call at all)."""
    from llm_router import ledger_view as lv

    provider = str(row.get("provider") or "").strip().lower()
    if provider in ("", "none", "cache", "test") or str(row.get("reason") or "") in _NOT_A_CALL:
        return None
    if provider in _CLAUDE_PROVIDERS or _is_claude(row.get("model")):
        return "claude"
    if provider in lv.LOCAL_PROVIDERS or str(row.get("model") or "").startswith(("ollama/", "ollama_chat/")):
        return "local"
    return "codex" if provider == "codex" else "other"


def local_figures(rows: list[dict]) -> dict:
    """``{attempts, ok}`` of the local rows (see the module docstring)."""
    local = [r for r in rows if classify_router_row(r) == "local"]
    return {"attempts": len(local), "ok": sum(1 for r in local if r.get("success") == 1)}


# ── the session figure ────────────────────────────────────────────────────────────────────────────────────

def _part(code: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- one source failing must not blank the others
        failopen.record(code, exc)
        return None


def compute(session_id: str, now: float | None = None, *, budget_s: float = BUDGET_S, root: Path | None = None) -> dict | None:
    """The session's routing figure, or None for an id that is not a plain id. A source that cannot be read leaves
    its fields ``None`` (unknown); the renderers then print ``n/a``, never 0."""
    if not _SAFE_SID.fullmatch(session_id or ""):
        return None
    now = time.time() if now is None else now
    prior = _load_result(session_id)
    if prior and prior.get("complete") and 0 <= now - float(prior.get("computed_at", 0)) < FRESH_S:
        return prior
    t = _part("CHZ-FO-ROUTING-TRANSCRIPT", lambda: transcript_figures(session_id, budget_s=budget_s, root=root))
    mcp_ids = t["mcp_tool_use_ids"] if t else []
    runs = _part("CHZ-FO-ROUTING-CODEX", lambda: codex_run_counts(_ns_rows(session_id), now))
    rows = _part("CHZ-FO-ROUTING-ROWS", lambda: router_rows(session_id, mcp_ids))

    delegated_ts = [r["row"]["ts"] for r in runs or () if r["row"].get("outcome") == "delegated"]
    codex_calls = sum(r["responses"] for r in runs or ())
    by_class = {"local": 0, "codex": 0, "other": 0, "claude": 0}
    for r in rows or ():
        if r.get("success") != 1:
            continue
        kind = classify_router_row(r)
        if kind is None:
            continue
        if kind == "codex" and r.get("reason") == "direct" and r.get("ts") is not None and any(
                -_TWIN_AFTER_S <= d - r["ts"] <= _TWIN_BEFORE_S for d in delegated_ts):
            continue                                        # the delegation's own routing row: the run is counted above
        by_class[kind] += 1

    complete = bool(t and t["complete"])
    # No main transcript on disk: the Claude side is unknown, and "0 Claude calls" would make every share 100%.
    known = t is not None and t["files"]["main"] > 0 and runs is not None and rows is not None
    non_claude = (t["proxy_served"] + t["other_model"] + codex_calls + by_class["local"] + by_class["codex"]
                  + by_class["other"]) if known else None
    claude = (t["claude"] + by_class["claude"]) if known else None
    result = {
        "v": SCHEMA, "session_id": session_id, "computed_at": now, "complete": complete,
        "claude": claude, "non_claude": non_claude,
        "total": None if non_claude is None or claude is None else claude + non_claude,
        "unclassified": t["unclassified"] if t else None,
        "by_source": None if not known else {
            "codex_runs": codex_calls, "router_local": by_class["local"], "router_codex": by_class["codex"],
            "router_other": by_class["other"], "proxy": t["proxy_served"], "transcript_other": t["other_model"]},
        "codex": None if runs is None else {
            "runs": len(runs), "responses": codex_calls, "flagged_runs": sum(1 for r in runs if r["flagged"])},
        "local": local_figures(rows) if rows is not None else None,
        "files": t["files"] if t else None,
    }
    if t is not None:
        _atomic_json(result_path(session_id), result)
    return result


# ── the 7-day organic window (the call mix's extra figures) ─────────────────────────────────────────────────

def window_extras(since: float, until: float, now: float, kind_of) -> dict:
    """What the 7-day call mix adds on top of the NS unit list, over ``[since, until]`` and the organic population
    (``kind_of(session_id, stamp)`` -> true for a session of the population):

    * ``codex_extra``: model responses beyond the one the unit list already counts for each Codex run in a counted
      session (minus one for a failed run that never got a response: the list counts that unit, nothing was served), ``by_session`` its per-session split, ``runs`` / ``flagged_runs`` for the disclosure;
    * ``local``: ``{attempts, ok}`` of the local ``routing_decisions`` rows of the same sessions.

    Rows with no session id cannot be placed in a population and are left out; ``unattributed_local`` counts them."""
    rows = [r for r in _ns_rows(None) if since <= r["ts"] <= until and kind_of(r.get("session_id"), r.get("session_kind"))]
    runs = codex_run_counts(rows, now)
    extra = 0
    by_session: dict[str, int] = {}
    for r in runs:
        row = r["row"]
        if not row.get("model"):
            continue                                        # the unit list leaves a model-less row unclassified
        more = r["responses"] - 1       # -1: a failed run with no response is a unit the list counts and that served nothing
        extra += more
        if more and isinstance(row.get("session_id"), str):
            by_session[row["session_id"]] = by_session.get(row["session_id"], 0) + more
    local = {"attempts": 0, "ok": 0}
    unattributed = 0
    from datetime import datetime, timezone

    from llm_router import ledger_view as lv

    con = lv._connect(None, 1.0)
    if con is not None:
        try:
            cols = lv._columns(con, "routing_decisions")
            if {"final_provider", "success", "session_id", "timestamp"} <= cols:
                prov = " AND provenance = 'runtime'" if "provenance" in cols else ""
                reason = "reason_code" if "reason_code" in cols else "NULL"
                lo = datetime.fromtimestamp(since - 86400.0, timezone.utc).strftime("%Y-%m-%d")
                for sid, ts, provider, model, success, why in con.execute(
                        f"SELECT session_id, timestamp, final_provider, final_model, success, {reason} "
                        f"FROM routing_decisions WHERE timestamp >= ?{prov}", (lo,)):
                    t = _ts(ts)
                    if t is None or not since <= t <= until:
                        continue
                    row = {"provider": provider, "model": model, "reason": why}
                    if classify_router_row(row) != "local":
                        continue
                    if not sid or not kind_of(sid, None):
                        unattributed += 0 if sid else 1
                        continue
                    local["attempts"] += 1
                    local["ok"] += 1 if success == 1 else 0
        finally:
            con.close()
    return {"codex_extra": extra, "by_session": by_session, "runs": len(runs),
            "flagged_runs": sum(1 for r in runs if r["flagged"]), "local": local, "unattributed_local": unattributed}
