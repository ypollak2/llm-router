"""One project, one scope — computed in one place.

Four modules answered "which project is this?" independently, and they did not
agree:

    okf.project_root          $LLM_ROUTER_PROJECT_ROOT, else nearest-.git walk
    semantic_cache            sha256($LLM_ROUTER_PROJECT_DIR or os.getcwd())
    result_cache._get_db_path sha256(whatever string the caller passed)
    gateway                   header, then body, then okf.project_root

Two different environment variable names and two different fallbacks. The
divergence is not theoretical and it is not symmetrical:

  * Run anything from `src/` or `tests/` and OKF walks up to the repo root
    while the caches key on the subdirectory. One project, two cache
    namespaces, and the split is invisible — a cache miss looks like a cache
    miss.
  * `result_cache` hashes its caller's raw string into a FILE PATH, not a
    column in a TTL'd table. A divergent spelling there does not age out; it
    orphans a database file that is never reopened, so it is never purged.
  * That caller is about to become router.py handing over the MCP client's
    reported root, which is whatever the client said — no `.git` walk at all.

So `resolve_scope()` is the single resolver and `scope_key()` the single hash
of its result. These tests pin the agreement rather than the implementation:
what matters is that two callers standing in different corners of one project
land on one answer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git_repo(path: Path) -> Path:
    (path / "src" / "deep").mkdir(parents=True)
    (path / "src" / "deep" / "mod.py").write_text("def f():\n    pass\n")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    return path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _git_repo(tmp_path / "repo")
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)
    return root


def test_a_subdirectory_resolves_to_the_repo_root(repo, monkeypatch):
    from llm_router.semantic.scope import resolve_scope

    monkeypatch.chdir(repo / "src" / "deep")
    assert resolve_scope() == repo.resolve()


def test_okf_and_the_resolver_agree_from_a_subdirectory(repo, monkeypatch):
    from llm_router import okf
    from llm_router.semantic.scope import resolve_scope

    monkeypatch.chdir(repo / "src" / "deep")
    assert okf.project_root() == resolve_scope()


def test_both_caches_key_on_the_same_project_from_any_subdirectory(repo, monkeypatch):
    """The regression that matters: same project, two corners, one namespace."""
    from llm_router import result_cache, semantic_cache

    monkeypatch.chdir(repo)
    sem_at_root = semantic_cache._project_scope()
    res_at_root = result_cache._get_db_path(str(repo), "code")

    monkeypatch.chdir(repo / "src" / "deep")
    sem_at_sub = semantic_cache._project_scope()
    res_at_sub = result_cache._get_db_path(str(repo / "src" / "deep"), "code")

    assert sem_at_root == sem_at_sub, (
        "the semantic cache split one project into two namespaces by cwd"
    )
    assert res_at_root == res_at_sub, (
        f"the result cache put one project in two database files:\n"
        f"  {res_at_root}\n  {res_at_sub}\n"
        f"and neither is ever reopened to be purged"
    )


def test_the_legacy_env_var_still_works(repo, monkeypatch):
    """`LLM_ROUTER_PROJECT_DIR` was the semantic cache's spelling.

    Anyone who set it has a working configuration and must keep one; it is
    accepted, with `LLM_ROUTER_PROJECT_ROOT` winning when both are set.
    """
    from llm_router.semantic.scope import resolve_scope

    monkeypatch.chdir(Path(repo).parent)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_DIR", str(repo / "src"))
    assert resolve_scope() == repo.resolve()

    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    assert resolve_scope() == repo.resolve()


def test_an_explicit_hint_beats_the_environment(repo, tmp_path, monkeypatch):
    """A caller that named a project meant that project.

    The MCP server is long-lived with a meaningless cwd; the explicit root is
    the only signal that survives it, so the environment must not override it.
    """
    from llm_router.semantic.scope import resolve_scope

    other = _git_repo(tmp_path / "other")
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    assert resolve_scope(other / "src" / "deep") == other.resolve()


def test_two_projects_never_share_a_key(repo, tmp_path):
    from llm_router.semantic.scope import scope_key

    other = _git_repo(tmp_path / "other")
    assert scope_key(repo) != scope_key(other)


def test_the_key_is_stable_across_spellings_of_one_path(repo):
    """`repo`, `repo/`, `repo/src/deep` and `repo/./src` are one project."""
    from llm_router.semantic.scope import scope_key

    keys = {
        scope_key(repo),
        scope_key(str(repo) + "/"),
        scope_key(repo / "src" / "deep"),
        scope_key(Path(str(repo) + "/./src")),
    }
    assert len(keys) == 1, f"one project produced {len(keys)} cache namespaces: {keys}"


def test_no_module_resolves_project_scope_on_its_own():
    """The guard. A fifth private resolver is how the first four happened.

    Only a real READ counts — `environ.get`, `environ[`, `getenv`. Naming the
    variable in a docstring or listing it in `env_registry.py`'s catalogue is
    documentation, not a second answer to "which project is this?".

    `gateway.py` is exempt and should not be: it WRITES the variable per request
    and restores it afterwards, to pass scope into a call it cannot otherwise
    reach. That is a race between concurrent requests, not a resolver, and it
    needs scope threaded through `grounding` as a value. Recorded here rather
    than fixed here — see docs/decisions/0002-semantic-layer.md.
    """
    import re as _re

    src = Path(__import__("llm_router").__file__).resolve().parent
    allowed = {"semantic/scope.py", "okf.py", "env_registry.py", "gateway.py"}
    read = _re.compile(
        r"(?:environ\.get\(|environ\[|getenv\()\s*['\"]"
        r"LLM_ROUTER_PROJECT_(?:DIR|ROOT)['\"]"
    )
    offenders = []
    for path in src.rglob("*.py"):
        rel = str(path.relative_to(src))
        if rel in allowed:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if read.search(line):
                offenders.append(f"{rel}:{i}")
    assert not offenders, (
        "these read a project-scope environment variable directly instead of "
        "calling semantic.scope.resolve_scope(), which is how four modules ended "
        "up disagreeing: " + ", ".join(offenders)
    )
