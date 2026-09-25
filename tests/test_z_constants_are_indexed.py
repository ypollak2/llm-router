"""Z: module-level constants are indexed, so their values can be retrieved.

A 14-question pilot (2026-09-25, qwen3-coder with ci.inject context): 6/14
correct; most failures asked for a constant's value — `_MIN_SCORE_DEFAULT = 2`,
the `_DEFAULTS = {...}` semantic modes, `SENTINEL_OPEN` — and the index held
only functions (15,268) and classes (1,387). The model got a neighbouring
function instead and answered wrongly (one default backwards).
"""
import sqlite3
import subprocess

import pytest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "okf.py").write_text(
        "_MIN_SCORE_DEFAULT = 2\n"
        "_DEFAULTS = {\n    'SOURCE': 'on',\n    'HISTORY': 'off',\n}\n"
        "SENTINEL_OPEN: str = '<<llm_router>>'\n\n"
        "def _min_score(raw):\n    return max(1, int(raw or _MIN_SCORE_DEFAULT))\n")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=r, check=True)
    from llm_router.semantic import indexer
    indexer.index(r)
    return r


@pytest.mark.parametrize("question,needle", [
    ("what is the default value of _MIN_SCORE_DEFAULT?", "_MIN_SCORE_DEFAULT = 2"),
    ("what does _DEFAULTS set SOURCE to?", "'SOURCE': 'on'"),
    ("which file defines SENTINEL_OPEN?", "SENTINEL_OPEN: str = '<<llm_router>>'"),
])
def test_a_named_constant_arrives_with_its_value(repo, question, needle):
    from llm_router.context_injection import inject
    out = inject(question, root=str(repo))
    assert needle in out, out[-800:]


def test_an_index_built_before_constants_existed_is_re_extracted(repo):
    from llm_router.semantic import indexer, store
    con = sqlite3.connect(store.index_path(root=repo))
    con.execute("DELETE FROM entity WHERE kind = 'constant'")
    con.execute("DELETE FROM meta WHERE key = 'extractor_version'")
    con.commit()
    assert not con.execute("SELECT 1 FROM entity WHERE kind='constant'").fetchall(), "premise"
    con.close()
    indexer.index(repo)
    con = sqlite3.connect(store.index_path(root=repo))
    assert con.execute("SELECT count(*) FROM entity WHERE kind='constant'").fetchone()[0] == 3
