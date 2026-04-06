#!/usr/bin/env python3
# llm-router-hook-version: 9
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
import time
import urllib.request
from pathlib import Path

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

OLLAMA_MODEL = os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "qwen3.5:latest")
OLLAMA_URL = os.environ.get("LLM_ROUTER_OLLAMA_URL", "http://localhost:11434")
OLLAMA_TIMEOUT = int(os.environ.get("LLM_ROUTER_OLLAMA_TIMEOUT", "5"))
CONFIDENCE_THRESHOLD = int(os.environ.get("LLM_ROUTER_CONFIDENCE_THRESHOLD", "4"))

# API keys for cheap fallback (read from env or .env files)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")

# Claude Code subscription mode — Claude models used via subscription, never API.
# Routing strategy by complexity (no pressure):
#   simple   → /model claude-haiku-4-5-20251001  (fast, free via subscription)
#   moderate → passthrough — Sonnet handles directly, no switch needed
#   complex  → /model claude-opus-4-6            (best quality, free via subscription)
#
# When pressure builds, external models take over tier by tier:
#   session  ≥ 85% → simple   tasks → external (Gemini Flash → Groq → GPT-4o-mini → Ollama)
#   sonnet   ≥ 95% → moderate tasks → external (GPT-4o → Gemini Pro → DeepSeek → Ollama)
#   weekly   ≥ 95% → complex  tasks → external (o3 → Gemini Pro → Ollama)
_CC_MODE = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")


def _get_pressure() -> dict[str, float]:
    """Read per-bucket Claude subscription pressure from usage.json or SQLite.

    Returns keys: session (5h window), sonnet (weekly Sonnet), weekly (all models)
    as fractions 0.0–1.0.

    Staleness handling:
    - If usage.json is fresh (≤30 min): use it directly.
    - If usage.json is stale AND last known session ≥ 70%: attempt an inline OAuth
      refresh before routing. Subscription usage only goes up in a session window,
      so stale data underestimates pressure. At high pressure (≥70%), this
      underestimate can cause catastrophic under-routing and session exhaustion.
    - If usage.json does not exist (new session): fall back to 0.0 — this is safe
      because there genuinely is no pressure to speak of.
    """
    usage_path = Path.home() / ".llm-router" / "usage.json"

    def _frac(d: dict, key: str) -> float:
        v = float(d.get(key, 0.0))
        return v / 100.0 if v > 1.0 else v  # normalise: percent→fraction

    try:
        raw = json.loads(usage_path.read_text())
        age_s = time.time() - float(raw.get("updated_at", 0))
        stale = age_s > 1800  # 30 minutes

        if stale:
            last_session = _frac(raw, "session_pct")
            # Only pay the ~300ms OAuth round-trip when we're actually at risk.
            if last_session >= _INLINE_REFRESH_PRESSURE_FLOOR:
                # Rate-limit the refresh to avoid hammering the API every prompt.
                last_inline_file = str(Path.home() / ".llm-router" / "last_inline_refresh.txt")
                last_inline = 0.0
                try:
                    last_inline = float(open(last_inline_file).read().strip())
                except Exception:
                    pass
                if (time.time() - last_inline) >= _INLINE_REFRESH_MIN_INTERVAL_SEC:
                    fresh = _fetch_usage_inline()
                    if fresh:
                        try:
                            with open(last_inline_file, "w") as _f:
                                _f.write(str(time.time()))
                        except Exception:
                            pass
                        return {
                            "session": _frac(fresh, "session_pct"),
                            "sonnet":  _frac(fresh, "sonnet_pct"),
                            "weekly":  _frac(fresh, "weekly_pct"),
                        }

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
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=4,
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

# ── Signal Patterns ──────────────────────────────────────────────────────────

SIGNALS: dict[str, dict[str, re.Pattern]] = {
    "image": {
        "intent": re.compile(
            r"\b(?:generate (?:an? )?(?:image|picture|photo|illustration|graphic)|"
            r"create (?:an? )?(?:image|picture|illustration|logo|"
            r"icon|graphic|banner|thumbnail|avatar|mockup|diagram)|"
            r"draw (?:a |an |the |me )?|design (?:a |an )?(?:visual|poster|flyer|card|cover)|"
            r"make (?:a |an )?(?:image|picture|photo|illustration)|"
            r"render|visualize|sketch)\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:artwork|portrait|landscape|scenery|sunset|sunrise|mountain|ocean|forest|city|"
            r"pixel art|wallpaper|infographic|"
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
            r"add (?:a |the )?(?:\w+ )*(?:feature|method|test|endpoint|route|handler|middleware)|"
            r"update (?:the |this )?(?:\w+ )*(?:code|logic|function|implementation)|"
            r"modify (?:the |this )|extend (?:the |this )|"
            r"(?:optimize|improve) (?:the |this )?(?:code|query|performance|function)|"
            r"set up|configure|install|bootstrap|initialize|"
            r"create (?:(?:a |the )?\w+ )*(?:function|class|module|component|hook|test|script|program|service|tool))\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:function|class|method|constructor|interface|enum|struct|"
            r"module|package|library|dependency|"
            r"endpoint|route|handler|middleware|controller|resolver|"
            r"database|schema|migration|orm|"
            r"test|spec|coverage|assertion|mock|fixture|"
            r"algorithm|data structure|linked list|hash map|binary tree|"
            r"authentication|authorization|jwt|oauth|"
            r"cache|queue|worker|cron|webhook|"
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
            r"compare (?:and contrast |.+ (?:to|with|vs|versus) )|"
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
            r"metric|kpi|benchmark|baseline|target|"
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
            r"tweet|post|description|pitch|proposal|speech|script|outline|"
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
            r"create (?:a |an )?(?:list|outline|plan|agenda|schedule))\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:blog post|article|essay|email|newsletter|"
            r"marketing copy|ad copy|social media|content strategy|"
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
    OLLAMA_MODEL,       # Primary: qwen3.5 (or env override)
    "qwen2.5:1.5b",    # Fallback: smaller, no thinking mode
]


def classify_with_ollama(text: str) -> str | None:
    """Classify using local Ollama. Tries primary model, falls back to smaller.

    Uses the chat API with think=False to disable thinking mode on reasoning
    models (qwen3.5, etc.) — otherwise they waste the token budget on CoT.
    """
    for model in OLLAMA_MODELS:
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
    if len(stripped) >= 10:
        ollama_result = classify_with_ollama(text)
        if ollama_result:
            return {
                "task_type": ollama_result,
                "complexity": classify_complexity(text, ollama_result),
                "method": "ollama",
            }

    # Layer 3: Cheap API model (Gemini Flash first — free tier, then GPT-4o-mini)
    if len(stripped) >= 10:
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


# ── Entry Point ──────────────────────────────────────────────────────────────


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    prompt = hook_input.get("prompt", "")
    if not prompt:
        sys.exit(0)

    result = classify_prompt(prompt)
    if result is None:
        sys.exit(0)

    task_type = result["task_type"]
    complexity = result["complexity"]
    method = result["method"]
    tool = TOOL_MAP.get(task_type, "llm_route")

    # ── Claude Code subscription mode ─────────────────────────────────────────
    # Never call Anthropic API. Thresholds decide when subscription → external.
    if _CC_MODE:
        pressure = _get_pressure()
        session_pct = pressure["session"]   # 5h rolling window
        sonnet_pct  = pressure["sonnet"]    # weekly Sonnet-specific
        weekly_pct  = pressure["weekly"]    # weekly all-models

        # Pressure tiers cascade — higher pressure forces ALL lower tiers external too.
        # weekly/session ≥ 95%  → everything external (global emergency)
        # sonnet          ≥ 95%  → moderate + simple external (Sonnet pool exhausted)
        #
        # NOTE: the old "session ≥ 85% → simple external" rule was removed.
        # Rationale: at 85% session there is still meaningful Haiku subscription
        # capacity remaining (15%). Routing simple tasks to paid external models
        # (Gemini Flash, Groq, GPT-4o-mini) while Haiku subscription is available
        # violates the "prefer already-paid capacity first" principle. The sonnet
        # tier threshold (≥ 95%) remains as the correct signal that subscription
        # models are genuinely exhausted for a given tier.
        all_external = weekly_pct >= 0.95 or session_pct >= 0.95
        use_external = {
            "simple":   all_external or sonnet_pct >= 0.95,
            "moderate": all_external or sonnet_pct >= 0.95,
            "complex":  all_external,
        }

        if not use_external.get(complexity, False):
            # Stay on subscription — suggest the right Claude tier
            _SUBSCRIPTION_MODELS = {
                "simple":   "claude-haiku-4-5-20251001",
                "moderate": None,                          # passthrough — no switch
                "complex":  "claude-opus-4-6",
            }
            target = _SUBSCRIPTION_MODELS.get(complexity)
            if target is None:
                # moderate + no pressure → still inject routing directive so Claude
                # calls the MCP tool instead of self-answering (100% routing coverage)
                pressure_summary = (
                    f"session={session_pct:.0%} sonnet={sonnet_pct:.0%} weekly={weekly_pct:.0%}"
                )
                directive = (
                    f"⚡ ROUTE→{tool}(complexity=\"moderate\") "
                    f"({task_type}/moderate via {method} | CC-MODE {pressure_summary}) | "
                    f"FORBIDDEN: self-answer · Agent subagents · WebSearch · WebFetch · Bash research | "
                    f"Call the tool NOW as your only action. Cheap model output IS your response."
                )
                json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                   "contextForAgent": directive}}, sys.stdout)
                sys.exit(0)
            pressure_summary = (
                f"session={session_pct:.0%} sonnet={sonnet_pct:.0%} weekly={weekly_pct:.0%}"
            )
            directive = (
                f"⚡ CC-MODE ({task_type}/{complexity} via {method} | {pressure_summary}) | "
                f"Run /model {target} then handle this task directly. "
                f"Subscription = no API cost. Do NOT call external llm_* tools."
            )
            json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                               "contextForAgent": directive}}, sys.stdout)
            sys.exit(0)

        # Pressure threshold exceeded for this tier → fall through to external routing

    # ── Standard external routing directive ───────────────────────────────────
    # complexity= is passed to every tool so route_and_call can derive the right
    # profile (simple→BUDGET/Haiku, moderate→BALANCED/Sonnet, complex→PREMIUM/Opus).
    # complexity_override= is the separate llm_route parameter name.
    if tool == "llm_route":
        tool_args = f'complexity_override="{complexity}"'
    elif tool in ("llm_query", "llm_code", "llm_analyze", "llm_generate"):
        tool_args = f'complexity="{complexity}"'
    else:
        tool_args = ""
    args_str = f"({tool_args})" if tool_args else ""
    stale_suffix = " [⚠️ STALE USAGE DATA >30min — run llm_check_usage for accurate routing]" if _is_pressure_stale() else ""
    directive = (
        f"⚡ ROUTE→{tool}{args_str} ({task_type}/{complexity} via {method}) | "
        f"FORBIDDEN: self-answer · Agent subagents · WebSearch · WebFetch · Bash research | "
        f"Call the tool NOW as your only action. Cheap model output IS your response."
        f"{stale_suffix}"
    )

    indicator = f"⚡ llm-router → {tool}  [{task_type}/{complexity} · {method}]"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "contextForAgent": directive,
        },
        "systemMessage": indicator,
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
