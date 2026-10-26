"""MAP-FIX: writers with different environments must not flap the shared map.

Measured on a real host (map_changes.jsonl, 2026-10-13..25): 623 codex and 622
claude status events, 58/57 OPENROUTER added/removed. The launchd proxy (PATH
/usr/bin:/bin:/usr/sbin:/sbin) wrote "claude CLI not found"; MCP servers and
hook children (full PATH) wrote "connected"; a fresh process missed the key
that lives only in ``$LLM_ROUTER_HOME/openrouter.key``. No real CLI runs here:
every binary is a fake in tmp_path.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from llm_router import config, map_rediscovery, resource_map, seats
from llm_router.discovery import keys
from llm_router.discovery.keys import KeyInfo

MINIMAL_PATH = "/usr/bin:/bin"


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _no_http(*_a, **_k):
    raise OSError("no network in tests")


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setattr(seats, "FALLBACK_DIRS", ())   # pass only via the learned file
    monkeypatch.setattr(seats, "LEARN_NAMES", (*seats.LEARN_NAMES, "fakenode"))
    monkeypatch.setattr(seats, "INTERPRETERS", ("fakenode",))
    return home


@pytest.fixture
def fake_clis(tmp_path):
    """claude (native-like sh script) and codex as `#!/usr/bin/env fakenode`."""
    bin_dir, node_dir = tmp_path / "bin", tmp_path / "nodebin"
    record = tmp_path / "probe_path.txt"
    _exe(bin_dir / "claude",
         '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}\'\n')
    _exe(bin_dir / "codex", "#!/usr/bin/env fakenode\n")
    _exe(node_dir / "fakenode", f'#!/bin/sh\necho "$PATH" > "{record}"\necho "Logged in using ChatGPT"\n')
    return bin_dir, node_dir, record


def _learn_from_full_path(bin_dir: Path, node_dir: Path) -> None:
    full = {"PATH": f"{bin_dir}{os.pathsep}{node_dir}{os.pathsep}{MINIMAL_PATH}"}
    for name in ("claude", "codex", "fakenode"):
        assert seats.find_cli(name, env=full)


def _detect(tmp_path) -> seats.Seats:
    return seats.detect_seats(home=tmp_path, env={}, opener=_no_http)


def test_minimal_path_finds_learned_clis_and_gives_node_to_the_probe(state, fake_clis, tmp_path, monkeypatch):
    bin_dir, node_dir, record = fake_clis
    _learn_from_full_path(bin_dir, node_dir)
    monkeypatch.setenv("PATH", MINIMAL_PATH)
    assert shutil.which("claude") is None and shutil.which("codex") is None   # non-vacuous

    found = _detect(tmp_path)

    assert found.claude.status == "connected" and found.claude.kind == "claude.ai"
    assert found.codex.status == "connected" and found.codex.kind == "chatgpt"
    assert str(node_dir) in record.read_text().strip().split(os.pathsep)


def test_without_the_learned_file_minimal_path_says_not_found(state, fake_clis, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", MINIMAL_PATH)
    found = _detect(tmp_path)
    assert found.claude.status == "absent" and found.claude.reason == "claude CLI not found"


def test_learned_file_is_private(state, fake_clis):
    _learn_from_full_path(*fake_clis[:2])
    path = state / seats.CLI_PATH_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert str(fake_clis[0]) in data["dirs"] and "claude" in data["dirs"][str(fake_clis[0])]["names"]


def _inputs(found: seats.Seats, tmp_path, key_rows: list) -> resource_map.MapInputs:
    return resource_map.MapInputs(
        seats=found, env={}, http_get=lambda _u: None, keys=lambda: key_rows,
        policy="balanced", codex_sessions=tmp_path / "no-sessions",
        claude_reading=lambda: (None, "absent", None), codex_counter=lambda: None,
        gemini_quota=lambda: None, competence={},
    )


def _poll(found, tmp_path, now, key_rows):
    return map_rediscovery.poll(inputs=_inputs(found, tmp_path, key_rows), now=now,
                                paths=[], map_path=tmp_path / "state" / "resource_map.json",
                                state_dir=tmp_path / "state", force=True)


OR_KEY = [KeyInfo("OPENROUTER_API_KEY", "openrouter", "env", ("llm-router",), "unknown")]


def _changes(state: Path) -> list[dict]:
    return [json.loads(x) for x in (state / map_rediscovery.CHANGES_FILE).read_text().splitlines()]


def test_two_writers_with_different_paths_do_not_flap(state, fake_clis, tmp_path, monkeypatch):
    bin_dir, node_dir, _ = fake_clis
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{node_dir}{os.pathsep}{MINIMAL_PATH}")
    t0 = time.time()
    _poll(_detect(tmp_path), tmp_path, t0, [])                 # full-PATH writer
    monkeypatch.setenv("PATH", MINIMAL_PATH)                   # launchd-shaped writer
    _poll(_detect(tmp_path), tmp_path, t0 + 60, OR_KEY)        # the key add forces a line

    lines = _changes(state)
    assert lines, "no change line written: the writer check below would be vacuous"
    assert not [e for e in lines if e["event"] == "status" and e["resource"] in ("claude", "codex")]
    assert all(set(e["writer"]) == {"pid", "entry", "path_sig"} for e in lines)
    data = resource_map.load_map(state / "resource_map.json")
    assert data["writer"]["pid"] == os.getpid()


def test_not_found_does_not_overwrite_a_recent_connected_but_age_caps_it(state, tmp_path):
    seen = seats.Seats(claude=seats.Seat(kind="claude.ai", plan="max"), codex=seats.Seat(kind="chatgpt"))
    blind = seats.Seats(claude=seats.Seat(reason="claude CLI not found"),
                        codex=seats.Seat(reason="codex CLI not found"))
    t0 = time.time() - 3 * 3600
    _poll(seen, tmp_path, t0, [])
    _poll(blind, tmp_path, t0 + 60, OR_KEY)
    rows = {r["resource"]: r for r in resource_map.load_map(state / "resource_map.json")["resources"]}
    assert rows["claude"]["status"] == "connected"
    assert rows["claude"]["carried_from"]["new_reason"] == "claude CLI not found"
    assert not [e for e in _changes(state) if e["event"] == "status"]

    _poll(blind, tmp_path, t0 + 2 * 3600, [])                  # past CARRY_MAX_AGE_S
    status = {e["resource"]: e["after"] for e in _changes(state) if e["event"] == "status"}
    assert status == {"claude": "absent", "codex": "absent"}


def test_real_logout_is_not_carried(state, tmp_path):
    seen = seats.Seats(claude=seats.Seat(kind="claude.ai"))
    out = seats.Seats(claude=seats.Seat(status="installed_not_connected", reason="Not logged in"))
    t0 = time.time()
    _poll(seen, tmp_path, t0, [])
    _poll(out, tmp_path, t0 + 60, [])
    assert [e["after"] for e in _changes(state) if e["resource"] == "claude"] == ["installed_not_connected"]


def test_disk_key_file_is_discovered_and_build_is_the_same_before_and_after_load(state, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_state_dir", lambda: state)   # conftest points it elsewhere
    (state / "openrouter.key").write_text("sk-test-not-real\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    monkeypatch.delenv("OPENROUTER_API_KEY")                   # undo restores "absent"
    monkeypatch.setattr(keys, "keychain_has", lambda _v: False)
    monkeypatch.chdir(tmp_path)

    found = {k.var: k for k in keys.discover(env={}, dotenv_paths=[], keychain=lambda _v: False)}
    assert found["OPENROUTER_API_KEY"].source == f"keyfile:{state / 'openrouter.key'}"

    def key_rows():
        inp = _inputs(seats.Seats(), tmp_path, [])
        inp.keys, inp.env = None, dict(os.environ)
        return [r for r in resource_map.build(inp)["resources"] if r["resource"].startswith("api/")]

    before = key_rows()
    assert "OPENROUTER_API_KEY" in config.load_disk_keys()
    assert os.environ["OPENROUTER_API_KEY"] == "sk-test-not-real"
    assert key_rows() == before
    assert any(r["resource"] == "api/OPENROUTER_API_KEY" for r in before)
    assert all("sk-test" not in json.dumps(r) for r in before)


@pytest.mark.parametrize("exc, words", [
    (subprocess.TimeoutExpired(["claude"], 3.0), "timed out"),
    (OSError("exec format error"), "failed to launch"),
])
def test_probe_failure_has_its_own_reason(exc, words, tmp_path):
    def runner(_argv, _timeout):
        raise exc
    found = seats.detect_seats(runner=runner, which=lambda _n: None, env={}, home=tmp_path,
                               opener=_no_http)
    for seat in (found.claude, found.codex):
        assert "CLI not found" not in seat.reason and words in seat.reason
        assert seat.status == "installed_not_connected"
        assert seats.no_evidence(seat.status, seat.reason)


def test_learned_dir_expires_after_its_binary_is_removed(state, tmp_path):
    bin_dir = tmp_path / "gone"
    _exe(bin_dir / "claude", "#!/bin/sh\n")
    t0 = time.time()
    assert seats.find_cli("claude", env={"PATH": str(bin_dir)}, now=t0)
    (bin_dir / "claude").unlink()

    assert str(bin_dir) in seats.learned_dirs(t0 + 100)        # non-vacuous
    late = t0 + seats.LEARNED_MAX_AGE_S + 1
    assert str(bin_dir) not in seats.learned_dirs(late)
    assert str(bin_dir) not in seats.search_path({"PATH": MINIMAL_PATH}, late).split(os.pathsep)
    assert seats.find_cli("claude", env={"PATH": MINIMAL_PATH}, now=late) is None


def test_carry_recent_for_the_rescan_path():
    now = time.time()
    iso = lambda t: seats.datetime.fromtimestamp(t, tz=seats.timezone.utc).isoformat()  # noqa: E731
    prior = seats.Seats(claude=seats.Seat(kind="claude.ai"), detected_at=iso(now - 600))
    blind = seats.Seats(claude=seats.Seat(reason="claude CLI not found"), detected_at=iso(now))
    assert seats.carry_recent(blind, prior, now).claude.status == "connected"
    old = seats.Seats(claude=seats.Seat(kind="claude.ai"), detected_at=iso(now - 7200))
    assert seats.carry_recent(blind, old, now) is blind


def test_repeated_carries_keep_the_first_sighting_as_anchor(state, tmp_path):
    """MAP-FIX-2: the 1 h window is anchored at the FIRST sighting. Blind polls 40 min
    apart must not each restart it: the seat is still carried before t0+1h and is
    reported absent at t0+80 min although the previous carry (t0+40 min) is <1 h old."""
    seen = seats.Seats(claude=seats.Seat(kind="claude.ai", plan="max"))
    blind = seats.Seats(claude=seats.Seat(reason="claude CLI not found"))
    t0 = time.time() - 6 * 3600
    mp = state / "resource_map.json"

    def claude_row():
        return {r["resource"]: r for r in resource_map.load_map(mp)["resources"]}["claude"]

    _poll(seen, tmp_path, t0, [])
    _poll(blind, tmp_path, t0 + 40 * 60, OR_KEY)               # carry 1, 40 min after sighting
    first = claude_row()
    assert first["status"] == "connected"
    anchor = first["carried_from"]["seen_at"]

    _poll(blind, tmp_path, t0 + 59 * 60, [])                   # carry 2, still inside the window
    second = claude_row()
    assert second["status"] == "connected"
    assert second["carried_from"]["seen_at"] == anchor, "carry moved the anchor"
    assert not [e for e in _changes(state) if e["event"] == "status"]

    _poll(blind, tmp_path, t0 + 80 * 60, OR_KEY)               # 80 min after sighting, 21 min after carry 2
    assert claude_row()["status"] == "absent"
    status = [(e["resource"], e["after"]) for e in _changes(state) if e["event"] == "status"]
    assert status == [("claude", "absent")]


def test_concurrent_learners_both_survive(state, tmp_path, monkeypatch):
    """MAP-FIX-2b N1: two processes teaching different dirs at once keep both entries.
    Both are forced to read cli_path.json before either writes (a lost update on an
    unlocked read-modify-write); under the lock the second read waits for the first write."""
    import threading
    gate = threading.Barrier(2, timeout=1.0)
    real_read = seats._read_learned
    local = threading.local()

    def racing_read():
        data = real_read()
        if not getattr(local, "done", False):      # only the first read per thread races
            local.done = True
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                pass
        return data

    monkeypatch.setattr(seats, "_read_learned", racing_read)
    a, b = tmp_path / "a" / "claude", tmp_path / "b" / "codex"
    for exe in (a, b):
        _exe(exe, "#!/bin/sh\n")
    now = time.time()
    ts = [threading.Thread(target=seats._learn, args=(n, str(p), now))
          for n, p in (("claude", a), ("codex", b))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    monkeypatch.setattr(seats, "_read_learned", real_read)
    assert set(seats.learned_dirs(now)) == {str(a.parent), str(b.parent)}


def test_relative_path_entry_is_not_recorded(state, tmp_path, monkeypatch):
    """MAP-FIX-2b N2: a hit through a relative PATH entry depends on this process's cwd;
    it is found for this process but never written to the shared cli_path.json."""
    _exe(tmp_path / "rel" / "claude", "#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    found = seats.find_cli("claude", env={"PATH": "rel"}, now=time.time())
    assert found is not None                                   # this process still finds it
    assert seats.learned_dirs() == []
    assert not (state / seats.CLI_PATH_FILE).exists()


@pytest.mark.timing
def test_busy_lock_skips_the_write_without_blocking(state, tmp_path):
    """MAP-FIX-2b: learning is best-effort. With the lock held by someone else,
    find_cli returns quickly and cli_path.json is left alone."""
    from llm_router.file_lock import exclusive_lock
    exe = _exe(tmp_path / "bin" / "claude", "#!/bin/sh\n")
    lock = state / (seats.CLI_PATH_FILE + ".lock")
    with exclusive_lock(lock) as held:
        assert held
        import threading
        out = {}

        def other():                     # flock conflicts across open files, even in one process
            t = time.monotonic()
            out["found"] = seats.find_cli("claude", env={"PATH": str(exe.parent)}, now=time.time())
            out["dt"] = time.monotonic() - t

        th = threading.Thread(target=other)
        th.start()
        th.join(10)
    assert out["found"] == str(exe)
    assert out["dt"] < 2.0, out["dt"]
    assert not (state / seats.CLI_PATH_FILE).exists()


def _iso(t: float) -> str:
    return seats.datetime.fromtimestamp(t, tz=seats.timezone.utc).isoformat()


def _blind_refresh(home: Path, now: float) -> seats.Seats:
    """refresh_seats from a process that sees no CLI (the launchd-shaped writer)."""
    return seats.refresh_seats(home=home, runner=lambda _a, _t: None, which=lambda _n: None,
                               env={}, opener=_no_http, now=now)


def test_refresh_without_the_cli_carries_a_recent_connected_seat(state, tmp_path):
    """MAP-FIX-2c: refresh_seats (doctor, install, the stale-cache hook path) saved its own
    verdict with detected_at=now, so a process that cannot see the CLI overwrote a seat
    another writer saw connected minutes ago. It now carries it like the rescan does,
    keeping the sighting's detected_at, and writes the not-found verdict after an hour."""
    t0 = time.time() - 6 * 3600
    seats.save_seats(seats.Seats(claude=seats.Seat(kind="claude.ai", plan="max"),
                                 detected_at=_iso(t0)), tmp_path)

    for dt in (10 * 60, 50 * 60):                              # two blind refreshes inside the hour
        got = _blind_refresh(tmp_path, t0 + dt)
        saved = seats.load_seats(tmp_path)
        assert got.claude.status == saved.claude.status == "connected", dt
        assert saved.detected_at == _iso(t0), "a carried save moved the anchor"

    _blind_refresh(tmp_path, t0 + 61 * 60)                     # past CARRY_MAX_AGE_S from the sighting
    saved = seats.load_seats(tmp_path)
    assert saved.claude.status != "connected"
    assert saved.claude.reason == "claude CLI not found"
    assert saved.detected_at == _iso(t0 + 61 * 60)


@pytest.mark.parametrize("err, writes, recorded", [
    (errno.EAGAIN, False, []),                                 # busy: skip, the next sighting re-learns
    (errno.ENOLCK, True, ["CHZ-FO-CLI-PATH-LOCK"]),            # no locking (NFS home): write, count it
])
def test_learn_tells_a_busy_lock_from_no_locking(err, writes, recorded, state, tmp_path, monkeypatch):
    """MAP-FIX-2c: exclusive_lock yields False both when another process holds the lock
    and when the filesystem has no locking. Only the first may skip the write; on the
    second, skipping disabled learning for good."""
    from types import SimpleNamespace

    from llm_router import failopen, file_lock

    def flock(_fd, _op):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(file_lock, "fcntl", SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8))
    monkeypatch.setattr(seats, "LEARN_LOCK_TIMEOUT_S", 0.0)
    codes: list[str] = []
    monkeypatch.setattr(failopen, "record", lambda code, *_a, **_k: codes.append(code))
    exe = _exe(tmp_path / "bin" / "claude", "#!/bin/sh\n")

    assert seats.find_cli("claude", env={"PATH": str(exe.parent)}, now=time.time()) == str(exe)
    assert (str(exe.parent) in seats.learned_dirs()) is writes
    assert codes == recorded


def test_lock_failopen_detail_names_the_errno(state, tmp_path, monkeypatch):
    """MAP-FIX-2d: CHZ-FO-CLI-PATH-LOCK said "unavailable" for every cause, so a
    read-only state dir (EACCES opening the lock file) was logged like a filesystem
    with no locking (ENOLCK from flock). The detail now names the errno."""
    from types import SimpleNamespace

    from llm_router import failopen, file_lock, state_files

    details: dict[str, str] = {}

    def run(label: str) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(failopen, "record",
                            lambda code, *_a, detail="", **_k: recorded.append(f"{code}:{detail}"))
        exe = _exe(tmp_path / label / "claude", "#!/bin/sh\n")
        assert seats.find_cli("claude", env={"PATH": str(exe.parent)}, now=time.time()) == str(exe)
        assert (str(exe.parent) in seats.learned_dirs()) is True   # wrote unlocked
        assert len(recorded) == 1
        details[label] = recorded[0]

    def flock(_fd, _op):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
    with monkeypatch.context() as m:
        m.setattr(file_lock, "fcntl", SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8))
        run("nolck")

    def open_private(*_a, **_k):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    with monkeypatch.context() as m:
        m.setattr(state_files, "open_private", open_private)
        run("acces")

    assert details["nolck"] == "CHZ-FO-CLI-PATH-LOCK:unavailable (ENOLCK)"
    assert details["acces"] == "CHZ-FO-CLI-PATH-LOCK:unavailable (EACCES)"


def test_exclusive_lock_status_errno_is_additive(tmp_path):
    """MAP-FIX-2d: status["state"] keeps its values; status["errno"] names the errno
    only when an OSError made locking unavailable, else None."""
    from llm_router.file_lock import exclusive_lock
    lock = tmp_path / "x.lock"
    st: dict = {}
    with exclusive_lock(lock, timeout=0, status=st) as got:
        assert got and st == {"state": "acquired", "errno": None}
        held: dict = {}
        with exclusive_lock(lock, timeout=0, status=held) as got2:
            assert not got2 and held == {"state": "held", "errno": None}
    (tmp_path / "f").write_text("x")       # a file where the lock's parent dir should be
    st = {}
    with exclusive_lock(tmp_path / "f" / "x.lock", timeout=0, status=st) as got3:
        assert not got3 and st == {"state": "unavailable", "errno": "ENOTDIR"}


def test_refresh_reports_a_carry_by_identity_not_equality(state, tmp_path, monkeypatch):
    """MAP-FIX-2d: Seat equality ignores status and reason, so a blind verdict that keeps
    the kind and plan (status installed_not_connected, reason "CLI not found") equals the
    connected seat it is carried over. refresh_seats must still report it as carried."""
    t0 = time.time() - 600
    seats.save_seats(seats.Seats(claude=seats.Seat(kind="claude.ai", plan="max"),
                                 detected_at=_iso(t0)), tmp_path)
    blind = seats.Seat(kind="claude.ai", plan="max", status="installed_not_connected",
                       reason="claude CLI not found")
    monkeypatch.setattr(seats, "detect_seats",
                        lambda **_kw: seats.Seats(claude=blind, detected_at=_iso(time.time())))
    carried: dict = {}
    out = seats.refresh_seats(tmp_path, carried=carried)
    assert out.claude.status == "connected"
    assert carried == {"claude": blind} and carried["claude"].status == "installed_not_connected"


def test_learned_dir_is_normalised(state, tmp_path):
    """A PATH entry with `..` is stored as the dir it names, so two spellings of one
    dir do not make two entries."""
    _exe(tmp_path / "bin" / "claude", "#!/bin/sh\n")
    (tmp_path / "sub").mkdir()
    entry = os.path.join(str(tmp_path), "sub", "..", "bin")
    assert seats.find_cli("claude", env={"PATH": entry}, now=time.time())
    assert seats.learned_dirs() == [str(tmp_path / "bin")]
