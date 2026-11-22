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
