"""SEC-1: the `llm-router-sse` console script must not exist.

`server.main_sse`'s own docstring states the prohibition and why:

    This function is INTENTIONALLY not exposed as a console script.
    The prior `llm_router-sse` entry point bound 0.0.0.0 with no auth and
    exposed the full 60-tool MCP surface — including filesystem tools
    and wallet — to anyone reachable on the network.

It lists three conditions that must ALL hold before re-adding it: bearer-token
auth wrapping `mcp.sse_app()`, INV-010 landed, and a `127.0.0.1` default with
`0.0.0.0` behind an explicit opt-in.

The script was nonetheless present in `[project.scripts]` and shipped in
13.0.3 with none of the three satisfied: `main_sse` has no auth middleware
(that is `main_sse_secured`, which the script did NOT point at), and it bound
`os.environ.get("HOST", "0.0.0.0")` without ever calling `_allow_public_bind()`
— a gate defined a few lines below it in the same module.

The prohibition names the UNDERSCORE spelling (`llm_router-sse`), which is
likely how it came back: the rebrand regenerated the script table with
hyphenated names and nothing watched for the hyphenated variant. These tests
therefore match on shape, not on one spelling.
"""
from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _console_scripts() -> dict[str, str]:
    with (_REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"].get("scripts", {})


def test_no_sse_console_script_is_declared():
    offenders = {n: t for n, t in _console_scripts().items() if "sse" in n.lower()}
    assert not offenders, (
        f"{offenders} - server.main_sse's docstring forbids exposing it as a "
        f"console script until auth, INV-010 and a localhost default all land."
    )


def test_nothing_points_at_main_sse():
    """Matched on TARGET as well as name: a differently-named script is the same hole."""
    offenders = {n: t for n, t in _console_scripts().items() if t.endswith(":main_sse")}
    assert not offenders, f"{offenders} exposes the unauthenticated SSE server"


def test_main_sse_defaults_to_localhost():
    """Condition 3 of the docstring's own re-add checklist."""
    import inspect

    from llm_router import server

    src = inspect.getsource(server.main_sse)
    assert '"0.0.0.0"' not in src, (
        "main_sse still defaults to 0.0.0.0; the docstring requires 127.0.0.1 "
        "with 0.0.0.0 behind an explicit opt-in"
    )


def test_main_sse_consults_the_public_bind_gate():
    """`_allow_public_bind()` sits in the same module and was never called."""
    import inspect

    from llm_router import server

    src = inspect.getsource(server.main_sse)
    assert "_allow_public_bind" in src, (
        "main_sse binds a socket without consulting the shared public-bind gate"
    )


@pytest.mark.slow
def test_built_wheel_ships_no_sse_script():
    """The artifact is what users install - check it, not just the source."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run(["uv", "build", "-o", out], cwd=_REPO,
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            pytest.skip(f"uv build failed: {r.stderr[-300:]}")
        wheels = list(Path(out).glob("*.whl"))
        if not wheels:
            pytest.skip("no wheel produced")
        zf = zipfile.ZipFile(wheels[0])
        eps = [n for n in zf.namelist() if n.endswith("entry_points.txt")]
        assert eps, "wheel declares no entry points at all"
        body = zf.read(eps[0]).decode()
    bad = [ln for ln in body.splitlines() if "sse" in ln.lower()]
    assert not bad, f"the built WHEEL ships an SSE console script: {bad}"
