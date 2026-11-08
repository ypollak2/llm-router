"""`llm-router ns-check --session ID` — North Star mechanism check (plan PR D).

Reads ``proxy_calls.jsonl`` only (no network, no model, no state written) and reports,
for one session, what the A1 per-turn classifier record says. The ledger is read up to
a byte offset fixed when the command starts (printed as the snapshot offset), so a
proxy that keeps appending cannot change the figures mid-run.

Exit codes: 0 the session was read and has supported non-override rows; 2 the ledger is
unreadable or the session has no rows; 3 the session has rows but none carries the A1
keys, or no supported non-override row exists (the decided % is then undefined and is
never printed as a pass).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

A1_KEYS = ("supported", "unsupported_reason", "cls_turn_idx", "cls_turn_sha", "cls_decision_id",
           "cls_turn_source", "cls_turn_reason", "cls_turn_tier", "cls_turn_decided", "cls_turn_ms")
OVERRIDE_REASONS = ("prefix_claude", "prefix_local")
FAILOPEN_PREFIX = "failopen:"


def _read_snapshot(path: Path) -> tuple[int, list[dict], int]:
    """(offset, rows, skipped lines) for the bytes present when the file was first sized."""
    offset = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(offset)
    rows, skipped = [], 0
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            skipped += 1
    return offset, rows, skipped


def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.1f}% ({num}/{den})"


def _a1_key_names() -> tuple[str, ...] | None:
    try:
        from llm_router.proxy.cls_decide import TURN_FIELD_DEFAULTS
        return tuple(TURN_FIELD_DEFAULTS)
    except Exception:  # noqa: BLE001 - report instead of inventing
        return None


def analyse(rows: list[dict]) -> dict:
    a1 = [r for r in rows if r.get("supported") is not None]
    sup = [r for r in a1 if r.get("supported") is True]
    unsup = [r for r in a1 if r.get("supported") is False]
    override = [r for r in sup if r.get("cls_turn_reason") in OVERRIDE_REASONS]
    denom = [r for r in sup if r.get("cls_turn_reason") not in OVERRIDE_REASONS]
    decided = [r for r in denom if r.get("cls_turn_decided") is True]
    # One decision per cls_decision_id; the classifier row (not an inherit) carries the verdict.
    verdicts: dict[str, dict] = {}
    for r in decided:
        did = r.get("cls_decision_id")
        if did and did not in verdicts:
            verdicts[did] = r
    return {
        "rows": len(rows), "a1": a1, "sup": sup, "unsup": unsup, "override": override,
        "denom": denom, "decided": decided, "verdicts": verdicts,
        "turns": {(r.get("cls_turn_idx"), r.get("cls_turn_sha")) for r in sup},
        "sources": Counter(r.get("cls_turn_source") or "none" for r in a1),
        "unsup_reasons": Counter(r.get("unsupported_reason") or "(none recorded)" for r in unsup),
        "failopen_reasons": Counter(r.get("cls_turn_reason") for r in denom
                                    if str(r.get("cls_turn_reason") or "").startswith(FAILOPEN_PREFIX)),
        "other_reasons": Counter(r.get("cls_turn_reason") or "(none)" for r in denom
                                 if r.get("cls_turn_decided") is not True
                                 and not str(r.get("cls_turn_reason") or "").startswith(FAILOPEN_PREFIX)),
        "override_reasons": Counter(r.get("cls_turn_reason") for r in override),
    }


def _table(title: str, counter: Counter) -> None:
    print(title)
    if not counter:
        print("  (none)")
    for key, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  {str(key):<32s} {n}")


def render(session: str, path: Path, offset: int, skipped: int, rows: list[dict], total: int) -> int:
    a = analyse(rows)
    print(f"ns-check session={session}")
    print(f"ledger: {path}")
    print(f"snapshot offset: {offset} bytes (rows read: {total} total, "
          f"{a['rows']} for this session; unparseable lines skipped: {skipped})")
    print(f"rows with A1 keys: {len(a['a1'])} of {a['rows']}"
          + ("  (rows without them predate A1)" if len(a["a1"]) < a["rows"] else ""))
    names = _a1_key_names()
    if names is not None and "cls_turn_sha" not in names:
        print("cls_turn_sha: not an A1 key in this build; nothing is reported for it")
    else:
        have = sum(1 for r in a["a1"] if r.get("cls_turn_sha"))
        print(f"cls_turn_sha: recorded on {have} of {len(a['a1'])} A1 rows")
    if names is not None:
        missing = [k for k in A1_KEYS if k not in names]
        if missing:
            print(f"A1 keys this reader expects but the build does not define: {', '.join(missing)}")
    print()
    if not a["a1"]:
        print("RESULT: no row of this session carries the A1 keys; the decided % is undefined (not a pass)")
        return 3
    print(f"turns (distinct cls_turn_idx+sha, supported rows): {len(a['turns'])}")
    print(f"supported rows: {len(a['sup'])}   unsupported rows: {len(a['unsup'])}   "
          f"override rows (excluded from the denominator): {len(a['override'])}")
    if a["denom"]:
        print(f"decided % of supported non-override rows: {_pct(len(a['decided']), len(a['denom']))}")
    else:
        print("decided % of supported non-override rows: undefined, 0 rows in the denominator")
    print("failopen turns go local, not decided")
    print()
    _table("cls_turn_source counts (all A1 rows):", a["sources"])
    print()
    _table("unsupported rows by reason:", a["unsup_reasons"])
    print()
    _table("failopen rows by reason:", a["failopen_reasons"])
    if a["other_reasons"]:
        print()
        _table("other supported non-override rows not decided, by reason:", a["other_reasons"])
    if a["override_reasons"]:
        print()
        _table("override rows by reason:", a["override_reasons"])
    print()
    vs = a["verdicts"]
    by_reason = Counter(v.get("cls_turn_reason") for v in vs.values())
    by_tier = Counter(v.get("cls_turn_tier") or "(none)" for v in vs.values())
    print(f"verdict distribution (decided turns, one per cls_decision_id, n={len(vs)}):")
    _table(" by reason:", by_reason)
    _table(" by tier:", by_tier)
    if len(vs) >= 2 and len(by_reason) == 1:
        print(f"  NOTE: every decided turn has the same verdict ({next(iter(by_reason))}, n={len(vs)}); "
              "a constant classifier looks like this")
    elif len(vs) < 2:
        print(f"  NOTE: n={len(vs)} decided turns; too few to tell a constant classifier from a deciding one")
    if not a["denom"]:
        print("\nRESULT: no supported non-override row; the decided % is undefined (not a pass)")
        return 3
    print("\nRESULT: read ok")
    return 0


def cmd_ns_check(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="llm-router ns-check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--session", required=True, help="session id (the ledger's session_id)")
    ap.add_argument("--ledger", help="path to proxy_calls.jsonl (default: the state dir's)")
    ns = ap.parse_args(args)
    if ns.ledger:
        path = Path(ns.ledger)
    else:
        from llm_router.proxy.ledger import ledger_path
        path = ledger_path()
    try:
        offset, all_rows, skipped = _read_snapshot(path)
    except OSError as exc:
        print(f"ns-check: cannot read the ledger {path}: {exc}", file=sys.stderr)
        return 2
    rows = [r for r in all_rows if r.get("session_id") == ns.session]
    if not rows:
        print(f"ns-check: no ledger rows for session {ns.session} in {path} "
              f"(snapshot offset {offset}, {len(all_rows)} rows read)", file=sys.stderr)
        return 2
    return render(ns.session, path, offset, skipped, rows, len(all_rows))
