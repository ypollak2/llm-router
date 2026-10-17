"""Routing observability report — a deep-dive of what LLM Router actually routed.

Joins the runtime ledgers into one markdown report:
  * ``usage.db``               — per-model calls, tokens in/out, latency, saved $
  * ``auto-route-debug.log``   — routing-outcome matrix (DIRECT success/skip/failed)
  * ``enforcement.log``        — overrides (the model did the work itself)

Run:  python -m llm_router.routing_report   →  writes ~/.llm-router/routing_report.md
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from llm_router import paths, routing_log
from llm_router.provider_classes import not_error_row_sql, real_decision_sql


def executed_routes(
    window_hours: float = 24.0, db_path=None, timeout_s: float = 0.25,
) -> dict | None:
    """Count served runtime decisions in the rolling ``window_hours`` window without creating a DB.

    P3.4-A: delegates to ``ledger_view`` (runtime provenance, real decisions, plus proxy ledger
    rows once each; the window is the named one when ``window_hours`` is 6/24/168/336)."""
    from llm_router import ledger_view as lv

    try:
        bounds = lv.rolling(window_hours)
        by_provider = lv.executed_by_provider(bounds, db_path=db_path, timeout_s=timeout_s)
        by_cause = lv.local_by_cause(bounds, db_path=db_path, timeout_s=timeout_s)
    except (sqlite3.Error, OSError):
        return None
    if by_provider is None:
        return None
    if by_cause is None:
        from llm_router import skip_causes as sc
        by_cause = {c: 0 for c in sc.CAUSES}
    return {
        "window_hours": window_hours,
        "total": sum(by_provider.values()),
        "by_provider": by_provider,
        "local_by_cause": by_cause,
    }


def executed_provider_rows(result: dict) -> list[tuple[str, int]]:
    """Return provider labels and counts in execution-report order."""
    counts = dict(result["by_provider"])
    counts.setdefault("ollama", 0)
    return [
        ("local" if provider == "ollama" else provider, n)
        for provider, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def format_executed_routes(result: dict | None, window_hours: float = 24.0) -> str:
    """Label execution counts separately from classifier hints."""
    hours = result["window_hours"] if result is not None else window_hours
    prefix = f"routes {hours:g}h:"
    if result is None:
        return f"{prefix} no execution data (usage.db missing or unreadable)"
    parts = [f"{prefix} {result['total']} executed"]
    for label, n in executed_provider_rows(result):
        parts.append(f"{label} {n}")
    return " · ".join(parts)


def format_local_by_cause(result: dict | None) -> str:
    """``local by cause: policy 3 bench 2 ...`` (zero causes omitted), or ``""`` when no local
    route was served or there is no data. A separate line, so the executed header keeps its form."""
    by_cause = (result or {}).get("local_by_cause") or {}
    if not sum(by_cause.values()):
        return ""
    return "local by cause: " + " ".join(f"{c} {n}" for c, n in by_cause.items() if n)


def _home():
    return paths.llm_router_home()


def _pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


_START_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION START\] ID=([\d.]+)")
_LINE_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION ([\d.]+)\] (.*)$")
_SESSION_RE = re.compile(r"session_id=(\S*)")

# A session id the hook could not resolve. The hook ran, but this is not a user
# prompt — same reason an empty id is excluded, which is what the test suite writes.
_NON_SESSIONS = {"", "unknown", "none", "None"}


class Outcome(str, Enum):
    SUCCESS = "success"      # a local model answered
    FAILED = "failed"        # it was tried and could not
    SKIPPED = "skipped"      # the gate declined to try
    OTHER = "other"          # no terminal line: crashed, truncated, still running


# Terminal markers, most specific first. A rejected draft is NOT a success: S2-6
# discarded it and the turn fell through to Claude.
_TERMINAL = (
    ("DRAFT REJECTED", Outcome.FAILED),
    ("DIRECT SUCCESS", Outcome.SUCCESS),
    ("DIRECT FAILED", Outcome.FAILED),
    ("DIRECT SKIP", Outcome.SKIPPED),
)

# Annotations — they describe WHY an invocation went the way it did, and are
# reported alongside rather than as outcomes. Counting a rescue as an outcome would
# double-count the invocation it belongs to.
_ANNOTATIONS = (
    ("SESSION RESCUE", "session_rescue"),
    ("OKF RESCUE", "okf_rescue"),
    ("DRAFT REJECTED", "rejected"),
    # Whether the draft was RELAYED or discarded. These land on the invocation
    # AFTER the one that produced the draft — the verdict needs the reply to
    # exist before it can be read — so they are annotations, not outcomes, and
    # `used + unused` tracks `drafts` without being tied to it across a day
    # boundary or a session that ended before the next prompt.
    ("DRAFT USED", "used"),
    ("DRAFT UNUSED", "unused"),
    ("PERSISTED", "turns_persisted"),
)


def parse_log(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    """One record per invocation, keyed by id. Malformed lines are skipped."""
    records: dict[str, dict[str, Any]] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        m = _START_RE.match(line)
        if m:
            day, iid = m.group(1), m.group(2)
            records.setdefault(iid, {"day": day, "session": None,
                                     "outcome": None, "notes": set()})
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        day, iid, rest = m.group(1), m.group(2), m.group(3)
        rec = records.setdefault(iid, {"day": day, "session": None,
                                       "outcome": None, "notes": set()})
        s = _SESSION_RE.search(rest)
        if s:
            rec["session"] = s.group(1)
        for marker, note in _ANNOTATIONS:
            if marker in rest:
                rec["notes"].add(note)
        if rec["outcome"] is None:
            # First terminal line wins: a line can repeat on retry, the outcome
            # cannot.
            for marker, outcome in _TERMINAL:
                if rest.startswith(marker) or f"] {marker}" in rest or marker in rest.split(":")[0]:
                    rec["outcome"] = outcome
                    break
    return records


def summarise(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-day counts over real user prompts only."""
    days: dict[str, dict[str, Any]] = {}
    for rec in records.values():
        if rec.get("session") in _NON_SESSIONS or rec.get("session") is None:
            continue
        d = days.setdefault(rec["day"], {
            "prompts": 0, "success": 0, "failed": 0, "skipped": 0, "other": 0,
            "session_rescue": 0, "okf_rescue": 0, "rejected": 0,
            "turns_persisted": 0, "rate": 0.0,
            # Phase 3(a): a draft was PRODUCED and then either relayed or discarded.
            # The denominator for grounding is drafts, not prompts — a prompt the
            # gate never routed produced no draft, so it was never a chance for the
            # check to fire, and counting it would dilute the rate with cases
            # grounding had no part in.
            "drafts": 0, "grounding_rate": None,
            # A draft PRODUCED is not work routed. Claude is told to discard the
            # draft whenever the answer depends on anything the draft model could
            # not see, which in a repo is most of the time — so `success` is an
            # upper bound and `used` is the number that corresponds to spend.
            "used": 0, "unused": 0, "use_rate": None, "effective_rate": None,
        })
        d["prompts"] += 1
        d[(rec["outcome"] or Outcome.OTHER).value] += 1
        for note in rec["notes"]:
            d[note] += 1
    for d in days.values():
        d["rate"] = (d["success"] / d["prompts"]) if d["prompts"] else 0.0
        d["drafts"] = d["success"] + d["rejected"]
        # None, not 0.0: a day with no drafts is a day grounding was never asked
        # about, and reporting that as a perfect record would be a lie of omission.
        d["grounding_rate"] = (
            d["rejected"] / d["drafts"] if d["drafts"] else None
        )
        judged = d["used"] + d["unused"]
        # None rather than 0.0 on a day with no verdicts: "we did not measure"
        # and "nothing was used" are different claims, and reporting the second
        # when the first is true is how the old counter misled.
        d["use_rate"] = (d["used"] / judged) if judged else None
        # The headline worth quoting: prompts whose answer actually came from a
        # local model, over all prompts. `rate` above counts drafts produced.
        d["effective_rate"] = (d["used"] / d["prompts"]) if d["prompts"] else None
    return days


def _generation_lines(log: Path) -> Iterable[str]:
    """Every line of the debug log across its rotated generations, oldest first."""
    for gen in routing_log.generations(log):
        with gen.open(encoding="utf-8", errors="ignore") as fh:
            yield from fh


def draft_acceptance(log: Path | None = None) -> tuple[int, int]:
    """(drafts RELAYED as the answer, drafts OFFERED) over the log.

    audit/28. `hooks/draft_usage.py` already decides this correctly — it looks
    for the `🎯 LLM Router routed` marker on the first line of the next
    assistant turn — and writes its verdict only to the debug log. Nothing
    tallied it, so the verdict was computed, correct, and read by nobody: the
    CLASS-A shape R12 exists to stop.

    Why it matters more than the routing rate: `DIRECT SUCCESS` counts drafts
    PRODUCED. `draft_usage.py`'s own docstring records a session whose log
    "showed successful routing throughout" while it "drove subscription quota
    from 49% to 79%, because every draft in it was discarded."

    Measured on this machine 2026-09-23: **0 used of 44 offered**, for 773
    seconds of local model time and ~18.7s of added latency on the median
    prompt.

    Returns a PAIR, never a bare rate — the denominator is the whole point in
    this repo, and an acceptance rate over 3 drafts is noise.

    Counted from the log rather than a new writer, exactly as
    `unterminated_invocations` is: the data already exists, and a second writer
    is a second thing that can disagree.
    """
    log = log or (_home() / "auto-route-debug.log")
    return _draft_counts(log)


# ── incremental draft counters (D-171 A2) ───────────────────────────────────
# `draft_acceptance` ran on every 10th prompt and read the whole, unbounded debug
# log (81 ms at 50 MB, 325 ms at 200 MB, measured). The counts are now kept in a
# sidecar next to the log: ``used``/``offered`` over the log up to ``offset``, plus a
# fingerprint of the file's first bytes. A call scans only what was appended since,
# which is the same answer as a full scan of an append-only log. If the log was
# rotated (``capped_log``: ``<log>.1`` newest backup), the unread remainder of the
# generation the sidecar described is counted, then the newer generations in full.
# A missing, corrupt or unwritable sidecar costs one full scan, never a wrong count.
_DRAFT_SIG_BYTES = 256
_DRAFT_BACKUPS = routing_log.BACKUPS  # generations the hook keeps when it rotates the log


def _count_draft_bytes(data: bytes) -> tuple[int, int]:
    used = offered = 0
    for line in data.split(b"\n"):
        if b"DRAFT UNUSED:" in line:
            offered += 1
        elif b"DRAFT USED:" in line or b"relayed as the answer" in line:
            used += 1
            offered += 1
    return used, offered


def _scan_from(path: Path, offset: int) -> tuple[int, int, int, int, int]:
    """(used, offered) over the COMPLETE lines of ``path`` from ``offset``, the offset
    just past the last complete line, and the (used, offered) of an unterminated
    last line (counted in the answer, not stored: it will be re-read once finished)."""
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    cut = data.rfind(b"\n") + 1
    used, offered = _count_draft_bytes(data[:cut])
    tail_used, tail_offered = _count_draft_bytes(data[cut:])
    return used, offered, offset + cut, tail_used, tail_offered


def _head(path: Path, n: int) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def _draft_counts(log: Path) -> tuple[int, int]:
    import hashlib

    side = log.with_name(log.name + ".draft_counts.json")
    state = None
    try:
        raw = json.loads(side.read_text(encoding="utf-8"))
        if (raw.get("v") == 1 and all(isinstance(raw.get(k), int) and raw[k] >= 0
                                      for k in ("offset", "sig_len", "used", "offered"))
                and isinstance(raw.get("sig"), str)):
            state = raw
    except (OSError, ValueError, AttributeError):
        state = None

    def matches(path: Path) -> bool:
        try:
            if path.stat().st_size < state["offset"]:
                return False
            head = _head(path, state["sig_len"])
            return (len(head) == state["sig_len"]
                    and hashlib.sha1(head).hexdigest() == state["sig"])
        except OSError:
            return False

    backups = [log.with_name(f"{log.name}.{n}") for n in range(1, _DRAFT_BACKUPS + 1)]
    backups = [b for b in backups if b.exists()]
    if not log.exists():
        return (state["used"], state["offered"]) if state else (0, 0)

    used = offered = 0
    todo: list[tuple[Path, int]]  # (file, offset) in the order they were written
    if state is not None and matches(log):
        used, offered = state["used"], state["offered"]
        todo = [(log, state["offset"])]
    elif state is not None and any(matches(b) for b in backups):
        k = next(i for i, b in enumerate(backups) if matches(b))
        used, offered = state["used"], state["offered"]
        todo = [(backups[k], state["offset"])]
        todo += [(b, 0) for b in reversed(backups[:k])] + [(log, 0)]
    else:
        todo = [(b, 0) for b in reversed(backups)] + [(log, 0)]

    end = 0
    tail_u = tail_o = 0
    log_scanned = False
    for path, off in todo:
        try:
            u, o, e, tu, to = _scan_from(path, off)
        except OSError:
            continue
        used += u
        offered += o
        if path is log:
            end, tail_u, tail_o, log_scanned = e, tu, to, True
        else:  # a finished generation: its unterminated last line is final, count it
            used += tu
            offered += to
    if not log_scanned:
        return (used, offered)
    try:
        sig_len = min(_DRAFT_SIG_BYTES, end)
        new = {"v": 1, "offset": end, "sig_len": sig_len,
               "sig": hashlib.sha1(_head(log, sig_len)).hexdigest(),
               "used": used, "offered": offered}
        tmp = side.with_name(side.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(new, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, side)
    except Exception as exc:  # noqa: BLE001 -- a counter cache that cannot be written is only slower
        from llm_router import failopen

        failopen.record("CHZ-FO-DRAFT-COUNTS-WRITE", exc)
    return (used + tail_u, offered + tail_o)


def unterminated_invocations(log: Path | None = None) -> tuple[int, int]:
    """(invocations with no terminal outcome, real invocations) over the log.

    R12. The CLAUDE.md invariant — every invocation logging ``prompt_len=`` logs
    exactly one of ``DIRECT:``, ``DIRECT SKIP:``, ``BYPASS`` or ``CONTINUATION``
    — has been enforced by a unit test since ``ENFORCE=off`` cost a day of
    investigation by skipping the DIRECT block while logging nothing. That test
    proves the code CAN log an outcome on the branches it knows about. It cannot
    see a branch nobody thought to test, which is what the original defect was.

    This computes the invariant on the traffic that actually happened, which is
    the only form of it with any evidential weight. `summarise` already buckets
    an invocation with no terminal line into ``other``; it had no caller.

    Returns a PAIR, never a bare rate: the denominator is the whole point in
    this repo (see CLAUDE.md, "A rate without its denominator is not a
    measurement"), and a rate over 20 prompts is noise reported as a collapse.
    Real user prompts only — `summarise` drops the test suite's sessions, which
    were 54% of this file once.
    """
    log = log or (_home() / "auto-route-debug.log")
    if not routing_log.generations(log):
        return (0, 0)
    days = summarise(parse_log(_generation_lines(log)))
    return (
        sum(d["other"] for d in days.values()),
        sum(d["prompts"] for d in days.values()),
    )


def _outcome_counts(log: Path) -> dict[str, int]:
    """Outcome matrix over REAL user prompts, one outcome per invocation.

    Counted raw line occurrences before. That included the test suite — 227
    invocations carrying `chain=['ollama/fake-model']` and an empty session id once
    sat in this log — and it could report more successes than prompts, because a
    DIRECT SUCCESS invocation never reaches OUTPUT COMPLETE, so the two were
    disjoint sets. `parse_log` groups by invocation and `summarise` drops
    non-sessions, so the numbers reconcile with the per-day view and with each
    other.
    """
    if not routing_log.generations(log):
        return {k: 0 for k in ("DIRECT SUCCESS", "DIRECT SKIP", "DIRECT FAILED")}
    days = summarise(parse_log(_generation_lines(log)))
    return {
        "DIRECT SUCCESS": sum(d["success"] for d in days.values()),
        "DIRECT SKIP": sum(d["skipped"] for d in days.values()),
        "DIRECT FAILED": sum(d["failed"] for d in days.values()),
    }


def _violations(log: Path) -> int:
    return len(re.findall(r"VIOLATION", log.read_text(errors="ignore"))) if log.exists() else 0


def generate_report() -> str:
    db = _home() / "usage.db"
    if not db.exists():
        return "# LLM Router routing report\n\n(no usage.db yet — nothing has routed)\n"

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        not_error = not_error_row_sql(con)
        rows = con.execute(
            """SELECT model, provider, COUNT(*) n,
                      SUM(input_tokens) tin, SUM(output_tokens) tout,
                      ROUND(AVG(latency_ms)) avg_ms, MAX(latency_ms) max_ms,
                      ROUND(SUM(COALESCE(saved_usd,0)), 5) saved
               FROM usage WHERE """ + not_error + """ GROUP BY model, provider ORDER BY n DESC"""
        ).fetchall()
        lats = [r[0] for r in con.execute(
            "SELECT latency_ms FROM usage WHERE latency_ms > 0 AND " + not_error).fetchall()]
        # routing source breakdown (gateway vs hook vs mcp) if reason_code exists
        try:
            srcs = con.execute(
                "SELECT COALESCE(classifier_type,'?'), COUNT(*) FROM routing_decisions "
                "WHERE " + real_decision_sql(con) + " GROUP BY 1 ORDER BY 2 DESC").fetchall()
        except sqlite3.Error:
            srcs = []
    finally:
        con.close()

    total_calls = sum(r[2] for r in rows)
    total_in = sum(r[3] or 0 for r in rows)
    total_out = sum(r[4] or 0 for r in rows)
    total_saved = sum(r[7] or 0 for r in rows)

    L = ["# LLM Router routing report", "", "## Summary",
         f"- **Routed calls:** {total_calls:,}",
         f"- **Tokens routed:** {total_in:,} in · {total_out:,} out · {total_in + total_out:,} total",
         f"- **Estimated saved (vs baseline):** ${total_saved:.4f}",
         f"- **Latency:** p50 {_pctl(lats, 50)/1000:.1f}s · p95 {_pctl(lats, 95)/1000:.1f}s · "
         f"max {(max(lats) if lats else 0)/1000:.1f}s", ""]

    L += ["## By model", "",
          "| model | provider | calls | tok in | tok out | avg ms | max ms | saved $ |",
          "|---|---|--:|--:|--:|--:|--:|--:|"]
    for model, provider, n, tin, tout, avg_ms, max_ms, saved in rows:
        L.append(f"| {model} | {provider} | {n} | {tin or 0:,} | {tout or 0:,} | "
                 f"{int(avg_ms or 0):,} | {int(max_ms or 0):,} | {saved or 0:.5f} |")
    L.append("")

    if srcs:
        L += ["## Routed via", "", "| source | calls |", "|---|--:|"]
        L += [f"| {s} | {n} |" for s, n in srcs] + [""]

    oc = _outcome_counts(_home() / "auto-route-debug.log")
    tot = sum(oc.values()) or 1
    L += ["## Routing outcomes (Claude Code hook path)", "",
          "| outcome | count | % |", "|---|--:|--:|"]
    L += [f"| {k} | {v} | {100*v/tot:.0f}% |" for k, v in oc.items()]
    L += [f"| overrides (model did the work) | {_violations(_home() / 'enforcement.log')} | — |", ""]

    if lats:
        slow = len([x for x in lats if x > 15000])
        L += ["## Latency note", "",
              f"- {slow} call(s) > 15s — typically Ollama model-swap cold-loads. "
              "Set `OLLAMA_MAX_LOADED_MODELS≥2` + `OLLAMA_KEEP_ALIVE=-1` to avoid.", ""]

    return "\n".join(L)


def main() -> None:
    report = generate_report()
    out = _home() / "routing_report.md"
    out.write_text(report)
    print(f"Wrote {out}\n")
    print(report)


if __name__ == "__main__":
    main()
