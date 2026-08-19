#!/usr/bin/env python3
# llm_router-hook-version: 7
"""PreToolUse[Agent] hook — intercept subagent spawning, route reasoning to cheap models.

When Claude spawns a subagent (Agent tool), this hook intercepts and decides:

  APPROVE → pure retrieval tasks: file reads, searches, directory listings.
            These need local filesystem access — subagents are the right tool.

  BLOCK   → reasoning tasks: analysis, coding, generation, explanation.
            These are routed to the appropriate llm_* MCP tool instead,
            which routes to a cheaper model than Opus.

Pressure-aware profile selection (passed to the MCP tool):
  < 85% quota:
    simple   → profile=budget   (Haiku — much cheaper than Opus)
    moderate → profile=balanced  (Sonnet — cheaper than Opus)
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


# ── Registered-tool surface (CHZ-SURF-01) ────────────────────────────────────
# Tool names are tier-dependent (LLM_ROUTER_SLIM). NEVER put a raw tool name in
# output: under the DEFAULT `consolidated` tier the legacy llm_query /
# llm_analyze / llm_code / llm_research / llm_generate names are not registered,
# so naming one hands the caller "Error: No such tool available" — after which
# it silently does the work on the expensive model and the savings dashboard
# cannot distinguish that from "chose not to route".
def _load_tool_surface_fns():
    """(route_tool, route_call, route_call_with_complexity, call_parts, tool_for_task).

    Falls back to the stdlib-only copy the installer drops next to the hooks, then
    to the in-repo source, then to identity (correct only for tier `off`).
    """
    try:
        from llm_router.tool_surface import (
            call_parts,
            route_call,
            route_call_with_complexity,
            route_tool,
            tool_for_task,
        )
        return route_tool, route_call, route_call_with_complexity, call_parts, tool_for_task
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
            return (_mod.route_tool, _mod.route_call,
                    _mod.route_call_with_complexity, _mod.call_parts,
                    _mod.tool_for_task)
    except Exception:  # noqa: BLE001 — a broken support module must not kill the hook
        pass
    return (
        lambda n, **k: n,
        lambda n, *a, **k: (f"{n}({', '.join(a)})" if a else n),
        lambda n, c, *a, **k: f"{n}(complexity='{c}'" + ("".join(', ' + x for x in a)) + ")",
        lambda n, **k: (n, []),
        # Last-resort fallback mirrors tool_surface.DEFAULT_TASK_TOOL: llm_route,
        # never a completion door — see RED8-06.
        lambda t: {"research": "llm_research", "generate": "llm_generate",
                   "analyze": "llm_analyze", "code": "llm_code",
                   "query": "llm_query", "image": "llm_image",
                   "coordination": "llm_query", "auto": "llm_route"}.get(t, "llm_route"),
    )


(route_tool, route_call,
 route_call_with_complexity, call_parts, tool_for_task) = _load_tool_surface_fns()

# ── .env loader (mirrors auto-route.py) ──────────────────────────────────────
# PreToolUse[Agent] runs without an interactive shell, so OLLAMA_BUDGET_MODELS,
# GEMINI_API_KEY, etc. from ~/.llm-router/.env are not in os.environ unless we load
# them. Without this, build_chain() falls back to its hardcoded default model
# (often not pulled) and DIRECT routing silently degrades to paid/Claude tiers.

_ENV_PATHS = [
    Path.cwd() / ".env",
    Path(__file__).resolve().parent.parent.parent.parent / ".env",  # dev: repo root
    Path.home() / ".llm-router" / ".env",
    Path.home() / ".env",
]


def _load_dotenv() -> None:
    """Load key=value pairs from .env files into os.environ (no override)."""
    for env_path in _ENV_PATHS:
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            pass


_load_dotenv()

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

# RED8-06: the private _TOOL_MAP is gone. It held 5 of the 8 task types and fell
# back to llm_analyze — a COMPLETION DOOR that cannot run tools — while
# auto-route.py fell back to llm_route for the same unrecognised input. The five
# shared keys agreed, so the maps looked consistent; the divergence only showed
# on everything else, and no test drove one prompt through both. The canonical
# map now lives in llm_router.tool_surface (see tool_for_task above).


# ── Agent loop circuit breaker ──────────────────────────────────────────────

def _get_max_depth() -> int:
    """Read LLM_ROUTER_MAX_AGENT_DEPTH from environment, default 3."""
    try:
        return int(os.environ.get("LLM_ROUTER_MAX_AGENT_DEPTH", "3"))
    except (ValueError, TypeError):
        return 3


def _get_session_id() -> str:
    """Return a session identifier unique to THIS Claude Code process.

    Prefers ``CLAUDE_CODE_SESSION_ID`` (set by Claude Code itself, unique per
    running session) over the legacy ~/.llm-router/session_id.txt scheme. That
    file is a single machine-wide singleton written fresh by every session's
    SessionStart hook — so two Claude Code windows running concurrently (e.g.
    one per project) silently share ONE session id, and therefore one agent
    nesting-depth counter. A depth-3 circuit trip in project A then blocks
    project B's very first, unrelated Agent call. CLAUDE_CODE_SESSION_ID is
    genuinely unique per process, so keying on it (see _depth_file below)
    gives each concurrent session its own counter instead.
    """
    env_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if env_session:
        return env_session
    session_file = Path.home() / ".llm-router" / "session_id.txt"
    try:
        return session_file.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def _depth_file(session_id: str) -> Path:
    """Per-session depth-state file — NOT a single shared file.

    Keying the file itself (not just a session_id field inside one shared
    file) means two concurrent sessions never read-modify-write the same
    file, closing the race window entirely rather than relying on a string
    comparison that both processes could pass at once.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return Path.home() / ".llm-router" / f"agent_depth_{safe}.json"


def _read_agent_depth(session_id: str) -> int:
    """Read current agent nesting depth for the given session."""
    try:
        data = json.loads(_depth_file(session_id).read_text())
        return int(data.get("depth", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0


def _write_agent_depth(session_id: str, depth: int) -> None:
    """Persist agent nesting depth for the current session (never below 0)."""
    depth = max(0, depth)
    _depth_file(session_id).write_text(json.dumps({
        "depth": depth,
        "session_id": session_id,
        "ts": time.time(),
    }))


# ── Agent call tracking (for error recovery) ────────────────────────────────

# Inline secret patterns (this file is loaded standalone by Claude Code as a
# hook, so we can't rely on the llm_router package being importable). Mirrors
# llm_router.library.store.scrub_secrets — inline substitution preserves the
# surrounding prompt for error-recovery context while stripping credentials.
# 🥷 Backslash-Security: using vibe-coding rules for Logging & Error Handling
_AGENT_SECRET_PATTERNS = [
    re.compile(r"\b[A-Z][A-Z0-9_]*_(?:API_)?KEY\s*[=:]\s*\S+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
]


def _scrub_agent_prompt(text: str) -> str:
    """Redact common credential patterns from a prompt before persisting."""
    for pat in _AGENT_SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _log_agent_call(subagent_type: str, prompt: str, decision: str) -> None:
    """Log agent call for error recovery tracking.

    Persists to ~/.llm-router/agent_calls.json with a rolling history of last 50 calls.
    Used by PostToolUse[Agent] hook to suggest fallbacks when agents fail.

    Secrets in the prompt are scrubbed before storage and the file is written
    owner-only (0o600) so pasted credentials can't leak to other local users.
    """
    calls_file = Path.home() / ".llm-router" / "agent_calls.json"

    # Read existing history
    history = []
    try:
        data = json.loads(calls_file.read_text())
        history = data.get("calls", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Append new call (scrub secrets before truncating/storing)
    history.append({
        "timestamp": time.time(),
        "subagent_type": subagent_type,
        "prompt": _scrub_agent_prompt(prompt[:500]),  # scrub + truncate
        "decision": decision,
        "session_id": _get_session_id(),
    })

    # Keep last 50 calls only
    history = history[-50:]

    # Write back owner-only.
    # 🥷 Backslash-Security: using vibe-coding rules for File Upload Security
    calls_file.parent.mkdir(parents=True, exist_ok=True)
    calls_file.write_text(json.dumps({
        "calls": history,
        "version": 1,
    }))
    try:
        os.chmod(calls_file, 0o600)
    except OSError:
        pass


# ── Agent cost estimation ───────────────────────────────────────────────────

# WP-03: hoisted out of _estimate_agent_cost (it was rebuilt on every call) and
# split by complexity. These are NOT model rates — they are whole-call USD
# budget guesses keyed by task shape, and none corresponds to any per-token
# price. The pricing lint flagged them anyway, because ("moderate","analyze")
# = 0.80 and ("complex","analyze") = 4.00 happen to spell the retired Haiku
# pair. Splitting by complexity keeps that coincidence out of one node so the
# lint stays credible; the numbers are unchanged.
#
# Worth stating now that they are visible: all ten are hand-estimated and have
# no derivation from real token pricing, so `budget_usd` is being enforced
# against invented figures. Making them real belongs with the escalation-budget
# work (WP-10), not here.
_SIMPLE_TASK_USD = {
    ("simple", "retrieval"): 0.15,
    ("simple", "query"): 0.30,
    ("simple", "code"): 0.20,
}
_MODERATE_TASK_USD = {
    ("moderate", "retrieval"): 0.30,
    ("moderate", "query"): 0.50,
    ("moderate", "code"): 1.00,
    ("moderate", "analyze"): 0.80,
}
_COMPLEX_TASK_USD = {
    ("complex", "code"): 3.00,
    ("complex", "analyze"): 4.00,
    ("complex", "research"): 2.50,
}
_AGENT_COST_ESTIMATES_USD = {
    **_SIMPLE_TASK_USD,
    **_MODERATE_TASK_USD,
    **_COMPLEX_TASK_USD,
}


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
    # Default conservative estimate for unmapped types
    return _AGENT_COST_ESTIMATES_USD.get((complexity, task_type), 1.50)


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

def _route_allowlist() -> set[str]:
    """Subagent types that BYPASS agent-routing (always approved).

    For agents that must do real tool-work — running tests, editing files,
    QA/validation, code review — where redirecting to an ``llm_*`` text call is
    not a substitute. ``Explore`` is always allowed separately.

    Configured via ``LLM_ROUTER_AGENT_ROUTE_ALLOW`` (comma-separated subagent_type
    values). Read from the environment first, then from ``~/.llm-router/.env`` so it
    takes effect without restarting the host. Example:
        LLM_ROUTER_AGENT_ROUTE_ALLOW=code-reviewer,qa,test-runner
    """
    vals = os.environ.get("LLM_ROUTER_AGENT_ROUTE_ALLOW", "").strip()
    if not vals:
        try:
            for line in (Path.home() / ".llm-router" / ".env").read_text().splitlines():
                line = line.strip()
                if line.startswith("LLM_ROUTER_AGENT_ROUTE_ALLOW="):
                    vals = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    return {t.strip() for t in vals.split(",") if t.strip()}


# ── Subagent DIRECT execution (route the work onto a cheap model) ────────────

_COMPLEXITY_RANK = {"simple": 1, "moderate": 2, "complex": 3}


def _govern_run(subagent_type: str, provider: str, model: str,
                in_tok: int, out_tok: int, complexity: str) -> None:
    """Phase 3 — record a routed subagent run as a governed agents/ session.

    Each routed subagent becomes a first-class session in ~/.llm-router/sessions.db
    (visible via llm_router_agent_list / llm_router_agent_check_budget): budget cap = the
    Claude-equivalent baseline it would have spent, consumed = the actual external
    cost. The gap (cap − consumed) is the saving, now auditable at the governance
    layer in addition to the savings log. Fire-and-forget; never breaks routing.
    """
    if os.environ.get("LLM_ROUTER_SUBAGENT_GOVERNANCE", "on").strip().lower() in ("0", "off", "false", "no"):
        return
    try:
        from llm_router.agents.session import SessionStore
        from llm_router.hooks.savings_logger import _baseline_cost, _cost_for
    except Exception:
        return
    try:
        external = _cost_for(provider, model, in_tok, out_tok)
        baseline = _baseline_cost(complexity, in_tok, out_tok)
        cap = baseline if baseline > 0 else max(external, 1e-6)
        store = SessionStore()
        try:
            sess = store.create(
                agent_id=f"subagent:{subagent_type}", budget_usd=cap,
                framework="llm_router-subagent-route",
            )
            store.record_step(sess.session_id, cost_usd=min(external, cap))
            store.complete(sess.session_id)
        finally:
            store.close()
    except Exception:
        pass


def _model_pin_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_SUBAGENT_MODEL_PIN", "on").strip().lower() not in ("0", "off", "false", "no")


def _emit_model_pin(tool_input: dict, model: str) -> None:
    """Phase 4 (Option-A) — approve the spawn but rewrite its model to a cheaper tier.

    Uses Claude Code PreToolUse input rewriting (`updatedInput` under
    `hookSpecificOutput`): the subagent still spawns with the full harness, just on a
    cheaper Claude tier. If the host build ignores `updatedInput`, the `allow` still
    holds and the spawn proceeds on the inherited model — graceful degradation.
    """
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "model": model},
            "permissionDecisionReason": f"[llm_router] model pinned → {model} (lightweight subagent)",
        }
    }, sys.stdout)


# ── llm_router multi-agent (v0.7.0): ALLOW real subagent spawns (cheap tier + routing) ──
# Lets councils / parallel reviews actually run as subagents instead of being
# replaced by a single cheap call. Cost stays bounded via the model pin below and
# the depth circuit-breaker in main(). Default on; LLM_ROUTER_ALLOW_SUBAGENTS=off
# restores the legacy block-and-route behavior.
_SPAWN_MODEL = {"simple": "haiku", "moderate": "sonnet", "complex": "sonnet"}
_MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}


def _spawn_model(complexity: str, caller_model: str | None) -> str:
    """Pick the spawn tier by complexity, but only ever DOWNGRADE relative to a
    model the caller explicitly requested (never spawn a costlier tier)."""
    routed = _SPAWN_MODEL.get(complexity, "sonnet")
    if caller_model and _MODEL_RANK.get(caller_model, 9) < _MODEL_RANK.get(routed, 9):
        return caller_model
    return routed


def _allow_routed_spawn() -> bool:
    return os.environ.get("LLM_ROUTER_ALLOW_SUBAGENTS", "on").strip().lower() not in (
        "0", "off", "false", "no")


_SPAWN_ROUTING_NOTE = (
    "\n\n--- llm_router routing (inherited) ---\n"
    "You are a llm_router-routed subagent. For substantive generation, analysis, "
    f"research, or code synthesis, prefer the llm_router MCP tools ("
    f"{route_tool('llm_query')} / {route_tool('llm_analyze')} / "
    f"{route_tool('llm_code')} / {route_tool('llm_research')}"
    ") over doing the heavy work directly; "
    "use your own file/search tools to gather context and apply concrete edits. "
    "Do NOT spawn further subagents."
)


def _with_routing_note(tool_input: dict) -> dict:
    ti = dict(tool_input)
    p = ti.get("prompt") or ""
    if "llm_router routing (inherited)" not in p:
        ti["prompt"] = p + _SPAWN_ROUTING_NOTE
    return ti


def _try_direct_subagent(
    prompt: str, task_type: str, complexity: str, session_id: str,
    subagent_type: str = "general-purpose",
) -> str | None:
    """Run the subagent's task on a routed cheap model instead of spawning Opus.

    Mirrors the main-session DIRECT path in auto-route.py: build a provider chain
    by complexity+pressure, execute (tool-loop for file work, single-shot for
    Q&A), log savings tagged ``claude_code_subagent``, and return the result text.

    Returns the routed output, or None to fall back to a real spawn. Fire-and-
    forget: any failure returns None so the subagent path stays robust.
    """
    if os.environ.get("LLM_ROUTER_SUBAGENT_DIRECT", "on").strip().lower() in ("0", "off", "false", "no"):
        return None
    # Only DIRECT-execute up to the configured complexity ceiling — bigger work
    # would block the hook too long and is better off as a real (cheaper-tier) spawn.
    max_c = os.environ.get("LLM_ROUTER_SUBAGENT_DIRECT_MAX_COMPLEXITY", "moderate").strip().lower()
    if _COMPLEXITY_RANK.get(complexity, 2) > _COMPLEXITY_RANK.get(max_c, 2):
        return None

    try:
        from llm_router.hooks.chain_builder import (
            build_chain,
            get_current_pressure,
            needs_claude_tools,
        )
        from llm_router.hooks.direct_executor import execute_agent, execute_chain
    except Exception:
        return None

    try:
        zone, _pct = get_current_pressure()
        chain = build_chain(complexity, zone, task_type)
        if not chain:
            return None
        if needs_claude_tools(prompt, task_type):
            result = execute_agent(prompt, chain, timeout=60)  # Ollama tool-loop
        else:
            result = execute_chain(prompt, chain, task_type, timeout=15)
    except Exception:
        return None

    if not result or not (getattr(result, "text", "") or "").strip():
        return None

    # Visible UI signal (Claude Code surfaces PreToolUse stderr to the user).
    if os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no"):
        try:
            sys.stderr.write(
                f"🎯 subagent routed → {result.model.provider}/{result.model.model} "
                f"· {task_type}/{complexity} · {result.latency_ms / 1000.0:.1f}s\n"
            )
        except Exception:
            pass

    # ── SAVINGS: same pipeline as main-session DIRECT, tagged for subagents ──
    try:
        from llm_router.hooks.savings_logger import log_direct_savings, log_direct_to_db
        log_direct_savings(
            result=result, task_type=task_type, complexity=complexity,
            session_id=session_id, host="claude_code_subagent",
        )
        log_direct_to_db(
            result=result, prompt=prompt, task_type=task_type,
            complexity=complexity, classifier_type="agent-route", session_id=session_id,
        )
    except Exception:
        pass

    _govern_run(subagent_type, result.model.provider, result.model.model,
                int(result.input_tokens or 0), int(result.output_tokens or 0), complexity)
    return result.text


def _log_cli_savings(content: str, provider: str, model: str, duration_sec: float,
                     prompt: str, task_type: str, complexity: str, session_id: str) -> None:
    """Log savings for a CLI-delegated subagent run. CLI agents don't report token
    counts, so estimate from text length (chars/4), the same heuristic cc-usage-track
    uses. host=claude_code_subagent_cli keeps delegation savings separately attributable."""
    try:
        from llm_router.hooks.direct_executor import DirectResult, ModelSpec
        from llm_router.hooks.savings_logger import log_direct_savings, log_direct_to_db
        synthetic = DirectResult(
            text=content, model=ModelSpec(provider, model),
            latency_ms=int(duration_sec * 1000),
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(content) // 4),
        )
        log_direct_savings(
            result=synthetic, task_type=task_type, complexity=complexity,
            session_id=session_id, host="claude_code_subagent_cli",
        )
        log_direct_to_db(
            result=synthetic, prompt=prompt, task_type=task_type,
            complexity=complexity, classifier_type="agent-route-cli", session_id=session_id,
        )
    except Exception:
        pass


def _try_cli_delegation(
    prompt: str, task_type: str, complexity: str, session_id: str,
    subagent_type: str = "general-purpose",
) -> str | None:
    """Phase 2 — delegate bigger/tool-heavy subagent work to a real external agent
    CLI (Codex / Gemini CLI) that brings its own toolchain and runs on an external
    subscription (free from Claude quota). Returns the CLI output, or None to fall
    back. Bounded by LLM_ROUTER_SUBAGENT_CLI_TIMEOUT so the hook can't hang.

    Triggers only for tool-needing or complex tasks — a single cheap LLM call
    (the DIRECT tier) already covers simple/moderate Q&A.
    """
    if os.environ.get("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "on").strip().lower() in ("0", "off", "false", "no"):
        return None

    try:
        from llm_router.hooks.chain_builder import needs_claude_tools
    except Exception:
        needs_claude_tools = lambda *_a, **_k: False  # noqa: E731
    if not (needs_claude_tools(prompt, task_type) or complexity == "complex"):
        return None

    # Budget guard: don't delegate if the session's agent budget is spent.
    if _get_remaining_budget() <= 0:
        return None

    try:
        import asyncio

        from llm_router.codex_agent import is_codex_available, run_codex
        from llm_router.gemini_cli_agent import is_gemini_cli_available, run_gemini_cli
    except Exception:
        return None

    timeout = 120
    try:
        timeout = max(15, int(os.environ.get("LLM_ROUTER_SUBAGENT_CLI_TIMEOUT", "120")))
    except (TypeError, ValueError):
        pass

    try:
        if is_codex_available():
            provider = "codex"
            res = asyncio.run(run_codex(prompt, timeout=timeout))
        elif is_gemini_cli_available():
            provider = "gemini-cli"
            res = asyncio.run(run_gemini_cli(prompt, timeout=timeout))
        else:
            return None
    except Exception:
        return None

    if not res or not getattr(res, "success", False) or not (res.content or "").strip():
        return None

    if os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no"):
        try:
            sys.stderr.write(
                f"🎯 subagent delegated → {provider}/{res.model} "
                f"· {task_type}/{complexity} · {res.duration_sec:.1f}s\n"
            )
        except Exception:
            pass

    _log_cli_savings(res.content, provider, res.model, res.duration_sec,
                     prompt, task_type, complexity, session_id)
    _govern_run(subagent_type, provider, res.model,
                max(1, len(prompt) // 4), max(1, len(res.content) // 4), complexity)
    return res.content


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
        if _model_pin_enabled():  # Phase 4: lightweight read/search → Haiku, not Opus
            _emit_model_pin(tool_input, "haiku")
            return
        sys.exit(0)

    # ── Special rule: allowlisted subagent types bypass routing ──────────────
    # Agents that must do real tool-work (run tests, QA, edit files) where an
    # llm_* call is not a substitute. See LLM_ROUTER_AGENT_ROUTE_ALLOW.
    if subagent_type in _route_allowlist():
        _log_agent_call(subagent_type, prompt, "approved_allowlist")
        sys.exit(0)

    # ── Circuit breaker: block if nesting too deep ──────────────────────────
    session_id = _get_session_id()
    current_depth = _read_agent_depth(session_id)
    max_depth = _get_max_depth()

    if current_depth >= max_depth:
        # Active alert: a runaway-nesting breaker trip should page ops,
        # not just silently block. Guarded so the hook never breaks.
        # stdout is the hook's JSON decision channel — structlog's default
        # sink is stdout, so redirect any alert logging to stderr to keep
        # the decision payload parseable.
        try:
            import contextlib

            from llm_router.alerts import RUNAWAY_BREAKER_TRIP, emit_alert
            with contextlib.redirect_stdout(sys.stderr):
                emit_alert(
                    RUNAWAY_BREAKER_TRIP,
                    detail={"session_id": session_id, "depth": current_depth, "max_depth": max_depth},
                )
        except Exception:
            pass
        result = {
            "decision": "block",
            "reason": (
                f"[llm_router] Agent loop circuit breaker: depth {current_depth}/{max_depth}. "
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
        if _model_pin_enabled():  # Phase 4: pure retrieval → Haiku, not Opus
            _emit_model_pin(tool_input, "haiku")
            return
        sys.exit(0)

    # ── Classify reasoning task ──────────────────────────────────────────────
    task_type = _classify_task_type(prompt)
    complexity = _classify_complexity(prompt)

    # ── llm_router multi-agent: ALLOW a real spawn on a cheap tier + inherit routing ─
    # (depth breaker above already bounds nesting; cheap model bounds cost). This
    # is what makes councils / parallel reviews possible instead of collapsing
    # every subagent into a single cheap call.
    if _allow_routed_spawn():
        model = _spawn_model(complexity, tool_input.get("model"))
        _log_agent_call(subagent_type, prompt, "allowed_routed_spawn")
        _emit_model_pin(_with_routing_note(tool_input), model)
        return

    # ── DIRECT subagent execution: route the work onto a cheap model ─────────
    # Instead of merely blocking with advice, actually run the task on the
    # routed chain and hand the result back as the subagent's output. Savings
    # are logged (host=claude_code_subagent). Falls through on any failure.
    _routed = _try_direct_subagent(prompt, task_type, complexity, session_id, subagent_type)
    if _routed is not None:
        _write_agent_depth(session_id, current_depth)  # roll back: no real spawn happened
        _log_agent_call(subagent_type, prompt, "routed_direct")
        json.dump({
            "decision": "block",
            "reason": (
                "[llm_router] Subagent task was executed by a routed model (not spawned); "
                "savings logged. Use this result directly as the subagent's output — "
                "do not re-do the work:\n\n" + _routed
            ),
        }, sys.stdout)
        return

    # ── Phase 2: CLI delegation for bigger/tool-heavy work ───────────────────
    # What DIRECT didn't take (tool tasks, complex work) goes to a real external
    # agent CLI (Codex / Gemini) running on an external subscription. Savings
    # logged (host=claude_code_subagent_cli). Falls through on any failure.
    _delegated = _try_cli_delegation(prompt, task_type, complexity, session_id, subagent_type)
    if _delegated is not None:
        _write_agent_depth(session_id, current_depth)  # roll back: no real spawn happened
        _log_agent_call(subagent_type, prompt, "routed_cli_delegation")
        json.dump({
            "decision": "block",
            "reason": (
                "[llm_router] Subagent task was delegated to an external agent CLI "
                "(Codex/Gemini); savings logged. Use this result directly as the "
                "subagent's output — do not re-do the work:\n\n" + _delegated
            ),
        }, sys.stdout)
        return

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
                f"[llm_router] Agent would exceed session budget.\n\n"
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
                f"[llm_router] Agent estimated cost exceeds per-agent limit.\n\n"
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
    tool = tool_for_task(task_type)

    _model_hint = {
        "budget": "Gemini Flash / Groq (session pressure — cheap external)",
        "balanced": "GPT-4o / Gemini Pro (quota pressure — external)",
        "premium": "Opus via subscription (no API cost — quota available)",
    }
    model_hint = _model_hint.get(profile, profile)

    # Agentic model pin (v0.5.5): when LLM_ROUTER_AGENTIC_MODEL is set, the router
    # leads agentic/reasoning tasks with it — surface that in the hint so the
    # route indicator reflects the real preferred model.
    _agentic = os.environ.get("LLM_ROUTER_AGENTIC_MODEL", "").strip()
    if _agentic:
        model_hint = f"{_agentic} (agentic pin) → {model_hint}"

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

    stale_note = (
        f"\n  ⚠️  Usage data >30min old — quota thresholds may be inaccurate. "
        f"Run {route_tool('llm_check_usage')}.\n"
    ) if _is_pressure_stale() else ""
    # CHZ-SURF-01: head + pinned args, kept separate so the multi-line call below
    # renders as `llm(\n  task="analyze",\n  prompt=…\n)` and not `llm(task=…)(…)`.
    _call_head, _call_pinned = call_parts(tool)
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
        # CHZ-SURF-01: head and pinned args come from call_parts, NOT route_tool —
        # route_tool already embeds the args (llm(task="analyze")), so using it as
        # the head here would emit the uncallable `llm(task="analyze")(prompt=…)`.
        # `profile=` was ALSO dropped: it is not a parameter of llm_query/analyze/
        # code/research/generate OR of the `llm` door, so every call this block
        # printed was rejected for an unexpected keyword argument on every tier.
        # The profile is already reported above in the "Profile:" line.
        f"     {_call_head}(\n"
        + "".join(f"       {_a},\n" for _a in _call_pinned) +
        f'       prompt="""{prompt_repr}""",\n'
        f"     )\n\n"
        f"  3. Return the tool output as your response — no further work needed.\n\n"
        f"Cost saved: subagent would use Opus for reasoning; {route_tool(tool)} uses {model_hint}."
    )

    result = {
        "decision": "block",
        "reason": block_reason,
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
