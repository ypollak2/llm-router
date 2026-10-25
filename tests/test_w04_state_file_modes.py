"""W0-4 / PRD S7 -- state files are 0600 and state directories 0700 (``state_files``).

Evidence: 2026-10-25 audit, 365 of 2,551 top-level files in a live state dir group- or
world-readable (``proxy_calls.jsonl``, ``sessions.db``). These tests run representative
writers in a temp ``LLM_ROUTER_HOME`` under a permissive umask and scan every path created.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sqlite3
import stat
import sys
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


def test_harden_state_counts_only_and_never_leaves_root(home, tmp_path):
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
    counts = state_files.harden_state()
    assert counts and counts["files_fixed"] >= 1 and counts["dirs_fixed"] >= 1
    assert counts["symlinks_skipped"] == 2
    assert set(counts) == {"files_scanned", "files_fixed", "dirs_scanned", "dirs_fixed", "symlinks_skipped"}
    assert stat.S_IMODE(wide.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "sub").stat().st_mode) == 0o700
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644, "chmod followed a symlink out of the root"


def test_harden_runs_on_every_call_not_once(home):
    """W0-4b: a file created 0644 AFTER a first run is repaired by the next run."""
    home.mkdir(mode=0o700)
    state_files.harden_state()
    late = home / "enforcement.log"
    late.write_text("x\n")
    late.chmod(0o644)
    counts = state_files.harden_state()
    assert counts["files_fixed"] == 1
    assert stat.S_IMODE(late.stat().st_mode) == 0o600
    assert not (home / ".modes_migrated_v1").exists(), "the once-only marker is gone"


def test_harden_refuses_symlinked_root_and_absent_root(home, tmp_path):
    assert state_files.harden_state() is None and not home.exists()
    real = tmp_path / "real"
    real.mkdir()
    f = real / "a"
    f.write_text("x")
    f.chmod(0o644)
    home.symlink_to(real)
    assert state_files.harden_state() is None
    assert stat.S_IMODE(f.stat().st_mode) == 0o644


def test_server_main_and_proxy_start_call_harden_state():
    for rel in ("server.py", "proxy/server.py"):
        assert "state_files.harden_state()" in (SRC / rel).read_text(), rel


def test_doctor_runs_hardening_every_time(home):
    home.mkdir(mode=0o755)
    from llm_router.commands import doctor
    for _ in range(2):
        f = home / "x.jsonl"
        f.write_text("a")
        f.chmod(0o644)
        try:
            doctor._run_doctor()
        except BaseException:  # noqa: BLE001 -- only the hardening side effect is under test
            pass
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_read_only_connect_never_creates_anything(home):
    with pytest.raises(sqlite3.OperationalError):
        state_files.connect_sqlite(paths.state_path("deep", "missing.db"), create=False)
    assert not home.exists(), "a read created a directory or file"
    home.mkdir(mode=0o755)
    db = home / "have.db"
    sqlite3.connect(db).close()
    db.chmod(0o644)
    state_files.connect_sqlite(db, create=False).close()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_read_only_aiosqlite_connect_never_creates_anything(home):
    with pytest.raises(sqlite3.OperationalError):
        state_files.connect_aiosqlite(paths.state_path("missing.db"), create=False)
    assert not home.exists()


# ── W0-4b: newly routed writers, umask 000 ────────────────────────────────────
def test_prompt_capture_and_gt_files_private(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GROUND_TRUTH", "1")
    monkeypatch.delenv("LLM_ROUTER_CAPTURE_PATH", raising=False)
    from llm_router import prompt_capture
    prompt_capture._record_outcome("skipped", "w04b-test")
    prompt_capture.capture("what is two plus two", route_id="r1", task_type="query")
    names = _assert_private(home)
    assert "gt_accumulation.jsonl" in names
    assert "prompt_capture.jsonl" in names, "capture wrote nothing; the mode check proved nothing"


def test_ground_truth_pool_files_private(home):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    try:
        from groundtruth import pool
    finally:
        sys.path.pop(0)
    p = pool.Pool()
    p._reject("w04b", {"x": 1})
    p._append(pool.Candidate(task_id="t1", prompt_sha256="x"))
    names = _assert_private(home)
    assert "ground_truth_candidates.jsonl" in names and "ground_truth_candidates.funnel.jsonl" in names


def _load_hook(name: str):
    spec = importlib.util.spec_from_file_location("w04b_" + name.replace("-", "_"), SRC / "hooks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("hook", ["enforce-route", "stop-enforce", "session-start", "session-end", "agent-route", "auto-route"])
def test_hook_helpers_create_private_files(home, hook):
    mod = _load_hook(hook)
    target = home / "hooklogs" / f"{hook}.log"
    target.parent.mkdir(parents=True)
    with mod._priv_open(target, "a", encoding="utf-8") as fh:
        fh.write("x\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if hasattr(mod, "_priv_sqlite"):
        db = home / "hooklogs" / f"{hook}.db"
        c = mod._priv_sqlite(db)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        names = _modes(home)
        c.close()
        assert names[f"hooklogs/{hook}.db"] == 0o600
        assert names[f"hooklogs/{hook}.db-wal"] == 0o600


@pytest.mark.parametrize("hook", ["enforce-route", "stop-enforce"])
def test_enforcement_log_written_by_hook_is_private(home, hook):
    """Drive the real enforcement.log writer of each hook (not just the helper) under umask 000."""
    mod = _load_hook(hook)
    if hook == "enforce-route":
        mod._log_violation("w04b-sess", "Read", "llm_query", outcome="BLOCKED")
    else:
        mod._log_direct_answer("w04b-sess", "llm_query", 1)
    log = mod._log_path()
    assert log.name == "enforcement.log" and log.exists(), "no enforcement.log written"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_aiosqlite_stores_and_sidecars_private(home):
    import asyncio

    async def go():
        conn = state_files.connect_aiosqlite(paths.state_path("ai.db"))
        async with conn as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("CREATE TABLE t(x)")
            await db.execute("INSERT INTO t VALUES (1)")
            await db.commit()
            return _modes(home)

    names = asyncio.run(go())
    assert names["ai.db"] == 0o600 and names["ai.db-wal"] == 0o600 and names["ai.db-shm"] == 0o600


def test_receipt_db_created_private_with_sidecars(home):
    import asyncio
    from llm_router import receipt_store

    r = receipt_store.Receipt(
        contract_id="c", model="m", task_type="query", complexity="simple", gates_passed=["g"],
        gate_count=1, all_passed=True, latency_ms=1.0, input_tokens=1, output_tokens=1,
        cost_usd=0.0, opus_equivalent_cost=0.0, tokens_reclaimed=0, savings_usd=0.0)
    asyncio.run(receipt_store.store_receipt(r))
    names = _assert_private(home)
    assert "receipts.db" in names, "store_receipt created no db; the mode check proved nothing"
    assert asyncio.run(receipt_store.get_session_receipts(0.0)), "round trip failed"


def test_reader_paths_create_nothing(home):
    import asyncio
    from llm_router import receipt_store
    assert asyncio.run(receipt_store.get_session_receipts(0.0)) == []
    assert not home.exists(), "a read created state"


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
# W0-4b: an AST scan, not a regex. The W0-4 regex (`open\([^()\n]*,`) could not see
# `open(paths.state_path("x"), "w")`, `open(str(p), "w")`, `open(p, mode="w")` or
# `os.open(..., O_CREAT)`, so 302 was a lower bound. Flagged call forms:
#   open(P, "w"/"a"/"x"/"+")  and  open(P, mode=...)   (builtin; any positional or keyword mode)
#   X.open("w"/...) / X.open(mode=...)                  (pathlib-style; mode is arg 0)
#   os.open(P, flags-containing-O_CREAT[, mode])        unless mode is a constant with no group/other bits
#   X.write_text(...) / X.write_bytes(...)
#   sqlite3.connect / aiosqlite.connect                 unless ":memory:" or a "file:" URI
# A call with `opener=` is routed (private opener). Files that never mention the state dir
# are not scanned. The top-level `hooks/` directory is scanned too; a file mirrored in
# src/llm_router/hooks is skipped there (test_hook_pair_guard keeps the two byte-identical, so
# counting both would double the number), an unmirrored one is keyed "@hooks/<name>".
# state_files.py and paths.py are the helpers themselves and are not scanned.
_BASELINE = Path(__file__).with_name("w04_raw_write_baseline.json")
_ROOT = Path(__file__).resolve().parents[1]
_STATE_AWARE = ("state_path", ".llm-router", "llm_router_home", "LLM_ROUTER_HOME")
_WRITE_CHARS = set("wax+")


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _mode_writes(call: ast.Call, pos: int) -> bool:
    """True when the call's mode (positional ``pos`` or ``mode=``) is a write mode, or is not
    a constant at all (cannot prove it reads)."""
    node = None
    if len(call.args) > pos:
        node = call.args[pos]
    for kw in call.keywords:
        if kw.arg == "mode":
            node = kw.value
    if node is None:
        return False
    s = _const_str(node)
    return True if s is None else bool(_WRITE_CHARS & set(s))


def _mentions(node, name: str) -> bool:
    return any((isinstance(n, ast.Attribute) and n.attr == name) or (isinstance(n, ast.Name) and n.id == name)
               for n in ast.walk(node))


def _is_raw_write(call: ast.Call) -> bool:
    if any(kw.arg == "opener" for kw in call.keywords):
        return False
    f = call.func
    if isinstance(f, ast.Name) and f.id == "open":
        return _mode_writes(call, 1)
    if not isinstance(f, ast.Attribute):
        return False
    base = f.value.id if isinstance(f.value, ast.Name) else None
    if f.attr == "open":
        if base == "os":
            flags = call.args[1] if len(call.args) > 1 else None
            if flags is None or not _mentions(flags, "O_CREAT"):
                return False
            mode = call.args[2] if len(call.args) > 2 else None
            if isinstance(mode, ast.Constant) and isinstance(mode.value, int) and not mode.value & 0o077:
                return False
            return True
        if base in ("webbrowser", "Image", "zipfile", "tarfile", "state_files"):
            return False
        return _mode_writes(call, 0)
    if f.attr in ("write_text", "write_bytes"):
        return True
    if f.attr == "connect" and (base or "").lstrip("_") in ("sqlite3", "aiosqlite"):
        a0 = _const_str(call.args[0]) if call.args else None
        if a0 is None and call.args and isinstance(call.args[0], ast.JoinedStr):
            first = call.args[0].values[0] if call.args[0].values else None
            a0 = _const_str(first) if first is not None else None
        return not (a0 is not None and (a0 == ":memory:" or a0.startswith("file:")))
    return False


def raw_write_lines(source: str) -> list[int]:
    """Line numbers of unrouted write sites in ``source`` (empty when it cannot be parsed)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return sorted(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_raw_write(n))


def _scan_files():
    for p in sorted(SRC.rglob("*.py")):
        if p.name not in ("state_files.py", "paths.py"):
            yield str(p.relative_to(SRC)), p
    for p in sorted((_ROOT / "hooks").glob("*.py")):
        if not (SRC / "hooks" / p.name).exists():  # a mirrored hook is byte-identical (pair guard)
            yield f"@hooks/{p.name}", p


def _raw_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for key, p in _scan_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        if not any(m in text for m in _STATE_AWARE):
            continue  # cannot be writing under the state dir
        n = len(raw_write_lines(text))
        if n:
            out[key] = n
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


_FORMS = {
    "open(call-arg, 'w')": 'open(paths.state_path("x"), "w")',
    "open(str(p), 'w')": 'open(str(p), "w")',
    "open(p, mode='w')": 'open(p, mode="w")',
    "os.open O_CREAT": "os.open(p, os.O_WRONLY | os.O_CREAT)",
    "Path.open('a')": 'p.open("a")',
    "write_text": 'p.write_text("x")',
    "sqlite3.connect": "sqlite3.connect(str(p))",
    "aiosqlite.connect": "aiosqlite.connect(p)",
}


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_ratchet_scanner_catches_form(form):
    assert raw_write_lines(f"import os\nx = {_FORMS[form]}\n") == [2], form


@pytest.mark.parametrize("src", [
    'open(p)', 'open(p, "r")', 'open(p, "w", opener=po)', 'os.open(p, os.O_CREAT, 0o600)',
    'os.open(p, os.O_RDONLY)', 'sqlite3.connect(":memory:")', 'sqlite3.connect("file:x?mode=ro", uri=True)',
    'state_files.open_private(p, "w")', 'state_files.connect_sqlite(p)', 'p.open("r")',
])
def test_ratchet_scanner_ignores_routed_or_read(src):
    assert raw_write_lines(f"import os\nx = {src}\n") == [], src


_scan_files_real = _scan_files


def test_ratchet_fails_on_uncommitted_scratch_module(tmp_path, monkeypatch):
    """The ratchet must reject a new unrouted writer written in any of the formerly-blind forms."""
    scratch = tmp_path / "scratch_writer.py"
    scratch.write_text(
        "import os\nfrom llm_router import paths\n"
        "def f(p):\n"
        "    open(paths.state_path('x'), 'w')\n"
        "    open(str(p), 'w')\n"
        "    open(p, mode='w')\n"
        "    os.open(p, os.O_WRONLY | os.O_CREAT)\n"
        "    p.open('a')\n"
        "    p.write_text('x')\n")
    monkeypatch.setattr(sys.modules[__name__], "_scan_files",
                        lambda: list(_scan_files_real()) + [("scratch_writer.py", scratch)])
    with pytest.raises(AssertionError, match="new raw state writes"):
        test_raw_state_writes_ratchet()


def test_ratchet_scan_is_not_empty_and_covers_hooks():
    files = [k for k, _ in _scan_files()]
    assert len(files) > 100 and "hooks/enforce-route.py" in files, "src tree not scanned"
    assert list((_ROOT / "hooks").glob("*.py")), "top-level hooks/ missing"
    assert all(not k.startswith("@hooks/") or not (SRC / "hooks" / k[7:]).exists() for k in files)
    assert _raw_counts(), "scan found nothing"


if __name__ == "__main__":  # pragma: no cover
    if "--write" in sys.argv:
        _BASELINE.write_text(json.dumps(_raw_counts(), indent=1, sort_keys=True) + "\n")
