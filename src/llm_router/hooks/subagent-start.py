"""SubagentStart hook — inject routing context into every new agent's initial messages.
# llm_router-hook-version: 1

Fires once when Claude spawns an agent (Agent tool call completes the PreToolUse
gate and runAgent() starts). The hook's additionalContext is prepended to the
agent's initialMessages so the agent is routing-aware from its very first turn.

Key differences from auto-route (UserPromptSubmit):
  - No prompt text available — cannot classify the task.
  - Cannot block — runAgent.ts only reads additionalContexts, no blocking path.
  - Output field MUST be "additionalContext" (not "contextForAgent") because
    runAgent.ts reads hookResult.additionalContexts directly, it never shows
    raw stdout to the agent.

Hook input:
  { "hook_event_name": "SubagentStart", "agent_id": "...", "agent_type": "..." }

Hook output:
  { "hookSpecificOutput": { "hookEventName": "SubagentStart", "additionalContext": "..." } }

Skips:
  - Explore agents (agent_type == "Explore") — pure retrieval, routing context is noise.
  - When usage.json is missing — exits cleanly, agent starts without context.
"""

from __future__ import annotations

import json
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


# ── Pressure reading ──────────────────────────────────────────────────────────

def _read_pressure() -> dict[str, float]:
    """Read per-bucket Claude subscription pressure from usage.json.

    Returns fractions 0.0–1.0 for each bucket. Defaults to 0.0 on any error
    (conservative: assume no pressure when data is missing).
    """
    usage_path = Path.home() / ".llm-router" / "usage.json"
    try:
        data = json.loads(usage_path.read_text())

        def _norm(k: str) -> float:
            v = float(data.get(k, 0.0))
            return v / 100.0 if v > 1.0 else v

        return {
            "session": _norm("session_pct"),
            "sonnet":  _norm("sonnet_pct"),
            "weekly":  _norm("weekly_pct"),
        }
    except Exception:
        return {"session": 0.0, "sonnet": 0.0, "weekly": 0.0}


def _is_pressure_stale(max_age_seconds: int = 1800) -> bool:
    """Return True if usage.json is missing or older than 30 minutes."""
    usage_path = Path.home() / ".llm-router" / "usage.json"
    if not usage_path.exists():
        return True
    return (time.time() - usage_path.stat().st_mtime) > max_age_seconds


def _pressure_status(p: dict[str, float]) -> str:
    """Classify overall pressure into a human-readable status label."""
    if p["weekly"] >= 0.95 or p["session"] >= 0.95:
        return "CRITICAL"
    if p["sonnet"] >= 0.95 or p["session"] >= 0.85:
        return "HIGH"
    if p["session"] >= 0.60 or p["sonnet"] >= 0.70:
        return "MEDIUM"
    return "LOW"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    agent_type = payload.get("agent_type", "")

    # Explore agents are pure retrieval — routing context adds noise, not value.
    if agent_type == "Explore":
        sys.exit(0)

    p = _read_pressure()
    status = _pressure_status(p)

    # Routing table summary — mirrors CLAUDE.md and auto-route logic.
    if status in ("LOW", "MEDIUM"):
        routing_rules = (
            "simple→Haiku (/model claude-haiku-4-5-20251001) | "
            "moderate→Sonnet (current) | "
            "complex→Opus (/model claude-opus-4-6) | "
            f"research→{route_tool('llm_research')} MCP tool"
        )
    else:
        # HIGH / CRITICAL — subscription pressure exceeded, use external providers
        routing_rules = (
            f"simple→{route_tool('llm_query')} (external) | "
            f"moderate→{route_tool('llm_analyze')} (external) | "
            f"complex→{route_tool('llm_code')} (external) | "
            f"research→{route_tool('llm_research')} (external)"
        )

    stale_note = (
        f"\n⚠️  Usage data >30min old — routing thresholds may be inaccurate. "
        f"Run {route_tool('llm_check_usage')}."
    ) if _is_pressure_stale() else ""
    context = (
        f"[llm_router] Routing context for this agent:\n"
        f"Pressure: session={p['session']:.0%} sonnet={p['sonnet']:.0%} "
        f"weekly={p['weekly']:.0%} | {status}\n"
        f"Rules: {routing_rules}\n"
        f"Your own Agent tool calls are intercepted by the routing hook — respect routing directives."
        f"{stale_note}"
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
