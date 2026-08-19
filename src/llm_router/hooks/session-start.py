#!/usr/bin/env python3
# llm_router-hook-version: 19
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
from pathlib import Path

# Import timeout config from llm_router package if available
try:
    from llm_router.timeout_config import subprocess_timeout, http_timeout
except ImportError:
    # Fallback to hardcoded defaults if llm_router not installed
    def subprocess_timeout() -> int:
        return int(os.environ.get("LLM_ROUTER_SUBPROCESS_TIMEOUT", "15"))
    def http_timeout() -> int:
        return int(os.environ.get("LLM_ROUTER_HTTP_TIMEOUT", "10"))

STATE_DIR              = os.path.expanduser("~/.llm-router")
SESSION_START_FILE     = os.path.join(STATE_DIR, "session_start.txt")
SESSION_ID_FILE        = os.path.join(STATE_DIR, "session_id.txt")
SESSION_SPEND_FILE     = os.path.join(STATE_DIR, "session_spend.json")
DB_PATH                = os.path.join(STATE_DIR, "usage.db")
WEEKLY_DIGEST_FILE     = os.path.join(STATE_DIR, "last_weekly_digest.txt")

# Savings baseline = the latest-Opus host rate, the SAME source of truth as
# cost._OPUS_PRICING / receipt_store / savings_logger. This banner previously
# priced against Sonnet ($3/$15) while every other surface used Opus, so its
# "saved last 7 days" figure disagreed with the llm_savings weekly bucket
# (RETROSPECTIVE B-6). Resolved lazily at use-site (see _weekly_digest) to keep
# this hook import-light; the fallback below is the current Opus rate.
_HOST_IN_PER_M_FALLBACK  = 5.0
_HOST_OUT_PER_M_FALLBACK = 25.0
_FREE_PROVIDERS   = {"ollama", "codex", "gemini_cli"}

# ── .env loader ───────────────────────────────────────────────────────────────
# Hooks run outside the MCP server process and don't inherit its env.
# Load .env so LLM_ROUTER_CLAUDE_SUBSCRIPTION and other settings are available.
_ENV_PATHS = [
    os.path.join(os.getcwd(), ".env"),  # CWD .env (hook runs from project root)
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".env"),
    os.path.expanduser("~/.env"),
    os.path.join(STATE_DIR, ".env"),
]


def _load_dotenv(load_into: "dict[str, str] | None" = None) -> None:
    """Load .env files into `load_into` (default: os.environ).

    Passing an explicit dict lets tests exec/import this module's loader
    without mutating global process env (audit P5: env-leakage class).
    Existing keys in the target mapping are never overwritten.
    """
    target = os.environ if load_into is None else load_into
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
                    if key and key not in target:
                        target[key] = value
        except OSError:
            pass


_load_dotenv()

_CC_MODE = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")

# chz-surface-ok: names resolved at render time by _localize_banner()
BANNER_ZERO_CLAUDE = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm_router ACTIVE — strict zero-Claude routing             ║
╠════════════════════════════════════════════════════════════════╣
║  Prompts execute through external routes before Claude runs.  ║
║  If no external route completes, the prompt is blocked.       ║
║  Prefix a prompt with `claude:` for intentional native use.   ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

# chz-surface-ok: names resolved at render time by _localize_banner()
BANNER_SUBSCRIPTION = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm_router ACTIVE — subscription mode (MCP-tool routing)  ║
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
║  routing is advisory; enforce mode decides what is blocked  ║
║  Prefer the cheap tool when it fits, else just do the work  ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

# chz-surface-ok: names resolved at render time by _localize_banner()
BANNER_API_KEYS = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm_router ACTIVE — API-key routing in effect             ║
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
║  routing is advisory; enforce mode decides what is blocked  ║
║  Prefer the cheap tool when it fits, else just do the work  ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

# chz-surface-ok: names resolved at render time by _localize_banner()
BANNER_LOCAL = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm_router ACTIVE — local routing (no cloud keys set)     ║
╠════════════════════════════════════════════════════════════════╣
║  No cloud API keys detected. Routing uses whatever is         ║
║  available locally (Ollama / Codex / Gemini CLI) or falls     ║
║  through to Claude when nothing else can serve the task.      ║
║  Add OPENAI_API_KEY / GEMINI_API_KEY / GROQ_API_KEY, etc. to  ║
║  enable cloud fallbacks — run `llm_router setup` to configure.    ║
╠════════════════════════════════════════════════════════════════╣
║  routing is advisory; enforce mode decides what is blocked  ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

# RED2-8-03: the banner must reflect ACTUAL provider availability, not just the
# subscription flag. Claiming "API-key routing in effect" and naming cloud
# providers when zero keys are configured (the README's own Ollama-first
# quickstart state) is false. Cloud-key vars llm_router can actually route through:
_CLOUD_KEY_VARS = (
    "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
    "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY", "PERPLEXITY_API_KEY",
)


def _any_cloud_key() -> bool:
    return any(os.environ.get(k, "").strip() for k in _CLOUD_KEY_VARS)


# ── Registered-tool surface (CHZ-SURF-01) ────────────────────────────────────
# The banner is injected into the model's context at session start, so the tool
# names in it TEACH the model what to call for the rest of the session. When the
# banner advertised llm_query/llm_analyze/llm_code/llm_research under the
# consolidated default — where none of them are registered — it was training the
# model to make calls that fail. Names are resolved against the live tier.
def _load_route_tool():
    """Return `llm_router.tool_surface.route_tool`, or None if unavailable."""
    try:
        from llm_router.tool_surface import route_tool
        return route_tool
    except ImportError:
        pass
    try:
        import importlib.util as _ilu
        _here = Path(__file__).resolve().parent
        for _cand in (_here / "llm_router_tool_surface.py", _here.parent / "tool_surface.py"):
            if not _cand.exists():
                continue
            _spec = _ilu.spec_from_file_location("llm_router_tool_surface", _cand)
            _mod = _ilu.module_from_spec(_spec)
            sys.modules["llm_router_tool_surface"] = _mod
            _spec.loader.exec_module(_mod)
            return _mod.route_tool
    except Exception:  # noqa: BLE001
        pass
    return None


_ROUTE_TOOL = _load_route_tool()

# Longest-first so no name is a prefix of a later match.
_BANNER_TOOL_NAMES = ("llm_research", "llm_generate", "llm_analyze", "llm_query", "llm_code")


def _fit(body: str, target: int) -> str:
    """Fit ``body`` to exactly ``target`` characters for the banner box.

    Resolved names are longer than the legacy ones (``llm(task="research")`` vs
    ``llm_research``), so reclaim room from the alignment padding first, and only
    then elide the trailing provider parenthetical. The box must stay square —
    a ragged banner reads as a broken install.
    """
    while len(body) > target and "  " in body:
        body = body.replace("  ", " ", 1)
    if len(body) > target:
        cut = body.rfind(" (")
        if cut > 0:
            body = body[:cut]
    return body[:target].ljust(target)


def _localize_banner(banner: str) -> str:
    """Rewrite legacy tool names in a banner to the ones actually registered."""
    if _ROUTE_TOOL is None:
        return banner
    out = []
    for line in banner.splitlines():
        hit = next((t for t in _BANNER_TOOL_NAMES if t in line), None)
        if hit is None or not (line.startswith("║") and line.endswith("║")):
            out.append(line)
            continue
        try:
            replacement = _ROUTE_TOOL(hit)
        except Exception:  # noqa: BLE001
            out.append(line)
            continue
        width = len(line) - 2
        out.append("║" + _fit(line[1:-1].replace(hit, replacement), width) + "║")
    return "\n".join(out)


def _resolve_banner(is_subscription: bool) -> str:
    if is_subscription or _CC_MODE:
        return _localize_banner(BANNER_SUBSCRIPTION)
    if _any_cloud_key():
        return _localize_banner(BANNER_API_KEYS)
    return _localize_banner(BANNER_LOCAL)  # honest: no cloud keys configured


BANNER = _resolve_banner(_CC_MODE)


_LLM_ROUTER_LOGO = "⚡ LLM Router"
_WELCOME_DIVIDER = "─" * 60


def _mode_label(is_subscription: bool) -> str:
    """One-word mode label for the welcome line: zero-claude / subscription /
    api-keys / local.

    RED2-11-03: must have a "local" branch so the welcome mode line agrees with
    the banner box (BANNER_LOCAL) when no cloud keys are configured — previously
    it always claimed "api-keys" even with zero keys, contradicting the box.
    """
    if _zero_claude_enabled():
        return "zero-claude (strict — external routes or block)"
    if is_subscription or _CC_MODE:
        return "subscription (Claude OAuth pressure cascade)"
    if _any_cloud_key():
        return "api-keys (Ollama → Codex → paid providers)"
    return "local (Ollama / Codex — no cloud keys set)"


def _enforce_label() -> str:
    """Human description of the RESOLVED enforcement mode (honest — no hardcoding).

    Resolves through the same source of truth the PreToolUse enforcer uses, so
    this line always matches actual behavior.
    """
    try:
        from llm_router.enforce_config import resolve_enforce_mode
        mode = resolve_enforce_mode()
    except Exception:
        mode = "soft"
    # CHZ-AUD-D-05/A-06 (RED-2 re-audit): honest per-mode text, verified against
    # enforce-route.py. On a ROUTED turn (one for which auto-route wrote a pending
    # directive), SMART holds Edit/Write/MultiEdit until the route is satisfied —
    # for ALL task types, NOT just Q&A (enforce-route.py:1181). Its only concession
    # over hard is that read-only Bash is allowed for code/non-Q&A tasks; write Bash
    # is still held. HARD/STRICT hold Bash/Edit/Write/MultiEdit/NotebookEdit
    # outright (only Read/Glob/Grep/LS proceed). Only off/shadow/advise/suggest/soft
    # never block. The smart label must not tell code tasks their write tools are
    # free, nor claim "never blocks" for a mode that blocks.
    descriptions = {
        "off": "off (no enforcement)",
        "shadow": "shadow (observe-only; never blocks)",
        "advise": "advise (silent; never blocks)",
        "suggest": "suggest (logs routing misses; never blocks)",
        "soft": "soft (logs routing misses; never blocks)",
        "smart": "smart — DEFAULT (on a routed turn holds Edit/Write/MultiEdit until routed for ALL tasks; Read/Glob/Grep/LS always proceed, read-only Bash proceeds for code tasks)",
        "hard": "hard (holds Edit/Write/MultiEdit/NotebookEdit + write Bash until routed; Read/Glob/Grep/LS proceed, and read-only Bash proceeds for code tasks)",
        "strict": "strict (holds Bash/Edit/Write/MultiEdit/NotebookEdit until routed — read-only Bash included; only Read/Glob/Grep/LS proceed)",
    }
    return descriptions.get(mode, f"{mode} (holds blocklisted tools until routed)")


def _render_welcome(is_subscription: bool) -> str:
    """Multi-line greeting printed to stderr at session start.

    Renders under Claude Code's 'SessionStart:startup hook success:' header,
    so each line lands inside a labeled status block in the UI. Kept short
    enough that it doesn't dominate the session-open scroll.
    """
    from datetime import datetime

    now = datetime.now().strftime("%a %b %d · %H:%M")
    mode = _mode_label(is_subscription)
    enforce = _enforce_label()

    # Painterly LLM Router banner — Chhuzom is the Bhutanese river confluence
    # where Paro Chhu + Thimphu Chhu meet to form Wang Chhu; three stupas
    # (Bhutanese, Tibetan, Nepali) guard the junction. See llm_router.banner.
    try:
        from llm_router.banner import render_banner
        painting = render_banner()
    except Exception:
        # Defensive: never let a banner failure block the SessionStart hook.
        painting = f"{_LLM_ROUTER_LOGO} — routing intelligence online"

    lines = [
        painting,
        "",
        _WELCOME_DIVIDER,
        f"   mode    → {mode}",
        f"   enforce → {enforce}",
        f"   opened  → {now}",
        "   chain   → Ollama · Codex · Gemini Flash · GPT-4o · Perplexity",
        "   tip     → run `llm_router summary` to see what this session saved",
    ]

    # F1: pull-routing feedback — if the project also has Cursor or Windsurf
    # config, note that those IDEs use pull routing (model must call MCP tools
    # manually) rather than the push routing active here in Claude Code.
    _pull_ides = []
    _project_root = Path(os.getcwd())
    if (_project_root / ".cursor").exists():
        _pull_ides.append("Cursor")
    if (_project_root / ".windsurf").exists():
        _pull_ides.append("Windsurf")
    if _pull_ides:
        _ide_list = " + ".join(_pull_ides)
        lines.append(
            f"   pull    → {_ide_list}: model must call llm_* tools (best-effort)"
        )

    lines.append(_WELCOME_DIVIDER)
    return "\n".join(lines)


def _zero_claude_enabled() -> bool:
    """Return True when prompt hooks are configured to block native turns."""
    env_value = os.environ.get("LLM_ROUTER_ZERO_CLAUDE", "").strip().lower()
    if env_value:
        return env_value in ("1", "true", "yes", "on", "zero_claude", "strict_zero")

    config_path = Path(STATE_DIR) / "routing.yaml"
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError:
        return False

    import re
    mode_match = re.search(r"^\s*mode\s*:\s*(\S+)\s*$", content, re.MULTILINE | re.IGNORECASE)
    if mode_match and mode_match.group(1).lower() in ("zero_claude", "strict_zero"):
        return True
    bool_match = re.search(r"^\s*zero_claude\s*:\s*(\S+)\s*$", content, re.MULTILINE | re.IGNORECASE)
    return bool(bool_match and bool_match.group(1).lower() in ("1", "true", "yes", "on"))


def _select_banner(is_subscription: bool) -> str:
    if _zero_claude_enabled():
        return _localize_banner(BANNER_ZERO_CLAUDE)
    # RED2-8-03: honest — fall to the local banner when no cloud keys are set
    # instead of claiming "API-key routing in effect".
    return _resolve_banner(is_subscription)


def _reset_session_stats() -> None:
    """Write current timestamp and a fresh UUID as session identifiers.
    Also resets session_spend.json so per-session cost tracking starts clean.
    Initialize prompt_sequence counter for per-prompt quota audit trail.
    Initialize routing lineage tracking (new decisions only)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(SESSION_START_FILE, "w") as f:
            f.write(str(time.time()))
        with open(SESSION_ID_FILE, "w") as f:
            f.write(str(uuid.uuid4()))
    except OSError:
        pass
    # Reset real-time spend tracker so session-end shows this session only
    # IMPORTANT: Include ALL fields from SessionSpend.get_summary() to ensure
    # proper isolation between sessions (v8.8.0: added savings tracking fields)
    try:
        fresh = {
            "total_usd": 0.0,
            "call_count": 0,
            "anomaly_flag": False,
            "session_start": time.time(),
            "top_model": None,
            "per_model": {},
            "per_tool": {},
            "prompt_sequence": 0,
            # v8.8.0: Token reclamation & savings fields (must be reset per session)
            "tokens_reclaimed": 0,
            "opus_equivalent_usd": 0.0,
            "net_savings_usd": 0.0,
            "extension_minutes": 0.0,
            "gate_pass_rate": 100.0,
            "gates_passed": 0,
            "gates_failed": 0,
        }
        tmp = SESSION_SPEND_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(fresh, f, indent=2)
        os.replace(tmp, SESSION_SPEND_FILE)
    except OSError:
        pass

    # Initialize routing lineage tracking (v10.2.0)
    try:
        from llm_router.hooks.lineage_integration import init_session_lineage
        init_session_lineage()
    except Exception:
        pass  # Gracefully skip if lineage system not available


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
            capture_output=True, text=True, timeout=subprocess_timeout(),
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


def _pxpipe_config() -> tuple[bool, str, str]:
    """Read pxpipe settings without importing the full llm_router.config module
    (hooks stay stdlib-only so they run in a fresh subprocess with no
    dependency on the package's import graph being ready)."""
    enabled = os.environ.get("LLM_ROUTER_PXPIPE_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    url = os.environ.get("LLM_ROUTER_PXPIPE_URL", "http://127.0.0.1:47821").rstrip("/")
    return enabled, url, os.environ.get("LLM_ROUTER_PXPIPE_HEAVY_MODELS", "claude-fable-5")


def _pxpipe_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1):
            return True
    except Exception:
        return False


def _ensure_pxpipe_running() -> str:
    """Auto-start a local pxpipe proxy for heavy-model context compression,
    if LLM_ROUTER_PXPIPE_ENABLED is set. Off by default — unlike Ollama, this
    redirects Claude Code's OWN Anthropic traffic (via settings.json's
    ANTHROPIC_BASE_URL, synced separately in _sync_pxpipe_anthropic_base_url),
    so it must be an explicit opt-in, not an always-on convenience.
    Returns a status line for the banner, or "" when disabled.
    """
    enabled, _url, _models = _pxpipe_config()
    if not enabled:
        return ""

    script = os.path.join(os.path.dirname(__file__), "start-pxpipe.sh")
    if not os.path.exists(script):
        script = os.path.join(os.path.expanduser("~/.claude/hooks"), "start-pxpipe.sh")
    if not os.path.exists(script):
        return "\n⚠️  start-pxpipe.sh not found — pxpipe not managed"

    try:
        result = subprocess.run(
            ["bash", script],
            capture_output=True, text=True, timeout=subprocess_timeout(),
        )
        stdout = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = stderr or stdout or "unknown error"
            return f"\n⚠️  pxpipe: {msg}"
        return f"\n{stdout}" if stdout else ""
    except subprocess.TimeoutExpired:
        return "\n⚠️  pxpipe start timed out — heavy-model calls will route normally"
    except Exception as e:
        return f"\n⚠️  pxpipe start failed: {e}"


def _sync_pxpipe_anthropic_base_url() -> str:
    """Wire (or self-heal) ANTHROPIC_BASE_URL in ~/.claude/settings.json so
    Claude Code's OWN traffic — not just LLM Router-routed calls — goes through
    pxpipe for heavy models. Read at Claude Code startup, before its API
    client is constructed, so this only takes effect on the NEXT session,
    never retroactively for the one currently starting.

    Safety rule: only ever WRITE the key when it's currently unset — ANY
    existing value (a corporate proxy, a different pxpipe port from a prior
    config change, anything) is left alone rather than risk overwriting
    something the user set deliberately. REMOVAL is the mirror case and can
    be more precise: only remove when the current value exactly equals what
    we would have set ourselves, so a genuinely unrelated value is never
    touched there either. Removal always points at a currently-reachable
    pxpipe, or clears the override entirely — Claude Code has no fallback
    if the configured base URL doesn't answer, so a stale pointer would
    break EVERY API call, not just heavy-model ones.
    """
    enabled, url, _models = _pxpipe_config()
    settings_path = Path.home() / ".claude" / "settings.json"

    try:
        data = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return ""  # don't touch a file we can't safely parse

    env = data.setdefault("env", {})
    current = env.get("ANTHROPIC_BASE_URL")
    want = url if (enabled and _pxpipe_reachable(url)) else None

    if want is not None:
        if current == want:
            return ""  # already correct, no write needed
        if current is not None:
            # Something else is already there — could be a corporate proxy
            # or any other reason the user set this deliberately. Never
            # overwrite a value we didn't set ourselves.
            return ""
        env["ANTHROPIC_BASE_URL"] = want
    else:
        if current is None or current != url:
            return ""  # nothing of ours to remove
        # Self-heal: pxpipe is disabled or unreachable — remove OUR pointer
        # rather than leave Claude Code aimed at a dead endpoint.
        del env["ANTHROPIC_BASE_URL"]
        if not env:
            del data["env"]

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        return f"\n⚠️  Could not update {settings_path}: {e}"

    if want is not None:
        return f"\n✅ Claude Code's own traffic now routes heavy models through pxpipe ({want})"
    return "\n↩️  pxpipe unavailable — reverted Claude Code to Anthropic's default endpoint"


def _refresh_claude_usage() -> str:
    """Fetch fresh Claude subscription usage from the OAuth API with retries.

    Attempts up to 3 times to refresh quota data, backing off 2s between retries.
    On success: writes to ~/.llm-router/usage.json and session_start_cc_pct.json
    On all-retries failure: writes conservative fallback (50% all pressures)

    Returns a one-line status string for the banner (empty on success).
    """
    max_retries = 3
    retry_delay = 2.0
    
    for attempt in range(max_retries):
        result = _refresh_claude_usage_attempt()
        if result["success"]:
            # Write both usage.json and session snapshot
            os.makedirs(STATE_DIR, exist_ok=True)
            usage_path = os.path.join(STATE_DIR, "usage.json")
            snap_path = os.path.join(STATE_DIR, "session_start_cc_pct.json")
            
            snapshot = {
                "session_pct": result["session_pct"],
                "weekly_pct": result["weekly_pct"],
                "sonnet_pct": result["sonnet_pct"],
                "highest_pressure": result["highest_pressure"],
                "updated_at": time.time(),
                # RED2-9-04: the SUCCESS path must set is_fallback explicitly.
                # Omitting it made the banner reader `get("is_fallback", True)`
                # default to True, so a successful subscription refresh was
                # mis-read as a fallback and the banner box showed the wrong mode
                # for every session after the first.
                "is_fallback": False,
            }
            
            try:
                with open(usage_path, "w") as f:
                    json.dump(snapshot, f)
                with open(snap_path, "w") as f:
                    json.dump(snapshot, f)
            except OSError:
                pass
            
            # Return success banner
            session_pct = result["session_pct"]
            weekly_pct = result["weekly_pct"]
            sonnet_pct = result["sonnet_pct"]
            highest_pressure = result["highest_pressure"]
            pressure_str = f"session={session_pct:.0f}% weekly={weekly_pct:.0f}% sonnet={sonnet_pct:.0f}%"
            
            if highest_pressure >= 0.95:
                return f"\n🔴 Usage: {pressure_str} — ALL external (full pressure)"
            if highest_pressure >= 0.85:
                return f"\n🟡 Usage: {pressure_str} — partial pressure active"
            return f"\n✅ Usage: {pressure_str}"
        
        # Retry on failure
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    
    # All retries failed — write conservative fallback (50% pressure)
    os.makedirs(STATE_DIR, exist_ok=True)
    usage_path = os.path.join(STATE_DIR, "usage.json")
    snap_path = os.path.join(STATE_DIR, "session_start_cc_pct.json")
    
    fallback = {
        "session_pct": 50,
        "weekly_pct": 50,
        "sonnet_pct": 50,
        "highest_pressure": 0.5,
        "updated_at": time.time(),
        "is_fallback": True,
    }
    
    try:
        with open(usage_path, "w") as f:
            json.dump(fallback, f)
        with open(snap_path, "w") as f:
            json.dump(fallback, f)
    except OSError:
        pass
    
    sys.stderr.write(
        "[llm_router] ⚠ Quota refresh failed (3 attempts)\n"
        "[llm_router]   Using conservative 50% pressure defaults\n"
    )
    return "\n⚠️  Usage: refresh failed (50% pressure fallback)"


def _refresh_claude_usage_attempt() -> dict:
    """Single attempt to fetch Claude subscription usage via OAuth.
    
    Returns:
        {"success": True, "session_pct": X, "weekly_pct": Y, "sonnet_pct": Z, "highest_pressure": P}
        or {"success": False} on any error
    """
    # Read OAuth token from macOS Keychain
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=subprocess_timeout(),
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {"success": False}
        creds = json.loads(r.stdout.strip())
        token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return {"success": False}
    except Exception:
        return {"success": False}

    # Call the OAuth usage API
    url = "https://api.anthropic.com/api/oauth/usage"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=http_timeout()) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return {"success": False}

    # Parse — the OAuth response has utilization as a percentage (0-100)
    try:
        session_pct = float((data.get("five_hour") or {}).get("utilization", 0.0))
        weekly_pct = float((data.get("seven_day") or {}).get("utilization", 0.0))
        sonnet_pct = float((data.get("seven_day_sonnet") or {}).get("utilization", 0.0))
        highest_pressure = max(session_pct, weekly_pct, sonnet_pct) / 100.0
        
        return {
            "success": True,
            "session_pct": round(session_pct, 1),
            "weekly_pct": round(weekly_pct, 1),
            "sonnet_pct": round(sonnet_pct, 1),
            "highest_pressure": round(highest_pressure, 4),
        }
    except Exception:
        return {"success": False}


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

        # Resolve the Opus host baseline from the single source of truth; fall
        # back to the current rate if cost isn't importable in this hook context.
        try:
            from llm_router.cost import (
                _HOST_INPUT_PER_M as _host_in,
                _HOST_OUTPUT_PER_M as _host_out,
            )
        except Exception:
            _host_in, _host_out = _HOST_IN_PER_M_FALLBACK, _HOST_OUT_PER_M_FALLBACK

        calls = total_in = total_out = 0
        saved = 0.0
        for provider, cnt, in_tok, out_tok, cost in rows:
            calls     += cnt
            total_in  += in_tok
            total_out += out_tok
            # Same free/subscription logic as cost.get_savings_by_period so the
            # weekly digest and llm_savings agree for the 7-day window.
            baseline   = (in_tok * _host_in + out_tok * _host_out) / 1_000_000
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


def _latency_hint() -> str:
    """Return a one-liner showing p50 latency for the top models seen in the last 7 days.

    Only shown when there is enough data (≥3 models with ≥2 calls each).
    Silent on any error so it never breaks the session start.
    """
    if not os.path.exists(DB_PATH):
        return ""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """
            SELECT model, AVG(latency_ms) as p50, COUNT(*) as n
            FROM usage
            WHERE success=1
              AND latency_ms > 100
              AND timestamp >= datetime('now', '-7 days')
            GROUP BY model
            HAVING n >= 2
            ORDER BY p50 ASC
            LIMIT 5
            """
        ).fetchall()
        conn.close()

        if len(rows) < 2:
            return ""

        parts = []
        for model, p50_ms, _ in rows:
            short = model.split("/")[-1] if "/" in model else model
            # Abbreviate common suffixes to keep it compact
            short = short.replace("-preview", "").replace("-latest", "")
            if len(short) > 16:
                short = short[:14] + "…"
            secs = p50_ms / 1000
            parts.append(f"{short} {secs:.1f}s")

        return "\n⚡ p50: " + " · ".join(parts)
    except Exception:
        return ""


def _preflight_check() -> str:
    """Check API keys, Ollama, and enforce-route mode. Returns a compact status line.

    Runs silently (never raises) so it cannot block session start.
    Only emits output when something needs attention.
    """
    # RED2-5-03: a missing OPTIONAL provider is not a defect. LLM Router routes over
    # whatever is available (any cloud key OR reachable Ollama OR Claude
    # subscription). Distinguish "you have ZERO usable routing paths" (genuinely
    # actionable — emit the imperative) from "one of several optional providers is
    # unconfigured" (informational — never tell the agent to 'fix' it, which could
    # push it to prompt for a credential that isn't needed).
    paths: list[str] = []          # usable routing paths (at least one → we're fine)
    optional_missing: list[str] = []

    for key, label in [
        ("OPENAI_API_KEY", "OpenAI"),
        ("GEMINI_API_KEY", "Gemini"),
        ("ANTHROPIC_API_KEY", "Anthropic"),
    ]:
        if os.environ.get(key, "").strip():
            paths.append(label)
        elif key == "ANTHROPIC_API_KEY" and _CC_MODE:
            # Claude arrives via the Pro/Max subscription in CC mode — a usable path.
            paths.append("Anthropic (subscription)")
        else:
            optional_missing.append(label)

    # Ollama
    try:
        import subprocess
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, timeout=subprocess_timeout()
        )
        if result.returncode == 0:
            paths.append("Ollama")
        else:
            optional_missing.append("Ollama (not running)")
    except Exception:
        optional_missing.append("Ollama (not found)")

    enforce = os.environ.get("LLM_ROUTER_ENFORCE", "smart")

    lines: list[str] = []

    if not paths:
        # Genuinely actionable: nothing can route. This is the only case that
        # warrants the imperative.
        lines.append("\n⚠️  No routing paths available — LLM Router cannot route.")
        lines.append("  Set an API key (OpenAI/Gemini/Anthropic) or start Ollama before starting.")
    elif optional_missing:
        # Informational only — routing works; these are extra optional providers.
        lines.append(
            "\nℹ️  Optional providers not configured: "
            + ", ".join(optional_missing)
            + " — routing works via " + ", ".join(paths) + "."
        )

    # Enforce mode is a heads-up, not a defect — keep it out of the 'fix' bucket.
    if enforce == "hard":
        lines.append("  ℹ️  LLM_ROUTER_ENFORCE=hard may block tools when no route is available — use smart/off to debug.")

    return "\n".join(lines)


def _format_learned_memory() -> str:
    """Format learned routing profiles for injection into session banner.

    Loads ~/.llm-router/learned_routes.json and formats as:
    【ROUTING MEMORY】
      security_review → opus (learned from 3 corrections)
      ...
    """
    try:
        learned_path = os.path.join(STATE_DIR, "learned_routes.json")
        if not os.path.exists(learned_path):
            return ""

        with open(learned_path) as f:
            learned = json.load(f)

        if not learned:
            return ""

        lines = ["\n【ROUTING MEMORY】"]
        for task_type, route_data in sorted(learned.items()):
            model = route_data.get("model", "?")
            confidence = route_data.get("confidence", 0)
            source = route_data.get("source", "?")
            model_short = model.split("/", 1)[-1] if "/" in model else model
            lines.append(
                f"  {task_type:<20} → {model_short:<20} "
                f"(learned from {confidence} {source})"
            )
        lines.append("  Use llm_reroute to override.")
        return "\n".join(lines)
    except Exception:
        return ""



def _validated_ollama_env_url(raw: str) -> str:
    """CHZ-SEC-06: never hand an unvalidated env URL to urlopen.

    Imported, not reimplemented — three earlier copies of this reader diverged
    and bypassed the fix. Fails CLOSED: an unavailable validator falls back to
    localhost rather than honouring an unchecked URL.
    """
    default = "http://localhost:11434"
    try:
        from llm_router.config import validate_ollama_url
    except Exception:
        return raw if raw == default else default
    return validate_ollama_url(raw) or default

def _warm_ollama_bg() -> None:
    """Fire-and-forget warm-up of the primary Ollama classification model.

    Ollama keeps models resident in memory after first use, but the very
    first call after a server restart (or after the keep-alive window
    expires) has multi-second model-load latency. That latency lands
    directly in the user's first prompt of a new Claude Code session,
    where llm_router's classifier needs Ollama warm to keep classification
    sub-second.

    Detach a background curl that sends a single-character prompt to the
    Ollama generate endpoint. The model loads, returns near-instantly
    (model isn't running yet, so the "compute" is the load itself), and
    stays resident for ``OLLAMA_KEEP_ALIVE`` (default 5m). By the time
    the user hits their first prompt 1-30s later, the classifier call
    finds the model already loaded.

    Opt-out: ``LLM_ROUTER_OLLAMA_WARMUP=off``. Override the model with
    ``LLM_ROUTER_OLLAMA_WARMUP_MODEL`` (default ``qwen3.5:latest`` — the
    model the production chain uses for classification).
    """
    if os.environ.get("LLM_ROUTER_OLLAMA_WARMUP", "on").strip().lower() in ("0", "off", "false", "no"):
        return
    model = os.environ.get("LLM_ROUTER_OLLAMA_WARMUP_MODEL", "qwen3.5:latest")
    base_url = _validated_ollama_env_url(
        os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ).rstrip("/")
    payload = json.dumps({"model": model, "prompt": " ", "stream": False})
    try:
        subprocess.Popen(
            [
                "curl", "-sm", "8", "-o", "/dev/null",
                "-X", "POST", f"{base_url}/api/generate",
                "-H", "Content-Type: application/json",
                "-d", payload,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        # Warm-up is best-effort — never let a curl-spawn failure block
        # session start. If Ollama isn't installed/running, the next
        # routing call will discover that anyway via the chain fallback.
        pass


def _maybe_refresh_benchmarks_bg() -> None:
    """Trigger a background benchmark refresh if the local file is stale.

    Detaches a subprocess immediately so the session-start hook returns in < 1ms.
    Only fires when ``~/.llm-router/benchmarks.json`` is missing or older than
    ``LLM_ROUTER_BENCHMARK_TTL_DAYS`` (default 7 days).
    """
    benchmarks_path = os.path.join(STATE_DIR, "benchmarks.json")
    ttl_days = int(os.environ.get("LLM_ROUTER_BENCHMARK_TTL_DAYS", "7"))

    # Check staleness — if file exists, compare generated_at timestamp.
    stale = True
    if os.path.exists(benchmarks_path):
        try:
            import json as _json
            from datetime import datetime, timezone
            data = _json.loads(open(benchmarks_path).read())
            generated_at_str = data.get("generated_at", "")
            if generated_at_str:
                generated_at = datetime.fromisoformat(generated_at_str)
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - generated_at).days
                stale = age_days >= ttl_days
        except Exception:
            stale = True

    if not stale:
        return

    # Find the project directory (to run with uv).
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    uv_path = subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip()
    if not uv_path:
        return

    script = (
        "from llm_router.benchmark_fetcher import generate_benchmarks_json; "
        f"from pathlib import Path; "
        f"generate_benchmarks_json(output_path=Path('{benchmarks_path}'))"
    )
    try:
        subprocess.Popen(
            [uv_path, "run", "--directory", project_dir, "python", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent session
        )
    except Exception:
        pass  # never block session start


def _maybe_update_pull_routing_rules() -> None:
    """Silently refresh IDE pull-routing rule files if they are out of date.

    Runs at most once per 24h (timestamp in ~/.llm-router/last_rules_check).
    Compares the installed .cursor/rules/use-llm_router.mdc and
    .windsurf/rules/use-llm_router.md against the bundled content from
    install_hooks.py; overwrites silently if they differ.  Never raises.
    """
    try:
        import time as _time
        _check_file = Path(STATE_DIR) / "last_rules_check"
        _now = _time.time()
        if _check_file.exists():
            try:
                _last = float(_check_file.read_text().strip())
                if _now - _last < 86400:  # 24h
                    return
            except (ValueError, OSError):
                pass

        _project = Path(os.getcwd())

        # Load bundled content from install_hooks module
        try:
            from llm_router.install_hooks import _CURSOR_RULE_CONTENT
        except ImportError:
            return  # package not installed — skip

        _updates = []

        # Cursor rules
        _cursor_rules = _project / ".cursor" / "rules" / "use-llm_router.mdc"
        if _cursor_rules.exists():
            try:
                if _cursor_rules.read_text(encoding="utf-8") != _CURSOR_RULE_CONTENT:
                    _cursor_rules.write_text(_CURSOR_RULE_CONTENT, encoding="utf-8")
                    _updates.append("Cursor")
            except OSError:
                pass

        # Windsurf rules (use same routing instructions, adapted label)
        _windsurf_rules = _project / ".windsurf" / "rules" / "use-llm_router.md"
        if _windsurf_rules.exists():
            try:
                _ws_content = _CURSOR_RULE_CONTENT.replace(
                    "Cursor uses pull routing: YOU must call the tool.",
                    "Windsurf uses pull routing: YOU must call the tool.",
                ).replace(
                    "native Cursor intelligence",
                    "native Windsurf intelligence",
                )
                if _windsurf_rules.read_text(encoding="utf-8") != _ws_content:
                    _windsurf_rules.write_text(_ws_content, encoding="utf-8")
                    _updates.append("Windsurf")
            except OSError:
                pass

        if _updates:
            print(
                f"⚡ LLM Router: updated pull-routing rules for {', '.join(_updates)}",
                file=sys.stderr,
            )

        # Record check time
        try:
            Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
            _check_file.write_text(str(_now))
        except OSError:
            pass

    except Exception:
        pass  # never block session start on rules refresh failure


def main() -> None:
    try:
        _hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _hook_input = {}

    # Session Context Accumulator: record Claude Code's real session_id (distinct
    # from SESSION_ID_FILE's fresh-per-session UUID above, which four other
    # consumers depend on and must not be disturbed) so later hooks can resolve
    # it without needing the env var. Fail-open — never blocks session start.
    try:
        from llm_router import session_store as _session_store
        _real_session_id = _hook_input.get("session_id") if isinstance(_hook_input, dict) else None
        if _real_session_id:
            _session_store.write_pointer(_real_session_id)
        _session_store.cleanup_old_sessions()
    except Exception:
        pass

    _reset_session_stats()
    _reset_stale_health()
    # Clear orphaned per-session state files from crashed/killed sessions.
    # Without this, stale files would block Bash/Edit in the new session
    # (pending_route_*.json) and leak old classification verdicts into the
    # length-heuristic fallback path (last_classification_*.json, INV-007).
    import glob as _glob
    _stale_globs = ("pending_route_*.json", "last_classification_*.json")
    for _g in _stale_globs:
        for _stale in _glob.glob(os.path.join(STATE_DIR, _g)):
            try:
                os.unlink(_stale)
            except OSError:
                pass

    hints = ""

    # 1. Ensure Ollama is running (start it if needed)
    hints += _ensure_ollama_running()

    # 1b. pxpipe (opt-in): auto-start the local proxy for heavy-model context
    # compression, then sync Claude Code's own ANTHROPIC_BASE_URL to it (or
    # self-heal it away) so this session's settings.json reflects whether
    # pxpipe actually came up. Takes effect next session, not this one —
    # settings.json is read before this hook ever runs.
    hints += _ensure_pxpipe_running()
    hints += _sync_pxpipe_anthropic_base_url()

    # 2. Select banner from cached subscription state (no OAuth taint in this path).
    # The cache is written by _refresh_claude_usage() during the previous session.
    # Using the cache here keeps the banner print() free of data derived from the
    # live OAuth token, satisfying static-analysis taint tracking.
    try:
        _usage_path = os.path.join(STATE_DIR, "usage.json")
        with open(_usage_path) as _uf:
            _cached_usage = json.load(_uf)
        # RED2-10-02: default to NOT-fallback (i.e. success) when the key is
        # absent. Several success-path usage.json writers (subscription.py,
        # usage-refresh.py) never write is_fallback; the fallback path ALWAYS
        # writes is_fallback=True explicitly. So a missing key means success —
        # the previous `True` default mis-read every such cache as a fallback and
        # showed the wrong banner box. This one-line reader fix covers all writers.
        _cached_sub = not _cached_usage.get("is_fallback", False)
    except Exception:
        _cached_sub = _CC_MODE
    banner = _select_banner(_cached_sub)

    # 3. Refresh Claude usage from OAuth API — updates the cache for next session.
    # Always attempt the refresh — if the OAuth token is present, we're in
    # subscription mode regardless of LLM_ROUTER_CLAUDE_SUBSCRIPTION env var.
    # This makes CC mode detection implicit (token present = CC mode) rather
    # than requiring a .env file that hooks may not have access to.
    usage_hint = _refresh_claude_usage()
    is_subscription = not usage_hint.startswith("\n⚠️")

    hints += usage_hint
    hints += _format_learned_memory()
    hints += _weekly_digest()
    hints += _latency_hint()
    hints += _preflight_check()

    # 5. Trigger benchmark refresh in background if stale (v5.0 adaptive router).
    # Runs as a detached subprocess so the session start is never blocked.
    _maybe_refresh_benchmarks_bg()

    # 6. Warm up Ollama's classifier model in the background so the first
    # prompt of the new session doesn't pay model-load latency on its
    # classification call. Detached, never blocks session start.
    _warm_ollama_bg()

    # Visible UI signal — Claude Code surfaces stderr as
    # "SessionStart:startup hook success: <msg>". Print the BANNER box first
    # so the prominent ╔═══╗ routing summary is the first thing users see,
    # then the painting/welcome below it.
    print(banner, file=sys.stderr)
    print("", file=sys.stderr)
    print(_render_welcome(is_subscription), file=sys.stderr)

    # Pull-routing auto-update: check if IDE rule files in the current
    # project are out of date compared to the bundled version in the package.
    # Runs at most once per day (gated by ~/.llm-router/last_rules_check).
    _maybe_update_pull_routing_rules()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": banner + hints,
        }
    }))


if __name__ == "__main__":
    main()
