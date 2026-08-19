"""CHZ-OKF-01/02 — per-project scoping, and quarantine of pre-policy model prose.

Two defects, one root cause: the store was a single flat global pile that was
never garbage-collected.

  scoping    a doc extracted while working in one repo stayed retrievable — and
             injectable — while working in an unrelated one.
  quarantine the verified-only policy stopped NEW prose from being written but
             never removed what was already there, so pre-policy model free-text
             (including, in the field, an invented filename) was still injected
             as background fact.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from llm_router import okf


@pytest.fixture(autouse=True)
def _fresh_cache():
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


def _write(path: Path, *, type_: str, title: str, body: str, **fm) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"type: {type_}", f"title: {title}"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n", encoding="utf-8")
    return path


# ── Project identity ─────────────────────────────────────────────────────────

def test_project_root_is_the_git_root_not_the_cwd(tmp_path, monkeypatch):
    """Otherwise src/ and tests/ accumulate two stores for one codebase."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert okf.project_root() == repo.resolve()


def test_slug_separates_same_named_checkouts(tmp_path):
    a, b = tmp_path / "one" / "llm_router", tmp_path / "two" / "llm_router"
    for p in (a, b):
        (p / ".git").mkdir(parents=True)
    sa, sb = okf.project_slug(a), okf.project_slug(b)
    assert sa != sb, "two clones of the same name must not share a store"
    assert sa.startswith("llm_router-") and sb.startswith("llm_router-")
    assert okf.project_slug(a) == sa, "slug must be stable across calls"


# ── Scoping: the cross-contamination fix ─────────────────────────────────────

def test_another_projects_docs_are_never_injected(tmp_path, monkeypatch):
    base = tmp_path / "knowledge"
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    for p in (mine, theirs):
        (p / ".git").mkdir(parents=True)

    _write(okf.project_knowledge_dir(mine, base) / "source" / "a.md",
           type_="SourceFile", title="mine/alpha.py", body="Defines: alpha")
    _write(okf.project_knowledge_dir(theirs, base) / "source" / "b.md",
           type_="SourceFile", title="theirs/beta.py", body="Defines: beta")

    monkeypatch.chdir(mine)
    titles = {c.title for c in okf._get_bundle(base)}
    assert "mine/alpha.py" in titles
    assert "theirs/beta.py" not in titles, "another project's doc leaked into retrieval"


def test_model_catalog_is_shared_across_projects(tmp_path, monkeypatch):
    """Model strengths do not change per repo, so the catalog stays global."""
    base = tmp_path / "knowledge"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(base / "models" / "m.md", type_="ModelCapability", title="fast-model", body="cheap")
    monkeypatch.chdir(repo)
    assert "fast-model" in {c.title for c in okf._get_bundle(base)}


def test_legacy_flat_source_is_no_longer_injected(tmp_path, monkeypatch):
    """The pre-scoping pile is what cross-contaminated; it stays on disk but out
    of retrieval until explicitly adopted."""
    base = tmp_path / "knowledge"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(base / "source" / "old.md", type_="SourceFile", title="legacy.py", body="Defines: old")
    monkeypatch.chdir(repo)
    assert "legacy.py" not in {c.title for c in okf._get_bundle(base)}


def test_bundle_cache_is_keyed_by_project(tmp_path, monkeypatch):
    """The MCP server is long-running and can serve several projects; a cache
    keyed on base alone would hand one project's docs to another for the TTL."""
    base = tmp_path / "knowledge"
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        (p / ".git").mkdir(parents=True)
    _write(okf.project_knowledge_dir(a, base) / "source" / "a.md",
           type_="SourceFile", title="a.py", body="Defines: fa")
    _write(okf.project_knowledge_dir(b, base) / "source" / "b.md",
           type_="SourceFile", title="b.py", body="Defines: fb")

    monkeypatch.chdir(a)
    assert {c.title for c in okf._get_bundle(base)} == {"a.py"}
    monkeypatch.chdir(b)  # same TTL window, different project
    assert {c.title for c in okf._get_bundle(base)} == {"b.py"}


def test_writes_land_in_the_project_store(tmp_path, monkeypatch):
    base = tmp_path / "knowledge"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    okf._write_source_concept("pkg/mod.py", "Defines: f", ["f"], "ollama/x", base)
    written = list((okf.project_knowledge_dir(repo, base) / "source").rglob("*.md"))
    assert written, "doc did not land in the project store"
    assert not (base / "source").exists(), "must not write to the legacy global pile"


# ── Quarantine ───────────────────────────────────────────────────────────────

def test_prose_is_flagged_and_verified_structure_is_kept(tmp_path):
    base = tmp_path / "knowledge"
    _write(base / "source" / "prose.md", type_="SourceFile", title="README.md",
           body="Here is the POSIX bash script `scripts/lint_capability_clims.sh`:")
    _write(base / "source" / "ok.md", type_="SourceFile", title="middleware.py",
           body="Defines: pre_tool_use")
    _write(base / "models" / "m.md", type_="ModelCapability", title="gpt", body="prose is fine here")

    report = okf.gc_store(base=base, apply=False)
    assert report["flagged"] == 1
    assert report["kept"] == 2, "model docs and verified summaries must be kept"
    assert "README.md" in report["flagged_docs"][0][1]


def test_gc_is_dry_run_by_default(tmp_path):
    base = tmp_path / "knowledge"
    doc = _write(base / "source" / "p.md", type_="SourceFile", title="x.py", body="rambling prose")
    okf.gc_store(base=base)
    assert doc.exists(), "dry run must not move anything"


def test_apply_quarantines_without_deleting(tmp_path):
    base = tmp_path / "knowledge"
    doc = _write(base / "source" / "p.md", type_="SourceFile", title="x.py", body="rambling prose")
    report = okf.gc_store(base=base, apply=True)
    assert not doc.exists()
    assert len(report["moved"]) == 1
    dest = Path(report["moved"][0][1])
    assert dest.exists(), "quarantine must preserve the file, never delete it"
    assert "quarantine" in dest.parts


def test_quarantined_docs_are_not_injected(tmp_path, monkeypatch):
    base = tmp_path / "knowledge"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(okf.project_knowledge_dir(repo, base) / "source" / "p.md",
           type_="SourceFile", title="x.py", body="rambling model prose")
    monkeypatch.chdir(repo)
    assert "x.py" in {c.title for c in okf._get_bundle(base)}
    okf.gc_store(base=base, apply=True)
    assert "x.py" not in {c.title for c in okf._get_bundle(base)}


def test_quarantine_never_clobbers_an_existing_copy(tmp_path):
    base = tmp_path / "knowledge"
    for _ in range(2):
        _write(base / "source" / "p.md", type_="SourceFile", title="x.py", body="prose one")
        okf.gc_store(base=base, apply=True)
    survivors = list((base / "quarantine").rglob("*.md"))
    assert len(survivors) == 2, "second quarantine overwrote the first"


# ── CHZ-OKF-03: OKF on the direct path, which is the default ────────────────

def test_direct_executor_injects_and_enriches(tmp_path, monkeypatch):
    """OKF was wired only into router.route_and_call. Direct execution is the
    DEFAULT and bypasses the router entirely, so most routed traffic neither
    received stored context nor contributed to the store — OKF looked enabled
    while doing nothing for the majority of calls."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "direct_executor",
        Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "direct_executor.py",
    )
    de = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = de  # @dataclass resolves types via sys.modules
    spec.loader.exec_module(de)

    assert hasattr(de, "_okf_inject"), "direct path does not consult the store"
    assert hasattr(de, "_okf_enrich"), "direct path does not contribute to the store"

    src = (Path(spec.origin)).read_text()
    assert "prompt = _okf_inject(prompt)" in src, "injection not applied to the prompt"
    assert "_okf_enrich(prompt, response," in src, "enrichment not applied to the response"


def test_okf_helpers_never_raise_into_the_hook(tmp_path, monkeypatch):
    """A hook that raises here drops the whole turn through to the expensive
    model — the exact opposite of what OKF is for."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "direct_executor2",
        Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "direct_executor.py",
    )
    de = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = de  # @dataclass resolves types via sys.modules
    spec.loader.exec_module(de)

    def _boom(*a, **k):
        raise RuntimeError("store on fire")

    monkeypatch.setattr(okf, "find_relevant", _boom)
    monkeypatch.setattr(okf, "enrich_from_response", _boom)
    assert de._okf_inject("hello") == "hello", "injection failure must pass the prompt through"
    de._okf_enrich("p", "r", "m")  # must not raise


def test_enrichment_strips_injected_context_before_capture():
    """Otherwise injected knowledge is re-captured as new knowledge — the
    self-poisoning feedback loop."""
    import asyncio

    poisoned = (
        "<knowledge_context>\n## [SourceFile] ghost.py\nDefines: ghost\n</knowledge_context>\n"
        "please look at real/module.py"
    )
    files, symbols = okf._extract_files_and_symbols(
        okf._KNOWLEDGE_CTX_RE.sub("", poisoned), "def real_fn(): pass"
    )
    assert "ghost.py" not in files, "injected block was re-captured into the store"
    assert "real/module.py" in files
    assert symbols == ["real_fn"]
    del asyncio
