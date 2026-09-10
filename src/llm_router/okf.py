"""Open Knowledge Format (OKF) integration for llm_router.

Reads ~/.llm-router/knowledge/ OKF bundles and injects relevant concept docs
as context before routing tasks to cheap models (#1 — context injection).
Writes ModelCapability docs from a seed catalog (#3 — model catalog).
Writes SourceFile docs as a side-effect of successful routing (#4 — enrichment).

OKF format: markdown + YAML frontmatter. Spec:
  https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

KNOWLEDGE_DIR = Path.home() / ".llm-router" / "knowledge"

# ── Project scoping (CHZ-OKF-01) ─────────────────────────────────────────────
# The store used to be one flat global pile, so a doc extracted while working in
# one repo stayed retrievable — and injectable — while working in an unrelated
# one. Knowledge about `enterprise/rbac.py` has no business being offered as
# context for a prompt about a different project: at best it wastes tokens, at
# worst the cheap model treats it as relevant background and answers around it.
#
# Docs are therefore written under a per-project directory keyed by the project's
# absolute path. Deliberately NOT inside the user's repo: these files are derived
# from model output, and a store living in the working tree gets swept up by
# `git add -A` and committed. Nothing llm_router infers should land in someone's
# history by accident.
PROJECTS_DIR = KNOWLEDGE_DIR / "projects"
MODELS_DIR = KNOWLEDGE_DIR / "models"
QUARANTINE_DIR = KNOWLEDGE_DIR / "quarantine"

# Pre-scoping global docs. Still on disk, no longer auto-injected — that IS the
# cross-contamination fix. `llm_router okf gc` reports and relocates them.
LEGACY_SOURCE_DIR = KNOWLEDGE_DIR / "source"

_SLUG_UNSAFE_RE = re.compile(r"[^\w.-]")


def project_root(start: Path | None = None) -> Path:
    """The repo root for ``start`` (nearest ancestor containing .git), else ``start``.

    Git root rather than raw cwd, so context follows the PROJECT and not whichever
    subdirectory a command ran from — otherwise `src/` and `tests/` would
    accumulate two disjoint stores for the same codebase.

    ``$LLM_ROUTER_PROJECT_ROOT`` overrides the walk (precedence mirrors
    ``session_store._project_id``'s ``$LLM_ROUTER_PROJECT_ID``). The override is
    what makes scoping usable from the MCP server at all: the server is a
    long-lived process whose cwd is wherever the host editor was launched — in the
    field that is ``$HOME``, which has no ``.git``, so every project collapsed into
    one bucket named after the home directory and cross-injected into every other
    (OKF-SCOPE-01: a `capital of Portugal` prompt retrieved another repo's
    `demo/llm/__init__.py`). An explicit root is the only signal that survives a
    process whose cwd is meaningless.
    """
    if start is None:
        override = os.environ.get("LLM_ROUTER_PROJECT_ROOT", "").strip()
        if override:
            try:
                return Path(override).expanduser().resolve()
            except Exception:  # noqa: BLE001 — expanduser raises RuntimeError on ~baduser
                pass  # unusable override → fall through to the cwd walk
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def project_slug(root: Path | None = None) -> str:
    """Stable directory name for a project: ``<basename>-<8 hex of abs path>``.

    The hash disambiguates same-named checkouts (two clones both called `llm_router`)
    while the basename keeps the directory legible to a human browsing the store.
    """
    import hashlib

    resolved = (root or project_root()).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    name = _SLUG_UNSAFE_RE.sub("_", resolved.name) or "root"
    return f"{name}-{digest}"


def project_knowledge_dir(root: Path | None = None, base: Path = KNOWLEDGE_DIR) -> Path:
    """Where THIS project's OKF docs live."""
    return base / "projects" / project_slug(root)


_BUNDLE_CACHE: list[OKFConcept] | None = None
_BUNDLE_LOADED_AT: float = 0.0
_BUNDLE_BASE: tuple[Path, Path] | None = None  # (base, project dir) — see _get_bundle
_BUNDLE_TTL_S: float = 60.0  # reload if knowledge dir changes within this window

# OKF context injection + enrichment are ON by default (verified-only policy).
# The store holds ONLY checkable facts — seeded ModelCapability docs, extracted
# symbol NAMES, real file paths, and the user's own prompts (SessionNote) — and
# NEVER model free-text prose, which was the hallucination amplifier that
# self-poisoned the store in the field (a `setup.py` doc captured a prompt + an
# echoed <knowledge_context> block and re-injected it forever). That loop is
# closed two ways: prose is never stored, and injected <knowledge_context> blocks
# are stripped before any re-capture (see _KNOWLEDGE_CTX_RE). With prose excluded
# there is nothing left to hallucinate, so default-on is safe. Disable with
# LLM_ROUTER_OKF=off.
def _okf_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_OKF", "on").strip().lower() not in ("0", "false", "off", "no")


# Never re-capture an injected knowledge block back into the store (feedback loop).
_KNOWLEDGE_CTX_RE = re.compile(r"<knowledge_context>.*?</knowledge_context>", re.DOTALL | re.IGNORECASE)

# Verified-structure extractors — the ONLY things pulled from text into the store.
# Real file paths (checkable) and defined symbol NAMES (checkable), never prose.
_FILE_PAT = re.compile(r'(?:^|\s)([\w./\-]+\.(?:py|ts|js|go|rs|java|md))\b', re.MULTILINE)
_SYM_PAT = re.compile(
    r'(?:def |class |fn |func |function |async def |async function )(\w+)\s*[({<:]',
    re.MULTILINE,
)


def _extract_files_and_symbols(
    clean_prompt: str,
    clean_response: str,
    max_symbols: int = 10,
) -> tuple[list[str], list[str]]:
    """Pull checkable structure only: real file paths + defined symbol names.
    Shared by enrichment and session capture so both honor the verified-only rule.

    ``max_symbols`` defaults to 10, which is right for enrichment — a model reply
    mentions a handful of symbols and the rest of the cap would be noise. It is
    wrong for indexing a whole file: `okf.py` defines ~40 functions and
    `find_relevant` is not among the first ten, so a prompt naming it retrieved
    unrelated test files while the module that defines it scored zero. Callers
    that read a complete source file raise the cap.
    """
    files = list(dict.fromkeys(
        m.group(1).lstrip("./")
        for m in _FILE_PAT.finditer(clean_prompt + "\n" + clean_response)
        if not m.group(1).startswith(".")
    ))[:5]
    symbols = list(dict.fromkeys(
        m.group(1) for m in _SYM_PAT.finditer(clean_response)
    ))[:max_symbols]
    return files, symbols


# ---------------------------------------------------------------------------
# Core data type
# ---------------------------------------------------------------------------

@dataclass
class OKFConcept:
    path: Path
    type: str
    title: str
    body: str
    description: str = ""
    resource: str = ""
    tags: list[str] = field(default_factory=list)
    timestamp: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_context_block(self) -> str:
        """Render for injection, without saying the same thing twice.

        `_write_source_concept` stores one string in two fields: `description` is
        `summary[:120]` and the body is the full `summary`. Emitting both put every
        SourceFile into the prompt as its symbol list truncated mid-name, followed
        immediately by the same list in full — 120 against 1003 characters for
        `router.py` on the machine this was found on. With three docs injected
        inside a 3000-token budget that is real space spent on a duplicate, and the
        truncated copy is worse than useless: a name cut in half is a name the model
        can complete wrongly.

        Deduplicated at render rather than at write, so documents already on disk
        benefit without a re-index. When one contains the other the body wins,
        because the description is the truncated one.
        """
        parts = [f"## [{self.type}] {self.title}"]
        desc = (self.description or "").strip()
        body = (self.body or "").strip()
        if desc and body:
            # Curated docs carry a description that genuinely says something the
            # body does not; only drop it when it is redundant.
            if body.startswith(desc) or desc.startswith(body):
                desc = ""
        if desc:
            parts.append(desc)
        if body:
            parts.append(body)
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_okf(text: str, path: Path) -> OKFConcept | None:
    """Parse markdown + YAML frontmatter into OKFConcept. Returns None on failure."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1])
        if not isinstance(fm, dict):
            return None
        fm = fm or {}
    except yaml.YAMLError:
        return None
    _standard = {"type", "title", "description", "resource", "tags", "timestamp"}
    return OKFConcept(
        path=path,
        type=str(fm.get("type", "Generic")),
        title=str(fm.get("title", path.stem)),
        body=parts[2].strip(),
        description=str(fm.get("description", "")),
        resource=str(fm.get("resource", "")),
        tags=[str(t) for t in (fm.get("tags") or [])],
        timestamp=str(fm.get("timestamp", "")),
        extra={k: v for k, v in fm.items() if k not in _standard},
    )


# ---------------------------------------------------------------------------
# Bundle loading (cached)
# ---------------------------------------------------------------------------

def _retrieval_roots(base: Path = KNOWLEDGE_DIR) -> list[Path]:
    """Directories eligible for INJECTION, most specific first.

    Exactly two: this project's docs, and the shared model catalog (which is
    project-independent by nature — model strengths don't change per repo).

    Not eligible: `quarantine/` (docs withdrawn from retrieval on purpose) and
    the legacy flat `source/` (the cross-project pile this scoping replaces).
    Both sit outside the two roots below, so neither is reachable.

    **`sessions/` IS eligible, and this docstring used to say it was not**
    (WP-17). Session notes are written under ``project_knowledge_dir()`` — the
    first root here — which ``_load_bundle_sync`` rglobs, so they land in the
    injection bundle. The old wording, "retrieved through its own path with its
    own scoping", described ``find_relevant_sessions()``; that function has no
    production caller, and the router reaches sessions through
    ``find_relevant()`` like everything else.

    The inclusion is DESIRABLE and is why cross-model context works at all: it
    is what hands a cheap local model the file paths and symbols from turns an
    expensive model already answered. The documentation was wrong, not the
    behaviour — but an auditor reading the old text would have concluded that
    session transcripts are never injected into a prompt.

    Consequence worth stating: because the wired path takes no
    ``exclude_session`` argument, a session also retrieves its OWN earlier
    notes. That is what in-session continuity means, so it is intended; the
    guard in ``find_relevant_sessions`` is simply unused. Both behaviours are
    pinned in tests/okf/test_cross_model_context.py.
    """
    return [project_knowledge_dir(base=base)]


def _catalog_root(base: Path = KNOWLEDGE_DIR) -> Path:
    """The shared ModelCapability catalog — read for ROUTING decisions, never
    injected as task context.

    It used to be a second retrieval root, which made it the single most
    frequently injected doc in the store: it is project-independent (so it
    matches every project), and its prose advertises the machinery itself
    ("Best used with: OKF context injection"), so any prompt containing the word
    "context" scored a hit. A `capital of Portugal` prompt came back carrying the
    `gemini-2.5-flash` capability sheet (OKF-SCOPE-01). A catalog of which model
    to pick is input to the router, not background for the task, so it is no
    longer reachable from ``find_relevant``.
    """
    return base / "models"


def _load_dir_sync(root: Path) -> list[OKFConcept]:
    """Parse every OKF doc under ``root``. Shared by the bundle and session lookup
    so both honour the same parse rules and skip list."""
    if not root.exists():
        return []
    out: list[OKFConcept] = []
    for md in root.rglob("*.md"):
        if md.name in ("index.md", "log.md"):
            continue
        try:
            concept = _parse_okf(md.read_text(encoding="utf-8"), md)
        except OSError:
            continue
        if concept:
            out.append(concept)
    return out


def _load_bundle_sync(base: Path = KNOWLEDGE_DIR) -> list[OKFConcept]:
    """Scan and parse the OKF concept docs eligible for injection."""
    roots = [r for r in _retrieval_roots(base) if r.exists()]
    if not roots:
        return []
    concepts: list[OKFConcept] = []
    seen: set[Path] = set()
    for md in (m for root in roots for m in root.rglob("*.md")):
        if md.name in ("index.md", "log.md") or md in seen:
            continue
        seen.add(md)
        try:
            concept = _parse_okf(md.read_text(encoding="utf-8"), md)
            if concept:
                concepts.append(concept)
        except OSError:
            pass
    return concepts


def _get_bundle(base: Path = KNOWLEDGE_DIR) -> list[OKFConcept]:
    """Return cached bundle, reloading if TTL expired or the SCOPE changed.

    CHZ-OKF-01: the cache key is (base, project dir), not base alone. Since
    scoping, the bundle depends on which project we are in — and the MCP server is
    a long-running process that can serve requests for several. Keying on `base`
    only would hand one project's docs to another for up to the TTL, which is the
    exact cross-contamination the scoping exists to prevent.
    """
    global _BUNDLE_CACHE, _BUNDLE_LOADED_AT, _BUNDLE_BASE
    now = time.monotonic()
    scope = (base, project_knowledge_dir(base=base))
    if (
        _BUNDLE_CACHE is not None
        and _BUNDLE_BASE == scope
        and (now - _BUNDLE_LOADED_AT) < _BUNDLE_TTL_S
    ):
        return _BUNDLE_CACHE
    _BUNDLE_CACHE = _load_bundle_sync(base)
    _BUNDLE_LOADED_AT = now
    _BUNDLE_BASE = scope
    return _BUNDLE_CACHE


def invalidate_cache() -> None:
    """Force bundle reload on next access (call after writing new concepts)."""
    global _BUNDLE_LOADED_AT, _BUNDLE_BASE
    _BUNDLE_LOADED_AT = 0.0
    _BUNDLE_BASE = None


# ---------------------------------------------------------------------------
# Relevance scoring and context injection (#1)
# ---------------------------------------------------------------------------

# Words that pass the \w{5,} filter but carry no domain signal — they describe the
# ACT of asking or the machinery being asked, so they match docs about anything.
# "context", "knowledge" and "injection" are the sharpest offenders: the store's
# own docs talk about OKF context injection, so any prompt that mentions context
# scored a hit on the machinery describing itself (OKF-SCOPE-01).
_SCORE_STOPWORDS = frozenset({
    "about", "above", "after", "again", "against", "already", "although", "always",
    "another", "answer", "anything", "because", "before", "being", "below", "between",
    "block", "blocks", "cannot", "could", "context", "could", "current", "described",
    "detail", "details", "differ", "different", "document", "documents", "during",
    "email", "every", "example", "examples", "exact", "exactly", "explain", "first",
    "following", "further", "given", "header", "hello", "helps", "information",
    "injection", "inside", "instead", "instruction", "instructions", "knowledge",
    "later", "least", "level", "might", "never", "nothing", "other", "others",
    "output", "please", "point", "possible", "prompt", "provide", "provided",
    "question", "really", "reply", "respond", "response", "result", "return",
    "right", "same", "section", "sections", "shall", "short", "should", "simply",
    "since", "something", "specific", "still", "story", "suppose", "supplied",
    "table", "their", "there", "these", "thing", "things", "think", "those",
    "three", "title", "titles", "today", "under", "until", "using", "value",
    "where", "whether", "which", "while", "whole", "would", "write", "wrote",
})

# A doc must reach this weighted score to be injected. Weights are assigned by
# WHERE the keyword lands (see _score), so the floor is not a keyword count:
# one hit on a doc's identity (title/tag) clears it, one hit in its prose does not.
# A flat count of 2 was tried first and was wrong — a file path is a single
# highly distinctive token, so `alpha_only.py` retrieved nothing.
_MIN_SCORE_DEFAULT = 2

# What a keyword match is worth. Title and tags are the doc's IDENTITY: matching
# them means the prompt is about this thing. Body prose is weak evidence — it is
# where incidental vocabulary lives, and it is how unrelated docs used to score.
_W_SYMBOL = 3
_W_TITLE = 2
_W_TAG = 2
_W_PATH_PART = 1
_W_BODY = 1

# A title that is a PATH is not the same kind of identity as a title that is a
# name. `tests/test_agent_loop.py` contains the word "agent", but the doc is not
# about agents — it is about that file. Scoring a path component at full title
# weight is what let "draft a blog post about agent evaluation" retrieve three
# test files once the store held 1063 docs instead of 2. Path components score
# like prose; the symbols the file actually defines score highest, because those
# are the verified structure the store exists to hold.
_PATHY_TITLE_RE = re.compile(r"[/\\]|\.\w{1,5}$")

# Split an identifier or path into matchable tokens: reconcile_invoice ->
# {reconcile, invoice, reconcile_invoice}. Substring matching was the earlier
# behaviour and it made "agent" match "agentic", "agents" and "test_agent_loop".
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# A prompt "names" something in the store only if it WRITES it as code: an
# identifier with an underscore, a dotted or slashed path, or camelCase. A word
# length threshold was tried first (>=6 chars) and was far too weak — "commit",
# "changes", "private", "process" and "working" all cleared it, so "yes, commit
# it" retrieved a budget-backend test and would have been routed as though the
# model had been handed relevant code. Measured on 376 real prompts that rule
# "rescued" 22.9% of gated prompts, nearly all of them false. English prose does
# not contain underscores or file extensions; source identifiers do.
_IDENTIFIER_SHAPED_RE = re.compile(
    r"_"                        # snake_case
    r"|\.(?:py|ts|tsx|js|jsx|go|rs|java|md|json|toml|ya?ml|sh)$"   # a real file
    r"|/"                       # a path
)

# Identifiers and paths must be pulled out BEFORE lowercasing and BEFORE the
# \b\w+\b pass, which splits on "." and "/" — that pass turns `okf.py` into
# {"okf", "py"} and `src/llm_router/okf.py` into four unremarkable words, so a
# prompt that quoted a real path could never be recognised as naming one.
_CODEISH_RE = re.compile(r"[A-Za-z_][\w./-]*(?:_[\w./-]*|\.[a-z]{1,4}|/[\w./-]+)")


def _tokens(text: str) -> set[str]:
    """Lowercased whole tokens, plus the undivided form of each identifier."""
    low = (text or "").lower()
    out = {t for t in _TOKEN_SPLIT_RE.split(low) if t}
    out.update(m for m in re.findall(r"[a-z_][a-z0-9_]{2,}", low))
    # Dotted/slashed forms too, so a doc mentioning `alpha_only.py` is matchable by
    # a prompt that writes `alpha_only.py`. Without this the split above turns the
    # filename into {"alpha", "only", "py"} on the doc side while the prompt side
    # keeps it whole (_CODEISH_RE), and the two can never meet.
    out.update(m for m in re.findall(r"[\w./-]*\w\.[a-z]{1,4}\b", low))
    return out


def _min_score() -> int:
    try:
        return max(1, int(os.environ.get("LLM_ROUTER_OKF_MIN_SCORE", _MIN_SCORE_DEFAULT)))
    except ValueError:
        return _MIN_SCORE_DEFAULT


def _is_indexed_source(concept: OKFConcept) -> bool:
    """A bulk-indexed source file: type SourceFile AND a path-shaped title.

    Both signals, because either alone misfires. Type alone catches hand-written
    SourceFile notes whose title is a name ("Router Module"), which are curated and
    few. Title shape alone catches a SessionNote whose title is the user's sentence
    — "fix the webhook backoff in retry.py" ends in ".py" without being a file.
    Only the conjunction identifies the docs `okf index` writes by the thousand,
    which are the ones that need the stricter matching rules.
    """
    return concept.type == "SourceFile" and bool(_PATHY_TITLE_RE.search(concept.title))


def _kw_in(kw: str, tokens: set[str]) -> bool:
    """Whole-token match, tolerant of a trailing plural.

    Exact tokens are what stopped "agent" matching "agentic" and every
    `test_agent_*.py`. But they also stopped "webhook" matching a note about
    `webhooks.py`, which is a real question about a real file. English plurals are
    the one variation worth keeping; broader stemming would reopen the substring
    problem it just closed.
    """
    if kw in tokens:
        return True
    if kw.endswith("s") and kw[:-1] in tokens:
        return True
    return kw + "s" in tokens


def _score(concept: OKFConcept, keywords: list[str]) -> int:
    """Weighted relevance: identity matches count double, prose matches count once.

    The old scorer flattened title, tags, description and body into one string and
    counted bare hits, so a doc whose *prose* happened to share one word with the
    prompt scored exactly as high as a doc the prompt actually named. Combined with
    a `> 0` floor that let every unrelated doc in (OKF-SCOPE-01).

    Matching is on whole tokens, not substrings. Substring matching made "agent"
    hit "agentic", "agents" and every `test_agent_*.py` in the repo — invisible
    while the store held 2 docs, decisive once `okf index` put 1063 in it.
    """
    title_raw = concept.title
    # Keyed on TYPE, not on whether the title happens to look like a path. A
    # SessionNote's title is the user's own sentence, and "fix the webhook backoff
    # in retry.py" contains ".py" — under a shape test it was treated as a source
    # file, scored at path weight and then required an identifier anchor it could
    # never have. SourceFile is the type `okf index` and enrichment write in bulk,
    # and bulk is the whole reason the stricter rules exist.
    pathy = _is_indexed_source(concept)
    symbols = {
        str(s).lower() for s in (concept.extra.get("key_symbols") or [])
    }
    sym_tokens: set[str] = set()
    for s in symbols:
        sym_tokens |= _tokens(s)
    # Naming the file itself — its stem or its whole path — identifies the doc as
    # surely as naming a symbol in it. Only an EXACT match counts: `agent` must not
    # earn this from `tests/test_agent_loop.py`, whose stem is `test_agent_loop`.
    low_title = title_raw.lower()
    exact_names = {low_title, low_title.rsplit("/", 1)[-1]}
    exact_names.add(exact_names.copy().pop().rsplit(".", 1)[0])
    exact_names.add(low_title.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    title_tokens = _tokens(title_raw)
    tag_tokens = _tokens(" ".join(concept.tags))
    body_tokens = _tokens(f"{concept.description} {concept.body}")

    total = 0
    for kw in keywords:
        if kw in symbols or kw in exact_names:   # names the symbol, or the file
            total += _W_SYMBOL
        elif _kw_in(kw, sym_tokens):  # part of a symbol (reconcile in reconcile_invoice)
            total += _W_TITLE
        elif _kw_in(kw, title_tokens):
            total += _W_PATH_PART if pathy else _W_TITLE
        elif _kw_in(kw, tag_tokens):
            total += _W_TAG
        elif _kw_in(kw, body_tokens):
            total += _W_BODY
    return total


def _keywords_for_retrieval(prompt: str) -> list[str]:
    """Prompt tokens usable for scoring: code-shaped tokens first, then prose.

    Code-shaped tokens (`find_relevant`, `src/llm_router/okf.py`) are extracted
    before the prose pass so that "." and "/" survive; they are what
    ``_IDENTIFIER_SHAPED_RE`` later accepts as evidence the prompt NAMES something.
    """
    codeish = [m.group(0).lower() for m in _CODEISH_RE.finditer(prompt or "")]
    prose = [
        w for w in re.findall(r"\b\w{5,}\b", (prompt or "").lower())
        if not w.isdigit() and w not in _SCORE_STOPWORDS
    ]
    return list(dict.fromkeys(codeish + prose))[:40]


def find_relevant(
    prompt: str,
    limit: int = 3,
    base: Path = KNOWLEDGE_DIR,
) -> list[OKFConcept]:
    """Find OKF concepts most relevant to prompt via keyword overlap."""
    if not _okf_enabled():
        return []  # opt-in; see _okf_enabled() — off by default to avoid contamination
    concepts = _get_bundle(base)
    if not concepts:
        return []
    keywords = _keywords_for_retrieval(prompt)
    if not keywords:
        return []
    floor = _min_score()
    # Precision gate (OKF-INDEX-01). Weighted keyword overlap ranks well among a
    # handful of docs and collapses at scale: with 1063 indexed files, "draft a
    # blog post about agent evaluation" and "write a python function that reverses
    # a linked list" both cleared the floor on incidental vocabulary, and prompts
    # that SHOULD have matched came back with unrelated test files ranked above the
    # module that actually defines the symbol.
    #
    # So a doc is injectable only when the prompt NAMES something in it — an exact
    # symbol or path token, and a distinctive one (>=6 chars, or containing an
    # underscore, which is what identifiers look like and what English words in a
    # question do not). `find_relevant` and `build_session_context` qualify; "list",
    # "agent" and "python" do not.
    #
    # This trades recall for precision deliberately. Injecting nothing costs a
    # routing opportunity; injecting the wrong file is what produces a confident
    # answer about code the model never saw.
    # The anchor requirement applies ONLY to indexed source files (a path-shaped
    # title). Those arrive in bulk — `okf index` writes one per tracked file, 1063
    # on this repo — and at that volume prose overlap is meaningless: every common
    # English word appears in some filename or symbol somewhere, so "yes, commit
    # it" retrieved a budget-backend test. Requiring the prompt to actually NAME
    # such a file, in identifier or path form, is what makes a bulk index safe.
    #
    # Curated concept docs (Table, Metric, SessionNote — things a human or the
    # session recorder wrote deliberately) are few and topical, so they keep
    # matching on topic. Demanding an identifier from them would make them
    # unreachable: nobody writes "caching_strategy" when they mean caching.
    anchors = {k for k in keywords if _IDENTIFIER_SHAPED_RE.search(k)}
    scored = []
    for c in concepts:
        s = _score(c, keywords)
        if s < floor:
            continue
        if _is_indexed_source(c) and not (anchors & _anchor_tokens(c)):
            continue
        scored.append((c, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _s in scored[:limit]]


def _anchor_tokens(concept: OKFConcept) -> set[str]:
    """The names a prompt can NAME this doc by: its symbols and its path tokens."""
    out = {str(s).lower() for s in (concept.extra.get("key_symbols") or [])}
    out |= _tokens(concept.title)
    out |= _tokens(" ".join(concept.tags))
    return out


def inject_context(prompt: str, concepts: list[OKFConcept]) -> str:
    """Prepend OKF concept docs to prompt inside a <knowledge_context> block.

    The block is explicitly labelled as retrieved-and-possibly-irrelevant. It used
    to be an unlabelled wall of markdown sitting in the most salient position in
    the request, above both the caller's own ``context=`` payload and the question,
    and models answered *from it* — a review asked about a supplied diff described
    the retrieved doc instead. Retrieval is a guess; the caller's material is not.
    Saying so in the prompt is what makes a wrong guess recoverable.
    """
    if not concepts:
        return prompt
    blocks = "\n\n".join(c.as_context_block() for c in concepts)
    return (
        "<knowledge_context>\n"
        "Background retrieved by keyword match from this project's notes. It may be\n"
        "irrelevant to the question. Anything the user supplied directly, and the\n"
        "question itself, take precedence — if this block does not bear on the\n"
        "question, ignore it entirely and never describe it back as the answer.\n\n"
        f"{blocks}\n"
        "</knowledge_context>\n\n"
        f"{prompt}"
    )


# ---------------------------------------------------------------------------
# Model Capability Catalog (#3)
# ---------------------------------------------------------------------------

_MODEL_CATALOG: dict[str, str] = {
    "gemini-2.5-flash": """\
---
type: ModelCapability
title: gemini-2.5-flash
description: Fast, cheap Gemini model. Best for code gen, refactoring, summarization.
resource: https://ai.google.dev/gemini-api/docs/models
tags: [cheap, fast, code, gemini, cli]
---

**Strengths**: code generation, refactoring, summarization, classification.
**Weaknesses**: multi-file architecture reasoning, novel algorithm design.
**Cost**: ~$0 (CLI quota). **p50 latency**: ~7s.
**Best used with**: OKF context injection for domain-specific tasks.
**Fallback to**: gemini-2.5-pro on quality failures.
""",
    "gemini-2.5-pro": """\
---
type: ModelCapability
title: gemini-2.5-pro
description: Higher-quality Gemini model. Use for architecture and complex analysis.
resource: https://ai.google.dev/gemini-api/docs/models
tags: [moderate-cost, quality, code, gemini, cli]
---

**Strengths**: complex reasoning, architecture design, multi-file refactors.
**Weaknesses**: slower than Flash; avoid for quick lookups.
**Cost**: ~$0 (CLI quota). **p50 latency**: ~28s.
**Best used with**: complex code tasks, deep analysis.
""",
    "gpt-5.5": """\
---
type: ModelCapability
title: gpt-5.5
description: GPT-5.5 via Codex CLI. Strong at complex reasoning and code.
resource: https://platform.openai.com/docs/models
tags: [codex, openai, complex, reasoning]
---

**Strengths**: complex reasoning, multi-step planning, novel algorithm design.
**Weaknesses**: slower; use only when Flash/Pro fail.
**Cost**: subscription. **p50 latency**: ~38s.
**Best used with**: complex architectural tasks where cheaper models fail.
""",
    "gpt-5.4": """\
---
type: ModelCapability
title: gpt-5.4
description: GPT-5.4 via Codex CLI. Premium reasoning for hardest tasks.
resource: https://platform.openai.com/docs/models
tags: [codex, openai, premium, reasoning]
---

**Strengths**: deepest reasoning, research tasks, architecture proposals.
**Weaknesses**: expensive; high latency (~67s p50).
**Cost**: subscription. **p50 latency**: ~67s.
**Best used with**: research, architecture decisions, tasks that need maximum quality.
""",
}


def seed_model_catalog(base: Path = KNOWLEDGE_DIR) -> int:
    """Write default ModelCapability docs if they don't already exist. Returns count written."""
    models_dir = base / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for model_name, content in _MODEL_CATALOG.items():
        safe = re.sub(r'[/:]', '-', model_name)
        path = models_dir / f"{safe}.md"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            written += 1
    if written:
        invalidate_cache()
    return written


def load_model_capability(
    model_name: str,
    base: Path = KNOWLEDGE_DIR,
) -> OKFConcept | None:
    """Load the ModelCapability OKF doc for a model. Returns None if not found."""
    safe = re.sub(r'[/:]', '-', model_name)
    short = model_name.split("/")[-1]
    for name in (safe, short):
        path = base / "models" / f"{name}.md"
        if path.exists():
            try:
                concept = _parse_okf(path.read_text(encoding="utf-8"), path)
                if concept and concept.type == "ModelCapability":
                    return concept
            except OSError:
                pass
    return None


# ---------------------------------------------------------------------------
# Side-effect enrichment — SourceFile concepts (#4)
# ---------------------------------------------------------------------------

def _write_source_concept(
    file_path: str,
    summary: str,
    key_symbols: list[str],
    last_model: str,
    base: Path,
    authoritative: bool = False,
) -> None:
    """Synchronous write; called in executor thread.

    MERGES by default: the new symbols are added to whatever the document already
    holds, never substituted for them.

    The overwrite this replaces was fine while only routed answers enriched, which
    was rare. OKF-INDEX-01 then put enrichment on `context-capture.py`, which fires
    on EVERY tool call with the default cap of 10 and sees only what the tool
    printed — so one tool result mentioning one function replaced a file's entire
    indexed document with that single symbol. Measured after a few hours of ordinary
    work against 1069 indexed documents:

        src/llm_router/hooks/auto-route.py    stored   1 / real  91
        src/llm_router/cost.py                stored   1 / real  63
        src/llm_router/okf.py                 stored   1 / real  36

    Silent, and pointed the wrong way: the eroded files are the large central ones,
    because those are what tool calls keep touching, so the documents most likely to
    be asked about were hollowed out first.

    ``authoritative=True`` is for `index_project`, which read the whole file and is
    therefore entitled to say a symbol is gone. A writer that saw a fragment must
    never be able to assert that the file contains less than it does.
    """
    rel = Path(file_path)
    # CHZ-OKF-01: under this PROJECT's directory, not the flat global `source/`.
    # A doc about `middleware.py` is only meaningful next to the repo it came
    # from; filed globally it becomes a retrieval hazard for every other project.
    concept_path = project_knowledge_dir(base=base) / "source" / rel.with_suffix(".md")
    concept_path.parent.mkdir(parents=True, exist_ok=True)

    tags: list[str] = ["source-file"]
    if rel.suffix in (".py", ".ts", ".js", ".go", ".rs", ".java"):
        tags.append(rel.suffix.lstrip("."))

    merged = list(key_symbols)
    if not authoritative and concept_path.exists():
        try:
            prior = _parse_okf(concept_path.read_text(encoding="utf-8"), concept_path)
        except OSError:
            prior = None
        if prior is not None:
            # Order: what this writer saw first, then what was already known. The
            # cap then trims the least recently observed rather than the newest.
            known = [str(x) for x in (prior.extra.get("key_symbols") or [])]
            merged = list(dict.fromkeys(merged + known))
    if merged != list(key_symbols):
        # The description is body text to `_score`; a stale one leaves the document
        # unfindable by symbols it still claims to hold.
        summary = "Defines: " + ", ".join(merged)

    fm: dict[str, Any] = {
        "type": "SourceFile",
        "title": str(rel),
        "description": summary[:120],
        "resource": str(file_path),
        "tags": tags,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if last_model:
        fm["last_model"] = last_model
    if merged:
        fm["key_symbols"] = merged[:200]

    body = summary or f"Source file: {file_path}"
    text = f"---\n{yaml.dump(fm, default_flow_style=False).strip()}\n---\n\n{body}\n"
    concept_path.write_text(text, encoding="utf-8")
    invalidate_cache()


async def enrich_from_response(
    prompt: str,
    response_text: str,
    model: str,
    base: Path = KNOWLEDGE_DIR,
) -> None:
    """Extract file references from prompt+response and write OKF SourceFile concepts.

    Designed as a fire-and-forget asyncio.create_task so it never blocks the
    response path. Failures are silently swallowed — enrichment is best-effort.
    """
    if not _okf_enabled():
        return  # opt-in; see _okf_enabled()
    try:
        # Strip any injected knowledge block FIRST — never re-capture it into the
        # store. Re-capturing it was the self-poisoning feedback loop.
        clean_prompt = _KNOWLEDGE_CTX_RE.sub("", prompt)
        clean_response = _KNOWLEDGE_CTX_RE.sub("", response_text)

        # Record ONLY checkable structure (real files + extracted symbol names),
        # never the model's free-text prose — that prose is unverified output and
        # was the hallucination vector (e.g. a fabricated plugin API stored as
        # "fact"). See _extract_files_and_symbols.
        files, symbols = _extract_files_and_symbols(clean_prompt, clean_response)
        if not files:
            return
        if not symbols:
            return  # nothing verifiable to record — don't invent a summary

        summary = "Defines: " + ", ".join(symbols)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _write_source_concept, files[0], summary, symbols, model, base
        )
    except Exception:  # noqa: BLE001 — enrichment must never crash the caller
        pass


# ---------------------------------------------------------------------------
# Session context (#2) — per-session, verified-only, cross-session retrievable
# ---------------------------------------------------------------------------

SESSIONS_DIR = KNOWLEDGE_DIR / "sessions"

_SID_SAFE_RE = re.compile(r"[^\w.-]")


def _safe_session_id(session_id: str) -> str:
    return _SID_SAFE_RE.sub("_", str(session_id))[:64]


def record_session_turn(
    session_id: str,
    prompt: str,
    response_text: str,
    model: str,
    base: Path = KNOWLEDGE_DIR,
) -> Path | None:
    """Capture VERIFIED-ONLY context for a turn → ``sessions/<id>/turn-NNNN.md``.

    Stores the user's real prompt (a checkable fact — it is their literal input)
    plus extracted file paths and symbol names. NEVER stores model prose. Because
    ``find_relevant`` rglobs the whole knowledge dir, these notes automatically
    become retrievable from ANY later session — the cross-session memory the user
    asked for. A turn with no verifiable structure (no file, no symbol) is skipped
    as chatter. Returns the written path, or None when disabled/skipped/failed.
    """
    if not _okf_enabled() or not session_id:
        return None
    try:
        clean_prompt = _KNOWLEDGE_CTX_RE.sub("", prompt or "").strip()
        clean_response = _KNOWLEDGE_CTX_RE.sub("", response_text or "")
        files, symbols = _extract_files_and_symbols(clean_prompt, clean_response)
        if not files and not symbols:
            return None  # nothing verifiable → don't store chatter

        safe_sid = _safe_session_id(session_id)
        # CHZ-OKF-01: under the project, like every other written doc. A session
        # transcript is the most project-specific material in the store; sharing
        # it across repos was the worst case of the cross-contamination.
        sess_dir = project_knowledge_dir(base=base) / "sessions" / safe_sid
        sess_dir.mkdir(parents=True, exist_ok=True)
        turn_n = len(list(sess_dir.glob("turn-*.md"))) + 1

        title = (clean_prompt.splitlines() or ["(empty prompt)"])[0][:100]
        body_parts = []
        if files:
            body_parts.append("Files: " + ", ".join(files))
        if symbols:
            body_parts.append("Symbols: " + ", ".join(symbols))
        body_parts.append(f"User request: {title}")

        fm: dict[str, Any] = {
            "type": "SessionNote",
            "title": title,
            "description": f"session {safe_sid} · turn {turn_n}",
            "tags": ["session", safe_sid, *files],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": safe_sid,
        }
        if model:
            fm["last_model"] = model
        text = f"---\n{yaml.dump(fm, default_flow_style=False).strip()}\n---\n\n" + "\n".join(body_parts) + "\n"

        path = sess_dir / f"turn-{turn_n:04d}.md"
        path.write_text(text, encoding="utf-8")
        invalidate_cache()
        return path
    except Exception:  # noqa: BLE001 — session capture must never crash the caller
        return None


def find_relevant_sessions(
    prompt: str,
    exclude_session: str | None = None,
    limit: int = 3,
    base: Path = KNOWLEDGE_DIR,
) -> list[OKFConcept]:
    """Retrieve SessionNote concepts from PRIOR sessions most relevant to ``prompt``.

    Same keyword-overlap scoring as ``find_relevant``, restricted to SessionNotes
    and excluding the caller's own session so a session never just echoes itself.
    """
    if not _okf_enabled():
        return []
    # Loaded directly, NOT via _get_bundle: sessions are excluded from the
    # injection bundle on purpose (they have their own exclude-own-session rule),
    # so going through the bundle would always find nothing.
    sessions_root = project_knowledge_dir(base=base) / "sessions"
    concepts = [
        c for c in _load_dir_sync(sessions_root) if c.type == "SessionNote"
    ]
    if exclude_session:
        safe = _safe_session_id(exclude_session)
        concepts = [c for c in concepts if c.extra.get("session_id") != safe]
    if not concepts:
        return []
    # Same stopword filter and weighted floor as find_relevant. This function has
    # no production caller today, but leaving the old `s > 0` scoring here would
    # quietly reintroduce OKF-SCOPE-01 the moment anything wired it up.
    keywords = list(dict.fromkeys(
        w for w in re.findall(r"\b\w{5,}\b", prompt.lower())
        if not w.isdigit() and w not in _SCORE_STOPWORDS
    ))[:25]
    if not keywords:
        return []
    floor = _min_score()
    scored = [(c, _score(c, keywords)) for c in concepts]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, s in scored[:limit] if s >= floor]


# ---------------------------------------------------------------------------
# Indexing a repository into the store (OKF-INDEX-01)
# ---------------------------------------------------------------------------
# Before this existed the store could ONLY be populated as a side effect of a
# successful routed call (`enrich_from_response`). That is a deadlock: routing is
# skipped because the model has no context, the model has no context because the
# store is empty, and the store is empty because nothing routed. On the machine
# this was found on the entire store held 2 project docs after weeks of work.
#
# Indexing writes the same verified-only material the enrichment path writes —
# real file paths and symbol NAMES pulled by the shared extractors, never prose —
# so nothing enters the store here that could not have entered it before. The only
# change is that it no longer requires a routed answer to get there.

def index_project(
    root: Path | None = None,
    base: Path = KNOWLEDGE_DIR,
    limit: int = 2000,
) -> dict[str, Any]:
    """Walk a repo's tracked source files and write a SourceFile doc for each.

    Uses ``git ls-files`` rather than a filesystem walk so the index inherits the
    repo's own .gitignore — a node_modules or .venv sweep would bury real code
    under vendored files and blow the store up.

    Only files that yield at least one extractable symbol are written. A file with
    no parseable definitions has nothing checkable to say, and a doc whose body is
    just its own filename adds retrieval noise without adding information.

    Returns a summary dict: ``{indexed, skipped, scanned, store}``.
    """
    root = (root or project_root()).resolve()
    store = project_knowledge_dir(root=root, base=base)
    result: dict[str, Any] = {"indexed": 0, "skipped": 0, "scanned": 0, "store": store}
    if not _okf_enabled():
        result["error"] = "OKF disabled (LLM_ROUTER_OKF=off)"
        return result

    import subprocess

    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"git ls-files failed: {exc}"
        return result
    if listing.returncode != 0:
        result["error"] = f"not a git repository: {root}"
        return result

    exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java"}
    for rel in listing.stdout.splitlines():
        if result["indexed"] >= limit:
            break
        rel = rel.strip()
        if not rel or Path(rel).suffix not in exts:
            continue
        result["scanned"] += 1
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            result["skipped"] += 1
            continue
        # Same extractors as the enrichment path — symbols only, never prose. The
        # file's own path is passed as the "prompt" side so _FILE_PAT has it.
        # 200, not the enrichment default of 10 — indexing reads a whole file and
        # a module's later functions are exactly the ones a prompt tends to name.
        _files, symbols = _extract_files_and_symbols(rel, text, max_symbols=200)
        if not symbols:
            result["skipped"] += 1
            continue
        # authoritative: the whole file was just read, so this IS the symbol set.
        # Only the indexer gets to shrink a document; enrichment, which sees a
        # fragment, merges.
        _write_source_concept(
            rel, "Defines: " + ", ".join(symbols), symbols, "", base,
            authoritative=True,
        )
        result["indexed"] += 1

    invalidate_cache()
    return result


# ---------------------------------------------------------------------------
# Quarantine + garbage collection (CHZ-OKF-02)
# ---------------------------------------------------------------------------
# The verified-only policy stopped NEW prose from entering the store, but it
# never removed the docs written before it. Those docs are still scored, still
# retrieved, and still injected — and prose is exactly the material that can be
# wrong. On the machine this was found on, a doc titled `README.md` carried a
# hallucinated filename (`lint_capability_clims.sh`) and was the top hit for any
# README-ish prompt.
#
# Quarantine, not delete: withdrawn docs move to knowledge/quarantine/ where they
# are out of retrieval (see _retrieval_roots) but fully recoverable. Deleting a
# user's store on their behalf is not ours to do.

# A SourceFile doc is VERIFIED when its body is the generated symbol summary and
# its frontmatter carries the symbols it claims. Anything else in a SourceFile is
# model text that predates the policy.
_VERIFIED_BODY_RE = re.compile(r"^Defines:\s*[\w,\s]+$")


def classify_concept(c: OKFConcept) -> tuple[str, str]:
    """``(verdict, reason)`` where verdict is 'keep' or 'quarantine'."""
    if c.type != "SourceFile":
        return "keep", f"{c.type} docs are not model-derived prose"
    body = c.body.strip()
    if not body:
        return "keep", "empty body carries no claims"
    if _VERIFIED_BODY_RE.match(body):
        return "keep", "verified symbol summary"
    if c.extra.get("key_symbols"):
        return "keep", "carries extracted key_symbols"
    return "quarantine", "free-text prose written before the verified-only policy"


def scan_store(base: Path = KNOWLEDGE_DIR) -> dict[str, list[OKFConcept]]:
    """Classify every doc in the store, including ones outside retrieval.

    Scans the legacy flat `source/` too — those are no longer injected, but they
    are still on disk and the user deserves to be told what is in them.
    """
    out: dict[str, list[OKFConcept]] = {"keep": [], "quarantine": []}
    roots = [
        base / "projects", base / "models", base / "source",
    ]
    for root in roots:
        if not root.exists():
            continue
        for md in sorted(root.rglob("*.md")):
            if md.name in ("index.md", "log.md"):
                continue
            try:
                concept = _parse_okf(md.read_text(encoding="utf-8"), md)
            except OSError:
                continue
            if concept is None:
                continue
            verdict, _reason = classify_concept(concept)
            out[verdict].append(concept)
    return out


def quarantine_concept(c: OKFConcept, base: Path = KNOWLEDGE_DIR) -> Path:
    """Move a doc out of retrieval into quarantine/, preserving its relative path.

    Never overwrites: a name collision gets a numeric suffix, so quarantining twice
    cannot destroy the first copy.
    """
    quarantine = base / "quarantine"
    try:
        rel = c.path.relative_to(base)
    except ValueError:
        rel = Path(c.path.name)
    dest = quarantine / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        n = 1
        while dest.with_name(f"{dest.stem}.{n}{dest.suffix}").exists():
            n += 1
        dest = dest.with_name(f"{dest.stem}.{n}{dest.suffix}")
    c.path.replace(dest)
    invalidate_cache()
    return dest


def gc_store(base: Path = KNOWLEDGE_DIR, apply: bool = False) -> dict[str, Any]:
    """Report (and optionally apply) quarantine of unverified docs.

    Dry-run by default. Moving a user's knowledge is a side effect they should ask
    for explicitly, so `apply` has to be set — `llm_router okf gc` reports, and
    `llm_router okf gc --apply` acts.
    """
    scanned = scan_store(base)
    moved: list[tuple[str, str]] = []
    if apply:
        for c in scanned["quarantine"]:
            dest = quarantine_concept(c, base)
            moved.append((str(c.path), str(dest)))
    return {
        "kept": len(scanned["keep"]),
        "flagged": len(scanned["quarantine"]),
        "applied": apply,
        "moved": moved,
        "flagged_docs": [
            (str(c.path), c.title, classify_concept(c)[1]) for c in scanned["quarantine"]
        ],
    }
