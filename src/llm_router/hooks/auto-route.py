#!/usr/bin/env python3
# llm-router-hook-version: 21
"""UserPromptSubmit hook — scoring classifier with Ollama + API fallback chain.

Classification chain (stops at first success):
  1. Skip patterns → truly local operations, no routing
  2. Heuristic scoring (instant, free) → high-confidence match routes immediately
  3. Ollama local LLM (free, 1-3s) → catches what heuristics miss
  4. Cheap API model (GPT-4o-mini/Gemini Flash, ~$0.0001) → when Ollama unavailable
  5. Weak heuristic match (score > 0 but below threshold)
  6. Auto fallback → llm_route (LLM router's own classifier)

Scoring uses three signal layers:
  Intent patterns  (+3) — action verbs, clear task markers
  Topic patterns   (+2) — domain-specific nouns and terms
  Format patterns  (+1) — structural cues, temporal markers
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ── v6.0 Visibility: HUD integration ─────────────────────────────────────────
try:
    from llm_router.statusline_hud import initialize_hud
except ImportError:
    def initialize_hud():
        """Fallback stub if statusline_hud is unavailable."""
        pass

# ── Model tracking integration ────────────────────────────────────────────────
try:
    from llm_router.model_tracking import log_routing_decision
except ImportError:
    def log_routing_decision(*args, **kwargs):
        """Fallback stub if model_tracking is unavailable."""
        pass

try:
    from llm_router.profiles import ROUTING_TABLE
    from llm_router.types import RoutingProfile, TaskType
except ImportError:
    ROUTING_TABLE = {}
    RoutingProfile = None
    TaskType = None

# ── .env loader (reads llm-router's .env for API keys) ──────────────────────

_ENV_PATHS = [
    Path(__file__).resolve().parent.parent.parent / ".env",  # project root .env
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

# ── Config ───────────────────────────────────────────────────────────────────

OLLAMA_MODEL = os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "gemma4:latest")
OLLAMA_URL = os.environ.get("LLM_ROUTER_OLLAMA_URL", "http://localhost:11434")
OLLAMA_TIMEOUT = int(os.environ.get("LLM_ROUTER_OLLAMA_TIMEOUT", "5"))
CONFIDENCE_THRESHOLD = int(os.environ.get("LLM_ROUTER_CONFIDENCE_THRESHOLD", "4"))
DISABLE_LLM_CLASSIFIERS = os.environ.get("LLM_ROUTER_DISABLE_LLM_CLASSIFIERS", "").lower() in (
    "1",
    "true",
    "yes",
)

# API keys for cheap fallback (read from env or .env files)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")

# Claude Code subscription mode — Claude models used via subscription, never API.
# NEW ROUTING PHILOSOPHY (v7.3+): Always route through MCP first for cost minimization.
# Claude subscription is a FALLBACK, not the first choice.
#
# Routing strategy by complexity:
#   simple   → Route via llm_query  (Ollama → Codex → Gemini Flash → Groq)
#             Claude Haiku available as fallback at <99% pressure
#   moderate → Route via llm_route  (Ollama → Codex → Gemini Pro → Claude Sonnet)
#             Find cheapest suitable option; Claude Sonnet last resort
#   complex  → Route via llm_route  (Ollama → Codex → o3 → Claude Opus)
#             Quality-first; Claude Opus fallback when others unavailable
#
# Hard cap enforcement at ≥99% pressure:
#   ALL → Route external only, block Claude entirely to protect quota limits
#
# Cost impact: Reduces Claude subscription usage by 50-90% for simple/moderate tasks
# while maintaining quality for complex work.
_CC_MODE = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")


def _get_pressure() -> dict[str, float]:
    """Read per-bucket Claude subscription pressure from usage.json or SQLite.

    Returns keys: session (5h window), sonnet (weekly Sonnet), weekly (all models)
    as fractions 0.0–1.0.

    Staleness handling (v7.5+ TTL-based):
    - Always validate cache age against TTL (default 300s)
    - Removed session_pct >= 70% gate — refresh regardless of pressure level
    - If cache fresh (age < TTL): use directly
    - If cache stale (age >= TTL): attempt inline refresh before routing
    - If no cache or OAuth fails: use conservative fallback (0.0)
    """
    usage_path = Path.home() / ".llm-router" / "usage.json"
    ttl_seconds = int(os.environ.get("LLM_ROUTER_QUOTA_TTL", "300"))

    def _frac(d: dict, key: str) -> float:
        v = float(d.get(key, 0.0))
        return v / 100.0 if v > 1.0 else v  # normalise: percent→fraction

    try:
        raw = json.loads(usage_path.read_text())
        age_s = time.time() - float(raw.get("updated_at", 0))
        is_fresh = age_s < ttl_seconds

        # Always validate TTL, refresh if stale
        if not is_fresh:
            # Attempt inline refresh regardless of pressure level
            fresh = _fetch_usage_inline()
            if fresh:
                return {
                    "session": _frac(fresh, "session_pct"),
                    "sonnet":  _frac(fresh, "sonnet_pct"),
                    "weekly":  _frac(fresh, "weekly_pct"),
                }

        # Cache is fresh or refresh failed — use cached values
        return {
            "session": _frac(raw, "session_pct"),
            "sonnet":  _frac(raw, "sonnet_pct"),
            "weekly":  _frac(raw, "weekly_pct"),
        }
    except Exception:
        pass

    # SQLite fallback — reads most recent claude_usage row
    db_path = Path.home() / ".llm-router" / "usage.db"
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=1)
        row = conn.execute(
            "SELECT messages_used, messages_limit FROM claude_usage ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[1] and row[1] > 0:
            p = min(1.0, row[0] / row[1])
            return {"session": p, "sonnet": p, "weekly": p}
    except Exception:
        pass

    return {"session": 0.0, "sonnet": 0.0, "weekly": 0.0}


def _apply_pressure_downgrade(complexity: str, pressure: dict[str, float]) -> tuple[str, str]:
    """Downgrade complexity when subscription budget pressure is high.

    When Sonnet or weekly quota is ≥95% exhausted, reduce task complexity to stay
    within cheaper model tiers (simple → Haiku, moderate → Sonnet fallback).

    Args:
        complexity: Original complexity level ('simple', 'moderate', 'complex')
        pressure: Dict with 'sonnet' and 'weekly' keys (fractions 0.0–1.0)

    Returns:
        (downgraded_complexity, suffix_message) tuple. suffix_message is empty string
        if no downgrade occurred.
    """
    sonnet_pct = pressure.get("sonnet", 0.0)
    weekly_pct = pressure.get("weekly", 0.0)

    if sonnet_pct >= 0.95 or weekly_pct >= 0.95:
        if complexity == "complex":
            return "moderate", " [⬇ sonnet-exhausted: complex→moderate]"
        if complexity == "moderate":
            return "simple", " [⬇ sonnet-exhausted: moderate→simple]"
    elif sonnet_pct >= 0.85:
        if complexity == "complex":
            return "moderate", " [⬇ sonnet-high: complex→moderate]"

    return complexity, ""


_USAGE_JSON = str(Path.home() / ".llm-router" / "usage.json")
# Inline refresh fires when data is stale AND last known session ≥ this threshold.
# Below this threshold, stale data is safe to use (pressure is low, risk of hitting
# limits is small). At 70%+ the window is closing fast enough to justify the ~300ms
# OAuth round-trip to get fresh data before every routing decision.
_INLINE_REFRESH_PRESSURE_FLOOR = 0.70
# Minimum interval between inline refreshes (avoid hammering the API on every prompt).
_INLINE_REFRESH_MIN_INTERVAL_SEC = 120  # 2 minutes


def _fetch_usage_inline() -> dict | None:
    """Live OAuth refresh of Claude subscription data — called when usage.json is stale.

    Uses the macOS Keychain (security command) to get the OAuth access token,
    then calls the Anthropic usage API. Writes fresh data to usage.json atomically.
    Returns the parsed data dict, or None on any failure (network, no token, etc.).
    """
    if sys.platform != "darwin":
        return None  # Keychain only available on macOS
    try:
        # Filter environment to exclude API keys and sensitive tokens
        safe_env = {k: v for k, v in os.environ.items() 
                   if not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASS", "AUTH"))}
        
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=4,
            env=safe_env,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        token = json.loads(r.stdout.strip()).get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return None
    except Exception:
        return None

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None

    try:
        s = float(data.get("five_hour",       {}).get("utilization", 0.0))
        w = float(data.get("seven_day",        {}).get("utilization", 0.0))
        n = float(data.get("seven_day_sonnet", {}).get("utilization", 0.0))
        result = {
            "session_pct": round(s, 1),
            "weekly_pct":  round(w, 1),
            "sonnet_pct":  round(n, 1),
            "updated_at":  time.time(),
            "highest_pressure": max(s, w, n),
        }
        state_dir = str(Path.home() / ".llm-router")
        os.makedirs(state_dir, exist_ok=True)
        tmp = _USAGE_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result, f)
        os.replace(tmp, _USAGE_JSON)
        return result
    except Exception:
        return None


def _is_pressure_stale(max_age_seconds: int = 1800) -> bool:
    """Return True if usage.json is missing or older than max_age_seconds (default 30 min).

    Three hooks read usage.json without checking freshness. Stale data causes
    routing decisions based on hours-old quota — either over-routing externally
    (quota refreshed but data says high) or under-routing (quota spiked but data
    says low). A 30-minute threshold balances accuracy vs. noise.
    """
    usage_path = Path.home() / ".llm-router" / "usage.json"
    if not usage_path.exists():
        return True
    return (time.time() - usage_path.stat().st_mtime) > max_age_seconds


def _log_quota_snapshot_sync(
    session_id: str,
    prompt_sequence: int,
    prompt_hash: str | None,
    pressure: dict,
    routing_decision_id: int | None,
    final_model: str | None,
    final_provider: str | None,
    complexity_requested: str | None,
    complexity_used: str | None,
    was_downgraded: bool,
    db_path: str,
) -> None:
    """Log per-prompt quota state to quota_snapshots table for audit trail.
    
    Inline implementation for hook scripts (stdlib-only, no imports needed).
    Captures the quota pressure at the moment a prompt arrived.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute(
                """INSERT INTO quota_snapshots (
                    session_id, prompt_sequence, prompt_hash,
                    claude_session_pct, claude_weekly_pct, claude_sonnet_pct,
                    openai_spent_usd, gemini_spent_usd, ollama_available,
                    cache_age_seconds, was_cache_fresh,
                    routing_decision_id, final_model, final_provider,
                    complexity_requested, complexity_used, was_downgraded
                ) VALUES (?,?,?, ?,?,?, ?,?,?, ?,?, ?,?,?, ?,?,?)""",
                (
                    session_id,
                    prompt_sequence,
                    prompt_hash,
                    pressure.get("session_pct", 0.0),
                    pressure.get("weekly_pct", 0.0),
                    pressure.get("sonnet_pct", 0.0),
                    0.0,  # openai_spent_usd (would need separate query to usage table)
                    0.0,  # gemini_spent_usd (would need separate query to usage table)
                    1,    # ollama_available
                    pressure.get("cache_age_seconds", 0.0),
                    1 if pressure.get("is_fresh", False) else 0,
                    routing_decision_id,
                    final_model,
                    final_provider,
                    complexity_requested,
                    complexity_used,
                    1 if was_downgraded else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Silent failure — quota snapshot is optional enhancement


# ── Skip Patterns (truly local operations) ───────────────────────────────────

# Only skip: slash commands, raw shell one-liners, and pure acknowledgement words.
# Anything that looks like a question or task goes through the classifier so it
# can be routed to Haiku / Ollama instead of burning top-tier model tokens.
SKIP_PATTERNS = re.compile(
    r"^/(?:route|help|clear|compact|init|login|doctor|memory|model|cost|config|"
    r"permissions|review|status|mcp|bug|learn|verify|tdd|plan|eval|claw|loop|"
    r"checkpoint|save-session|resume-session|sessions|instinct|skill|usage)\b|"
    r"^\s*(?:git |npm |pip |uv |cargo |make |docker |brew |curl |wget |"
    r"chmod |mkdir |rm |mv |cp |ls |cd |cat |grep |rtk )\b|"
    r"^\s*(?:yes|no|ok|sure|thanks|thank you|y|n|k|go ahead|do it|looks good|lgtm)\s*$",
    re.IGNORECASE,
)

# ── Build Task Patterns (code fast-path) ─────────────────────────────────────
#
# When a prompt clearly asks for write/edit/fix/implement work, we can skip the
# slower classifier layers and route straight to llm_code. This keeps auto-route
# aligned with the repo rule that coding work still routes through llm_* tools.
# ── Content Generation Detection (v7.4.0+) ────────────────────────────────────
#
# When a prompt involves creating written content (narratives, design specs, cards,
# documentation), route via llm_generate FIRST, then integrate result locally.
# This prevents the routing miss where "add content to file" skips generation routing.
#
# Pattern: "write/create/draft X" → route llm_generate
# Pattern: "add card/section Y to file Z" → route llm_generate then integrate
#

_CONTENT_GENERATION_VERBS = re.compile(
    r"\b(write|draft|compose|create.*content|author|"
    r"rewrite|improve.*wording|refactor.*text|edit.*copy|"
    r"add.*card|add.*section|add.*paragraph|add.*slide|add.*visualization)\b",
    re.IGNORECASE,
)

_CONTENT_FILE_PATTERNS = re.compile(
    r"(?:to|in|at)\s+(?:\w+/)*\w+\.(?:md|txt|rst|html|json|yaml|yml)(?:\s|$|,)",
    re.IGNORECASE,
)

_DECOMPOSITION_PATTERNS = re.compile(
    r"(?:write|generate|create|add).*(?:card|section|paragraph|narrative|blueprint|spec).*"
    r"(?:to|in)\s+\w+\.(?:md|blueprint|carousel)",
    re.IGNORECASE,
)


def _is_content_generation_task(prompt: str) -> bool:
    """Return True when the prompt involves creating written content.
    
    Detects patterns like:
    - "write narrative about X"
    - "add carousel card about Y to file.md"
    - "create design spec for Z"
    - "draft documentation for module"
    
    These should route via llm_generate FIRST, then integrate locally.
    """
    has_generation_verb = bool(_CONTENT_GENERATION_VERBS.search(prompt))
    
    # Simple generation: just the verb
    if has_generation_verb and not _CONTENT_FILE_PATTERNS.search(prompt):
        return True
    
    # Decomposition pattern: "add X to file.md" = generate + integrate
    if _DECOMPOSITION_PATTERNS.search(prompt):
        return True
    
    return False


#
# Criteria: must have BOTH a build verb AND a build object to avoid false positives.
# "implement" alone might be "how do I implement X?" → still route to query.
# "implement the budget oracle in budget.py" → clearly a coding task → don't route.

_BUILD_VERBS = re.compile(
    r"\b(implement|build|write|create|add|fix|refactor|update|modify|edit|scaffold|"
    r"migrate|port|integrate|wire|connect|code|develop|finish|complete|continue "
    r"implement|phase \d|start phase|begin phase)\b",
    re.IGNORECASE,
)
_BUILD_OBJECTS = re.compile(
    r"\b(function|class|module|file|test|hook|endpoint|migration|script|"
    r"the code|\.py\b|\.ts\b|\.go\b|budget\.py|discover\.py|scorer\.py|"
    r"chain_builder\.py|router\.py|types\.py|config\.py|phase \d|"
    r"todo list|task list|checklist)\b|"
    r"\b(in (?:src|tests|hooks|the)[\s/])",
    re.IGNORECASE,
)


def _is_build_task(prompt: str) -> bool:
    """Return True when the prompt clearly asks for code implementation work.

    Requires both a build verb AND a build object — prevents false positives
    like "how do I implement X?" which still routes to llm_query.
    """
    return bool(_BUILD_VERBS.search(prompt)) and bool(_BUILD_OBJECTS.search(prompt))


# ── Session Type Tracking ─────────────────────────────────────────────────────
# Written to ~/.llm-router/session_{id}.json when Claude's first tool call in
# a session is a file edit (Edit/Write/MultiEdit). Once a session is marked
# "coding", enforce-route.py skips all enforcement for the rest of the session.

def _session_type_path(session_id: str) -> "Path":
    return _ROUTER_DIR / f"session_{session_id}.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON to *path* via a same-directory temp file + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
        # Secure the file (may contain routing analysis or session metadata)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def mark_session_coding(session_id: str) -> None:
    """Mark this session as a coding session to disable enforcement."""
    if not session_id:
        return
    try:
        _write_json_atomic(
            _session_type_path(session_id),
            {"session_type": "coding", "marked_at": time.time()},
        )
    except OSError:
        pass


# ── Signal Patterns ──────────────────────────────────────────────────────────

SIGNALS: dict[str, dict[str, re.Pattern]] = {
    "image": {
        "intent": re.compile(
            r"\b(?:generate (?:an? )?(?:image|picture|photo|illustration|graphic|logo|"
            r"icon|banner|thumbnail|avatar|mockup|diagram)|"
            r"create (?:an? )?(?:image|picture|illustration|logo|"
            r"icon|graphic|banner|thumbnail|avatar|mockup|diagram)|"
            r"draw (?:a |an |the |me )?|design (?:a |an )?(?:visual|poster|flyer|card|cover)|"
            r"make (?:a |an )?(?:image|picture|photo|illustration)|"
            r"render|visualize|sketch)\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:artwork|portrait|landscape|scenery|sunset|sunrise|mountain|ocean|forest|city|"
            r"pixel art|wallpaper|infographic|logo|mockup|brand(?:ing)?|"
            r"meme|sticker|sprite|texture|concept art|"
            r"photorealistic|cartoon|anime|watercolor|oil painting|abstract|"
            r"dall-?e|midjourney|stable diffusion|flux)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:in the style of|aesthetic|color palette|aspect ratio|"
            r"resolution|4k|hd|minimalist|flat design|artistic)\b",
            re.IGNORECASE,
        ),
    },
    "query": {
        "intent": re.compile(
            r"\b(?:what does|what(?:'s| is)|how does|explain (?:what|how)|"
            r"define|definition of|describe (?:what|how)|summarize how)\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:rest api|api|foreign key|database index(?:es)?|index(?:es)?|sql|"
            r"os\.path\.join|json|yaml|regex|http|oauth|jwt)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:quick|simple|brief|short|definition|overview|eli5)\b|\?$",
            re.IGNORECASE,
        ),
    },
    "research": {
        "intent": re.compile(
            r"\b(?:research|look up|look into|search for|find out|investigate|discover|"
            r"what(?:'s| is) (?:the )?(?:latest|newest|most recent|current)|"
            r"what happened|who (?:won|raised|acquired|launched|announced|released|founded|created)|"
            r"how (?:much|many) (?:did|has|have|does|were|are|is|was)|"
            r"market analysis|competitive analysis|benchmark|survey|report on|"
            r"check (?:the |if |whether ))\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:funding|fundraise|raised|investment|investor|valuation|ipo|"
            r"series [a-f]|seed round|venture capital|vc|startup|unicorn|"
            r"acquisition|merger|m&a|revenue|growth|market share|"
            r"industry|sector|economy|stock|earnings|quarterly|"
            r"news|announcement|launch|release|update|"
            r"trend|trending|viral|popular|emerging|"
            r"report|study|survey|statistics|data|ranking|ranked|"
            r"regulation|policy|law|legislation|bill|ruling|"
            r"election|political|geopolitical|conflict|"
            r"climate|weather|disaster|pandemic|outbreak|"
            r"sports|championship|tournament|olympics|"
            r"award|prize|winner|nominee|"
            r"company|companies|brand|corporation|firm|"
            r"ceo|founder|executive|leader|"
            r"price|pricing|cost|rate|fee|salary|compensation|"
            r"ai|artificial intelligence|machine learning|llm|gpt|"
            r"crypto|bitcoin|ethereum|blockchain|nft|"
            r"real estate|housing|mortgage|rent)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:top \d+|best \d+|worst \d+|biggest \d+|largest \d+|"
            r"latest|recent|this (?:week|month|year|quarter)|"
            r"in (?:january|february|march|april|may|june|july|august|"
            r"september|october|november|december)|"
            r"in 20\d{2}|today|yesterday|last (?:week|month|year)|"
            r"currently|right now|as of|breaking|"
            r"list of|ranked|ranking|leaderboard|comparison|"
            r"around the world|globally|worldwide)\b",
            re.IGNORECASE,
        ),
    },
    "code": {
        "intent": re.compile(
            r"\b(?:implement|refactor|write (?:a |the )?(?:function|class|module|api|"
            r"endpoint|script|program|test|hook|component|service)|"
            r"build (?:a |the )?(?:app|service|tool|cli|library|package|component|feature)|"
            r"scaffold|boilerplate|port .+ to|migrate|"
            r"fix (?:the |this |a )?(?:\w+ )*(?:bug|error|issue|crash|failing test|exception)|"
            r"add (?:a |the )?(?:\w+ )*(?:feature|method|test|endpoint|route|handler|"
            r"middleware|support|integration|login)|"
            r"update (?:the |this )?(?:\w+ )*(?:code|logic|function|implementation|client|"
            r"api client|service|handler|middleware|endpoint)|"
            r"modify (?:the |this )|extend (?:the |this )|"
            r"(?:optimize|improve) (?:the |this )?(?:code|query|performance|function)|"
            r"set up|configure|install|bootstrap|initialize|"
            r"create (?:(?:a |the )?\w+ )*(?:function|class|module|component|hook|test|script|program|service|tool))\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:function|class|method|constructor|interface|enum|struct|"
            r"module|package|library|dependency|"
            r"endpoint|route|handler|middleware|controller|resolver|client|api client|"
            r"database|schema|migration|orm|"
            r"test|spec|coverage|assertion|mock|fixture|"
            r"algorithm|data structure|linked list|hash map|binary tree|"
            r"authentication|authorization|jwt|oauth|login|dashboard|"
            r"cache|queue|worker|cron|webhook|retry|rate limit|429|response(?:s)?|"
            r"dockerfile|ci/cd|pipeline|github actions|"
            r"linter|formatter|type checker|compiler|bundler)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:in (?:python|typescript|javascript|rust|go|java|kotlin|swift|c\+\+|ruby|php)|"
            r"using (?:react|vue|angular|express|django|flask|fastapi|spring|nextjs)|"
            r"with (?:tests|types|error handling|logging|documentation)|"
            r"async|sync|concurrent|parallel|recursive|iterative)\b",
            re.IGNORECASE,
        ),
    },
    "analyze": {
        "intent": re.compile(
            r"\b(?:analyze|evaluate|assess|review (?:the |this |my )|"
            r"critique|debug|diagnose|"
            r"explain why|root cause|investigate|audit|"
            r"compare (?:and contrast |.+ (?:to|with|vs|versus) |.+ and .+)|"
            r"pros and cons|trade-?offs?|advantages|disadvantages|"
            r"deep dive|what do you think|what(?:'s| is) (?:your |the )?(?:opinion|take|assessment)|"
            r"help me understand|break down|walk me through|"
            r"should (?:I|we)|which (?:is|should|would) (?:be )?(?:better|best|preferred)|"
            r"why (?:did|does|is|was|would|should)|"
            r"what went wrong|what caused|how to improve|"
            r"is (?:it |.{1,30} )?worth|does it make sense)\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:performance|bottleneck|latency|throughput|efficiency|"
            r"security|vulnerability|risk|threat|exposure|"
            r"architecture|system design|design pattern|approach|strategy|"
            r"cost-benefit|roi|impact|outcome|"
            r"quality|reliability|scalability|maintainability|"
            r"trade-?off|decision|choice|option|alternative|"
            r"root cause|failure|incident|outage|regression|"
            r"error|exception|stack trace|traceback|crash|panic|segfault|"
            r"metric|kpi|benchmark|baseline|target|queue|stream(?:s)?|broker|"
            r"replication|logical replication|cdc|background jobs|"
            r"code review|pull request|diff|changeset)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:step by step|in detail|thoroughly|comprehensively|"
            r"with examples|with evidence|with data|"
            r"strengths and weaknesses|swot|"
            r"short-term|long-term|immediate|strategic)\b",
            re.IGNORECASE,
        ),
    },
    "generate": {
        "intent": re.compile(
            r"\b(?:write (?:(?:me |us )?(?:a |an |the )?)?(?:blog|article|email|letter|story|poem|"
            r"tweet|post|description|pitch|proposal|speech|script|outline|copy|"
            r"hero section|faq(?: answers)?|headline|messaging|onboarding copy|"
            r"welcome modal copy|landing page copy|website copy|"
            r"summary|bio|resume|cover letter|announcement|press release|"
            r"newsletter|report|whitepaper|message|response|reply|comment|"
            r"review|testimonial|caption|title|headline|tagline|slogan|"
            r"prompt|template|checklist|guide|tutorial)|"
            r"draft (?:a |an |the |me )?|compose|brainstorm|come up with|"
            r"generate (?:a |some )?(?:text|content|copy|ideas|names|titles)|"
            r"rewrite|translate|paraphrase|rephrase|"
            r"edit (?:the |this )?(?:text|copy|content|writing)|"
            r"make (?:it |this )?(?:sound|more|less )|"
            r"summarize (?:this|the|a )|"
            r"create (?:a |an )?(?:list|outline|plan|agenda|schedule|copy|"
            r"hero section|faq(?: answers)?|headline|messaging|welcome modal copy|"
            r"landing page copy|website copy))\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:blog post|article|essay|email|newsletter|"
            r"marketing copy|ad copy|social media|content strategy|hero section|"
            r"welcome modal|onboarding copy|landing page|website copy|faq answers?|"
            r"pricing page|launch email|"
            r"creative writing|fiction|non-fiction|narrative|"
            r"documentation|readme|changelog|release notes|"
            r"presentation|slide deck|pitch deck|"
            r"contract|agreement|terms of service|privacy policy|"
            r"recipe|itinerary|playlist|agenda)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:formal|informal|casual|professional|friendly|persuasive|"
            r"concise|verbose|detailed|brief|"
            r"bullet points|numbered list|markdown|html|"
            r"for (?:an? )?(?:audience|reader|customer|client|user)|"
            r"word count|characters|paragraphs|sections|tone|voice)\b",
            re.IGNORECASE,
        ),
    },
}

# ── Complexity Patterns ──────────────────────────────────────────────────────

COMPLEXITY_DEEP_REASONING = re.compile(
    r"\b(?:prove (?:that|mathematically|formally)|"
    r"mathematical(?:ly)? (?:prove|derive|show)|"
    r"formal proof|theorem|lemma|axiom|corollary|"
    r"derive from first principles?|first[- ]principles? (?:derivation|analysis|explanation)|"
    r"from (?:the )?fundamentals?|foundational(?:ly)?|"
    r"philosophical(?:ly)? (?:analyze|examine|argue|discuss)|"
    r"what does it mean (?:fundamentally|philosophically|at its core)|"
    r"synthesize (?:the )?research|comprehensive literature review|"
    r"rigorous(?:ly)? (?:analyze|prove|derive|examine)|"
    r"formal(?:ly)? (?:specify|verify|prove)|"
    r"induction|deduction|proof by contradiction|reductio ad absurdum)\b",
    re.IGNORECASE,
)

COMPLEXITY_COMPLEX = re.compile(
    r"\b(?:architect|design system|from scratch|end-to-end|comprehensive|"
    r"novel approach|research paper|synthesis|multi-step|workflow|pipeline|"
    r"in-depth|thorough|detailed plan|full implementation|production|"
    r"scalable|distributed|microservice|security audit|"
    r"compare multiple|across all|entire|complete)\b",
    re.IGNORECASE,
)

COMPLEXITY_SIMPLE = re.compile(
    r"\b(?:quick|simple|short|one-liner|brief|"
    r"summarize|tldr|eli5|just|only|small|tiny|minor)\b",
    re.IGNORECASE,
)

# ── Scoring Engine ───────────────────────────────────────────────────────────

INTENT_WEIGHT = 3
TOPIC_WEIGHT = 2
FORMAT_WEIGHT = 1

LAYER_WEIGHTS = {
    "intent": INTENT_WEIGHT,
    "topic": TOPIC_WEIGHT,
    "format": FORMAT_WEIGHT,
}


def score_categories(text: str) -> dict[str, int]:
    """Score each category using three signal layers."""
    scores: dict[str, int] = {}
    for category, layers in SIGNALS.items():
        total = 0
        for layer_name, weight in LAYER_WEIGHTS.items():
            pattern = layers.get(layer_name)
            if pattern:
                matches = pattern.findall(text)
                unique = len({m.lower() if isinstance(m, str) else m[0].lower() for m in matches})
                total += unique * weight
        scores[category] = total
    return scores


# ── LLM Classifiers ─────────────────────────────────────────────────────────

CLASSIFY_PROMPT = (
    "Classify this user prompt into exactly ONE category. "
    "Reply with ONLY the category name, nothing else.\n\n"
    "Categories:\n"
    "- research: Current events, news, facts, market data, trends, real-world lookups, statistics\n"
    "- generate: Writing, drafting, content creation, brainstorming, emails, articles, summaries\n"
    "- analyze: Evaluation, debugging, comparison, deep reasoning, trade-offs, code review\n"
    "- code: Programming, implementation, building software, fixing bugs, refactoring\n"
    "- query: Simple factual questions, definitions, explanations, how things work\n"
    "- image: Image/visual generation, design, artwork creation\n\n"
    "User prompt: {prompt}\n\n"
    "Category:"
)

VALID_CATEGORIES = {"research", "generate", "analyze", "code", "query", "image"}


def _extract_category(raw: str) -> str | None:
    """Extract a valid category name from LLM response text."""
    for word in re.split(r"[\s,.\n/<>]+", raw.lower()):
        cleaned = word.strip("*`'\"()-")
        if cleaned in VALID_CATEGORIES:
            return cleaned
    return None


OLLAMA_MODELS = [
    "qwen3.5:latest",      # Primary: best reasoning (Feb 2025)
    "qwen3-coder-next",    # Code specialization
    "qwen2.5:latest",      # Secondary fallback
    "gemma4:latest",       # Lightweight validation
]

# Task-specific model selection (code tasks use specialized model)
OLLAMA_CODE_MODELS = [
    "kimi-k2.6:cloud",      # Primary: best for code (256K context, autonomous execution)
    "qwen3-coder-next",     # Secondary: specialized code model
    "qwen3.5:latest",       # Fallback: general reasoning
]


def classify_with_ollama(text: str) -> str | None:
    """Classify using local Ollama. Tries primary model, falls back to smaller.

    Uses the chat API with think=False to disable thinking mode on reasoning
    models (qwen3.5, etc.) — otherwise they waste the token budget on CoT.
    
    Automatically uses qwen3-coder-next for code-looking text, qwen3.5 for others.
    """
    # Detect if this looks like a code task
    code_indicators = re.compile(
        r"\b(refactor|debug|implement|fix|bug|function|class|method|test|import|module)\b",
        re.IGNORECASE
    )
    is_code_task = code_indicators.search(text[:500])
    
    # Select model priority: code-specialized for code tasks, general for others
    models_to_try = OLLAMA_CODE_MODELS if is_code_task else OLLAMA_MODELS
    
    for model in models_to_try:
        try:
            body = json.dumps({
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a task classifier. Reply with ONLY a single category name, nothing else.",
                    },
                    {
                        "role": "user",
                        "content": CLASSIFY_PROMPT.format(prompt=text[:500]),
                    },
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 10},
            }).encode()
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
                result = json.loads(resp.read())
                content = result.get("message", {}).get("content", "")
                category = _extract_category(content)
                if category:
                    return category
        except Exception:
            continue
    return None


def classify_with_openai(text: str) -> str | None:
    """Classify using GPT-4o-mini. ~$0.0001 per call."""
    if not OPENAI_API_KEY:
        return None
    try:
        body = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a task classifier. Reply with ONLY a single category name."},
                {"role": "user", "content": CLASSIFY_PROMPT.format(prompt=text[:500])},
            ],
            "temperature": 0,
            "max_tokens": 10,
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            return _extract_category(content)
    except Exception:
        return None


def classify_with_gemini(text: str) -> str | None:
    """Classify using Gemini Flash. Free tier / near-free."""
    if not GEMINI_API_KEY:
        return None
    try:
        body = json.dumps({
            "contents": [{"parts": [{"text": CLASSIFY_PROMPT.format(prompt=text[:500])}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 10},
        }).encode()
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            content = result["candidates"][0]["content"]["parts"][0]["text"]
            return _extract_category(content)
    except Exception:
        return None


# ── Complexity Classifier ────────────────────────────────────────────────────


def classify_complexity(text: str, task_type: str) -> str:
    """Determine task complexity from text signals."""
    if COMPLEXITY_DEEP_REASONING.search(text):
        return "deep_reasoning"
    if COMPLEXITY_COMPLEX.search(text):
        return "complex"
    if COMPLEXITY_SIMPLE.search(text):
        return "simple"
    if len(text) > 500:
        return "complex"
    if len(text) > 150:
        return "moderate"
    return "simple" if task_type == "query" else "moderate"


# ── Main Classifier ──────────────────────────────────────────────────────────


def classify_prompt(text: str) -> dict | None:
    """Classify using heuristic scoring → Ollama → cheap API → weak heuristic → auto."""
    stripped = text.strip()

    if not stripped or len(stripped) < 8:
        return None
    if SKIP_PATTERNS.search(stripped):
        return None

    # Build task fast-path: deterministic llm_code routing for obvious coding work.
    if _is_build_task(stripped):
        return {
            "task_type": "code",
            "complexity": classify_complexity(text, "code"),
            "method": "build-fast-path",
        }

    # Content generation detection (v7.4.0+): Suggest routing for create/write/add tasks
    if _is_content_generation_task(stripped):
        return {
            "task_type": "generate",
            "complexity": classify_complexity(text, "generate"),
            "method": "content-generation-fast-path",
            "suggestion": "content-generation-decomposition",
        }

    # Layer 1: Heuristic scoring (instant, free)
    scores = score_categories(text)
    best_category = max(scores, key=scores.get)
    best_score = scores[best_category]

    if best_score >= CONFIDENCE_THRESHOLD:
        return {
            "task_type": best_category,
            "complexity": classify_complexity(text, best_category),
            "method": "heuristic",
            "score": best_score,
        }

    # Layer 2: Ollama local LLM (free, 1-3s)
    if not DISABLE_LLM_CLASSIFIERS and len(stripped) >= 10:
        ollama_result = classify_with_ollama(text)
        if ollama_result:
            return {
                "task_type": ollama_result,
                "complexity": classify_complexity(text, ollama_result),
                "method": "ollama",
            }

    # Layer 3: Cheap API model (Gemini Flash first — free tier, then GPT-4o-mini)
    if not DISABLE_LLM_CLASSIFIERS and len(stripped) >= 10:
        api_result = classify_with_gemini(text) or classify_with_openai(text)
        if api_result:
            return {
                "task_type": api_result,
                "complexity": classify_complexity(text, api_result),
                "method": "api",
            }

    # Layer 4: Weak heuristic match (score > 0 but below threshold)
    if best_score > 0:
        return {
            "task_type": best_category,
            "complexity": classify_complexity(text, best_category),
            "method": "heuristic-weak",
            "score": best_score,
        }

    # Layer 5: Question / unknown — treat as query so cheap model (Haiku) handles it.
    # This ensures codebase questions, "why doesn't X work", meta-questions, etc.
    # are routed instead of silently falling through to the top-tier model.
    if len(stripped) >= 8:
        return {
            "task_type": "query",
            "complexity": classify_complexity(text, "query"),
            "method": "fallback",
        }

    return None


# ── MCP Capability Map ───────────────────────────────────────────────────────
#
# Known non-llm-router MCP servers and the intent patterns that match them.
# When the user's prompt matches one of these patterns AND that server's tools
# are available in the current session, we skip issuing an llm_* routing
# directive — Claude should use that MCP server's tools directly.
#
# Pattern order matters: more specific servers are checked first.
_MCP_INTENT_PATTERNS: list[tuple[re.Pattern, list[str]]] = [
    # Obsidian / note-taking
    (re.compile(
        r"\b(?:obsidian|vault|note(?:s)?|zettelkasten|journal entry|"
        r"create (?:a )?note|add (?:a )?note|write (?:a )?note|"
        r"open (?:a )?note|find (?:a )?note|search (?:my )?notes|"
        r"daily note|weekly note|meeting note)\b",
        re.IGNORECASE,
    ), ["obsidian", "mcp-obsidian", "obsidian-mcp"]),

    # GitHub / git hosting
    (re.compile(
        r"\b(?:github|gitlab|gitea|"
        r"open (?:an? )?(?:issue|pr|pull request)|"
        r"create (?:an? )?(?:issue|pr|pull request|gist)|"
        r"list (?:issues|prs|pull requests)|"
        r"search (?:issues|repos|repositories)|"
        r"merge (?:pr|pull request)|close (?:issue|pr))\b",
        re.IGNORECASE,
    ), ["github", "gitlab"]),

    # Google Calendar / scheduling
    (re.compile(
        r"\b(?:calendar|gcal|google calendar|"
        r"(?:schedule|create|add|book) (?:a )?(?:meeting|event|appointment)|"
        r"(?:my )?(?:meetings?|events?) (?:today|tomorrow|this week)|"
        r"free (?:time|slot)|available (?:time|slot)|"
        r"invite .+ to|block (?:time|calendar))\b",
        re.IGNORECASE,
    ), ["google-calendar", "gcal", "calendar"]),

    # Gmail / email
    (re.compile(
        r"\b(?:gmail|"
        r"(?:send|compose|draft|write|reply to) (?:an? )?(?:email|message)|"
        r"(?:check|read|open) (?:my )?(?:email|inbox|messages)|"
        r"email .+ about|forward (?:this|the) email)\b",
        re.IGNORECASE,
    ), ["gmail", "google-mail"]),

    # Slack
    (re.compile(
        r"\b(?:slack|"
        r"(?:send|post|message) (?:in|to|on) (?:#\w+|\w+ channel)|"
        r"(?:check|read) (?:slack|#\w+|the channel)|"
        r"dm .+|direct message .+)\b",
        re.IGNORECASE,
    ), ["slack"]),

    # Linear / Jira / project management
    (re.compile(
        r"\b(?:linear|jira|"
        r"(?:create|open|close|update) (?:a )?(?:ticket|issue|task|story|epic)|"
        r"(?:assign|move) (?:ticket|issue|task)|"
        r"sprint backlog|project board)\b",
        re.IGNORECASE,
    ), ["linear", "jira", "atlassian"]),

    # Notion
    (re.compile(
        r"\b(?:notion|"
        r"(?:create|add|update) (?:a )?(?:notion )?(?:page|database|block)|"
        r"(?:search|find) (?:in )?notion)\b",
        re.IGNORECASE,
    ), ["notion"]),
]


def _build_mcp_capability_map(tools: list[dict]) -> dict[str, list[str]]:
    """Parse available tools into a server → [tool_names] map.

    Only non-llm-router MCP servers are included — llm_* tools are handled
    by the standard routing path. Returns empty dict if no external MCP servers.
    """
    servers: dict[str, list[str]] = {}
    for tool in tools:
        name = tool.get("name", "") if isinstance(tool, dict) else str(tool)
        if not name.startswith("mcp__"):
            continue
        parts = name.split("__", 2)  # ["mcp", "server-name", "tool-name"]
        if len(parts) != 3:
            continue
        server = parts[1]
        if server in ("llm-router", "llm_router"):
            continue  # skip our own tools
        servers.setdefault(server, []).append(parts[2])
    return servers


def _match_mcp_server(prompt: str, capability_map: dict[str, list[str]]) -> str | None:
    """Return the MCP server name if the prompt clearly targets an available server.

    Checks intent patterns in order. Returns the first matching server that is
    actually available in capability_map, or None if no match.
    """
    if not capability_map:
        return None
    available = set(capability_map.keys())
    for pattern, server_hints in _MCP_INTENT_PATTERNS:
        if not pattern.search(prompt):
            continue
        for hint in server_hints:
            # Accept partial matches: "obsidian" matches "mcp-obsidian", etc.
            for server in available:
                if hint in server or server in hint:
                    return server
    return None


# ── Tool Mapping ─────────────────────────────────────────────────────────────

TOOL_MAP = {
    "research": "llm_research",
    "generate": "llm_generate",
    "analyze": "llm_analyze",
    "code": "llm_code",
    "query": "llm_query",
    "image": "llm_image",
    "auto": "llm_route",
}

_ROUTER_DIR = Path.home() / ".llm-router"
_ENFORCEMENT_LOG_PATH = _ROUTER_DIR / "enforcement.log"
_PENDING_ROUTE_TTL_SEC = 60  # 60s per-turn TTL — old directives don't poison new turns

# ── Context-Aware Routing (v2.5) ─────────────────────────────────────────────
# Short continuation prompts inherit the prior turn's classification so the
# full Ollama/API classifier chain isn't re-invoked for "ok do it" / "yes" etc.
_LAST_ROUTE_TTL = 1800  # 30 minutes — reuse context within same working session

_CONTINUATION_RE = re.compile(
    r"^(?:yes|no|ok|okay|sure|yep|nope|y|n|"
    r"continue|proceed|go ahead|do it|do that|"
    r"sounds good|great|perfect|agreed|correct|right|"
    r"wait|hmm|actually|and|also|but|"
    r"stop|skip|cancel)\s*[!?.]*$",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^(?:no|nope|n|stop|skip|cancel|wait|nevermind|never mind)\s*[!?.]*$",
    re.IGNORECASE,
)


def _pending_state_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"pending_route_{session_id}.json"


def _read_pending_state(session_id: str) -> dict | None:
    path = _pending_state_path(session_id)
    try:
        data = json.loads(path.read_text())
        if time.time() - float(data.get("issued_at", 0)) > _PENDING_ROUTE_TTL_SEC:
            path.unlink(missing_ok=True)
            return None
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _clear_pending_state(session_id: str) -> None:
    _pending_state_path(session_id).unlink(missing_ok=True)


def _log_unrouted_turn(session_id: str, pending: dict) -> None:
    expected_tool = pending.get("expected_tool", "llm_route")
    task_type = pending.get("task_type", "?")
    complexity = pending.get("complexity", "?")
    try:
        _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with _ENFORCEMENT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] NO_ROUTE session={session_id[:12]} "
                f"expected={expected_tool} task={task_type}/{complexity}\n"
            )
    except OSError:
        pass


def _consume_unresolved_pending(session_id: str) -> dict | None:
    pending = _read_pending_state(session_id)
    if pending is None:
        return None
    _log_unrouted_turn(session_id, pending)
    _clear_pending_state(session_id)
    return pending


def _last_route_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"last_route_{session_id}.json"


def _save_last_route(session_id: str, task_type: str, complexity: str, tool: str) -> None:
    if not session_id:
        return
    try:
        _write_json_atomic(
            _last_route_path(session_id),
            {
                "task_type": task_type,
                "complexity": complexity,
                "tool": tool,
                "saved_at": time.time(),
            },
        )
    except OSError:
        pass


def _load_last_route(session_id: str) -> dict | None:
    if not session_id:
        return None
    path = _last_route_path(session_id)
    try:
        data = json.loads(path.read_text())
        if time.time() - float(data.get("saved_at", 0)) > _LAST_ROUTE_TTL:
            path.unlink(missing_ok=True)
            return None
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _is_continuation(prompt: str) -> bool:
    """Return True if the prompt looks like a continuation of the prior task.

    Matches short affirmatives/negatives (yes/ok/go ahead/stop/…) that carry no
    new task signal of their own — these should inherit the prior route rather
    than re-triggering the full classifier chain.
    """
    stripped = prompt.strip()
    if _CONTINUATION_RE.match(stripped):
        return True
    # Also treat very short prompts (≤5 words) with zero heuristic signal as continuations
    words = stripped.split()
    if 1 <= len(words) <= 5:
        scores = score_categories(stripped)
        if max(scores.values(), default=0) == 0:
            return True
    return False


def _is_short_code_followup(prompt: str, last_route: dict | None) -> bool:
    """Return True if prompt is a short follow-up after a code task.

    Short prompts (≤15 words) after a code classification inherit the code
    context rather than being re-classified as generate/query via the fallback.
    Example: "explain why the dashboard doesn't update" (7 words) after editing
    code would otherwise score 0 on heuristics and fall through to query/generate.
    """
    if last_route is None:
        return False
    if last_route.get("task_type") != "code":
        return False
    words = prompt.strip().split()
    return 1 <= len(words) <= 15


def _prior_violation_notice(pending: dict | None) -> str:
    if pending is None:
        return ""
    expected_tool = pending.get("expected_tool", "llm_route")
    task_type = pending.get("task_type", "?")
    complexity = pending.get("complexity", "?")
    return (
        "⚠ PREVIOUS TURN VIOLATED ROUTING: "
        f"expected {expected_tool} for {task_type}/{complexity}, but no llm_* tool was called. "
        "This was logged.\n"
    )


def _load_learned_routes() -> dict[str, dict]:
    """Load learned routing overrides from ~/.llm-router/learned_routes.json.

    Returns:
        Dict mapping task_type → {model, confidence, source, last_correction}
        Empty dict if file doesn't exist or is invalid.
    """
    try:
        learned_path = Path.home() / ".llm-router" / "learned_routes.json"
        if not learned_path.exists():
            return {}
        return json.loads(learned_path.read_text())
    except Exception:
        return {}


def _check_learned_override(task_type: str, learned_routes: dict) -> tuple[str, str] | None:
    """Check if task_type has a learned route override with confidence >= 3.

    Args:
        task_type: The classified task type (e.g., "code", "analyze")
        learned_routes: Loaded learned routes dict

    Returns:
        Tuple of (tool, method_suffix) if override applies, else None
    """
    if task_type not in learned_routes:
        return None

    route_data = learned_routes[task_type]
    confidence = route_data.get("confidence", 0)

    # Only apply if confidence >= 3 (locked in)
    if confidence < 3:
        return None

    model = route_data.get("model", "")
    if not model:
        return None

    # Infer tool from model name
    tool = "llm_route"  # fallback
    if "claude" in model.lower():
        # Claude models shouldn't be used here (subscription mode routes via MCP anyway)
        # but if a user learned this, respect it
        tool = "llm_route"
    elif "gpt" in model.lower() or "openai" in model.lower():
        # Likely a coding/analysis task for external model
        tool = "llm_code"
    elif "gemini" in model.lower():
        tool = "llm_query"  # or llm_analyze, but query is conservative
    else:
        tool = "llm_route"

    method_suffix = f" [learned route: {model}]"
    return (tool, method_suffix)


# ── Entry Point ──────────────────────────────────────────────────────────────

def _get_selected_model(task_type: str, complexity: str) -> tuple[str, str]:
    """Get the selected model for a task type/complexity pair.
    
    Returns:
        (model_name, provider) tuple. Falls back to ("unknown", "unknown") if not found.
    """
    if not ROUTING_TABLE or not TaskType or not RoutingProfile:
        return "unknown", "unknown"
    
    try:
        # Map task_type string to TaskType enum
        task_map = {
            "query": TaskType.QUERY,
            "research": TaskType.RESEARCH,
            "generate": TaskType.GENERATE,
            "analyze": TaskType.ANALYZE,
            "code": TaskType.CODE,
            "image": TaskType.IMAGE,
            "video": TaskType.VIDEO,
            "audio": TaskType.AUDIO,
        }
        task_enum = task_map.get(task_type.lower())
        if not task_enum:
            return "unknown", "unknown"
        
        # Use BALANCED profile as default (can be customized per user)
        profile_enum = RoutingProfile.BALANCED
        
        # Get the routing chain
        chain = ROUTING_TABLE.get((profile_enum, task_enum))
        if not chain or not chain:
            return "unknown", "unknown"
        
        # Return the first (selected) model
        selected = chain[0]
        # Extract provider from "provider/model" format
        provider = selected.split("/")[0] if "/" in selected else selected
        
        # For Ollama, enhance with the actual model name from auto-route config
        if provider == "ollama":
            # Read from OLLAMA_BUDGET_MODELS env var (set in .env)
            ollama_models = os.environ.get("OLLAMA_BUDGET_MODELS", "").split(",")
            if ollama_models and ollama_models[0].strip():
                selected = f"ollama/{ollama_models[0].strip()}"
            else:
                # Fallback to configured model
                selected = f"ollama/{OLLAMA_MODEL}"
        
        return selected, provider
    except Exception:
        return "unknown", "unknown"


_DEBUG_LOG = Path.home() / ".llm-router" / "auto-route-debug.log"


def _debug_log(msg: str) -> None:
    """Log debug info to help diagnose hook invocation issues."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_DEBUG_LOG, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass  # Silently fail if logging doesn't work


def main() -> None:
    invocation_id = time.time()
    _debug_log(f"[INVOCATION START] ID={invocation_id:.3f}")

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _debug_log(f"[INVOCATION {invocation_id:.3f}] JSON parse failed, exiting")
        sys.exit(0)

    prompt = hook_input.get("prompt", "")
    _debug_log(f"[INVOCATION {invocation_id:.3f}] prompt_len={len(prompt)} session_id={hook_input.get('session_id', 'unknown')[:8]}")
    if not prompt:
        sys.exit(0)

    session_id = hook_input.get("session_id", "")

    # ── v6.0 Visibility: Initialize HUD session state ─────────────────────────
    initialize_hud()

    previous_unrouted = _consume_unresolved_pending(session_id) if session_id else None

    # ── MCP capability check — runs before LLM classification ────────────────
    # If the prompt clearly targets an available non-llm-router MCP server
    # (Obsidian, GitHub, Calendar, etc.), skip the routing directive entirely.
    # Claude should use that server's tools directly — no cheap-LLM routing needed.
    raw_tools = hook_input.get("tools", [])
    capability_map = _build_mcp_capability_map(raw_tools)
    matched_server = _match_mcp_server(prompt, capability_map)
    if matched_server:
        # Emit an informational hint (not mandatory) so Claude knows why no directive
        server_tools = capability_map.get(matched_server, [])
        tool_hint = f"mcp__{matched_server}__{server_tools[0]}" if server_tools else f"mcp__{matched_server}__*"
        hint = (
            f"💡 MCP ROUTE: {matched_server} — use {tool_hint} tools for this task. "
            f"No llm_* routing needed — {matched_server} handles it directly."
        )
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "contextForAgent": hint,
            }
        }
        _debug_log(f"[INVOCATION {invocation_id:.3f}] EARLY EXIT: direct MCP route to {matched_server}")
        json.dump(output, sys.stdout)
        sys.exit(0)

    # ── Context-Aware Routing (v2.5) ─────────────────────────────────────────
    # Short continuation prompts inherit the prior turn's route — instant, free.
    last_route = _load_last_route(session_id) if session_id else None
    if last_route and _is_continuation(prompt):
        task_type  = last_route["task_type"]
        complexity = last_route["complexity"]
        tool       = last_route["tool"]
        # Negative continuations (no/stop/skip) → downgrade to cheap query
        if _NEGATIVE_RE.match(prompt.strip()):
            task_type  = "query"
            complexity = "simple"
            tool       = "llm_query"
        method = "context-inherit"
    elif last_route and _is_short_code_followup(prompt, last_route):
        # Short follow-ups after code tasks inherit code classification.
        # Don't save — preserve original code context for subsequent turns.
        task_type  = last_route["task_type"]
        complexity = last_route["complexity"]
        tool       = last_route["tool"]
        method     = "code-context-inherit"
    else:
        result = classify_prompt(prompt)
        if result is None:
            sys.exit(0)
        task_type  = result["task_type"]
        complexity = result["complexity"]
        method     = result["method"]
        tool       = TOOL_MAP.get(task_type, "llm_route")

        # ── v6.1: Check for learned routing overrides ─────────────────────────────
        learned_routes = _load_learned_routes()
        learned_override = _check_learned_override(task_type, learned_routes)
        if learned_override:
            tool, method_suffix = learned_override
            method = f"learned{method_suffix}"
        # ────────────────────────────────────────────────────────────────────────

        # Save classification so the next turn can inherit if it's a continuation
        _save_last_route(session_id, task_type, complexity, tool)

    # ── Claude Code routing: Always use MCP tools (free-first chain) ──────────
    # v6.11.1: Prioritize Ollama → Codex → OpenAI → Gemini over subscription Sonnet
    # This ensures maximum cost savings by routing through available models first.
    #
    # Even in subscription mode, MCP tools handle fallback to Sonnet if needed.
    # Ollama can be used for free; Codex is free tier; APIs as fallbacks.
    
    requested_complexity = None
    _pressure_suffix = ""
    
    if _CC_MODE:
        pressure = _get_pressure()
        requested_complexity = complexity  # Save original before pressure downgrade
        complexity, _pressure_suffix = _apply_pressure_downgrade(complexity, pressure)
        
        # Only override routing to /model if pressure is CRITICAL (>95%)
        # Otherwise always use MCP tools which have better cost optimization
        if pressure.get("session_pct", 0) >= 95 or pressure.get("weekly_pct", 0) >= 95:
            # Critical pressure: use direct subscription fallback
            if complexity == "complex":
                # Complex tasks truly need Opus
                directive = (
                    f"⚡ SUBSCRIPTION OVERRIDE: {task_type}/{complexity} → /model claude-opus-4-6"
                    f" [CRITICAL PRESSURE: session={pressure.get('session_pct', 0):.0%}] "
                    f"| Handle directly (subscription included). Do NOT call llm_* tools."
                )
                _debug_log(f"[INVOCATION {invocation_id:.3f}] CRITICAL PRESSURE: routing to Opus")
                json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                   "contextForAgent": _prior_violation_notice(previous_unrouted) + directive}}, sys.stdout)
                sys.exit(0)

    # ── Activation mode (shadow / suggest / enforce) ──────────────────────────
    # Priority: env var > .llm-router.yml repo config > ~/.llm-router/.env > "hard"
    # shadow  — observe only; emit passive hint, write NO pending state
    # suggest — show soft hint; write pending state (enforce-route treats it as soft/logged-only)
    # enforce / hard (default) — block Claude if routing is violated
    _enforce_mode = os.environ.get("LLM_ROUTER_ENFORCE", "").lower()
    if not _enforce_mode:
        # Try reading from .llm-router.yml in cwd or ancestor
        try:
            import yaml as _yaml
            _repo_yml = Path.cwd() / ".llm-router.yml"
            if not _repo_yml.exists():
                for _p in Path.cwd().parents:
                    _c = _p / ".llm-router.yml"
                    if _c.exists():
                        _repo_yml = _c
                        break
            if _repo_yml.exists():
                _repo_data = _yaml.safe_load(_repo_yml.read_text()) or {}
                _enforce_mode = str(_repo_data.get("enforce", "")).lower()
        except Exception:
            pass
    if not _enforce_mode:
        _enforce_mode = "hard"

    # ── Standard external routing directive ───────────────────────────────────
    stale_suffix = " [⚠️ STALE USAGE DATA >30min — run llm_check_usage]" if _is_pressure_stale() else ""

    # Get selected model for tracking and indicator enhancement
    selected_model, provider = _get_selected_model(task_type, complexity)
    
    # Log routing decision for later evaluation
    try:
        # Suppress all output during tracking (handlers may output to stdout)
        import io
        import logging
        _old_stdout = sys.stdout
        _old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        _old_level = logging.getLogger("llm_router.model_tracking").level
        logging.getLogger("llm_router.model_tracking").setLevel(logging.CRITICAL)
        
        try:
            log_routing_decision(
                task_type=task_type,
                complexity=complexity,
                classification_method=method,
                selected_model=selected_model,
                provider=provider,
                notes=f"routed via {tool}" if tool != TOOL_MAP.get(task_type) else None,
            )
        finally:
            sys.stdout = _old_stdout
            sys.stderr = _old_stderr
            logging.getLogger("llm_router.model_tracking").setLevel(_old_level)
    except Exception:
        pass  # Silently fail if tracking is unavailable

    # ── Log quota snapshot for per-prompt audit trail ──────────────────────────
    # Increment prompt_sequence counter and log quota state at routing time
    if _CC_MODE and session_id:
        try:
            # Increment prompt_sequence in session_spend.json
            session_spend_path = os.path.expanduser("~/.llm-router/session_spend.json")
            prompt_sequence = 0
            if os.path.exists(session_spend_path):
                try:
                    with open(session_spend_path, "r") as f:
                        spend_data = json.load(f)
                        prompt_sequence = spend_data.get("prompt_sequence", 0)
                except Exception:
                    pass
            
            prompt_sequence += 1
            
            # Update session_spend.json with new prompt_sequence
            if os.path.exists(session_spend_path):
                try:
                    with open(session_spend_path, "r") as f:
                        spend_data = json.load(f)
                    spend_data["prompt_sequence"] = prompt_sequence
                    tmp = session_spend_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(spend_data, f)
                    os.replace(tmp, session_spend_path)
                except Exception:
                    pass
            
            # Log quota snapshot (fire-and-forget)
            db_path = os.path.expanduser("~/.llm-router/usage.db")
            pressure = _get_pressure() if _CC_MODE else {"session_pct": 0.0, "weekly_pct": 0.0, "sonnet_pct": 0.0}
            was_downgraded = requested_complexity is not None and requested_complexity != complexity
            _log_quota_snapshot_sync(
                session_id=session_id,
                prompt_sequence=prompt_sequence,
                prompt_hash=None,  # Could add prompt hash here if needed
                pressure=pressure,
                routing_decision_id=None,  # Hook doesn't have access to this
                final_model=selected_model,
                final_provider=provider,
                complexity_requested=requested_complexity,
                complexity_used=complexity,
                was_downgraded=was_downgraded,
                db_path=db_path,
            )
        except Exception:
            pass  # Silent failure — quota snapshot is optional enhancement

    if _enforce_mode == "shadow":
        # Passive observation — no pending state, no blocking
        directive = (
            f"👁 OBSERVATION [{_enforce_mode}]: ✨ {task_type}/{complexity} ✨ "
            f"would route to {tool} → 🧠 {selected_model} [via {method}{stale_suffix}]"
        )
        indicator = f"👁 {task_type}/{complexity} ✨ {tool} → 🧠 {selected_model}"
        write_pending = False
    elif _enforce_mode == "suggest":
        # Soft hint — pending state written but enforce-route only logs, never blocks
        directive = (
            f"💡 SUGGESTED: ✨ {task_type}/{complexity} ✨ {tool} → 🧠 {selected_model} "
            f"[via {method}{stale_suffix}] | You may answer directly if preferred"
        )
        indicator = f"💡 {task_type}/{complexity} ✨ {tool} → 🧠 {selected_model}"
        write_pending = True
    else:
        # enforce / hard (default)
        directive = (
            f"⚡ MANDATORY ROUTE: ✨ {task_type}/{complexity} ✨ {tool} → 🧠 {selected_model} "
            f"[via {method}{stale_suffix}] | "
            f"FORBIDDEN: self-answer · Agent subagents · WebSearch · WebFetch | "
            f"Call the tool NOW as your ONLY action."
        )
        indicator = f"✨ {task_type}/{complexity} ✨ {tool} → 🧠 {selected_model}"
        write_pending = True

    directive = _prior_violation_notice(previous_unrouted) + directive

    # ── Write enforcement state for enforce-route.py (PreToolUse hook) ──────────
    if write_pending and session_id:
        _state_path = _pending_state_path(session_id)
        try:
            _now = time.time()
            _write_json_atomic(
                _state_path,
                {
                    "expected_tool": tool,
                    "expected_server": "",
                    "task_type": task_type,
                    "complexity": complexity,
                    "requested_complexity": requested_complexity,  # Original before pressure downgrade
                    "issued_at": _now,
                    "expires_at": _now + _PENDING_ROUTE_TTL_SEC,
                    "turn_id": int(_now),  # proxy for turn — clears when next prompt arrives
                    "session_id": session_id,
                },
            )
        except OSError:
            pass

    # ── Append mid-session trend indicator for visibility ────────────────────────
    trend_indicator = ""
    try:
        from llm_router.monitoring.live_tracker import get_live_trend_indicator
        trend_indicator = get_live_trend_indicator()
        
        # Attempt to capture hourly snapshot (async, fire-and-forget)
        try:
            from llm_router.monitoring.live_tracker import check_and_capture_hourly_snapshot
            import threading
            def _capture_snapshot():
                try:
                    import asyncio
                    asyncio.run(check_and_capture_hourly_snapshot())
                except Exception:
                    pass
            thread = threading.Thread(target=_capture_snapshot, daemon=True)
            thread.start()
        except Exception:
            pass
    except Exception:
        pass
    
    if trend_indicator:
        indicator = f"{indicator}  {trend_indicator}"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "contextForAgent": directive,
        }
    }
    _debug_log(f"[INVOCATION {invocation_id:.3f}] OUTPUTTING: tool={tool} task={task_type}/{complexity} method={method}")
    json.dump(output, sys.stdout)
    _debug_log(f"[INVOCATION {invocation_id:.3f}] OUTPUT COMPLETE")


if __name__ == "__main__":
    main()
