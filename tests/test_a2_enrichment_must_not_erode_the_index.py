"""Enrichment must not shrink a document the index wrote.

`_write_source_concept` does `write_text` — a full overwrite, no merge. That is
fine for `okf index`, which reads a whole file with `max_symbols=200`. It is wrong
for `context-capture.py`, which fires on EVERY tool call with the default cap of 10
and sees only whatever the tool happened to print.

So a tool result mentioning one function replaces that file's entire indexed
document with that one symbol. Measured on this machine after a few hours of
ordinary work, against the 1069 documents `okf index` had written:

    docs with FEWER symbols than the file defines: 19

    src/llm_router/hooks/auto-route.py    stored   1 / real  91
    src/llm_router/cost.py                stored   1 / real  63
    src/llm_router/router.py              stored   1 / real  52
    src/llm_router/okf.py                 stored   1 / real  36

The failure is silent and points the wrong way: the eroded files are the large
central ones, because those are what tool calls keep touching — so the documents
most likely to be asked about are hollowed out first. A live query for
`_write_source_concept` retrieved nothing an hour after the same query worked, and
the cause looked like a scoping problem rather than what it was.

Self-inflicted by OKF-INDEX-01 in 13.2.0: before it, only routed answers enriched,
and they were rare enough that nobody noticed the overwrite.

The rule: enrichment adds what it learned, and never removes what was already
known. A writer that sees less than the file contains must not be able to assert
that the file contains less.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router import okf


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


def _doc(base: Path, title: str) -> dict:
    path = okf.project_knowledge_dir(base=base) / "source" / Path(title).with_suffix(".md")
    concept = okf._parse_okf(path.read_text(encoding="utf-8"), path)
    return {"symbols": list(concept.extra.get("key_symbols") or []), "concept": concept}


RICH = ["alpha_fn", "bravo_fn", "charlie_fn", "delta_fn", "echo_fn"]


def test_a_later_write_with_fewer_symbols_does_not_shrink_the_doc(tmp_path):
    """The exact field failure: 36 indexed symbols replaced by 1."""
    base = tmp_path / "knowledge"
    okf._write_source_concept("pkg/mod.py", "Defines: " + ", ".join(RICH), RICH, "", base)
    assert set(_doc(base, "pkg/mod.py")["symbols"]) == set(RICH)

    # A tool call that only mentioned one of them.
    okf._write_source_concept("pkg/mod.py", "Defines: alpha_fn", ["alpha_fn"], "", base)

    after = set(_doc(base, "pkg/mod.py")["symbols"])
    assert after >= set(RICH), f"index eroded: {sorted(after)}"


def test_a_later_write_still_adds_genuinely_new_symbols(tmp_path):
    """Merging must not become 'ignore everything after the first write'."""
    base = tmp_path / "knowledge"
    okf._write_source_concept("pkg/mod.py", "Defines: alpha_fn", ["alpha_fn"], "", base)
    okf._write_source_concept("pkg/mod.py", "Defines: bravo_fn", ["bravo_fn"], "", base)
    assert set(_doc(base, "pkg/mod.py")["symbols"]) == {"alpha_fn", "bravo_fn"}


def test_reindexing_a_shrunken_file_can_remove_symbols(tmp_path):
    """A file really can lose a function, and the index must be able to say so.

    Only the indexer gets that authority — it read the whole file. Enrichment,
    which saw a fragment, never does.
    """
    base = tmp_path / "knowledge"
    okf._write_source_concept("pkg/mod.py", "Defines: " + ", ".join(RICH), RICH, "", base)
    okf._write_source_concept(
        "pkg/mod.py", "Defines: alpha_fn", ["alpha_fn"], "", base, authoritative=True
    )
    assert _doc(base, "pkg/mod.py")["symbols"] == ["alpha_fn"]


def test_the_description_reflects_the_merged_set(tmp_path):
    """The description is what `_score` reads as body text; a stale one would
    make the doc unfindable by the symbols it still claims to hold."""
    base = tmp_path / "knowledge"
    okf._write_source_concept("pkg/mod.py", "Defines: " + ", ".join(RICH), RICH, "", base)
    okf._write_source_concept("pkg/mod.py", "Defines: alpha_fn", ["alpha_fn"], "", base)
    desc = _doc(base, "pkg/mod.py")["concept"].description
    for sym in RICH:
        assert sym in desc, f"{sym} survived in key_symbols but vanished from the description"


def test_a_merged_doc_is_still_retrievable_by_an_older_symbol(tmp_path):
    """What the erosion actually cost: the query that stopped working."""
    base = tmp_path / "knowledge"
    okf._write_source_concept("pkg/mod.py", "Defines: " + ", ".join(RICH), RICH, "", base)
    okf._write_source_concept("pkg/mod.py", "Defines: alpha_fn", ["alpha_fn"], "", base)
    okf.invalidate_cache()
    hits = okf.find_relevant("what does echo_fn do", base=base)
    assert [h.title for h in hits] == ["pkg/mod.py"]


def test_a_corrupt_existing_doc_does_not_block_the_write(tmp_path):
    """Merging reads the old doc; an unreadable one must not lose the new data."""
    base = tmp_path / "knowledge"
    d = okf.project_knowledge_dir(base=base) / "source" / "pkg"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.md").write_text("this is not valid frontmatter", encoding="utf-8")
    okf._write_source_concept("pkg/mod.py", "Defines: alpha_fn", ["alpha_fn"], "", base)
    assert "alpha_fn" in _doc(base, "pkg/mod.py")["symbols"]


def test_the_symbol_cap_still_bounds_a_merged_doc(tmp_path):
    """Union across many writes must not grow a document without limit."""
    base = tmp_path / "knowledge"
    for i in range(60):
        okf._write_source_concept(
            "pkg/mod.py", f"Defines: sym_{i}", [f"sym_{i}"], "", base
        )
    assert len(_doc(base, "pkg/mod.py")["symbols"]) <= 200
