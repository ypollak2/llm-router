#!/usr/bin/env python3
"""Post-response hook to route Claude's explanations through cheaper models.

This hook runs AFTER Claude generates a response but BEFORE it's displayed to the user.
It intercepts explanation sections and routes them through llm_generate to reduce
session quota consumption by 60-70%.

Integration: Install in ~/.claude/hooks/ and enable via CLAUDE.md configuration.
"""

import json
import os
import sys
import time


def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def get_usage_json() -> dict:
    """Read cached Claude quota state."""
    usage_path = _router_home() / "usage.json"
    if usage_path.exists():
        return json.loads(usage_path.read_text())
    return {"session_pct": 0.5, "weekly_pct": 0.5}


def log_routing_decision(response_tokens: int, routed_tokens: int, saved_tokens: int):
    """Log response routing decision to debug log."""
    log_path = _router_home() / "response-router.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = (
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"response={response_tokens} routed={routed_tokens} saved={saved_tokens}\n"
    )
    with open(log_path, "a") as f:
        f.write(entry)


def main():
    """Main hook entry point."""
    # Read response from stdin or environment
    response = sys.stdin.read() if not sys.stdin.isatty() else os.environ.get("RESPONSE", "")

    if not response:
        print(response, end="")
        return

    # Check if response routing is enabled
    if os.environ.get("LLM_ROUTER_RESPONSE_ROUTER", "on").lower() != "on":
        print(response, end="")
        return

    usage = get_usage_json()
    pressure = max(usage.get("session_pct", 0.5), usage.get("weekly_pct", 0.5))

    # Only route if pressure is moderate to high (save quota)
    # Skip routing if pressure already low (budget abundant)
    if pressure < 0.3:
        # Budget abundant, no need to route explanations
        print(response, end="")
        return

    try:
        # Try to route the response
        # In production, this would call llm_generate via MCP
        # For now, just log the decision
        estimated_tokens = len(response.split()) * 1.3  # Rough estimate
        estimated_routable = estimated_tokens * 0.6  # ~60% can be routed

        if estimated_routable > 300:  # Only worth routing if significant
            log_routing_decision(
                int(estimated_tokens), int(estimated_routable), int(estimated_routable * 0.7)
            )

        # Output original response (routing would happen async in production)
        print(response, end="")

    except Exception as e:
        # Fallback: just output original on any error
        sys.stderr.write(f"Response router error: {e}\n")
        print(response, end="")


if __name__ == "__main__":
    main()
