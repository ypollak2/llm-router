"""M-06 — `busy_timeout` must be set BEFORE `journal_mode=WAL`, everywhere.

`sqlite_wal.enable_wal` exists in this repository specifically to fix two
cold-start defects, and its own docstring records the measurements:

  * switching to WAL needs an exclusive lock, and at 12 concurrent cold starts
    **4 of 12** `LineageStore` constructions raised `database is locked`;
  * the PRAGMA reports failure by **returning the mode in effect**, not by
    raising — so losing the race non-exceptionally yields `"delete"`, the
    connection proceeds in rollback-journal mode where a writer blocks every
    reader, and the *next* operation is the one that fails. That is how 66
    events went missing across 2400 concurrent writes with nothing in any log.

The audit found `enable_wal` adopted by **3 of 9** sites. Of the six that had
not:

    result_cache, agents/session, dashboard/tui   set busy_timeout AFTER WAL
    semantic/store, semantic/traces, cost.py      never set it at all

Setting it afterwards is the one ordering that leaves the single statement which
most needs the timeout running on SQLite's 5-second default. This is the audit's
recurring shape: not "write the fix", but "finish adopting the fix that already
exists".
"""

from __future__ import annotations

import pathlib
import re
import sqlite3


SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "llm_router"

# Sites that open a SQLite connection and put it into WAL mode.
_WAL_SITES = [
    "result_cache.py",
    "agents/session.py",
    "dashboard/tui.py",
    "semantic/store.py",
    "semantic/traces.py",
    "cost.py",
    "hooks/cc-usage-track.py",
]


def _sites_with_raw_wal() -> dict[str, list[int]]:
    """Files still issuing `journal_mode=WAL` themselves, and where."""
    out: dict[str, list[int]] = {}
    for rel in _WAL_SITES:
        f = SRC / rel
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'journal_mode\s*=\s*WAL', line) and not line.strip().startswith("#"):
                out.setdefault(rel, []).append(i)
    return out


def test_every_raw_wal_site_sets_busy_timeout_first():
    """Ordering, checked as ordering — not merely as presence.

    A file can contain both PRAGMAs and still be wrong; only the sequence
    matters, so compare line numbers.
    """
    problems = []
    for rel, wal_lines in _sites_with_raw_wal().items():
        text = (SRC / rel).read_text(encoding="utf-8")
        lines = text.splitlines()
        timeout_lines = [
            i for i, line in enumerate(lines, 1)
            if re.search(r"busy_timeout", line) and not line.strip().startswith("#")
        ]
        for wal_line in wal_lines:
            earlier = [t for t in timeout_lines if t < wal_line]
            if not earlier:
                problems.append(
                    f"{rel}:{wal_line} sets journal_mode=WAL with no busy_timeout before it"
                )
    assert not problems, "\n".join(problems)


def test_the_sync_sites_delegate_to_the_shared_helper():
    """Five sync sites had hand-rolled PRAGMAs; they must use `enable_wal`.

    Hand-rolling is how the three original call sites drifted into two different
    wrong orderings in the first place.
    """
    missing = []
    for rel in ("result_cache.py", "agents/session.py", "dashboard/tui.py",
                "semantic/store.py", "semantic/traces.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        if "enable_wal(" not in text:
            missing.append(rel)
    assert not missing, f"still hand-rolling the WAL pragma: {missing}"


def test_cost_notices_when_wal_was_not_established():
    """The async path cannot use the sync helper, so it must replicate the check.

    A silent fallback to rollback-journal mode is the defect; falling back is
    not.
    """
    text = (SRC / "cost.py").read_text(encoding="utf-8")
    block = text.split("PRAGMA journal_mode = WAL")[1][:800]
    assert "wal" in block.lower(), "cost.py does not inspect the returned mode"
    assert "warning" in block.lower() or "failopen" in block.lower(), (
        "cost.py can fall back to rollback-journal mode without telling anyone"
    )


def test_enable_wal_really_sets_the_timeout_first(tmp_path):
    """Anti-vacuity: the helper must genuinely do what the sites now rely on."""
    from llm_router.sqlite_wal import enable_wal

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        assert enable_wal(conn, busy_timeout_ms=1234, label="test") is True
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_the_scan_found_something_to_check():
    """Denominator guard. An empty site list passes every assertion above."""
    found = _sites_with_raw_wal()
    total = sum(len(v) for v in found.values())
    # cost.py and the hook still issue the PRAGMA directly (async / standalone),
    # which is fine — but if the scan finds nothing at all it is broken.
    assert total >= 2, (
        f"the WAL-site scan matched {total} statements across {len(found)} files; "
        f"it is not looking where it thinks it is"
    )
