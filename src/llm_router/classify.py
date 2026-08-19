# SPDX-License-Identifier: MIT
"""Unified prompt classifier — the single source of truth for LLM Router routing.

Shared classification engine. ``router.py`` and ``gateway.py`` route through this
(each via its own ``ClassifyPolicy``); ``hooks/auto-route.py`` stays the reference
engine and keeps its own copy of these tables — Option B, since the hook routes
live sessions and was left untouched.

Design (see llm_router-audit-roadmap):

    classify_signals(prompt)  ── deterministic, 0-cost, 0-latency ──▶ ClassifySignal
        │  weighted intent×3 + topic×2 + format×1 scoring (arena core)
        │  + keyword/length complexity
        ▼
    confidence gate  (best_score >= _CONFIDENCE_THRESHOLD ?)
        │ confident  → use the signal result as-is (the ~80% hot path)
        │ ambiguous  → escalate:
        ▼
    await classify_complexity(...)   ── real LLM classifier (classifier.py) ──▶
        merge its complexity + inferred_task_type back in

Model SELECTION is intentionally NOT done here. This returns (task_type,
complexity); callers feed that into the existing config-registry machinery
(``COMPLEXITY_TO_PROFILE`` → ``get_model_chain`` sourced from
``policies/standard.yaml``). No hardcoded ``provider/model`` literals live here —
that would violate the project's no-hardcoding rule (see
llm_router-no-hardcoding-opensource). This is the key difference from the RouterArena
submission's ``_tier``: the arena router hardcodes its pool because the arena
fixes the pool; production must resolve through the registry.

The deterministic engine is the weighted intent×3 + topic×2 + format×1 scoring
shared with the hook. The ``_SIGNALS`` / ``_COMPLEXITY_*`` tables below are
backfilled VERBATIM from ``hooks/auto-route.py``'s live production tables
(categories image/query/research/code/analyze/generate — ``coordination`` is
skipped: no matching ``TaskType``). Keep them in sync with the hook until the two
share a module (the full dedup deferred as higher-risk/lower-benefit). Arena-only
benchmark fast-paths (``\\boxed{X}`` MCQ markers, harness-prefix templates) are
deliberately excluded — they never fire on organic prompts and could misfire on
real LaTeX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from llm_router.capabilities import CapabilityRequirement, RelevantContext
from llm_router.types import Complexity, TaskType

# ── Weighted-signal engine (arena core, benchmark fast-paths removed) ─────────

_INTENT_W, _TOPIC_W, _FORMAT_W = 3, 2, 1
_CONFIDENCE_THRESHOLD = 2  # best_score >= this → trust the heuristic; else escalate

# Backfilled verbatim from hooks/auto-route.py (Option B) — the hook's live,
# production-tuned tables. Keep in sync with the hook until they share a module.
_SIGNALS: dict[str, dict[str, re.Pattern]] = {
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
            r"find (?:the |current |recent )?(?:latest|current|recent|newest|"
            r"best practices?|guidance|recommendations?)|"
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
            # "fix the X" / "fix for the X" / "patch the X" — broad enough
            # to catch implementation prompts like "fix the auto-route
            # classifier" or "continue with the fix for the branch"
            # without requiring a trailing bug/error/issue noun. The
            # required determiner (the/this/a/for the/...) filters out
            # bare-noun usage like "the fix was hard" (no determiner
            # follows "fix").
            r"(?:fix|patch|repair|resolve)\s+"
            r"(?:the\s+|this\s+|a\s+|an\s+|for\s+the\s+|for\s+a\s+|for\s+an\s+|"
            r"my\s+|our\s+|these\s+|those\s+)\w+|"
            r"fix (?:the |this |a )?(?:\w+ ){0,4}(?:bug|error|issue|crash|failing test|exception)|"
            r"add (?:a |the )?(?:\w+ ){0,4}(?:feature|method|test|endpoint|route|handler|"
            r"middleware|support|integration|login|validation|form|field|button|column|"
            r"index|migration|config|option|flag|component|hook|logging|logger)|"
            # Deletion/removal of code artefacts — anchored to a code noun so it
            # doesn't catch "remove me from the mailing list".
            r"(?:remove|delete|drop|strip)\s+(?:the |this |these |all |any |unused |"
            r"deprecated |dead )?(?:\w+ ){0,3}(?:import|imports|logging|log|logger|call|"
            r"calls|function|method|dependency|dependencies|code|line|lines|file|test|"
            r"tests|endpoint|route|handler|variable|field|column|comment|comments)|"
            # "wire up the X button/handler/…", "hook up the …"
            r"(?:wire|hook)\s+up\s+(?:the |this |a )?(?:\w+ ){0,3}(?:button|handler|"
            r"endpoint|event|listener|callback|session|form|api|service|component|route)|"
            r"(?:rename|replace|extract|inline|move)\s+(?:the |this |a )?(?:\w+ ){0,3}"
            r"(?:function|method|class|variable|module|file|component|import|endpoint|handler)|"
            r"update (?:the |this )?(?:\w+ ){0,4}(?:code|logic|function|implementation|client|"
            r"api client|service|handler|middleware|endpoint)|"
            r"modify (?:the |this )|extend (?:the |this )|"
            # Relaxed so intervening adjectives are allowed ("optimize the slow
            # database query").
            r"(?:optimize|improve|speed up) (?:the |this )?(?:\w+ ){0,4}(?:code|query|"
            r"performance|function|latency|throughput|speed|render|load time)|"
            r"set up|configure|install|bootstrap|initialize|"
            r"create (?:\w+ ){0,5}(?:function|class|module|component|hook|test|script|program|service|tool))\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:function|class|method|constructor|interface|enum|struct|"
            r"module|package|library|dependency|"
            r"endpoint|route|handler|middleware|controller|resolver|client|api client|"
            r"database|schema|migration|orm|"
            # Testing vocabulary — when a prompt is "build tests for X",
            # this is implementation work, not analysis work. The 5 QA
            # pillars and the bench harness corpus both trigger here.
            r"tests?|spec|coverage|assertion|mock|fixture|"
            r"qa|quality assurance|test suite|regression test|"
            r"unit test|integration test|functional test|e2e test|"
            r"non[- ]functional|integrity|usability|"
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
            r"compare (?:and contrast|\w[^.]{0,80}? (?:to|with|vs|versus)|\w[^.]{0,60}? and [^.]{0,60})|"
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

_COMPLEXITY_DEEP = re.compile(
    # Formal academic / mathematical triggers (original)
    r"\b(?:prove (?:that|mathematically|formally)|"
    r"mathematical(?:ly)? (?:prove|derive|show)|"
    r"formal proof|theorem|lemma|axiom|corollary|"
    r"derive from first principles?|first[- ]principles?\b|"
    r"from (?:the )?fundamentals?|foundational(?:ly)?|"
    r"philosophical(?:ly)? (?:analyze|examine|argue|discuss|analysis)|"
    r"what does it mean (?:fundamentally|philosophically|at its core)|"
    r"synthesize (?:the )?research|comprehensive literature review|"
    r"rigorous(?:ly)? (?:analyze|prove|derive|examine|analysis)|"
    r"formal(?:ly)? (?:specify|verify|prove)|"
    r"mathematical induction|(?:proof |by )(?:induction|deduction|contradiction)|reductio ad absurdum|"
    # Natural-language chain-of-thought triggers (new — catches everyday deep-think requests)
    r"step[- ]by[- ]step|think (?:this )?through|reason (?:through|about|carefully)|"
    r"chain[- ]of[- ]thought|think (?:carefully|deeply|step[- ]by[- ]step)|"
    r"walk me through (?:the )?(?:reasoning|logic|steps|derivation)|"
    r"explain (?:your )?reasoning|show (?:your )?work|"
    r"think (?:out )?loud|reason (?:out )?loud|"
    r"let me (?:reason|think)|think aloud|"
    # Explicit deep-dive triggers
    r"deep[- ]dive|root[- ]cause analysis|"
    r"understand (?:why|how exactly)|exactly (?:why|how)|"
    r"what is (?:the )?(?:root cause|underlying reason)|"
    r"trace (?:through|the (?:logic|reasoning|chain)))\b",
    re.IGNORECASE,
)

_COMPLEXITY_COMPLEX = re.compile(
    r"\b(?:architect|design system|from scratch|end-to-end|comprehensive|"
    r"novel approach|research paper|synthesis|multi-step|workflow|pipeline|"
    r"in-depth|thorough|detailed plan|full implementation|production|"
    r"scalable|distributed|microservice|security audit|"
    r"compare multiple|across all|entire|complete)\b",
    re.IGNORECASE,
)

_COMPLEXITY_SIMPLE = re.compile(
    r"\b(?:quick|simple|short|one-liner|brief|"
    r"summarize|tldr|eli5|just|only|small|tiny|minor)\b",
    re.IGNORECASE,
)

# ── Task-type complexity FLOOR (anti-under-classification policy) ──────────────
# Under-routing is the expensive mistake: a task sent to too-cheap a tier fails
# and bounces back to the subscription agent (double work). Each task type carries
# a minimum routing tier — analysis/research are premium-tier work by nature,
# codegen/writing are at least mid-tier. A classifier may raise complexity above
# the floor, never below it. ``query`` has no floor (lookups stay cheap). Measured
# lift: golden-set exact accuracy 48% → 76% at 0ms/$0 (scripts/eval_router_ensemble.py).
_TASK_COMPLEXITY_FLOOR: dict[str, Complexity] = {
    "generate": Complexity.MODERATE,
    "code": Complexity.MODERATE,
    "analyze": Complexity.COMPLEX,
    "research": Complexity.COMPLEX,
}
_COMPLEXITY_RANK: dict[Complexity, int] = {
    c: i
    for i, c in enumerate(
        [Complexity.SIMPLE, Complexity.MODERATE, Complexity.COMPLEX, Complexity.DEEP_REASONING]
    )
}


def apply_complexity_floor(complexity: Complexity, task_type: str | TaskType) -> Complexity:
    """Clamp ``complexity`` UP to the task-type floor (never down). Single source
    of truth for the floor policy — also imported by llm_router.ensemble."""
    key = task_type.value if isinstance(task_type, TaskType) else str(task_type)
    floor = _TASK_COMPLEXITY_FLOOR.get(key)
    if floor is None:
        return complexity
    return floor if _COMPLEXITY_RANK[floor] > _COMPLEXITY_RANK.get(complexity, 0) else complexity


def _score_categories(text: str) -> dict[str, int]:
    scores: dict[str, int] = {}
    for category, layers in _SIGNALS.items():
        total = 0
        for layer, weight in (("intent", _INTENT_W), ("topic", _TOPIC_W), ("format", _FORMAT_W)):
            pat = layers.get(layer)
            if pat:
                matches = pat.findall(text)
                unique = len({m.lower() if isinstance(m, str) else m[0].lower() for m in matches})
                total += unique * weight
        scores[category] = total
    return scores


# ── Per-path policy ───────────────────────────────────────────────────────────
#
# The three routing paths carry deliberately-tuned, regression-locked complexity
# curves (the router's <600/[600,2000]/>2000 partition is a documented fix for an
# over-escalation-cost incident; the gateway defaults low-signal prompts to
# analyze). Rather than force one policy on all of them (a real cost change), each
# path passes its own ``ClassifyPolicy`` and shares this ONE engine — so a fix to
# the scoring/complexity algorithm now lands in every path at once.


@dataclass(frozen=True)
class ClassifyPolicy:
    keyword_complexity: bool = True  # apply the DEEP/COMPLEX/SIMPLE regexes first
    complex_min: int = 500  # len > complex_min → complex
    simple_max: int = 0  # len <= simple_max → simple (non-query tail)
    task_aware_query: bool = True  # queries get their own length curve
    query_moderate_min: int = 400  # query: len > this → moderate, else simple
    low_signal_default: str = "query"  # task_type when no category scores confidently
    apply_floor: bool = True  # clamp complexity up to the task-type floor (anti-under-routing)


# Reference policy (the UserPromptSubmit hook): keyword-aware, >500 → complex,
# query-aware. This is the module default.
HOOK_POLICY = ClassifyPolicy()

# Router ``_resolve_profile`` no-hint fallback: pure length, <600 simple /
# [600,2000] moderate / >2000 complex. Preserves the documented cost fix exactly.
ROUTER_POLICY = ClassifyPolicy(
    keyword_complexity=False,
    complex_min=2000,
    simple_max=599,
    task_aware_query=False,
)

# Gateway (external side door): pure length, <=400 simple / (400,2000] moderate /
# >2000 complex; low-signal prompts default to analyze (its historical default).
GATEWAY_POLICY = ClassifyPolicy(
    keyword_complexity=False,
    complex_min=2000,
    simple_max=400,
    task_aware_query=False,
    low_signal_default="analyze",
    apply_floor=False,  # external side door keeps its own tuned length curve
)


def _base_complexity(text: str, task_type: str, policy: ClassifyPolicy) -> Complexity:
    if policy.keyword_complexity:
        # Calibrated reason-gate replaces the bare `_COMPLEXITY_DEEP.search`.
        # It uses that same regex as one feature (deep-keyword hit is sufficient
        # by default, so this is a superset of the old behaviour), plus
        # math-density / length / code-fence signals it can be calibrated on.
        # Imported lazily to avoid an import cycle (reason_gate pulls the
        # compiled regexes from this module).
        from llm_router.reason_gate import needs_reasoning

        if needs_reasoning(text):
            return Complexity.DEEP_REASONING
        if _COMPLEXITY_COMPLEX.search(text):
            return Complexity.COMPLEX
        if _COMPLEXITY_SIMPLE.search(text):
            return Complexity.SIMPLE
    n = len(text)
    if n > policy.complex_min:
        return Complexity.COMPLEX
    if policy.task_aware_query and task_type == "query":
        return Complexity.MODERATE if n > policy.query_moderate_min else Complexity.SIMPLE
    if n <= policy.simple_max:
        return Complexity.SIMPLE
    return Complexity.MODERATE


def _complexity(text: str, task_type: str, policy: ClassifyPolicy) -> Complexity:
    base = _base_complexity(text, task_type, policy)
    if policy.apply_floor:
        return apply_complexity_floor(base, task_type)
    return base


# ── Public API ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassifySignal:
    task_type: TaskType
    complexity: Complexity
    score: int  # winning category's weighted score
    confident: bool  # score >= _CONFIDENCE_THRESHOLD
    method: str = "signals"
    # CF-2: capability-aware classification. Defaults keep every existing
    # positional constructor call (task_type, complexity, score, confident) valid.
    capabilities: CapabilityRequirement = field(default_factory=CapabilityRequirement)
    relevant_context: RelevantContext | None = None


def complexity_for(
    prompt: str, *, task_type: str = "query", policy: ClassifyPolicy = HOOK_POLICY
) -> Complexity:
    """Complexity only — skips category scoring. Used by the router path, which
    already receives ``task_type`` from the caller and only needs complexity."""
    return _complexity(prompt, task_type, policy)


def classify_signals(prompt: str, policy: ClassifyPolicy = HOOK_POLICY) -> ClassifySignal:
    """Deterministic task_type + complexity. Never raises, never blocks.

    Sync fast path shared by every caller. When ``confident`` is False the caller
    MAY escalate via :func:`classify` (async LLM); callers that must stay sync
    (the gateway endpoint, the hook pre-flight) use this result directly — it is
    always a valid routing decision.
    """
    scores = _score_categories(prompt)
    best = max(scores, key=lambda k: scores.get(k, 0))
    best_score = scores[best]
    confident = best_score >= _CONFIDENCE_THRESHOLD
    task = best if confident else policy.low_signal_default
    try:
        task_type = TaskType(task)
    except ValueError:
        task_type = TaskType.QUERY
    complexity = _complexity(prompt, task_type.value, policy)
    # CF-2: attach the capability vector (needs-tools? which kind?) from the single
    # shared predicate. Pure/sync/fail-open — never blocks a routing decision.
    try:
        from llm_router.capabilities import detect_capabilities
        capabilities = detect_capabilities(prompt, task_type.value).required
    except Exception:  # noqa: BLE001 — capability detection must never break classify
        capabilities = CapabilityRequirement()
    return ClassifySignal(task_type, complexity, best_score, confident,
                          capabilities=capabilities)


async def classify(
    prompt: str,
    *,
    allow_llm: bool = True,
    policy: ClassifyPolicy = HOOK_POLICY,
    quality_mode: str = "balanced",
) -> ClassifySignal:
    """Signal core + LLM escalation on the ambiguous tail.

    Confident heuristic → returned as-is (no API call). Ambiguous prompt +
    ``allow_llm`` → escalate to the real LLM classifier and merge its
    ``complexity`` / ``inferred_task_type``. Any classifier failure falls back
    to the heuristic, so this never stalls routing.
    """
    sig = classify_signals(prompt, policy)
    if sig.confident or not allow_llm:
        return sig

    # Ambiguous category. Prefer the embedding classifier (calibrated softmax,
    # no LLM call) when a Firewall-v2-clean centroid artifact is present. It
    # abstains (returns None) with no artifact or an unreachable embedding
    # backend, so this whole block is a no-op until such an artifact ships —
    # behaviour is byte-identical to the LLM-only path below until then.
    try:
        from llm_router.semantic_classify import classify_semantic

        sem = await classify_semantic(prompt)
    except Exception:  # noqa: BLE001 — never let it stall routing
        sem = None
    if sem is not None:
        # Sharper task_type without an API call. Recompute complexity for the
        # new task_type so the query-aware length curve stays consistent.
        complexity = complexity_for(prompt, task_type=sem.task_type.value, policy=policy)
        return ClassifySignal(
            sem.task_type, complexity, sig.score, confident=True, method="embedding"
        )

    # Embedding head abstained — spend one cheap classifier call instead.
    try:
        from llm_router.classifier import classify_complexity

        result = await classify_complexity(prompt, quality_mode=quality_mode)
    except Exception:  # noqa: BLE001 — never let classification failure stall routing
        return sig

    try:
        complexity = Complexity(result.complexity)
    except (ValueError, AttributeError):
        complexity = sig.complexity
    inferred = getattr(result, "inferred_task_type", None)
    try:
        task_type = TaskType(inferred) if inferred else sig.task_type
    except ValueError:
        task_type = sig.task_type
    return ClassifySignal(task_type, complexity, sig.score, confident=True, method="llm")
