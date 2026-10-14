"""PLAN v16 P1.1 task 3 / P1.1-a: exactly seven context doors, each wired and tested.

``context_pack.DOORS`` is the list. Each door must have a call site that hands
``build_pack`` its door name, and its own ``test_context_pack_door_<name>.py``
that proves the wiring behaviourally. A door added to ``DOORS`` without both,
or a call site that loses its door, fails here.

``test_a_spy_sees_each_door_call_build_pack`` is the P1.1-a spy: it runs one
behavioural test per door (``SPY_TESTS``, each driving that door's real code) in a
child pytest with ``build_pack`` wrapped before any test module is imported, and
requires that the doors seen are exactly the seven, one per test.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from llm_router import context_pack

SRC = Path(context_pack.__file__).parent
TESTS = Path(__file__).parent

#: door -> (source file, pattern proving that file hands the door to build_pack)
WIRING = {
    "hook": ("hooks/auto-route.py", r'build_pack\(.{0,600}?door="hook"'),
    "mcp": ("router.py", r'build_pack\(.{0,600}?door="mcp"'),
    "proxy": ("proxy/backends.py", r'build_pack\(.{0,600}?door="proxy"'),
    "codex": ("codex_agent.py", r'build_pack\(.{0,600}?door="codex"'),
    "agent": ("hooks/agent-route.py", r'build_pack\(.{0,600}?door="agent"'),
    # The SDK packs inside its own build_chain/execute_chain path (D-98).
    "sdk": ("sdk.py", r'build_pack\(.{0,600}?door="sdk"'),
    # The gateway goes through route_and_call -> _call_text, which packs with
    # door=context_door; route_server (the core behind every gateway endpoint)
    # passes "gateway".
    "gateway": ("route_server.py", r'context_door="gateway"'),
}

#: door -> the behavioural test that drives that door's real call site
SPY_TESTS = {
    "hook": "test_context_pack_door_hook.py::test_draft_builds_one_hook_pack_and_relays_tool_blocks",
    "mcp": "test_context_pack_door_mcp.py::test_builds_one_mcp_pack_per_call",
    "proxy": "test_context_pack_door_proxy.py::"
             "test_local_tier_builds_the_pack_once_per_request_with_door_proxy",
    "codex": "test_context_pack_door_codex.py::"
             "test_build_pack_called_once_with_codex_door_and_request_untruncated",
    "agent": "test_context_pack_door_agent_route.py::test_direct_run_builds_pack_once_with_agent_door",
    "sdk": "test_context_pack_door_sdk.py::test_messages_reach_execute_chain_as_history",
    "gateway": "test_context_pack_door_gateway.py::"
               "test_chat_completion_reaches_the_provider_as_packed_turns",
}

#: Installed in the child pytest before collection, so every door test's own
#: spy (``real = context_pack.build_pack``) wraps this recorder.
_RECORDER = """
    import json, os, sys
    from llm_router import context_pack

    _real, _seen, _cur = context_pack.build_pack, {}, [None]

    def _caller():
        # The first frame outside the tests (their spies wrap this recorder) and
        # outside this plugin: the production code that called build_pack.
        f = sys._getframe(2)
        while f is not None and ("/tests/" in f.f_code.co_filename
                                 or f.f_code.co_filename == __file__):
            f = f.f_back
        return f.f_code.co_filename if f is not None else ""

    def _recording(*args, **kwargs):
        _seen.setdefault(_cur[0], []).append([kwargs.get("door"), _caller()])
        return _real(*args, **kwargs)

    context_pack.build_pack = _recording

    def pytest_runtest_setup(item):
        _cur[0] = item.nodeid.split("[", 1)[0]

    def pytest_runtest_teardown(item):
        _cur[0] = None

    def pytest_sessionfinish(session, exitstatus):
        with open(os.environ["DOOR_SPY_OUT"], "w") as fh:
            json.dump({"seen": _seen, "exit": int(exitstatus)}, fh)
"""

#: door -> its behavioural test file
TEST_FILES = {door: f"test_context_pack_door_{door}.py" for door in context_pack.DOORS}
TEST_FILES["agent"] = "test_context_pack_door_agent_route.py"


def test_there_are_exactly_seven_doors():
    assert context_pack.DOORS == ("hook", "mcp", "proxy", "codex", "agent", "sdk", "gateway")
    assert set(WIRING) == set(context_pack.DOORS)


@pytest.mark.parametrize("door", context_pack.DOORS)
def test_each_door_is_wired(door):
    path, pattern = WIRING[door]
    text = (SRC / path).read_text(encoding="utf-8")
    assert re.search(pattern, text, re.S), f"{door}: no build_pack call site in {path}"


def test_shared_router_path_packs_with_the_callers_door():
    text = (SRC / "router.py").read_text(encoding="utf-8")
    assert re.search(r"build_pack\(.{0,600}?door=context_door", text, re.S)


@pytest.mark.parametrize("door", context_pack.DOORS)
def test_each_door_has_its_own_test_file(door):
    assert (TESTS / TEST_FILES[door]).is_file(), f"{door}: missing {TEST_FILES[door]}"


@pytest.mark.parametrize("door", context_pack.DOORS)
def test_build_pack_accepts_every_door(door):
    pack = context_pack.build_pack("REQ", [], target_window=8192, door=door, project="")
    assert pack.request == "REQ"


def test_build_pack_rejects_an_unknown_door():
    with pytest.raises(ValueError):
        context_pack.build_pack("REQ", [], target_window=8192, door="eighth", project="")


def test_spy_tests_cover_every_door():
    assert set(SPY_TESTS) == set(context_pack.DOORS)
    for door, node in SPY_TESTS.items():
        assert node.startswith(TEST_FILES[door] + "::"), door


# A nested pytest (interpreter start, conftest, seven door tests): ~6-8 s alone, so
# the suite-wide 30 s timeout is too tight under `-n auto` load (P12-FLAKE-1, see
# test_n_suite_ignores_the_operators_settings.py). The subprocess budget sits just
# inside the mark so a hang reports the child's output.
@pytest.mark.timeout(180)
@pytest.mark.xdist_group("nested_pytest")
def test_a_spy_sees_each_door_call_build_pack(tmp_path):
    (tmp_path / "door_spy_recorder.py").write_text(textwrap.dedent(_RECORDER), encoding="utf-8")
    out = tmp_path / "seen.json"
    home = tmp_path / "home"
    (home / "lr").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LLM_ROUTER_", "PYTEST_", "CLAUDE_"))}
    env.update(DOOR_SPY_OUT=str(out), HOME=str(home), LLM_ROUTER_HOME=str(home / "lr"),
               PYTHONPATH=os.pathsep.join([str(tmp_path), str(SRC.parent),
                                           env.get("PYTHONPATH", "")]))
    nodes = [str(TESTS / node) for node in SPY_TESTS.values()]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "door_spy_recorder", "-p", "no:cacheprovider",
         "-p", "no:xdist", "-o", "addopts=", *nodes],
        cwd=TESTS.parent, env=env, capture_output=True, text=True, timeout=170,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    seen = json.loads(out.read_text(encoding="utf-8"))["seen"]
    by_test = {node.rsplit("tests/", 1)[-1]: calls for node, calls in seen.items() if node}
    assert {node: {d for d, _ in by_test.get(node, [])} for node in SPY_TESTS.values()} == {
        node: {door} for door, node in SPY_TESTS.items()}
    # Each call came from production code (the llm_router package, source tree or
    # installed, or the hooks/ mirror), never from a test calling build_pack itself.
    callers = {Path(f).as_posix() for calls in by_test.values() for _d, f in calls}
    assert callers and all("/llm_router/" in f or "/hooks/" in f for f in callers), callers
    called = {d for calls in by_test.values() for d, _ in calls}
    assert called == set(context_pack.DOORS) and len(called) == 7
