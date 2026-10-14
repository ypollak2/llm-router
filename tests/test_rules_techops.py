"""Bundled routing rule text for P2.1 task 1."""
from __future__ import annotations

from llm_router.install_hooks import _RULES_SRC, _rules_version


def _text() -> str:
    return (_RULES_SRC / "llm_router.md").read_text(encoding="utf-8")


def test_rule_no_longer_says_not_your_file_tools():
    assert "not your file tools" not in _text()


def test_rule_describes_condensing_conditionally():
    text = _text()
    # The switch is not shipped yet, so the rule must not name an env var that src does not read.
    assert "condensing is enabled" in text and "not shipped yet" in text and "re-run" in text
    assert "LLM_ROUTER_TECH_OPS`" not in text


def test_rules_version_bumped_so_installed_copies_update(tmp_path):
    old = tmp_path / "llm_router.md"
    old.write_text("<!-- llm_router-rules-version: 10 -->\n", encoding="utf-8")
    assert _rules_version(_RULES_SRC / "llm_router.md") > _rules_version(old)
