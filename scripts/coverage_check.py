#!/usr/bin/env python3
"""Decision-point coverage of host model calls (PLAN-v16 P2.2-a).

``coverage_check.py --since 2026-10-20T00:00:00Z --until 2026-11-03T00:00:00Z``

Unit: one host model call = one distinct assistant message id in a Claude Code
transcript (main thread and sub-agent sidechains). A call PASSED when a proxy ledger
row with that ``msg_id`` has ``tier_reason`` or ``reason`` set, or ``decision ==
"served"`` (D-125). "Decision-point coverage" is not "routing coverage": a forward
with a recorded reason passes. The bar (point estimate, interval always printed):

* A  coverage >= 0.99 on the per-session-capped pool, and Wilson 95% lower bound >= 0.95;
* B  every call that did not pass is explained (a skips row, or a fail-open
     ``proxy_down`` row): no ``unexplained`` call, no unexplained ``no_decision`` call;
* C  every skips row in the window has a reason from REASONS and a door from DOORS.

Verdicts: PASS (exit 0); FAIL and NOT INFORMATIVE (exit 1); a refused run (exit 2).
NOT INFORMATIVE: n < --min-n, fewer than 5 sessions, one session > 60% of the calls,
no transcripts, an unreadable or rotated ledger, too many missing timestamps or unknown
entrypoints. An empty input never passes.

Reads only message ids, timestamps, ``entrypoint``, ``isSidechain`` and the model field
from transcripts: no prompt or answer text is read, stored or printed. Session ids are
printed as 8-hex sha256 prefixes. The only write is ``--write`` / ``--json`` (0600).
Stdlib only, so the file can be pinned by its sha256 (``--expect-script-sha``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "coverage_check/1"
MIN_N = 200
CAP = 40
MIN_SESSIONS = 5
LARGEST_SESSION_MAX = 0.60
MISSING_TS_MAX = 0.05
UNKNOWN_ENTRYPOINT_MAX = 0.20
WILSON_FLOOR = 0.95
BAR = 0.99
MATCH_WINDOW_S = 120.0
CAP_SEED = "p22a-v1:"
Z95 = 1.959963984540054
# Kept equal to llm_router.skips.REASONS / DOORS (tests/test_coverage_check.py pins it).
REASONS = frozenset({"headless", "base_url_override", "tiers_off", "routing_off"})
DOORS = frozenset({"agent-route", "session-start", "proxy"})
# Only a skip at one of these (door, reason) pairs explains a call that has no ledger row: it
# explains every later call of its session. Any other skips row (the closed set still lists the
# "proxy" door, for structure) explains nothing: a per-call proxy row would let any call within
# +-MATCH_WINDOW_S of it be called "explained" and hide a proxy BYPASS, the failure this measure
# exists to catch (D-150). A fail_open ``proxy_down`` row explains calls within MATCH_WINDOW_S.
SESSION_LEVEL = frozenset({("session-start", "base_url_override"), ("agent-route", "headless")})
PROXY_DOWN = "proxy_down"
# Prereg items this script does not build; listed in every result so nothing passes silently.
NOT_EVALUATED = ("D", "E", "F", "prereg_sha_check", "allowed_roots_refusal", "supplement_sessions")
_PRINTABLE_REASON = re.compile(r"^[a-z0-9_:.-]{1,40}$")
_ALLOWED_KINDS = {"organic", "headless"}


class Refused(Exception):
    """The run is not allowed (exit 2); nothing is computed."""


# ── small helpers ────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    override = os.environ.get("LLM_ROUTER_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".llm-router"


def _projects_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECTS_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".claude" / "projects"


def parse_utc(value) -> float | None:
    """Epoch seconds for an ISO-8601 string WITH a UTC designator, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def _ts_any(value) -> float | None:
    """A row timestamp: epoch number or ISO string (naive ISO is read as UTC)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def wilson(k: int, n: int) -> list[float] | None:
    if n <= 0:
        return None
    p = k / n
    z2 = Z95 * Z95
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def sid8(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _jsonl(path: Path, *, generations: bool) -> tuple[list[dict], bool]:
    """(rows, readable). With ``generations`` the rotated ``<name>.1`` is read first.
    A malformed line is skipped. ``readable`` is False when no generation could be read."""
    candidates = [path.with_name(path.name + ".1"), path] if generations else [path]
    rows: list[dict] = []
    readable = False
    for candidate in candidates:
        try:
            fh = candidate.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        readable = True
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    return rows, readable


# ── transcripts ──────────────────────────────────────────────────────────────

def scan_transcripts(root: Path) -> tuple[dict, dict]:
    """``({(session, msg_id): call}, {session: first_ts})`` over every transcript under ``root``.

    ``call`` = {"ts", "entrypoint", "sidechain", "synthetic", "session", "msg_id"}. One message is
    written as several entries: the earliest timestamp counts, a sidechain flag on any entry marks the
    call a sidechain call. ``ts`` is None when no entry carried one. Layout as in
    ``llm_router.o3_transcripts``: ``<project>/<session>.jsonl`` and
    ``<project>/<session>/subagents/**/*.jsonl`` (all sidechain)."""
    calls: dict[tuple[str, str], dict] = {}
    first_ts: dict[str, float] = {}
    files: list[tuple[Path, str, bool]] = []
    try:
        for main in root.glob("*/*.jsonl"):
            files.append((main, main.stem, False))
        for sub in root.glob("*/*/subagents/**/*.jsonl"):
            files.append((sub, sub.relative_to(root).parts[1], True))
    except OSError:
        return calls, first_ts
    for path, session, sub_file in sorted(files):
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                msg = d.get("message")
                mid = msg.get("id") if isinstance(msg, dict) else None
                if not (isinstance(mid, str) and mid):
                    continue
                ts = _ts_any(d.get("timestamp"))
                if ts is not None and (session not in first_ts or ts < first_ts[session]):
                    first_ts[session] = ts
                model = msg.get("model") if isinstance(msg, dict) else None
                entrypoint = d.get("entrypoint")
                call = calls.get((session, mid))
                if call is None:
                    calls[(session, mid)] = {
                        "session": session, "msg_id": mid, "ts": ts,
                        "entrypoint": entrypoint if isinstance(entrypoint, str) and entrypoint else None,
                        "sidechain": bool(sub_file or d.get("isSidechain") is True),
                        "synthetic": model == "<synthetic>",
                    }
                    continue
                if ts is not None and (call["ts"] is None or ts < call["ts"]):
                    call["ts"] = ts
                if call["entrypoint"] is None and isinstance(entrypoint, str) and entrypoint:
                    call["entrypoint"] = entrypoint
                if sub_file or d.get("isSidechain") is True:
                    call["sidechain"] = True
                if model != "<synthetic>":
                    call["synthetic"] = False
    return calls, first_ts


def entrypoint_class(entrypoint: str | None) -> str:
    if not entrypoint:
        return "unknown"
    return "headless" if entrypoint.strip().lower().startswith("sdk") else "interactive"


# ── ledger, skips, fail-open ─────────────────────────────────────────────────

def _set(value) -> bool:
    return value is not None and value != ""


def row_passes(row: dict) -> bool:
    return _set(row.get("tier_reason")) or _set(row.get("reason")) or row.get("decision") == "served"


def _load_exclusions(path: Path | None) -> set[str]:
    if path is None:
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused(f"--exclude-sessions file cannot be read: {type(exc).__name__}") from exc
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


class _Skips:
    """skips.jsonl rows of the window, indexed per session."""

    def __init__(self, rows: list[dict], since: float, until: float):
        self.rows_in_window = 0
        self.invalid = 0
        self.reasonless = 0
        self.reasons: dict[str, int] = {}
        self.session_level: dict[str, float] = {}
        for r in rows:
            ts = _ts_any(r.get("ts"))
            if ts is None or not (since <= ts < until):
                continue
            self.rows_in_window += 1
            reason, door = r.get("reason"), r.get("door")
            if not (isinstance(reason, str) and reason.strip()):
                self.reasonless += 1
                self.invalid += 1
                continue
            if reason not in REASONS or door not in DOORS:
                self.invalid += 1
            key = reason if _PRINTABLE_REASON.match(reason) else "other"
            self.reasons[key] = self.reasons.get(key, 0) + 1
            sid = r.get("session_id")
            if not (isinstance(sid, str) and sid):
                continue
            if (door, reason) in SESSION_LEVEL:
                self.session_level[sid] = min(ts, self.session_level.get(sid, ts))

    def explains(self, session: str, ts: float | None) -> bool:
        if ts is None:
            return False
        start = self.session_level.get(session)
        return start is not None and ts >= start


NOT_ROUTED_REASONS = frozenset({"routing_off", "tiers_off"})
OFF_SHARE_FLAG = 0.90


def route_kind(row: dict) -> str:
    """How a PASSING ledger row passed: ``routed`` (served locally, or the tier policy decided in
    route mode), ``advise`` (the proxy only proposed), ``off`` (routing_off / tiers_off with no
    tier decision) or ``other_reason`` (any other recorded reason, e.g. not_eligible)."""
    if row.get("decision") == "served":
        return "routed"
    if row.get("tier_reason") == "advise" or row.get("authority") == "advise":
        return "advise"
    if _set(row.get("tier_reason")):
        return "routed"
    if row.get("reason") in NOT_ROUTED_REASONS:
        return "off"
    return "other_reason"


def routed_share(calls: list[dict]) -> dict:
    """Beside coverage (prereg C2): of the PASSED calls, how many a decision actually routed.
    Coverage counts a recorded reason as a decision point; a proxy that never routes but always
    logs ``routing_off`` would score 100%, so a window of (nearly) all ``off`` / ``advise`` rows is
    flagged as no evidence for R-RTE-2 (the router executes by default)."""
    kinds = {"routed": 0, "advise": 0, "off": 0, "other_reason": 0}
    for c in calls:
        if c["class"] == "passed":
            kinds[c["route_kind"]] += 1
    n = sum(kinds.values())
    share = lambda k: round(k / n, 4) if n else None  # noqa: E731
    off_share, advise_share = share(kinds["off"]), share(kinds["advise"])
    reasons = []
    if off_share is not None and off_share >= OFF_SHARE_FLAG:
        reasons.append(f"routing_off/tiers_off is {_pct(off_share)} of passed calls")
    if advise_share is not None and advise_share >= OFF_SHARE_FLAG:
        reasons.append(f"advise is {_pct(advise_share)} of passed calls")
    return {"passed": n, **kinds, "routed_share": share(kinds["routed"]), "off_share": off_share,
            "advise_share": advise_share, "not_evidence_for_RRTE2": bool(reasons),
            "not_evidence_reason": "; ".join(reasons) or None}


def _failopen_times(rows: list[dict]) -> list[float]:
    out = []
    for r in rows:
        if r.get("c") == PROXY_DOWN:
            ts = _ts_any(r.get("ts"))
            if ts is not None:
                out.append(ts)
    return sorted(out)


def _near(times: list[float], ts: float | None) -> bool:
    return ts is not None and any(abs(ts - t) <= MATCH_WINDOW_S for t in times)


# ── the measurement ──────────────────────────────────────────────────────────

def _stratum(calls: list[dict]) -> dict:
    n = len(calls)
    k = sum(1 for c in calls if c["class"] == "passed")
    return {"n": n, "passed": k, "coverage": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def _shares(calls: list[dict]) -> tuple[int, float | None, list[float]]:
    per: dict[str, int] = {}
    for c in calls:
        per[c["session"]] = per.get(c["session"], 0) + 1
    n = len(calls)
    if not n:
        return 0, None, []
    shares = sorted((v / n for v in per.values()), reverse=True)
    return len(per), round(shares[0], 4), [round(s, 4) for s in shares[:3]]


def cap_calls(calls: list[dict], cap: int) -> list[dict]:
    """Per session, the ``cap`` calls with the lowest sha256(seed + msg_id): outcome-blind, and not
    "the first K", which would hide a late-session bypass. ``cap`` 0 keeps everything."""
    if cap <= 0:
        return list(calls)
    per: dict[str, list[dict]] = {}
    for c in calls:
        per.setdefault(c["session"], []).append(c)
    kept: list[dict] = []
    for items in per.values():
        items.sort(key=lambda c: hashlib.sha256((CAP_SEED + c["msg_id"]).encode("utf-8")).hexdigest())
        kept.extend(items[:cap])
    return kept


def measure(*, since: float, until: float, projects_dir: Path, ledger: Path, skips: Path, failopen: Path,
            exclude: set[str], all_kinds: bool, min_n: int, cap: int, switch: float | None = None) -> dict:
    transcript_calls, first_ts = scan_transcripts(projects_dir)
    ledger_rows, ledger_readable = _jsonl(ledger, generations=True)
    skip_rows, skips_readable = _jsonl(skips, generations=True)
    failopen_rows, _ = _jsonl(failopen, generations=False)

    by_msg: dict[str, list[dict]] = {}
    session_kinds: dict[str, set[str]] = {}
    ledger_ts: list[float] = []
    no_msg_id = 0
    for r in ledger_rows:
        ts = _ts_any(r.get("ts"))
        if ts is not None:
            ledger_ts.append(ts)
        sid, mid = r.get("session_id"), r.get("msg_id")
        if isinstance(sid, str) and sid and isinstance(r.get("session_kind"), str):
            session_kinds.setdefault(sid, set()).add(r["session_kind"])
        if isinstance(mid, str) and mid:
            by_msg.setdefault(mid, []).append(r)
        elif ts is not None and since <= ts < until:
            no_msg_id += 1

    # ---- which calls are in the population
    in_window = missing_ts = synthetic = 0
    excluded_calls = 0
    candidates: list[dict] = []
    started_before: set[str] = set()
    floor = switch if switch is not None else since  # a session counts only if it began at/after T_switch
    excluded_listed: set[str] = set()
    excluded_kind: set[str] = set()
    for c in transcript_calls.values():
        if c["ts"] is None:
            missing_ts += 1
            continue
        if not (since <= c["ts"] < until):
            continue
        if c["synthetic"]:
            synthetic += 1
            continue
        in_window += 1
        sid = c["session"]
        if sid in exclude:
            excluded_listed.add(sid)
            excluded_calls += 1
            continue
        if not all_kinds and (session_kinds.get(sid, set()) - _ALLOWED_KINDS):
            excluded_kind.add(sid)
            excluded_calls += 1
            continue
        if first_ts.get(sid, floor) < floor:
            started_before.add(sid)  # its environment was read before the authority switch
            excluded_calls += 1
            continue
        candidates.append(c)

    failopen_times = _failopen_times(failopen_rows)
    skip_index = _Skips(skip_rows, since, until)
    multi_row = 0
    for c in candidates:
        rows = by_msg.get(c["msg_id"], [])
        if len(rows) > 1:
            multi_row += 1
        passing = [r for r in rows if row_passes(r)]
        if passing:
            cls = "passed"
            c["route_kind"] = route_kind(passing[0])
        elif rows:
            cls = "no_decision"
        elif skip_index.explains(c["session"], c["ts"]):
            cls = "skip_explained"
        elif _near(failopen_times, c["ts"]):
            cls = "failopen_explained"
        else:
            cls = "unexplained"
        c["class"] = cls
        c["explained"] = cls in ("passed", "skip_explained", "failopen_explained") or (
            cls == "no_decision" and (skip_index.explains(c["session"], c["ts"]) or _near(failopen_times, c["ts"])))
        c["ep"] = entrypoint_class(c["entrypoint"])

    kept = cap_calls(candidates, cap)
    n = len(kept)
    k = sum(1 for c in kept if c["class"] == "passed")
    classes = {name: sum(1 for c in kept if c["class"] == name)
               for name in ("passed", "no_decision", "skip_explained", "failopen_explained", "unexplained")}
    classes["no_decision_explained"] = sum(1 for c in kept if c["class"] == "no_decision" and c["explained"])
    n_sessions, largest, top3 = _shares(kept)
    u_n = len(candidates)
    u_k = sum(1 for c in candidates if c["class"] == "passed")
    u_sessions, u_largest, _ = _shares(candidates)

    window_msgs = {c["msg_id"] for c in transcript_calls.values()}
    orphan_rows = 0
    for mid, rows in by_msg.items():
        if mid in window_msgs:
            continue
        orphan_rows += sum(1 for r in rows if (t := _ts_any(r.get("ts"))) is not None and since <= t < until)

    strata = {
        "headless": _stratum([c for c in kept if c["ep"] == "headless"]),
        "interactive": _stratum([c for c in kept if c["ep"] == "interactive"]),
        "unknown": _stratum([c for c in kept if c["ep"] == "unknown"]),
        "sidechain": _stratum([c for c in kept if c["sidechain"]]),
        "main": _stratum([c for c in kept if not c["sidechain"]]),
    }
    breakdown: dict[str, int] = {}
    for c in kept:
        if not c["explained"]:
            key = f"{c['ep']}/{'sidechain' if c['sidechain'] else 'main'}"
            breakdown[key] = breakdown.get(key, 0) + 1

    # ---- NOT INFORMATIVE
    ni: list[str] = []
    if not transcript_calls:
        ni.append("no_transcripts")
    if n < min_n:
        ni.append(f"n_below_min_n:{n}<{min_n}")
    if n_sessions < MIN_SESSIONS:
        ni.append(f"sessions_below_{MIN_SESSIONS}:{n_sessions}")
    if (largest or 0) > LARGEST_SESSION_MAX or (u_largest or 0) > LARGEST_SESSION_MAX:
        ni.append("largest_session_share_above_0.60")
    if not ledger_readable:
        ni.append("ledger_unreadable")
    elif ledger_ts and min(ledger_ts) > since:
        ni.append("ledger_rotated_after_window_start")
    if skips_readable and skips.with_name(skips.name + ".1").exists():
        # A rotated skips log has dropped its oldest rows; one that never rotated is complete.
        skip_ts = [t for r in skip_rows if (t := _ts_any(r.get("ts"))) is not None]
        if skip_ts and min(skip_ts) > since:
            ni.append("skips_rotated_after_window_start")
    seen = in_window + missing_ts
    if seen and missing_ts / seen > MISSING_TS_MAX:
        ni.append("missing_timestamps_above_5pct")
    if n and strata["unknown"]["n"] / n > UNKNOWN_ENTRYPOINT_MAX:
        ni.append("unknown_entrypoint_above_20pct")

    coverage = round(k / n, 4) if n else None
    ci = wilson(k, n)
    clause_a = bool(n) and (k / n) >= BAR and ci is not None and ci[0] >= WILSON_FLOOR
    clause_b = classes["unexplained"] == 0 and classes["no_decision"] == classes["no_decision_explained"]
    clause_c = skip_index.invalid == 0
    if switch is None:
        ni.insert(0, "switch_unknown")  # without T_switch "sessions that began after the switch" is a guess
    if ni:
        verdict = "NOT INFORMATIVE"
    elif clause_a and clause_b and clause_c:
        verdict = "PASS"
    else:
        verdict = "FAIL"

    return {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window": {"since": _iso(since), "until": _iso(until)},
        "switch": {"known": switch is not None, "ts": _iso(switch) if switch is not None else None},
        "min_n": min_n, "cap": cap, "preregistered_params": (min_n == MIN_N and cap == CAP),
        "verdict": verdict, "informative": not ni, "not_informative_reasons": ni,
        "n": n, "passed": k, "coverage": coverage, "wilson95": ci,
        "classes": classes,
        "n_sessions": n_sessions, "largest_session_share": largest, "top3_session_shares": top3,
        "uncapped": {"n": u_n, "passed": u_k, "coverage": round(u_k / u_n, 4) if u_n else None,
                     "wilson95": wilson(u_k, u_n), "n_sessions": u_sessions, "largest_session_share": u_largest},
        "routed_share": routed_share(kept),
        "strata": strata,
        "unexplained_breakdown": breakdown,
        "skips": {"rows_in_window": skip_index.rows_in_window, "invalid": skip_index.invalid,
                  "reasonless": skip_index.reasonless, "reasons": dict(sorted(skip_index.reasons.items()))},
        "excluded": {"sessions_listed": len(excluded_listed), "sessions_by_kind": len(excluded_kind),
                     "sessions_started_before_switch": len(started_before), "calls": excluded_calls,
                     "synthetic_entries": synthetic, "missing_timestamp": missing_ts},
        "orphan_ledger_rows": orphan_rows, "multi_row_msg_ids": multi_row,
        "ledger_rows_without_msg_id": no_msg_id,
        "clauses": {"A": clause_a, "B": clause_b, "C": clause_c, "not_evaluated": list(NOT_EVALUATED)},
        "inputs": {"ledger_sha256": _sha256_file(ledger), "skips_sha256": _sha256_file(skips),
                   "failopen_sha256": _sha256_file(failopen)},
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── command line ─────────────────────────────────────────────────────────────

def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render(res: dict) -> str:
    ci = res["wilson95"]
    c = res["classes"]
    lines = [
        f"coverage_check {res['window']['since']} .. {res['window']['until']}  (decision-point coverage, not routing coverage)",
        f"verdict: {res['verdict']}",
        f"coverage: {_pct(res['coverage'])} ({res['passed']}/{res['n']}, Wilson95 "
        f"{_pct(ci[0]) if ci else 'n/a'}-{_pct(ci[1]) if ci else 'n/a'})  cap {res['cap']}/session, min n {res['min_n']}",
        f"routed share: {_pct(res['routed_share']['routed_share'])} of {res['routed_share']['passed']} passed "
        f"(routed {res['routed_share']['routed']}, advise {res['routed_share']['advise']}, "
        f"off {res['routed_share']['off']}, other reason {res['routed_share']['other_reason']})",
        f"uncapped: {_pct(res['uncapped']['coverage'])} ({res['uncapped']['passed']}/{res['uncapped']['n']})",
        f"sessions: {res['n_sessions']}  largest share {_pct(res['largest_session_share'])}  "
        f"top3 {', '.join(_pct(s) for s in res['top3_session_shares']) or 'n/a'}",
        "classes: " + ", ".join(f"{k}={v}" for k, v in c.items()),
        "strata: " + "; ".join(f"{k} {v['passed']}/{v['n']}" for k, v in res["strata"].items()),
        f"skips in window: {res['skips']['rows_in_window']}  invalid {res['skips']['invalid']}  "
        f"reasonless {res['skips']['reasonless']}",
        f"orphan ledger rows: {res['orphan_ledger_rows']}  multi-row msg ids: {res['multi_row_msg_ids']}",
        "clauses: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in res["clauses"].items()
                                if k != "not_evaluated") + "  (not evaluated here: " + ", ".join(res["clauses"]["not_evaluated"]) + ")",
    ]
    if res["routed_share"]["not_evidence_for_RRTE2"]:
        lines.append("NOT EVIDENCE FOR R-RTE-2: " + res["routed_share"]["not_evidence_reason"])
    if res["not_informative_reasons"]:
        lines.append("NOT INFORMATIVE: " + "; ".join(res["not_informative_reasons"]))
    if not res["preregistered_params"]:
        lines.append("NOT PREREGISTERED: --min-n / --cap differ from the preregistered 200 / 40")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Decision-point coverage of host model calls (P2.2-a).")
    ap.add_argument("--since", help="window start, ISO-8601 with a UTC designator (required)")
    ap.add_argument("--until", help="window end, same form (required)")
    ap.add_argument("--projects-dir", type=Path, default=None, help="default: ~/.claude/projects")
    ap.add_argument("--ledger", type=Path, default=None, help="default: <state>/proxy_calls.jsonl")
    ap.add_argument("--skips", type=Path, default=None, help="default: <state>/skips.jsonl")
    ap.add_argument("--failopen", type=Path, default=None, help="default: <state>/fail_open.jsonl")
    ap.add_argument("--exclude-sessions", type=Path, default=None, help="file: one session id per line")
    ap.add_argument("--all-kinds", action="store_true",
                    help="keep sessions whose ledger session_kind is research or harness")
    ap.add_argument("--switch-file", type=Path, default=None,
                    help='JSON {"ts": ...} written by the owner at the switch; refused when its ts is after '
                         "--since. Without it the run is NOT INFORMATIVE (switch_unknown)")
    ap.add_argument("--expect-script-sha", default=None, help="refuse unless this file has this sha256")
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--cap", type=int, default=CAP, help="calls kept per session (0 = no cap)")
    ap.add_argument("--json", dest="json_out", type=Path, default=None, help="also write the result here (0600)")
    ap.add_argument("--write", action="store_true", help="write <state>/coverage_check.json for `llm-router kpi`")
    return ap


def run(argv: list[str]) -> int:
    ap = build_parser()
    args, extra = ap.parse_known_args(argv)
    try:
        if any(a == "--days" or a.startswith("--days=") for a in extra):
            raise Refused("--days is refused: the window is absolute (--since / --until)")
        if extra:
            raise Refused(f"unknown argument {extra[0]!r}")
        since, until = parse_utc(args.since), parse_utc(args.until)
        if since is None or until is None:
            raise Refused("--since and --until are required, ISO-8601 with a UTC designator (e.g. 2026-10-20T00:00:00Z)")
        if until <= since:
            raise Refused("--until must be after --since")
        if args.expect_script_sha is not None and _sha256_file(Path(__file__).resolve()) != args.expect_script_sha:
            raise Refused("this script does not match --expect-script-sha")
        switch = None
        if args.switch_file is not None:
            try:
                switch = parse_utc(json.loads(args.switch_file.read_text(encoding="utf-8")).get("ts"))
            except (OSError, ValueError, AttributeError) as exc:
                raise Refused(f"--switch-file cannot be read: {type(exc).__name__}") from exc
            if switch is None:
                raise Refused("--switch-file has no usable ts")
            if switch > since:
                raise Refused("--since is earlier than the recorded switch time")
        exclude = _load_exclusions(args.exclude_sessions)
    except Refused as exc:
        print(f"coverage_check: refused: {exc}", file=sys.stderr)
        return 2
    state = _state_dir()
    res = measure(
        since=since, until=until,
        projects_dir=args.projects_dir or _projects_dir(),
        ledger=args.ledger or state / "proxy_calls.jsonl",
        skips=args.skips or state / "skips.jsonl",
        failopen=args.failopen or state / "fail_open.jsonl",
        exclude=exclude, all_kinds=args.all_kinds, min_n=args.min_n, cap=args.cap, switch=switch,
    )
    print(render(res))
    if args.json_out is not None:
        _write_private(args.json_out, res)
    if args.write:
        _write_private(state / "coverage_check.json", res)
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
