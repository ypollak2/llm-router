"""CHZ-AUD-007 regression: README enforcement-mode default must match code default.

F01/North Star: enforce_config.DEFAULT_ENFORCE is 'smart' — enforce routing out of
the box so offloadable work goes to cheaper models. This test keeps the README's
documented default in lockstep with the code default.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from llm_router.enforce_config import DEFAULT_ENFORCE  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
README = REPO_ROOT / "README.md"
# The full config table + enforcement ladder moved to the linked configuration
# reference in v1.0.0 (README is the lean landing page). The honesty gate scans the
# whole user-facing doc corpus — README plus the pages it prominently links — so the
# default-consistency guarantee holds wherever the table canonically lives.
_DOC_FILES = [README, REPO_ROOT / "Docs" / "configuration.md"]


def _doc_corpus() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _DOC_FILES if p.exists())


def _readme_enforce_table_default() -> str | None:
    """Extract the value from the LLM_ROUTER_ENFORCE row in the config reference table."""
    match = re.search(r"\|\s*`LLM_ROUTER_ENFORCE`\s*\|\s*`([^`]+)`\s*\|", _doc_corpus())
    return match.group(1).strip() if match else None


def _readme_enforce_mode_section_default() -> str | None:
    """Return the mode marked '(default)' in the Enforcement Modes table, if any."""
    match = re.search(r"\|\s*`([^`]+)`\s*\(\*\*?default\*\*?\)", _doc_corpus())
    return match.group(1).strip() if match else None


class TestEnforceDefaultConsistency:
    """The README must document the same default as enforce_config.DEFAULT_ENFORCE."""

    def test_enforce_config_default_is_smart(self) -> None:
        assert DEFAULT_ENFORCE == "smart", (
            f"enforce_config.DEFAULT_ENFORCE is '{DEFAULT_ENFORCE}', expected 'smart' "
            "(F01/North Star). If the default changed, update this test AND README together."
        )

    def test_readme_config_table_matches_code_default(self) -> None:
        readme_default = _readme_enforce_table_default()
        assert readme_default is not None, (
            "Could not find LLM_ROUTER_ENFORCE row in README config table."
        )
        assert readme_default == DEFAULT_ENFORCE, (
            f"README config table says LLM_ROUTER_ENFORCE default is '{readme_default}' "
            f"but code default is '{DEFAULT_ENFORCE}'. Update README.md so they agree."
        )

    def test_readme_enforcement_modes_section_default_marker(self) -> None:
        mode_section_default = _readme_enforce_mode_section_default()
        if mode_section_default is None:
            return  # no '(default)' marker is acceptable if the table row is right
        assert mode_section_default == DEFAULT_ENFORCE, (
            f"Enforcement Modes table marks '{mode_section_default}' as (default), "
            f"but code default is '{DEFAULT_ENFORCE}'. Move the marker to the '{DEFAULT_ENFORCE}' row."
        )

    def test_readme_does_not_show_soft_as_enforce_default(self) -> None:
        """'soft' must not be marked (default) — 'smart' is the real default now."""
        text = README.read_text(encoding="utf-8")
        assert "`soft` (default)" not in text, (
            "README marks '`soft` (default)', but the code ships 'smart' as the default."
        )
