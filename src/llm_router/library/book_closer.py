"""Book closer — session-end summary + conservative Biography merge.

Rules:
  * book.md is written from Chapter synopses — one hop from Chapters,
    which are one hop from raw. Never deeper (anti summary-of-summary).
  * An unsealed tail (events after the last seal) gets a final loop-end
    Chapter before the Book closes — no memory left stranded in Working
    Memory overnight.
  * Biography updates are append-conservative: every fact cites a Chapter
    with inline "(as of <sha>, <date>)"; nothing is ever silently deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

from llm_router.library.sealer import _librarian, seal_chapter
from llm_router.library.store import LibraryDoc, LibraryStore, now_utc, scrub_secrets

MAX_BIO_FACTS = 40


def _chapter_synopsis(ch: LibraryDoc) -> str:
    first = next((ln for ln in ch.body.splitlines() if ln.strip()), "")
    return (f"- {ch.meta.get('title')} [{ch.meta.get('seal_trigger')}, "
            f"{ch.meta.get('sealed_at')}, {ch.meta.get('sealed_at_sha')}] "
            f"{first[:140]}")


def close_book(store: LibraryStore, book: str, cwd: Path | None = None) -> Path | None:
    # 1. Seal the unsealed tail first.
    seal_chapter(store, book, "loop-end", cwd=cwd)

    chapters = store.list_chapters(book)
    if not chapters:
        return None

    synopses = "\n".join(_chapter_synopsis(c) for c in chapters)
    prompt = (
        "Write a 4-8 sentence closing summary of this work session from its "
        "chapter synopses. Factual, past tense, cite chapter titles. Then, "
        "under a line 'DURABLE:', list 0-3 facts that will still matter in a "
        "month (architecture decisions, conventions, invariants) — or 'none'."
        f"\n\nCHAPTERS:\n{synopses}\n")
    out = _librarian(prompt, timeout=90)

    if out:
        parts = re.split(r"\nDURABLE:\s*\n?", out, maxsplit=1)
        summary = parts[0].strip()
        durable = parts[1].strip() if len(parts) > 1 else ""
    else:
        summary = "(mechanical close — librarian unavailable)\n" + synopses
        durable = ""

    last = chapters[-1]
    path = store.write_doc(f"books/{book}/book.md", {
        "type": "book",
        "book": book,
        "chapters": len(chapters),
        "closed_at": now_utc(),
        "last_sealed_sha": last.meta.get("sealed_at_sha", ""),
        "written_at": now_utc(),
    }, scrub_secrets(summary))

    if durable and durable.lower().strip() != "none":
        _merge_biography(store, book, durable, str(last.meta.get("sealed_at_sha", "")))

    store.regen_indexes()
    return path


def _merge_biography(store: LibraryStore, book: str, durable: str, sha: str) -> None:
    """Append-conservative: new dated facts go under '## Durable facts';
    existing text is never rewritten or removed."""
    bio = store.read_doc("biography/biography.md")
    stamp = now_utc()[:10]
    new_facts = []
    for ln in durable.splitlines():
        ln = ln.strip().lstrip("-*• ").strip()
        # skip empties and "none"-style non-facts, however phrased
        if not ln or re.fullmatch(r"\(?none\)?\.?", ln, re.IGNORECASE):
            continue
        new_facts.append(f"- {ln} (as of {sha or 'unknown'}, {stamp}; book {book})")
    if not new_facts:
        return

    if bio is None:
        body = "# Biography\n\n(auto-started by book closer)\n\n## Durable facts\n" + "\n".join(new_facts)
        meta = {"type": "biography", "written_at": now_utc(), "last_updated": now_utc(),
                "source_books": [book]}
    else:
        body = bio.body
        if "## Durable facts" not in body:
            body += "\n\n## Durable facts\n"
        existing = {ln.split(" (as of")[0].strip() for ln in body.splitlines()
                    if ln.startswith("- ")}
        add = [f for f in new_facts if f.split(" (as of")[0].strip() not in existing]
        # cap growth; oldest facts stay (they earned their shelf space)
        current_count = sum(1 for ln in body.splitlines() if ln.startswith("- "))
        add = add[: max(0, MAX_BIO_FACTS - current_count)]
        if not add:
            return
        body = body.rstrip() + "\n" + "\n".join(add)
        meta = dict(bio.meta)
        meta["last_updated"] = now_utc()
        books = meta.get("source_books") or []
        if not isinstance(books, list):
            books = [str(books)]
        if book not in books:
            books.append(book)
        meta["source_books"] = books[-12:]
    store.write_doc("biography/biography.md", meta, body)
