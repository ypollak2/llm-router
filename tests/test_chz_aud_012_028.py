"""CHZ-AUD-012 / CHZ-AUD-028 regression: README must accurately describe the hook mechanism.

The UserPromptSubmit hook injects an ADVISORY hint via additionalContext — it does NOT
guarantee another model executes the request.  Real execution only happens when:
  - DIRECT execution path fires (LLM_ROUTER_DIRECT_EXECUTION=true + self-contained prompt), OR
  - An actual llm_* MCP tool is called by Claude.

Any README / docs claim that implies "every prompt is intercepted/executed by another model"
or "routing is enforced / guaranteed on the advisory path" is false and must be corrected.

This test suite scans README.md for forbidden phrasing and asserts required clarifying
language is present.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
README = REPO_ROOT / "README.md"
# The hook-mechanism detail (enforcement ladder, direct-execution mode, push/pull
# guarantees) moved to the linked reference docs in v1.0.0. This honesty gate scans the
# user-facing doc corpus — README plus the pages it prominently links — so forbidden
# "guaranteed interception" phrasing is banned across ALL of them and the required
# clarifying language must exist somewhere a user is routed to.
_DOC_FILES = [
    README,
    REPO_ROOT / "Docs" / "configuration.md",
    REPO_ROOT / "Docs" / "ide-setup.md",
]

# ---------------------------------------------------------------------------
# Forbidden patterns: phrases that imply guaranteed non-Claude execution on
# the advisory (non-DIRECT) hook path.  These must NOT appear in the README.
# ---------------------------------------------------------------------------
FORBIDDEN_PATTERNS = [
    # The original false claim
    (
        r"routing is automatic and guaranteed",
        "Phrase 'routing is automatic and guaranteed' implies the advisory hook "
        "forces another model to execute — only the DIRECT path does that.",
    ),
]

# ---------------------------------------------------------------------------
# Required patterns: the README MUST contain clarifying language that
# distinguishes DIRECT (real execution) from advisory (hint-only).
# ---------------------------------------------------------------------------
REQUIRED_PATTERNS = [
    (
        r"(?:advisory|hint.only|additionalContext|suggestion.only|not.guaranteed|does not guarantee|no guarantee)",
        "README must contain clarifying language that the hook emits advisory hints, "
        "not guaranteed model execution, on the non-DIRECT path.",
    ),
    (
        r"DIRECT",
        "README must explain the DIRECT path as the mechanism that truly bypasses Claude.",
    ),
]


def _readme_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _DOC_FILES if p.exists())


class TestHookMechanismDocumentation:
    """README must distinguish advisory hints from guaranteed model execution."""

    def test_push_routing_guarantee_claim_is_qualified(self) -> None:
        """The push-routing section may use 'guaranteed' only if qualified to self-contained prompts.

        The FAQ previously said 'Push routing ... routing is automatic and guaranteed'
        without qualification, misleading users into thinking ALL prompts bypass Claude.
        Acceptable forms: 'guaranteed for self-contained prompts', 'guaranteed cost savings
        on every turn (self-contained)' etc.  This test ensures plain unqualified
        'automatic and guaranteed' does not appear on the hook-mechanism path.
        """
        text = _readme_text()
        # Look for the problematic unqualified claim in the FAQ answer specifically
        # (not the general sentence about self-contained turns which is fine)
        faq_match = re.search(
            r"Push routing.*?routing is automatic and guaranteed",
            text,
            re.DOTALL,
        )
        assert faq_match is None, (
            "README FAQ still contains 'Push routing ... routing is automatic and guaranteed' "
            "without qualification. The UserPromptSubmit hook only guarantees execution bypass "
            "for self-contained prompts on the DIRECT path. Advisory/hint mode does not "
            "guarantee another model runs. Add a qualifier such as '(self-contained prompts only)'."
        )

    def test_readme_clarifies_advisory_vs_direct(self) -> None:
        """README must explain that the hook emits advisory hints, not guaranteed executions."""
        text = _readme_text()
        has_clarification = any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern, _ in REQUIRED_PATTERNS
        )
        assert has_clarification, (
            "README does not explain the difference between advisory hints (additionalContext) "
            "and the DIRECT execution path. Add a section clarifying that in advise/suggest/soft "
            "modes the hook emits a suggestion only — Claude may still answer directly."
        )

    def test_readme_does_not_contain_forbidden_guaranteed_interception_claim(self) -> None:
        """None of the forbidden 'guaranteed interception' phrases appear in README."""
        text = _readme_text()
        for pattern, message in FORBIDDEN_PATTERNS:
            assert not re.search(pattern, text, re.IGNORECASE), (
                f"README contains forbidden phrase matching '{pattern}'. {message}"
            )

    def test_readme_enforcement_modes_note_advisory_nature(self) -> None:
        """The Enforcement Modes section must mention that advise/soft/suggest never block."""
        text = _readme_text()
        # The section must exist and mention that advise/soft never forces compliance
        enforcement_section = re.search(
            r"## Enforcement Modes(.+?)(?=\n## |\Z)",
            text,
            re.DOTALL,
        )
        assert enforcement_section is not None, (
            "README missing '## Enforcement Modes' section."
        )
        section_text = enforcement_section.group(1)
        has_never_blocks = re.search(
            r"never block|never blocks|log.only|no.*block|hint.*advisory",
            section_text,
            re.IGNORECASE,
        )
        assert has_never_blocks is not None, (
            "Enforcement Modes section does not mention that soft/advise modes never block. "
            "Add a note distinguishing advisory (never-block) from hard/strict (actually blocks)."
        )

    def test_readme_direct_execution_section_exists_and_qualifies_block_guarantee(self) -> None:
        """Direct execution section must clarify when Claude is *actually* skipped."""
        text = _readme_text()
        # The direct execution section should mention self-contained vs context-dependent
        direct_section = re.search(
            r"#+\s+Direct execution mode(.+?)(?=\n###|\n##|\Z)",
            text,
            re.DOTALL,
        )
        assert direct_section is not None, (
            "README missing 'Direct execution mode' sub-section. "
            "It must explain when Claude is truly skipped (self-contained prompts only)."
        )
        section_text = direct_section.group(1)
        # Must mention that context-dependent prompts still go through Claude
        qualifies_context_dependent = re.search(
            r"context.dependent|advisory only|Claude.*handle|still.*Claude|Claude still",
            section_text,
            re.IGNORECASE,
        )
        assert qualifies_context_dependent is not None, (
            "Direct execution mode section does not clarify that context-dependent prompts "
            "still go through Claude (advisory path). Add this distinction."
        )
