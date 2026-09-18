"""The fortieth fact must not be the last thing this project ever learns.

`_merge_biography` capped growth with:

    current_count = sum(1 for ln in body.splitlines() if ln.startswith("- "))
    add = add[: max(0, MAX_BIO_FACTS - current_count)]

The comment above it reads "oldest facts stay (they earned their shelf space)",
and for a while they had. But the cap is absolute: once the document holds 40
bullets, `add` is empty on every subsequent merge and the biography is frozen
for the life of the repository. Nothing warns. Sessions keep closing, durable
facts keep being extracted, and every one of them is discarded.

Worse, it is ordered wrongly for the purpose. What earned a place is not what
arrived first — a fact from the first week of a project outranks a fact about
the bug that is live today purely by being older.

Raising the constant postpones the problem by exactly the amount it is raised.
The fix is to stop making the readable digest the store of record: every fact
becomes its own retrievable record, and `biography.md` becomes a bounded,
human-sized view over them that says so when it is not showing everything.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.library import book_closer
from llm_router.library.store import LibraryStore


@pytest.fixture
def store(tmp_path: Path) -> LibraryStore:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    # `repo_root` shells out to `git rev-parse`, so a bare .git directory is
    # not enough — it has to be a checkout git will actually answer for.
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    st = LibraryStore.for_repo(repo)
    assert st is not None
    st.ensure_layout()
    return st


def _fill(store: LibraryStore, n: int, prefix: str = "fact") -> None:
    facts = "\n".join(f"- {prefix} number {i}" for i in range(n))
    book_closer._merge_biography(store, f"book-{prefix}", facts, "abc1234")


def test_a_fact_after_the_cap_is_still_recorded(store):
    _fill(store, book_closer.MAX_BIO_FACTS, prefix="early")
    _fill(store, 1, prefix="the-live-bug")

    recorded = book_closer.all_durable_facts(store)
    assert any("the-live-bug" in f for f in recorded), (
        f"the {book_closer.MAX_BIO_FACTS + 1}th fact was discarded to preserve "
        f"the first {book_closer.MAX_BIO_FACTS}; {len(recorded)} facts recorded"
    )


def test_every_fact_survives_many_merges(store):
    for i in range(12):
        _fill(store, 10, prefix=f"round{i}")

    recorded = book_closer.all_durable_facts(store)
    assert len(recorded) == 120, (
        f"120 distinct facts were merged and {len(recorded)} survived"
    )
    assert any("round11" in f for f in recorded), "the most recent round was lost"
    assert any("round0" in f for f in recorded), "the earliest round was lost"


def test_the_readable_digest_stays_bounded(store):
    """Unbounded records, bounded document. Both, not one or the other."""
    for i in range(12):
        _fill(store, 10, prefix=f"round{i}")

    bio = store.read_doc("biography/biography.md")
    assert bio is not None
    bullets = [ln for ln in bio.body.splitlines() if ln.startswith("- ")]
    assert len(bullets) <= book_closer.MAX_BIO_FACTS, (
        f"the digest grew to {len(bullets)} bullets and stopped being readable"
    )


def test_the_digest_says_when_it_is_not_showing_everything(store):
    """A truncated view that looks complete is worse than no view."""
    for i in range(12):
        _fill(store, 10, prefix=f"round{i}")

    body = store.read_doc("biography/biography.md").body
    assert "120" in body, (
        "the digest shows a subset of the facts without saying so, so a reader "
        f"has no way to know 120 exist:\n{body[-400:]}"
    )


def test_duplicate_facts_are_still_deduplicated(store):
    """Unbounded storage is not a licence to record the same thing forever."""
    _fill(store, 5, prefix="same")
    _fill(store, 5, prefix="same")

    recorded = book_closer.all_durable_facts(store)
    assert len(recorded) == 5, f"the same five facts were recorded twice: {recorded}"


def test_existing_biography_text_is_never_rewritten(store):
    """The append-conservative contract this function was written with."""
    _fill(store, 3, prefix="original")
    before = store.read_doc("biography/biography.md").body
    first_bullet = next(ln for ln in before.splitlines() if ln.startswith("- "))

    _fill(store, 3, prefix="later")
    after = store.read_doc("biography/biography.md").body
    assert first_bullet in after, "an existing fact's text was rewritten"
