"""Y: a function the question names arrives with where it is CALLED.

After X, llm("what does _append_transcript_shard write, and in which condition
is it called?") answered the first half from the injected body and guessed the
second (it quoted the function's own guard). The index already records call
sites (relation.type='call_candidate', 85K rows); they were never rendered.
Up to 3 non-test call sites, each with the lines above it (where the condition
lives); a site whose line no longer names the function (stale index) is skipped.
"""
import subprocess

import pytest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "pkg" / "ledger.py").write_text(
        "def _settle_rows(rows):\n    return {'SETTLED': sum(rows)}\n")
    (r / "pkg" / "flow.py").write_text(
        "from pkg.ledger import _settle_rows\n\n"
        "def close_day(rows, frozen):\n"
        "    if not frozen and CALLER_CONDITION_MARKER:\n"
        "        return _settle_rows(rows)\n"
        "    return None\n")
    (r / "tests" / "test_flow.py").write_text(
        "from pkg.ledger import _settle_rows\n\ndef test_it():\n    TEST_CALL_MARKER = _settle_rows([1])\n")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=r, check=True)
    from llm_router.semantic import indexer
    indexer.index(r)
    return r


def test_the_named_function_comes_with_its_call_site_and_condition(repo):
    from llm_router.context_injection import inject
    out = inject("where is _settle_rows called, and under what condition?", root=str(repo))
    assert "def _settle_rows" in out, "premise: the function itself was retrieved"
    assert "pkg/flow.py:5" in out, "the call site is missing"
    assert "CALLER_CONDITION_MARKER" in out, "the line holding the condition is missing"


def test_test_files_are_not_offered_as_call_sites(repo):
    from llm_router.context_injection import inject
    out = inject("where is _settle_rows called?", root=str(repo))
    assert "TEST_CALL_MARKER" not in out
