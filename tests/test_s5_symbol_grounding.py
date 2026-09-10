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


CONTEXT = "[assistant] Fixed persona attachment in demo/host/identity.py."


def test_a_symbol_from_the_context_is_grounded(hook):
    ctx = "[assistant] attach_persona_documents() was never called."
    assert hook._symbol_violations(
        "Call attach_persona_documents() during review setup.", ctx, prompt="fix it"
    ) == []


def test_a_symbol_from_the_prompt_is_grounded(hook):
    assert hook._symbol_violations(
        "reconcile_invoice() should be idempotent.", CONTEXT,
        prompt="is reconcile_invoice() idempotent?",
    ) == []


def test_an_invented_symbol_is_caught(hook, monkeypatch):
    monkeypatch.setattr(hook, "_known_symbols", lambda: {"find_relevant", "inject_context"})
    bad = hook._symbol_violations(
        "Call reconcile_invoice_totals() first.", CONTEXT, prompt="fix it"
    )
    assert "reconcile_invoice_totals" in bad


def test_a_symbol_in_the_okf_index_is_grounded(hook, monkeypatch):
    monkeypatch.setattr(hook, "_known_symbols", lambda: {"find_relevant"})
    assert hook._symbol_violations(
        "find_relevant() returns the top matches.", CONTEXT, prompt="explain"
    ) == []


def test_an_empty_index_never_rejects(hook, monkeypatch):
    """Unknown is not invented. Before `okf index` runs, nothing is checkable."""
    monkeypatch.setattr(hook, "_known_symbols", lambda: set())
    assert hook._symbol_violations(
        "Call anything_at_all() twice.", CONTEXT, prompt="go"
    ) == []


def test_prose_is_still_never_flagged(hook, monkeypatch):
    """The U7 case. Structural checks cannot see it; the gate handles it."""
    monkeypatch.setattr(hook, "_known_symbols", lambda: {"find_relevant"})
    draft = "# U7: Document Attachment Fix\n\nThe issue is that documents are missing."
    assert hook._symbol_violations(draft, CONTEXT, prompt="continue with U7") == []


def test_common_language_is_not_mistaken_for_a_symbol(hook, monkeypatch):
    """`run()`-shaped English must not trip the check."""
    monkeypatch.setattr(hook, "_known_symbols", lambda: {"find_relevant"})
    draft = "You should test() the change, then build() and deploy() it."
    flagged = hook._symbol_violations(draft, CONTEXT, prompt="go")
    assert flagged == [], f"ordinary words flagged as invented symbols: {flagged}"


def test_the_relay_decision_accounts_for_symbols(hook, monkeypatch):
    monkeypatch.setattr(hook, "_known_symbols", lambda: {"find_relevant"})
    assert hook._draft_is_relayable(
        "Call reconcile_invoice_totals() first.", CONTEXT, prompt="fix it"
    ) is False
    assert hook._draft_is_relayable(
        "find_relevant() returns matches.", CONTEXT, prompt="explain"
    ) is True


def test_symbol_checking_can_be_disabled_independently(hook, monkeypatch):
    """Path checking is cheap and certain; symbol checking depends on an index
    being fresh, so it gets its own switch."""
    monkeypatch.setattr(hook, "_known_symbols", lambda: {"find_relevant"})
    monkeypatch.setenv("LLM_ROUTER_SYMBOL_GROUNDING", "off")
    assert hook._draft_is_relayable(
        "Call reconcile_invoice_totals() first.", CONTEXT, prompt="fix it"
    ) is True


def test_a_broken_symbol_lookup_fails_open(hook, monkeypatch):
    def _boom():
        raise RuntimeError("index unavailable")
    monkeypatch.setattr(hook, "_known_symbols", _boom)
    assert hook._symbol_violations("Call whatever() now.", CONTEXT, prompt="go") == []
