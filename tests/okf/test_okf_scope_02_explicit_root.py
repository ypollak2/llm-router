"""OKF-SCOPE-02 — an explicitly requested project is the one that gets written.

OKF-SCOPE-01 fixed *retrieval* contamination: a prompt in project B no longer
gets project A's documents. The write side was never fixed, and it fails the
other way round.

`index_project` takes a `root`. It resolves it, reports it in the returned
`store`, and then hands the write to `_write_source_concept`, which has no
`root` parameter at all and recomputes its destination with
`project_knowledge_dir(base=base)` — `root=None`, so `project_root()` falls back
to `$LLM_ROUTER_PROJECT_ROOT` or a `.git` walk from the process cwd.

So `index_project(root=B)` run from inside A reports B's store and writes A's.
The summary dict says one thing and the filesystem says another, which is the
worst shape a scope bug can have: it looks like it worked.

Three writers share the defect, and each is a separate case below, because a
`root` parameter that defaults to `None` fixes the one function it is added to
and leaves every caller that does not pass it exactly as broken as before:

  * `index_project` → `_write_source_concept`
  * `record_session_turn`, which computes its own `sessions/` destination
  * `hooks/context-capture.py`, which calls `_write_source_concept` DIRECTLY,
    bypassing `enrich_from_response`, on every tool call — the highest-volume
    writer of the three

The fourth case is the guard: a new writer that forgets `root` fails here at the
commit that adds it, rather than months later in a store nobody can explain.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from llm_router import okf


def _git_repo(path: Path, filename: str, body: str) -> Path:
    """A real git checkout — `index_project` shells out to `git ls-files`."""
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(body, encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.test"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True,
                       capture_output=True, timeout=30)
    return path


@pytest.fixture(autouse=True)
def _clean_cache():
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


@pytest.fixture
def two_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Project A is the process cwd. Project B is the one we ask for."""
    a = _git_repo(tmp_path / "project-a", "alpha.py", "def alpha_only():\n    pass\n")
    b = _git_repo(tmp_path / "project-b", "beta.py", "def beta_only():\n    pass\n")
    store = tmp_path / "store"
    store.mkdir()
    # The process is *inside* A, and nothing points at B but the argument.
    monkeypatch.chdir(a)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)
    monkeypatch.setenv("LLM_ROUTER_OKF", "on")
    return a, b, store


def _docs_under(store: Path, root: Path) -> list[Path]:
    proj = store / "projects" / okf.project_slug(root)
    return sorted(p for p in proj.rglob("*.md")) if proj.exists() else []


def test_index_project_writes_under_the_root_it_was_given(two_projects):
    a, b, store = two_projects

    summary = okf.index_project(root=b, base=store)

    assert summary["indexed"] == 1, summary
    b_docs = _docs_under(store, b)
    a_docs = _docs_under(store, a)
    assert [p.name for p in b_docs] == ["beta.md"], (
        f"indexing B wrote nothing under B. Summary claimed store="
        f"{summary['store']}; found under A instead: {[str(p) for p in a_docs]}"
    )
    assert a_docs == [], (
        f"indexing B leaked into A's knowledge directory: {[str(p) for p in a_docs]}"
    )


def test_indexed_doc_is_retrievable_from_b_and_invisible_from_a(two_projects):
    a, b, store = two_projects
    okf.index_project(root=b, base=store)

    from_b = okf.find_relevant("beta_only", base=store, root=b)
    from_a = okf.find_relevant("beta_only", base=store, root=a)

    assert any("beta.py" in c.title for c in from_b), (
        "B's own symbol is not retrievable from B"
    )
    assert not from_a, (
        f"B's document is retrievable from project A: {[c.title for c in from_a]}"
    )


def test_write_source_concept_honours_an_explicit_root(two_projects):
    a, b, store = two_projects

    okf._write_source_concept(
        "beta.py", "Defines: beta_only", ["beta_only"], "", store,
        authoritative=True, root=b,
    )

    assert [p.name for p in _docs_under(store, b)] == ["beta.md"]
    assert _docs_under(store, a) == []


def test_record_session_turn_honours_an_explicit_root(two_projects):
    a, b, store = two_projects

    written = okf.record_session_turn(
        "sess-1", "look at beta.py", "def beta_only():\n    pass\n", "m",
        base=store, root=b,
    )

    assert written is not None
    b_slug = okf.project_slug(b)
    assert b_slug in str(written), (
        f"session turn for project B landed outside B's namespace: {written}"
    )
    assert _docs_under(store, a) == [], (
        "a session turn recorded for B leaked into A"
    )


def test_every_okf_writer_takes_an_explicit_root():
    """The guard: adding `root` to one function does not fix its callers.

    `hooks/context-capture.py` calls `_write_source_concept` directly on every
    tool call. A `root=None` default keeps it silently on the cwd fallback, so
    the highest-volume writer stays broken while the unit tests above go green.
    """
    import inspect

    for fn in (okf._write_source_concept, okf.record_session_turn,
               okf.enrich_from_response, okf.index_project):
        assert "root" in inspect.signature(fn).parameters, (
            f"okf.{fn.__name__} cannot be told which project it is writing to"
        )

    src = Path(okf.__file__).resolve().parent
    offenders = []
    for path in list(src.rglob("*.py")) + list(src.rglob("*-*.py")):
        if path.name == "okf.py":
            continue
        raw = path.read_text(encoding="utf-8")
        # Prose mentions these names constantly — this module's own comments
        # explain the bug being fixed. Blank the comment bodies so only real call
        # sites are matched, and keep the newlines so line numbers stay true.
        text = "\n".join(
            "" if ln.lstrip().startswith(("#", '"""', "'''", "*")) else ln
            for ln in raw.splitlines()
        )
        for call in ("_write_source_concept(", "record_session_turn(",
                     "enrich_from_response("):
            idx = 0
            while (idx := text.find(call, idx)) != -1:
                # The call's argument list, to its balanced close paren.
                depth, j = 0, idx + len(call) - 1
                while j < len(text):
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if "root=" not in text[idx:j]:
                    line = text.count("\n", 0, idx) + 1
                    offenders.append(f"{path.relative_to(src)}:{line} {call[:-1]}")
                idx = j
    assert not offenders, (
        "these write to the OKF store without saying which project, so they fall "
        "back to the process cwd: " + ", ".join(offenders)
    )
