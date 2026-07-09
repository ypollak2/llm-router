"""Deterministic signal classifier — the 0-cost, 0-latency routing hot path.

Extracted VERBATIM from ``hooks/auto-route.py`` (score_categories /
classify_complexity + their SIGNALS/weight/complexity tables) into an importable
module so the router core, gateway, and MCP tools can classify synchronously
without shelling through the hook. The hook now imports from here, so there is a
single source of truth (no drift between the hook's copy and everything else).

Weighted intent*3 + topic*2 + format*1 scoring; ``classify_signals`` returns a
task_type + complexity + confidence in one call. When ``confident`` is False the
caller MAY escalate to the LLM classifier (classifier.py) — model SELECTION and
LLM escalation are intentionally NOT done here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

CONFIDENCE_THRESHOLD = int(os.environ.get("LLM_ROUTER_CONFIDENCE_THRESHOLD", "2"))

COORDINATION_MAX_LEN = 150

INTENT_WEIGHT = 3

TOPIC_WEIGHT = 2

FORMAT_WEIGHT = 1

LAYER_WEIGHTS = {
    "intent": INTENT_WEIGHT,
    "topic": TOPIC_WEIGHT,
    "format": FORMAT_WEIGHT,
}

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
    "coordination": {
        # Intent: only words that are *strongly* coordination-signal in
        # isolation. Removed `continue`, `proceed`, `verify`, `check`,
        # `test`, `update`, `execute`, `run`, `build`, `compile`, `is`,
        # `are`, `does`, `please`, `thanks` — they fire false positives
        # on substantive prompts. The remaining set is git/deploy verbs
        # plus short single-token acknowledgements.
        "intent": re.compile(
            r"\b(?:push|pull|deploy|release|publish|go ahead|"
            r"yes|ok|y|n|"
            r"commit|merge|sync|fetch|rebase)\b",
            re.IGNORECASE,
        ),
        "topic": re.compile(
            r"\b(?:git|github|push|pull|commit|merge|branch|"
            r"release|deploy|publish|pypi|pipeline|ci|test(?:s)?|"
            r"script|build|version|setup|install|initialize|"
            r"verification|approval|confirmation)\b",
            re.IGNORECASE,
        ),
        "format": re.compile(
            r"\b(?:quick|just|go|proceed|now|asap|ready|done|finished|complete)\b",
            re.IGNORECASE,
        ),
    },
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
    # Length gate: long prompts cannot be coordination, regardless of
    # which short coordination words happen to appear in them.
    if len(text) > COORDINATION_MAX_LEN:
        scores["coordination"] = 0
    return scores

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


@dataclass
class ClassifySignal:
    """A deterministic classification result. ``confident`` is True when the
    winning score cleared ``CONFIDENCE_THRESHOLD`` — the ~80% hot path."""

    task_type: str
    complexity: str
    score: int
    confident: bool
    method: str = "heuristic"


def classify_signals(text: str) -> ClassifySignal:
    """Deterministic (task_type, complexity) in one call. Never raises, never
    blocks — the sync fast path shared by every caller. When ``confident`` is
    False the caller may escalate via the LLM classifier."""
    scores = score_categories(text)
    best = max(scores, key=scores.get) if scores else "query"
    best_score = scores.get(best, 0)
    confident = best_score >= CONFIDENCE_THRESHOLD
    complexity = classify_complexity(text, best)
    return ClassifySignal(best, complexity, best_score, confident)
