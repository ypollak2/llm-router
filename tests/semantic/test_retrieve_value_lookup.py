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

import ast
import re
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


# --- the collision class -----------------------------------------------------
#
# A lowercase constant value is vocabulary. These are string values of real
# constants in this repository (budget_key.py, budget_lineage_reconciliation.py)
# and also ordinary words in a generic question. A review found 3 of 9 prompts
# built that way got a repository constant attached by the first version of the
# value lookup, which accepted any underscore-shaped token.

_COLLISION_PROMPTS = [
    "how do I pick a session scope like agent_session in a generic app",
    "how do you name an unregistered_parent row in a ledger schema",
    "what does underdebited_parent mean in accounting software",
]


@pytest.fixture
def collision_project(tmp_path: Path):
    repo = _repo(tmp_path / "repo", {
        "budget_key.py": 'SCOPE_AGENT_SESSION = "agent_session"\n',
        "recon.py": ('UNREGISTERED = "unregistered_parent"\n'
                     'UNDERDEBITED = "underdebited_parent"\n'),
    })
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    return repo, base


@pytest.mark.parametrize("prompt", _COLLISION_PROMPTS)
def test_a_lowercase_constant_value_in_a_generic_question_attaches_nothing(
        collision_project, prompt):
    repo, base = collision_project
    result = sretrieve.retrieve(prompt, root=repo, base=base)
    assert result.entities == [], [(e.relative_path, e.name) for e in result.entities]


def test_a_single_underscore_upper_token_is_not_a_value_lookup(tmp_path):
    repo = _repo(tmp_path / "repo", {"c.py": 'ENV_FAKE = "FAKE_KEY"\n'})
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    result = sretrieve.retrieve("how do I rotate a FAKE_KEY", root=repo, base=base)
    assert result.entities == []


_SRC = Path(__file__).resolve().parents[2] / "src" / "llm_router"
_WORDLIKE = re.compile(r"[a-z]+(?:_[a-z0-9]+)+")
_TEMPLATES = [
    "how do I name a column like {x} in a generic database schema",
    "what does {x} usually mean in accounting software",
    "is it a good idea to use {x} as a status value in a web app",
    "how do I pick a {x} setting in a generic app",
    "explain {x} to a new developer on any project",
    "what is the best way to represent {x} in a REST API",
]


def _lowercase_constant_values() -> list[str]:
    """Every module-level `NAME = "snake_case"` string in the package: the
    population this lookup collides with, mined live so it grows with the repo."""
    values: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and _WORDLIKE.fullmatch(node.value.value)
                    and all(isinstance(t, ast.Name) for t in node.targets)):
                values.add(node.value.value)
    return sorted(values)


def test_no_lowercase_constant_value_in_the_package_attaches_to_a_generic_question(
        tmp_path):
    values = _lowercase_constant_values()
    # An empty population passes everything; fail loudly if the mining broke.
    assert len(values) >= 30, f"only {len(values)} values mined from {_SRC}"

    body = "".join(f'C{i} = "{v}"\n' for i, v in enumerate(values))
    repo = _repo(tmp_path / "repo", {"consts.py": body})
    base = tmp_path / "store"
    ix.index(root=repo, base=base)

    hits = []
    for i, value in enumerate(values):
        prompt = _TEMPLATES[i % len(_TEMPLATES)].format(x=value)
        result = sretrieve.retrieve(prompt, root=repo, base=base)
        if result.entities:
            hits.append((prompt, [e.name for e in result.entities]))
    assert hits == [], f"{len(hits)}/{len(values)} generic prompts got attachments: {hits[:5]}"
