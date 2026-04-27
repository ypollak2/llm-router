#!/usr/bin/env python3
# llm-router-hook-version: 2
"""PostToolUse[Agent] hook — intercept agent failures and suggest fallbacks.

When an Agent tool call fails (timeout, OOM, unparseable output), this hook:
1. Reads the most recent agent call from agent_calls.json
2. Classifies the failure type (resource limit, parse error, etc.)
3. Determines if the task was retrieval or reasoning
4. Suggests an appropriate fallback:
   - Retrieval failed → use Read/Grep/Glob MCP tools
   - Reasoning failed → use llm_* MCP tools
   - Resource limit → break task into smaller chunks

The suggestion is offered to the user; fallback is not automatic.
Explore agents are exempt (pure retrieval, rarely fails).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path


# ── Retrieval detection ──────────────────────────────────────────────────────
# Patterns that indicate a task is primarily retrieval/file-access oriented.
# These are high-confidence signals: find, search, list, read, grep, glob, etc.
# Pattern is flexible to match "find all [adjective] files", "search for X", etc.

_RETRIEVAL_INTENT = re.compile(
    r"\b(?:"
    # Primary retrieval verbs
    r"find (?:all )?[\w\s]*?(?:files?|classes?|functions?|methods?|patterns?|"
    r"references?|usages?|imports?|calls?|definitions?|symbols?)|"
    r"search (?:for )?|grep|"
    r"list (?:all )?[\w\s]*?(?:files?|directories?|modules?|classes?|functions?)|"
    r"locate|discover|scan|inventory|browse|"
    # Reading/accessing
    r"read (?:the )?(?:file|content|source|code|body|text)|"
    r"get (?:the )?(?:content|text|source|code|body)|"
    r"show (?:me )?(?:the )?(?:files?|structure|code|content)|"
    r"display (?:the )?(?:files?|content|code)|"
    # Exploring
    r"explore (?:the )?(?:codebase|directory|repo|structure|files?)|"
    r"map (?:the )?(?:codebase|dependencies|structure)|"
    # Direct tool names
    r"glob|which (?:files?|paths?)|where (?:is|are)|what files?"
    r")\b",
    re.IGNORECASE,
)

# Patterns that indicate analysis or reasoning (not pure retrieval)
_REASONING_INTENT = re.compile(
    r"\b(?:analyze|explain|understand|review|fix|debug|optimize|refactor|"
    r"implement|design|architect|evaluate|assess|compare|recommend|should|why)\b",
    re.IGNORECASE,
)


# ── Failure classification ───────────────────────────────────────────────────

def _read_last_agent_call() -> dict | None:
    """Get the most recent agent call from history."""
    calls_file = Path.home() / ".llm-router" / "agent_calls.json"
    try:
        data = json.loads(calls_file.read_text())
        calls = data.get("calls", [])
        return calls[-1] if calls else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _classify_failure(error_output: str) -> str:
    """Classify failure type from error message.
    
    Returns one of: resource_limit, unparseable_output, resource_crash, unknown_failure
    """
    lower_err = error_output.lower()
    
    # Resource limits (timeout, quota exceeded, etc.)
    if any(x in lower_err for x in ["timeout", "timed out", "exceeded", "limit", "duration"]):
        return "resource_limit"
    
    # Unparseable output (JSON/format errors)
    if any(x in lower_err for x in ["json", "parse", "invalid", "syntax", "malformed"]):
        return "unparseable_output"
    
    # Memory/crash errors
    if any(x in lower_err for x in ["memory", "oom", "segfault", "killed", "crash"]):
        return "resource_crash"
    
    # Generic failure (no pattern matched)
    return "unknown_failure"


def _is_retrieval_intent(prompt: str) -> bool:
    """Heuristic: is this primarily a retrieval task (vs reasoning)?
    
    Returns True if prompt has strong retrieval signals AND no strong reasoning signals.
    """
    has_retrieval = bool(_RETRIEVAL_INTENT.search(prompt))
    has_reasoning = bool(_REASONING_INTENT.search(prompt))
    
    # Pure retrieval if has retrieval intent and no reasoning intent
    return has_retrieval and not has_reasoning


def _task_type_from_prompt(prompt: str) -> str:
    """Infer task type from prompt to give better suggestions.
    
    Returns: 'retrieval', 'analysis', 'code', 'generate', or 'unknown'
    """
    prompt_lower = prompt.lower()
    
    # Code-related tasks
    if any(x in prompt_lower for x in ["implement", "write", "function", "code", "class", "fix", "bug", "debug"]):
        return "code"
    
    # Content generation
    if any(x in prompt_lower for x in ["write", "draft", "generate", "create", "summarize", "document"]):
        return "generate"
    
    # Analysis/reasoning
    if any(x in prompt_lower for x in ["analyze", "explain", "evaluate", "assess", "review", "audit"]):
        return "analysis"
    
    # Retrieval
    if _is_retrieval_intent(prompt):
        return "retrieval"
    
    return "unknown"


def _get_fallback_suggestion(last_call: dict | None, failure_type: str) -> str | None:
    """Generate fallback suggestion based on agent type, task type, and failure.
    
    Decision order:
    1. If Explore agent → no suggestion (pure retrieval, rarely fails)
    2. If retrieval task → suggest Read/Grep/Glob (best for file access, priority over failure type)
    3. Then check failure type for reasoning tasks:
       - resource_limit → break into chunks
       - unparseable → use reliable MCP tool
       - resource_crash → reduce scope
       - unknown → task-appropriate MCP tool
    
    Returns a helpful suggestion string, or None if no suggestion is appropriate.
    """
    if last_call is None:
        return None
    
    subagent_type = last_call.get("subagent_type", "").lower()
    prompt = last_call.get("prompt", "")
    task_type = _task_type_from_prompt(prompt)
    
    # Explore agents are pure retrieval; they rarely fail so no suggestion needed
    if subagent_type == "explore":
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PRIORITY 1: RETRIEVAL FAILURES → always suggest Read/Grep/Glob
    # ═══════════════════════════════════════════════════════════════════════════
    # Retrieval tasks (find, search, list, read) should use direct file-access tools
    # instead of agents. Recommend this regardless of the failure type.
    if _is_retrieval_intent(prompt):
        return (
            "[llm-router] Agent failed to retrieve file/code data. "
            "Use these file-access tools instead (faster and more reliable):\n\n"
            "  • Glob: Find files by filename pattern\n"
            "  • Grep: Search file contents\n"
            "  • Read: Get file contents directly\n\n"
            "These bypass the agent and access the filesystem directly."
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PRIORITY 2: REASONING FAILURES → check failure type
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Resource limit failures: suggest breaking into chunks
    if failure_type == "resource_limit":
        tools_suggestion = ""
        if task_type == "code":
            tools_suggestion = "  • llm_code: Handles code tasks efficiently\n"
        elif task_type == "analysis":
            tools_suggestion = "  • llm_analyze: Better at scoped analysis\n"
        elif task_type == "generate":
            tools_suggestion = "  • llm_generate: Optimized for writing\n"
        
        return (
            "[llm-router] Agent hit a resource limit (timeout, quota, etc.). "
            "Try one of these:\n\n"
            "  1. Break the task into smaller chunks (fewer files, focused scope)\n"
            "  2. Try these MCP tools (better at resource management):\n"
            f"{tools_suggestion}"
            "  3. Be more specific in your prompt to reduce complexity"
        )
    
    # UNPARSEABLE OUTPUT: agent bug, use reliable MCP tools
    if failure_type == "unparseable_output":
        return (
            "[llm-router] Agent output couldn't be parsed (likely agent bug). "
            "Try a reliable MCP tool instead:\n\n"
            f"  Based on your task ('{task_type}'), try:\n"
            "  • llm_code: For code tasks\n"
            "  • llm_analyze: For analysis and reasoning\n"
            "  • llm_query: For simple questions\n\n"
            "These return well-structured, parseable output."
        )
    
    # RESOURCE CRASH: too much work for agent
    if failure_type == "resource_crash":
        return (
            "[llm-router] Agent crashed (memory/process killed). "
            "The task may be too large. Try:\n\n"
            "  1. Reduce scope (fewer files, simpler prompt)\n"
            "  2. Break into multiple smaller steps\n"
            "  3. Use MCP tools (better memory management):\n"
            "     • llm_analyze: For analysis\n"
            "     • llm_code: For code work\n"
            "     • llm_query: For simple lookups"
        )
    
    # GENERIC FAILURE: pick tool based on task type
    if task_type == "code":
        return (
            "[llm-router] Agent failed. For code tasks, try llm_code MCP tool:\n\n"
            "  llm_code(prompt=\"...\")\n\n"
            "This routes to the best code model and handles scope better."
        )
    elif task_type == "analysis":
        return (
            "[llm-router] Agent failed. For analysis tasks, try llm_analyze MCP tool:\n\n"
            "  llm_analyze(prompt=\"...\")\n\n"
            "This provides stronger reasoning for complex tasks."
        )
    elif task_type == "generate":
        return (
            "[llm-router] Agent failed. For writing/generation, try llm_generate MCP tool:\n\n"
            "  llm_generate(prompt=\"...\")\n\n"
            "This is optimized for content creation."
        )
    else:
        # Fallback for unknown task type
        return (
            "[llm-router] Agent failed. Try one of these MCP tools:\n\n"
            "  • llm_query: For simple questions\n"
            "  • llm_analyze: For analysis\n"
            "  • llm_code: For code work\n"
            "  • llm_generate: For writing"
        )


# ── Budget reconciliation ───────────────────────────────────────────────────

def _reconcile_budget(failure_type: str) -> None:
    """Reconcile provisional spend with actual cost on agent completion.

    When an agent fails, we refund a portion of the provisional cost (50%)
    because we didn't deliver value. This prevents budget from being permanently
    locked up on failed attempts.

    On success, provisional spend is treated as final (no adjustment).
    """
    budget_file = Path.home() / ".llm-router" / "session_budget.json"

    try:
        data = json.loads(budget_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return  # Budget not initialized, skip reconciliation

    provisional_spend = float(data.get("provisional_spend", 0.0))
    remaining = float(data.get("remaining", 0.0))

    # On failure, refund 50% of the cost (we wasted that computational effort)
    if failure_type in ("resource_limit", "unparseable_output", "resource_crash", "unknown_failure"):
        refund = provisional_spend * 0.5
        new_remaining = remaining + refund

        # Update budget
        data["remaining"] = new_remaining
        data["provisional_spend"] = max(0.0, provisional_spend - refund)
        data["last_reconciliation"] = time.time()
        data["last_reconciliation_type"] = f"refund_{failure_type}"

        budget_file.write_text(json.dumps(data))

    # On success, no adjustment needed (provisional becomes final)


# ── Main hook ────────────────────────────────────────────────────────────────

def main() -> None:
    """Handle PostToolUse[Agent] events to suggest fallbacks on failure."""
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return  # Can't parse input, ignore
    
    # Only handle Agent tool results
    if hook_input.get("tool_name") != "Agent":
        return
    
    # Extract the tool result (error output on failure)
    tool_result = hook_input.get("tool_result")
    if tool_result is None:
        return  # Success — no output, no error to handle
    
    error_message = str(tool_result)
    
    # Only process if it looks like an error
    if not error_message or "error" not in error_message.lower():
        return
    
    # Read last agent call and classify the failure
    last_call = _read_last_agent_call()
    failure_type = _classify_failure(error_message)

    # Reconcile budget: refund partial cost on failure
    _reconcile_budget(failure_type)

    suggestion = _get_fallback_suggestion(last_call, failure_type)

    # Output suggestion to stdout (Claude Code will display it to the user)
    if suggestion:
        print(json.dumps({
            "suggestion": suggestion,
            "failure_type": failure_type,
            "original_error": error_message[:200],  # Truncate very long errors
            "last_agent_type": last_call.get("subagent_type") if last_call else None,
        }))


if __name__ == "__main__":
    main()
