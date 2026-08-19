"""The mcp 2.0 model field names, asserted so the major version is testable.

WHY THIS EXISTS
===============

`tests/qa/test_mcp_handshake.py` already reads these fields, which is how the
2.0 renames were found at all — porting this project failed 17 tests on them:

    Tool.inputSchema                 -> input_schema
    InitializeResult.serverInfo      -> server_info
    InitializeResult.protocolVersion -> protocol_version

But the handshake tests read them incidentally, while checking something else. If
the installed mcp changed major version, they would fail for reasons that need
interpretation — a stack trace about a missing attribute, three layers into a
server fixture.

This file asserts the renames DIRECTLY, so a major-version mismatch fails with a
sentence saying so.

WHY IT MATTERS BEYOND THIS PROJECT

The downstream copy of this codebase was reported VERIFIED on 2726 passing
tests. It was not verified; it was unexercised — that repository has zero
references to any of these fields, so its suite passed regardless of which mcp
major was installed. A suite that cannot fail on a change is not evidence about
that change.

This test is the discriminating version, and it lives here because upstream is
where work lands first. It goes downstream as a copy during the next sync.

WHAT IT ASSERTS

Both directions. Presence catches a resolve to 1.x. Absence catches a
compatibility shim reintroducing the camelCase names, which would let 1.x-era
code keep working and make the pin's bounds stop meaning anything — the failure
would then resurface somewhere with no test at all.
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
        f"expose the 2.x API this project is pinned and ported to — either the "
        f"pin was widened downward, or the environment resolved 1.x."
    )


@pytest.mark.parametrize(
    "model,new,old", _RENAMES, ids=[f"{m.__name__}.{o}" for m, _, o in _RENAMES]
)
def test_the_1x_field_name_is_gone(model, new: str, old: str):
    """Absence matters as much as presence — see the module docstring."""
    assert old not in model.model_fields, (
        f"{model.__name__} still exposes {old!r} alongside {new!r}. That makes "
        f"the 1.x/2.x distinction untestable, which is exactly how the downstream "
        f"copy ended up with a green suite over an incomplete port."
    )


def test_the_server_builds_against_these_models():
    """End-to-end, because the field checks alone prove nothing about the port.

    They pass against a bare `mcp` install with no project code involved. Without
    this assertion they would be a statement about the dependency.
    """
    import llm_router.server as server

    assert type(server.mcp).__name__ == "MCPServer", (
        f"server built a {type(server.mcp).__name__}, not MCPServer — the port "
        f"is incomplete or the module resolved an older copy"
    )
