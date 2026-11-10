"""PR-1 of PLAN-LOCAL-QUEUE: the cross-process local gate. Real subprocesses with barriers."""
from __future__ import annotations

import asyncio
import errno
import gc
import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from llm_router.toolkit import local_gate as g
from llm_router.toolkit.adapters.ollama import LocalModelConflict

URL = "http://localhost:11999"  # never contacted: the gate only locks files
SRC = str(Path(g.__file__).resolve().parents[2])

HOLDER = textwrap.dedent("""
    import os, sys, time
    from llm_router.toolkit import local_gate as g
    url, mode, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])
    fn = g.admit_exclusive if mode == "exclusive" else g.admit
    a = fn(url, time.monotonic() + wait)
    print("READY", list(a.slots), flush=True)
    sys.stdin.read()          # barrier: hold until the parent closes stdin
    a.release()
    print("RELEASED", flush=True)
""")

FORKER = textwrap.dedent("""
    import os, signal, sys, time
    from llm_router.toolkit import local_gate as g
    url, child_acquires = sys.argv[1], sys.argv[2] == "1"
    old = g.admit(url, time.monotonic() + 10)
    pid = os.fork()
    if pid == 0:
        if child_acquires:
            new = g.admit(url, time.monotonic() + 30)   # waits for the parent to die
            old.release()                                # inherited admission: must be a no-op
            print("CHILDHELD", flush=True)
        time.sleep(60)
        os._exit(0)
    print("CHILD", pid, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
""")

PAUSER = textwrap.dedent("""
    import sys, time
    from llm_router.toolkit import local_gate as g
    def hook():
        print("PAUSED", flush=True)
        sys.stdin.readline()
    g._hook_after_validate = hook
    a = g.admit(sys.argv[1], time.monotonic() + 20)
    print("READY", flush=True)
    sys.stdin.read()
    a.release()
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))   # paths.state_path honours it first
    monkeypatch.setenv("LLM_ROUTER_LOCAL_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "1")
    monkeypatch.setenv("PYTHONPATH", SRC)
    g._FS_CACHE.clear()
    monkeypatch.setattr(g, "_fstype_for", lambda p: "apfs")  # tmp may be any fs; fs tests patch their own
    return tmp_path


@pytest.fixture
def procs():
    live, pids = [], []
    yield live, pids
    for p in live:
        if p.poll() is None:
            p.kill()
        p.wait()
        for f in (p.stdin, p.stdout):
            if f:
                f.close()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def spawn(procs, script, *args, env_extra=None):
    e = dict(os.environ, **(env_extra or {}))
    # the monkeypatched fs probe does not cross processes; give children the same answer
    code = "from llm_router.toolkit import local_gate as _g; _g._fstype_for = lambda p: 'apfs'\n" + script
    p = subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=e)
    procs[0].append(p)
    return p


def line(p, timeout=10.0):
    r, _, _ = select.select([p.stdout], [], [], timeout)
    assert r, f"no output within {timeout}s; stderr={p.stderr.read() if p.poll() is not None else ''}"
    return p.stdout.readline().strip()


def hold(procs, url=URL, mode="slot", wait=10, env_extra=None):
    p = spawn(procs, HOLDER, url, mode, str(wait), env_extra=env_extra)
    assert line(p).startswith("READY")
    return p


def drop(p):
    p.stdin.close()
    assert line(p) == "RELEASED"
    p.wait(timeout=5)


def rows(env):
    f = env / "home" / ".llm-router" / "local_gate.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def dl(s):
    return time.monotonic() + s


# ── occupancy ────────────────────────────────────────────────────────────────

@pytest.mark.timing
def test_one_slot_second_holder_times_out_then_waits_and_acquires(env, procs):
    a = hold(procs)
    t = time.monotonic()
    with pytest.raises(g.LocalQueueTimeout) as ei:
        g.admit(URL, dl(0.4))
    assert 0.35 <= time.monotonic() - t < 1.5          # deadline is absolute and honoured
    assert isinstance(ei.value, LocalModelConflict)
    waiter = spawn(procs, HOLDER, URL, "slot", "15")
    time.sleep(0.4)
    assert select.select([waiter.stdout], [], [], 0)[0] == []   # still waiting: slot is held
    drop(a)
    assert line(waiter).startswith("READY")


def test_two_slots_allow_two_holders_third_waits(env, procs, monkeypatch):
    e2 = {"LLM_ROUTER_LOCAL_SLOTS": "2"}
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "2")
    a, b = hold(procs, env_extra=e2), hold(procs, env_extra=e2)
    with pytest.raises(g.LocalQueueTimeout):
        g.admit(URL, dl(0.3))
    drop(a)
    adm = g.admit(URL, dl(2))
    assert adm.slots == (0,) or adm.slots == (1,)
    adm.release()
    drop(b)


def test_sigkill_of_holder_frees_slot(env, procs):
    a = hold(procs)
    with pytest.raises(g.LocalQueueTimeout):
        g.admit(URL, dl(0.2))
    a.kill()
    a.wait()
    g.admit(URL, dl(2)).release()


def test_expired_deadline_still_attempts_once(env):
    g.admit(URL, time.monotonic() - 5).release()


# ── fork lifecycle ───────────────────────────────────────────────────────────

def test_fork_child_does_not_keep_parents_slot(env, procs):
    p = spawn(procs, FORKER, URL, "0")
    kind, pid = line(p).split()
    assert kind == "CHILD"
    procs[1].append(int(pid))
    p.wait(timeout=5)
    os.kill(int(pid), 0)                                 # child still alive
    g.admit(URL, dl(2)).release()                        # ...yet the slot is free


def test_fork_child_new_fds_and_stale_admission_are_safe(env, procs):
    p = spawn(procs, FORKER, URL, "1")
    kind, pid = line(p).split()
    procs[1].append(int(pid))
    assert line_after_death(p, procs) == "CHILDHELD"
    with pytest.raises(g.LocalQueueTimeout):             # the child's NEW admission is still held
        g.admit(URL, dl(0.4))


def line_after_death(p, procs):
    # the child inherited the parent's stdout pipe; read its message
    return line(p, 15)


# ── config / reset ───────────────────────────────────────────────────────────

def test_reset_cannot_change_n_between_validation_and_acquisition(env, procs):
    p = spawn(procs, PAUSER, URL)
    assert line(p) == "PAUSED"                           # admit holds config.lock, validated, slot not yet taken
    with pytest.raises(g.LocalQueueTimeout):
        g.reset(URL, dl(0.4), slots=3)
    assert json.loads((g.gate_dir(URL) / "slots.json").read_text()) == {"slots": 1}
    p.stdin.write("\n")
    p.stdin.flush()
    assert line(p) == "READY"
    with pytest.raises(g.LocalQueueResetRefused):        # holder live
        g.reset(URL, dl(1), slots=3)
    p.stdin.close()
    p.wait(timeout=5)
    assert g.reset(URL, dl(2), slots=3) == 3


def test_reset_takes_every_slot_file_present_not_just_range_n(env, procs):
    g.admit(URL, dl(1)).release()
    d = g.gate_dir(URL)
    holder = spawn(procs, textwrap.dedent("""
        import os, sys, fcntl
        fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX); print("READY", flush=True); sys.stdin.read()
    """), str(d / "slot-7.lock"))
    assert line(holder) == "READY"
    with pytest.raises(g.LocalQueueResetRefused):
        g.reset(URL, dl(1), slots=2)


def test_corrupt_config_with_live_holder_is_left_alone_then_recovered(env, procs):
    a = hold(procs)
    cfg = g.gate_dir(URL) / "slots.json"
    cfg.write_text("{not json")
    with pytest.raises(g.LocalQueueTimeout):
        g.admit(URL, dl(0.4))
    assert cfg.read_text() == "{not json"                # not rewritten under a live holder
    assert sum(r["outcome"] == "config_corrupt_holders" for r in rows(env)) == 1
    drop(a)
    g.admit(URL, dl(2)).release()
    assert json.loads(cfg.read_text()) == {"slots": 1}
    assert any(r["outcome"] == "config_recovered" for r in rows(env))


def test_corrupt_config_recovery_counts_stray_slot_files(env, procs):
    g.admit(URL, dl(1)).release()
    d = g.gate_dir(URL)
    (d / "slots.json").write_text("[]")
    holder = spawn(procs, textwrap.dedent("""
        import os, sys, fcntl
        fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX); print("READY", flush=True); sys.stdin.read()
    """), str(d / "slot-5.lock"))
    assert line(holder) == "READY"
    with pytest.raises(g.LocalQueueTimeout):
        g.admit(URL, dl(0.3))
    assert (d / "slots.json").read_text() == "[]"


def test_slots_conflict_raises_and_writes_row(env, procs, monkeypatch):
    a = hold(procs, env_extra={"LLM_ROUTER_LOCAL_SLOTS": "2"})
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "3")
    with pytest.raises(g.LocalQueueConfigConflict) as ei:
        g.admit(URL, dl(1))
    assert isinstance(ei.value, LocalModelConflict)
    assert [r for r in rows(env) if r["outcome"] == "slots_conflict"][0]["file_slots"] == 2
    drop(a)


def test_config_written_via_temp_fsync_rename(env, monkeypatch):
    seen = {"fsync": 0, "replace": []}
    real_replace, real_fsync = os.replace, os.fsync

    def replace(src, dst):
        seen["replace"].append((os.path.basename(src), json.loads(Path(src).read_text())))
        real_replace(src, dst)

    def fsync(fd):
        seen["fsync"] += 1
        real_fsync(fd)

    monkeypatch.setattr(g.os, "replace", replace)
    monkeypatch.setattr(g.os, "fsync", fsync)
    g.admit(URL, dl(1)).release()
    assert seen["replace"] and seen["replace"][0][1] == {"slots": 1}
    assert ".tmp" in seen["replace"][0][0] and seen["fsync"] >= 1


# ── exclusive ────────────────────────────────────────────────────────────────

def test_exclusive_with_slot_held_elsewhere_releases_everything_and_retries(env, procs, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "2")
    e2 = {"LLM_ROUTER_LOCAL_SLOTS": "2"}
    g.admit(URL, dl(1)).release()                        # create config with N=2
    a = hold(procs, env_extra=e2)                        # takes slot 0
    b = hold(procs, env_extra=e2)                        # takes slot 1
    drop(a)                                              # slot 0 free, slot 1 held
    excl = spawn(procs, HOLDER, URL, "exclusive", "20", env_extra=e2)
    time.sleep(0.3)                                      # exclusive now polls: takes 0, fails on 1, must let go of 0
    end = time.monotonic() + 1.5
    while time.monotonic() < end:                        # a hold-and-wait exclusive would starve every probe
        adm = g.admit(URL, dl(0.15))
        assert adm.slots == (0,)
        adm.release()
        time.sleep(0.05)
    drop(b)
    assert line(excl).startswith("READY [0, 1]")
    excl.stdin.close()


def test_exclusive_blocks_ordinary_admission(env):
    ex = g.admit_exclusive(URL, dl(1))
    with pytest.raises(g.LocalQueueTimeout):
        g.admit(URL, dl(0.2))
    ex.release()
    g.admit(URL, dl(1)).release()


# ── URL aliases ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("other", ["http://127.0.0.1:11999", "http://[::1]:11999", "HTTP://LocalHost:11999/v1/x/"])
def test_loopback_aliases_share_one_gate(env, procs, other):
    a = hold(procs)
    with pytest.raises(g.LocalQueueTimeout):
        g.admit(other, dl(0.25))
    drop(a)


def test_canonical_url_and_distinct_gates(env):
    assert g.canonical_url("http://localhost") == "http://loopback:80"
    assert g.canonical_url("https://127.0.0.1/") == "https://loopback:443"
    assert g.gate_dir("http://localhost:1") != g.gate_dir("http://localhost:2")
    assert g.gate_dir("http://localhost:1") == g.gate_dir("http://[::1]:1/")
    held = g.admit("http://localhost:1", dl(1))
    g.admit("http://localhost:2", dl(1)).release()       # other server unaffected
    held.release()


# ── filesystem check ─────────────────────────────────────────────────────────

def test_fs_allowlist_and_unknown_disables(env, monkeypatch, procs):
    assert g._fs_allowed("apfs") and g._fs_allowed("ext4") and g._fs_allowed("tmpfs")
    assert not g._fs_allowed("nfs") and not g._fs_allowed("") and not g._fs_allowed("smbfs")
    a = hold(procs)
    for bad in ("nfs", ""):
        g._FS_CACHE.clear()
        monkeypatch.setattr(g, "_fstype_for", lambda p, bad=bad: bad)
        adm = g.admit(URL, dl(0.2))                      # holder is live; a working gate would time out
        assert adm.disabled
    assert any(r["outcome"] == "gate_disabled_fs" for r in rows(env))
    drop(a)


def test_linux_mount_table_longest_mount_wins():
    mounts = "rootfs / ext4 rw 0 0\nsrv:/x /home nfs rw 0 0\n/dev/y /home/me/fast xfs rw 0 0\n"
    assert g._linux_fstype("/home/me/fast/a", mounts) == "xfs"
    assert g._linux_fstype("/home/other", mounts) == "nfs"
    assert g._linux_fstype("/etc", mounts) == "ext4"
    assert g._linux_fstype("/home/me/fastx", mounts) == "nfs"


def test_real_fstype_probe_returns_a_string(tmp_path):
    assert isinstance(g._fstype_for(str(tmp_path)), str)


# ── async ────────────────────────────────────────────────────────────────────

def test_async_acquire_does_not_block_the_loop(env, procs):
    hold(procs)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.ensure_future(ticker())
        with pytest.raises(g.LocalQueueTimeout):
            await g.aadmit(URL, dl(0.5))
        n_wait = ticks
        t.cancel()
        return n_wait

    assert asyncio.run(main()) >= 20                     # ~50 expected; a blocking sleep gives ~0


def test_async_acquires_after_release_and_exclusive(env, procs):
    a = hold(procs)

    async def main():
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, lambda: a.stdin.close())
        adm = await g.aadmit(URL, dl(5))
        adm.release()
        ex = await g.aadmit_exclusive(URL, dl(2))
        ex.release()

    asyncio.run(main())


# ── kill switch / fail open ──────────────────────────────────────────────────

@pytest.mark.timing
@pytest.mark.parametrize("val", [None, "off", "0", ""])
def test_kill_switch_default_off_is_a_noop_without_row(env, procs, monkeypatch, val):
    a = hold(procs)                                      # children run with the queue on
    gate_root = env / "gate"
    before = sorted(p.name for p in (env / "home").rglob("*"))
    if val is None:
        monkeypatch.delenv("LLM_ROUTER_LOCAL_QUEUE")
    else:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", val)
    rows_before = len(rows(env))
    t = time.monotonic()
    adm = g.admit(URL, dl(5))
    assert adm.disabled and time.monotonic() - t < 0.5   # on, this would wait for the holder
    adm.release()
    assert len(rows(env)) == rows_before                 # no row written by the off path
    assert sorted(p.name for p in (env / "home").rglob("*")) == before
    drop(a)
    # a fresh process that never had the queue on touches nothing at all
    fresh = env / "fresh"
    (fresh / "home").mkdir(parents=True)
    e = {"HOME": str(fresh / "home"), "LLM_ROUTER_LOCAL_GATE_DIR": str(fresh / "gate"), "PYTHONPATH": SRC,
         "PATH": os.environ.get("PATH", "")}
    code = ("import time; from llm_router.toolkit import local_gate as g; "
            "a = g.admit('%s', time.monotonic() + 1); assert a.disabled; a.release()" % URL)
    subprocess.run([sys.executable, "-c", code], env=e, check=True, timeout=30)
    assert not (fresh / "gate").exists() and not list((fresh / "home").rglob("*"))
    assert gate_root.exists()                            # only the earlier queue-on holder created it


def test_unwritable_gate_dir_fails_open_with_row(env, monkeypatch):
    blocker = env / "afile"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_GATE_DIR", str(blocker))
    assert g.admit(URL, dl(1)).disabled
    assert rows(env)[-1]["outcome"] == "gate_dir_unwritable"


def test_readonly_existing_gate_dir_fails_open_with_row(env):
    d = g.gate_dir(URL)
    d.mkdir(parents=True)
    d.chmod(0o500)
    try:
        adm = g.admit(URL, dl(1))
        assert adm.disabled and adm.slots == ()
        assert rows(env)[-1]["outcome"] == "gate_dir_unwritable"
    finally:
        d.chmod(0o700)


def test_unexpected_lock_error_fails_open(env, monkeypatch):
    def boom(fd):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(g, "_try_lock", boom)
    assert g.admit(URL, dl(1)).disabled
    assert rows(env)[-1]["outcome"] == "lock_error"


def test_rows_hold_counts_only(env):
    g.admit(URL, dl(1), caller="classifier").release()
    r = rows(env)[-1]
    assert r["outcome"] == "released" and r["caller"] == "classifier" and "held_ms" in r
    assert set(r) <= {"ts", "url_key", "outcome", "kind", "caller", "wait_ms", "held_ms"}


# ── Windows path via a fake msvcrt ───────────────────────────────────────────

def test_windows_msvcrt_path(monkeypatch, tmp_path):
    class Fake:
        LK_NBLCK, LK_UNLCK = 2, 0
        locked: set = set()
        pos: list = []

        @classmethod
        def locking(cls, fd, mode, n):
            cls.pos.append(os.lseek(fd, 0, os.SEEK_CUR))
            key = os.fstat(fd).st_ino
            if mode == cls.LK_NBLCK:
                if key in cls.locked:
                    raise OSError(errno.EACCES, "locked")
                cls.locked.add(key)
            else:
                cls.locked.discard(key)

    monkeypatch.setitem(sys.modules, "msvcrt", Fake)
    monkeypatch.setattr(g, "_USE_MSVCRT", True)
    p = tmp_path / "f"
    fd1, fd2 = os.open(p, os.O_RDWR | os.O_CREAT), os.open(p, os.O_RDWR)
    os.lseek(fd1, 5, os.SEEK_SET)
    assert g._try_lock(fd1) is True
    assert g._try_lock(fd2) is False                     # EACCES = contention
    g._unlock(fd1)
    assert g._try_lock(fd2) is True
    assert set(Fake.pos) == {0}                          # always seek(0) first
    os.close(fd1)
    os.close(fd2)


# ── review fixes: GC fallback, re-entrancy guard, reset validation ───────────

@pytest.mark.timing
def test_dropped_admission_frees_its_slot_via_gc(env):
    a = g.admit(URL, dl(2))
    assert not a.disabled
    del a                                                # no release(), no with
    gc.collect()
    t = time.monotonic()
    b = g.admit(URL, dl(2))
    assert not b.disabled and time.monotonic() - t < 1.0
    b.release()


def test_dropped_admission_under_held_lifecycle_lock_is_deferred_then_freed(env):
    a = g.admit(URL, dl(2))
    with g._LIFECYCLE:                                   # GC inside a locked region must not deadlock
        del a
        gc.collect()
    assert g._PENDING_CLOSE
    b = g.admit(URL, dl(2))                              # next attempt drains the deferred fd
    assert not b.disabled and not g._PENDING_CLOSE
    b.release()


def test_reset_drains_gc_deferred_fd_instead_of_refusing(env):
    a = g.admit(URL, dl(2))
    with g._LIFECYCLE:
        del a
        gc.collect()
    assert g._PENDING_CLOSE
    assert g.reset(URL, dl(2), slots=1) == 1             # deferred fd is not a live holder
    assert not g._PENDING_CLOSE


def test_fork_child_forgets_parent_deferred_fds(env):
    a = g.admit(URL, dl(2))
    with g._LIFECYCLE:
        del a
        gc.collect()
    assert g._PENDING_CLOSE
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.write(w, b"1" if not g._PENDING_CLOSE else b"0")
        os._exit(0)
    os.close(w)
    got = os.read(r, 1)
    os.close(r)
    os.waitpid(pid, 0)
    assert got == b"1"
    g.admit(URL, dl(2)).release()                        # parent still drains its own deferred fd
    assert not g._PENDING_CLOSE


def test_release_detaches_finalizer_so_reused_fd_is_not_closed(env):
    a = g.admit(URL, dl(2))
    a.release()
    b = g.admit(URL, dl(2))                              # likely reuses a's fd number
    fd = b._fds[0]
    del a
    gc.collect()
    os.fstat(fd)                                         # still open
    b.release()


@pytest.mark.timing
def test_admit_exclusive_while_holding_a_slot_raises_immediately(env):
    a = g.admit(URL, dl(2))
    t = time.monotonic()
    with pytest.raises(g.LocalQueueReentrant) as ei:
        g.admit_exclusive(URL, dl(30))
    assert time.monotonic() - t < 1.0
    assert isinstance(ei.value, g._LocalQueueError) and isinstance(ei.value, LocalModelConflict)
    with pytest.raises(g.LocalQueueReentrant):
        asyncio.run(g.aadmit_exclusive(URL, dl(30)))
    a.release()
    g.admit_exclusive(URL, dl(2)).release()              # fine once released


def test_admit_exclusive_reentrancy_ignores_other_servers(env):
    a = g.admit(URL, dl(2))
    g.admit_exclusive("http://localhost:11998", dl(2)).release()
    a.release()


@pytest.mark.parametrize("bad", [0, -1, -5])
def test_reset_rejects_slots_below_one(env, bad):
    with pytest.raises(ValueError):
        g.reset(URL, dl(1), slots=bad)
    assert not (g.gate_dir(URL) / "slots.json").exists()


@pytest.mark.timing
def test_other_thread_holding_makes_exclusive_wait_not_raise(env):
    held, go = threading.Event(), threading.Event()

    def holder():
        a = g.admit(URL, dl(5))
        held.set()
        go.wait(10)
        a.release()

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(5)
    threading.Timer(0.4, go.set).start()
    t = time.monotonic()
    ex = g.admit_exclusive(URL, dl(10))                  # must wait for the other thread, not raise
    assert not ex.disabled and time.monotonic() - t >= 0.3
    ex.release()
    th.join(5)


@pytest.mark.timing
def test_other_task_holding_makes_exclusive_wait_but_child_task_raises(env):
    async def main():
        held, go = asyncio.Event(), asyncio.Event()

        async def holder():
            a = await g.aadmit(URL, dl(5))
            held.set()
            await go.wait()
            a.release()

        async def spawned_by_holder():
            a = await g.aadmit(URL, dl(5))
            try:
                with pytest.raises(g.LocalQueueReentrant):
                    await asyncio.create_task(g.aadmit_exclusive(URL, dl(30)))
            finally:
                a.release()

        h = asyncio.create_task(holder())
        await held.wait()
        asyncio.get_running_loop().call_later(0.4, go.set)
        t = time.monotonic()
        ex = await g.aadmit_exclusive(URL, dl(10))       # sibling task: waits, does not raise
        assert not ex.disabled and time.monotonic() - t >= 0.3
        ex.release()
        await h
        await spawned_by_holder()

    asyncio.run(main())
