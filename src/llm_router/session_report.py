"""Session summary (P35-4, D-220): every figure of one session, each with its n and window.

``build(session_id)`` (or ``build(scope="today")``) returns plain dicts; ``render(report)`` prints the
block the session-end child appends to ``last_summary.ansi`` and ``llm-router summary --session`` prints.
Nothing here runs on a hook's synchronous path: it reads the proxy ledger, ``usage.db`` and the verdict
files (hundreds of ms), so only the detached child and the CLI call it.

Fields (unknown is ``None`` and prints ``n/a``, never 0):

* ``targets``: the session's ACTIONS (:func:`offload_share.action_units`, the S1 unit) by who served
  them: ``local``, ``cheap`` (Haiku and other very-cheap models), ``frontier`` (Claude or another paid
  model) and ``spread`` (another Frontier subscription). ``offload_numerator`` = ``local + cheap``: spread
  work is in ``n`` only, never in the numerator (D-24).
* ``offload``: S1 per action: k = actions in the S1 numerator (served off-Frontier, not redone, lane
  quality-sampled), n = all actions. A session with no quality-sampled lane is unknown, not 0.
* ``escalations``: proxy rows whose ``tier_reason`` is an escalation (``offload_share.ESCALATION_REASONS``).
* ``cache_hit_rate``: semantic-cache hits / lookups of the scope; below
  ``semantic_cache.MIN_INFORMATIVE_LOOKUPS`` lookups the rate is ``None`` (the counts are kept).
* ``quota_saved``: ~ estimate. Human turns served off Claude x measured weekly pp per Claude turn
  (``quota_samples.quota_burn``, 14 days) = ``~% of window``; divided by the median pp per active hour
  (``quota_rate``) = ``~work-hours``. Both carry their n's.
* ``quality``: quality vs Frontier from the blind-judge verdicts (``shadow_frontier.verdict_summary``) (7 days; not per session: the verdict
  rows carry no session id): k acceptable of n decided, beside the Frontier answer's own rate.
  ``judge``: the judge cache (``judge_summary.json``) as a separate line.
* ``recommendation``: computed from ``targets`` and ``quality`` (see :func:`recommend`), marked ``~``,
  or ``no recommendation (n/a: <input>)`` naming the missing input.
"""
from __future__ import annotations

import re
import time
from collections import Counter

from llm_router import failopen
from llm_router import ledger_view as lv

SCHEMA = 1
#: How far back the session's actions are read. A session is one Claude Code conversation; rows of the
#: session older than this are not in the summary (the window label says ``session``).
SESSION_LOOKBACK_DAYS = 7
QUALITY_DAYS = 7
BURN_DAYS = 14
MARKER = "── session summary (P35-4) ──"


# ── readers ────────────────────────────────────────────────────────────────────

def _part(code: str, fn, default=None):
    """Run one reader; a failure is recorded (T14) and the field is unknown."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- one field failing never loses the others
        failopen.record(code, exc)
        return default


def _in_scope(row: dict, bounds: lv.Bounds) -> bool:
    ts = row.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not bounds.covers(float(ts)):
        return False
    return bounds.session_id is None or row.get("session_id") == bounds.session_id


def _actions(bounds: lv.Bounds, proxy_rows: list[dict]) -> list[dict]:
    """The scope's S1 actions. A session scope admits that session whatever its kind (the summary is
    about the user's own session); a time window keeps the KPI population (organic sessions)."""
    from llm_router import offload_share as osh

    until = bounds.until_ts
    since = bounds.since_ts if bounds.since_ts is not None else until - SESSION_LOOKBACK_DAYS * 86400.0
    sid = bounds.session_id
    if sid is None:
        return osh.action_units(since, until, proxy_rows=proxy_rows)["actions"]
    mine = [r for r in proxy_rows if r.get("session_id") == sid]
    out = osh.action_units(since, until, proxy_rows=mine,
                           kind_of=lambda s, _stamp: "organic" if s == sid else None)["actions"]
    return [a for a in out if a.get("session_id") == sid]


def targets_field(actions: list[dict] | None, window: str) -> dict:
    """Actions per serving target; ``offload_numerator`` = local + cheap, spread never in it (pure)."""
    from llm_router import offload_share as osh

    if actions is None:
        return {"local": None, "cheap": None, "frontier": None, "spread": None,
                "offload_numerator": None, "n": None, "window": window}
    c = Counter(a["served_class"] for a in actions)
    local, cheap = c[osh.SC_LOCAL], c[osh.SC_HAIKU] + c[osh.SC_CHEAP]
    return {"local": local, "cheap": cheap, "frontier": c[osh.SC_PAID], "spread": c[osh.SC_SPREAD],
            "offload_numerator": local + cheap, "n": len(actions), "window": window}


def offload_field(actions: list[dict] | None, window: str) -> dict:
    """S1 per action: k in the numerator of n actions; no quality-sampled lane = unknown (pure)."""
    if actions is None:
        return {"value": None, "k": None, "n": None, "window": window, "na_reason": "no action ledger"}
    k = sum(1 for a in actions if a["numerator"])
    sampled = any(a["sampled"] for a in actions)
    why = None if actions and sampled else ("no action" if not actions else "no quality-sampled lane")
    return {"value": k / len(actions) if why is None else None, "k": k, "n": len(actions),
            "window": window, "na_reason": why}


def escalations_field(proxy_rows: list[dict] | None, bounds: lv.Bounds) -> dict:
    """Escalations from the proxy rows' ``tier_reason``; ``of`` = the scope's proxy rows (side calls out)."""
    from llm_router import offload_share as osh

    if proxy_rows is None:
        return {"n": None, "of": None, "window": bounds.name, "source": "proxy_calls.jsonl tier_reason",
                "na_reason": "no proxy ledger"}
    rows = [r for r in proxy_rows if _in_scope(r, bounds) and r.get("tier_reason") != "side_call"]
    return {"n": sum(1 for r in rows if r.get("tier_reason") in osh.ESCALATION_REASONS), "of": len(rows),
            "window": bounds.name, "source": "proxy_calls.jsonl tier_reason", "na_reason": None}


def cache_field(bounds: lv.Bounds) -> dict:
    from llm_router.semantic_cache import MIN_INFORMATIVE_LOOKUPS

    s = lv.cache_hit_stats(bounds)
    if s is None:
        return {"value": None, "hits": None, "n": None, "window": bounds.name, "min_n": MIN_INFORMATIVE_LOOKUPS,
                "na_reason": "no lookup recorded"}
    low = s.n < MIN_INFORMATIVE_LOOKUPS
    return {"value": None if low else s.hits / s.n, "hits": s.hits, "n": s.n, "window": bounds.name,
            "min_n": MIN_INFORMATIVE_LOOKUPS,
            "na_reason": f"n={s.n} < {MIN_INFORMATIVE_LOOKUPS} lookups" if low else None}


def turns_off_claude(actions: list[dict] | None) -> int | None:
    """Human TURNS served off Claude (the unit the burn rate is measured in): a turn's first call (or a
    zero-Claude hook turn) by the proxy, sub-agent or hook door, served by a local, very-cheap or
    other-Frontier model, not redone. MCP assists and technical ops ran inside a Claude turn (its quota
    was spent anyway); Haiku is a Claude call. Same rule as the statusline's ``saved`` field (P35-2)."""
    from llm_router import offload_share as osh

    if actions is None:
        return None
    off = {osh.SC_LOCAL, osh.SC_CHEAP, osh.SC_SPREAD}
    doors = {osh.DOOR_PROXY, osh.DOOR_SUBAGENT, osh.DOOR_HOOK}
    return sum(1 for a in actions if a["first"] and a["door"] in doors and a["served_class"] in off
               and not a["redone"])


def quota_saved_field(actions: list[dict] | None, window: str, now: float) -> dict:
    """``~% of window`` and ``~work-hours`` saved; both estimates with their n's. n=0 turns is n/a."""
    from llm_router import quota_rate, quota_samples

    turns = turns_off_claude(actions)
    burn = quota_samples.quota_burn(now - BURN_DAYS * 86400.0, now)["measured"]
    per_turn = burn.get("weekly_per_turn")
    pp = None if per_turn is None or not turns else turns * per_turn
    why = ("no action ledger" if turns is None else "n=0 turns off Claude" if turns == 0
           else f"no measured burn per turn ({BURN_DAYS}d)" if per_turn is None else None)
    as_of = lv._iso(now)
    src = f"turns off Claude x quota_burn weekly pp/turn ({BURN_DAYS}d)"
    pct = lv.Figure(pp, turns, window, as_of, src, "estimated", why, burn.get("n_turns") if pp else None)
    rate = quota_rate.median_pp_per_active_hour(now=now)
    wh = quota_rate.work_hours_figure(pp, turns if pp is not None else None, rate, window=window,
                                      as_of=as_of, source=src + " / quota_rate median")
    if pp is None:
        wh = lv.Figure(None, turns, window, as_of, wh.source, "estimated", why, rate.n)
    return {"pct": pct.to_dict(), "work_hours": wh.to_dict(), "turns": turns,
            "burn": {"weekly_per_turn": per_turn, "n_turns": burn.get("n_turns"),
                     "n_sessions": burn.get("n_sessions"), "window": f"{BURN_DAYS}d"}}


def quality_field(now: float, days: int = QUALITY_DAYS) -> dict:
    """Quality vs Frontier: ``shadow_frontier.verdict_summary`` (the statusline's source, P35-2): verdicts
    in ``days`` where the served answer was ``acceptable`` (k) of those that decided (n), and the Frontier
    answer's own acceptable rate over the rows that judged it."""
    from llm_router import shadow_frontier

    s = shadow_frontier.verdict_summary(now=now, days=days)
    fk, fn = s["frontier_k"], s["frontier_n"]
    return {"value": s["rate"], "k": s["k"], "n": s["n"], "wilson": s["wilson"],
            "frontier_value": fk / fn if fn else None, "frontier_k": fk, "frontier_n": fn,
            "undecided": s["undecided"], "window": s["window"]}


def judge_field(now: float) -> dict:
    """The judge line: the child's cache (``judge_summary.json``), else computed over the same window."""
    from llm_router import judge_view

    d = judge_view.read_cache()
    if d:
        return {"mean": d.get("mean"), "n": d.get("n"), "window": (d.get("window") or {}).get("label")}
    days = judge_view.DEFAULT_WINDOW_DAYS
    s = judge_view.judge_summary(now - days * 86400.0, now, label=judge_view.window_label(days), now=now)
    return {"mean": s.mean, "n": s.n, "window": s.window}


# ── recommendation ─────────────────────────────────────────────────────────────

_GROUP_NAMES = {"qa": "Q&A", "code": "code", "tech_op": "technical-op"}


def recommend(actions: list[dict] | None, quality: dict | None) -> str:
    """One recommendation computed from the fields, or ``no recommendation (n/a: <input>)``.

    Inputs: the session's dominant task group (its share of the actions: the census) and the
    eligibility grade (:func:`eligibility.grade`, the one rule) of the cheap answer against the Frontier
    answer in the verdicts. ``eligible`` -> offload more of that work; ``failed`` -> keep it on Frontier;
    ``unknown`` / ``probe`` (fewer than the rule's n) -> no recommendation naming the missing input."""
    from llm_router import eligibility
    from llm_router.lane_quality import PAIRS_MIN_N

    if not actions:
        return "no recommendation (n/a: no action in the session)"
    if not quality or not quality.get("n") or not quality.get("frontier_n"):
        n = (quality or {}).get("n") or 0
        return f"no recommendation (n/a: quality vs Frontier, n={n}, {(quality or {}).get('window', '7d')})"
    groups = Counter(a["task_group"] for a in actions)
    group, g_n = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    ev = eligibility.evidence_from_counts(quality["k"], quality["n"], quality["frontier_k"], quality["frontier_n"])
    grade = eligibility.grade(ev)
    qn, qw = quality["n"], quality["window"]
    if grade in ("unknown", "probe"):
        return f"no recommendation (n/a: quality vs Frontier n={qn} < {PAIRS_MIN_N}, {qw})"
    name = _GROUP_NAMES.get(group, group)
    share = f"{g_n / len(actions):.0%} of this session's actions, n={len(actions)}"
    if grade == "eligible":
        return (f"~offload more {name} work ({share}): the cheap answer is within {ev.gap_pts:.1f} pts "
                f"of Frontier (n={qn}, {qw})")
    return (f"~keep {name} work on Frontier ({share}): the cheap answer trails Frontier by "
            f"{ev.gap_pts:.1f} pts (n={qn}, {qw})")


# ── build / render ─────────────────────────────────────────────────────────────

_SID_BAD = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_session_id(sid: str | None) -> str | None:
    """F116: a session id is printed back (``no rows for session X``, the label), so it must be
    ``[A-Za-z0-9._-]+``. ``None`` means "no id given" (the caller reads the current session).
    Anything else -- the empty string, a space, an ESC, a newline -- raises ``ValueError``:
    it is refused, never rewritten (F114 mapped bad characters to ``_``, so ``abc def`` and
    ``abc_def`` named the same session), and an empty ``--session ""`` never falls back to the
    current session."""
    if sid is None:
        return None
    if not sid or _SID_BAD.search(sid):
        raise ValueError(f"invalid session id {sid!r}: use only letters, digits, '.', '_' and '-'")
    return sid


def _has_rows(bounds: lv.Bounds, actions: list[dict] | None, proxy_rows: list[dict] | None) -> bool:
    """True unless the session id is known to have NO row anywhere (proxy ledger, usage table).
    Unknown (no usage database to ask) is not 'no rows'."""
    sid = bounds.session_id
    if sid is None or actions:
        return True
    if any(r.get("session_id") == sid for r in proxy_rows or []):
        return True
    usage = _part("CHZ-FO-SESSION-REPORT-USAGE", lambda: lv.session_usage_rows(bounds))
    return usage is None or bool(usage)


def _bounds(session_id: str | None, scope: str, now: float) -> lv.Bounds:
    if scope == "session":
        sid = sanitize_session_id(session_id if session_id is not None else lv.read_session_id())
        return lv.session_scope(session_id=sid, session_start=lv.read_session_start(), now=now)
    return lv.resolve(scope, now=now)


def build(session_id: str | None = None, *, scope: str = "session", now: float | None = None) -> dict:
    """The session summary. ``session_id`` None reads ``session_id.txt``; with no id the session is the
    rows since ``session_start.txt`` (labelled so). ``scope`` another ledger window (``today``, ``7d``...)
    summarises that window instead (organic sessions only)."""
    now = time.time() if now is None else float(now)
    bounds = _bounds(session_id, scope, now)
    w = bounds.name
    from llm_router.proxy import ledger as pl

    path = pl.ledger_path()
    proxy_rows = _part("CHZ-FO-SESSION-REPORT-PROXY", lambda: pl.read_rows(path) if path.exists() else None)
    actions = _part("CHZ-FO-SESSION-REPORT-ACTIONS", lambda: _actions(bounds, proxy_rows or []))
    quality = _part("CHZ-FO-SESSION-REPORT-QUALITY", lambda: quality_field(now))
    return {
        "v": SCHEMA, "as_of": lv._iso(now), "window": w, "label": bounds.label,
        "session_id": bounds.session_id,
        "no_rows": not _has_rows(bounds, actions, proxy_rows),
        "targets": targets_field(actions, w),
        "offload": offload_field(actions, w),
        "escalations": escalations_field(proxy_rows, bounds),
        "cache_hit_rate": _part("CHZ-FO-SESSION-REPORT-CACHE", lambda: cache_field(bounds))
        or {"value": None, "hits": None, "n": None, "window": w, "na_reason": "cache read failed"},
        "quota_saved": _part("CHZ-FO-SESSION-REPORT-QUOTA", lambda: quota_saved_field(actions, w, now)),
        "quality": quality,
        "judge": _part("CHZ-FO-SESSION-REPORT-JUDGE", lambda: judge_field(now)),
        "recommendation": recommend(actions, quality),
    }


def _n(v) -> str:
    return "n/a" if v is None else f"{v:,}"


def render(r: dict) -> str:
    """The block (pure): one figure per line, each with its n and window; unknown prints n/a."""
    w = r["window"]
    t = r["targets"]
    if r.get("no_rows"):
        return f"no rows for session {r['session_id']}\n"
    lines = [f"{MARKER} {r['label']} · as of {r['as_of']}"]
    if t["n"] is None:
        lines.append(f"  targets n/a (no action ledger, {w})")
    elif t["n"] == 0:
        lines.append(f"  targets n/a (n=0 actions, {w})")
    else:
        lines.append(f"  targets local {t['local']} · cheap {t['cheap']} · frontier {t['frontier']} · "
                     f"spread {t['spread']} (n={t['n']} actions, {w}; offload numerator local+cheap "
                     f"{t['offload_numerator']}, spread never in it)")
    o = r["offload"]
    if o["value"] is not None:
        lines.append(f"  offload {100 * o['value']:.0f}% (k={o['k']}, n={o['n']}, {w})")
    else:
        lines.append(f"  offload n/a (n={_n(o['n'])}, {w}, {o['na_reason']})")
    e = r["escalations"]
    if e["n"] is None:
        lines.append(f"  escalations n/a (n=n/a, {w}, {e['na_reason']})")
    else:
        lines.append(f"  escalations {e['n']} (n={e['of']} proxy calls, {w}, tier_reason)")
    c = r["cache_hit_rate"]
    if c["value"] is not None:
        lines.append(f"  cache hit rate {100 * c['value']:.0f}% (hits={c['hits']}, n={c['n']} lookups, {w})")
    else:
        lines.append(f"  cache hit rate n/a (n={_n(c['n'])} lookups, {w}, {c['na_reason']})")
    q = r.get("quota_saved")
    if q is None:
        lines.append(f"  quota saved n/a (n=n/a, {w}, quota read failed)")
    else:
        pct, wh = lv.Figure.from_dict(q["pct"]), lv.Figure.from_dict(q["work_hours"])
        p_txt = lv.show(pct, unit="subscription", quantity="pct", with_n=True)
        if p_txt is None:
            why = "" if q["turns"] == 0 else f", {pct.na_reason}"
            lines.append(f"  quota saved n/a (n={_n(q['turns'])} turns off Claude, {w}{why})")
        else:
            b = q["burn"]
            wh_txt = lv.show(wh, unit="subscription", quantity="work_hours", with_n=True) \
                or f"~work-hours n/a ({wh.na_reason})"
            lines.append(f"  quota saved {p_txt} · {wh_txt} (turns off Claude x measured burn "
                         f"{b['weekly_per_turn']:.3f} pp/turn over {b['window']})")
    ql = r.get("quality")
    if not ql or ql["value"] is None:
        lines.append(f"  quality vs Frontier n/a (n={_n((ql or {}).get('n'))}, {(ql or {}).get('window', '7d')})")
    else:
        fr = "n/a" if ql["frontier_value"] is None else f"{100 * ql['frontier_value']:.0f}%"
        lines.append(f"  quality vs Frontier {100 * ql['value']:.0f}% acceptable vs Frontier {fr} "
                     f"(n={ql['n']}, {ql['window']}; {ql['undecided']} undecided)")
    j = r.get("judge")
    if j and j.get("mean") is not None and j.get("n"):
        lines.append(f"  judge {j['mean']:.2f} (n={j['n']}, {j['window']})")
    else:
        lines.append(f"  judge n/a (n={_n((j or {}).get('n'))}, {(j or {}).get('window') or 'judge read failed'})")
    lines.append(f"  recommendation: {r['recommendation']}")
    return "\n".join(lines) + "\n"
