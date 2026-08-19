"""Library layer (v0.8.1) — deterministic tests, no librarian/network.

Every LLM-touching path is exercised through its mechanical fallback by
stubbing _librarian to return None, so this suite is offline-safe.
"""

import json
import subprocess

import pytest

from llm_router.library import abridge as A
from llm_router.library import book_closer, pack, sealer
from llm_router.library.store import LibraryStore, scrub_secrets


@pytest.fixture()
def store(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # Bare CI runners have no global git identity — set one repo-locally so
    # the empty init commit (and any sealer-triggered commits) never fail.
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "llm_router-test"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email",
                    "llm_router-test@localhost"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-q", "-m", "init"], check=True)
    (tmp_path / ".llm-router").mkdir()
    s = LibraryStore.for_repo(tmp_path)
    s.ensure_layout()
    # offline: all librarian calls fall back to mechanical paths
    monkeypatch.setattr(sealer, "_librarian", lambda *a, **k: None)
    monkeypatch.setattr(A, "_librarian", lambda *a, **k: None)
    return s


def _seed_book(store, name="testbook", events=3):
    p = store.root / f"books/{name}/raw/events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for i in range(events):
            # mirror the harvest hook's real schema — 1-based ids, ts, files
            f.write(json.dumps({"id": i + 1, "ts": f"2026-07-12T13:0{i}:00Z",
                                "tool": "Bash", "desc": f"step {i}",
                                "cmd": f"echo {i}", "outcome": "ok",
                                "exit": 0, "files": [f"src/f{i}.py"],
                                "sha": "", "branch": "main"}) + "\n")
    return name


# ---- store ----------------------------------------------------------------

def test_doc_roundtrip_preserves_meta(store):
    store.write_doc("books/b/x.md", {"type": "chapter", "n": 1}, "body text")
    doc = store.read_doc("books/b/x.md")
    assert doc.meta["type"] == "chapter" and doc.meta["n"] == 1
    assert doc.body.strip() == "body text"


def test_read_missing_returns_none(store):
    assert store.read_doc("books/nope/x.md") is None


def test_scrub_secrets_redacts_keys():
    dirty = "token sk-abc123DEF456ghi789JKL012mno345 and ghp_" + "a" * 36
    clean = scrub_secrets(dirty)
    assert "sk-abc123" not in clean and "ghp_" + "a" * 36 not in clean


# ---- sealer (mechanical) ---------------------------------------------------

def test_seal_mechanical_fallback(store, tmp_path):
    book = _seed_book(store)
    ch = sealer.seal_chapter(store, book, "test-trigger", cwd=tmp_path)
    assert ch is not None
    chapters = store.list_chapters(book)
    assert len(chapters) == 1
    assert chapters[0].meta.get("seal_trigger") == "test-trigger"


def test_seal_empty_tail_is_noop(store, tmp_path):
    book = _seed_book(store, events=0)
    assert sealer.seal_chapter(store, book, "test", cwd=tmp_path) is None


# ---- book closer -----------------------------------------------------------

def test_close_book_writes_book_md(store, tmp_path, monkeypatch):
    monkeypatch.setattr(book_closer, "_librarian", lambda *a, **k: None)
    book = _seed_book(store)
    path = book_closer.close_book(store, book, cwd=tmp_path)
    assert path is not None
    doc = store.read_doc(f"books/{book}/book.md")
    assert doc.meta["type"] == "book" and doc.meta["chapters"] >= 1


def test_biography_merge_filters_none_and_dedupes(store):
    book_closer._merge_biography(store, "b1", "- None\n- (none)\n", "aaa")
    assert store.read_doc("biography/biography.md") is None
    book_closer._merge_biography(store, "b1", "- Fact one\n- none\n", "aaa")
    book_closer._merge_biography(store, "b2", "- Fact one\n", "bbb")
    bio = store.read_doc("biography/biography.md")
    assert bio.body.count("Fact one") == 1
    assert "(as of aaa" in bio.body


def test_biography_cap(store):
    for i in range(book_closer.MAX_BIO_FACTS + 10):
        book_closer._merge_biography(store, "b", f"- fact number {i}\n", "c")
    bio = store.read_doc("biography/biography.md")
    n = sum(1 for ln in bio.body.splitlines() if ln.startswith("- "))
    assert n <= book_closer.MAX_BIO_FACTS


# ---- abridge ---------------------------------------------------------------

def test_abridge_mechanical_and_cache(store, tmp_path):
    book = _seed_book(store)
    sealer.seal_chapter(store, book, "t", cwd=tmp_path)
    ch = store.list_chapters(book)[0]
    first = A.abridge(store, ch, "brief")
    assert first and first == A.abridge(store, ch, "brief")
    sha = A._chapter_sha(ch)
    cached = store.read_doc(f"cache/abridge/{sha}--brief.md")
    assert cached.meta["mechanical"] is True


def test_abridge_full_is_verbatim_and_bad_tier_raises(store, tmp_path):
    book = _seed_book(store)
    sealer.seal_chapter(store, book, "t", cwd=tmp_path)
    ch = store.list_chapters(book)[0]
    assert A.abridge(store, ch, "full") == ch.body
    with pytest.raises(ValueError):
        A.abridge(store, ch, "novella")


def test_mechanical_respects_budget():
    m = A._mechanical("A sentence here. " * 200, "line")
    assert len(m) <= A.TIER_TOKENS["line"] * 4 + 2


# ---- pack ------------------------------------------------------------------

def test_gate_shares_sum_to_one():
    assert abs(sum(pack.GATE_SHARES.values()) - 1.0) < 1e-9


def test_pack_empty_store_is_empty(store):
    assert pack.pack_for(store, None) == ""


def test_pack_orders_freshest_first(store, tmp_path, monkeypatch):
    monkeypatch.setattr(book_closer, "_librarian", lambda *a, **k: None)
    book = _seed_book(store)
    sealer.seal_chapter(store, book, "t", cwd=tmp_path)
    store.write_doc("working-memory/delta.md", {"type": "delta"}, "- live edit")
    out = pack.pack_for(store, book, budget_tokens=2000)
    now_i = out.index("Now (working memory)")
    ch_i = out.index("Latest chapter")
    assert now_i < ch_i


def test_pack_clamps_to_budget(store, tmp_path):
    book = _seed_book(store, events=10)
    sealer.seal_chapter(store, book, "t", cwd=tmp_path)
    store.write_doc("working-memory/delta.md", {"type": "delta"}, "x " * 5000)
    out = pack.pack_for(store, book, budget_tokens=100)
    assert len(out) < 100 * 4 * 2
