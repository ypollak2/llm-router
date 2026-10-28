"""GOAL-INELIG / W1-2: count routes to a model that was NOT eligible at decision time.

Post-hoc and read-only (``llm-router kpi`` and ``llm-router doctor`` call it; no routing path does).
Sources: ``usage.db`` ``routing_decisions`` (MCP router, hook / SDK / agent-route doors) and
``proxy_calls.jsonl`` (proxy door). Each row's ``inventory_sig`` (:mod:`decision_stamp`, W0-1)
resolves through ``usage.db`` ``inventory_snapshots`` to the map rows the eligibility filter saw
at that instant.

What a ledger row can prove (and what the counter checks):

* provider level  the chosen model's provider is map-known and has no connected row;
* model level     for a local provider the exact ``chain_id`` row must exist and be connected.

Not countable from the ledgers, and reported as such rather than counted as eligible:
capability (vision / token needs are not stored per decision) and privacy (no per-decision
local-only flag).

A row with no ``inventory_sig``, or a signature with no snapshot, is **not evaluable**; it is never
counted as eligible. Cache hits and no-route rows are not routes. An empty or wholly unevaluable
window reads "not evaluable", never "0 ineligible". Rows are split by population: ``organic``,
``synthetic`` (``is_real = 0`` rows and harness / headless sessions), ``coordinator`` (research
sessions: the coordinator runs from ``~/.rsi`` or a scratchpad), ``untagged`` (no resolvable kind).
The informative bar (07-ACCEPTANCE section 4) is judged on organic rows only.
"""
from __future__ import annotations

import collections
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 07-ACCEPTANCE section 4 live clause: >= 300 organic evaluated decisions, >= 3 sessions, largest <= 60%.
MIN_ORGANIC_N = 300
MIN_SESSIONS = 3
MAX_SESSION_SHARE = 0.60

POPULATIONS = ("organic", "synthetic", "coordinator", "untagged")
_PROVIDER_ALIASES = {"claude": "anthropic", "gemini-cli": "gemini_cli"}
_NOT_A_ROUTE = {"cache", "none", ""}
_NOT_CHECKABLE = ("capability (vision / token needs are not stored per decision)",
                  "privacy (no per-decision local-only flag)")


def _provider_and_id(model: str | None, provider: str | None) -> tuple[str, str]:
    m = (model or "").strip()
    p = (provider or "").strip().lower()
    if m.startswith("cache/"):
        return "cache", m
    if "/" in m:
        mp = m.split("/", 1)[0].lower()
        p = p or mp
        if mp == p:
            return _PROVIDER_ALIASES.get(p, p), m
    p = _PROVIDER_ALIASES.get(p, p)
    return p, (f"{p}/{m}" if m and p else m)


def judge(snapshot: dict, provider: str, chain_id: str) -> tuple[str, str]:
    """``('eligible' | 'ineligible' | 'unknown_to_map', reason)`` for one decision's snapshot."""
    by_prov: dict[str, list[dict]] = collections.defaultdict(list)
    for r in snapshot.get("rows") or []:
        by_prov[str(r.get("chain_id", "")).split("/", 1)[0]].append(r)
    prows = by_prov.get(provider)
    if not prows:
        return "unknown_to_map", "provider has no map row"
    if not any(r.get("status") == "connected" for r in prows):
        return "ineligible", f"provider {provider} map-known, no connected row"
    if {r.get("kind") for r in prows} == {"local"}:
        row = next((r for r in prows if r.get("chain_id") == chain_id), None)
        if row is None:
            return "ineligible", f"model-level: {chain_id} absent from the inventory"
        if row.get("status") != "connected":
            return "ineligible", f"model-level: {chain_id} status {row.get('status')}"
    return "eligible", "connected"


def _ts(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str) and len(raw) >= 19:
        try:
            return datetime.strptime(raw[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def _file_sig(db: Path) -> tuple:
    """(exists, size, mtime_ns) of the db, its -wal and its -shm: what must not move during an immutable read."""
    out = []
    for suffix in ("", "-wal", "-shm"):
        try:
            st = Path(str(db) + suffix).stat()
            out.append((True, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((False, 0, 0))
    return tuple(out)


def _open_ro(db: Path, *, allow_immutable: bool = True) -> tuple[sqlite3.Connection, bool]:
    """``(connection, immutable)``. ``mode=ro``; a WAL db with no -shm cannot be opened that way (SQLite
    must create the -shm), so, only when no non-empty -wal file exists, read the main file ``immutable=1``
    (the caller then verifies nothing moved during the read). Never rw / rwc."""
    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        immutable = False
    except sqlite3.OperationalError:
        if con is not None:
            con.close()
        wal = Path(str(db) + "-wal")
        if not allow_immutable or (wal.exists() and wal.stat().st_size > 0):
            raise
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=2.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        immutable = True
    con.execute("PRAGMA query_only = ON")
    return con, immutable


def _read_db(db: Path, since: float, until: float, retry: bool = False) -> tuple[dict[str, dict], list[dict], str | None]:
    """(snapshots by sig, decision rows in window, error). Opened read-only (:func:`_open_ro`);
    a db that cannot be opened that way is reported unreadable (not evaluable), never opened for writing."""
    if not db.exists():
        return {}, [], None
    try:
        before = _file_sig(db)
        con, immutable = _open_ro(db, allow_immutable=not retry)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            snaps: dict[str, dict] = {}
            if "inventory_snapshots" in tables:
                for sig, body in con.execute("SELECT sig, body_json FROM inventory_snapshots"):
                    try:
                        snaps[sig] = json.loads(body)
                    except ValueError:
                        pass
            rows: list[dict] = []
            if "routing_decisions" in tables:
                cols = {r[1] for r in con.execute("PRAGMA table_info(routing_decisions)")}
                sel = ["id", "timestamp", "final_model", "final_provider", "reason_code", "classifier_type"]
                sel = [c for c in sel if c in cols]
                extra = [c for c in ("inventory_sig", "session_id", "is_real") if c in cols]
                day_floor = time.strftime("%Y-%m-%d", time.gmtime(since - 86400))
                q = f"SELECT {', '.join(sel + extra)} FROM routing_decisions WHERE timestamp >= ?"
                for raw in con.execute(q, (day_floor,)):
                    d = dict(zip(sel + extra, raw))
                    t = _ts(d.get("timestamp"))
                    if t is None or not (since <= t <= until):
                        continue
                    rc, ct = d.get("reason_code") or "", d.get("classifier_type")
                    door = ("agent-route" if ct == "agent-route-cli" else "hook/sdk-direct" if rc == "direct"
                            else "mcp-router" if rc.startswith("router_") else "other")
                    rows.append({"model": d.get("final_model"), "provider": d.get("final_provider"),
                                 "sig": d.get("inventory_sig"), "session": d.get("session_id"),
                                 "door": door, "real": d.get("is_real") != 0, "stamp": None})
        finally:
            con.close()
        if immutable and _file_sig(db) != before:  # a writer moved under an unlocked read: do not trust it
            if retry:
                return {}, [], "usage.db changed during read"
            snaps, rows, err = _read_db(db, since, until, retry=True)
            return (snaps, rows, err) if err is None else ({}, [], "usage.db changed during read")
        return snaps, rows, None
    except sqlite3.Error as exc:
        return {}, [], f"usage.db unreadable: {exc}"


def _read_proxy(path: Path, since: float, until: float) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("ts")
            if isinstance(d, dict) and isinstance(t, (int, float)) and since <= t <= until:
                rows.append({"model": d.get("served_model"), "provider": d.get("served_by"),
                             "sig": d.get("inventory_sig"), "session": d.get("session_id"),
                             "door": "proxy", "real": True, "stamp": d.get("session_kind")})
    return rows


def _population(row: dict, index) -> str:
    if not row["real"]:
        return "synthetic"
    kind = index.resolve(row["session"], row["stamp"]).kind
    return {"organic": "organic", "research": "coordinator", "harness": "synthetic",
            "headless": "synthetic"}.get(kind, "untagged")


def compute(*, since: float | None = None, until: float | None = None, days: float = 7.0,
            db_path: Path | None = None, proxy_path: Path | None = None) -> dict:
    """The ineligible-route counter over ``[since, until]`` (default: the last ``days`` days)."""
    from llm_router import paths, session_kind

    until = time.time() if until is None else until
    since = until - days * 86400 if since is None else since
    if db_path is None:
        try:
            from llm_router.config import get_config
            db_path = Path(get_config().llm_router_db_path)
        except Exception:  # noqa: BLE001 - report-only: fall back to the canonical path
            db_path = paths.state_path("usage.db")
    proxy_path = proxy_path or paths.state_path("proxy_calls.jsonl")

    snaps, rows, err = _read_db(Path(db_path), since, until)
    try:
        proxy_rows = _read_proxy(Path(proxy_path), since, until)
    except OSError as exc:
        proxy_rows = []
        err = err or f"proxy_calls.jsonl unreadable: {type(exc).__name__}"
    rows += proxy_rows
    index = session_kind.KindIndex(
        [{"session_id": r["session"], "session_kind": r["stamp"]} for r in proxy_rows], backfill=False)

    per_pop = {p: collections.Counter() for p in POPULATIONS}
    per_door: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    organic_sessions: collections.Counter = collections.Counter()
    examples: list[dict] = []
    for r in rows:
        prov, cid = _provider_and_id(r["model"], r["provider"])
        pop = _population(r, index)
        if prov in _NOT_A_ROUTE or (r["model"] or "") in _NOT_A_ROUTE:
            per_pop[pop]["not_a_route"] += 1
            continue
        snap = snaps.get(r["sig"]) if r["sig"] else None
        if snap is None:
            key = "not_evaluable_no_sig" if not r["sig"] else "not_evaluable_sig_unresolved"
            per_pop[pop][key] += 1
            per_door[r["door"]][key] += 1
            continue
        verdict, why = judge(snap, prov, cid)
        per_pop[pop][verdict] += 1
        per_door[r["door"]][verdict] += 1
        if pop == "organic":
            organic_sessions[r["session"]] += 1
        if verdict == "ineligible" and len(examples) < 20:
            examples.append({"population": pop, "door": r["door"], "model": cid, "why": why})

    def _tot(counter_key: str) -> int:
        return sum(c[counter_key] for c in per_pop.values())

    evaluated = _tot("eligible") + _tot("ineligible")  # a provider the map never knew is not judged either way
    unknown = _tot("unknown_to_map")
    ineligible = _tot("ineligible")
    not_evaluable = _tot("not_evaluable_no_sig") + _tot("not_evaluable_sig_unresolved")
    organic = per_pop["organic"]
    organic_eval = organic["eligible"] + organic["ineligible"]
    largest = max(organic_sessions.values()) if organic_sessions else 0
    share = largest / organic_eval if organic_eval else None
    informative = (organic_eval >= MIN_ORGANIC_N and len(organic_sessions) >= MIN_SESSIONS
                   and share is not None and share <= MAX_SESSION_SHARE)
    return {
        "window": [since, until], "rows_read": len(rows), "evaluated_n": evaluated,
        "unknown_to_map_n": unknown, "not_evaluable": not_evaluable,
        "evaluable": evaluated > 0 and not err,
        # None, never 0, when nothing could be evaluated: an unevaluable set must not read as 0 ineligible.
        "ineligible": ineligible if evaluated > 0 and not err else None,
        "populations": {p: dict(c) for p, c in per_pop.items()},
        "per_door": {k: dict(v) for k, v in per_door.items()},
        "organic_evaluated_n": organic_eval, "organic_ineligible": organic["ineligible"] if evaluated > 0 and not err else None,
        "organic_sessions": len(organic_sessions), "largest_session_share": share,
        "informative": informative, "not_checkable": list(_NOT_CHECKABLE),
        "error": err, "examples": examples,
    }


def render_line(res: dict) -> str:
    """One line. Never says "0 ineligible" unless something was actually evaluated."""
    days = (res["window"][1] - res["window"][0]) / 86400.0
    head = f"ineligible routes ({days:.1f}d):"
    if res.get("error"):
        return f"{head} not evaluable ({res['error']})"
    n_ne = res["not_evaluable"]
    if not res["evaluable"]:
        if n_ne:
            return (f"{head} not evaluable: {n_ne} route(s) carry no resolvable inventory stamp "
                    f"(n={res['rows_read']} rows); unstamped rows are never counted as eligible")
        return f"{head} not evaluable: no routed decisions in the window (n={res['rows_read']} rows)"
    pops = res["populations"]
    split = ", ".join(
        f"{p} {pops[p].get('ineligible', 0)}/"
        f"{sum(pops[p].get(k, 0) for k in ('eligible', 'ineligible'))}"
        for p in POPULATIONS if any(pops[p].get(k) for k in ("eligible", "ineligible")))
    tail = "" if res["informative"] else (
        f"; not informative (organic n={res['organic_evaluated_n']} of {MIN_ORGANIC_N}, "
        f"{res['organic_sessions']} of {MIN_SESSIONS} sessions)")
    unk = f"; {res['unknown_to_map_n']} unknown to the map" if res.get("unknown_to_map_n") else ""
    return (f"{head} {res['ineligible']} of {res['evaluated_n']} evaluable ({split}); "
            f"{n_ne} not evaluable (no resolvable stamp){unk}{tail}")


def error_result(since: float, until: float, reason: str) -> dict:
    """A not-evaluable result for a counter that could not run at all (reason shown by :func:`render_line`)."""
    return {"window": [since, until], "rows_read": 0, "evaluated_n": 0, "ineligible": None,
            "organic_ineligible": None, "not_evaluable": 0, "unknown_to_map_n": 0, "evaluable": False,
            "populations": {}, "per_door": {}, "organic_evaluated_n": 0, "organic_sessions": 0,
            "largest_session_share": None, "informative": False, "not_checkable": list(_NOT_CHECKABLE),
            "error": reason, "examples": []}
