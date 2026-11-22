"""prev_tool_targets: salted hash of the file/command the previous tool calls targeted."""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from llm_router.proxy import prev_targets as pt
from llm_router.proxy import steps

CANARY_PATH = "/home/PREVTARGETS-CANARY-PATH/secret.py"
CANARY_CMD = "curl https://PREVTARGETS-CANARY-CMD.example/token"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    pt.reset_for_tests()
    yield
    pt.reset_for_tests()


def _use(name, inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def _body(uses):
    return {"messages": [{"role": "user", "content": "go"},
                         {"role": "assistant", "content": uses},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}]}


def _h(name, inp):
    (e,) = steps.prev_tool_targets(_body([_use(name, inp)]))
    return e["h"]


def test_hash_is_stable_within_an_install_and_12_hex():
    h = _h("Edit", {"file_path": "/a/b.py"})
    assert h == _h("Edit", {"file_path": "/a/b.py"})
    assert len(h) == 12 and int(h, 16) >= 0
    pt.reset_for_tests()  # new process, same salt file
    assert _h("Edit", {"file_path": "/a/b.py"}) == h


def test_hash_differs_across_salts(tmp_path, monkeypatch):
    h1 = _h("Bash", {"command": "git status"})
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "other"))
    assert _h("Bash", {"command": "git status"}) != h1


def test_salt_file_is_0600_32_bytes_and_created_once(tmp_path):
    _h("Edit", {"file_path": "/x"})
    p = tmp_path / "home" / pt.SALT_FILE
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and len(p.read_bytes()) == 32
    before = p.read_bytes()
    pt.reset_for_tests()
    _h("Edit", {"file_path": "/x"})
    assert p.read_bytes() == before


def test_bad_salt_file_gives_null_hash_not_a_crash(tmp_path):
    p = tmp_path / "home" / pt.SALT_FILE
    p.parent.mkdir(parents=True)
    p.write_bytes(b"short")
    os.chmod(p, 0o600)
    assert _h("Edit", {"file_path": "/x"}) is None
    p.write_bytes(os.urandom(32))
    os.chmod(p, 0o644)  # group/world readable: refused
    pt.reset_for_tests()
    assert _h("Edit", {"file_path": "/x"}) is None


def test_normalisation_files_and_commands():
    assert _h("Edit", {"file_path": "/a//b/./c/../b.py"}) == _h("Edit", {"file_path": "/a/b/b.py"})
    assert _h("Edit", {"file_path": "/a/b.py"}) != _h("Edit", {"file_path": "/a/c.py"})
    assert _h("Bash", {"command": "  git   status\n"}) == _h("Bash", {"command": "git status"})
    assert _h("Bash", {"command": "git status"}) != _h("Bash", {"command": "git diff"})
    # same text as a path and as a command are the same normalised text (a deliberate non-goal to separate)
    assert _h("NotebookEdit", {"notebook_path": "/n.ipynb"}) == _h("Edit", {"file_path": "/n.ipynb"})


def test_absent_or_odd_inputs_are_null_not_zero():
    for name, inp in [("Edit", None), ("Edit", {}), ("Edit", {"file_path": None}), ("Edit", {"file_path": 0}),
                      ("Edit", {"file_path": "  "}), ("Bash", {"command": ""}), ("Bash", {"command": 5}),
                      ("Bash", "ls"), ("Grep", {"pattern": "x"}), ("mcp__x__y", {"file_path": "/a"})]:
        (e,) = steps.prev_tool_targets(_body([_use(name, inp)]))
        assert e == {"name": name, "h": None}, (name, inp)
    assert steps.prev_tool_targets({"messages": "junk"}) == []
    assert steps.prev_tool_targets({}) == []


def test_list_is_bounded_and_ordered():
    uses = [_use("Edit", {"file_path": f"/f{i}"}) for i in range(20)]
    out = steps.prev_tool_targets(_body(uses))
    assert len(out) == pt.MAX_TARGETS == 8
    assert out[0]["h"] == _h("Edit", {"file_path": "/f0"})


def test_no_raw_text_in_step_fields():
    body = _body([_use("Edit", {"file_path": CANARY_PATH, "old_string": "x"}),
                  _use("Bash", {"command": CANARY_CMD})])
    raw = json.dumps(steps.step_fields(body))
    assert "PREVTARGETS-CANARY" not in raw and "secret.py" not in raw
    fields = steps.step_fields(body)
    assert [t["name"] for t in fields["prev_tool_targets"]] == ["Edit", "Bash"]
    assert all(t["h"] for t in fields["prev_tool_targets"])


def test_step_fields_error_path_has_empty_targets(monkeypatch):
    seen = []
    monkeypatch.setattr(steps, "prev_tools", lambda b: 1 / 0)
    f = steps.step_fields({}, on_error=seen.append)
    assert f["step_error"] is True and f["prev_tool_targets"] == [] and len(seen) == 1


def test_performance_is_trivial():
    _h("Edit", {"file_path": "/warm"})
    body = _body([_use("Bash", {"command": "make test " * 50}) for _ in range(8)])
    t = time.perf_counter()
    for _ in range(1000):
        steps.prev_tool_targets(body)
    assert (time.perf_counter() - t) / 1000 < 0.001  # < 1 ms per row


def test_concurrent_creation_yields_one_salt(tmp_path):
    import threading
    home = tmp_path / "home"
    path = home / pt.SALT_FILE
    out, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        out.append(pt._load_or_create(path))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(out) == 8 and None not in out and len(set(out)) == 1
    assert out[0] == path.read_bytes()
    assert [p.name for p in home.iterdir()] == [pt.SALT_FILE]  # no temp files left


def test_concurrent_creation_across_processes(tmp_path):
    import subprocess
    import sys
    home = tmp_path / "home"
    code = ("import sys; from pathlib import Path; from llm_router.proxy import prev_targets as p;"
            "sys.stdout.write(p._load_or_create(Path(sys.argv[1])).hex())")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(home / pt.SALT_FILE)],
                              stdout=subprocess.PIPE, env=dict(os.environ)) for _ in range(4)]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs) and len(set(outs)) == 1 and len(outs[0]) == 64


def test_recovers_after_the_operator_replaces_a_bad_file_and_failure_is_not_cached(tmp_path, monkeypatch):
    p = tmp_path / "home" / pt.SALT_FILE
    p.parent.mkdir(parents=True)
    p.write_bytes(b"")
    os.chmod(p, 0o600)
    now = [1000.0]
    monkeypatch.setattr(pt.time, "monotonic", lambda: now[0])
    from llm_router import failopen
    codes = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, *, detail="": codes.append((code, detail)))
    assert pt.salt() is None
    assert pt.salt() is None  # inside the retry window: no re-read, no second event
    assert [c for c, _ in codes] == ["LR-FO-PROXY-PREV-TARGETS-SALT"]
    assert p.read_bytes() == b""  # a file we did not create is never overwritten
    p.write_bytes(os.urandom(32))
    assert pt.salt() is None  # still inside 60 s
    now[0] += pt.RETRY_S + 1
    got = pt.salt()
    assert got == p.read_bytes()
    assert pt.salt() == got  # now cached


def test_short_write_is_handled(monkeypatch, tmp_path):
    real = os.write
    monkeypatch.setattr(pt.os, "write", lambda fd, data: real(fd, bytes(data)[:5]))
    path = tmp_path / "home" / pt.SALT_FILE
    got = pt._load_or_create(path)
    assert got is not None and len(got) == 32 and path.read_bytes() == got


def test_module_works_without_o_nofollow(monkeypatch):
    monkeypatch.setattr(pt, "_NOFOLLOW", 0)
    h = _h("Edit", {"file_path": "/a"})
    assert h and h == _h("Edit", {"file_path": "/a"})


def test_no_file_io_under_the_lock_and_one_retrier(tmp_path, monkeypatch):
    import threading
    seen = {"calls": 0, "locked": []}
    gate, started = threading.Event(), threading.Event()
    real = pt._load_or_create

    def slow(path):
        seen["calls"] += 1
        seen["locked"].append(pt._lock.locked())
        started.set()
        gate.wait(5)
        return real(path)

    monkeypatch.setattr(pt, "_load_or_create", slow)
    res = []
    t = threading.Thread(target=lambda: res.append(pt.salt()))
    t.start()
    assert started.wait(5)
    assert pt.salt() is None  # another thread does not wait and does not do I/O
    gate.set()
    t.join()
    assert seen["calls"] == 1 and seen["locked"] == [False] and res[0] is not None


def test_temp_file_is_unlinked_when_link_fails_otherwise(tmp_path, monkeypatch):
    def boom(src, dst, *a, **k):
        raise PermissionError("no link")

    monkeypatch.setattr(pt.os, "link", boom)
    home = tmp_path / "home"
    assert pt._create_salt(home / pt.SALT_FILE) is None
    assert list(home.iterdir()) == []
