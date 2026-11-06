"""W8 (coordinator S8): the sub-agent scan costs a refresh only what changed.

The opt-in sub-agent scan of ``northstar.units(subagents=True)`` decoded every record of every sub-agent
transcript on every ``statusline --refresh`` (measured +3 s per run, ``changelog.d/W8-subagent-index.changed.md``).
It now keeps, per file and keyed by the file's state ``(size, mtime_ns)``, the three facts a plain message
needs (id, model, time): an unchanged file is not decoded again. Each test builds the transcripts by hand.
"""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from llm_router import northstar as ns
from llm_router import paths
from tests.test_northstar import _home_dir
from tests.test_w8_ns_inputs import LOCAL, OPUS, SID, SONNET, T0, _asst, _calls, _main, _sub, _text, _tool, _units, _routing_db


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))


def _index_path():
    return paths.state_path(ns.SUBAGENT_INDEX_NAME)


def _spy(monkeypatch):
    """The sub-agent transcripts ``_iter_jsonl`` is asked to decode, in order."""
    read = []
    real = ns._iter_jsonl
    monkeypatch.setattr(ns, "_iter_jsonl", lambda path: read.append(str(path)) or real(path))
    return read


def _fixture(tmp_path):
    """A session with a main file and three sub-agent files: a multi-record message, a message shared with
    another file, a synthetic record, a record with no id and one with no model."""
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [
        _asst(T0 + 10, "msg_s1", SONNET, _text("a")),
        _asst(T0 + 11, "msg_s1", SONNET, _tool("tu_s1", "Bash")),       # a second record of the same message
        _asst(T0 + 12, "msg_dup", SONNET, _text("b")),
        _asst(T0 + 13, "msg_syn", "<synthetic>", _text("No response requested.")),
    ])
    _sub(proj, "agent-a2.jsonl", [
        _asst(T0 + 20, "msg_dup", SONNET, _text("again")),             # counted once per session
        _asst(T0 + 21, None, None, _text("no id, no model")),
        _asst(T0 + 22, "msg_s2", OPUS, _text("c")),
    ])
    _sub(proj, "workflows/wf_1/agent-b1.jsonl", [_asst(T0 + 30, "msg_s3", SONNET, _text("d"))])
    return proj


def _sub_reads(read):
    return sorted(os.path.basename(p) for p in read if "subagents" in p)


def test_a_second_scan_decodes_no_unchanged_subagent_file(tmp_path, monkeypatch):
    proj = _fixture(tmp_path)
    read = _spy(monkeypatch)
    first = _units(proj, subagents=True)
    assert _sub_reads(read) == ["agent-a1.jsonl", "agent-a2.jsonl", "agent-b1.jsonl"]      # cold: all three
    read.clear()
    second = _units(proj, subagents=True)
    assert _sub_reads(read) == []                          # steady state: none
    assert second == first and len(_calls(first, ns.UNIT_SIDECHAIN)) == 5


def test_the_indexed_units_are_the_decoded_units(tmp_path):
    """Cold (no index), warm (from the index) and a scan that ignores a damaged index give the same units."""
    proj = _fixture(tmp_path)
    cold = _units(proj, subagents=True)
    assert _index_path().is_file()
    warm = _units(proj, subagents=True)
    _index_path().write_text("{ not json")
    rebuilt = _units(proj, subagents=True)
    assert cold == warm == rebuilt
    side = _calls(cold, ns.UNIT_SIDECHAIN)
    # file order; the second record of msg_s1, the repeat of msg_dup and the synthetic record add no unit
    assert [u["model"] for u in side] == [SONNET, SONNET, None, OPUS, SONNET]


def test_a_changed_file_is_decoded_again_and_only_that_one(tmp_path, monkeypatch):
    proj = _fixture(tmp_path)
    _units(proj, subagents=True)
    path = proj / SID / "subagents" / "agent-a2.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_asst(T0 + 40, "msg_new", SONNET, _text("later"))) + "\n")
    read = _spy(monkeypatch)
    rows = _units(proj, subagents=True)
    assert _sub_reads(read) == ["agent-a2.jsonl"]
    assert len(_calls(rows, ns.UNIT_SIDECHAIN)) == 6        # the appended message is a unit
    read.clear()
    _units(proj, subagents=True)
    assert _sub_reads(read) == []                           # and the index follows the new state


def test_a_touched_file_with_the_same_size_is_decoded_again(tmp_path, monkeypatch):
    proj = _fixture(tmp_path)
    _units(proj, subagents=True)
    path = proj / SID / "subagents" / "workflows" / "wf_1" / "agent-b1.jsonl"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    read = _spy(monkeypatch)
    _units(proj, subagents=True)
    assert _sub_reads(read) == ["agent-b1.jsonl"]


def test_a_file_with_an_mcp_call_is_always_read_so_its_model_join_is_current(tmp_path, monkeypatch):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-m.jsonl", [_asst(T0 + 10, "msg_m", SONNET, _tool("toolu_M", task="query"))])
    _sub(proj, "agent-plain.jsonl", [_asst(T0 + 11, "msg_p", SONNET, _text())])
    before = _calls(_units(proj, subagents=True), ns.UNIT_ROUTED_MCP)
    assert [u["model"] for u in before] == [None]            # no routing row yet
    _routing_db(tmp_path, [("toolu_M", LOCAL, "router_unhinted", "2026-11-03 10:00:00")])
    read = _spy(monkeypatch)
    after = _calls(_units(proj, subagents=True), ns.UNIT_ROUTED_MCP)
    assert _sub_reads(read) == ["agent-m.jsonl"]             # the plain file came from the index, the MCP file did not
    assert [u["model"] for u in after] == [LOCAL]            # an unchanged file, a changed answer


def test_a_file_with_a_message_a_proxy_served_is_read_in_full(tmp_path, monkeypatch):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_lr", SONNET, _text()), _asst(T0 + 11, "msg_other", SONNET, _text())])
    assert [u["lever"] for u in _calls(_units(proj, subagents=True), ns.UNIT_SIDECHAIN)] == ["agent_route", "agent_route"]
    ledger = _home_dir(tmp_path) / "proxy_calls.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"ts": %d, "session_id": "%s", "msg_id": "msg_lr", "decision": "served", '
                      '"model": "%s", "task_type": "code"}\n' % (T0 + 10, SID, LOCAL))
    read = _spy(monkeypatch)
    rows = _calls(_units(proj, subagents=True), ns.UNIT_SIDECHAIN)
    assert _sub_reads(read) == ["agent-a1.jsonl"]            # the ledger changed, the file did not
    assert sorted((u["lever"], u["model"]) for u in rows) == [("agent_route", SONNET), ("proxy", LOCAL)]


@pytest.mark.parametrize("content", ["{ not json", "[]", json.dumps({"v": 99, "files": {}}), json.dumps({"v": 1, "files": []}),
                                     json.dumps({"v": 1, "files": {"x": 1}})])
def test_a_damaged_or_foreign_index_is_rebuilt_not_trusted(tmp_path, monkeypatch, content):
    proj = _fixture(tmp_path)
    first = _units(proj, subagents=True)
    _index_path().write_text(content)
    read = _spy(monkeypatch)
    assert _units(proj, subagents=True) == first
    assert len(_sub_reads(read)) == 3                        # nothing was trusted
    assert json.loads(_index_path().read_text())["v"] == 1   # and a good index was written back


def test_an_entry_with_a_malformed_message_is_not_trusted(tmp_path, monkeypatch):
    proj = _fixture(tmp_path)
    first = _units(proj, subagents=True)
    data = json.loads(_index_path().read_text())
    victim = next(k for k in data["files"] if k.endswith("agent-b1.jsonl"))
    data["files"][victim]["msgs"][0] = ["msg_s3", 7, "later"]            # a model that is no string, a ts that is no number
    _index_path().write_text(json.dumps(data))
    read = _spy(monkeypatch)
    assert _units(proj, subagents=True) == first
    assert _sub_reads(read) == ["agent-b1.jsonl"]


def test_the_default_scan_never_reads_or_writes_the_index(tmp_path):
    """The Stop line and the quality breaker (hot paths) keep reading main files only."""
    proj = _fixture(tmp_path)
    assert _calls(_units(proj), ns.UNIT_SIDECHAIN) == []
    assert not _index_path().exists()


def test_a_single_session_scan_indexes_only_that_sessions_files(tmp_path):
    other = "bbbbbbbb-1111-2222-3333-444444444444"
    proj = _fixture(tmp_path)
    _main(tmp_path, [_asst(T0 + 1, "msg_o", OPUS, _text())], sid=other)
    _sub(proj, "agent-o1.jsonl", [_asst(T0 + 10, "msg_o1", SONNET, _text())], sid=other)
    list(ns.units(days=None, session_id=SID, root=proj.parent, subagents=True))
    keys = list(json.loads(_index_path().read_text())["files"])
    assert keys and all(f"/{SID}/subagents/" in k for k in keys)


def test_the_files_that_are_gone_leave_the_index_and_another_roots_entries_stay(tmp_path):
    proj = _fixture(tmp_path)
    _units(proj, subagents=True)
    data = json.loads(_index_path().read_text())
    data["files"]["/some/other/root/p/s/subagents/agent-x.jsonl"] = {"sig": [1, 2], "mcp": False, "msgs": []}
    _index_path().write_text(json.dumps(data))
    (proj / SID / "subagents" / "agent-a1.jsonl").unlink()
    _units(proj, subagents=True)
    keys = set(json.loads(_index_path().read_text())["files"])
    assert not any(k.endswith("agent-a1.jsonl") for k in keys)
    assert "/some/other/root/p/s/subagents/agent-x.jsonl" in keys
    assert sum(k.endswith(("agent-a2.jsonl", "agent-b1.jsonl")) for k in keys) == 2


def test_the_window_prefilter_still_applies_and_leaves_no_entry(tmp_path, monkeypatch):
    proj = _main(tmp_path, [_asst(time.time() - 60, "msg_main", OPUS, _text())])
    _sub(proj, "agent-old.jsonl", [_asst(time.time() - 60, "msg_old", SONNET, _text())])
    old = proj / SID / "subagents" / "agent-old.jsonl"
    os.utime(old, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    read = _spy(monkeypatch)
    assert _calls(list(ns.units(days=7, root=proj.parent, subagents=True)), ns.UNIT_SIDECHAIN) == []
    assert _sub_reads(read) == []
    assert not _index_path().exists() or not any(k.endswith("agent-old.jsonl") for k in json.loads(_index_path().read_text())["files"])


def test_the_index_is_private_and_holds_no_message_text(tmp_path):
    proj = _fixture(tmp_path)
    _units(proj, subagents=True)
    assert stat.S_IMODE(_index_path().stat().st_mode) == 0o600
    text = _index_path().read_text()
    assert "no id, no model" not in text and "again" not in text          # ids, models and times only
