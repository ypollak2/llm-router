"""A question can name the VALUE of a constant, not its name.

`FROZEN_IN_GROUND_TRUTH` is the string value of `FROZEN = "FROZEN_IN_GROUND_TRUTH"`;
the AST index knows the entity as `FROZEN`, so a lookup by the value found
nothing. The fix reads the constant's stored first line (its signature) for an
exact quoted match, for underscore-shaped identifiers only.

The precision tests matter more than the recall one. A free-text search (git
grep) was tried first and attached repository text to 14 of 33 generic
non-repo questions, because env-var and snake_case names occur in any repo.
Text merely appearing in the repo must NOT make a question about the repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.semantic import indexer as ix
from llm_router.semantic import pack as spack
from llm_router.semantic import retrieve as sretrieve
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


@pytest.fixture
def project(tmp_path: Path):
    files = {
        "pool.py": (
            "# lifecycle: CAPTURED -> ELIGIBLE -> FROZEN_IN_GROUND_TRUTH\n"
            "\n\n"
            "FROZEN = \"FROZEN_IN_GROUND_TRUTH\"\n"
            "ENV_FLAG = 'LLM_ROUTER_FLAG'\n"
            "LABEL = \"a longer string mentioning FROZEN_IN_GROUND_TRUTH inside\"\n"
        ),
        "util.py": "def log_it(msg):\n    return msg\n",
        # Env vars and snake_case words that appear in text, never as a
        # constant's value: the shape of every false positive found by grep.
        "cfg.py": ("import os\n\n\ndef db():\n"
                   "    return os.environ.get(\"DATABASE_URL\"), 'max_retries'\n"),
        "NOTES.md": "DATABASE_URL and user_id are documented here.\n",
    }
    repo = _repo(tmp_path / "repo", files)
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    return repo, base


def test_a_constants_string_value_resolves_to_the_constant(project):
    repo, base = project
    assert sstore.find_definitions("FROZEN_IN_GROUND_TRUTH", root=repo, base=base) == []

    result = sretrieve.retrieve(
        "what does the FROZEN_IN_GROUND_TRUTH state mean", root=repo, base=base)

    assert [(e.relative_path, e.name, e.kind) for e in result.entities] == [
        ("pool.py", "FROZEN", "constant")], result.entities


def test_single_quoted_values_resolve_too(project):
    repo, base = project
    result = sretrieve.retrieve("where is LLM_ROUTER_FLAG read", root=repo, base=base)
    assert [e.name for e in result.entities] == ["ENV_FLAG"]


def test_a_value_found_by_constant_goes_ahead_of_other_matches(project):
    """In a long prompt many identifiers match something; the value the
    question is about must not be cut by the result limit."""
    repo, base = project
    result = sretrieve.retrieve(
        "log_it and FROZEN_IN_GROUND_TRUTH", root=repo, base=base, limit=1)
    assert [e.name for e in result.entities] == ["FROZEN"]


def test_a_name_inside_a_longer_string_is_not_a_value_match(project):
    """`LABEL` merely mentions the token; only an exact quoted literal counts."""
    repo, base = project
    result = sretrieve.retrieve("FROZEN_IN_GROUND_TRUTH", root=repo, base=base)
    assert "LABEL" not in [e.name for e in result.entities]


@pytest.mark.parametrize("prompt", [
    "what's the right value for DATABASE_URL in docker-compose",
    "what does user_id map to in a typical JWT payload",
    "how should I set max_retries for an http client",
])
def test_generic_questions_naming_text_in_the_repo_get_nothing(project, prompt):
    """The precision pin. These tokens ARE in the repo's text (cfg.py, NOTES.md)
    but are not a constant's value, and the question is not about this repo."""
    repo, base = project
    result = sretrieve.retrieve(prompt, root=repo, base=base)
    assert result.entities == [], result.entities


def test_acronyms_and_product_names_never_reach_the_value_lookup(project):
    repo, base = project
    (repo / "k.py").write_text('SDK_NAME = "SDK"\nAPI = "API"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    ix.index(root=repo, base=base)
    q = "what's the best API for the GitHub SDK in my app"
    idents, _ = sretrieve.seeds_from(q)
    assert {"API", "SDK", "GitHub"} <= set(idents), "premise: these are seeds"

    conn = sstore.connect(repo, base)
    try:
        assert sretrieve._constants_holding_value(conn, idents, set()) == []
    finally:
        conn.close()


def test_the_value_reaches_the_rendered_pack(project):
    """End to end through pack.build: the model is shown the file and the name."""
    repo, base = project
    built = spack.build("what does the FROZEN_IN_GROUND_TRUTH state mean",
                        root=repo, base=base)
    rendered = spack.render(built)
    assert "pool.py" in rendered and "FROZEN" in rendered
    assert "FROZEN_IN_GROUND_TRUTH" in rendered


def test_a_real_definition_is_unchanged(project):
    repo, base = project
    result = sretrieve.retrieve("where is log_it defined", root=repo, base=base)
    assert [(e.kind, e.name) for e in result.entities] == [("function", "log_it")]


def test_a_shaped_seed_that_truly_does_not_exist_stays_empty(project):
    repo, base = project
    result = sretrieve.retrieve("where is nonexistent_symbol_xyz defined",
                                root=repo, base=base)
    assert result.entities == []
    assert result.status == "ok"
    assert result.note == "no entity matched the seeds"


def test_value_hits_are_bounded(tmp_path):
    files = {f"m{i}.py": f'C{i} = "SHARED_VALUE_TOKEN"\n' for i in range(12)}
    repo = _repo(tmp_path / "repo", files)
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    result = sretrieve.retrieve("explain SHARED_VALUE_TOKEN", root=repo, base=base)
    assert 0 < len(result.entities) <= sretrieve._VALUE_MAX_PER_IDENT
