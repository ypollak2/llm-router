"""OKF anchor fix — a path's basename/stem and a project's own acronyms anchor it.

Diagnosis: ``~/.rsi/research/routing-experiment-2026-10-01/okf_context_diagnosis.md``
§3, root cause #3. Two independent gaps, verified there by direct function calls
against the real retrieval code before this fix:

1. ``_anchor_tokens(concept)`` ran ``_tokens(concept.title)`` on a path-shaped
   title, and ``_tokens`` keeps a dotted/slashed form WHOLE — so
   ``src/llm_router/okf.py`` tokenized to the full relative path, never to
   ``okf.py`` or ``okf`` alone. A prompt that named the file the way a human
   actually types it ("explain okf.py") could never anchor the doc.
2. A bare acronym ("OKF") is 3 characters with no underscore, extension or
   slash, so ``_keywords_for_retrieval`` never even collected it as a scorable
   keyword, let alone an anchor. "What does OKF mean" could not have retrieved
   the doc that answers it even with perfect project scoping.

Both are fixed narrowly: ``_anchor_tokens`` now also carries a path-shaped
title's basename and stem (``_path_name_tokens``); a bare acronym is collected
only when it is written in caps in the prompt (``_ACRONYM_RE`` /
``_acronym_candidates`` — capitalisation, not length, is the signal, so this
does not reopen the ">=6 chars" false-positive problem _IDENTIFIER_SHAPED_RE's
own docstring describes), and it is promoted to an ANCHOR only when it names
something the project actually defines: an indexed file's own stem, or a tag a
concept's author wrote.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from llm_router import okf


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """An OKF store with the okf.py doc itself and one unrelated source file."""
    proj = tmp_path / "projects" / okf.project_slug(tmp_path / "repo-a")
    proj.mkdir(parents=True)
    (proj / "okf_doc.md").write_text(
        textwrap.dedent("""\
            ---
            type: SourceFile
            title: src/llm_router/okf.py
            description: Open Knowledge Format (OKF) integration for llm_router.
            tags: [knowledge, retrieval]
            key_symbols: [find_relevant, inject_context, OKFConcept]
            ---

            Defines: find_relevant, inject_context, OKFConcept.
            """),
        encoding="utf-8",
    )
    (proj / "invoice.md").write_text(
        textwrap.dedent("""\
            ---
            type: SourceFile
            title: billing/invoice_reconciler.py
            description: Reconciles invoice line items against ledger entries.
            tags: [billing, invoice, reconciler]
            key_symbols: [reconcile_invoice, ledger_delta, InvoiceMismatch]
            ---

            Defines: reconcile_invoice, ledger_delta, InvoiceMismatch.
            """),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_cache():
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


def _titles(hits):
    return [c.title for c in hits]


# ── the two headline questions the diagnosis names ───────────────────────────


def test_explain_okf_py_retrieves_the_doc(store, monkeypatch):
    """Naming the file the way a human types it must anchor the doc about it."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    hits = okf.find_relevant("explain okf.py and how context injection works", base=store)
    assert _titles(hits) == ["src/llm_router/okf.py"]


def test_what_does_okf_mean_retrieves_the_doc(store, monkeypatch):
    """The bare acronym, capitalised as a human actually writes it, must anchor too."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    hits = okf.find_relevant("what does OKF mean", base=store)
    assert _titles(hits) == ["src/llm_router/okf.py"]


# ── the basename/stem fix in isolation ────────────────────────────────────────


def test_anchor_tokens_carries_basename_and_stem():
    concept = okf.OKFConcept(
        path=Path("unused.md"),
        type="SourceFile",
        title="src/llm_router/okf.py",
        body="d",
        description="d",
        tags=[],
        extra={},
    )
    anchors = okf._anchor_tokens(concept)
    assert "okf.py" in anchors, "basename missing from anchor tokens"
    assert "okf" in anchors, "stem missing from anchor tokens"
    # the pre-fix behaviour (whole-path token) must still be present too
    assert "src/llm_router/okf.py" in anchors


def test_path_name_tokens_empty_for_a_non_path_title():
    assert okf._path_name_tokens("Routing Policy Overview") == set()


# ── the negative test: shape alone must not promote a generic acronym ────────


def test_generic_acronym_does_not_match_an_unrelated_doc(store, monkeypatch):
    """A capitalised 3-letter word that names nothing in the project must not anchor.

    "CLI" and "WTF" are the acronym shape (2-5 caps letters) but match no indexed
    file's stem and no concept's tag in this fixture, so neither may retrieve
    billing/invoice_reconciler.py or src/llm_router/okf.py. Without the
    stem/tag gate, EVERY capitalised short word would start matching whichever
    doc happens to clear the score floor on incidental vocabulary — the same
    failure mode OKF-INDEX-01 already fixed once for ordinary words.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    assert okf.find_relevant("explain how the CLI should behave here", base=store) == []
    assert okf.find_relevant("WTF is going on with this invoice", base=store) == []


def test_lowercase_short_word_is_not_treated_as_an_acronym(store, monkeypatch):
    """Only CAPS admits the acronym path; lowercase stays excluded by length."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    assert okf.find_relevant("okf", base=store) == []


def test_acronym_promotion_requires_an_actual_stem_or_tag_match(store, monkeypatch):
    """The gate: a caps acronym only anchors a doc it actually names.

    "INV" matches neither invoice_reconciler.py's stem nor okf.py's — it must
    retrieve nothing, even though "invoice" words are nearby in the fixture.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    assert okf.find_relevant("INV needs a fix", base=store) == []
