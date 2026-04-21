#!/usr/bin/env python3
"""Gemini CLI auto-route hook — injects MANDATORY ROUTE hint before model answers.

This hook fires on UserPromptSubmit (after the user types a prompt, before Gemini
responds). It runs a 3-layer complexity classifier to determine if the task is
simple/moderate/complex, then injects a hint into the system message telling Gemini
which llm-router MCP tool to call.

Usage: Installed at ~/.llm-router/hooks/gemini-cli-auto-route.py by `llm-router install`.
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

        # Map complexity to recommended tool
        if complexity == "simple":
            return "⚡ MANDATORY ROUTE: query/simple → call llm_query(complexity='simple')"
        elif complexity == "complex":
            return "⚡ MANDATORY ROUTE: analyze/complex → call llm_analyze(complexity='complex')"
        else:
            return "⚡ MANDATORY ROUTE: analyze/moderate → call llm_analyze(complexity='moderate')"
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
