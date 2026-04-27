#!/usr/bin/env python3
# llm-router-hook-version: 2
"""PreToolUse[Agent] hook — intercept subagent spawning, route reasoning to cheap models.

When Claude spawns a subagent (Agent tool), this hook intercepts and decides:

  APPROVE → pure retrieval tasks: file reads, searches, directory listings.
            These need local filesystem access — subagents are the right tool.

  BLOCK   → reasoning tasks: analysis, coding, generation, explanation.
            These are routed to the appropriate llm_* MCP tool instead,
            which routes to a model 10-50x cheaper than Opus.

Pressure-aware profile selection (passed to the MCP tool):
  < 85% quota:
    simple   → profile=budget   (Haiku — 50x cheaper than Opus)
    moderate → profile=balanced  (Sonnet — 10x cheaper)
    complex  → profile=premium   (Opus — best quality, full quota available)
  ≥ 85% quota:
    simple   → profile=budget   (cheapest external: Gemini Flash, Groq)
    moderate → profile=balanced  (DeepSeek, GPT-4o)
    complex  → profile=balanced  (same — at high pressure premium = balanced cost)

Note: Explore subagent type is always approved (pure retrieval by design).
Note: Mixed tasks (read files then analyze) are blocked; Claude is instructed
      to read files with local tools then pass content to the MCP tool.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# ── Agent resource limits ────────────────────────────────────────────────────

AGENT_MAX_COST_USD = 5.0            # Hard per-agent cost limit
SESSION_MAX_COST_USD = 50.0         # Hard per-session cost limit (fallback)
SOFT_BUDGET_FACTOR = 0.8            # Warn if cost > 80% of remaining budget

# ── Retrieval detection (approve subagent) ───────────────────────────────────
# These signal that the subagent's job is FINDING/READING, not REASONING.
# If these dominate and no reasoning verbs are present → approve.

_RETRIEVAL_INTENT = re.compile(
    r"\b(?:find (?:all |every |any )?(?:files?|classes?|functions?|methods?|patterns?|"
    r"references?|usages?|imports?|calls?|definitions?|symbols?)|"
    r"search (?:for|through|across)|"
    r"list (?:all |every )?(?:files?|directories?|modules?|classes?|functions?)|"
    r"glob|grep|scan|inventory|discover|locate|"
    r"what files?|which files?|where (?:is|are)|show me (?:the )?(?:files?|structure|list)|"
    r"explore (?:the )?(?:codebase|directory|repo|project|structure)|"
    r"map (?:the )?(?:codebase|dependencies|imports?|structure)|"
    r"read (?:the )?(?:file|files?|content) (?:at|from|of|named?)|"
    r"get (?:the )?(?:content|text|source) (?:of|from))\b",
    re.IGNORECASE,
)

_REASONING_INTENT = re.compile(
    r"\b(?:analyze|analyse|evaluate|assess|explain|describe|summarize|"
    r"implement|write|create|build|generate|draft|"
    r"fix|debug|diagnose|resolve|repair|"
    r"compare|contrast|review|audit|critique|"
    r"optimize|refactor|improve|redesign|"
    r"plan|design|architect|strategy|"
    r"why|how does|what causes|what is (?:wrong|the (?:issue|problem|bug|root cause))|"
    r"identify (?:bugs?|issues?|problems?|patterns?|improvements?)|"
    r"should (?:I|we)|is (?:it |this )?(?:correct|right|good|bad|safe|secure))\b",
    re.IGNORECASE,
)

# ── Complexity signals ───────────────────────────────────────────────────────

_COMPLEX_SIGNALS = re.compile(
    r"\b(?:comprehensive|complete|full|entire|end-to-end|thorough|in-depth|"
    r"detailed|deep dive|all (?:aspects?|parts?|components?|modules?|files?)|"
    r"across (?:the )?(?:codebase|repo|project|all)|"
    r"architecture|system design|multiple|several|various|every|"
    r"production|scalable|critical|security|performance)\b",
    re.IGNORECASE,
)

_SIMPLE_SIGNALS = re.compile(
    r"\b(?:quick|simple|brief|short|just|only|single|one|"
    r"small|minor|tiny|trivial|basic|specific|particular)\b",
    re.IGNORECASE,
)

# ── Task type → MCP tool mapping ─────────────────────────────────────────────

_TASK_SIGNALS: dict[str, re.Pattern] = {
    "code": re.compile(
        r"\b(?:implement|write (?:a |the )?(?:function|class|module|test|script)|"
        r"build|scaffold|refactor|fix (?:the |a )?(?:bug|error|issue|crash)|"
        r"add (?:a )?(?:feature|method|test|endpoint)|"
        r"update (?:the )?(?:code|logic|function)|"
        r"create (?:a )?(?:function|class|module|component|test))\b",
        re.IGNORECASE,
    ),
    "analyze": re.compile(
        r"\b(?:analyze|evaluate|assess|review|audit|critique|debug|diagnose|"
        r"explain|describe|compare|identify (?:issues?|bugs?|problems?|patterns?)|"
        r"root cause|deep dive|how does|why (?:does|is|did)|"
        r"what (?:is|are) (?:the )?(?:issue|problem|bug|pattern|bottleneck)|"
        r"should (?:we|I)|pros? and cons?|trade-?off)\b",
        re.IGNORECASE,
    ),
    "research": re.compile(
        r"\b(?:research|look up|find out|what(?:'s| is) (?:the )?(?:latest|current)|"
        r"what happened|market|trend|news|latest|recent|current state)\b",
        re.IGNORECASE,
    ),
    "generate": re.compile(
        r"\b(?:write (?:a |an |the )?(?:document|readme|changelog|report|email|"
        r"summary|description|comment|docstring)|"
        r"draft|compose|create (?:content|documentation|text))\b",
        re.IGNORECASE,
    ),
    "query": re.compile(
        r"\b(?:what is|what are|how (?:do|does|can|to)|"
        r"where (?:is|are)|when (?:does|did|is)|which|"
        r"tell me|can you explain|define|clarify)\b",
        re.IGNORECASE,
    ),
}

_TOOL_MAP = {
    "code": "llm_code",
    "analyze": "llm_analyze",
    "research": "llm_research",
    "generate": "llm_generate",
    "query": "llm_query",
}


# ── Agent loop circuit breaker ──────────────────────────────────────────────

def _get_max_depth() -> int:
    """Read LLM_ROUTER_MAX_AGENT_DEPTH from environment, default 3."""
    try:
        return int(os.environ.get("LLM_ROUTER_MAX_AGENT_DEPTH", "3"))
    except (ValueError, TypeError):
        return 3


def _get_session_id() -> str:
    """Read current session ID from ~/.llm-router/session_id.txt."""
    session_file = Path.home() / ".llm-router" / "session_id.txt"
    try:
        return session_file.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def _read_agent_depth(session_id: str) -> int:
    """Read current agent nesting depth for the given session.

    If the session ID in agent_depth.json doesn't match, return 0 (new session).
    """
    depth_file = Path.home() / ".llm-router" / "agent_depth.json"
    try:
        data = json.loads(depth_file.read_text())
        if data.get("session_id") != session_id:
            return 0  # New session — reset depth
        return int(data.get("depth", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0


def _write_agent_depth(session_id: str, depth: int) -> None:
    """Persist agent nesting depth for the current session."""
    depth_file = Path.home() / ".llm-router" / "agent_depth.json"
    depth_file.write_text(json.dumps({
        "depth": depth,
        "session_id": session_id,
        "ts": time.time(),
    }))


# ── Agent call tracking (for error recovery) ────────────────────────────────

def _log_agent_call(subagent_type: str, prompt: str, decision: str) -> None:
    """Log agent call for error recovery tracking.
    
    Persists to ~/.llm-router/agent_calls.json with a rolling history of last 50 calls.
    Used by PostToolUse[Agent] hook to suggest fallbacks when agents fail.
    """
    calls_file = Path.home() / ".llm-router" / "agent_calls.json"
    
    # Read existing history
    history = []
    try:
        data = json.loads(calls_file.read_text())
        history = data.get("calls", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    
    # Append new call
    history.append({
        "timestamp": time.time(),
        "subagent_type": subagent_type,
        "prompt": prompt[:500],  # Truncate long prompts
        "decision": decision,
        "session_id": _get_session_id(),
    })
    
    # Keep last 50 calls only
    history = history[-50:]
    
    # Write back
    calls_file.write_text(json.dumps({
        "calls": history,
        "version": 1,
    }))


# ── Agent cost estimation ───────────────────────────────────────────────────

def _estimate_agent_cost(complexity: str, task_type: str) -> float:
    """Estimate agent call cost in USD based on complexity and task type.
    
    Base rates (conservative upper estimates):
    - simple/retrieval: $0.15
    - simple/query: $0.30
    - simple/code: $0.20
    - moderate/retrieval: $0.30
    - moderate/query: $0.50
    - moderate/code: $1.00
    - moderate/analyze: $0.80
    - complex/code: $3.00
    - complex/analyze: $4.00
    - complex/research: $2.50
    
    Returns conservative estimate to avoid budget surprises.
    """
    rates = {
        ("simple", "retrieval"): 0.15,
        ("simple", "query"): 0.30,
        ("simple", "code"): 0.20,
        ("moderate", "retrieval"): 0.30,
        ("moderate", "query"): 0.50,
        ("moderate", "code"): 1.00,
        ("moderate", "analyze"): 0.80,
        ("complex", "code"): 3.00,
        ("complex", "analyze"): 4.00,
        ("complex", "research"): 2.50,
    }
    # Default conservative estimate for unmapped types
    return rates.get((complexity, task_type), 1.50)


def _initialize_session_budget() -> float:
    """Initialize session budget if not already done.

    Creates ~/.llm-router/session_budget.json with initial budget based on
    quota pressure. Called once per session to set up provisional tracking.

    Returns the initial budget in USD.
    """
    budget_file = Path.home() / ".llm-router" / "session_budget.json"

    # If already initialized this session, return existing
    if budget_file.exists():
        try:
            data = json.loads(budget_file.read_text())
            if data.get("session_id") == _get_session_id():
                return float(data.get("initial", 30.0))
        except (json.JSONDecodeError, ValueError):
            pass

    # Calculate initial budget based on quota pressure
    pressure = _get_claude_pressure()
    # Allocate 30% of available budget to agents this session
    # This prevents a single session from consuming entire weekly quota
    base_budget = 30.0
    allocated = base_budget * (1.0 - pressure)
    initial_budget = max(5.0, allocated)  # Minimum $5 always allocated

    budget_file.write_text(json.dumps({
        "session_id": _get_session_id(),
        "initial": initial_budget,
        "remaining": initial_budget,
        "provisional_spend": 0.0,
        "timestamp": time.time(),
    }))

    return initial_budget


def _decrement_budget_provisional(estimated_cost: float) -> None:
    """Decrement remaining budget provisionally when agent is approved.

    This prevents multiple agents from each thinking they have budget available.
    Provisional spend will be reconciled against actual cost when agent completes.
    """
    budget_file = Path.home() / ".llm-router" / "session_budget.json"

    try:
        data = json.loads(budget_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _initialize_session_budget()
        data = json.loads(budget_file.read_text())

    remaining = float(data.get("remaining", 30.0))
    provisional = float(data.get("provisional_spend", 0.0))

    # Decrement remaining by estimated cost
    new_remaining = max(0.0, remaining - estimated_cost)
    new_provisional = provisional + estimated_cost

    data["remaining"] = new_remaining
    data["provisional_spend"] = new_provisional
    data["timestamp"] = time.time()

    budget_file.write_text(json.dumps(data))


def _get_remaining_budget() -> float:
    """Get remaining session budget in USD.

    Priority:
      1. ~/.llm-router/session_budget.json (provisional tracking)
      2. Infer from usage.json (session % remaining)
      3. Conservative default $10 (assume 1/3 remaining)

    Returns a float >= 0.0 representing remaining budget in USD.
    """
    # Layer 1: Session budget file (tracking provisional spend)
    budget_file = Path.home() / ".llm-router" / "session_budget.json"
    try:
        data = json.loads(budget_file.read_text())
        if "remaining" in data:
            remaining = float(data.get("remaining", 0.0))
            return max(0.0, remaining)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Layer 2: Infer from usage pressure
    session_pct = _get_claude_pressure()  # 0.0–1.0
    # Assume $30 typical session budget
    session_budget = 30.0
    spent = session_budget * session_pct
    remaining = max(0.0, session_budget - spent)
    return remaining


# ── Session pressure ─────────────────────────────────────────────────────────

def _get_claude_pressure() -> float:
    """Read Claude quota pressure from cache file or SQLite DB.

    Priority:
      1. ~/.llm-router/usage.json  — written by llm_update_usage, fastest
      2. ~/.llm-router/usage.db    — SQLite claude_usage table, authoritative
      3. Conservative default 0.3  — never assume unlimited quota when blind

    Returns a fraction 0.0–1.0.
    """
    # Layer 1: fast JSON cache
    usage_path = Path.home() / ".llm-router" / "usage.json"
    try:
        data = json.loads(usage_path.read_text())
        if "highest_pressure" in data:
            return float(data["highest_pressure"])
        session_pct = data.get("session_pct", 0.0) / 100.0
        weekly_pct = data.get("weekly_pct", 0.0) / 100.0
        return max(session_pct, weekly_pct)
    except Exception:
        pass

    # Layer 2: SQLite fallback — reads most recent claude_usage row
    db_path = Path.home() / ".llm-router" / "usage.db"
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=1)
        row = conn.execute(
            "SELECT messages_used, messages_limit FROM claude_usage "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[1] and row[1] > 0:
            return min(1.0, row[0] / row[1])
    except Exception:
        pass

    # Layer 3: conservative default — don't assume full quota when blind
    return 0.3


def _is_pressure_stale(max_age_seconds: int = 1800) -> bool:
    """Return True if usage.json is missing or older than 30 minutes."""
    usage_path = Path.home() / ".llm-router" / "usage.json"
    if not usage_path.exists():
        return True
    return (time.time() - usage_path.stat().st_mtime) > max_age_seconds


# ── Classifiers ───────────────────────────────────────────────────────────────

def _is_retrieval_only(prompt: str) -> bool:
    """True if the task is pure file/symbol retrieval with no reasoning required."""
    has_retrieval = bool(_RETRIEVAL_INTENT.search(prompt))
    has_reasoning = bool(_REASONING_INTENT.search(prompt))
    # Approve only when clearly retrieval AND no analysis intent detected
    return has_retrieval and not has_reasoning


def _classify_complexity(prompt: str) -> str:
    # Explicit complex signals or very long prompt → complex
    if _COMPLEX_SIGNALS.search(prompt) or len(prompt) > 500:
        return "complex"
    # Only downgrade to simple if there are explicit simple signals
    # AND the prompt is genuinely short (don't let small prompts sneak by)
    if _SIMPLE_SIGNALS.search(prompt) and len(prompt) < 80:
        return "simple"
    return "moderate"


def _classify_task_type(prompt: str) -> str:
    """Return the best-matching task type for the subagent prompt."""
    scores: dict[str, int] = {}
    for task, pattern in _TASK_SIGNALS.items():
        matches = pattern.findall(prompt)
        scores[task] = len(matches)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] > 0 else "analyze"


def _complexity_to_profile(complexity: str, session: float, sonnet: float, weekly: float) -> str:
    """Map complexity + per-bucket pressure to the appropriate routing profile.

    Cascade rule: higher pressure forces ALL lower complexity tiers external too.
      weekly/session ≥ 95% → everything external (global emergency)
      sonnet         ≥ 95% → simple + moderate external
      session        ≥ 85% → simple only external
    """
    all_external = weekly >= 0.95 or session >= 0.95
    if all_external:
        return "budget" if complexity == "simple" else "balanced"
    if sonnet >= 0.95:
        return "budget" if complexity == "simple" else "balanced"
    if complexity == "simple" and session >= 0.85:
        return "budget"
    return {"simple": "budget", "moderate": "balanced", "complex": "premium"}[complexity]


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)  # approve: can't parse input

    tool_name = hook_input.get("tool_name", "")
    if tool_name != "Agent":
        sys.exit(0)  # approve: not an Agent call

    tool_input = hook_input.get("tool_input", {})
    prompt = tool_input.get("prompt", "").strip()
    subagent_type = tool_input.get("subagent_type", "general-purpose")

    if not prompt:
        sys.exit(0)  # approve: nothing to classify

    # ── Initialize session budget if not already done ──────────────────────────
    _initialize_session_budget()

    # ── Always approve Explore subagents — they're pure retrieval ────────────
    if subagent_type == "Explore":
        _log_agent_call(subagent_type, prompt, "approved_explore")
        sys.exit(0)

    # ── Circuit breaker: block if nesting too deep ──────────────────────────
    session_id = _get_session_id()
    current_depth = _read_agent_depth(session_id)
    max_depth = _get_max_depth()

    if current_depth >= max_depth:
        result = {
            "decision": "block",
            "reason": (
                f"[llm-router] Agent loop circuit breaker: depth {current_depth}/{max_depth}. "
                f"Too many nested agents. Use llm_* MCP tools directly instead."
            ),
        }
        json.dump(result, sys.stdout)
        return

    # Increment depth before approving any non-Explore agent
    _write_agent_depth(session_id, current_depth + 1)

    # ── Detect retrieval-only tasks ──────────────────────────────────────────
    if _is_retrieval_only(prompt):
        _log_agent_call(subagent_type, prompt, "approved_retrieval")
        sys.exit(0)

    # ── Classify reasoning task ──────────────────────────────────────────────
    task_type = _classify_task_type(prompt)
    complexity = _classify_complexity(prompt)
    
    # ── Estimate cost for this agent call ───────────────────────────────────
    estimated_cost = _estimate_agent_cost(complexity, task_type)
    remaining_budget = _get_remaining_budget()
    
    # ── Check resource limits ───────────────────────────────────────────────
    # Soft limit: warn if cost > 80% of remaining budget (informational only)
    soft_limit = remaining_budget * SOFT_BUDGET_FACTOR
    if estimated_cost > soft_limit and remaining_budget > 0:
        # Could log warning here if we had stderr access
        # sys.stderr.write(f"[warning] Agent cost ${estimated_cost:.2f} exceeds soft limit (80% of remaining ${remaining_budget:.2f})\n")
        pass
    
    # Hard limit: block if cost exceeds remaining budget
    if estimated_cost > remaining_budget:
        result = {
            "decision": "block",
            "reason": (
                f"[llm-router] Agent would exceed session budget.\n\n"
                f"  Estimated cost: ${estimated_cost:.2f}\n"
                f"  Remaining budget: ${remaining_budget:.2f}\n\n"
                f"Use llm_* MCP tools instead (typically cheaper and more efficient)."
            ),
        }
        json.dump(result, sys.stdout)
        return
    
    # Hard limit: block if cost exceeds per-agent maximum
    if estimated_cost > AGENT_MAX_COST_USD:
        result = {
            "decision": "block",
            "reason": (
                f"[llm-router] Agent estimated cost exceeds per-agent limit.\n\n"
                f"  Estimated: ${estimated_cost:.2f}\n"
                f"  Per-agent limit: ${AGENT_MAX_COST_USD:.2f}\n\n"
                f"Task is too complex for a single agent. Break it into smaller steps\n"
                f"or use a series of llm_* MCP tool calls."
            ),
        }
        json.dump(result, sys.stdout)
        return

    # ── All limit checks passed: decrement budget provisionally ──────────────────
    # This tracks the estimated cost as "provisional spend" so multiple agents
    # don't all think they have budget available. Will be reconciled on completion.
    _decrement_budget_provisional(estimated_cost)

    # Log the blocked reasoning task call for error recovery tracking
    _log_agent_call(subagent_type, prompt, "blocked_reasoning")
    
    raw_pressure = _get_claude_pressure()  # legacy single value for display

    # Read per-bucket pressure from usage.json for accurate threshold decisions
    _p = {"session": raw_pressure, "sonnet": raw_pressure, "weekly": raw_pressure}
    _usage_path = Path.home() / ".llm-router" / "usage.json"
    try:
        _data = json.loads(_usage_path.read_text())
        def _f(k: str) -> float:
            v = float(_data.get(k, 0.0))
            return v / 100.0 if v > 1.0 else v
        _p = {"session": _f("session_pct"), "sonnet": _f("sonnet_pct"), "weekly": _f("weekly_pct")}
    except Exception:
        pass

    profile = _complexity_to_profile(complexity, _p["session"], _p["sonnet"], _p["weekly"])
    tool = _TOOL_MAP.get(task_type, "llm_analyze")

    _model_hint = {
        "budget": "Gemini Flash / Groq (session pressure — cheap external)",
        "balanced": "GPT-4o / Gemini Pro (quota pressure — external)",
        "premium": "Opus via subscription (no API cost — quota available)",
    }
    model_hint = _model_hint.get(profile, profile)

    pressure_note = ""
    if _p["weekly"] >= 0.95:
        pressure_note = f"  ⚠️  Weekly={_p['weekly']:.0%} — all tiers on external models.\n"
    elif _p["sonnet"] >= 0.95:
        pressure_note = f"  ⚠️  Sonnet={_p['sonnet']:.0%} — moderate/complex on external models.\n"
    elif _p["session"] >= 0.85:
        pressure_note = f"  ⚠️  Session={_p['session']:.0%} — simple tasks on external models.\n"

    # Build the block instruction
    # Use repr() for the prompt so newlines are visible and the instruction is copy-safe
    prompt_repr = prompt[:800] + ("..." if len(prompt) > 800 else "")

    stale_note = "\n  ⚠️  Usage data >30min old — quota thresholds may be inaccurate. Run llm_check_usage.\n" if _is_pressure_stale() else ""
    block_reason = (
        f"[AGENT-ROUTE] Subagent blocked — routing reasoning to cheap model.\n\n"
        f"  Task:       {task_type}/{complexity}\n"
        f"  Est. Cost:  ${estimated_cost:.2f} (remaining: ${remaining_budget:.2f})\n"
        f"  Profile:    {profile} → {model_hint}\n"
        f"  Quota:      session={_p['session']:.0%} sonnet={_p['sonnet']:.0%} weekly={_p['weekly']:.0%}\n"
        f"{pressure_note}"
        f"{stale_note}\n"
        f"ACTION REQUIRED — do this instead of spawning the subagent:\n\n"
        f"  1. If the task needs LOCAL FILE CONTENT:\n"
        f"     Use Read / Grep / Glob tools to extract the text.\n"
        f"     Embed the content directly in the prompt below.\n\n"
        f"  2. Call this MCP tool:\n\n"
        f"     {tool}(\n"
        f'       prompt="""{prompt_repr}""",\n'
        f'       profile="{profile}",\n'
        f"     )\n\n"
        f"  3. Return the tool output as your response — no further work needed.\n\n"
        f"Cost saved: subagent would use Opus for reasoning; {tool} uses {model_hint}."
    )

    result = {
        "decision": "block",
        "reason": block_reason,
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
