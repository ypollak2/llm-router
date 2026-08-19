"""ENF-FIX-2 (GAP-ENF-2 / INV-ROUTE-006) — context-dependent prompts.

Two things must hold for a prompt that references the user's local repo/state:

1. **Regression lock.** It must never again receive a HARD "call llm_* FIRST and
   ONLY" directive (the GAP-ENF-2 dead-end, where the router's own
   context-dependent detector fired yet a hard research directive still stood).
   Enforcement is suppressed; the directive is advisory. This test pins that.

2. **Provisioned tool-capable door.** When the context-dependent prompt ALSO
   needs local execution / repo ops (ENF-FIX-1's signal), the advisory must name
   the PROVISIONED tool-capable door — ``llm_act(context=…)`` — not a text-only
   door (which cannot run the work even with context). A context-dependent prompt
   that does NOT need execution keeps its text-only suggestion (no over-routing).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _hint(prompt: str) -> str:
    """Invoke the auto-route UserPromptSubmit hook and return its directive text."""
    with tempfile.TemporaryDirectory(prefix="llm_router-enf2-") as home:
        env = dict(os.environ)
        env.update({
            "HOME": home,
            "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
            "LLM_ROUTER_DIRECT_EXECUTION": "0",
            "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "",
        })
        r = subprocess.run([sys.executable, str(HOOK)],
                           input=json.dumps({"prompt": prompt, "session_id": ""}),
                           capture_output=True, text=True, env=env)
        assert r.stdout.strip(), f"hook produced no output for {prompt!r} (stderr: {r.stderr[:400]})"
        out = json.loads(r.stdout)
        if "hookSpecificOutput" in out:
            hso = out["hookSpecificOutput"]
            return hso.get("contextForAgent") or hso.get("additionalContext", "")
        if out.get("decision") == "block":
            return out.get("message", "")
        return json.dumps(out)


# ── 1. Regression lock: context-dependent → advisory, never hard-FIRST ────────

def test_context_dependent_prompt_is_advisory_not_hard_directive():
    hint = _hint("merge #160 once green, then continue the loop")
    assert "context-dependent" in hint.lower()
    # The hard-enforcement directive's signature phrases must be absent.
    assert "FIRST and ONLY" not in hint, "context-dependent prompt must not get a hard directive"
    assert "HARD ENFORCEMENT" not in hint
    # And the advisory framing must be present.
    assert "advisory" in hint.lower() or "Nothing is blocked" in hint


# ── 2. Provisioned door: context-dependent + execution → llm_act(context=…) ───

def test_context_dependent_execution_prompt_names_tool_capable_door():
    """Fail-before: names a text-only door (llm_query/llm_research/llm) for
    'route WITH context'. Pass-after: names llm_act — the provisioned door that
    can actually run the execution work."""
    hint = _hint("merge #160 once green, then run the test suite and commit the result")
    assert "context-dependent" in hint.lower()
    assert "llm_act(context=" in hint, f"execution+context must name the provisioned door: {hint!r}"


def test_context_dependent_nonexecution_prompt_keeps_text_only_suggestion():
    """Guard against over-routing: a context-dependent prompt that does NOT need
    execution must keep its text-only door suggestion, not be pushed to llm_act."""
    hint = _hint("summarize what my repo's README currently says about setup")
    assert "context-dependent" in hint.lower()
    assert "llm_act(context=" not in hint, f"non-execution context prompt must not name llm_act: {hint!r}"
