"""SubagentStart hook — inject routing context into every new agent's initial messages.

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
from pathlib import Path


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
            "research→llm_research MCP tool"
        )
    else:
        # HIGH / CRITICAL — subscription pressure exceeded, use external providers
        routing_rules = (
            "simple→llm_query (external) | "
            "moderate→llm_analyze (external) | "
            "complex→llm_code (external) | "
            "research→llm_research (external)"
        )

    context = (
        f"[llm-router] Routing context for this agent:\n"
        f"Pressure: session={p['session']:.0%} sonnet={p['sonnet']:.0%} "
        f"weekly={p['weekly']:.0%} | {status}\n"
        f"Rules: {routing_rules}\n"
        f"Your own Agent tool calls are intercepted by the routing hook — respect routing directives."
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
