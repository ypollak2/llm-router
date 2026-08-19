"""Token amounts on routing indicators.

Executed routes already carry exact input+output tokens (route banner, savings
log, compact_line/statusline via surface_status). For SUGGEST-only routes no
model has run, so the directive shows an ESTIMATE labelled "~N tok".
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    cached = sys.modules.get("auto_route_tok")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_tok", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_tok"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ar():
    return _load()


def test_estimate_prompt_tokens(ar):
    assert ar._estimate_prompt_tokens("") == 0
    assert ar._estimate_prompt_tokens(None) == 0
    assert ar._estimate_prompt_tokens("abcd") == 1          # 4 chars ≈ 1 tok
    assert ar._estimate_prompt_tokens("a" * 400) == 100


def test_suggest_directive_shows_estimated_tokens(tmp_path):
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(tmp_path)
    env["LLM_ROUTER_ENFORCE"] = "suggest"
    env["LLM_ROUTER_DIRECT_EXECUTION"] = "off"
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": "what is the capital of France and Spain combined",
                          "session_id": "tk"}),
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "SUGGESTED" in ctx
    # Estimated, explicitly marked with ~ so it's not read as an exact count.
    assert "~" in ctx and "tok" in ctx
