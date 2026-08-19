#!/usr/bin/env python3
# llm_router-hook-version: 1
"""Gemini CLI auto-route hook — injects MANDATORY ROUTE hint before model answers.

This hook fires on UserPromptSubmit (after the user types a prompt, before Gemini
responds). It runs a 3-layer complexity classifier to determine if the task is
simple/moderate/complex, then injects a hint into the system message telling Gemini
which llm_router MCP tool to call.

Usage: Installed at ~/.llm-router/hooks/gemini-cli-auto-route.py by `llm_router install`.
Registered in Gemini CLI's hook config to fire on UserPromptSubmit.

Classification layers:
1. Heuristics (instant, free) — regex patterns for common task types
2. Ollama qwen3.5 (local, free) — cheap local LLM for nuanced classification
3. Gemini Flash (API, ~$0.0001) — fallback when layers 1–2 unavailable
"""

import json
import sys
import asyncio
from typing import Optional


# ── Registered-tool surface (CHZ-SURF-01) ────────────────────────────────────
# Tool names are tier-dependent (LLM_ROUTER_SLIM). NEVER put a raw tool name in
# output: under the DEFAULT `consolidated` tier the legacy llm_query /
# llm_analyze / llm_code / llm_research / llm_generate names are not registered,
# so naming one hands the caller "Error: No such tool available" — after which
# it silently does the work on the expensive model and the savings dashboard
# cannot distinguish that from "chose not to route".
def _load_tool_surface_fns():
    """(route_tool, route_call, route_call_with_complexity) from llm_router.tool_surface.

    Falls back to the stdlib-only copy the installer drops next to the hooks, then
    to the in-repo source, then to identity (correct only for tier `off`).
    """
    try:
        from llm_router.tool_surface import (
            route_call,
            route_call_with_complexity,
            route_tool,
        )
        return route_tool, route_call, route_call_with_complexity
    except ImportError:
        pass
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _here = _P(__file__).resolve().parent
        for _cand in (_here / "llm_router_tool_surface.py", _here.parent / "tool_surface.py"):
            if not _cand.exists():
                continue
            _spec = _ilu.spec_from_file_location("llm_router_tool_surface", _cand)
            _mod = _ilu.module_from_spec(_spec)
            sys.modules["llm_router_tool_surface"] = _mod  # dataclasses needs this
            _spec.loader.exec_module(_mod)
            return _mod.route_tool, _mod.route_call, _mod.route_call_with_complexity
    except Exception:  # noqa: BLE001 — a broken support module must not kill the hook
        pass
    return (
        lambda n, **k: n,
        lambda n, *a, **k: (f"{n}({', '.join(a)})" if a else n),
        lambda n, c, *a, **k: f"{n}(complexity='{c}'" + ("".join(', ' + x for x in a)) + ")",
    )


route_tool, route_call, route_call_with_complexity = _load_tool_surface_fns()


async def classify_complexity(prompt: str) -> tuple[str, float]:
    """Classify task complexity (simple/moderate/complex).

    Returns:
        Tuple of (complexity: str, confidence: float where 1.0 = high confidence)
    """
    # Layer 1: Heuristics (instant, free)
    prompt_lower = prompt.lower()

    # Quick patterns for high-confidence classification
    simple_patterns = [
        r"what is",
        r"explain",
        r"define",
        r"summarize",
        r"list",
        r"how do i",
    ]
    complex_patterns = [
        r"debug",
        r"refactor",
        r"implement",
        r"design",
        r"architecture",
        r"optimize",
        r"analyze",
    ]

    import re

    for pattern in simple_patterns:
        if re.search(pattern, prompt_lower):
            return "simple", 0.9

    for pattern in complex_patterns:
        if re.search(pattern, prompt_lower):
            return "complex", 0.85

    # Layer 2: Try Ollama qwen3.5 (local, cheap)
    try:
        from llm_router.classifier import classify_prompt_heuristic

        result = await classify_prompt_heuristic(prompt)
        if result:
            complexity = result.get("complexity", "moderate")
            confidence = result.get("confidence", 0.6)
            return complexity, confidence
    except Exception:
        pass

    # Layer 3: Fallback to default
    return "moderate", 0.5


async def get_routing_hint(prompt: str) -> Optional[str]:
    """Generate MANDATORY ROUTE hint based on classified complexity.

    Returns:
        A formatted hint string, or None if classification fails.
    """
    try:
        complexity, _confidence = await classify_complexity(prompt)
        prompt_lower = prompt.lower()

        # Heuristic for task type
        is_code = any(kw in prompt_lower for kw in ["code", "script", "function", "refactor", "bug", "implement"])
        is_generate = any(kw in prompt_lower for kw in ["write", "draft", "compose", "blog", "article"])
        is_research = any(kw in prompt_lower for kw in ["latest", "recent", "news", "current events", "research"])

        # Map complexity + task type to recommended tool
        if is_research:
            return f"⚡ MANDATORY ROUTE: research/{complexity} → call {route_call_with_complexity('llm_research', complexity)}"
        
        if is_code:
            return f"⚡ MANDATORY ROUTE: code/{complexity} → call {route_call_with_complexity('llm_code', complexity)}"
            
        if is_generate:
            return f"⚡ MANDATORY ROUTE: generate/{complexity} → call {route_call_with_complexity('llm_generate', complexity)}"

        if complexity == "simple":
            return f"⚡ MANDATORY ROUTE: query/simple → call {route_call_with_complexity('llm_query', 'simple')}"
        elif complexity == "complex":
            return f"⚡ MANDATORY ROUTE: analyze/complex → call {route_call_with_complexity('llm_analyze', 'complex')}"
        else:
            return f"⚡ MANDATORY ROUTE: analyze/moderate → call {route_call_with_complexity('llm_analyze', 'moderate')}"
    except Exception:
        return None


def hook_handler(event_data: dict) -> dict:
    """Handle UserPromptSubmit event from Gemini CLI.

    Gemini CLI calls this hook with event_data containing:
      - prompt: str — the user's prompt
      - context: dict — optional session context

    Returns:
        Modified event_data with system_message or context updated.
    """
    try:
        prompt = event_data.get("prompt", "")
        if not prompt:
            return event_data

        # Run async classification
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            hint = loop.run_until_complete(get_routing_hint(prompt))
        finally:
            loop.close()

        if hint:
            # Inject hint into system message or context
            if "system_message" in event_data:
                event_data["system_message"] = f"{hint}\n\n{event_data['system_message']}"
            elif "context" in event_data and isinstance(event_data["context"], dict):
                event_data["context"]["routing_hint"] = hint
            else:
                event_data["routing_hint"] = hint

        return event_data
    except Exception as e:
        # Never let hook errors break Gemini
        print(f"Auto-route hook error (ignored): {e}", file=sys.stderr)
        return event_data


if __name__ == "__main__":
    # When called directly, expect event JSON on stdin
    try:
        event_data = json.loads(sys.stdin.read())
        result = hook_handler(event_data)
        print(json.dumps(result))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
