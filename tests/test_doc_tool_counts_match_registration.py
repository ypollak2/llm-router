"""The tool counts the docs state are the counts the server registers.

Audit 2026-09-24 (06_mcp_hosts.md MCP-01, 12_docs_claims.md DOC-01/02): docs said
60 tools and 11 front-door tools in seven places, a docstring said 41; the
server registered 70, and the default consolidated surface had 12 (the
twelfth was llm_local_task, the one that can write). Measured, not recalled.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCS = ["README.md", "guide/TOOLS.md", "guide/GETTING_STARTED.md", "guide/README.md",
        "guide/QUICKSTART_2MIN.md", "guide/HOST_SUPPORT_MATRIX.md"]


def _registered(monkeypatch, slim):
    monkeypatch.setenv("LLM_ROUTER_SLIM", slim)
    import importlib
    import llm_router.server as server
    server = importlib.reload(server)
    return len(asyncio.run(server.mcp.list_tools()))


def test_premise_the_counts_are_what_we_think(monkeypatch):
    from llm_router.tool_tiers import CONSOLIDATED_TOOLS
    assert _registered(monkeypatch, "off") > len(CONSOLIDATED_TOOLS) > 0


@pytest.mark.parametrize("doc", DOCS)
def test_stated_totals_match_registration(monkeypatch, doc):
    total = _registered(monkeypatch, "off")
    text = (REPO / doc).read_text(encoding="utf-8")
    stated = {int(n) for n in re.findall(r"\b(\d{2,3}) (?:MCP )?tools\b", text)}
    assert stated <= {total}, f"{doc} states {sorted(stated)} tools; the server registers {total}"


@pytest.mark.parametrize("doc", DOCS)
def test_stated_front_door_count_matches(doc):
    from llm_router.tool_tiers import CONSOLIDATED_TOOLS
    text = (REPO / doc).read_text(encoding="utf-8")
    stated = {int(n) for n in re.findall(r"\b(\d{1,2})\s+front-door\b", text)}
    assert stated <= {len(CONSOLIDATED_TOOLS)}, (doc, sorted(stated), len(CONSOLIDATED_TOOLS))
