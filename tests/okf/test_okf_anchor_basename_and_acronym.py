"""OKF anchor fix — an explicit filename anchors the file it names.

Diagnosis: ``~/.rsi/research/routing-experiment-2026-10-01/okf_context_diagnosis.md``
§3, root cause #3, narrowed after independent re-review (OKF-ANCHOR-02/03).

The only gap this fix closes: ``_anchor_tokens(concept)`` ran ``_tokens(concept.title)``
on a path-shaped title, and ``_tokens`` keeps a dotted/slashed form WHOLE — so
``src/llm_router/okf.py`` tokenized to the full relative path, never to
``okf.py`` alone. A prompt that named the file the way a human actually types
it ("explain okf.py") could never anchor the doc about it, even though the
prompt token "okf.py" is already identifier-shaped and already an anchor
CANDIDATE (``_IDENTIFIER_SHAPED_RE`` matches on the ``.py`` extension).

Fixed narrowly: ``_anchor_tokens`` now also carries a path-shaped title's
BASENAME, with its extension, via ``_path_name_tokens``. Nothing else changed.

Two earlier, broader attempts at this fix were tried and both reverted after
review, because both reopened the exact false-positive class
``_IDENTIFIER_SHAPED_RE``'s own docstring warns about, just through a
different door:

1. Carrying the bare STEM too ("okf", not just "okf.py") let any short,
   common word that happened to equal a real file's stem act as an anchor —
   "BASE image for docker", "best API for weather data" matched this repo's
   own base.py / api.py purely because a file by that name exists somewhere
   in a 1637-doc index. Dropped: only the basename, never the stem, is
   carried now.
2. A bare capitalised acronym ("OKF", "API", "CLI") admitted as a keyword and
   promoted via a doc's stem or tag, even behind a stoplist, still leaked:
   13 of 13 non-stoplisted short stems from the real index (GC, BASE, TEST,
   CORE, STATE, SSE, PII, TUI, TEAM, TEXT, COST, EDIT, FS, ...) reproduced the
   same collision the stoplist was built to prevent, and the auto-applied
   "py" tag (present on 1595 of 1637 real docs) made a tag-based route
   abusable too. Dropped entirely: no acronym machinery of any kind remains.

Residual, accepted: "what does OKF mean" and bare "okf" still retrieve
nothing. Only an explicit filename (a token with a dot plus a known source
extension, matched against a doc's basename) anchors; nothing shorter does.
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


@pytest.fixture
def store_with_real_stems(tmp_path: Path) -> Path:
    """A store shaped like the real 1637-doc llm-router index: files whose
    STEMS are common English/tech words, plus the near-universal "py" tag
    that index auto-applies to almost every source doc (1595 of 1637 real
    docs carry it). Neither a bare-stem match nor the "py" tag may anchor —
    this is exactly the leak OKF-ANCHOR-02 found and this fix must not
    reopen, under either route.
    """
    proj = tmp_path / "projects" / okf.project_slug(tmp_path / "repo-stems")
    proj.mkdir(parents=True)
    files = {
        "api": "src/llm_router/api.py",
        "cli": "src/llm_router/cli.py",
        "base": "src/llm_router/base.py",
        "gc": "src/llm_router/commands/gc.py",
        "test": "src/llm_router/commands/test.py",
        "core": "src/llm_router/observability/core.py",
        "state": "src/llm_router/state.py",
    }
    for stem, title in files.items():
        (proj / f"{stem}.md").write_text(
            textwrap.dedent(f"""\
                ---
                type: SourceFile
                title: {title}
                description: A module named {stem}.
                tags: [py]
                key_symbols: [{stem}_entry]
                ---

                Defines: {stem}_entry.
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


# ── the headline question the diagnosis names ────────────────────────────────


def test_explain_okf_py_retrieves_the_doc(store, monkeypatch):
    """Naming the file the way a human types it must anchor the doc about it."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    hits = okf.find_relevant("explain okf.py and how context injection works", base=store)
    assert _titles(hits) == ["src/llm_router/okf.py"]


# ── the basename fix in isolation: basename yes, bare stem no ────────────────


def test_anchor_tokens_gains_the_basename_this_fix_adds():
    """``_path_name_tokens`` is this fix's only addition to ``_anchor_tokens``,
    and it contributes the basename ("okf.py") and nothing else — in
    particular, never a bare stem on its own (OKF-ANCHOR-02's lesson).

    A bare "okf" DOES still end up in the full anchor set below, same as on
    origin/main (``_tokens(title)``'s generic word-split already produces it
    from "src/llm_router/okf.py", independent of this fix, and is gated by
    the score floor like any other token — untouched here). What matters is
    that ``_path_name_tokens`` itself, this fix's new surface, is basename-only.
    """
    assert okf._path_name_tokens("src/llm_router/okf.py") == {"okf.py"}

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
    # the pre-fix behaviour (whole-path token) must still be present too
    assert "src/llm_router/okf.py" in anchors


def test_path_name_tokens_empty_for_a_non_path_title():
    assert okf._path_name_tokens("Routing Policy Overview") == set()


def test_path_name_tokens_is_basename_only():
    assert okf._path_name_tokens("src/llm_router/okf.py") == {"okf.py"}


# ── the negative test: no bare-stem route, no tag route, for a realistic index ─


def test_generic_short_stem_prompts_do_not_match_real_style_stems(
    store_with_real_stems, monkeypatch,
):
    """Generic, repo-agnostic prompts that happen to contain a capitalised
    short word must not retrieve a file merely because that file's STEM
    equals the word, and must not retrieve it via the near-universal "py"
    tag either. Every doc in this fixture is tagged "py"; if the tag alone
    could anchor, all of them would leak on every one of these prompts.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store_with_real_stems / "repo-stems"))
    prompts = [
        "how does GC work in Java",
        "BASE image for docker",
        "best API for weather data in my app",
        "is PY good for data science",
        "how do I TEST a flask endpoint",
        "what is the CORE idea behind functional programming",
        "how should I manage STATE in a react app",
        "write a quick CLI tool in bash",
    ]
    for p in prompts:
        hits = okf.find_relevant(p, base=store_with_real_stems)
        assert _titles(hits) == [], f"leaked on {p!r}: {_titles(hits)}"


# ── documented residual: no acronym or bare-stem path exists at all ──────────


def test_what_does_okf_mean_is_unanswered(store, monkeypatch):
    """Accepted residual: the bare acronym/stem "okf" (lowercase or caps) is
    not identifier-shaped (no dot, slash or underscore) and is below the
    >=5-char prose floor, so it is never collected as a scorable keyword at
    all. "What does OKF mean" cannot retrieve the doc that answers it.
    Explicit filenames ("okf.py") already work; the bare name does not, and
    is left that way on purpose — see the module docstring above for why the
    two broader fixes that could have closed this were both reverted.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    assert okf.find_relevant("what does OKF mean", base=store) == []
    assert okf.find_relevant("okf", base=store) == []
