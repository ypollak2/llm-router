"""Extensive tests for src/llm_router/okf.py — OKF context injection, model catalog, enrichment."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import llm_router.okf as okf
from llm_router.okf import (
    OKFConcept,
    _get_bundle,
    _load_bundle_sync,
    _parse_okf,
    enrich_from_response,
    find_relevant,
    inject_context,
    invalidate_cache,
    load_model_capability,
    seed_model_catalog,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_md(base: Path, rel: str, content: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _concept(base: Path, name: str, type_: str, title: str, tags: list[str], body: str) -> Path:
    """Write a concept where retrieval will actually look for it.

    CHZ-OKF-01: injection reads the per-project store plus the shared model
    catalog, not the bare root of the knowledge dir. Fixtures dropped at the root
    are on disk but unreachable, which would make these tests assert nothing.
    """
    fm = {"type": type_, "title": title, "tags": tags, "description": f"Desc of {title}"}
    text = f"---\n{yaml.dump(fm).strip()}\n---\n\n{body}\n"
    rel = okf.project_knowledge_dir(base=base) / "source" / f"{name}.md"
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.write_text(text, encoding="utf-8")
    return rel


def _reset_cache() -> None:
    invalidate_cache()


# ---------------------------------------------------------------------------
# _parse_okf — unit tests
# ---------------------------------------------------------------------------

class TestParseOkf:
    def _path(self, tmp_path: Path) -> Path:
        return tmp_path / "concept.md"

    def test_valid_full_frontmatter(self, tmp_path: Path) -> None:
        p = self._path(tmp_path)
        text = "---\ntype: Table\ntitle: My Table\ndescription: A desc\ntags: [foo, bar]\n---\n\nBody text here."
        c = _parse_okf(text, p)
        assert c is not None
        assert c.type == "Table"
        assert c.title == "My Table"
        assert c.description == "A desc"
        assert c.tags == ["foo", "bar"]
        assert c.body == "Body text here."

    def test_only_required_type(self, tmp_path: Path) -> None:
        p = self._path(tmp_path)
        c = _parse_okf("---\ntype: Metric\n---\n\nSome body.", p)
        assert c is not None
        assert c.type == "Metric"
        assert c.title == "concept"   # defaults to path.stem
        assert c.tags == []
        assert c.description == ""

    def test_no_frontmatter_delimiter_returns_none(self, tmp_path: Path) -> None:
        assert _parse_okf("Just plain markdown.", self._path(tmp_path)) is None

    def test_incomplete_delimiter_returns_none(self, tmp_path: Path) -> None:
        assert _parse_okf("---\ntype: X\n", self._path(tmp_path)) is None

    def test_invalid_yaml_returns_none(self, tmp_path: Path) -> None:
        text = "---\n: bad: [yaml\n---\n\nbody"
        assert _parse_okf(text, self._path(tmp_path)) is None

    def test_yaml_scalar_not_dict_returns_none(self, tmp_path: Path) -> None:
        # YAML parses to a string, not a dict — must not crash
        assert _parse_okf("---\njust a scalar\n---\n\nbody", self._path(tmp_path)) is None

    def test_yaml_list_not_dict_returns_none(self, tmp_path: Path) -> None:
        assert _parse_okf("---\n- item1\n- item2\n---\n\nbody", self._path(tmp_path)) is None

    def test_extra_frontmatter_keys_in_extra(self, tmp_path: Path) -> None:
        text = "---\ntype: X\ncustom_field: 42\nanother: hello\n---\n\nbody"
        c = _parse_okf(text, self._path(tmp_path))
        assert c is not None
        assert c.extra["custom_field"] == 42
        assert c.extra["another"] == "hello"

    def test_tags_none_becomes_empty_list(self, tmp_path: Path) -> None:
        c = _parse_okf("---\ntype: X\ntags:\n---\n\nbody", self._path(tmp_path))
        assert c is not None
        assert c.tags == []

    def test_empty_body(self, tmp_path: Path) -> None:
        c = _parse_okf("---\ntype: X\n---\n\n", self._path(tmp_path))
        assert c is not None
        assert c.body == ""

    def test_body_preserves_whitespace_stripped(self, tmp_path: Path) -> None:
        c = _parse_okf("---\ntype: X\n---\n\n  hello  \n  world  \n", self._path(tmp_path))
        assert c is not None
        assert "hello" in c.body


# ---------------------------------------------------------------------------
# _load_bundle_sync and _get_bundle
# ---------------------------------------------------------------------------

class TestBundleLoading:
    def setup_method(self) -> None:
        _reset_cache()

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _load_bundle_sync(tmp_path / "missing") == []

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _load_bundle_sync(tmp_path) == []

    def test_loads_single_concept(self, tmp_path: Path) -> None:
        _concept(tmp_path, "tables/users", "Table", "Users", ["core"], "User records.")
        concepts = _load_bundle_sync(tmp_path)
        assert len(concepts) == 1
        assert concepts[0].type == "Table"
        assert concepts[0].title == "Users"

    def test_loads_multiple_concepts(self, tmp_path: Path) -> None:
        _concept(tmp_path, "a", "Table", "A", [], "body a")
        _concept(tmp_path, "b", "Metric", "B", [], "body b")
        _concept(tmp_path, "c/d", "Reference", "D", [], "body d")
        assert len(_load_bundle_sync(tmp_path)) == 3

    def test_skips_index_and_log(self, tmp_path: Path) -> None:
        _write_md(tmp_path, "index.md", "---\ntype: X\n---\nbody")
        _write_md(tmp_path, "log.md", "---\ntype: X\n---\nbody")
        _concept(tmp_path, "real", "Table", "Real", [], "body")
        concepts = _load_bundle_sync(tmp_path)
        assert len(concepts) == 1
        assert concepts[0].title == "Real"

    def test_skips_malformed_files(self, tmp_path: Path) -> None:
        _write_md(tmp_path, "bad.md", "no frontmatter here")
        _concept(tmp_path, "good", "Table", "Good", [], "body")
        concepts = _load_bundle_sync(tmp_path)
        assert len(concepts) == 1

    def test_get_bundle_caches_result(self, tmp_path: Path) -> None:
        _concept(tmp_path, "one", "Table", "One", [], "body")
        first = _get_bundle(tmp_path)
        # Write another file — cache should return stale result
        _concept(tmp_path, "two", "Table", "Two", [], "body")
        second = _get_bundle(tmp_path)
        assert first is second  # same list object = cache hit

    def test_get_bundle_respects_different_base(self, tmp_path: Path) -> None:
        base_a = tmp_path / "a"
        base_b = tmp_path / "b"
        _concept(base_a, "x", "Table", "X", [], "body")
        _concept(base_b, "y", "Metric", "Y", [], "body")
        a_concepts = _get_bundle(base_a)
        b_concepts = _get_bundle(base_b)
        assert {c.title for c in a_concepts} == {"X"}
        assert {c.title for c in b_concepts} == {"Y"}

    def test_invalidate_cache_forces_reload(self, tmp_path: Path) -> None:
        _concept(tmp_path, "one", "Table", "One", [], "body")
        before = _get_bundle(tmp_path)
        invalidate_cache()
        _concept(tmp_path, "two", "Table", "Two", [], "body")
        after = _get_bundle(tmp_path)
        assert len(before) == 1
        assert len(after) == 2

    def test_get_bundle_ttl_expiry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _concept(tmp_path, "one", "Table", "One", [], "body")
        # Simulate TTL expiry by rewinding the loaded-at timestamp
        _get_bundle(tmp_path)
        monkeypatch.setattr(okf, "_BUNDLE_LOADED_AT", 0.0)
        _concept(tmp_path, "two", "Table", "Two", [], "body")
        reloaded = _get_bundle(tmp_path)
        assert len(reloaded) == 2


# ---------------------------------------------------------------------------
# find_relevant
# ---------------------------------------------------------------------------

class TestFindRelevant:
    # find_relevant() is opt-in (LLM_ROUTER_OKF=on) — off by default to avoid the
    # self-poisoning contamination bug; these tests exercise the enabled path.
    @pytest.fixture(autouse=True)
    def _enable_okf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ROUTER_OKF", "on")

    def setup_method(self) -> None:
        _reset_cache()

    def test_returns_empty_when_no_bundle(self, tmp_path: Path) -> None:
        assert find_relevant("router gemini python", base=tmp_path / "empty") == []

    def test_finds_concept_by_keyword_in_title(self, tmp_path: Path) -> None:
        _concept(tmp_path, "router", "SourceFile", "Router Module", ["routing"], "Routes tasks.")
        _concept(tmp_path, "other", "SourceFile", "Unrelated Thing", ["cache"], "Cache logic.")
        results = find_relevant("router code task", base=tmp_path)
        assert any(c.title == "Router Module" for c in results)

    def test_finds_concept_by_keyword_in_tags(self, tmp_path: Path) -> None:
        _concept(tmp_path, "c", "SourceFile", "Cache", ["caching", "redis"], "Caches results.")
        results = find_relevant("caching strategy options", base=tmp_path)
        assert len(results) == 1
        assert results[0].title == "Cache"

    def test_finds_concept_by_keyword_in_body(self, tmp_path: Path) -> None:
        _concept(tmp_path, "x", "Table", "Users", [], "Contains authentication tokens and sessions.")
        results = find_relevant("authentication tokens refresh", base=tmp_path)
        assert len(results) == 1

    def test_excludes_zero_score_concepts(self, tmp_path: Path) -> None:
        _concept(tmp_path, "a", "Table", "Alpha", ["alpha"], "Alpha data.")
        _concept(tmp_path, "b", "Table", "Beta", ["beta"], "Beta data.")
        results = find_relevant("completely unrelated zymurgy", base=tmp_path)
        assert results == []

    def test_respects_limit(self, tmp_path: Path) -> None:
        for i in range(5):
            _concept(tmp_path, f"router{i}", "SourceFile", f"Router {i}", ["router"], "Routes tasks.")
        results = find_relevant("router routing routes", limit=3, base=tmp_path)
        assert len(results) <= 3

    def test_ranks_by_score_descending(self, tmp_path: Path) -> None:
        _concept(tmp_path, "high", "T", "High", ["router", "routing", "route"], "Routes routing router.")
        _concept(tmp_path, "low", "T", "Low", ["router"], "Just a router.")
        results = find_relevant("router routing route", base=tmp_path)
        assert results[0].title == "High"

    def test_ignores_short_words_under_5_chars(self, tmp_path: Path) -> None:
        _concept(tmp_path, "x", "T", "X", ["go"], "Go is a programming language.")
        # "go" is 2 chars — won't be extracted as keyword
        results = find_relevant("go run", base=tmp_path)
        assert results == []

    def test_empty_prompt_returns_empty(self, tmp_path: Path) -> None:
        _concept(tmp_path, "x", "T", "X", ["anything"], "body")
        results = find_relevant("", base=tmp_path)
        assert results == []


# ---------------------------------------------------------------------------
# inject_context
# ---------------------------------------------------------------------------

class TestInjectContext:
    def _make_concept(self) -> OKFConcept:
        return OKFConcept(
            path=Path("test.md"),
            type="Table",
            title="Users Table",
            body="Contains user records.",
            description="The users table.",
        )

    def test_empty_concepts_returns_prompt_unchanged(self) -> None:
        assert inject_context("my prompt", []) == "my prompt"

    def test_wraps_in_knowledge_context_tag(self) -> None:
        c = self._make_concept()
        result = inject_context("my prompt", [c])
        assert result.startswith("<knowledge_context>")
        assert "</knowledge_context>" in result

    def test_original_prompt_preserved_after_context(self) -> None:
        c = self._make_concept()
        result = inject_context("my prompt", [c])
        assert result.endswith("my prompt")

    def test_concept_title_in_output(self) -> None:
        c = self._make_concept()
        result = inject_context("prompt", [c])
        assert "Users Table" in result
        assert "Table" in result

    def test_concept_body_in_output(self) -> None:
        c = self._make_concept()
        result = inject_context("prompt", [c])
        assert "Contains user records." in result

    def test_concept_description_in_output(self) -> None:
        c = self._make_concept()
        result = inject_context("prompt", [c])
        assert "The users table." in result

    def test_multiple_concepts_all_present(self) -> None:
        c1 = OKFConcept(path=Path("a.md"), type="T", title="Alpha", body="Alpha body.")
        c2 = OKFConcept(path=Path("b.md"), type="T", title="Beta", body="Beta body.")
        result = inject_context("prompt", [c1, c2])
        assert "Alpha" in result
        assert "Beta" in result
        assert "Alpha body." in result
        assert "Beta body." in result

    def test_concept_without_description_no_blank_line(self) -> None:
        c = OKFConcept(path=Path("x.md"), type="T", title="X", body="body", description="")
        result = inject_context("p", [c])
        # No empty description line injected
        assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# seed_model_catalog
# ---------------------------------------------------------------------------

class TestSeedModelCatalog:
    def setup_method(self) -> None:
        _reset_cache()

    def test_seeds_four_models(self, tmp_path: Path) -> None:
        n = seed_model_catalog(tmp_path)
        assert n == 4

    def test_creates_model_files(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        models_dir = tmp_path / "models"
        assert models_dir.exists()
        md_files = list(models_dir.glob("*.md"))
        assert len(md_files) == 4

    def test_idempotent_second_call_writes_zero(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        n2 = seed_model_catalog(tmp_path)
        assert n2 == 0

    def test_all_model_docs_have_capability_type(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        for md in (tmp_path / "models").glob("*.md"):
            c = _parse_okf(md.read_text(), md)
            assert c is not None, f"{md.name} failed to parse"
            assert c.type == "ModelCapability", f"{md.name} has wrong type: {c.type}"

    def test_known_models_present(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        titles = {
            _parse_okf(p.read_text(), p).title
            for p in (tmp_path / "models").glob("*.md")
        }
        assert "gemini-2.5-flash" in titles
        assert "gemini-2.5-pro" in titles
        assert "gpt-5.5" in titles
        assert "gpt-5.4" in titles

    def test_partial_seed_fills_gap(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        # Delete one model doc
        (tmp_path / "models" / "gemini-2.5-flash.md").unlink()
        n = seed_model_catalog(tmp_path)
        assert n == 1  # only the missing one is re-written


# ---------------------------------------------------------------------------
# load_model_capability
# ---------------------------------------------------------------------------

class TestLoadModelCapability:
    def setup_method(self) -> None:
        _reset_cache()

    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        assert load_model_capability("gemini-2.5-flash", tmp_path / "empty") is None

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        (tmp_path / "models").mkdir(parents=True)
        assert load_model_capability("unknown-model", tmp_path) is None

    def test_loads_seeded_model(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        c = load_model_capability("gemini-2.5-flash", tmp_path)
        assert c is not None
        assert c.type == "ModelCapability"
        assert c.title == "gemini-2.5-flash"

    def test_rejects_wrong_type(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        _write_md(
            tmp_path,
            "models/mymodel.md",
            "---\ntype: SourceFile\ntitle: mymodel\n---\nbody",
        )
        assert load_model_capability("mymodel", tmp_path) is None

    def test_loads_by_short_name(self, tmp_path: Path) -> None:
        # Model stored as "openai/gpt-4o" → file "openai-gpt-4o.md", lookup by short "gpt-4o"
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        _write_md(
            tmp_path,
            "models/gpt-4o.md",
            "---\ntype: ModelCapability\ntitle: gpt-4o\n---\nbody",
        )
        c = load_model_capability("openai/gpt-4o", tmp_path)
        assert c is not None
        assert c.title == "gpt-4o"

    def test_sanitizes_slash_in_model_name(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        _write_md(
            tmp_path,
            "models/google-gemini-2.5-flash.md",
            "---\ntype: ModelCapability\ntitle: google/gemini-2.5-flash\n---\nbody",
        )
        c = load_model_capability("google/gemini-2.5-flash", tmp_path)
        assert c is not None


# ---------------------------------------------------------------------------
# enrich_from_response
# ---------------------------------------------------------------------------

class TestEnrichFromResponse:
    # enrich_from_response() is opt-in (LLM_ROUTER_OKF=on) — see TestFindRelevant.
    @pytest.fixture(autouse=True)
    def _enable_okf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ROUTER_OKF", "on")

    def setup_method(self) -> None:
        _reset_cache()

    async def test_writes_source_concept_for_mentioned_file(self, tmp_path: Path) -> None:
        await enrich_from_response(
            "Refactor src/llm_router/router.py to fix routing",
            "def route_and_call(prompt, ctx): pass",
            "gemini-2.5-flash",
            base=tmp_path,
        )
        # CHZ-OKF-01: docs are written under the per-project directory now.
        source_dir = okf.project_knowledge_dir(base=tmp_path) / "source"
        assert source_dir.exists()
        md_files = list(source_dir.rglob("*.md"))
        assert len(md_files) == 1

    async def test_written_concept_is_valid_okf(self, tmp_path: Path) -> None:
        await enrich_from_response(
            "Fix src/llm_router/router.py",
            "def route_and_call(): pass\ndef _dispatch(): pass",
            "gemini-2.5-flash",
            base=tmp_path,
        )
        md = next((okf.project_knowledge_dir(base=tmp_path) / "source").rglob("*.md"))
        c = _parse_okf(md.read_text(), md)
        assert c is not None
        assert c.type == "SourceFile"

    async def test_extracts_symbols_from_response(self, tmp_path: Path) -> None:
        await enrich_from_response(
            "Update src/llm_router/router.py",
            "def route_and_call(prompt):\n    pass\nclass Router:\n    pass",
            "gpt-5.5",
            base=tmp_path,
        )
        md = next((okf.project_knowledge_dir(base=tmp_path) / "source").rglob("*.md"))
        c = _parse_okf(md.read_text(), md)
        assert c is not None
        assert "route_and_call" in (c.extra.get("key_symbols") or [])
        assert "Router" in (c.extra.get("key_symbols") or [])

    async def test_records_model_name(self, tmp_path: Path) -> None:
        await enrich_from_response(
            "Fix src/llm_router/router.py",
            "def route_and_call(prompt, ctx): pass",  # must contain a real
            # symbol — enrich_from_response only records checkable structure,
            # never invents a summary from prose with nothing extractable
            "gemini-2.5-flash",
            base=tmp_path,
        )
        md = next((okf.project_knowledge_dir(base=tmp_path) / "source").rglob("*.md"))
        c = _parse_okf(md.read_text(), md)
        assert c is not None
        assert c.extra.get("last_model") == "gemini-2.5-flash"

    async def test_no_op_when_no_files_mentioned(self, tmp_path: Path) -> None:
        await enrich_from_response(
            "What is the meaning of life?",
            "Forty two.",
            "gemini-2.5-flash",
            base=tmp_path,
        )
        assert not (okf.project_knowledge_dir(base=tmp_path) / "source").exists()

    async def test_swallows_exceptions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _explode(*a, **kw):
            raise RuntimeError("disk is full")
        monkeypatch.setattr(okf, "_write_source_concept", _explode)
        # Must not raise
        await enrich_from_response(
            "Fix src/llm_router/router.py", "def foo(): pass", "x", base=tmp_path
        )

    async def test_detects_typescript_files(self, tmp_path: Path) -> None:
        await enrich_from_response(
            "Refactor src/app/router.ts for TypeScript",
            "function routeRequest() {}",
            "gemini-2.5-flash",
            base=tmp_path,
        )
        md_files = list((okf.project_knowledge_dir(base=tmp_path) / "source").rglob("*.md")) if (okf.project_knowledge_dir(base=tmp_path) / "source").exists() else []
        assert len(md_files) >= 1

    async def test_limits_to_first_file_when_many_mentioned(self, tmp_path: Path) -> None:
        prompt = "Files: src/a.py src/b.py src/c.py src/d.py src/e.py src/f.py"
        await enrich_from_response(prompt, "some response", "x", base=tmp_path)
        if (okf.project_knowledge_dir(base=tmp_path) / "source").exists():
            md_files = list((okf.project_knowledge_dir(base=tmp_path) / "source").rglob("*.md"))
            assert len(md_files) == 1  # only first file enriched


# ---------------------------------------------------------------------------
# OKFConcept.as_context_block
# ---------------------------------------------------------------------------

class TestOkfConceptAsContextBlock:
    def test_includes_type_and_title(self) -> None:
        c = OKFConcept(path=Path("x.md"), type="Table", title="My Table", body="body")
        block = c.as_context_block()
        assert "[Table]" in block
        assert "My Table" in block

    def test_includes_description_when_present(self) -> None:
        c = OKFConcept(path=Path("x.md"), type="T", title="T", body="body", description="A desc")
        assert "A desc" in c.as_context_block()

    def test_omits_description_when_empty(self) -> None:
        c = OKFConcept(path=Path("x.md"), type="T", title="T", body="body", description="")
        block = c.as_context_block()
        assert block.count("\n") < 3  # title + body only

    def test_includes_body(self) -> None:
        c = OKFConcept(path=Path("x.md"), type="T", title="T", body="Special body content")
        assert "Special body content" in c.as_context_block()

    def test_empty_body_no_crash(self) -> None:
        c = OKFConcept(path=Path("x.md"), type="T", title="T", body="")
        block = c.as_context_block()
        assert "[T]" in block


# ---------------------------------------------------------------------------
# Integration: end-to-end find → inject pipeline
# ---------------------------------------------------------------------------

class TestEndToEndPipeline:
    # find_relevant()/enrich_from_response() are opt-in — see TestFindRelevant.
    @pytest.fixture(autouse=True)
    def _enable_okf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ROUTER_OKF", "on")

    def setup_method(self) -> None:
        _reset_cache()

    def test_seeded_models_are_findable(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        invalidate_cache()
        results = find_relevant("gemini routing performance latency", base=tmp_path)
        assert any("gemini" in c.title for c in results)

    def test_inject_after_find_produces_valid_prompt(self, tmp_path: Path) -> None:
        seed_model_catalog(tmp_path)
        invalidate_cache()
        concepts = find_relevant("gemini flash code routing task", base=tmp_path)
        prompt = "Write a router that picks gemini flash for simple tasks"
        result = inject_context(prompt, concepts)
        assert "<knowledge_context>" in result
        assert prompt in result

    async def test_enriched_concepts_are_findable_after_routing(self, tmp_path: Path) -> None:
        # Simulate a routing call: enrich first, then find
        await enrich_from_response(
            "Fix src/llm_router/router.py route_and_call function",
            "def route_and_call(prompt, ctx): return 'result'",
            "gemini-2.5-flash",
            base=tmp_path,
        )
        invalidate_cache()
        results = find_relevant("router route_and_call llm_router", base=tmp_path)
        assert len(results) >= 1
        assert any("router" in c.title.lower() or "router" in c.body.lower() for c in results)
