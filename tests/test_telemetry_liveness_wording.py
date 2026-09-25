"""`success_rate` must be documented as LIVENESS, not quality.

CHZ-JUDGE-QUEUE wording fix. `success_rate` (non-empty, non-refusal) was
described in `ModelStats`' docstring as "quality (``success_rate``)", which
this repo's own T-09 finding already flagged as wrong: a confident, fluent,
entirely wrong answer scores 1.0. This test pins the corrected name.

Asserts on the actual `__doc__` attribute rather than a repo-wide grep of
telemetry.py's source text — a stray comment elsewhere in the file cannot
satisfy an attribute check the way a plain string search could (see this
repo's CLAUDE.md, "A remediation is not done until its red-check uses the
NARROWEST mutation" / A-10: comments are not in the AST, and `__doc__` is
populated only from the real docstring, never from a `#` comment).
"""

from __future__ import annotations

from llm_router.telemetry import ModelStats


def test_model_stats_docstring_names_success_rate_liveness():
    doc = ModelStats.__doc__ or ""
    assert "liveness" in doc.lower(), (
        "ModelStats' docstring must name what success_rate actually measures "
        f"— liveness — not just describe it. Got: {doc!r}"
    )


def test_model_stats_docstring_does_not_call_success_rate_quality_unqualified():
    """The docstring may still discuss quality in general (judge_mean IS a
    real quality measurement) — it must specifically stop calling
    success_rate itself "quality"."""
    doc = ModelStats.__doc__ or ""
    assert "quality (``success_rate``)" not in doc, (
        "success_rate must not be directly labeled 'quality' in the class "
        f"docstring — it measures liveness, not correctness. Got: {doc!r}"
    )
