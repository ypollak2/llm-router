#!/usr/bin/env python3
"""UserPromptSubmit hook — auto-classifies user prompts and injects routing hints.

Reads the user's message from stdin (JSON), runs a fast heuristic classifier,
and returns a routing hint as contextForAgent so Claude knows which llm_* tool
to use without an extra round-trip.

Fast path (~0ms): Uses keyword/pattern heuristics, no LLM call.
The LLM-based classifier runs later inside the MCP tool if needed.
"""

import json
import re
import sys


# ── Heuristic Classifier ────────────────────────────────────────────────────

RESEARCH_PATTERNS = re.compile(
    r"\b(research|latest|current|news|trending|find out|look up|search for|"
    r"what happened|who is|what is the latest|compare .+ (to|with|vs)|"
    r"market analysis|competitive|benchmark)\b",
    re.IGNORECASE,
)

GENERATE_PATTERNS = re.compile(
    r"\b(write|draft|create|compose|generate text|brainstorm|"
    r"blog post|article|email|letter|story|poem|tweet|post|"
    r"marketing copy|tagline|slogan|headline)\b",
    re.IGNORECASE,
)

ANALYZE_PATTERNS = re.compile(
    r"\b(analyze|evaluate|assess|review|critique|debug|diagnose|"
    r"explain why|root cause|investigate|audit|compare and contrast|"
    r"pros and cons|trade-?offs?|deep dive)\b",
    re.IGNORECASE,
)

CODE_PATTERNS = re.compile(
    r"\b(implement|refactor|write (a |the )?(function|class|module|api|endpoint)|"
    r"code (a |the )?|build (a |the )?|create (a |the )?(script|program|app|service)|"
    r"algorithm|data structure|optimize (the |this )?code|port .+ to)\b",
    re.IGNORECASE,
)

QUERY_PATTERNS = re.compile(
    r"\b(what is|who is|when did|where is|how does|how do you|"
    r"define|explain|describe|tell me about|what are the|"
    r"difference between|meaning of)\b",
    re.IGNORECASE,
)

IMAGE_PATTERNS = re.compile(
    r"\b(generate (an? )?image|create (an? )?(image|picture|illustration|logo|icon)|"
    r"draw|design (a |an )?|visual|artwork|photo of)\b",
    re.IGNORECASE,
)

# Signals that the user wants Claude to act directly (NOT route externally)
LOCAL_PATTERNS = re.compile(
    r"\b(edit |fix |change |update |modify |delete |remove |add .+ to |"
    r"commit|push|pull|merge|deploy|run |install |test |lint |"
    r"read |open |show me |list |find (file|class|function)|"
    r"git |npm |pip |uv |cargo |make |docker )\b",
    re.IGNORECASE,
)

COMPLEXITY_SIGNALS_COMPLEX = re.compile(
    r"\b(architect|design system|from scratch|end-to-end|comprehensive|"
    r"novel approach|research paper|synthesis|multi-step|workflow|pipeline|"
    r"in-depth|thorough|detailed plan)\b",
    re.IGNORECASE,
)

COMPLEXITY_SIGNALS_SIMPLE = re.compile(
    r"\b(quick|simple|short|one-liner|brief|what is|how to|define|"
    r"summarize|tldr|eli5|just|only)\b",
    re.IGNORECASE,
)


def classify_prompt(text: str) -> dict | None:
    """Fast heuristic classification. Returns None if no clear routing signal."""
    # Skip if it looks like a local/direct task
    if LOCAL_PATTERNS.search(text):
        return None

    # Skip very short prompts (likely conversational)
    if len(text.strip()) < 15:
        return None

    # Determine task type
    task_type = None
    if IMAGE_PATTERNS.search(text):
        task_type = "image"
    elif RESEARCH_PATTERNS.search(text):
        task_type = "research"
    elif CODE_PATTERNS.search(text):
        task_type = "code"
    elif ANALYZE_PATTERNS.search(text):
        task_type = "analyze"
    elif GENERATE_PATTERNS.search(text):
        task_type = "generate"

    if task_type is None:
        if QUERY_PATTERNS.search(text):
            task_type = "query"
        else:
            return None

    # Determine complexity
    if COMPLEXITY_SIGNALS_COMPLEX.search(text):
        complexity = "complex"
    elif COMPLEXITY_SIGNALS_SIMPLE.search(text):
        complexity = "simple"
    elif len(text) > 500:
        complexity = "complex"
    elif len(text) > 150:
        complexity = "moderate"
    else:
        complexity = "simple" if task_type == "query" else "moderate"

    return {"task_type": task_type, "complexity": complexity}


# ── Tool Mapping ─────────────────────────────────────────────────────────────

TOOL_MAP = {
    "research": "llm_research",
    "generate": "llm_generate",
    "analyze": "llm_analyze",
    "code": "llm_code",
    "query": "llm_query",
    "image": "llm_image",
}


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    # Extract user message from the hook payload
    # UserPromptSubmit provides: { "session_id", "prompt", ... }
    prompt = hook_input.get("prompt", "")
    if not prompt:
        sys.exit(0)

    result = classify_prompt(prompt)
    if result is None:
        # No clear routing signal — let Claude handle normally
        sys.exit(0)

    task_type = result["task_type"]
    complexity = result["complexity"]
    tool = TOOL_MAP.get(task_type, "llm_route")

    hint = (
        f"[ROUTE: {task_type}/{complexity}] "
        f"This task matches external LLM routing. "
        f"Recommended: use `{tool}` tool"
        f"{f' with complexity_override={complexity}' if tool == 'llm_route' else ''}. "
        f"The user's prompt has been pre-classified as {complexity} {task_type}."
    )

    # Output hook response with context injection
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "contextForAgent": hint,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
