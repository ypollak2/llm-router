#!/usr/bin/env python3
# llm-router-hook-version: 11
"""SessionStart hook — inject routing banner, start Ollama, refresh Claude usage.

Fires once when a new Claude Code session begins. Four jobs:
  1. Auto-start Ollama via start-ollama.sh (free local routing tier).
  2. Refresh Claude subscription usage from the OAuth API (subscription mode only).
  3. Inject a compact routing table at position 0 of the context window,
     so routing rules are always salient regardless of session length.
  4. Reset the session stats tracker so session-end summary is accurate.

Mode detection (auto):
  LLM_ROUTER_CLAUDE_SUBSCRIPTION=true → subscription mode (OAuth pressure cascade)
  otherwise                           → API-key mode (always routes to external providers)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime

STATE_DIR              = os.path.expanduser("~/.llm-router")
SESSION_START_FILE     = os.path.join(STATE_DIR, "session_start.txt")
SESSION_ID_FILE        = os.path.join(STATE_DIR, "session_id.txt")
DB_PATH                = os.path.join(STATE_DIR, "usage.db")
WEEKLY_DIGEST_FILE     = os.path.join(STATE_DIR, "last_weekly_digest.txt")

_SONNET_IN_PER_M  = 3.0
_SONNET_OUT_PER_M = 15.0
_FREE_PROVIDERS   = {"ollama", "codex"}

# ── .env loader ───────────────────────────────────────────────────────────────
# Hooks run outside the MCP server process and don't inherit its env.
# Load .env so LLM_ROUTER_CLAUDE_SUBSCRIPTION and other settings are available.
_ENV_PATHS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".env"),
    os.path.expanduser("~/.env"),
    os.path.join(STATE_DIR, ".env"),
]


def _load_dotenv() -> None:
    for env_path in _ENV_PATHS:
        if not os.path.exists(env_path):
            continue
        try:
            with open(env_path) as f:
                for line in f:
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

_CC_MODE = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")

BANNER_SUBSCRIPTION = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm-router ACTIVE — subscription mode (MCP-tool routing)  ║
╠════════════════════════════════════════════════════════════════╣
║  Every task routes to the cheapest capable model via MCP:    ║
║  simple   → llm_query   (Ollama → Codex → Gemini Flash)      ║
║  moderate → llm_analyze (Ollama → Codex → GPT-4o)            ║
║  complex  → llm_code    (Ollama → Codex → o3)                ║
║  research → llm_research (Perplexity — web-grounded)         ║
╠════════════════════════════════════════════════════════════════╣
║  Subscription usage tracked for session-end delta reporting  ║
║  Inline OAuth refresh keeps pressure data fresh              ║
╠════════════════════════════════════════════════════════════════╣
║  FORBIDDEN when ROUTE hint present:                          ║
║  Agent subagents · self-answer · WebSearch · WebFetch        ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

BANNER_API_KEYS = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm-router ACTIVE — API-key routing in effect             ║
╠════════════════════════════════════════════════════════════════╣
║  Every task is routed to the cheapest capable external model: ║
║  simple   → llm_query   (Gemini Flash / Groq / GPT-4o-mini)  ║
║  moderate → llm_analyze (GPT-4o / Gemini Pro)                ║
║  complex  → llm_code    (o3 / Gemini Pro)                    ║
║  research → llm_research (Perplexity — web-grounded)         ║
╠════════════════════════════════════════════════════════════════╣
║  Free-first chain: Ollama → Codex → paid API providers        ║
║  Set GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, etc.      ║
╠════════════════════════════════════════════════════════════════╣
║  FORBIDDEN when ROUTE hint present:                          ║
║  Agent subagents · self-answer · WebSearch · WebFetch        ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

BANNER = BANNER_SUBSCRIPTION if _CC_MODE else BANNER_API_KEYS


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


def _ensure_ollama_running() -> str:
    """Start Ollama via start-ollama.sh. Returns a status line for the banner."""
    script = os.path.join(os.path.dirname(__file__), "start-ollama.sh")
    if not os.path.exists(script):
        # Fallback: look next to the installed hook
        script = os.path.join(os.path.expanduser("~/.claude/hooks"), "start-ollama.sh")
    if not os.path.exists(script):
        return "\n⚠️  start-ollama.sh not found — Ollama not managed"

    try:
        result = subprocess.run(
            ["bash", script],
            capture_output=True, text=True, timeout=15,
        )
        stdout = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = stderr or stdout or "unknown error"
            return f"\n⚠️  Ollama: {msg}"
        return f"\n{stdout}" if stdout else ""
    except subprocess.TimeoutExpired:
        return "\n⚠️  Ollama start timed out — first routing call may be slow"
    except Exception as e:
        return f"\n⚠️  Ollama start failed: {e}"


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
        snapshot = {
            "session_pct": round(session_pct, 1),
            "weekly_pct": round(weekly_pct, 1),
            "sonnet_pct": round(sonnet_pct, 1),
            "highest_pressure": round(highest_pressure, 4),
            "updated_at": time.time(),
        }
        usage_path = os.path.join(STATE_DIR, "usage.json")
        with open(usage_path, "w") as f:
            json.dump(snapshot, f)
        # Snapshot for session-end delta reporting
        snap_path = os.path.join(STATE_DIR, "session_start_cc_pct.json")
        with open(snap_path, "w") as f:
            json.dump(snapshot, f)

        # Pressure indicator for the banner
        pressure_str = f"session={session_pct:.0f}% weekly={weekly_pct:.0f}% sonnet={sonnet_pct:.0f}%"
        if highest_pressure >= 0.95:
            return f"\n🔴 Usage: {pressure_str} — ALL external (full pressure)"
        if highest_pressure >= 0.85:
            return f"\n🟡 Usage: {pressure_str} — partial pressure active"
        return f"\n✅ Usage: {pressure_str}"
    except Exception as e:
        return f"\n⚠️  Usage parse failed: {e}"


def _weekly_digest() -> str:
    """Return a one-line weekly savings summary shown on Mondays (or after 6+ day gap).

    Queries usage.db directly — no import from the package needed.
    Writes a timestamp file so it fires at most once per week.
    """
    today = datetime.now()
    is_monday = today.weekday() == 0

    # Check last-shown timestamp
    try:
        with open(WEEKLY_DIGEST_FILE) as f:
            last_ts = float(f.read().strip())
        since_last = time.time() - last_ts
        if since_last < 6 * 86400:     # shown within the last 6 days — skip
            return ""
    except (OSError, ValueError):
        if not is_monday:
            return ""   # First run — only show on Mondays

    if not os.path.exists(DB_PATH):
        return ""

    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """
            SELECT provider,
                   COUNT(*),
                   COALESCE(SUM(input_tokens),  0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cost_usd),      0)
            FROM usage
            WHERE success=1
              AND timestamp >= datetime('now', '-7 days')
            GROUP BY provider
            """
        ).fetchall()
        conn.close()

        calls = total_in = total_out = 0
        saved = 0.0
        for provider, cnt, in_tok, out_tok, cost in rows:
            calls     += cnt
            total_in  += in_tok
            total_out += out_tok
            baseline   = (in_tok * _SONNET_IN_PER_M + out_tok * _SONNET_OUT_PER_M) / 1_000_000
            if provider in _FREE_PROVIDERS:
                saved += baseline
            elif provider != "subscription":
                saved += max(0.0, baseline - cost)

        if calls == 0:
            return ""

        # Record shown
        try:
            with open(WEEKLY_DIGEST_FILE, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass

        total_tok = total_in + total_out
        tok_str = f"{total_tok / 1000:.1f}k" if total_tok >= 1000 else str(total_tok)
        yearly = saved / 7 * 365
        return (
            f"\n📊 Weekly digest: {calls} calls · {tok_str} tok · ${saved:.2f} saved last 7 days"
            f"  (≈${yearly:.0f}/yr at this rate)"
        )
    except Exception:
        return ""


def main() -> None:
    try:
        json.load(sys.stdin)  # consume input (may be empty)
    except (json.JSONDecodeError, EOFError):
        pass

    _reset_session_stats()
    _reset_stale_health()
    # Clear any orphaned pending-route state files from crashed/killed sessions.
    # Without this, stale files would block Bash/Edit in the new session.
    import glob as _glob
    for _stale in _glob.glob(os.path.join(STATE_DIR, "pending_route_*.json")):
        try:
            os.unlink(_stale)
        except OSError:
            pass

    hints = ""

    # 1. Ensure Ollama is running (start it if needed)
    hints += _ensure_ollama_running()

    # 2. Refresh Claude usage from OAuth API.
    # Always attempt the refresh — if the OAuth token is present, we're in
    # subscription mode regardless of LLM_ROUTER_CLAUDE_SUBSCRIPTION env var.
    # This makes CC mode detection implicit (token present = CC mode) rather
    # than requiring a .env file that hooks may not have access to.
    usage_hint = _refresh_claude_usage()
    is_subscription = not usage_hint.startswith("\n⚠️")

    # Pick the right banner based on detected mode
    if is_subscription or _CC_MODE:
        banner = BANNER_SUBSCRIPTION
    else:
        banner = BANNER_API_KEYS

    hints += usage_hint
    hints += _weekly_digest()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "contextForAgent": banner + hints,
        }
    }))


if __name__ == "__main__":
    main()
