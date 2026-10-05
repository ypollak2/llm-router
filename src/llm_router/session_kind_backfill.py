"""Backfill ``session_kind`` for sessions that predate PR #250's tagging hook.

WHY: ``session_kind.py``'s docstring says a session with no SessionStart tag stays
untagged forever -- "A tag file can only be created, not backfilled." Most sessions on
this machine predate the hook and are EXCLUDED from every KPI that reads
:class:`session_kind.KindIndex` (NS, D1, D2, D3): not miscounted, just invisible. This
module recovers a kind for those sessions from the one durable record that survives
them: the Claude Code transcript file, ``~/.claude/projects/*/<session_id>.jsonl``,
which already carries ``cwd`` and ``entrypoint`` on its own records -- the exact two
signals :func:`session_kind.classify_with_basis` uses. Same rules, same precedence,
applied after the fact instead of at session start.

CONTRACT (owner-approved, 2026-10-04):

* The sidecar is a SEPARATE file, ``session_kind_backfill.jsonl``. The four ledgers
  (``proxy_calls.jsonl``, ``edit_outcomes.jsonl``, ``north_star_units.jsonl``,
  ``usage_outcomes.jsonl``) are read ONLY to find which ``session_id``s need a kind;
  this module never writes to any of them.
* Write-once per session: a ``session_id`` that already has a sidecar row (a resolved
  kind, or ``"unknown"``) is never re-derived or rewritten. Re-running
  ``--backfill-tags`` is therefore a no-op once every untagged session_id has one row.
  Strict under concurrency too: the append takes a file lock and re-reads the sidecar
  inside it (:func:`_append_rows`), so two runs started together never write the same
  ``session_id`` twice and never interleave a line.
* A live tag always wins: :meth:`session_kind.KindIndex.resolve` consults the sidecar
  LAST, only after the tag file, the record's own stamp, and the session's proxy rows
  all have nothing to say. This module never touches that precedence; it only supplies
  the last-resort fallback. :func:`session_kind.kind_of` -- the proxy hot path and
  ``tag_session``'s own write-once check -- never reads the sidecar at all, so a
  backfilled kind can never shadow or pre-empt a live tag.
* Who reads the sidecar (:func:`load_sidecar`) -- and who does not. Read: ``llm-router
  kpi`` (NS, D1, D2 via ``northstar.units(backfill=True)``; D3 via its own
  ``KindIndex``) and this module's own command (``--backfill-tags``,
  ``--validate-backfill``). Not read, by construction (``backfill`` defaults to off in
  ``northstar.units`` / ``build_sessions`` / ``_scan_proxy_ledger``): the proxy, every
  hook, the Stop line (``northstar.current_session_line``), the quality breaker and
  ``llm-router northstar``. ``tests/test_session_kind_backfill.py`` pins zero
  ``load_sidecar`` calls on the Stop-line and quality-breaker paths.
* Evidence insufficiency -> ``"unknown"``, never ``"organic"``. ``"unknown"`` is NOT a
  member of ``session_kind.VALID_KINDS``; ``KindIndex`` never resolves a session to it,
  so an ``unknown`` session stays exactly as invisible to the KPIs as an untagged one
  is today.
* No prompt text, no full paths. A row holds ``session_id``, ``kind``, ``source``
  (``"backfill"``), ``basis`` (one coarse code, below) and ``ts``.
* Deleting the sidecar restores the pre-backfill behaviour exactly: ``KindIndex`` finds
  nothing at its last step, same as if this module had never run.
"""
from __future__ import annotations

import errno
import glob as _glob
import json
import re
import time
from pathlib import Path
from typing import Any

from llm_router import edit_ledger, paths, session_kind, usage_outcome
from llm_router.file_lock import exclusive_lock

#: Evidence-insufficiency basis codes. ``session_kind.BASIS_*`` covers the rule-based
#: ones once evidence exists; these three cover the ways it does not.
BASIS_NO_TRANSCRIPT = "no_transcript"                  # no ~/.claude/projects/*/<sid>.jsonl file
BASIS_UNREADABLE_TRANSCRIPT = "unreadable_transcript"  # file exists but not one line parsed as JSON
BASIS_NO_CWD_EVIDENCE = "no_cwd_evidence"              # parsed fine, but no record carries cwd/entrypoint
BASIS_EVIDENCE_CAP = "evidence_cap"                    # a read cap was hit before cwd AND entrypoint were seen

#: How much of one transcript ``derive_kind`` will read. ``cwd`` and ``entrypoint`` sit on
#: a transcript's first records, so the loop normally stops within a few KiB; the caps bound
#: the cases where it would not (a field that never appears reads a multi-hundred-MiB file
#: to EOF; one enormous line is read whole into memory). Hitting either cap before BOTH
#: fields are seen is "unknown", never a guess: a missing ``entrypoint`` may be exactly an
#: ``sdk*`` session that a partial read would have called organic.
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024

#: What a session id looks like (Claude Code's are UUIDs, sub-agents' ``agent-<hex>``): the
#: charset ``session_kind._safe`` keeps, starting alphanumeric so ``..`` and dotfiles cannot
#: match. Anything else (a path separator above all) never reaches a glob.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: Not in ``session_kind.VALID_KINDS`` by design -- see the module docstring.
UNKNOWN_KIND = "unknown"

SIDECAR_FILENAME = "session_kind_backfill.jsonl"

#: The four ledgers session_ids are enumerated from (see the module docstring). Each is
#: read as plain JSONL, tolerant of corrupt lines -- the same pattern
#: ``proxy.ledger.read_rows`` and ``usage_outcome.load_ledger`` already use.
_LEDGER_FILENAMES = (
    "proxy_calls.jsonl",        # llm_router.proxy.ledger.LEDGER_NAME
    edit_ledger.LEDGER_FILENAME,
    "north_star_units.jsonl",   # llm_router.northstar._north_star_units_path()
    usage_outcome.LEDGER_FILENAME,
)


def sidecar_path() -> Path:
    return paths.state_path(SIDECAR_FILENAME)


def _iter_jsonl(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def ledger_session_ids(home: Path | None = None) -> set[str]:
    """Every distinct ``session_id`` across the four ledgers, read-only."""
    base = home if home is not None else paths.llm_router_home()
    ids: set[str] = set()
    for name in _LEDGER_FILENAMES:
        for row in _iter_jsonl(base / name):
            sid = row.get("session_id")
            if isinstance(sid, str) and sid:
                ids.add(sid)
    return ids


def unit_session_ids(root: Path | None = None) -> set[str]:
    """Every ``session_id`` that has at least one north-star unit, over ALL time. NS, D1
    and D2 are computed from these transcript-derived units, and most of them belong to
    sessions that never appear in any ledger -- enumerating only the ledgers would leave
    exactly the sessions those KPIs need unresolved. Read-only (it parses transcripts;
    it writes nothing), and slow on a large corpus -- a one-shot admin cost. Only the
    session ids are used, so the sidecar is deliberately not consulted
    (``build_sessions``' default ``backfill=False``)."""
    from llm_router import northstar
    return set(northstar.build_sessions(days=None, root=root, backfill=False))


def _proxy_kind_index(home: Path | None = None) -> "session_kind.KindIndex":
    """A KindIndex over the proxy ledger with the sidecar switched OFF: answers "does
    live evidence already resolve this session?" without the answer depending on rows
    this command wrote earlier."""
    base = home if home is not None else paths.llm_router_home()
    idx = session_kind.KindIndex(backfill=False)
    for row in _iter_jsonl(base / "proxy_calls.jsonl"):
        idx.add(row.get("session_id"), row.get("session_kind"))
    return idx


def load_sidecar(path: Path | None = None) -> dict[str, str]:
    """``session_id -> kind`` (``"unknown"`` included) from every valid row. Corrupt
    lines are skipped, not fatal. A duplicate ``session_id`` keeps its FIRST row only --
    write-once means there should never be one, but a hand-edited file must not crash a
    reader, and the first row is the one every earlier read already saw and used."""
    target = path if path is not None else sidecar_path()
    out: dict[str, str] = {}
    for row in _iter_jsonl(target):
        sid, kind = row.get("session_id"), row.get("kind")
        if isinstance(sid, str) and sid and isinstance(kind, str) and kind and sid not in out:
            out[sid] = kind
    return out


def _claude_projects_dir() -> Path:
    from llm_router import northstar
    return northstar.claude_projects_dir()


def build_transcript_index(root: Path | None = None) -> dict[str, Path]:
    """``session_id -> its transcript path``, built once so deriving a kind for many
    sessions does not re-list ``~/.claude/projects`` per session."""
    base = root if root is not None else _claude_projects_dir()
    return {p.stem: p for p in base.glob("*/*.jsonl")}


def _transcript_path(session_id: str, root: Path | None = None,
                     index: dict[str, Path] | None = None) -> Path | None:
    if index is not None:
        return index.get(session_id)
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None  # ``../../etc/passwd`` must never be interpolated into a glob pattern
    base = root if root is not None else _claude_projects_dir()
    matches = sorted(base.glob(f"*/{_glob.escape(session_id)}.jsonl"))
    return matches[0] if matches else None


def derive_kind(session_id: str, root: Path | None = None,
                index: dict[str, Path] | None = None) -> tuple[str, str]:
    """``(kind, basis)`` for ``session_id`` from its transcript alone. ``kind`` is
    ``"unknown"`` when the transcript is missing, unreadable, carries neither a ``cwd``
    nor an ``entrypoint`` field on any record, or does not show both within
    ``MAX_TRANSCRIPT_BYTES`` / ``MAX_LINE_BYTES`` -- NEVER ``"organic"`` as a default:
    missing evidence must stay invisible, not get counted."""
    path = _transcript_path(session_id, root, index)
    if path is None:
        return UNKNOWN_KIND, BASIS_NO_TRANSCRIPT
    cwd: str | None = None
    entrypoint: str | None = None
    saw_any_row = False
    capped = False
    read = 0
    try:
        with path.open("rb") as fh:
            while True:
                if read >= MAX_TRANSCRIPT_BYTES:
                    capped = bool(fh.read(1))   # exactly at EOF is a complete read, not a cap hit
                    break
                raw = fh.readline(min(MAX_LINE_BYTES + 1, MAX_TRANSCRIPT_BYTES - read))
                if not raw:
                    break
                read += len(raw)
                if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    capped = True               # one line longer than the cap: stop, do not slurp it
                    break
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                saw_any_row = True
                if cwd is None and isinstance(row.get("cwd"), str):
                    cwd = row["cwd"]
                if entrypoint is None and isinstance(row.get("entrypoint"), str):
                    entrypoint = row["entrypoint"]
                if cwd is not None and entrypoint is not None:
                    break
    except OSError:
        return UNKNOWN_KIND, BASIS_UNREADABLE_TRANSCRIPT
    if capped and (cwd is None or entrypoint is None):
        return UNKNOWN_KIND, BASIS_EVIDENCE_CAP
    if not saw_any_row:
        return UNKNOWN_KIND, BASIS_UNREADABLE_TRANSCRIPT
    if cwd is None and entrypoint is None:
        return UNKNOWN_KIND, BASIS_NO_CWD_EVIDENCE
    return session_kind.classify_with_basis(cwd=cwd, entrypoint=entrypoint)


#: errno values meaning "this location cannot be written", not "someone holds the lock".
_UNWRITABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def _append_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    """Append ``rows`` whose ``session_id`` is not already in the sidecar; returns how
    many were written. Append-only, 0600 (``paths.private_opener``), matching
    ``usage_outcome.record_outcomes``'s idiom -- except this write path RAISES on
    failure: it is a deliberate one-shot admin operation the owner asked for
    (``kpi --backfill-tags``), not hot-path telemetry that must never block a session.

    CONCURRENCY. ``backfill_sessions`` decides what is new from a snapshot it took a
    while ago (a transcript scan, seconds to minutes on a large corpus), so two runs
    started together both believe every session is new. The write is therefore a
    critical section: take ``file_lock.exclusive_lock`` on a sibling ``.lock`` file,
    RE-READ the sidecar inside it, and append only what is still missing, in one
    ``write`` call. Write-once is then strict -- no duplicate ``session_id`` rows, no
    interleaved or torn lines -- not merely tolerated by the first-wins reader.

    A directory this call creates is 0700 (the sidecar names sessions); an existing one
    is left exactly as its owner made it. It is created BEFORE the lock is taken,
    because ``exclusive_lock`` would otherwise create it with the default mode."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(exc.errno, f"cannot create {path.parent} ({exc.strerror}); "
                      "nothing was written") from exc
    lock_path = path.with_name(path.name + ".lock")
    with exclusive_lock(lock_path) as held:
        if not held:
            # ``exclusive_lock`` reports every failure as "not held". Tell contention
            # (another run holds it) from a permission / read-only-filesystem error,
            # which no amount of waiting fixes and would mislead the owner.
            try:
                with open(lock_path, "a+"):
                    pass
            except OSError as exc:
                if exc.errno in _UNWRITABLE_ERRNOS or isinstance(exc, PermissionError):
                    raise OSError(exc.errno, f"cannot write {lock_path} ({exc.strerror}: permission "
                                  "denied or read-only filesystem); nothing was written") from exc
            raise OSError(f"could not lock {lock_path} (is another --backfill-tags running?); "
                          "nothing was written")
        present = set(load_sidecar(path))
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if row["session_id"] not in present:
                present.add(row["session_id"])
                fresh.append(row)
        if fresh:
            payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in fresh)
            with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
                fh.write(payload)
        return len(fresh)


def backfill_sessions(*, dry_run: bool = False, home: Path | None = None,
                      root: Path | None = None, sidecar: Path | None = None,
                      now: float | None = None, with_units: bool = True) -> dict[str, Any]:
    """Derive and (unless ``dry_run``) persist a kind for every ``session_id`` that has
    neither live evidence nor an existing sidecar row. Candidates are the session_ids in
    the four ledgers plus (``with_units``) those of every session with a north-star unit.
    Returns counts only -- never prompt text or full paths -- so a caller can print or
    log the result safely.
    """
    sidecar_file = sidecar if sidecar is not None else sidecar_path()
    existing = load_sidecar(sidecar_file)
    from_ledgers = ledger_session_ids(home)
    from_units = unit_session_ids(root) if with_units else set()
    candidates = sorted((from_ledgers | from_units) - existing.keys())
    now_ts = now if now is not None else time.time()
    transcripts = build_transcript_index(root)
    live = _proxy_kind_index(home)

    new_rows: list[dict[str, Any]] = []
    by_kind: dict[str, int] = {}
    by_basis: dict[str, int] = {}
    skipped = {session_kind.SOURCE_TAG: 0, session_kind.SOURCE_LEDGER: 0,
               session_kind.SOURCE_CONFLICT: 0}
    for sid in candidates:
        res = live.resolve(sid)
        if res.source in skipped:
            # Live evidence already speaks for this session (its tag file, or its proxy
            # rows' stamps -- agreeing, or in conflict). KindIndex never reaches the
            # sidecar in any of those cases, so a row here would be dead weight, and
            # for a conflict it would be a guess laid over a disagreement.
            skipped[res.source] += 1
            continue
        kind, basis = derive_kind(sid, root, transcripts)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_basis[basis] = by_basis.get(basis, 0) + 1
        new_rows.append({"session_id": sid, "kind": kind, "source": session_kind.SOURCE_BACKFILL,
                          "basis": basis, "ts": now_ts})

    written = 0
    if new_rows and not dry_run:
        written = _append_rows(sidecar_file, new_rows)

    return {
        "candidates": len(candidates), "already_backfilled": len(existing),
        "candidates_only_from_units": len(from_units - from_ledgers - existing.keys()),
        "skipped_live_tag": skipped[session_kind.SOURCE_TAG],
        "skipped_live_proxy_stamp": skipped[session_kind.SOURCE_LEDGER],
        "skipped_live_conflict": skipped[session_kind.SOURCE_CONFLICT],
        "new_rows": len(new_rows), "written": written, "dry_run": dry_run,
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "by_basis": dict(sorted(by_basis.items(), key=lambda kv: -kv[1])),
        "sidecar_path": str(sidecar_file),
    }


def validate_against_live(*, home: Path | None = None,
                          root: Path | None = None) -> dict[str, Any]:
    """Run the backfill rules on sessions that ALREADY carry a live kind and compare.

    Population: every session with a transcript or a ledger row whose live kind is known
    -- from its tag file, or from record stamps in the four ledgers that all agree (a
    session whose stamps disagree is counted in ``live_conflicts_excluded``, not scored).
    Each is run through :func:`derive_kind` exactly as an untagged session would be.
    Read-only and counts-only. The one limit the transcript cannot see: a session whose
    live kind came from the ``LLM_ROUTER_SESSION_KIND`` override. It shows up here as a
    disagreement, which is the honest measure of how much that blind spot costs.

    ``agreement_strict`` counts an ``unknown`` verdict as a miss; ``agreement_decided``
    leaves unknowns out of the denominator (they never enter a KPI, so they cannot make
    one wrong -- they only make the backfill smaller)."""
    base = home if home is not None else paths.llm_router_home()
    stamps: dict[str, set[str]] = {}
    for name in _LEDGER_FILENAMES:
        for row in _iter_jsonl(base / name):
            sid, kind = row.get("session_id"), row.get("session_kind")
            if isinstance(sid, str) and sid and kind in session_kind.VALID_KINDS:
                stamps.setdefault(sid, set()).add(kind)
    transcripts = build_transcript_index(root)
    population = set(transcripts) | ledger_session_ids(home)

    confusion: dict[str, dict[str, int]] = {}
    by_live_source = {session_kind.SOURCE_TAG: 0, session_kind.SOURCE_STAMP: 0}
    n = agree = unknown = conflicts = 0
    for sid in sorted(population):
        live = session_kind.kind_of(sid)
        source = session_kind.SOURCE_TAG
        if live is None:
            seen = stamps.get(sid, set())
            if not seen:
                continue
            if len(seen) > 1:
                conflicts += 1
                continue
            live, source = next(iter(seen)), session_kind.SOURCE_STAMP
        derived, _basis = derive_kind(sid, root, transcripts)
        n += 1
        by_live_source[source] += 1
        confusion.setdefault(live, {})
        confusion[live][derived] = confusion[live].get(derived, 0) + 1
        if derived == UNKNOWN_KIND:
            unknown += 1
        elif derived == live:
            agree += 1
    decided = n - unknown
    return {
        "n": n, "agree": agree, "unknown": unknown, "disagree": decided - agree,
        "agreement_strict": (agree / n) if n else None,
        "agreement_decided": (agree / decided) if decided else None,
        "live_conflicts_excluded": conflicts, "by_live_source": by_live_source,
        "confusion": {k: dict(sorted(v.items())) for k, v in sorted(confusion.items())},
    }


def render_validation(result: dict[str, Any]) -> str:
    def pct(x: float | None, n: int) -> str:
        return "not measurable (n=0)" if x is None else f"{x * 100:.1f}% (n={n})"

    n = result["n"]
    lines = [
        f"backfill rules vs live evidence: {n} session(s) scored "
        f"({result['by_live_source']['tag']} by tag file, {result['by_live_source']['stamp']} by "
        f"agreeing record stamps; {result['live_conflicts_excluded']} excluded for conflicting stamps).",
        f"agreement, unknown counted as a miss: {pct(result['agreement_strict'], n)}",
        f"agreement among decided (unknown left out): "
        f"{pct(result['agreement_decided'], n - result['unknown'])}",
        "confusion (rows = live kind, columns = backfill verdict):",
    ]
    for live, row in result["confusion"].items():
        lines.append(f"  {live}: " + ", ".join(f"{k} {v}" for k, v in row.items()))
    if n < 30:
        lines.append(f"note: n={n} is too small for the 95% gate to mean much.")
    return "\n".join(lines)


def render_backfill_report(result: dict[str, Any]) -> str:
    verb = "would write" if result["dry_run"] else "wrote"
    live = (result["skipped_live_tag"] + result["skipped_live_proxy_stamp"]
            + result["skipped_live_conflict"])
    lines = [
        f"session_kind backfill: {result['candidates']} session_id(s) to consider "
        f"({result['candidates_only_from_units']} found only through their north-star units)"
        + (f"; {result['already_backfilled']} already in the sidecar from an earlier run"
           if result["already_backfilled"] else "") + ".",
        f"{live} already resolve from live evidence and get no row "
        f"(tag file {result['skipped_live_tag']}, proxy-row stamps "
        f"{result['skipped_live_proxy_stamp']}, proxy-row conflict "
        f"{result['skipped_live_conflict']}).",
        f"{verb} {result['new_rows']} new sidecar row(s)"
        + ("" if result["dry_run"] else f" ({result['written']} written)") + ".",
    ]
    if not result["dry_run"] and result["written"] < result["new_rows"]:
        lines.append(f"{result['new_rows'] - result['written']} of them were already written by a "
                     "concurrent run and were skipped (write-once).")
    if result["by_kind"]:
        lines.append("by kind: " + ", ".join(f"{k} {n}" for k, n in result["by_kind"].items()))
    if result["by_basis"]:
        lines.append("by basis: " + ", ".join(f"{k} {n}" for k, n in result["by_basis"].items()))
    lines.append(f"sidecar: {result['sidecar_path']}"
                 + (" (dry-run: not written)" if result["dry_run"] else ""))
    return "\n".join(lines)
