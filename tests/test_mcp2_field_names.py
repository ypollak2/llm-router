"""The mcp 2.0 model fields this repo's suite never touched.

WHY THIS EXISTS
===============

The mcp 2.0 port here was reported VERIFIED on 2726 passing tests. It was not.
mcp 2.0.0 renamed model fields to snake_case:

    Tool.inputSchema                 -> input_schema
    InitializeResult.serverInfo      -> server_info
    InitializeResult.protocolVersion -> protocol_version

Porting the upstream project hit exactly those renames and failed 17 tests. This
repository has ZERO references to any of them, in src/ or tests/ — so its green
suite could not have detected the change. It measured what the suite exercises,
not whether the port is correct.

That is why "the suite passes" was downgraded to PROVISIONAL rather than
accepted. A test that touches the fields is the only thing that can upgrade it,
and adding one is the point of this file: without it, re-running the suite would
produce the same green for the same uninformative reason.

WHAT IT ASSERTS

That the installed mcp exposes the 2.x names and NOT the 1.x ones. Both
directions matter: the presence check catches a downgrade to 1.x, and the
absence check catches a future rename back or a compatibility shim that would
make the pin's upper bound quietly meaningless.
"""

from __future__ import annotations

import pytest

from mcp.types import InitializeResult, Tool

#: (model, 2.x name, the 1.x name it replaced)
_RENAMES = [
    (Tool, "input_schema", "inputSchema"),
    (InitializeResult, "server_info", "serverInfo"),
    (InitializeResult, "protocol_version", "protocolVersion"),
]


@pytest.mark.parametrize(
    "model,new,old", _RENAMES, ids=[f"{m.__name__}.{n}" for m, n, _ in _RENAMES]
)
def test_the_2x_field_name_exists(model, new: str, old: str):
    assert new in model.model_fields, (
        f"{model.__name__} has no field {new!r}. The installed mcp does not "
        f"expose the 2.x API this code is pinned and ported to — either the pin "
        f"was widened downward, or the environment resolved 1.x."
    )


@pytest.mark.parametrize(
    "model,new,old", _RENAMES, ids=[f"{m.__name__}.{o}" for m, _, o in _RENAMES]
)
def test_the_1x_field_name_is_gone(model, new: str, old: str):
    """Absence matters as much as presence.

    If a shim reintroduced the camelCase names, code written against 1.x would
    keep working and the pin's lower bound would stop meaning anything — the
    failure would resurface later, somewhere with no test at all.
    """
    assert old not in model.model_fields, (
        f"{model.__name__} still exposes {old!r} alongside {new!r}. That makes "
        f"the 1.x/2.x distinction untestable here, which is how this repository "
        f"got a green suite over an incomplete port in the first place."
    )


def test_the_server_builds_against_these_models():
    """End-to-end: the port is not just importable, it constructs.

    The field checks above pass against a bare `mcp` install with no project code
    involved. Without this, they would prove something about the dependency and
    nothing about the port.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import llm_router.server as server

    assert type(server.mcp).__name__ == "MCPServer", (
        f"server built a {type(server.mcp).__name__}, not MCPServer — the port "
        f"is incomplete or the module resolved an older copy"
    )
