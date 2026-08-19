"""Regression: RED1-9-02 — _remove_toml_table_block must not swallow adjacent
tables that are not blank-line-separated (valid TOML)."""
from llm_router.commands.install import _remove_toml_table_block


def test_adjacent_table_no_blank_line_preserved():
    txt = '[model_providers.llm_router]\nname = "C"\nbase_url = "u"\n[other]\nk = 1\n'
    out = _remove_toml_table_block(txt, "model_providers.llm_router")
    assert "[model_providers.llm_router]" not in out
    assert "[other]" in out and "k = 1" in out


def test_table_between_two_others_preserved():
    txt = '[a]\nx=1\n[model_providers.llm_router]\nname="C"\n[b]\ny=2\n'
    out = _remove_toml_table_block(txt, "model_providers.llm_router")
    assert "[model_providers.llm_router]" not in out
    assert "[a]" in out and "x=1" in out
    assert "[b]" in out and "y=2" in out


def test_blank_line_separated_still_works():
    txt = '[model_providers.llm_router]\nname="C"\n\n[b]\ny=2\n'
    out = _remove_toml_table_block(txt, "model_providers.llm_router")
    assert "[model_providers.llm_router]" not in out and "[b]" in out
