"""S5 — a draft may not name functions or classes that do not exist either.

S2-6 validates file paths and stops there. It caught a draft citing
`demo/main.py` in the S2-5b quality check, which is the mechanical failure it was
built for. It does not catch a draft that names a plausible-sounding function that
was never written — and `okf index` has now put 1063 files' worth of real symbol
names in the store, so that claim became checkable too.

Scope, stated honestly. This does NOT catch the U7-class fabrication from S2-5b:

    "continue with U7"  ->  "# U7: Document Attachment Fix for Arena Demo ..."

That invention was pure prose, naming no symbols at all. Nothing structural can
catch it, which is why S2-5b fixed it at the gate instead — the right layer for it.
What this adds is the case between the two: a draft that sounds specific because it
cites `reconcile_invoice_totals()`, where no such function exists anywhere.

The asymmetry that makes this safe: a symbol found in the index, in the context, or
in the prompt is grounded. Only a name that appears in NONE of them is a violation,
and even then only when the store has been indexed — an empty index means "unknown",
not "invented", and unknown must never reject.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router import grounding

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("s5_auto_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["s5_auto_route"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def _retrieved(body: str) -> str:
    """Context as it actually arrives when OKF fired.

    Symbol checking only applies to retrieval-backed answers — the index knows this
    repository and nothing else, so it has no standing to judge names in a
    general-purpose answer. The block is the signal that the draft is about the
    indexed project, so a realistic fixture has to carry it.
    """
    return f"<knowledge_context>\n{body}\n</knowledge_context>"


CONTEXT = _retrieved("[assistant] Fixed persona attachment in demo/host/identity.py.")


def test_a_symbol_from_the_context_is_grounded(hook):
    ctx = "[assistant] attach_persona_documents() was never called."
    assert grounding.symbol_violations(
        "Call attach_persona_documents() during review setup.", ctx, prompt="fix it"
    ) == []


def test_a_symbol_from_the_prompt_is_grounded(hook):
    assert grounding.symbol_violations(
        "reconcile_invoice() should be idempotent.", CONTEXT,
        prompt="is reconcile_invoice() idempotent?",
    ) == []


def test_an_invented_symbol_is_caught(hook, monkeypatch):
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant", "inject_context"})
    bad = grounding.symbol_violations(
        "Call reconcile_invoice_totals() first.", CONTEXT, prompt="fix it"
    )
    assert "reconcile_invoice_totals" in bad


def test_a_symbol_in_the_okf_index_is_grounded(hook, monkeypatch):
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    assert grounding.symbol_violations(
        "find_relevant() returns the top matches.", CONTEXT, prompt="explain"
    ) == []


def test_an_empty_index_never_rejects(hook, monkeypatch):
    """Unknown is not invented. Before `okf index` runs, nothing is checkable."""
    monkeypatch.setattr(grounding, "known_symbols", lambda: set())
    assert grounding.symbol_violations(
        "Call anything_at_all() twice.", CONTEXT, prompt="go"
    ) == []


def test_prose_is_still_never_flagged(hook, monkeypatch):
    """The U7 case. Structural checks cannot see it; the gate handles it."""
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    draft = "# U7: Document Attachment Fix\n\nThe issue is that documents are missing."
    assert grounding.symbol_violations(draft, CONTEXT, prompt="continue with U7") == []


def test_common_language_is_not_mistaken_for_a_symbol(hook, monkeypatch):
    """`run()`-shaped English must not trip the check."""
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    draft = "You should test() the change, then build() and deploy() it."
    flagged = grounding.symbol_violations(draft, CONTEXT, prompt="go")
    assert flagged == [], f"ordinary words flagged as invented symbols: {flagged}"


def test_the_relay_decision_accounts_for_symbols(hook, monkeypatch):
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    assert grounding.draft_is_relayable(
        "Call reconcile_invoice_totals() first.", CONTEXT, prompt="fix it"
    ) is False
    assert grounding.draft_is_relayable(
        "find_relevant() returns matches.", CONTEXT, prompt="explain"
    ) is True


def test_symbol_checking_can_be_disabled_independently(hook, monkeypatch):
    """Path checking is cheap and certain; symbol checking depends on an index
    being fresh, so it gets its own switch."""
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    monkeypatch.setenv("LLM_ROUTER_SYMBOL_GROUNDING", "off")
    assert grounding.draft_is_relayable(
        "Call reconcile_invoice_totals() first.", CONTEXT, prompt="fix it"
    ) is True


def test_a_broken_symbol_lookup_fails_open(hook, monkeypatch):
    def _boom():
        raise RuntimeError("index unavailable")
    monkeypatch.setattr(grounding, "known_symbols", _boom)
    assert grounding.symbol_violations("Call whatever() now.", CONTEXT, prompt="go") == []


# ── backticked identifiers: the gap the live probe found ────────────────────

LIVE_ANSWER = (
    "The function `_write_source_concept` is defined in `src/llm_router/okf.py`. "
    "It writes a concept's source code to a file, primarily used for debugging or "
    "logging purposes. The function is called by `write_concept` and "
    "`write_concept_from_mcp`."
)


def test_the_live_fabrication_is_caught(hook, monkeypatch):
    """Found by probing the real MCP tool after Stage B landed.

    Retrieval was correct — `src/llm_router/okf.py` is where the function lives,
    and the model could only know that from OKF. It then invented two callers.
    Neither `write_concept` nor `write_concept_from_mcp` exists; the real callers
    are okf.py:1078 and context-capture.py:148.

    The check passed it, because `_symbol_violations` required a CALL shape —
    `name(` with adjacent parens — and the model wrote prose with the names in
    backticks. That shape requirement was deliberate: `\\w+\\s*\\(` had a 2-in-3
    false-positive rate on ordinary English. Backticks are the missing third form:
    a fenced identifier is an explicit code claim, not incidental prose.
    """
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"_write_source_concept"})
    bad = grounding.symbol_violations(LIVE_ANSWER, _retrieved("okf.py"), prompt="what does it do")
    assert "write_concept" in bad
    assert "write_concept_from_mcp" in bad
    assert "_write_source_concept" not in bad, "the real symbol must stay grounded"


def test_a_backticked_symbol_that_exists_is_grounded(hook, monkeypatch):
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    assert grounding.symbol_violations(
        "Call `find_relevant` first.", _retrieved("okf.py"), prompt="explain"
    ) == []


@pytest.mark.parametrize("fenced", [
    "`src/llm_router/okf.py`",       # a path — the path check's job, not this one
    "`okf.py`",
    "`--dry-run`",                   # a flag
    "`pip install llm-routing`",     # a command
    "`LLM_ROUTER_PROJECT_ROOT`",     # an env var, not a function
    "`MAX_RETRIES`",                 # a module constant
    "`true`",                        # a literal
    "`some plain words`",
])
def test_backticked_non_symbols_are_not_flagged(hook, monkeypatch, fenced):
    """Backticks fence many things. Only identifier-shaped names are code claims.

    ALL_CAPS is excluded on purpose: env vars and constants are the most common
    backticked identifier-shaped tokens in this project's own writing, and flagging
    them would reject correct answers about configuration.
    """
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    flagged = grounding.symbol_violations(
        f"See {fenced} for details.", _retrieved("okf.py"), prompt="go")
    assert flagged == [], f"{fenced} was read as an invented symbol: {flagged}"


def test_a_backticked_symbol_present_in_context_is_grounded(hook, monkeypatch):
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"other_thing"})
    ctx = _retrieved("[assistant] attach_persona_documents was never called.")
    assert grounding.symbol_violations(
        "Call `attach_persona_documents` during setup.", ctx, prompt="fix"
    ) == []


def test_backticked_and_called_forms_agree(hook, monkeypatch):
    """The same invented name must be caught whichever way it is written."""
    monkeypatch.setattr(grounding, "known_symbols", lambda: {"find_relevant"})
    ctx = _retrieved("## [SourceFile] src/llm_router/okf.py")
    a = grounding.symbol_violations("Call `made_up_helper` now.", ctx, prompt="x")
    b = grounding.symbol_violations("Call made_up_helper() now.", ctx, prompt="x")
    assert a == b == ["made_up_helper"]
