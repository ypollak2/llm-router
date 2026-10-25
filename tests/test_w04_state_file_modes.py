"""W0-4 / PRD S7 -- state files are 0600 and state directories 0700 (``state_files``).

Evidence: 2026-10-25 audit, 365 of 2,551 top-level files in a live state dir group- or
world-readable (``proxy_calls.jsonl``, ``sessions.db``). These tests run representative
writers in a temp ``LLM_ROUTER_HOME`` under a permissive umask and scan every path created.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from llm_router import capped_log, paths, state_files
from llm_router.file_lock import exclusive_lock

SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "statehome"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    old = os.umask(0o000)  # worst case: the umask widens everything the code does not pin
    yield h
    os.umask(old)


def _modes(root: Path) -> dict[str, int]:
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = Path(dp) / n
            if not p.is_symlink():
                out[str(p.relative_to(root))] = stat.S_IMODE(p.stat().st_mode)
    return out


def _assert_private(root: Path) -> dict[str, int]:
    modes = _modes(root)
    assert modes, "scan found nothing to check"  # an empty set passes everything
    bad = {k: oct(m) for k, m in modes.items() if m != (0o700 if (root / k).is_dir() else 0o600)}
    assert not bad, bad
    return modes


def test_writers_create_private_files_and_dirs(home):
    capped_log.append(paths.state_path("sub", "a.jsonl"), b'{"x":1}\n', max_bytes=10)  # rotates
    capped_log.append(paths.state_path("sub", "a.jsonl"), b'{"x":2}\n', max_bytes=10)
    with exclusive_lock(paths.state_path("lk", "thing.lock")):
        pass
    from llm_router.proxy import ledger
    ledger.write_row({"decision": "served"}, path=paths.state_path("deep", "er", "proxy_calls.jsonl"))
    with state_files.open_private(paths.state_path("o", "x.json"), "w") as fh:
        fh.write("{}")
    from llm_router import session_store
    session_store.record_event("w04-sess", "prompt", "hello")
    conn = state_files.connect_sqlite(paths.state_path("db", "t.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()  # -wal and -shm exist while the connection is open
    names = _assert_private(home)
    conn.close()
    assert any(k.endswith("t.db-wal") for k in names) and any(k.endswith("t.db-shm") for k in names)
    assert any(k.endswith("a.jsonl.1") for k in names), "rotated generation not covered"
    assert any(k.endswith(".lock") for k in names)
    _assert_private(home)


def test_agent_session_store_db_and_sidecars_private(home):
    from llm_router.agents.session import SessionStore
    store = SessionStore()
    store._conn.execute("CREATE TABLE IF NOT EXISTS w04(x)")
    store._conn.execute("INSERT INTO w04 VALUES (1)")
    store._conn.commit()
    names = _assert_private(home)
    assert "sessions.db" in names
    store._conn.close()


def test_existing_wide_file_is_tightened_on_open(home):
    home.mkdir(mode=0o755)
    p = home / "old.jsonl"
    p.write_text("x\n")
    p.chmod(0o644)
    with state_files.open_private(p, "a") as fh:
        fh.write("y\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_in_memory_sqlite_creates_no_file(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_files.connect_sqlite(":memory:").close()
    assert not (tmp_path / ":memory:").exists()


def test_migration_once_counts_only_and_never_leaves_root(home, tmp_path):
    home.mkdir(mode=0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "keep.txt"
    secret.write_text("x")
    secret.chmod(0o644)
    (home / "sub").mkdir(mode=0o755)
    wide = home / "sub" / "proxy_calls.jsonl"
    wide.write_text("{}\n")
    wide.chmod(0o666)
    (home / "link").symlink_to(secret)            # symlink to a file outside
    (home / "dirlink").symlink_to(outside)        # symlink to a dir outside
    counts = state_files.migrate_once()
    assert counts and counts["files_fixed"] >= 1 and counts["dirs_fixed"] >= 1
    assert counts["symlinks_skipped"] == 2
    assert stat.S_IMODE(wide.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "sub").stat().st_mode) == 0o700
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644, "chmod followed a symlink out of the root"
    marker = home / state_files.MARKER_NAME
    assert set(json.loads(marker.read_text())) <= {
        "files_scanned", "files_fixed", "dirs_scanned", "dirs_fixed", "symlinks_skipped", "ts"}
    wide.chmod(0o666)
    assert state_files.migrate_once() is None, "migration ran twice"
    assert stat.S_IMODE(wide.stat().st_mode) == 0o666


def test_migration_noop_when_state_dir_absent(home):
    assert state_files.migrate_once() is None
    assert not home.exists()


def test_doctor_runs_migration(home, capsys):
    home.mkdir(mode=0o755)
    f = home / "x.jsonl"
    f.write_text("a")
    f.chmod(0o644)
    from llm_router.commands import doctor
    try:
        doctor._run_doctor()
    except BaseException:  # noqa: BLE001 -- only the migration side effect is under test
        pass
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


# ── SEC-S2: no state file holds a value from the test .env ────────────────────
def test_env_secret_values_never_land_in_state_files(home, tmp_path, monkeypatch):
    secret = "sk-w04-" + "Z9q" * 8
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={secret}\n")
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    from llm_router import failopen, session_store
    from llm_router.proxy import ledger
    ledger.write_row({"decision": "served", "provider": "openai"}, path=paths.state_path("proxy_calls.jsonl"))
    capped_log.append(paths.state_path("hook_latency.jsonl"), b'{"ms":1}\n', 1000)
    session_store.record_event("w04-s2", "prompt", f"use my key {secret} please")
    failopen.record("W04-S2", RuntimeError(f"auth failed for {secret}"))
    hits = [str(p) for p in home.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    assert not hits, f"env secret found in state files: {[Path(h).name for h in hits]}"
    assert any(p.is_file() for p in home.rglob("*")), "scan found no state files"


# ── Source-scan ratchet: raw state writes may only go down ────────────────────
_RAW = re.compile(r"""\bopen\([^()\n]*,\s*["'](?:w|a|wb|ab|w\+|a\+)["']|\.open\(\s*["'](?:w|a|wb|ab)["']|\.write_text\(|\.write_bytes\(|sqlite3\.connect\((?!\s*f?["']file:)""")
_BASELINE = Path(__file__).with_name("w04_raw_write_baseline.json")


def _raw_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(SRC.rglob("*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "state_path" not in text and ".llm-router" not in text and "llm_router_home" not in text:
            continue  # cannot be writing under the state dir
        n = sum(1 for ln in text.splitlines()
                if _RAW.search(ln) and "opener=" not in ln and "state_files." not in ln
                and not ln.lstrip().startswith("#"))
        if n:
            out[str(p.relative_to(SRC))] = n
    return out


def test_raw_state_writes_ratchet():
    """A writer in a file that knows the state dir must use ``state_files`` (or ``opener=``).
    The baseline is the legacy count per file: it may only go DOWN. A new file, or a higher
    count, fails; migrate the writer to ``state_files`` instead of raising the number.
    Regenerate after lowering: ``python tests/test_w04_state_file_modes.py --write``."""
    base = json.loads(_BASELINE.read_text())
    assert base, "baseline empty -- the ratchet would pass everything"
    cur = _raw_counts()
    worse = {f: (base.get(f, 0), n) for f, n in cur.items() if n > base.get(f, 0)}
    assert not worse, f"new raw state writes (baseline, now): {worse}"
    stale = {f: (b, cur.get(f, 0)) for f, b in base.items() if cur.get(f, 0) < b}
    assert not stale, f"lower the baseline (baseline, now): {stale}"


if __name__ == "__main__":  # pragma: no cover
    import sys
    if "--write" in sys.argv:
        _BASELINE.write_text(json.dumps(_raw_counts(), indent=1, sort_keys=True) + "\n")
