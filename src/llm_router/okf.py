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
    """
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


def _extract_files_and_symbols(clean_prompt: str, clean_response: str) -> tuple[list[str], list[str]]:
    """Pull checkable structure only: real file paths + defined symbol names.
    Shared by enrichment and session capture so both honor the verified-only rule."""
    files = list(dict.fromkeys(
        m.group(1).lstrip("./")
        for m in _FILE_PAT.finditer(clean_prompt + "\n" + clean_response)
        if not m.group(1).startswith(".")
    ))[:5]
    symbols = list(dict.fromkeys(m.group(1) for m in _SYM_PAT.finditer(clean_response)))[:10]
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
        parts = [f"## [{self.type}] {self.title}"]
        if self.description:
            parts.append(self.description)
        if self.body.strip():
            parts.append(self.body.strip())
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
    return [project_knowledge_dir(base=base), base / "models"]


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

def _score(concept: OKFConcept, keywords: list[str]) -> int:
    searchable = (
        f"{concept.title} {concept.description} {' '.join(concept.tags)} {concept.body}"
    ).lower()
    return sum(1 for kw in keywords if kw in searchable)


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
    keywords = list(dict.fromkeys(
        w for w in re.findall(r'\b\w{5,}\b', prompt.lower()) if not w.isdigit()
    ))[:25]
    if not keywords:
        return []
    scored = [(c, _score(c, keywords)) for c in concepts]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, s in scored[:limit] if s > 0]


def inject_context(prompt: str, concepts: list[OKFConcept]) -> str:
    """Prepend OKF concept docs to prompt inside a <knowledge_context> block."""
    if not concepts:
        return prompt
    blocks = "\n\n".join(c.as_context_block() for c in concepts)
    return f"<knowledge_context>\n{blocks}\n</knowledge_context>\n\n{prompt}"


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
) -> None:
    """Synchronous write; called in executor thread."""
    rel = Path(file_path)
    # CHZ-OKF-01: under this PROJECT's directory, not the flat global `source/`.
    # A doc about `middleware.py` is only meaningful next to the repo it came
    # from; filed globally it becomes a retrieval hazard for every other project.
    concept_path = project_knowledge_dir(base=base) / "source" / rel.with_suffix(".md")
    concept_path.parent.mkdir(parents=True, exist_ok=True)

    tags: list[str] = ["source-file"]
    if rel.suffix in (".py", ".ts", ".js", ".go", ".rs", ".java"):
        tags.append(rel.suffix.lstrip("."))

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
    if key_symbols:
        fm["key_symbols"] = key_symbols[:10]

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
    keywords = list(dict.fromkeys(
        w for w in re.findall(r"\b\w{5,}\b", prompt.lower()) if not w.isdigit()
    ))[:25]
    if not keywords:
        return []
    scored = [(c, _score(c, keywords)) for c in concepts]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, s in scored[:limit] if s > 0]


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
