#!/usr/bin/env python3
# llm-router-hook-version: 5
"""SessionStart hook — inject routing banner, start Ollama, refresh Claude usage.

Fires once when a new Claude Code session begins. Four jobs:
  1. Auto-start Ollama if it is not running (free local routing tier).
  2. Refresh Claude subscription usage from the OAuth API so pressure-based
     routing has accurate data from the first request of the session.
  3. Inject a compact routing table at position 0 of the context window,
     so routing rules are always salient regardless of session length.
  4. Reset the session stats tracker so session-end summary is accurate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

STATE_DIR = os.path.expanduser("~/.llm-router")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
SESSION_ID_FILE = os.path.join(STATE_DIR, "session_id.txt")

BANNER = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm-router ACTIVE — subscription routing in effect        ║
╠════════════════════════════════════════════════════════════════╣
║  simple   → /model claude-haiku-4-5-20251001  (subscription) ║
║  moderate → Sonnet handles directly (passthrough)             ║
║  complex  → /model claude-opus-4-6            (subscription) ║
║  research → llm_research  (Perplexity — web-grounded)        ║
╠════════════════════════════════════════════════════════════════╣
║  Under pressure, external models activate tier by tier:       ║
║  session ≥85% → simple external (Ollama → Gemini Flash)      ║
║  sonnet  ≥95% → moderate external (Ollama → GPT-4o)          ║
║  weekly  ≥95% → ALL external (Ollama → cloud fallback)       ║
╠════════════════════════════════════════════════════════════════╣
║  FORBIDDEN when ROUTE hint present:                          ║
║  Agent subagents · self-answer · WebSearch · WebFetch        ║
╚════════════════════════════════════════════════════════════════╝
""".strip()


def _reset_session_stats() -> None:
    """Write current timestamp and a fresh UUID as session identifiers."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(SESSION_START_FILE, "w") as f:
            f.write(str(time.time()))
        with open(SESSION_ID_FILE, "w") as f:
            f.write(str(uuid.uuid4()))
    except OSError:
        pass


def _reset_stale_health() -> None:
    """Write a stale-reset marker so the router process resets stale circuit breakers."""
    reset_file = os.path.join(STATE_DIR, "reset_stale.flag")
    try:
        with open(reset_file, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _is_ollama_reachable() -> bool:
    """Return True if Ollama is running and responding."""
    ollama_url = os.environ.get("LLM_ROUTER_OLLAMA_URL", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=2):
            return True
    except Exception:
        return False


def _ensure_ollama_running() -> str:
    """Start Ollama if not running. Returns a status line for the banner."""
    ollama_url = os.environ.get("LLM_ROUTER_OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "qwen3.5:latest")

    if _is_ollama_reachable():
        # Check if the required model is installed
        try:
            with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=2) as r:
                data = json.loads(r.read())
            installed = [m.get("name", "") for m in data.get("models", [])]
            if not any(
                m == ollama_model or m.startswith(ollama_model.split(":")[0])
                for m in installed
            ):
                return (
                    f"\n⚠️  Ollama running but {ollama_model!r} not installed — "
                    f"run: ollama pull {ollama_model}"
                )
            return ""  # healthy
        except Exception:
            return ""  # reachable, ignore model check error

    # Not running — try to start it
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach so it survives hook exit
        )
        # Give it up to 3 seconds to become ready
        for _ in range(6):
            time.sleep(0.5)
            if _is_ollama_reachable():
                return "\n✅ Ollama started (was offline)"
        return "\n⚠️  Ollama start attempted — still starting up (first call may be slower)"
    except FileNotFoundError:
        return "\n⚠️  Ollama not installed — free local tier unavailable"


def _refresh_claude_usage() -> str:
    """Fetch fresh Claude subscription usage from the OAuth API.

    Reads the OAuth token from macOS Keychain, calls the Anthropic usage
    endpoint, and writes the result to ~/.llm-router/usage.json so pressure-
    based routing is accurate from the first request of this session.

    Returns a one-line status string for the banner (empty on success).
    """
    # Read OAuth token from macOS Keychain
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return "\n⚠️  Could not read Claude credentials — run llm_check_usage manually"
        creds = json.loads(r.stdout.strip())
        token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return "\n⚠️  OAuth token not found — run llm_check_usage manually"
    except subprocess.TimeoutExpired:
        return "\n⚠️  Keychain read timed out — run llm_check_usage manually"
    except Exception as e:
        return f"\n⚠️  Keychain error: {e}"

    # Call the OAuth usage API
    url = "https://api.anthropic.com/api/oauth/usage"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return f"\n⚠️  Usage API failed: {e}"

    # Parse — the OAuth response has utilization as a percentage (0-100)
    try:
        session_pct = float(data.get("five_hour", {}).get("utilization", 0.0))
        weekly_pct = float(data.get("seven_day", {}).get("utilization", 0.0))
        sonnet_pct = float(data.get("seven_day_sonnet", {}).get("utilization", 0.0))
        highest_pressure = max(session_pct, weekly_pct, sonnet_pct) / 100.0

        os.makedirs(STATE_DIR, exist_ok=True)
        usage_path = os.path.join(STATE_DIR, "usage.json")
        with open(usage_path, "w") as f:
            json.dump({
                "session_pct": round(session_pct, 1),
                "weekly_pct": round(weekly_pct, 1),
                "sonnet_pct": round(sonnet_pct, 1),
                "highest_pressure": round(highest_pressure, 4),
                "updated_at": time.time(),
            }, f)

        # Pressure indicator for the banner
        pressure_str = f"session={session_pct:.0f}% weekly={weekly_pct:.0f}% sonnet={sonnet_pct:.0f}%"
        if highest_pressure >= 0.95:
            return f"\n🔴 Usage: {pressure_str} — ALL external (full pressure)"
        if highest_pressure >= 0.85:
            return f"\n🟡 Usage: {pressure_str} — partial pressure active"
        return f"\n✅ Usage: {pressure_str}"
    except Exception as e:
        return f"\n⚠️  Usage parse failed: {e}"


def main() -> None:
    try:
        json.load(sys.stdin)  # consume input (may be empty)
    except (json.JSONDecodeError, EOFError):
        pass

    _reset_session_stats()
    _reset_stale_health()

    hints = ""

    # 1. Ensure Ollama is running (start it if needed)
    hints += _ensure_ollama_running()

    # 2. Refresh Claude usage from OAuth API (accurate pressure from session start)
    hints += _refresh_claude_usage()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "contextForAgent": BANNER + hints,
        }
    }))


if __name__ == "__main__":
    main()
