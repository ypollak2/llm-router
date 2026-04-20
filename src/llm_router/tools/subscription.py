"""Subscription usage tools — llm_check_usage, llm_update_usage, llm_refresh_claude_usage."""

from __future__ import annotations

from llm_router.claude_usage import FETCH_USAGE_JS, parse_api_response
from llm_router import state as _state


async def llm_check_usage() -> str:
    """Check real-time Claude subscription usage (session limits, weekly limits, extra spend).

    Shows cached data if available. If no data cached, returns the JS snippet
    to run via Playwright's browser_evaluate (one call, no page navigation needed).

    The budget pressure from this data feeds directly into model routing —
    higher usage = more aggressive downshifting to cheaper models.
    """
    _last_usage = _state.get_last_usage()
    if _last_usage:
        return _last_usage.summary()

    return (
        "No cached usage data yet.\n\n"
        "**Quick setup** (one Playwright call):\n"
        "1. `browser_navigate` to `https://claude.ai` (any page, just need auth cookies)\n"
        "2. `browser_evaluate` with this JS:\n"
        f"```js\n{FETCH_USAGE_JS}\n```\n"
        "3. Pass the result to `llm_update_usage`\n\n"
        "This fetches the same JSON API that claude.ai's settings page uses."
    )


async def llm_update_usage(data: dict) -> str:
    """Update cached Claude usage from the JSON API response.

    Call this with the result from browser_evaluate(FETCH_USAGE_JS).
    Accepts the full JSON object from the claude.ai internal API.

    The cached data is used by llm_classify for real budget pressure
    instead of token-based estimates.

    Args:
        data: JSON response from the claude.ai usage API (via browser_evaluate).
    """
    import os
    import time

    from llm_router.claude_usage import set_claude_pressure

    _last_usage = parse_api_response(data)
    _state.set_last_usage(_last_usage)

    # Propagate pressure into the routing layer so model chains are
    # reordered immediately (Claude first below 85%, externals first above).
    # Use highest_pressure (raw max of session/weekly), not effective_pressure.
    # The 99% hard cap must hold unconditionally — even when a session reset
    # is imminent, we don't want to risk crossing the weekly limit.
    set_claude_pressure(_last_usage.highest_pressure)

    # Write refresh timestamp so the usage-refresh hook knows when data
    # was last fetched and can decide whether to prompt for a re-fetch.
    import asyncio
    import json as _json

    state_dir = os.path.expanduser("~/.llm-router")
    await asyncio.to_thread(os.makedirs, state_dir, None, True)

    state_file = os.path.join(state_dir, "usage_last_refresh.txt")
    await asyncio.to_thread(
        lambda: open(state_file, "w").write(str(time.time()))
    )

    # Write usage.json so hook scripts (agent-route.py etc.) can read current
    # pressure without importing Python packages or hitting the DB.
    usage_json_file = os.path.join(state_dir, "usage.json")
    await asyncio.to_thread(
        lambda: open(usage_json_file, "w").write(_json.dumps({
            "session_pct": round(_last_usage.session_pct * 100, 1),
            "weekly_pct": round(_last_usage.weekly_pct * 100, 1),
            "sonnet_pct": round(_last_usage.sonnet_pct * 100, 1),
            "highest_pressure": round(_last_usage.highest_pressure, 4),
            "updated_at": time.time(),
        }))
    )

    return _last_usage.summary()


async def llm_refresh_claude_usage() -> str:
    """Refresh Claude subscription usage via the OAuth API — no browser required.

    Reads the Claude Code OAuth token from the macOS Keychain, calls the
    Anthropic OAuth usage endpoint, and updates the local usage cache.

    Requires: Claude Code installed and authenticated on macOS.
    """
    import json as _json
    import urllib.request

    from llm_router.claude_usage import parse_oauth_response, set_claude_pressure
    from llm_router.safe_subprocess import safe_subprocess_run

    # ── Step 1: read OAuth token from macOS Keychain (using safe subprocess) ──
    try:
        r = safe_subprocess_run(
            "security", "find-generic-password", "-s", "Claude Code-credentials", "-w",
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return (
                "Could not read Claude Code credentials from Keychain.\n"
                "Make sure Claude Code is installed and you are signed in."
            )
        creds = _json.loads(r.stdout.strip())
        token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return "OAuth token not found in Keychain credentials. Try signing out and back into Claude Code."
    except Exception as e:
        if "TimeoutExpired" in str(type(e)):
            return "Keychain read timed out."
        elif "JSONDecodeError" in str(type(e)):
            return f"Could not parse Keychain credentials: {e}"
        elif "FileNotFoundError" in str(type(e)):
            return "`security` command not available — macOS only."
        return f"Keychain error: {e}"

    # ── Step 2: call the OAuth usage API ─────────────────────────────────────
    url = "https://api.anthropic.com/api/oauth/usage"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        data = _json.loads(body)
    except Exception as e:
        return f"OAuth usage API call failed: {e}"

    # ── Step 3: parse and cache ───────────────────────────────────────────────
    _last_usage = parse_oauth_response(data)
    _state.set_last_usage(_last_usage)

    set_claude_pressure(_last_usage.highest_pressure)

    import os
    import time
    state_dir = os.path.expanduser("~/.llm-router")
    os.makedirs(state_dir, exist_ok=True)

    with open(os.path.join(state_dir, "usage_last_refresh.txt"), "w") as f:
        f.write(str(time.time()))

    with open(os.path.join(state_dir, "usage.json"), "w") as f:
        _json.dump({
            "session_pct": round(_last_usage.session_pct * 100, 1),
            "weekly_pct": round(_last_usage.weekly_pct * 100, 1),
            "sonnet_pct": round(_last_usage.sonnet_pct * 100, 1),
            "highest_pressure": round(_last_usage.highest_pressure, 4),
            "updated_at": time.time(),
        }, f)

    return _last_usage.summary()


def register(mcp, should_register=None) -> None:
    """Register subscription tools with the FastMCP instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_check_usage"):
        mcp.tool()(llm_check_usage)
    if gate("llm_update_usage"):
        mcp.tool()(llm_update_usage)
    if gate("llm_refresh_claude_usage"):
        mcp.tool()(llm_refresh_claude_usage)
