"""P2.12 (P212-4): ``llm_router_admin(action="install")``, the MCP install door.

Same tmp-HOME harness as test_p212_receipt.py: real installers, no real HOME, no seat probe.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from llm_router.commands import install
from llm_router.tools import consolidated
from tests.test_p212_receipt import _add_host, _point_at


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _token(text: str) -> str:
    return json.loads(re.search(r"value='(\{.*\})'", text).group(1))["confirm"]


def _fixture(root: Path, monkeypatch) -> Path:
    home = _point_at(root, monkeypatch)
    for h in ("codex", "gemini-cli"):
        _add_host(home, h)
    return home


@pytest.mark.asyncio
async def test_two_step_install_is_silent_and_matches_the_cli_digest(tmp_path, monkeypatch, capsys):
    a = _fixture(tmp_path / "a", monkeypatch)
    before = _tree(a)

    plan = await consolidated.llm_router_admin("install")
    assert _tree(a) == before, "step 1 wrote files"
    assert install.read_receipt() is None
    assert "Nothing installed yet" in plan and "codex" in plan and "claude-code" in plan

    done = await consolidated.llm_router_admin("install", json.dumps({"confirm": _token(plan)}))
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    mcp_receipt = install.read_receipt()
    assert mcp_receipt is not None and install.receipt_failures(mcp_receipt) == []
    assert f"digest: {mcp_receipt['digest']}" in done and done.startswith("Installed.")
    from llm_router import authority
    assert not authority.state_file().exists(), "install must not set the routing authority"

    b = _fixture(tmp_path / "b", monkeypatch)
    assert install.cmd_install(["--auto"]) == 0
    assert install.read_receipt()["digest"] == mcp_receipt["digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["wrong", "expired", "garbage"])
async def test_bad_token_is_refused_and_writes_nothing(tmp_path, monkeypatch, kind):
    home = _fixture(tmp_path / "h", monkeypatch)
    plan = await consolidated.llm_router_admin("install")
    token = _token(plan)
    before = _tree(home)
    if kind == "wrong":
        token = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif kind == "expired":
        monkeypatch.setattr("time.time", lambda: float(token.split(".")[0]) + install.INSTALL_TOKEN_TTL_S + 1)
    else:
        token = "nope"
    out = await consolidated.llm_router_admin("install", json.dumps({"confirm": token}))
    assert out.startswith("Not installed")
    assert _tree(home) == before and install.read_receipt() is None
