"""X1: a private name (leading underscore) is a retrieval seed.

2026-09-25: llm("what does _append_transcript_shard in hooks/auto-route.py
write…") got repository evidence about log_routing_decision and route_tool,
never the named function, and the model invented an env var and a condition.
The index held the function (hooks/auto-route.py:2125); `seeds_from` did not
extract it — `_SNAKE` requires a leading letter and `\\b` cannot sit between
`_` and a letter, so every `_private` name was dropped.
"""
from llm_router.semantic.retrieve import seeds_from


def test_a_private_snake_name_is_a_seed():
    idents, _ = seeds_from("what does _append_transcript_shard write?")
    assert "_append_transcript_shard" in idents, idents


def test_a_single_word_private_name_is_a_seed():
    idents, _ = seeds_from("why does _needs_web return true here, and what about _okf_inject?")
    assert "_needs_web" in idents and "_okf_inject" in idents, idents


def test_dunder_and_plain_words_do_not_become_seeds():
    idents, _ = seeds_from("the __init__ file and the _ character are not the point")
    assert "_" not in idents
    assert "the" not in idents and "character" not in idents


def test_public_names_still_work():
    idents, _ = seeds_from("what does append_transcript_shard do")
    assert "append_transcript_shard" in idents


# ── X2: a function the question NAMES comes with its code ────────────────────
# After X1 the named function ranked first, but evidence carried its signature
# only; a model asked "what does X write" had a name and no code, and guessed.

import subprocess  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "ledger.py").write_text(
        "def _settle_rows(rows):\n"
        "    total = sum(r['amount'] for r in rows)\n"
        "    return {'SETTLED_MARKER': total}\n\n"
        "def unrelated_helper(x):\n"
        "    return 'UNRELATED_BODY_MARKER'\n")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=r, check=True)
    from llm_router.semantic import indexer
    indexer.index(r)
    return r


def test_a_named_function_arrives_with_its_body(repo):
    from llm_router.context_injection import inject
    out = inject("what does _settle_rows in pkg/ledger.py return?", root=str(repo))
    assert "def _settle_rows" in out, "premise: the named function was retrieved"
    assert "SETTLED_MARKER" in out, "the body of the named function is missing"


def test_an_unnamed_neighbour_stays_a_signature(repo):
    from llm_router.context_injection import inject
    out = inject("what does _settle_rows in pkg/ledger.py return?", root=str(repo))
    assert "UNRELATED_BODY_MARKER" not in out


# ── X3: retrieval searches with the QUESTION, not the prepended knowledge ────
# inject() handed semantic retrieval the prompt AFTER the OKF knowledge block
# and repo-state were prepended; every function those blocks mention became a
# seed and their bodies crowded out the one the user asked about. Live: the
# named `_append_transcript_shard` was missing, `_configure_logging` present.

def test_prepended_knowledge_does_not_become_the_query(repo, monkeypatch):
    from llm_router import context_injection, okf
    monkeypatch.setattr(okf, "find_relevant", lambda *a, **k: ["concept"])
    monkeypatch.setattr(okf, "inject_context",
                        lambda prompt, concepts: "<knowledge_context>see unrelated_helper"
                                                 "</knowledge_context>\n" + prompt)
    out = context_injection.inject("what does _settle_rows in pkg/ledger.py return?",
                                   root=str(repo))
    assert "see unrelated_helper" in out, "premise: the knowledge block was prepended"
    assert "SETTLED_MARKER" in out
    assert "UNRELATED_BODY_MARKER" not in out, "a name from the knowledge block became a seed"
