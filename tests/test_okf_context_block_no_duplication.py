"""The injected block must not repeat itself.

`_write_source_concept` stores the same string twice: `description` is
`summary[:120]` and the body is the full `summary`. `as_context_block` then emits
both, so every SourceFile doc arrives at the model as its symbol list truncated
mid-name, immediately followed by the same list in full:

    ## [SourceFile] src/llm_router/router.py
    Defines: route_and_call, build_chain, ..., _format_subprocess_chain_error,      <- 120 chars
    Defines: route_and_call, build_chain, ..., execute_chain, _call_text, ...       <- 1003 chars

Measured on this machine: 120 against 1003 characters for `router.py`. With three
docs injected that is real budget spent on a duplicate, inside a draft context
budget of 3000 tokens — and the truncated copy is actively worse than useless,
because a name cut in half is a name the model can complete wrongly.

Fixed at render rather than at write, so documents already on disk benefit without
a re-index. The body wins when one contains the other, since the description is the
truncated one.
"""
from __future__ import annotations

from pathlib import Path

from llm_router.okf import OKFConcept


def _concept(description: str, body: str) -> OKFConcept:
    return OKFConcept(
        path=Path("x.md"), type="SourceFile", title="pkg/mod.py",
        description=description, body=body,
    )


def test_a_truncated_description_is_not_emitted_alongside_the_full_body():
    full = "Defines: " + ", ".join(f"symbol_{i}" for i in range(40))
    block = _concept(full[:120], full).as_context_block()
    assert block.count("Defines:") == 1, f"symbol list emitted twice:\n{block}"
    assert "symbol_39" in block, "the full list must be the one that survives"


def test_an_identical_description_and_body_are_emitted_once():
    same = "Defines: alpha_fn, bravo_fn"
    block = _concept(same, same).as_context_block()
    assert block.count("Defines:") == 1


def test_a_genuinely_different_description_is_kept():
    """Deduplication must not become 'drop the description'.

    Curated docs — the model catalog, hand-written notes — carry a description that
    says something the body does not.
    """
    block = _concept(
        "Reconciles invoice line items against ledger entries.",
        "Defines: reconcile_invoice, ledger_delta",
    ).as_context_block()
    assert "Reconciles invoice line items" in block
    assert "reconcile_invoice" in block


def test_the_title_line_is_always_present():
    block = _concept("d", "b").as_context_block()
    assert block.startswith("## [SourceFile] pkg/mod.py")


def test_an_empty_description_is_harmless():
    block = _concept("", "Defines: alpha_fn").as_context_block()
    assert "alpha_fn" in block
    assert block.count("Defines:") == 1


def test_an_empty_body_falls_back_to_the_description():
    block = _concept("Defines: alpha_fn", "").as_context_block()
    assert "alpha_fn" in block


def test_a_real_indexed_doc_does_not_duplicate(tmp_path, monkeypatch):
    """End to end through the writer that produces the asymmetry."""
    from llm_router import okf

    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    base = tmp_path / "knowledge"
    syms = [f"function_number_{i}" for i in range(30)]
    okf._write_source_concept("pkg/mod.py", "Defines: " + ", ".join(syms), syms, "", base)

    path = okf.project_knowledge_dir(base=base) / "source" / "pkg" / "mod.md"
    concept = okf._parse_okf(path.read_text(encoding="utf-8"), path)
    block = concept.as_context_block()
    assert block.count("Defines:") == 1, f"duplicated:\n{block}"
    assert "function_number_29" in block
