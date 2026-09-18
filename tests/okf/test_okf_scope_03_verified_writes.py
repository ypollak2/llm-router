"""OKF-SCOPE-03 — "checkable" is not the same as "checked".

`okf.py`'s own comment says the store holds ONLY checkable structure: real file
paths and extracted symbol names, never model prose. That is the right policy
and it is enforced for prose. It is not enforced for the paths and the symbols.

`_extract_files_and_symbols` runs two independent regexes over the text —
`_FILE_PAT` over prompt+response, `_SYM_PAT` over the response — and returns two
unrelated lists. `enrich_from_response` then pairs `files[0]` with *all* of
`symbols` and writes `"Defines: ..."`. Nothing reads the file. Nothing checks
that the file exists. Nothing checks that any of those symbols is defined in it.

So a model reply that mentions `nonexistent_module.py` and shows a
`def fabricated_symbol():` becomes a stored, retrievable, authoritative-looking
claim that `nonexistent_module.py` defines `fabricated_symbol`. The store was
built to stop the model inventing facts, and this is the one path where it still
can — dressed as structure rather than prose, which is exactly why it survived
the verified-only policy.

The pairing is wrong even when both halves are real. A reply that discusses
`billing/invoice.py` and shows a function defined in `billing/ledger.py` files
the ledger's symbol under the invoice's name.

`record_session_turn` repeats the whole thing on its own write path: unverified
paths go into its `tags` (where `find_relevant` scores them) and its body.

The rule these tests pin: a name is written only after the file has been read
and the definition found in it. Everything else is dropped, and the drop is
counted rather than silent — an enrichment that quietly discards everything and
an enrichment that had nothing to say look identical otherwise.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from llm_router import okf


@pytest.fixture(autouse=True)
def _clean_cache():
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A project with two real modules, and a store to write into."""
    root = tmp_path / "repo"
    (root / "billing").mkdir(parents=True)
    (root / "billing" / "invoice.py").write_text(
        "def build_invoice():\n    pass\n", encoding="utf-8")
    (root / "billing" / "ledger.py").write_text(
        "def post_ledger_entry():\n    pass\n", encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("LLM_ROUTER_OKF", "on")
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    return root, store


def _symbols_written(store: Path, root: Path) -> dict[str, list[str]]:
    """{relative doc path: key_symbols} for every SourceFile doc in the store."""
    proj = store / "projects" / okf.project_slug(root)
    out: dict[str, list[str]] = {}
    if not proj.exists():
        return out
    for path in proj.rglob("*.md"):
        concept = okf._parse_okf(path.read_text(encoding="utf-8"), path)
        if concept is not None:
            out[str(path.relative_to(proj))] = [
                str(s) for s in (concept.extra.get("key_symbols") or [])
            ]
    return out


def test_a_file_that_does_not_exist_is_never_written(repo):
    root, store = repo

    asyncio.run(okf.enrich_from_response(
        prompt="what does nonexistent_module.py do?",
        response_text="It defines the core entry point:\n\n"
                      "```python\ndef fabricated_symbol():\n    ...\n```\n",
        model="m", base=store, root=root,
    ))

    assert _symbols_written(store, root) == {}, (
        "a file the writer never opened became a stored claim about what it defines"
    )
    assert not okf.find_relevant("fabricated_symbol", base=store, root=root), (
        "the fabricated symbol is retrievable and will be injected as repo knowledge"
    )


def test_symbols_defined_elsewhere_are_not_filed_under_the_wrong_file(repo):
    root, store = repo

    # Both names are real. The pairing is not: the shown definition lives in
    # ledger.py, and `files[0]` is invoice.py.
    asyncio.run(okf.enrich_from_response(
        prompt="compare billing/invoice.py with billing/ledger.py",
        response_text="```python\ndef post_ledger_entry():\n    ...\n```\n",
        model="m", base=store, root=root,
    ))

    written = _symbols_written(store, root)
    assert "source/billing/invoice.md" not in written or (
        "post_ledger_entry" not in written["source/billing/invoice.md"]
    ), (
        f"ledger.py's symbol was filed as a definition in invoice.py: {written}"
    )


def test_a_real_definition_in_a_real_file_still_gets_written(repo):
    """The verification must not turn enrichment off.

    A check that rejects everything passes every negative test above and makes
    the feature useless. This is the positive control.
    """
    root, store = repo

    asyncio.run(okf.enrich_from_response(
        prompt="explain billing/invoice.py",
        response_text="```python\ndef build_invoice():\n    ...\n```\n",
        model="m", base=store, root=root,
    ))

    written = _symbols_written(store, root)
    assert written.get("source/billing/invoice.md") == ["build_invoice"], written


def test_a_partial_rejection_still_writes_what_survived(repo):
    """The mixed case: some names verify, some do not.

    Written because the first implementation of this fix raised NameError on the
    branch that reports the rejection — inside `enrich_from_response`'s bare
    `except Exception`, which swallowed it and turned enrichment off entirely.
    Neither of the tests above could see it: the negative cases expect an empty
    store either way, and the positive control drops nothing, so the reporting
    branch never ran. Every path that fires only on rejection needs a case where
    something is rejected AND something survives.
    """
    root, store = repo

    asyncio.run(okf.enrich_from_response(
        prompt="explain billing/invoice.py",
        response_text="```python\ndef build_invoice():\n    ...\n"
                      "def fabricated_symbol():\n    ...\n```\n",
        model="m", base=store, root=root,
    ))

    written = _symbols_written(store, root)
    assert written.get("source/billing/invoice.md") == ["build_invoice"], (
        f"a partial rejection lost the verified symbol too: {written}"
    )


def test_rejections_are_counted_not_silently_dropped(repo):
    """An enrichment that discarded everything must not look like a quiet no-op."""
    root, store = repo

    kept, rejected = okf.verify_symbols(
        "billing/invoice.py", ["build_invoice", "fabricated_symbol"], root=root,
    )

    assert kept == ["build_invoice"]
    assert rejected == ["fabricated_symbol"], (
        "the unverified name was dropped without being reported, so nothing "
        "downstream can tell 'nothing to record' from 'everything was invented'"
    )


def test_verify_symbols_refuses_a_path_outside_the_project(repo):
    """A path that escapes the root is not verifiable — it is an exfiltration try."""
    root, store = repo

    kept, rejected = okf.verify_symbols(
        "../../../../etc/passwd", ["root"], root=root,
    )

    assert kept == []
    assert rejected == ["root"]


def test_session_turns_do_not_claim_files_that_do_not_exist(repo):
    """`record_session_turn` has the same defect on its own write path.

    Its `tags` are scored by `find_relevant`, so an invented path there is not
    inert metadata — it is a retrieval key pointing at a file that never existed.

    Two kinds of fact live in one note and only one is a claim about the repo.
    The user's prompt is checkable because it IS their literal input — true even
    when they name a path that does not exist, and remembering that they asked
    is the whole point of the cross-session note. The `tags`, `Files:` and
    `Symbols:` entries are assertions about what the repository contains, and
    they are what retrieval scores. Verification filters the second and leaves
    the first alone; dropping the note entirely would discard a real user prompt
    to punish a filename they mistyped.
    """
    root, store = repo

    written_path = okf.record_session_turn(
        "sess-1",
        "look at nonexistent_module.py",
        "```python\ndef fabricated_symbol():\n    ...\n```\n",
        "m", base=store, root=root,
    )

    assert written_path is not None, "the user's real prompt was thrown away"
    concept = okf._parse_okf(written_path.read_text(encoding="utf-8"), written_path)
    assert concept is not None
    assert "nonexistent_module.py" not in concept.tags, (
        f"a file that does not exist became a retrieval tag: {concept.tags}"
    )
    body = concept.body
    assert "Files:" not in body, (
        f"a file that does not exist was asserted as repo structure: {body!r}"
    )
    assert "Symbols:" not in body, (
        f"a symbol nothing defines was asserted as repo structure: {body!r}"
    )
    # The prompt itself survives — that is what the user actually said.
    assert "nonexistent_module.py" in body


def test_a_session_turn_still_records_what_is_real(repo):
    """Positive control for the session path."""
    root, store = repo

    okf.record_session_turn(
        "sess-2",
        "look at billing/ledger.py",
        "```python\ndef post_ledger_entry():\n    ...\n```\n",
        "m", base=store, root=root,
    )

    proj = store / "projects" / okf.project_slug(root) / "sessions"
    written = "\n".join(p.read_text(encoding="utf-8") for p in proj.rglob("*.md"))
    assert "billing/ledger.py" in written
    assert "post_ledger_entry" in written
