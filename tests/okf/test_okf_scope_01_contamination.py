"""OKF-SCOPE-01 — retrieval must not contaminate a prompt it has nothing to do with.

Field failure this pins (2026-09-10, s64-workbench / llm-router diagnosis):

    llm(task="query", prompt="... the question is: what is the capital of Portugal?")
    → the model reported having been handed `demo/llm/__init__.py` (a doc from a
      DIFFERENT project) and `ModelCapability gemini-2.5-flash`, 3677 tokens of
      injected background on a one-line general-knowledge question.

Three separate defects produced that, and each gets a test here:

1. `project_root()` walked for `.git` from **cwd**. The MCP server is long-lived and
   its cwd is wherever the host editor launched — `$HOME` in the field, which has no
   `.git`, so every project resolved to one bucket and cross-injected.
2. The shared `models/` catalog was a retrieval root. It is project-independent, so
   it matched everything, and its own prose advertises "OKF context injection" — so
   any prompt containing "context" scored a hit on the machinery describing itself.
3. `find_relevant` returned anything with `s > 0`. One shared word was enough.

The earlier "reply with the third word of the context" diagnostic is deliberately
NOT reused: on 2026-09-06 it returned "flash" and was read as context being dropped,
when in fact the model was reading the injected `gemini-2.5-flash` doc. A retrieved
doc can satisfy a positional question by coincidence. Ask for a token that exists
nowhere but the payload instead — see `test_caller_payload_survives_injection`.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from llm_router import okf


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """An OKF store with one project doc and the shared model catalog."""
    proj = tmp_path / "projects" / okf.project_slug(tmp_path / "repo-a")
    proj.mkdir(parents=True)
    (proj / "widget.md").write_text(
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
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "gemini-2.5-flash.md").write_text(
        textwrap.dedent("""\
            ---
            type: ModelCapability
            title: gemini-2.5-flash
            description: Fast, cheap Gemini model. Best for code gen, summarization.
            tags: [cheap, fast, code, gemini, cli]
            ---

            **Best used with**: OKF context injection for domain-specific tasks.
            """),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_cache():
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


# ── defect 1: scope resolution ──────────────────────────────────────────────

def test_project_root_honors_explicit_override(tmp_path, monkeypatch):
    """The override is the only signal that survives a process whose cwd is $HOME."""
    target = tmp_path / "some" / "repo"
    target.mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(target))
    assert okf.project_root() == target.resolve()


def test_project_root_override_absent_falls_back_to_walk(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert okf.project_root(sub) == repo.resolve()


def test_unusable_override_does_not_raise(tmp_path, monkeypatch):
    # `~nosuchuser` makes expanduser() raise — scoping must degrade, never crash
    # the request, since injection is best-effort background.
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", "~nosuchuser_zz991/repo")
    assert isinstance(okf.project_root(), Path)  # fell through, did not explode


def test_two_projects_do_not_share_a_scope(tmp_path, monkeypatch):
    """The bug: both resolved to the $HOME slug and saw each other's docs."""
    a, b = tmp_path / "repo-a", tmp_path / "repo-b"
    for r in (a, b):
        (r / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(a))
    slug_a = okf.project_slug()
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(b))
    slug_b = okf.project_slug()
    assert slug_a != slug_b


# ── defect 2: the model catalog is not task context ─────────────────────────

def test_model_catalog_is_not_a_retrieval_root(store):
    roots = okf._retrieval_roots(base=store)
    assert okf._catalog_root(base=store) not in roots
    assert not any(r.name == "models" for r in roots)


def test_capability_sheet_never_injected(store, monkeypatch):
    """The exact field symptom: a capability sheet arriving as task background."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    hits = okf.find_relevant(
        "explain how OKF context injection chooses a knowledge document",
        base=store,
    )
    assert all(c.title != "gemini-2.5-flash" for c in hits), (
        f"model catalog leaked into task context: {[c.title for c in hits]}"
    )


# ── defect 3: relevance floor ───────────────────────────────────────────────

def test_unrelated_prompt_retrieves_nothing(store, monkeypatch):
    """`capital of Portugal` must come back with zero docs."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    assert okf.find_relevant("what is the capital of Portugal?", base=store) == []


def test_generic_machinery_words_do_not_match(store, monkeypatch):
    """Prompts about asking, not about the domain, score zero distinctive hits."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    prompt = (
        "List the exact titles of any documents, knowledge blocks or context "
        "sections supplied alongside this question. Provide a short response."
    )
    assert okf.find_relevant(prompt, base=store) == []


def test_genuinely_relevant_prompt_still_retrieves(store, monkeypatch):
    """The floor must not be a mute button — real overlap still injects."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    hits = okf.find_relevant(
        "reconcile_invoice raises InvoiceMismatch on ledger_delta drift",
        base=store,
    )
    assert [c.title for c in hits] == ["billing/invoice_reconciler.py"]


def test_one_prose_only_word_is_below_the_floor(store, monkeypatch):
    """A word appearing only in a doc's BODY is incidental vocabulary, not a topic.

    Scored at _W_BODY=1, under the floor of 2. `entries` appears in the fixture's
    description ("ledger entries") and nowhere in its title or tags.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    assert okf.find_relevant("how do database entries get ordered?", base=store) == []


def test_one_identity_word_clears_the_floor(store, monkeypatch):
    """The counterpart: a single hit on title or tags IS the topic.

    A file path is one highly distinctive token, so a flat 2-keyword floor would
    have made paths unretrievable — which is exactly what the first attempt at
    this fix broke.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    hits = okf.find_relevant("invoice_reconciler", base=store)
    assert [c.title for c in hits] == ["billing/invoice_reconciler.py"]


def test_floor_is_configurable(store, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(store / "repo-a"))
    monkeypatch.setenv("LLM_ROUTER_OKF_MIN_SCORE", "1")
    assert okf._min_score() == 1
    monkeypatch.setenv("LLM_ROUTER_OKF_MIN_SCORE", "not-a-number")
    assert okf._min_score() == okf._MIN_SCORE_DEFAULT


# ── defect 4: precedence of retrieved background ────────────────────────────

def test_injected_block_declares_itself_subordinate(store):
    concepts = okf._load_dir_sync(store / "projects" / okf.project_slug(store / "repo-a"))
    assert concepts, "fixture produced no concepts"
    out = okf.inject_context("what is 2+2?", concepts)
    lowered = out.lower()
    assert "may be" in lowered and "irrelevant" in lowered
    assert "ignore it entirely" in lowered
    assert out.endswith("what is 2+2?"), "question must stay in the final position"


def test_inject_context_noop_without_concepts():
    assert okf.inject_context("unchanged", []) == "unchanged"


# ── the diagnostic that replaces "third word" ───────────────────────────────

def test_caller_payload_survives_injection(store):
    """A token existing ONLY in the caller's payload must still be in the prompt.

    This is the honest form of the 2026-09-06 diagnostic. "Third word of the
    context" was passable by a retrieved doc that happened to have a plausible
    third word, which is how a contamination bug was misread as a delivery bug.
    A unique marker cannot be answered by coincidence.
    """
    marker = "ZEBRA-MARKER-7741"
    concepts = okf._load_dir_sync(store / "projects" / okf.project_slug(store / "repo-a"))
    out = okf.inject_context(f"[Additional context]\n{marker}\n\nEcho the marker.", concepts)
    assert marker in out
