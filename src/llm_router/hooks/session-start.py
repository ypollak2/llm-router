#!/usr/bin/env python3
# llm_router-hook-version: 32
"""SessionStart hook — inject routing banner, start Ollama, refresh Claude usage.

Fires once when a new Claude Code session begins. Four jobs:
  1. Auto-start Ollama via start-ollama.sh (free local routing tier).
  2. Refresh Claude subscription usage from the OAuth API (subscription mode only).
  3. Inject a compact routing table at position 0 of the context window,
     so routing rules are always salient regardless of session length.
  4. Reset the session stats tracker so session-end summary is accurate.

Usage refresh (job 2) NEVER blocks session start (owner decision 2026-10-01).
The banner line renders from the last known usage.json immediately (stale
data is fine — labelled with its age); a fresh-enough cache
(LLM_ROUTER_SESSION_START_USAGE_FRESH_S, default 300s) skips the refresh
entirely, otherwise a single detached process re-invokes this script with
``--background-usage-refresh`` to do the keychain + OAuth fetch out of band,
gated by a cooldown (LLM_ROUTER_SESSION_START_USAGE_COOLDOWN_S, default 60s)
so a burst of session starts can't pile up concurrent refreshes. See
_refresh_claude_usage_nonblocking().

Mode detection (auto):
  LLM_ROUTER_CLAUDE_SUBSCRIPTION=true → subscription mode (OAuth pressure cascade)
  otherwise                           → API-key mode (always routes to external providers)
"""

from __future__ import annotations

import json
import os
import sqlite3
import hashlib
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# -- KPI G1: record how long this invocation ran (llm_router.hook_latency) -----
# The clock starts BEFORE the first llm_router import, so the package import is
# inside the measurement. Armed only when run as a script: a test that imports
# this file must not register an exit-time write. Fail-open: no llm_router on
# the path means no row for this run; any other error is reported on stderr
# (never stdout, which the host parses) and the hook carries on.
import time as _hl_time

_HOOK_T0 = _hl_time.monotonic()
if __name__ == "__main__":
    try:
        from llm_router import hook_latency as _hook_latency

        _hook_latency.begin("session-start", "SessionStart", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

# M4.1: name where the time went (phases_ms on the hook_latency row). Both are no-ops
# unless the recorder was armed above, so a test that imports this file records nothing.
try:
    from llm_router.hook_latency import mark_main_start as _hl_mark_main, phase as _hl_phase
except ImportError:  # llm_router is not importable on this host: no recorder, no phases
    import contextlib as _hl_contextlib

    def _hl_phase(name):  # noqa: ARG001
        return _hl_contextlib.nullcontext()

    def _hl_mark_main():
        return None

# Import timeout config from llm_router package if available
try:
    from llm_router.timeout_config import subprocess_timeout, http_timeout
except ImportError:
    # Fallback to hardcoded defaults if llm_router not installed
    def subprocess_timeout() -> int:
        return int(os.environ.get("LLM_ROUTER_SUBPROCESS_TIMEOUT", "15"))
    def http_timeout() -> int:
        return int(os.environ.get("LLM_ROUTER_HTTP_TIMEOUT", "10"))

# Shared gate for the opt-in background benchmark fetch (LLM_ROUTER_AUTO_BENCHMARK_FETCH,
# off by default — North Star #5, local-first). llm_router.benchmarks.maybe_refresh_
# benchmarks_background() is the OTHER trigger that can launch this fetch; importing
# the same check here means the two triggers can't drift on what "opt-in" means.
try:
    from llm_router.benchmarks import benchmark_auto_fetch_enabled
except ImportError:
    # Fallback if llm_router not installed — same values as benchmarks.py.
    def benchmark_auto_fetch_enabled() -> bool:
        return os.environ.get("LLM_ROUTER_AUTO_BENCHMARK_FETCH", "").strip().lower() in (
            "1", "on", "true", "yes",
        )

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


def _state_dir():
    return str(_router_home())
def _session_start_file():
    return os.path.join(_state_dir(), "session_start.txt")
def _session_id_file():
    return os.path.join(_state_dir(), "session_id.txt")
def _session_spend_file():
    return os.path.join(_state_dir(), "session_spend.json")
def _db_path():
    return os.path.join(_state_dir(), "usage.db")
def _weekly_digest_file():
    return os.path.join(_state_dir(), "last_weekly_digest.txt")

# Savings baseline = the latest-Opus host rate, the SAME source of truth as
# cost._OPUS_PRICING / receipt_store / savings_logger. This banner previously
# priced against Sonnet ($3/$15) while every other surface used Opus, so its
# "saved last 7 days" figure disagreed with the llm_savings weekly bucket
# (RETROSPECTIVE B-6). Resolved lazily at use-site (see _weekly_digest) to keep
# this hook import-light; the fallback below is the current Opus rate.
_HOST_IN_PER_M_FALLBACK  = 4.0
_HOST_OUT_PER_M_FALLBACK = 20.0
_FREE_PROVIDERS   = {"ollama", "codex", "gemini_cli"}
_CACHE_PROVIDER   = "cache"  # semantic-cache-served call: not a paid/free/subscription call (provider_classes.py)

# ── .env loader ───────────────────────────────────────────────────────────────
# Hooks run outside the MCP server process and don't inherit its env.
# Load .env so LLM_ROUTER_CLAUDE_SUBSCRIPTION and other settings are available.
def _load_dotenv(load_into: "dict[str, str] | None" = None) -> None:
    """Load .env files into `load_into` (default: os.environ); never override.

    One implementation, shared by every entry point: llm_router.env_loader.
    The real environment always wins; the working directory's .env is filtered
    (SEC-002/003). `load_into` lets tests load into a dict instead of os.environ.
    """
    try:
        from llm_router.env_loader import load_dotenv_files
    except Exception:
        load_dotenv_files = None
    if load_dotenv_files is not None:
        load_dotenv_files(extra_paths=[Path(__file__).resolve().parent.parent.parent.parent / ".env"], target=load_into)
        return
    # llm_router is not importable (plugin bundle run by a bare interpreter):
    # user-level files only. The project's own .env is never trusted without
    # the SEC-002/003 filter that lives in llm_router.env_loader.
    target = os.environ if load_into is None else load_into
    for env_path in (Path(_state_dir()) / ".env", Path.home() / ".env"):
        try:
            text = env_path.read_text()
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in target:
                target[key] = value.strip().strip("\"'")


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
║  enable cloud fallbacks — run `llm-router setup` to configure.    ║
╠════════════════════════════════════════════════════════════════╣
║  routing is advisory; enforce mode decides what is blocked  ║
╚════════════════════════════════════════════════════════════════╝
""".strip()

# Owner decision 2026-10-01: development Q&A is answered by Claude directly, never
# routed to a local model (measured: local Q&A acceptable 3-17% vs 60-100% for
# Sonnet). The default banner therefore teaches the model that, instead of the
# simple/moderate/research -> llm_* table the routing banners above carry. Those
# stay reachable behind LLM_ROUTER_QA_ROUTING=on (same switch as the hooks).
_QUIET_BANNER_BODY = (
    "Questions and analysis are not routed; answer them directly.",
    "Code tasks and local bounded edits keep their routing.",
)
_QUIET_BANNER_MODES = {
    "subscription": "subscription mode",
    "api": "API-key mode",
    "local": "local mode",
}


def _qa_routing_on() -> bool:
    """True when the owner has re-enabled Q&A routing (LLM_ROUTER_QA_ROUTING=on)."""
    return os.environ.get("LLM_ROUTER_QA_ROUTING", "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _quiet_banner(mode: str) -> str:
    width = 64
    title = f"  \u26a1 llm_router ACTIVE \u2014 {_QUIET_BANNER_MODES[mode]}"
    rows = ["\u2554" + "\u2550" * width + "\u2557", "\u2551" + title.ljust(width) + "\u2551",
            "\u2560" + "\u2550" * width + "\u2563"]
    rows += ["\u2551" + ("  " + line).ljust(width) + "\u2551" for line in _QUIET_BANNER_BODY]
    rows.append("\u255a" + "\u2550" * width + "\u255d")
    return "\n".join(rows)


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
    qa_routing = _qa_routing_on()
    if is_subscription or _CC_MODE:
        return _localize_banner(BANNER_SUBSCRIPTION) if qa_routing else _quiet_banner("subscription")
    if _any_cloud_key():
        return _localize_banner(BANNER_API_KEYS) if qa_routing else _quiet_banner("api")
    # honest: no cloud keys configured
    return _localize_banner(BANNER_LOCAL) if qa_routing else _quiet_banner("local")


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
        "   tip     → run `llm-router summary` to see what this session saved",
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

    config_path = Path(_state_dir()) / "routing.yaml"
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
    os.makedirs(_state_dir(), exist_ok=True)
    try:
        with open(_session_start_file(), "w") as f:
            f.write(str(time.time()))
        with open(_session_id_file(), "w") as f:
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
        tmp = _session_spend_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(fresh, f, indent=2)
        os.replace(tmp, _session_spend_file())
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
    reset_file = os.path.join(_state_dir(), "reset_stale.flag")
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


def _effective_base_url(cwd: str) -> tuple[str | None, str]:
    """(value, where) of ANTHROPIC_BASE_URL this session routes with; (None, "") when unset.

    os.environ first: Claude Code applies settings ``env`` to the process, and hooks
    inherit it, so the merged result is already there. Fallback for a launcher that does
    not: the settings files in Claude Code's own precedence (project local, project,
    user). Stdlib-only, no network (docs/BUGS.md PD-HEALTH-1)."""
    v = os.environ.get("ANTHROPIC_BASE_URL")
    if v and v.strip():
        return v.strip(), "the environment"
    for path in (
        os.path.join(cwd, ".claude", "settings.local.json"),
        os.path.join(cwd, ".claude", "settings.json"),
        os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
    ):
        try:
            with open(path) as fh:
                env = json.load(fh).get("env")
            v = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
        except Exception:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip(), path
    return None, ""


def _routes_to_local_port(value: str | None, ports: list) -> bool:
    from urllib.parse import urlsplit

    if not value:
        return False
    try:
        parts = urlsplit(value if "//" in value else "//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1") and port in ports


def _safe_host(value: str | None) -> str:
    """host[:port] only, via the doctor's redacting helper; never userinfo, path or key."""
    if not value:
        return "(unset)"
    try:
        from llm_router.proxy_liveness import _host_of

        return _host_of(value)
    except Exception:
        return "(not shown)"


def _env_block_sha256(env) -> str:
    """Same bytes as the doctor repair's env_block_sha256 (a parity test pins it)."""
    import hashlib

    blob = json.dumps(env if isinstance(env, dict) else None, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _record_settings_observe(session_id) -> None:
    """P0.14-c: one ``observe`` row per session start in settings_writes.jsonl, so the
    next unexplained removal of env.ANTHROPIC_BASE_URL (2026-10-08, writer unknown) is
    bracketed between two session starts. Read-only on settings.json; only when
    proxy-default is installed (sentinel present); never prints the URL."""
    state = _state_dir()
    if not os.path.exists(os.path.join(state, "proxy_default.json")):
        return
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "observe",
           "session_id": session_id if isinstance(session_id, str) else None,
           "settings_exists": os.path.exists(path), "base_url_present": False,
           "env_sha256": None, "mtime": None}
    try:
        row["mtime"] = os.stat(path).st_mtime
        with open(path) as fh:
            data = json.load(fh)
        env = data.get("env") if isinstance(data, dict) else None
        v = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
        row["base_url_present"] = isinstance(v, str) and bool(v.strip())
        row["env_sha256"] = _env_block_sha256(env)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        row["base_url_present"] = None  # unreadable: unknown, not absent
    with open(os.path.join(state, "settings_writes.jsonl"), "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _check_proxy_default_health() -> str:
    """Warn at session start if the default proxy (`commands/proxy_default.py`,
    ``llm-router install --proxy-default``) is installed but not answering.

    This CANNOT fix the session that is currently starting: ``settings.json``'s
    ``env.ANTHROPIC_BASE_URL`` is read by Claude Code before its API client is
    constructed, and this hook only runs after that — the exact same timing
    `_sync_pxpipe_anthropic_base_url` above documents for its own
    self-heal ("takes effect next session, not this one"). So unlike that
    function, this one does not try to rewrite settings.json at all; it can
    only tell the operator what is wrong and how to fix it before their next
    call fails too. Investigated for this feature (2026-09-30): there is no
    SessionStart hook mechanism that overrides the base URL for the session
    already in flight.

    Stdlib-only (a raw TCP connect, not `llm_router.proxy_default.proxy_health`)
    so this hook keeps working even in a fresh subprocess where the `llm_router`
    package import fails — same reasoning as the timeout_config import at the
    top of this file.
    """
    sentinel_path = os.path.join(_state_dir(), "proxy_default.json")
    if not os.path.exists(sentinel_path):
        return ""  # proxy-default was never installed — nothing to check
    try:
        with open(sentinel_path) as fh:
            sentinel = json.load(fh)
        port = int(sentinel.get("port", 8787))
        upstream = sentinel.get("upstream_port")
        upstream = int(upstream) if upstream is not None else None
    except Exception:
        return ""  # an unreadable sentinel is not evidence of a dead proxy

    if sentinel.get("enabled") is False:
        return ""
    ports = [port] + ([upstream] if upstream is not None else [])
    # Project settings live under the project root Claude Code launched in, which
    # hooks receive as CLAUDE_PROJECT_DIR; cwd is only a fallback (a hook's cwd can
    # drift into a subdirectory).
    value, where = _effective_base_url(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if not _routes_to_local_port(value, ports):
        # Installed, but this session does not use it: a dead port is irrelevant,
        # silent bypass is the problem. Unless the owner declined routing on
        # purpose (`llm-router doctor --fix-routing --decline`, P0.14-c).
        if sentinel.get("routing_opt_out"):
            return ""
        missing = (
            f"{where} sets it to {_safe_host(value)}" if value
            else "~/.claude/settings.json env.ANTHROPIC_BASE_URL is missing "
                 "(and no project settings or environment sets it)"
        )
        restore = "llm-router install --proxy-default" if value else (
            "llm-router doctor --fix-routing  (or llm-router install --proxy-default); "
            "removed on purpose: llm-router doctor --fix-routing --decline"
        )
        return (
            f"\n⚠️  llm-router proxy-default is installed but this session does not route "
            f"through 127.0.0.1:{port}: {missing}, so routing is OFF.\n"
            f"    Restore (takes effect next session):  {restore}"
        )

    import socket as _socket

    def _answers(p: int) -> bool:
        try:
            with _socket.create_connection(("127.0.0.1", p), timeout=1.0):
                return True
        except OSError:
            return False

    # Literals, not imported from llm_router.proxy_default.LABEL / SHIM_LABEL:
    # this hook is stdlib-only by design (see the docstring above) and must
    # print the same strings even when the package itself fails to import.
    # Kept in sync by hand; tests/test_session_start_proxy_default.py asserts
    # they agree.
    label = "com.llm_router.proxy"
    shim_label = "com.llm_router.proxy-shim"

    if _answers(port):
        # With the fail-open shim (docs/BUGS.md P010-1) `port` is the shim's, and
        # the shim accepts even when the main proxy behind it is dead: probe
        # the main proxy too, or a crash-looping proxy reads as healthy while
        # every call silently bypasses routing.
        if upstream is None or _answers(upstream):
            return ""  # answering — nothing to say
        return (
            f"\n⚠️  llm-router main proxy is not answering on 127.0.0.1:{upstream} — "
            f"the fail-open shim on :{port} is sending every call straight to "
            f"api.anthropic.com, so routing is bypassed (recorded as proxy_down in "
            f"fail_open.jsonl).\n"
            f"    Recover with:  launchctl kickstart -k gui/$(id -u)/{label}"
            f"   (macOS)  or  systemctl --user restart llm_router-proxy  (Linux)\n"
            f"    Logs: {os.path.join(_state_dir(), 'logs', 'proxy.err.log')}"
        )

    if upstream is not None:
        return (
            f"\n⚠️  llm-router fail-open shim is installed but not answering on "
            f"127.0.0.1:{port} — every API call this session (and every session "
            f"until this is fixed) will fail.\n"
            f"    Recover with:  launchctl kickstart -k gui/$(id -u)/{shim_label}"
            f"   (macOS)  or  systemctl --user restart llm_router-proxy-shim  (Linux)\n"
            f"    Or disable it:  llm-router install --proxy-default off\n"
            f"    Logs: {os.path.join(_state_dir(), 'logs', 'proxy-shim.err.log')}"
        )
    return (
        f"\n⚠️  llm-router proxy-default is installed but not answering on "
        f"127.0.0.1:{port} — every API call this session (and every session "
        f"until this is fixed) will fail.\n"
        f"    Recover with:  launchctl kickstart -k gui/$(id -u)/{label}"
        f"   (macOS)  or  systemctl --user restart llm_router-proxy  (Linux)\n"
        f"    Or disable it:  llm-router install --proxy-default off\n"
        f"    Logs: {os.path.join(_state_dir(), 'logs', 'proxy.err.log')}"
    )


def _check_ledger_silence() -> str:
    """P0.14-d: 0 proxy ledger rows in the last 30 min while organic Claude Code turns were
    recorded and proxy-default is on (``proxy_liveness.ledger_silence``; the same rule
    ``kpi`` and ``doctor`` print). Reads only the ledgers' tails. Needs ``llm_router``;
    without it, or on any error, says nothing (an unreadable ledger is not a silent one)."""
    try:
        from llm_router.proxy_liveness import ledger_silence

        silence = ledger_silence()
    except Exception:  # noqa: BLE001 -- a liveness check must never break session start
        return ""
    return f"\n⚠️  llm-router {silence['message']}" if silence.get("silent") else ""


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
            os.makedirs(_state_dir(), exist_ok=True)
            usage_path = os.path.join(_state_dir(), "usage.json")
            snap_path = os.path.join(_state_dir(), "session_start_cc_pct.json")
            
            snapshot = {
                "session_pct": result["session_pct"],
                "weekly_pct": result["weekly_pct"],
                "sonnet_pct": result["sonnet_pct"],
                "highest_pressure": result["highest_pressure"],
                # Carried through so the two writers of usage.json agree on its
                # shape. Absent here, a session start silently removed it.
                "session_resets_at": result.get("session_resets_at", ""),
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
    os.makedirs(_state_dir(), exist_ok=True)
    usage_path = os.path.join(_state_dir(), "usage.json")
    snap_path = os.path.join(_state_dir(), "session_start_cc_pct.json")
    
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


def _usage_fresh_threshold_s() -> float:
    """How young ``usage.json`` must be to skip a refresh entirely.

    Owner decision 2026-10-01: the SessionStart hook must never block on the
    OAuth usage fetch. Default 5 minutes.
    """
    try:
        return float(os.environ.get("LLM_ROUTER_SESSION_START_USAGE_FRESH_S", "300"))
    except (TypeError, ValueError):
        return 300.0


def _usage_refresh_cooldown_s() -> float:
    """Minimum gap between background-refresh spawns, so a burst of fast
    session starts (e.g. several terminal tabs opened together) cannot pile
    up concurrent keychain + OAuth calls."""
    try:
        return float(os.environ.get("LLM_ROUTER_SESSION_START_USAGE_COOLDOWN_S", "60"))
    except (TypeError, ValueError):
        return 60.0


def _usage_refresh_spawn_file() -> str:
    return os.path.join(_state_dir(), "usage_refresh_spawn.txt")


def _read_cached_usage() -> dict | None:
    """Parsed ``usage.json``, or None when missing/unreadable/corrupt."""
    usage_path = os.path.join(_state_dir(), "usage.json")
    try:
        with open(usage_path) as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _usage_json_age_sec(cached: dict | None) -> float | None:
    """Age in seconds of ``cached``'s ``updated_at``, or None when ``cached``
    is None or carries no usable timestamp.

    A missing/null ``updated_at`` is unknown, not zero: coercing it to 0 would
    make an absent timestamp compare as "infinitely old" (``time.time() - 0``)
    rather than "no reading yet", so a missing key is read as None explicitly
    before any numeric comparison.
    """
    if not cached:
        return None
    raw = cached.get("updated_at")
    if raw is None:
        return None
    try:
        updated_at = float(raw)
    except (TypeError, ValueError):
        return None
    if updated_at <= 0:
        return None
    return time.time() - updated_at


def _usage_refresh_marker_age() -> float | None:
    """Seconds since the spawn marker was last claimed, or None if absent."""
    try:
        age = time.time() - os.path.getmtime(_usage_refresh_spawn_file())
    except OSError:
        return None
    # Future-dated marker (clock skew, restored backup): negative age must
    # count as expired, not as "inside the cooldown" forever.
    return age if age >= 0 else float("inf")


def _claim_usage_refresh_spawn(cooldown_s: float) -> bool:
    """Atomically claim the right to start ONE background refresh.

    Reuses ``llm_router.file_lock.exclusive_lock`` (already used by
    ``session_store`` for its own cross-process critical section) rather than
    a hand-rolled create/rename dance: a non-blocking ``flock`` on a sibling
    ``.lock`` file serializes the check-then-touch of the marker across both
    threads and processes, so two hooks racing at the same instant cannot
    both see "stale" and both proceed. ``timeout=0`` means a hook that loses
    the race returns immediately rather than waiting — SessionStart must
    never block here either. Any failure (can't import, can't lock, read-only
    state dir, ...) means no claim: a refresh that cannot even record itself
    must not be able to pile up.
    """
    path = _usage_refresh_spawn_file()
    try:
        from llm_router.file_lock import exclusive_lock
    except Exception:
        # Without the lock helper, fall back to the plain cooldown check. An
        # import failure is permanent on that install, so failing closed here
        # would switch the background refresh off for good.
        try:
            age = _usage_refresh_marker_age()
            if age is not None and age < cooldown_s:
                return False
            os.makedirs(_state_dir(), exist_ok=True)
            with open(path, "w") as f:
                f.write(str(time.time()))
            return True
        except Exception:
            return False
    try:
        from pathlib import Path

        os.makedirs(_state_dir(), exist_ok=True)
        with exclusive_lock(Path(path + ".lock"), timeout=0.0) as locked:
            if not locked:
                return False
            age = _usage_refresh_marker_age()
            if age is not None and age < cooldown_s:
                return False
            with open(path, "w") as f:
                f.write(str(time.time()))
            return True
    except Exception:
        return False


def _release_usage_refresh_claim() -> None:
    try:
        os.unlink(_usage_refresh_spawn_file())
    except OSError as _exc:
        # T-14: still fail-open (a stuck marker just means the next session
        # start waits out the cooldown instead of retrying immediately), but
        # no longer silent about it.
        try:
            from llm_router import failopen as _fo
            _fo.record("CHZ-FO-SESSION-START-USAGE-CLAIM-RELEASE", _exc)
        except Exception:  # noqa: BLE001
            pass


def _background_usage_refresh_argv() -> list[str]:
    """argv that re-runs THIS script as the detached refresher.

    From a standalone (frozen) build ``sys.executable`` is the binary, not an
    interpreter, so a hook is launched as ``<binary> run-hook <script>``
    (see ``install_hooks._python_exe`` and ``cli.py`` run-hook, which hands the
    trailing args to the script as its argv). ``is_frozen`` is reused from
    install_hooks rather than re-implemented; if llm_router cannot be imported
    we are not in a frozen build, so the plain interpreter form is correct.
    """
    try:
        from llm_router.install_hooks import is_frozen

        frozen = is_frozen()
    except Exception:
        frozen = False
    if frozen:
        return [sys.executable, "run-hook", __file__, "--background-usage-refresh"]
    return [sys.executable, __file__, "--background-usage-refresh"]


def _spawn_background_usage_refresh() -> None:
    """Detach a background process that runs the SAME keychain + OAuth
    refresh this hook used to run inline (see ``_refresh_claude_usage``), so
    SessionStart itself never waits on it. The caller holds the spawn claim.

    The retry logic, keychain read, and OAuth HTTP call are untouched — only
    WHERE they run changes. Never raises; a failed spawn releases the claim
    (so the next session start retries) and must not break session start.
    """
    if not _spawn_detached(_background_usage_refresh_argv()):
        _release_usage_refresh_claim()


def _spawn_detached(argv: list[str]) -> bool:
    """Start ``argv`` detached (own session, no stdio), the one way this hook
    starts a re-run of itself. True when it started. Never raises."""
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _usage_is_measured(cached: object) -> bool:
    """True only for a usage.json that carries real percentages.

    ``install_hooks.seed_usage_json()`` writes ``{"pending": True, ...}`` with
    no pct fields on every install; a cache missing the fields is equally "not
    measured yet" and must never render as 0%.
    """
    if not isinstance(cached, dict) or cached.get("pending"):
        return False
    for key in ("session_pct", "weekly_pct", "sonnet_pct", "highest_pressure"):
        v = cached.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
    return True


def _write_session_baseline(cached: object) -> None:
    """Keep ``session_start_cc_pct.json`` (session-end's delta baseline)
    written on EVERY session start, as the inline refresh used to.

    Measured, non-fallback cache -> its values (no network). Otherwise
    (pending/missing/unmeasured/fallback) -> the same 50% fallback marker
    ``_refresh_claude_usage`` writes when a refresh fails. Only the baseline
    file is written here, never usage.json, so a pending seed stays pending.
    The background refresher overwrites the baseline when it succeeds.
    """
    if _usage_is_measured(cached) and not cached.get("is_fallback"):
        snapshot = dict(cached)
    else:
        snapshot = {
            "session_pct": 50,
            "weekly_pct": 50,
            "sonnet_pct": 50,
            "highest_pressure": 0.5,
            "updated_at": time.time(),
            "is_fallback": True,
        }
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(os.path.join(_state_dir(), "session_start_cc_pct.json"), "w") as f:
            json.dump(snapshot, f)
    except OSError:
        pass


def _usage_hint_from_cache(cached: object, age_sec: float | None) -> str:
    """Render the banner usage line from the last known ``usage.json``
    (already read by the caller) without ever waiting on a live refresh. Past
    the fresh threshold, the line is explicitly labelled stale with its age
    rather than presented as current data.
    """
    if not isinstance(cached, dict):
        return "\n⚠️  Usage: no cached data yet — refreshing in background"
    if not _usage_is_measured(cached):
        return "\n⚠️  Usage: not measured yet — refreshing in background"

    if cached.get("is_fallback"):
        return "\n⚠️  Usage: last refresh failed (50% pressure fallback) — retrying in background"

    session_pct = cached["session_pct"]
    weekly_pct = cached["weekly_pct"]
    sonnet_pct = cached["sonnet_pct"]
    highest_pressure = cached["highest_pressure"]
    pressure_str = f"session={session_pct:.0f}% weekly={weekly_pct:.0f}% sonnet={sonnet_pct:.0f}%"

    age_note = ""
    if age_sec is not None and age_sec > _usage_fresh_threshold_s():
        age_note = f" (stale, {int(age_sec / 60)}m old)"

    if highest_pressure >= 0.95:
        return f"\n🔴 Usage: {pressure_str}{age_note} — ALL external (full pressure)"
    if highest_pressure >= 0.85:
        return f"\n🟡 Usage: {pressure_str}{age_note} — partial pressure active"
    return f"\n✅ Usage: {pressure_str}{age_note}"


def _refresh_claude_usage_nonblocking() -> str:
    """Never block SessionStart on the OAuth usage fetch.

    Owner decision 2026-10-01: returns promptly using the last known
    ``usage.json`` (stale data is fine — labelled as such past the fresh
    threshold). When the cache is stale, missing, pending (fresh install) or a
    failed-refresh fallback, kicks off exactly one background refresh,
    gated by an exclusive cooldown claim so a burst of session starts cannot
    pile up concurrent keychain/OAuth calls. The session-end baseline is still
    written on every start (``_write_session_baseline``). The keychain read and
    OAuth call themselves (``_refresh_claude_usage`` /
    ``_refresh_claude_usage_attempt``) are unchanged.
    """
    cached = _read_cached_usage()
    age_sec = _usage_json_age_sec(cached)
    # A fallback write carries a current updated_at but holds no real data, and
    # a pending seed carries none: neither may count as fresh.
    fresh = (
        _usage_is_measured(cached)
        and not cached.get("is_fallback")
        and age_sec is not None
        and age_sec < _usage_fresh_threshold_s()
    )

    _write_session_baseline(cached)

    if not fresh and _claim_usage_refresh_spawn(_usage_refresh_cooldown_s()):
        _spawn_background_usage_refresh()

    return _usage_hint_from_cache(cached, age_sec)


def _run_background_usage_refresh_entrypoint() -> None:
    """Entry point for the detached child spawned by
    ``_spawn_background_usage_refresh``. Runs the exact retry/keychain/OAuth
    logic that used to run inline in ``main()`` — unchanged — just out of
    process so it can never block SessionStart.
    """
    _refresh_claude_usage()


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
        # When the 5h window refreshes. The same endpoint has always carried it
        # next to the utilization figure this function already reads; dropping it
        # here is why the statusline's ⏰ segment never rendered. usage-refresh.py
        # was taught to persist it, but THIS writer overwrites usage.json at every
        # session start, so a key only one of two writers knew about was wiped
        # within a minute of being written.
        session_resets_at = (data.get("five_hour") or {}).get("resets_at", "")

        return {
            "success": True,
            "session_pct": round(session_pct, 1),
            "weekly_pct": round(weekly_pct, 1),
            "sonnet_pct": round(sonnet_pct, 1),
            "highest_pressure": round(highest_pressure, 4),
            "session_resets_at": session_resets_at,
        }
    except Exception:
        return {"success": False}


def _seats_hint() -> str:
    """One line naming the subscriptions this machine is logged in to.

    Uses the cache written by install/doctor when it is fresh (24 h), and
    re-detects with a short budget otherwise, so a session start never waits
    on a slow CLI. Empty string when the seats module is unavailable.
    """
    try:
        from llm_router import seats as _seats
    except ImportError:
        return ""
    try:
        cached = _seats.load_seats()
        if cached is None or cached.is_stale():
            cached = _seats.refresh_seats(timeout=2.0)
    except Exception:  # noqa: BLE001
        return ""
    return "\n💺 Seats: " + cached.summary_line()


def _weekly_digest() -> str:
    """Return a one-line weekly savings summary shown on Mondays (or after 6+ day gap).

    Queries usage.db directly — no import from the package needed.
    Writes a timestamp file so it fires at most once per week.
    """
    today = datetime.now()
    is_monday = today.weekday() == 0

    # Check last-shown timestamp
    try:
        with open(_weekly_digest_file()) as f:
            last_ts = float(f.read().strip())
        since_last = time.time() - last_ts
        if since_last < 6 * 86400:     # shown within the last 6 days — skip
            return ""
    except (OSError, ValueError):
        if not is_monday:
            return ""   # First run — only show on Mondays

    if not os.path.exists(_db_path()):
        return ""

    try:
        conn = sqlite3.connect(_db_path())
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
            if provider == _CACHE_PROVIDER:
                continue
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
            with open(_weekly_digest_file(), "w") as f:
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
    if not os.path.exists(_db_path()):
        return ""
    try:
        conn = sqlite3.connect(_db_path())
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
        learned_path = os.path.join(_state_dir(), "learned_routes.json")
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

# The model _warm_edit_model_bg() loaded this session (with the edit call's own
# num_ctx / keep_alive), so _warm_ollama_bg() does not reload it at server defaults.
_EDIT_WARMED_MODEL: str | None = None


def _warm_ollama_bg() -> None:
    """Fire-and-forget warm-up of the primary Ollama classification model.

    Skips the model ``_warm_edit_model_bg`` has just loaded WITH the edit call's
    num_ctx and keep_alive: warming it again here with the server defaults would
    make Ollama reload it at a different context, undoing the edit warm-up.

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
    ``LLM_ROUTER_OLLAMA_WARMUP_MODEL`` (default: the first installed model — the
    model the production chain uses for classification).
    """
    if os.environ.get("LLM_ROUTER_OLLAMA_WARMUP", "on").strip().lower() in ("0", "off", "false", "no"):
        return
    # Discovery, not a guess. This defaulted to qwen3.5:latest, which is not
    # installed here, so the warm-up 404'd and warmed nothing on EVERY session
    # while reporting success. Another fixed name would rot the same way.
    model = os.environ.get("LLM_ROUTER_OLLAMA_WARMUP_MODEL", "").strip()
    if not model:
        try:
            from llm_router.model_discovery import first_installed
            model = first_installed() or ""
        except Exception:
            model = ""
    if not model:
        return          # nothing installed; nothing to warm
    if _EDIT_WARMED_MODEL:
        # The edit model was just loaded and pinned (keep_alive=-1). Warming any
        # other model here would evict it on a box that holds one model, so the
        # generic warm-up yields whenever the edit warm-up ran.
        return
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


def _warm_edit_model_bg() -> str | None:
    """Plan 3.7: load the zero-Claude edit model with the SAME model/num_ctx/keep_alive
    the edit call sends, so the first edit of the session finds it resident.
    Detached; returns the model warmed, or None (scope off, opted out, no spawn)."""
    global _EDIT_WARMED_MODEL
    try:
        from llm_router.warm import warm_edit_model_bg
        _EDIT_WARMED_MODEL = warm_edit_model_bg()
    except Exception:
        _EDIT_WARMED_MODEL = None
    return _EDIT_WARMED_MODEL


def _ollama_contention_hint() -> str:
    """Plan 3.7: warn when 2+ models are resident (they share one slot and have
    evicted/corrupted each other). Only when zero-Claude edits are on, since that
    is the path it breaks. Empty string when fine or unknown."""
    if os.environ.get("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "").strip().lower() != "edit":
        return ""
    try:
        from llm_router.warm import dedicated_server_warning
        warning = dedicated_server_warning()
    except Exception:
        return ""
    return f"\n⚠️ {warning}" if warning else ""


def _ollama_watchdog_hint() -> str:
    """One line when the persisted watchdog state says the local Ollama is hung
    (accepts connections, does not generate). Empty when fine, unknown or off."""
    try:
        from llm_router.ollama_watchdog import enabled, hung_hint
        hint = hung_hint() if enabled() else None
    except Exception:
        return ""
    return f"\n{hint}" if hint else ""


def _ollama_watchdog_bg() -> None:
    """Detach a hang check of the local Ollama (a real 1-token generate; /api/tags
    cannot see a wedged runner). Non-blocking; rate-limited and single-flight inside
    the module; restart is opt-in there (LLM_ROUTER_OLLAMA_WATCHDOG_RESTART)."""
    try:
        from llm_router.ollama_watchdog import enabled
        if not enabled():
            return
        subprocess.Popen(
            [sys.executable, "-m", "llm_router.ollama_watchdog"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except Exception:
        pass  # never block session start


def _drain_judge_queue_bg() -> None:
    """Detach a background drain of the judge grading queue.

    CHZ-JUDGE-QUEUE. The hot path only enqueues sampled responses
    (`judge.enqueue_for_grading`) — it never calls a judge model itself, so
    grading has to happen somewhere out of band. This spawns exactly the way
    `_warm_ollama_bg` and `_maybe_reindex_okf_bg` already do: a detached
    subprocess so session start is never delayed by it, and a failure here is
    invisible to (and never blocks) the user's turn. The same drain is also
    reachable directly via `llm-router judge drain` for anyone who wants to
    run it on a schedule instead of piggybacking on session start.

    Opt-out: LLM_ROUTER_JUDGE_AUTODRAIN=0.
    """
    if os.environ.get("LLM_ROUTER_JUDGE_AUTODRAIN", "").strip().lower() in ("0", "off", "false", "no"):
        return
    script = (
        "import asyncio; from llm_router.judge import drain_queue; "
        "asyncio.run(drain_queue())"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass  # never block session start


def _maybe_refresh_benchmarks_bg() -> None:
    """Trigger a background benchmark refresh if the local file is stale.

    Opt-in only (North Star #5, local-first): off unless
    ``LLM_ROUTER_AUTO_BENCHMARK_FETCH=1`` is set. Without it, llm-router would
    reach out to huggingface.co / github / litellm on every session whose
    ``~/.llm-router/benchmarks.json`` is missing or stale, with no consent —
    the bundled copy in ``data/benchmarks.json`` is always enough to route.

    When enabled, detaches a subprocess immediately so the session-start hook
    returns in < 1ms. Only fires when ``~/.llm-router/benchmarks.json`` is
    missing or older than ``LLM_ROUTER_BENCHMARK_TTL_DAYS`` (default 7 days).
    """
    if not benchmark_auto_fetch_enabled():
        return  # opt-in only — no network fetch without explicit consent (no
        # debug log here: this hook, unlike auto-route.py, does not write to
        # auto-route-debug.log; skipped branches elsewhere in this file
        # follow the same silent-return convention, e.g. _warm_ollama_bg and
        # _maybe_reindex_okf_bg's own env-gate checks above)
    benchmarks_path = os.path.join(_state_dir(), "benchmarks.json")
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


def _maybe_reindex_okf_bg(cwd: str | None = None) -> None:
    """Refresh this project's OKF index in the background, if it looks stale.

    WHY, precisely — because the obvious reason is wrong. A stale index does NOT
    cause the grounding gate to reject a valid citation:

      * `grounding_violations` checks the DISK (`Path(path).exists()`), so a file
        created seconds ago already passes.
      * `symbol_violations` consults the index, but since 2026-09-15 it falls back
        to `git grep --untracked` before calling a name invented. That is what
        closes the within-session gap, and re-indexing cannot: you write a
        function at 14:20 and ask about it at 14:21, and any index built at
        session start is already stale.

    What a stale index actually costs is RETRIEVAL. `okf.find_relevant` cannot
    return a document it has never seen, so a prompt about a new module gets no
    injected context, the OKF RESCUE arm cannot fire, and the prompt falls through
    to Claude for want of material rather than for want of capability.

    Detached exactly like `_warm_ollama_bg` and `_maybe_refresh_benchmarks_bg`, so
    the first prompt is never delayed. The index takes seconds on a 1200-file
    repo; doing it inline would be felt on every session.

    Note the index covers TRACKED files only (`git ls-files`), so an uncommitted
    new file is never indexed however often this runs — another reason the disk
    fallback in `symbol_violations`, not this, is what fixes the rejection.

    Off with LLM_ROUTER_OKF_AUTOINDEX=0.
    """
    if os.environ.get("LLM_ROUTER_OKF_AUTOINDEX", "").strip() in ("0", "off", "false", "no"):
        return
    root = cwd or os.getcwd()
    try:
        if not os.path.isdir(os.path.join(root, ".git")):
            return          # not a project; nothing to index
    except Exception:
        return

    # Only when something actually changed since the last index. The stamp is per
    # project, so switching repos re-indexes the new one rather than skipping it.
    ttl_h = float(os.environ.get("LLM_ROUTER_OKF_AUTOINDEX_TTL_H", "6") or 6)
    stamp = os.path.join(
        _state_dir(), "okf_index_stamp",
        hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:16],
    )
    try:
        age_h = (time.time() - os.path.getmtime(stamp)) / 3600.0
        if age_h < ttl_h and not _repo_changed_since(root, os.path.getmtime(stamp)):
            return
    except OSError:
        pass                # no stamp yet — index

    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass

    # index_project takes a Path, not a str. Passing a str raised AttributeError
    # in the detached child — which, with stderr going to DEVNULL, failed in total
    # silence and looked exactly like "the index is up to date". A background task
    # that cannot report its own failure is worse than no background task, so the
    # child writes its outcome to a log the stamp points at.
    log = stamp + ".log"
    script = (
        "from pathlib import Path; from llm_router import okf; "
        f"okf.index_project(Path({root!r}))"
    )
    try:
        with open(log, "w") as errfh:
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=errfh,
                start_new_session=True,
            )
    except Exception:
        pass                # never block session start


def _repo_changed_since(root: str, since: float) -> bool:
    """Has any tracked source file been modified since *since*?

    Cheap enough to run inline: `git status --porcelain` on a warm repo is a few
    milliseconds, and it answers the only question that matters — is there
    anything new to index at all.
    """
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root, capture_output=True, text=True, timeout=2.0,
        )
        return bool(r.stdout.strip())
    except Exception:
        return True         # unknown means index; a wasted index is cheap


def _maybe_update_pull_routing_rules() -> None:
    """Silently refresh IDE pull-routing rule files if they are out of date.

    Runs at most once per 24h (timestamp in ~/.llm-router/last_rules_check).
    Compares the installed .cursor/rules/use-llm_router.mdc and
    .windsurf/rules/use-llm_router.md against the bundled content from
    install_hooks.py; overwrites silently if they differ.  Never raises.
    """
    try:
        import time as _time
        _check_file = Path(_state_dir()) / "last_rules_check"
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
            Path(_state_dir()).mkdir(parents=True, exist_ok=True)
            _check_file.write_text(str(_now))
        except OSError:
            pass

    except Exception:
        pass  # never block session start on rules refresh failure


# ── P0.9: everything not needed for the first prompt runs in ONE detached child ──
#
# PLAN v16 P0.9-a: session-start p95 <= 2,000 ms. Live p95 was 16,178 ms (n = 65,
# [HL7]). The sync path used to start Ollama (start-ollama.sh waits up to 10 s),
# run `ollama list` (_preflight_check), re-detect seats (up to 2 s), query usage.db
# twice (digest, latency hint), probe Ollama for co-resident models, sync pxpipe,
# run git for the OKF index check and spawn five background processes. None of it
# is needed before the first prompt. main() now keeps the session tag, the stale-
# state reset, the proxy health line, the banner from cached usage and the
# additionalContext, and spawns one child (``--background-session-work``) for the
# rest. The child's hint lines are cached in ``session_start_hints.json`` and the
# NEXT session start shows them (a line describes the machine, so one session old
# is fine; older than ``_HINTS_MAX_AGE_S`` is dropped).

_HINTS_FILENAME = "session_start_hints.json"
_HINTS_MAX_AGE_S = 24 * 3600


def _hints_cache_path() -> str:
    return os.path.join(_state_dir(), _HINTS_FILENAME)


def _read_cached_hints(now: float | None = None) -> str:
    """Hint lines the last background run wrote; "" when missing, unreadable or
    older than ``_HINTS_MAX_AGE_S``. Never raises."""
    try:
        with open(_hints_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        ts, hints = data.get("ts"), data.get("hints")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not isinstance(hints, str):
            return ""
        if (time.time() if now is None else now) - float(ts) > _HINTS_MAX_AGE_S:
            return ""
        return hints
    except Exception:  # noqa: BLE001 -- a missing cache is the first-run normal
        return ""


def _write_cached_hints(hints: str) -> None:
    """Atomically replace the hint cache. Never raises."""
    try:
        path = _hints_cache_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "hints": hints}, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- next session simply shows no hints
        return


def _background_session_work_argv(cwd: str, session_id: str = "") -> list[str]:
    """argv that re-runs THIS script as the detached session-work child (frozen
    builds go through ``run-hook``, as ``_background_usage_refresh_argv`` does).
    The Claude Code session id rides along for the GE6 start sample."""
    try:
        from llm_router.install_hooks import is_frozen

        frozen = is_frozen()
    except Exception:
        frozen = False
    if frozen:
        return [sys.executable, "run-hook", __file__, "--background-session-work", cwd, session_id or ""]
    return [sys.executable, __file__, "--background-session-work", cwd, session_id or ""]


def _spawn_background_session_work(cwd: str, session_id: str = "") -> None:
    """Detach the child that does the session-start work the first prompt does
    not need. Never raises: a failed spawn costs the hints and warm-ups, never
    the session start."""
    _spawn_detached(_background_session_work_argv(cwd, session_id))


def _quota_start_sample(session_id: str) -> None:
    """GE6 / S3: one quota sample (cached usage.json, no network) for this
    session's start into quota_samples.jsonl; the Stop hook appends the rest.
    No session id (an older spawn) writes nothing: append_session_sample refuses it."""
    from llm_router import quota_samples as _quota_samples

    _quota_samples.append_session_sample(session_id, "start")


def _run_background_session_work(cwd: str, session_id: str = "") -> None:
    """The detached child: the GE6 quota start sample first (its time should be
    the session's start, before seconds of Ollama start), then start Ollama, sync pxpipe, build the hint lines and
    cache them for the next session start, then the warm-ups, indexers, judge
    drain, watchdog and the daily rules refresh. Each step is fail-open, so one
    failure never skips the rest."""
    def _step(fn, *args):
        try:
            return fn(*args) or ""
        except Exception:  # noqa: BLE001 -- one failed step must not skip the rest
            return ""

    _step(_quota_start_sample, session_id)
    hints = ""
    hints += _step(_ensure_ollama_running)
    hints += _step(_ensure_pxpipe_running)
    hints += _step(_sync_pxpipe_anthropic_base_url)
    for fn in (_seats_hint, _format_learned_memory, _weekly_digest, _latency_hint,
               _preflight_check, _ollama_contention_hint, _ollama_watchdog_hint):
        hints += _step(fn)
    _write_cached_hints(hints)

    _step(_maybe_refresh_benchmarks_bg)
    _step(_maybe_reindex_okf_bg, cwd)
    _step(_warm_edit_model_bg)
    _step(_warm_ollama_bg)
    _step(_drain_judge_queue_bg)
    _step(_ollama_watchdog_bg)
    _step(_maybe_update_pull_routing_rules)


def main() -> None:
    _hl_mark_main()
    try:
        _hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _hook_input = {}

    # Verifier PR C (SHADOW): when delegated patches are waiting, start ONE detached
    # `python -m llm_router.verify_worker` (fixed argv, DEVNULL, own session, env allowlist,
    # flock + cooldown; see llm_router.verify_queue). Never waits for it. Fail-open, recorded.
    try:
        from llm_router import verify_queue as _verify_queue
        _verify_queue.spawn_worker_if_needed()
    except Exception as _vq_exc:  # noqa: BLE001
        try:
            from llm_router import failopen as _fo
            _fo.record("CHZ-FO-VERIFY-WORKER-SPAWN", _vq_exc)
        except Exception:  # noqa: BLE001
            pass

    # Session Context Accumulator: record Claude Code's real session_id (distinct
    # from SESSION_ID_FILE's fresh-per-session UUID above, which four other
    # consumers depend on and must not be disturbed) so later hooks can resolve
    # it without needing the env var. Fail-open — never blocks session start.
    try:
        from llm_router.hook_latency import set_session as _hl_set_session

        _hl_set_session(_hook_input.get("session_id") if isinstance(_hook_input, dict) else None)
    except Exception:  # noqa: BLE001 -- older llm_router without set_session: no session on the row
        pass

    try:
        from llm_router import session_store as _session_store
        _real_session_id = _hook_input.get("session_id") if isinstance(_hook_input, dict) else None
        with _hl_phase("session_io"):
            if _real_session_id:
                _session_store.write_pointer(_real_session_id)
            _session_store.cleanup_old_sessions()
    except Exception:
        pass

    # KPI session tagging (organic / research / harness / headless): persisted
    # per session so the proxy and the ledgers can tag rows. Fail-open.
    try:
        from llm_router import session_kind as _session_kind
        if isinstance(_hook_input, dict):
            with _hl_phase("session_io"):
                _session_kind.tag_session(_hook_input.get("session_id"), _hook_input.get("cwd"))
    except Exception:
        pass

    with _hl_phase("reset_state"):
        _reset_session_stats()
        _reset_stale_health()
    # Clear orphaned per-session state files from crashed/killed sessions.
    # Without this, stale files would block Bash/Edit in the new session
    # (pending_route_*.json) and leak old classification verdicts into the
    # length-heuristic fallback path (last_classification_*.json, INV-007).
    import glob as _glob
    _stale_globs = ("pending_route_*.json", "last_classification_*.json")
    with _hl_phase("reset_state"):
        for _g in _stale_globs:
            for _stale in _glob.glob(os.path.join(_state_dir(), _g)):
                try:
                    os.unlink(_stale)
                except OSError:
                    pass

    hints = ""

    # 1/1b. Ollama start and pxpipe sync run in the background child (P0.9).
    # 1c. Proxy-default (opt-in via `llm-router install --proxy-default`):
    # warn loudly if it's installed but dead. Cannot self-heal this session
    # (see the function's own docstring for why) — only the next one.
    with _hl_phase("proxy_health"):
        hints += _check_proxy_default_health()
        hints += _check_ledger_silence()
    with _hl_phase("session_io"):
        try:
            _record_settings_observe(_hook_input.get("session_id") if isinstance(_hook_input, dict) else None)
        except Exception:  # noqa: BLE001 -- an observe row must never block session start
            pass

    # 2. Select banner from cached subscription state (no OAuth taint in this path).
    # The cache is written by _refresh_claude_usage() during the previous session.
    # Using the cache here keeps the banner print() free of data derived from the
    # live OAuth token, satisfying static-analysis taint tracking.
    try:
        _usage_path = os.path.join(_state_dir(), "usage.json")
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

    # 3. Claude usage banner line — NEVER blocks on the OAuth API (owner
    # decision 2026-10-01). Returns immediately from the last known
    # usage.json (stale is fine, labelled as such) and, when that cache is
    # stale or missing, detaches exactly one background process to refresh
    # it for next time (cooldown-gated against pile-ups). If the OAuth token
    # is present, we're in subscription mode regardless of
    # LLM_ROUTER_CLAUDE_SUBSCRIPTION env var — this makes CC mode detection
    # implicit (token present = CC mode) rather than requiring a .env file
    # that hooks may not have access to.
    with _hl_phase("usage"):
        usage_hint = _refresh_claude_usage_nonblocking()
    is_subscription = not usage_hint.startswith("\n⚠️")

    hints += usage_hint
    # The other hint lines come from the last background run (P0.9).
    with _hl_phase("hints"):
        hints += _read_cached_hints()

    # 5-6c. GE6 quota start sample, benchmarks, OKF index, model warm-ups, judge
    # drain, watchdog, Ollama start, pxpipe, the slow hint lines and the daily
    # rules refresh: one detached child (P0.9-a). Never blocks session start.
    _sid = _hook_input.get("session_id") if isinstance(_hook_input, dict) else None
    with _hl_phase("bg_spawn"):
        _spawn_background_session_work(
            (_hook_input.get("cwd") if isinstance(_hook_input, dict) else None)
            or os.getcwd(),
            _sid if isinstance(_sid, str) else "",
        )

    # Visible UI signal — Claude Code surfaces stderr as
    # "SessionStart:startup hook success: <msg>". Print the BANNER box first
    # so the prominent ╔═══╗ routing summary is the first thing users see,
    # then the painting/welcome below it.
    with _hl_phase("banner"):
        print(banner, file=sys.stderr)
        print("", file=sys.stderr)
        print(_render_welcome(is_subscription), file=sys.stderr)

    # Pull-routing auto-update runs in the background child (P0.9).

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": banner + hints,
        }
    }))


_BACKGROUND_FLAGS = ("--background-usage-refresh", "--background-session-work")


def _entry(argv: list[str]) -> None:
    """Dispatch on argv: a detached background child, or the hook itself."""
    if any(flag in argv for flag in _BACKGROUND_FLAGS):
        # A background child re-runs this file, so the latency stanza at the top
        # armed a "session-start" row for it too: its seconds of Ollama start,
        # keychain and OAuth were then counted as session-start latency (BUGS
        # P09-3). The recorder checks this switch when it writes, at exit.
        os.environ["LLM_ROUTER_HOOK_LATENCY"] = "off"
    if "--background-usage-refresh" in argv:
        # The detached child spawned by _spawn_background_usage_refresh(): only
        # the usage refresh, writing usage.json, never the rest of SessionStart.
        _run_background_usage_refresh_entrypoint()
    elif "--background-session-work" in argv:
        # The P0.9 child spawned by _spawn_background_session_work(); its
        # arguments are the session's project directory and its session id.
        i = argv.index("--background-session-work")
        _run_background_session_work(argv[i + 1] if len(argv) > i + 1 else os.getcwd(),
                                     argv[i + 2] if len(argv) > i + 2 else "")
    else:
        main()


if __name__ == "__main__":
    _entry(sys.argv[1:])
