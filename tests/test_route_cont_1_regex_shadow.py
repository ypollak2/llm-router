"""ROUTE-CONT-1: the strict-ack and directive continuation patterns must not shadow each other.

`hooks/auto-route.py` once defined `_CONTINUATION_RE` twice (strict ack at ~L2715, broad
directive pattern added by 9a370e69 at ~L3717). The later one won at import, so the
UserPromptSubmit "strict ack" bypass swallowed real directives ("keep going, open the PR
when done") before they were recorded or routed. See docs/bugs/ROUTE-CONT-1.md.
"""

from __future__ import annotations

import ast
import collections
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = [ROOT / "hooks" / "auto-route.py", ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"]
SRC_HOOK = HOOKS[1]
DIRECTIVE = "keep going, open the PR when done"
BYPASS_LINE = "CONTINUATION: bypass to host agent (strict ack)"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("auto_route_route_cont_1", SRC_HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(prompt: str, home: Path) -> str:
    """Run the hook in a scratch HOME; return the debug log text."""
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        LLM_ROUTER_HOME=str(home / ".llm-router"),
        LLM_ROUTER_DISABLE_LLM_CLASSIFIERS="1",
        LLM_ROUTER_DIRECT_EXECUTION="0",
        OPENAI_API_KEY="", GEMINI_API_KEY="", GOOGLE_API_KEY="",
    )
    env.pop("LLM_ROUTER_DISABLE_CONTINUATION_BYPASS", None)
    subprocess.run(
        [sys.executable, str(SRC_HOOK)],
        input=json.dumps({"prompt": prompt, "session_id": "route-cont-1"}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    logs = list((home / ".llm-router").glob("auto-route-debug*.log"))
    return "\n".join(p.read_text() for p in logs)


@pytest.mark.parametrize("ack", ["ok", "yes", "continue", "go ahead", "stop"])
def test_single_word_acks_still_bypass(ack, tmp_path):
    assert BYPASS_LINE in _run(ack, tmp_path)


def test_directive_is_not_bypassed_as_strict_ack(tmp_path):
    log = _run(DIRECTIVE, tmp_path)
    assert "INVOCATION" in log, "check found nothing to check: hook produced no log"
    assert BYPASS_LINE not in log


def test_regex_split(hook):
    assert hook._STRICT_ACK_RE.match("ok")
    assert hook._STRICT_ACK_RE.match(DIRECTIVE) is None
    assert hook._CONTINUATION_DIRECTIVE_RE.match(DIRECTIVE)
    assert not hasattr(hook, "_CONTINUATION_RE")


def test_is_continuation_uses_strict_ack_for_directive_prompts(hook):
    # `_is_continuation` documents "short affirmatives ... carry no new task signal".
    assert hook._is_continuation("ok") is True
    assert hook._STRICT_ACK_RE.match("fix it") is None
    # The one narrowing from strict-only (review of #421): directive-only words no longer count as a
    # continuation by themselves; _is_short_code_followup still catches them after a code task.
    assert hook._is_continuation("push it") is False
    assert hook._is_continuation("retry") is False


def test_directive_call_sites_keep_broad_behaviour(hook):
    # _is_context_dependent (D-1): short bare imperative continuations are context-dependent.
    assert hook._is_context_dependent("keep going") is True
    assert hook._is_context_dependent("fix it") is True
    # _is_short_code_followup: directive after a code task inherits code context.
    assert hook._is_short_code_followup("keep going", {"task_type": "code"}) is True
    assert hook._is_short_code_followup("push it", {"task_type": "code"}) is True


@pytest.mark.parametrize("path", HOOKS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_module_level_name_assigned_twice(path):
    tree = ast.parse(path.read_text())
    counts = collections.Counter(
        t.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(t, ast.Name)
    )
    assert counts, "check found nothing to check"
    assert counts["_STRICT_ACK_RE"] == 1 and counts["_CONTINUATION_DIRECTIVE_RE"] == 1
    assert "_CONTINUATION_RE" not in counts
    dupes = {n: c for n, c in counts.items() if c > 1}
    assert dupes == {} or set(dupes) <= KNOWN_DUPES, dupes


# Pre-existing duplicates outside this defect's scope; the guard blocks new ones.
KNOWN_DUPES: set[str] = set()
