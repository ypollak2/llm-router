"""pack_for() — assemble a context pack through 5 freshness gates.

Gates, newest to oldest (budget shares of the total token budget):
  G1 working-memory  (25%) — delta.md verbatim; the live, unsealed now.
  G2 current-tail    (20%) — most recent sealed chapter of the open book, full.
  G3 session-briefs  (25%) — brief-tier abridgements of the open book's
                             remaining chapters, newest first.
  G4 older-books     (15%) — book.md summaries (fallback: line-tier chapter
                             abridgements) of prior books, newest first.
  G5 biography       (15%) — durable facts; the oldest, slowest layer.

Invariants:
  * a gate never steals another gate's budget — unspent share is simply
    returned to the caller as headroom (predictability > packing density),
  * every slice is labeled with its provenance so the model can weigh
    freshness itself,
  * chars/4 token heuristic throughout — same convention as abridge.
"""

from __future__ import annotations


from llm_router.library.abridge import abridge
from llm_router.library.store import LibraryStore

GATE_SHARES = {
    "working-memory": 0.25,
    "current-tail": 0.20,
    "session-briefs": 0.25,
    "older-books": 0.15,
    "biography": 0.15,
}


def _fit(text: str, tokens: int) -> str:
    budget = tokens * 4
    return text if len(text) <= budget else text[:budget].rsplit("\n", 1)[0]


def pack_for(store: LibraryStore, current_book: str | None,
             budget_tokens: int = 2000) -> str:
    slices: list[str] = []

    def gate(name: str, header: str, text: str | None) -> None:
        if not text or not text.strip():
            return
        share = int(budget_tokens * GATE_SHARES[name])
        fitted = _fit(text.strip(), share)
        if fitted.strip():
            slices.append(f"### {header}\n{fitted}")

    # G1 — working memory
    delta = store.read_doc("working-memory/delta.md")
    gate("working-memory", "Now (working memory)", delta.body if delta else None)

    # G2 + G3 — current book
    chapters = store.list_chapters(current_book) if current_book else []
    if chapters:
        tail = chapters[-1]
        gate("current-tail",
             f"Latest chapter — {tail.meta.get('title', 'untitled')}", tail.body)
        rest = chapters[:-1]
        if rest:
            briefs = "\n".join(
                f"- [{c.meta.get('sealed_at', '?')}] {c.meta.get('title', '?')}: "
                f"{abridge(store, c, 'brief')}"
                for c in reversed(rest))
            gate("session-briefs", "Earlier this session", briefs)

    # G4 — older books, newest first
    older: list[str] = []
    for book in store.list_books():
        if book == current_book:
            continue
        doc = store.read_doc(f"books/{book}/book.md")
        if doc is not None:
            older.append(f"- [{book}] {doc.body.strip()}")
        else:
            chs = store.list_chapters(book)
            if chs:
                older.append(f"- [{book}] " + " / ".join(
                    abridge(store, c, "line") for c in chs[-3:]))
        if len(older) >= 5:
            break
    gate("older-books", "Recent sessions", "\n".join(older))

    # G5 — biography
    bio = store.read_doc("biography/biography.md")
    gate("biography", "Durable facts (biography)", bio.body if bio else None)

    if not slices:
        return ""
    return "## Library context (freshest first)\n\n" + "\n\n".join(slices) + "\n"
