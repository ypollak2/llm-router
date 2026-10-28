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


def _open_ro(db: Path) -> sqlite3.Connection:
    """``mode=ro``; a WAL db with no -shm cannot be opened that way (SQLite must create the -shm), so,
    only when no non-empty -wal file exists (nothing unmerged to miss), read the main file ``immutable=1``."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError:
        wal = Path(str(db) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=2.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    con.execute("PRAGMA query_only = ON")
    return con


def _read_db(db: Path, since: float, until: float) -> tuple[dict[str, dict], list[dict], str | None]:
    """(snapshots by sig, decision rows in window, error). Opened read-only (:func:`_open_ro`);
    a db that cannot be opened that way is reported unreadable (not evaluable), never opened for writing."""
    if not db.exists():
        return {}, [], None
    try:
        con = _open_ro(db)
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
            return snaps, rows, None
        finally:
            con.close()
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
    proxy_rows = _read_proxy(Path(proxy_path), since, until)
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

    evaluated = sum(_tot(k) for k in ("eligible", "ineligible", "unknown_to_map"))
    ineligible = _tot("ineligible")
    not_evaluable = _tot("not_evaluable_no_sig") + _tot("not_evaluable_sig_unresolved")
    organic = per_pop["organic"]
    organic_eval = organic["eligible"] + organic["ineligible"] + organic["unknown_to_map"]
    largest = max(organic_sessions.values()) if organic_sessions else 0
    share = largest / organic_eval if organic_eval else None
    informative = (organic_eval >= MIN_ORGANIC_N and len(organic_sessions) >= MIN_SESSIONS
                   and share is not None and share <= MAX_SESSION_SHARE)
    return {
        "window": [since, until], "rows_read": len(rows), "evaluated_n": evaluated,
        "ineligible": ineligible, "not_evaluable": not_evaluable,
        "evaluable": evaluated > 0,
        "populations": {p: dict(c) for p, c in per_pop.items()},
        "per_door": {k: dict(v) for k, v in per_door.items()},
        "organic_evaluated_n": organic_eval, "organic_ineligible": organic["ineligible"],
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
        f"{sum(pops[p].get(k, 0) for k in ('eligible', 'ineligible', 'unknown_to_map'))}"
        for p in POPULATIONS if any(pops[p].get(k) for k in ("eligible", "ineligible", "unknown_to_map")))
    tail = "" if res["informative"] else (
        f"; not informative (organic n={res['organic_evaluated_n']} of {MIN_ORGANIC_N}, "
        f"{res['organic_sessions']} of {MIN_SESSIONS} sessions)")
    return (f"{head} {res['ineligible']} of {res['evaluated_n']} evaluable ({split}); "
            f"{n_ne} not evaluable (no resolvable stamp){tail}")
