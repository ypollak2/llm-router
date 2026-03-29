#!/usr/bin/env python3
"""UserPromptSubmit hook — auto-classifies ALL user prompts and injects routing hints.

Reads the user's message from stdin (JSON), runs a fast heuristic classifier,
and returns a routing hint as contextForAgent so Claude knows which llm_* tool
to use without an extra round-trip.

Fast path (~0ms): Uses keyword/pattern heuristics, no LLM call.
The LLM-based classifier runs later inside the MCP tool if needed.

Design: Routes EVERYTHING except truly local shell/git/filesystem operations.
If in doubt, route — the LLM router will pick the best model.
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
    r"marketing copy|tagline|slogan|headline|summary|summarize|"
    r"rewrite|translate|paraphrase)\b",
    re.IGNORECASE,
)

ANALYZE_PATTERNS = re.compile(
    r"\b(analyze|evaluate|assess|review|critique|debug|diagnose|"
    r"explain why|root cause|investigate|audit|compare and contrast|"
    r"pros and cons|trade-?offs?|deep dive|what do you think|"
    r"help me understand|break down|walk me through)\b",
    re.IGNORECASE,
)

CODE_PATTERNS = re.compile(
    r"\b(implement|refactor|write (a |the )?(function|class|module|api|endpoint)|"
    r"code (a |the )?|build (a |the )?|create (a |the )?(script|program|app|service)|"
    r"algorithm|data structure|optimize (the |this )?code|port .+ to|"
    r"fix (the |this |a )?(\w+ )*(bug|error|issue|crash)|"
    r"add (a |the )?(\w+ )*(feature|method|test)|"
    r"update (the |this )?(\w+ )*(code|logic|function)|"
    r"change (the |this )?(\w+ )*(behavior|implementation)|"
    r"modify (the |this )?|improve (the |this )?|extend|enhance|migrate|"
    r"set up|configure|scaffold|boilerplate|template)\b",
    re.IGNORECASE,
)

QUERY_PATTERNS = re.compile(
    r"\b(what is|who is|when did|where is|how does|how do you|how can|"
    r"define|explain|describe|tell me about|what are the|can you|"
    r"difference between|meaning of|why does|why is|is it possible)\b",
    re.IGNORECASE,
)

IMAGE_PATTERNS = re.compile(
    r"\b(generate (an? )?image|create (an? )?(image|picture|illustration|logo|icon)|"
    r"draw|design (a |an )?|visual|artwork|photo of)\b",
    re.IGNORECASE,
)

# ONLY skip truly local shell/filesystem/git operations that Claude handles directly.
# These are operations that don't need any LLM — they're mechanical commands.
SKIP_PATTERNS = re.compile(
    r"^/(route|help|clear|compact|init|login|doctor|memory|model|cost|config|permissions|review|status|mcp|bug)\b|"
    r"^\s*(git |npm |pip |uv |cargo |make |docker |brew |curl |wget |chmod |mkdir |rm |mv |cp |ls |cd |cat |grep )\b|"
    r"^\s*(commit|push|pull|merge|deploy|rebase|stash|cherry-?pick)\b|"
    r"^\s*(show me the |read |open |list files|find file|find class|find function)\b",
    re.IGNORECASE,
)

COMPLEXITY_SIGNALS_COMPLEX = re.compile(
    r"\b(architect|design system|from scratch|end-to-end|comprehensive|"
    r"novel approach|research paper|synthesis|multi-step|workflow|pipeline|"
    r"in-depth|thorough|detailed plan|full implementation|production|"
    r"scalable|distributed|microservice|security audit)\b",
    re.IGNORECASE,
)

COMPLEXITY_SIGNALS_SIMPLE = re.compile(
    r"\b(quick|simple|short|one-liner|brief|what is|how to|define|"
    r"summarize|tldr|eli5|just|only|small|tiny|minor)\b",
    re.IGNORECASE,
)


def classify_prompt(text: str) -> dict | None:
    """Fast heuristic classification. Returns None only for truly local/shell tasks."""
    stripped = text.strip()

    # Skip slash commands and truly local shell operations
    if SKIP_PATTERNS.search(stripped):
        return None

    # Skip very short prompts (likely conversational or confirmations)
    if len(stripped) < 10:
        return None

    # Skip empty / whitespace-only
    if not stripped:
        return None

    # Determine task type — try each pattern
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
    elif QUERY_PATTERNS.search(text):
        task_type = "query"

    # If no pattern matched, default to llm_route (let the LLM classifier decide)
    if task_type is None:
        # Only route if the prompt is substantial enough
        if len(stripped) >= 20:
            task_type = "auto"
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
    "auto": "llm_route",
}


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    prompt = hook_input.get("prompt", "")
    if not prompt:
        sys.exit(0)

    result = classify_prompt(prompt)
    if result is None:
        sys.exit(0)

    task_type = result["task_type"]
    complexity = result["complexity"]
    tool = TOOL_MAP.get(task_type, "llm_route")

    hint = (
        f"[ROUTE: {task_type}/{complexity}] "
        f"Auto-route this task to external LLM. "
        f"Use `{tool}` tool"
        f"{f' with complexity_override={complexity}' if tool == 'llm_route' else ''}. "
        f"Pre-classified as {complexity} {task_type}."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "contextForAgent": hint,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
