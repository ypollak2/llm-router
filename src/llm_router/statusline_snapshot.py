"""The per-session statusline snapshot: five fields, each with its n and window (P35-2, D-220).

ONE writer, :func:`refresh`, runs only in a detached refresher (``statusline_segments`` for the
default layout, ``llm-router statusline --refresh --host H --session S`` for Codex / Pi / the fast
tick). It writes ``snapshots/<host>-<session>.json`` (0600, atomic, newest :data:`KEEP` kept) with a
pre-rendered ``line``; every reader (the shell, the Codex Stop hook, the Pi extension) prints that
line and computes nothing, so the surfaces agree by construction::

    router: <model> (<reason>) | offload <x>% (n=, session)
        | Claude 5h <p>% (n=1 poll, age <m>m, 5h) / Codex <k>/<budget> delegations (5h)
        | quality <q>% (n=, 7d) | saved ~<h> work-hours (n=, rate n=, today)

Rules: an unknown field prints ``n/a`` (never 0) and still names its n and window; a snapshot older
than :data:`STALE_S` reads as ``n/a`` as a whole; no model or plan name is written here (D-205), every
name is a value read from a ledger. A part that fails is recorded (``failopen``) and prints ``n/a``.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from llm_router import failopen, paths

SCHEMA = 1
#: A snapshot older than this is not data: the line reads ``n/a``.
STALE_S = 15 * 60.0
KEEP = 20
DIR_NAME = "snapshots"
#: usage.json older than this is not a quota reading (same bar as the status line cache).
QUOTA_STALE_S = 1800.0
QUALITY_DAYS = 7
SESSION_ACTION_DAYS = 7
BURN_DAYS = 14
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
#: C0, DEL and C1 (U+0080-U+009F: U+009B is CSI on some terminals) controls, and the field separator.
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f|]")
_NAME_MAX = 48


def _clean(value: object) -> str:
    """One printable token: no control characters and no ``|`` (the field separator)."""
    return _CTRL.sub(" ", str(value)).strip()[:_NAME_MAX] or "n/a"


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def snapshot_path(host: str, session_id: str) -> Path:
    return paths.state_path(DIR_NAME, f"{_UNSAFE.sub('_', host)}-{_UNSAFE.sub('_', session_id)}.json")


def _part(code: str, fn):
    """``fn()``, or None (unknown) after recording why it failed."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- one field failing must not blank the others
        failopen.record(code, exc)
        return None


# ── the five fields ────────────────────────────────────────────────────────────

def _proxy_newest(session_id: str) -> tuple[float, str, str] | None:
    """(ts, model, reason) of the newest call row of this session in ``proxy_calls.jsonl``, read
    from the tail (the file only grows; the newest row of a live session is at the end)."""
    path = paths.state_path("proxy_calls.jsonl")
    if not path.is_file():
        return None                                  # no proxy ledger: the router field falls back to the db
    size = path.stat().st_size
    window = 256 * 1024
    while True:
        start = max(0, size - window)
        with path.open("rb") as fh:
            fh.seek(start)
            lines = fh.read(size - start).split(b"\n")
        if start:
            lines = lines[1:]                       # the first line is cut mid-way
        for line in reversed(lines):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("session_id") != session_id:
                continue
            ts = _num(r.get("ts"))
            model = r.get("served_model") or r.get("requested_model")
            if ts is not None and model and r.get("decision") in ("served", "forwarded", "fallback"):
                return ts, _clean(model), _clean(r.get("tier_reason") or r.get("reason") or "n/a")
        if start == 0 or window >= 8 * 1024 * 1024:
            return None
        window *= 2


def _decision_newest(session_id: str) -> tuple[float, str, str] | None:
    from llm_router import ledger_view as lv
    from llm_router.provider_classes import real_decision_sql

    con = lv._connect(None, 0.5)
    if con is None:
        return None
    try:
        cols = lv._columns(con, "routing_decisions")
        if "session_id" not in cols:
            return None
        reason = "reason_code" if "reason_code" in cols else "NULL"
        runtime = " AND provenance = 'runtime'" if "provenance" in cols else ""   # not test / unknown rows
        row = con.execute(f"SELECT timestamp, COALESCE(final_model, final_provider), {reason} "
                          "FROM routing_decisions WHERE session_id = ? "
                          f"AND COALESCE(final_model, final_provider) IS NOT NULL{runtime} "
                          f"AND ({real_decision_sql(con)}) "
                          "ORDER BY timestamp DESC, id DESC LIMIT 1", (session_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    ts = datetime.strptime(str(row[0])[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()
    return ts, _clean(row[1]), _clean(row[2] or "n/a")


def router_field(session_id: str) -> dict:
    """The newest call of the session, a proxy turn or a routing decision (proxy turns count)."""
    found = [r for r in (_part("CHZ-FO-SNAP-ROUTER-PROXY", lambda: _proxy_newest(session_id)),
                         _part("CHZ-FO-SNAP-ROUTER-DB", lambda: _decision_newest(session_id))) if r]
    if not found:
        return {"model": None, "reason": None, "ts": None}
    ts, model, reason = max(found, key=lambda r: r[0])
    return {"model": model, "reason": reason, "ts": ts}


def _actions(session_id: str, now: float) -> list[dict]:
    from llm_router import offload_share as osh

    return osh.action_units(now - SESSION_ACTION_DAYS * 86400.0, now)["actions"]


def offload_field(actions: list[dict] | None, session_id: str) -> dict:
    """S1 per-action offload for THIS session: k offloaded and quality-sampled, n actions. A session
    whose lanes are all unsampled has no measured numerator: unknown, not 0."""
    mine = [a for a in actions or () if a.get("session_id") == session_id]
    k = sum(1 for a in mine if a["numerator"])
    sampled = any(a["sampled"] for a in mine)
    return {"k": k, "n": len(mine), "window": "session",
            "value": (k / len(mine)) if mine and sampled else None,
            "why": None if sampled or not mine else "no quality-sampled lane"}


def quota_field(now: float) -> dict:
    """Claude 5h from ONE ``usage.json`` poll (not a fallback, not pending, not stale) and Codex from the
    delegation window.

    The Claude % is a single reading, so its n is that reading: ``n=1`` plus the poll's age when it is
    used, ``n=0`` and no value when there is none. The 5-minute ``quota_history.jsonl`` rows are other
    readings and are NOT the n of this figure (rv-632 must-fix 3).

    Codex is a COUNT, not a %: ``used`` = delegations ``codex_window`` counted in its rolling 5h window,
    against ``budget`` (the router's own delegation budget). 0 is a measured count of delegations, so it
    is printed as ``0/<budget>``; a % of it would read as a value with n=0 (D-237). No
    ``codex_window.json`` (never delegated on this machine) or no budget: both None, the field is n/a."""
    out: dict = {"claude": {"pct": None, "n": 0, "age_s": None, "window": "5h"},
                 "codex": {"used": None, "budget": None, "window": "5h"}}
    try:
        u = json.loads(paths.state_path("usage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        u = None                                  # no readable poll: n=0, the field prints n/a
    pct = _num(u.get("session_pct")) if isinstance(u, dict) and not u.get("is_fallback") \
        and not u.get("pending") else None
    upd = _num(u.get("updated_at")) if isinstance(u, dict) else None
    if pct is not None and upd is not None and 0 <= now - upd <= QUOTA_STALE_S:
        out["claude"].update(pct=pct, n=1, age_s=now - upd)
    if paths.state_path("codex_window.json").exists():
        from llm_router import codex_window

        s = codex_window.snapshot(now)
        out["codex"].update(used=s.used, budget=s.budget)
    return out


def quality_field(now: float) -> dict:
    from llm_router import shadow_frontier

    return shadow_frontier.verdict_summary(now=now, days=QUALITY_DAYS)


def turns_off_claude(actions: list[dict] | None, since: float) -> int:
    """Human TURNS served off Claude since ``since``: the unit the burn rate below is measured in.

    An action counts when it begins a human turn (``first``: a ``turn_first`` proxy row the transcript, or
    with none the ledger, keeps as a typed prompt's answer; or a zero-Claude hook turn), came in by the
    proxy, sub-agent or hook door, was served by a local, very-cheap or other-Frontier model, and was not
    redone. Left out: continuation calls (about 8.5 per human turn), MCP / direct ``local_assist`` calls
    and technical ops (Claude made the turn's first call, so its quota was spent anyway: offload_share's
    own M0.3 rule), Haiku (a Claude call), and redone turns (Claude answered them again)."""
    from llm_router import offload_share as osh

    off = {osh.SC_LOCAL, osh.SC_CHEAP, osh.SC_SPREAD}
    doors = {osh.DOOR_PROXY, osh.DOOR_SUBAGENT, osh.DOOR_HOOK}
    return sum(1 for a in actions or () if a["first"] and a["door"] in doors and a["served_class"] in off
               and not a["redone"] and a["ts"] >= since)


def saved_field(actions: list[dict] | None, now: float):
    """``~work-hours`` saved today = (turns served off Claude today x weekly pp per Claude turn) / median
    weekly pp per active hour.

    Both factors are per human turn (rv-632 must-fix 1): :func:`turns_off_claude` counts turns, and
    ``quota_burn``'s measured ``weekly_per_turn`` is weekly pp per Stop turn. That burn is a POOLED
    RATIO over the last :data:`BURN_DAYS` days (sum of per-session weekly deltas / sum of their Stop
    turns), not the plan's median per action: ``quota_samples`` records quota at session start and at
    each Stop, never per model call, so no per-action burn can be measured from it. The rate below
    (``quota_rate``) is a median per active hour. No turn served off Claude today is n/a, not 0."""
    from llm_router import ledger_view as lv
    from llm_router import quota_rate, quota_samples

    n = turns_off_claude(actions, lv.resolve("today", now=now).since_ts) if actions is not None else None
    line = quota_samples.quota_burn(now - BURN_DAYS * 86400.0, now)["measured"]
    per_turn = line["weekly_per_turn"]
    pp = None if per_turn is None or n is None else n * per_turn
    rate = quota_rate.median_pp_per_active_hour(now=now)
    fig = quota_rate.work_hours_figure(pp, n if pp is not None else None, rate, window="today",
                                       as_of=lv._iso(now), source="quota_burn pooled pp/turn x quota_rate median")
    if n == 0:     # nothing served off Claude: unmeasured, never "~0"; this reason before a rate's
        fig = lv.Figure(None, 0, "today", fig.as_of, fig.source, "estimated",
                        "n=0 turns off Claude, today", fig.n_basis)
    return fig


# ── the line ───────────────────────────────────────────────────────────────────

def _pct(label: str, value: float | None, n, window: str) -> str:
    ns = "n/a" if n is None else f"n={n}"
    return f"{label} {value:.0f}% ({ns}, {window})" if value is not None else f"{label} n/a ({ns}, {window})"


def render_line(f: dict) -> str:
    """The pre-rendered line from the fields (pure)."""
    from llm_router import ledger_view as lv

    r = f["router"]
    router = f"router: {r['model']} ({r['reason']})" if r["model"] else "router: n/a"
    o = f["offload"]
    if o["value"] is not None:
        offload = f"offload {100 * o['value']:.0f}% (n={o['n']}, {o['window']})"
    else:
        why = f", {o['why']}" if o.get("why") else ""
        offload = f"offload n/a (n={o['n']}, {o['window']}{why})"
    q = f["quota"]
    c = q["claude"]
    if c["pct"] is not None and c.get("n") == 1 and c.get("age_s") is not None:
        claude = f"Claude 5h {c['pct']:.0f}% (n=1 poll, age {int(c['age_s'] // 60)}m, {c['window']})"
    else:                                         # no reading behind it: never a value with n=0
        claude = f"Claude 5h n/a (n=0, {c['window']})"
    cx = q["codex"]
    if cx.get("used") is not None and cx.get("budget") is not None:   # a measured count, 0 included
        codex = f"Codex {cx['used']}/{cx['budget']} delegations ({cx['window']})"
    else:
        codex = f"Codex n/a (n/a, {cx['window']})"
    quota = claude + " / " + codex
    ql = f["quality"]
    quality = (_pct("quality", None if ql["rate"] is None else 100 * ql["rate"], ql["n"], ql["window"]))
    fig = lv.Figure.from_dict(f["saved"]) if f["saved"] else None
    text = lv.show(fig, unit="subscription", quantity="work_hours", with_n=True)
    why = fig.na_reason if fig and fig.na_reason else "n/a"
    # a reason that is already "n/a (...)" is printed as is, not wrapped in a second n/a
    saved = f"saved {text}" if text else (f"saved {why}" if why.startswith("n/a (") else f"saved n/a ({why})")
    parts = [router, offload, quota, quality, saved]
    j = f.get("judge")
    if j:
        parts.append(f"judge {j['mean']:.2f} (n={j['n']}, {j['window']})")
    return " | ".join(parts)


def _judge_field() -> dict | None:
    from llm_router import judge_view

    d = judge_view.read_cache()
    if not d or not d.get("n") or d.get("mean") is None:
        return None
    return {"mean": d["mean"], "n": d["n"], "window": d["window"]["label"]}


def build(session_id: str, now: float) -> dict:
    """The fields (each failing on its own to unknown) and the rendered line."""
    actions = _part("CHZ-FO-SNAP-ACTIONS", lambda: _actions(session_id, now))
    saved = _part("CHZ-FO-SNAP-SAVED", lambda: saved_field(actions, now))
    quality = _part("CHZ-FO-SNAP-QUALITY", lambda: quality_field(now)) or {
        "n": 0, "k": 0, "rate": None, "wilson": None, "window": f"{QUALITY_DAYS}d"}
    fields = {
        "router": _part("CHZ-FO-SNAP-ROUTER", lambda: router_field(session_id))
        or {"model": None, "reason": None, "ts": None},
        "offload": offload_field(actions, session_id),
        "quota": _part("CHZ-FO-SNAP-QUOTA", lambda: quota_field(now))
        or {"claude": {"pct": None, "n": 0, "age_s": None, "window": "5h"},
            "codex": {"used": None, "budget": None, "window": "5h"}},
        "quality": quality,
        "saved": saved.to_dict() if saved is not None else None,
        "judge": _part("CHZ-FO-SNAP-JUDGE", _judge_field),
    }
    return {"v": SCHEMA, "written_at": now, "session_id": session_id, "fields": fields,
            "line": render_line(fields)}


# ── write / read ───────────────────────────────────────────────────────────────

def _prune(keep: int = KEEP) -> None:
    files = sorted(paths.state_path(DIR_NAME).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        p.unlink(missing_ok=True)


def refresh(*, host: str, session_id: str, now: float | None = None) -> dict:
    """Compute the snapshot and write it (0600, atomic, newest :data:`KEEP` kept). Refresher only:
    it reads the ledgers (hundreds of ms), so no hook or shell calls it synchronously."""
    now = time.time() if now is None else now
    data = {"host": host, **build(session_id, now)}
    path = snapshot_path(host, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _part("CHZ-FO-SNAP-PRUNE", _prune)
    # F107: the fast tick has no refresher of its own; its `--refresh` lands here (daily, off the sync path).
    from llm_router import statusline_tick
    _part("F107-HISTORY-PRUNE", lambda: statusline_tick.prune_history(str(paths.llm_router_home()), now))
    return data


def read(host: str, session_id: str) -> dict | None:
    """The snapshot as written, or None (absent, unreadable, another schema, no usable stamp or line).
    Staleness is the caller's call (:func:`read_line` applies :data:`STALE_S`)."""
    try:
        d = json.loads(snapshot_path(host, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("v") != SCHEMA or _num(d.get("written_at")) is None \
            or not isinstance(d.get("line"), str):
        return None
    return d


def read_line(host: str, session_id: str, *, now: float | None = None) -> str:
    """The pre-rendered line, or ``n/a`` when there is no snapshot, it is unreadable, or it is older
    than :data:`STALE_S`. One small file read; imports nothing heavy."""
    now = time.time() if now is None else now
    d = read(host, session_id)
    if d is None or not 0 <= now - d["written_at"] <= STALE_S:
        return "n/a"
    return d["line"]
