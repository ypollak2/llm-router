"""llm_router Library — persistent session memory in OKF.

Metaphor map (see docs/LIBRARY.md):
  Library         .llm_router/context/           the store
  Biography       biography/biography.md     stable cross-session project brief
  Book            books/<session>/           one session's memory
  Chapter         books/<s>/chapters/*.md    immutable, sealed on git events
  Working Memory  working-memory/delta.md    mutable in-flight context
  Manuscript      books/<s>/raw/events.jsonl mechanical zero-token harvest
  Abridgement     abridgements/<sha>/<tier>  fit-to-window cache
  Remembering     session-open pack assembly
"""

from llm_router.library.store import LibraryStore  # noqa: F401
