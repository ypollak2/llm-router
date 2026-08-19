"""Abridge — tiered compression of Chapters with content-addressed caching.

Tiers (token budgets are approximate, chars/4 heuristic):
  * full   — the chapter body verbatim (no LLM call).
  * brief  — 3-5 sentence abridgement (~150 tokens).
  * line   — single line (~30 tokens), suitable for book-level scans.

Cache: .llm_router/context/cache/abridge/<sha>--<tier>.md
Keyed on the *sealed chapter sha + tier*, so:
  * unchanged chapters are free forever,
  * a re-sealed/edited chapter automatically misses and re-abridges,
  * the cache never needs invalidation logic — stale keys are just unread.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from llm_router.library.sealer import _librarian
from llm_router.library.store import LibraryDoc, LibraryStore, now_utc, scrub_secrets

TIERS = ("full", "brief", "line")
TIER_TOKENS = {"brief": 150, "line": 30}

_PROMPTS = {
    "brief": ("Abridge this work-session chapter to 3-5 factual sentences. "
              "Keep concrete identifiers (files, functions, versions, shas). "
              "Past tense. No preamble.\n\nCHAPTER:\n{body}\n"),
    "line": ("Compress this chapter to ONE line (<= 20 words) stating what "
             "changed and why it matters. No preamble.\n\nCHAPTER:\n{body}\n"),
}


def _chapter_sha(ch: LibraryDoc) -> str:
    """Prefer the git sha stamped at seal time; fall back to a content hash
    so unsealed/legacy chapters still cache correctly."""
    sha = str(ch.meta.get("sealed_at_sha") or "").strip()
    if sha:
        return sha
    return hashlib.sha256(ch.body.encode("utf-8", "replace")).hexdigest()[:12]


def _mechanical(body: str, tier: str) -> str:
    """Deterministic fallback when the librarian is unavailable —
    truncation at sentence boundaries, never an error."""
    text = re.sub(r"\s+", " ", body).strip()
    budget = TIER_TOKENS[tier] * 4  # chars
    if len(text) <= budget:
        return text
    cut = text[:budget]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > budget // 2 else cut).strip() + " …"


def abridge(store: LibraryStore, ch: LibraryDoc, tier: str = "brief") -> str:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
    if tier == "full":
        return ch.body

    sha = _chapter_sha(ch)
    cache_rel = f"cache/abridge/{sha}--{tier}.md"
    cached = store.read_doc(cache_rel)
    if cached is not None:
        return cached.body.strip()

    out = _librarian(_PROMPTS[tier].format(body=ch.body[:12000]), timeout=60)
    text = (scrub_secrets(out.strip()) if out else _mechanical(ch.body, tier)).strip()
    store.write_doc(cache_rel, {
        "type": "abridgement",
        "tier": tier,
        "chapter_sha": sha,
        "source": ch.path.as_posix() if isinstance(ch.path, Path) else str(ch.path),
        "mechanical": not bool(out),
        "written_at": now_utc(),
    }, text)
    return text


def abridge_book(store: LibraryStore, book: str, tier: str = "brief") -> list[tuple[LibraryDoc, str]]:
    """Abridge every chapter of a book at the given tier (cache-first)."""
    return [(ch, abridge(store, ch, tier)) for ch in store.list_chapters(book)]
