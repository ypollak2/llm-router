"""PLAN v16 P1.1 task 3: exactly seven context doors, each wired and tested.

``context_pack.DOORS`` is the list. Each door must have a call site that hands
``build_pack`` its door name, and its own ``test_context_pack_door_<name>.py``
that proves the wiring behaviourally. A door added to ``DOORS`` without both,
or a call site that loses its door, fails here.
"""
from __future__ import annotations

import re
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
    # sdk and gateway share route_and_call -> _call_text, which packs with
    # door=context_door; sdk is that parameter's default, gateway is passed by
    # route_server (the core behind every gateway endpoint).
    "sdk": ("router.py", r'context_door: str = "sdk"'),
    "gateway": ("route_server.py", r'context_door="gateway"'),
}

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
