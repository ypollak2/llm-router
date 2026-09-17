"""A derived index that never presents stale structure as current.

OKF's source documents say a file "Defines: a, b, c". That answers one
question. It cannot answer where a symbol is defined without scanning every
document, cannot say which files import it, and — the part that matters — has
no notion of the snapshot it described, so a document written before a rename
keeps asserting the old shape indefinitely.

This index is DERIVED. Nothing here is a source of truth: delete the database
and it rebuilds from the repository. That is what licenses it to be
aggressive about discarding — an entry whose bytes no longer hash to what was
parsed is not evidence, it is a memory of evidence, and the distinction is the
whole design.

The cases below are the acceptance list from the research document's §9, plus
one the plan's first draft dropped: a seeded experience record keys on a path,
and something has to actually join it to the entity once that path is indexed.
Without that test the experience layer and the structural layer ship as two
databases that never meet.

Extraction is `ast`, never a model. A regex thinks a name in a comment is a
definition; `ast` knows the difference, and a claim about what a file defines
has to be one the file can be made to prove.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.semantic import indexer as ix
from llm_router.semantic import store as sstore


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True,
                   capture_output=True, timeout=30)
    return path


LEDGER = '''\
"""Ledger entries."""


def post_entry(amount):
    """Write one entry."""
    return amount


class Ledger:
    def balance(self):
        return 0
'''

INVOICE = '''\
from ledger import post_entry


def build_invoice(lines):
    # post_entry is mentioned in this comment and that is not a definition
    return post_entry(sum(lines))
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path / "repo",
                 {"ledger.py": LEDGER, "invoice.py": INVOICE})


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "store"


# ── extraction ───────────────────────────────────────────────────────────────

def test_a_symbol_is_found_in_the_file_that_defines_it(repo, base):
    ix.index(root=repo, base=base)

    hits = sstore.find_definitions(root=repo, base=base, name="post_entry")
    assert [h.relative_path for h in hits] == ["ledger.py"]
    assert hits[0].kind == "function"
    assert hits[0].start_line == 4, "the span does not point at the definition"


def test_a_mention_in_a_comment_is_not_a_definition(repo, base):
    """The difference between `ast` and a regex, stated as a test."""
    ix.index(root=repo, base=base)

    hits = sstore.find_definitions(root=repo, base=base, name="post_entry")
    assert [h.relative_path for h in hits] == ["ledger.py"], (
        "invoice.py mentions post_entry in a comment and in a call, and was "
        "recorded as defining it"
    )


def test_imports_are_recorded_as_relations(repo, base):
    ix.index(root=repo, base=base)

    importers = sstore.find_importers(root=repo, base=base, module="ledger")
    assert [r.relative_path for r in importers] == ["invoice.py"]


def test_a_class_and_its_methods_are_both_entities(repo, base):
    ix.index(root=repo, base=base)

    assert sstore.find_definitions(root=repo, base=base, name="Ledger")
    method = sstore.find_definitions(root=repo, base=base, name="balance")
    assert method and method[0].qualified_name == "Ledger.balance", (
        "a method was recorded without the class that scopes it, so two "
        "classes with a `balance` are indistinguishable"
    )


# ── freshness: the part that makes it safe to trust ──────────────────────────

def test_deleting_a_file_removes_its_entities(repo, base):
    ix.index(root=repo, base=base)
    assert sstore.find_definitions(root=repo, base=base, name="post_entry")

    (repo / "ledger.py").unlink()
    ix.index(root=repo, base=base)

    assert sstore.find_definitions(root=repo, base=base, name="post_entry") == [], (
        "a deleted file's definitions are still being reported as current"
    )


def test_a_renamed_symbol_does_not_linger(repo, base):
    ix.index(root=repo, base=base)
    (repo / "ledger.py").write_text(
        LEDGER.replace("post_entry", "record_entry"), encoding="utf-8")
    ix.index(root=repo, base=base)

    assert sstore.find_definitions(root=repo, base=base, name="post_entry") == []
    assert sstore.find_definitions(root=repo, base=base, name="record_entry")


def test_a_parse_failure_reports_unavailable_rather_than_stale_structure(repo, base):
    """The dangerous case. Old relations must not describe new code."""
    ix.index(root=repo, base=base)
    (repo / "ledger.py").write_text("def post_entry(  # unclosed\n", encoding="utf-8")
    ix.index(root=repo, base=base)

    status = sstore.file_status(root=repo, base=base, relative_path="ledger.py")
    assert status == "parse_failed", f"status is {status!r}"
    assert sstore.find_definitions(root=repo, base=base, name="post_entry") == [], (
        "the file no longer parses and the index is still answering questions "
        "about it from the last version that did"
    )


def test_retrieved_evidence_carries_the_hash_it_was_parsed_from(repo, base):
    """So a caller can tell current evidence from a memory of evidence."""
    ix.index(root=repo, base=base)
    hit = sstore.find_definitions(root=repo, base=base, name="post_entry")[0]

    assert hit.source_hash
    assert sstore.evidence_is_current(root=repo, base=base, entity=hit)

    (repo / "ledger.py").write_text(LEDGER + "\n# touched\n", encoding="utf-8")
    assert not sstore.evidence_is_current(root=repo, base=base, entity=hit), (
        "the file changed and the index still reports its evidence as current"
    )


def test_reindexing_is_incremental_but_correct(repo, base):
    first = ix.index(root=repo, base=base)
    assert first.files_parsed == 2

    second = ix.index(root=repo, base=base)
    assert second.files_parsed == 0, "unchanged files were reparsed"
    assert second.files_skipped == 2

    (repo / "invoice.py").write_text(INVOICE + "\ndef extra():\n    pass\n",
                                     encoding="utf-8")
    third = ix.index(root=repo, base=base)
    assert third.files_parsed == 1
    assert sstore.find_definitions(root=repo, base=base, name="extra")


# ── isolation ────────────────────────────────────────────────────────────────

def test_two_projects_do_not_see_each_other(tmp_path):
    a = _repo(tmp_path / "a", {"mod.py": "def only_in_a():\n    pass\n"})
    b = _repo(tmp_path / "b", {"mod.py": "def only_in_b():\n    pass\n"})
    base = tmp_path / "store"

    ix.index(root=a, base=base)
    ix.index(root=b, base=base)

    assert sstore.find_definitions(root=a, base=base, name="only_in_b") == []
    assert sstore.find_definitions(root=b, base=base, name="only_in_a") == []
    assert sstore.find_definitions(root=a, base=base, name="only_in_a")


def test_indexing_b_from_inside_a_writes_bs_index(tmp_path, monkeypatch):
    """The prerequisite defect, at a new layer. It must not be reintroduced."""
    a = _repo(tmp_path / "a", {"mod.py": "def only_in_a():\n    pass\n"})
    b = _repo(tmp_path / "b", {"mod.py": "def only_in_b():\n    pass\n"})
    base = tmp_path / "store"
    monkeypatch.chdir(a)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)

    ix.index(root=b, base=base)

    assert sstore.find_definitions(root=b, base=base, name="only_in_b")
    assert sstore.find_definitions(root=a, base=base, name="only_in_b") == []


def test_a_symlink_cannot_pull_in_files_outside_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def exfiltrated():\n    pass\n")
    repo = _repo(tmp_path / "repo", {"mod.py": "def fine():\n    pass\n"})
    try:
        (repo / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    base = tmp_path / "store"

    ix.index(root=repo, base=base)

    assert sstore.find_definitions(root=repo, base=base, name="exfiltrated") == [], (
        "a symlink walked the indexer out of the project it was scoped to"
    )


# ── the join the first draft of the plan dropped ─────────────────────────────

def test_an_experience_record_joins_to_the_entity_it_names(tmp_path):
    """Otherwise the two layers are two databases that never meet.

    The experience record keys on (path, symbol) because entities did not
    exist when it was written. Once they do, something has to resolve that key
    — and it has to be tested, or the gap surfaces later as an empty section in
    a context pack.
    """
    from llm_router.semantic import experience as exp

    repo = _repo(tmp_path / "repo", {"ledger.py": LEDGER})
    base = tmp_path / "store"
    ix.index(root=repo, base=base)

    store = exp.ExperienceStore(tmp_path / "experience")
    store.put(exp.Lesson(
        lesson_id="ledger-001",
        statement="post_entry must not be called twice for one invoice line.",
        affected_paths=["ledger.py"],
        affected_symbols=["post_entry"],
        known_from="2026-09-18",
    ))

    linked = ix.link_experience(store, root=repo, base=base)

    assert linked, "no experience record resolved to any entity"
    link = linked[0]
    assert link.record.lesson_id == "ledger-001"
    assert [e.qualified_name for e in link.entities] == ["post_entry"]
    assert link.entities[0].relative_path == "ledger.py"
    assert link.is_resolved


def test_a_record_naming_only_a_file_is_resolved_not_empty(tmp_path):
    """A decision about a module names no function, and is not therefore lost.

    Found by running the real seed set against the real index: five records
    came back looking exactly like records whose code had been deleted, because
    a bare entity list cannot tell "no symbol was named" from "the symbol is
    gone". Most decisions are about a module.
    """
    from llm_router.semantic import experience as exp

    repo = _repo(tmp_path / "repo", {"ledger.py": LEDGER})
    base = tmp_path / "store"
    ix.index(root=repo, base=base)

    store = exp.ExperienceStore(tmp_path / "experience")
    store.put(exp.Decision(
        decision_id="ledger-is-append-only",
        statement="The ledger is append-only.",
        affected_paths=["ledger.py"],
        known_from="2026-09-18",
    ))

    link = ix.link_experience(store, root=repo, base=base)[0]
    assert link.entities == []
    assert link.resolved_paths == ["ledger.py"]
    assert link.missing_paths == []
    assert link.is_resolved, (
        "a decision about a whole module reported itself unresolved, which is "
        "indistinguishable from one whose file was deleted"
    )


def test_a_record_naming_a_path_that_no_longer_exists_is_reported_not_dropped(tmp_path):
    """A lesson about deleted code is a lesson about why it was deleted."""
    from llm_router.semantic import experience as exp

    repo = _repo(tmp_path / "repo", {"ledger.py": LEDGER})
    base = tmp_path / "store"
    ix.index(root=repo, base=base)

    store = exp.ExperienceStore(tmp_path / "experience")
    store.put(exp.Lesson(
        lesson_id="gone-002",
        statement="The old reconciler double-counted refunds.",
        affected_paths=["reconciler.py"],
        affected_symbols=["reconcile"],
        known_from="2026-09-18",
    ))

    linked = ix.link_experience(store, root=repo, base=base)
    assert len(linked) == 1, (
        "a lesson whose code is gone was silently dropped instead of being "
        "returned with its missing paths named"
    )
    link = linked[0]
    assert link.entities == []
    assert link.missing_paths == ["reconciler.py"]
    assert not link.is_resolved
