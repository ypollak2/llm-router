"""The ONE place where every limit of the commit_message / pr_description jobs is stated (ported from the frozen PREREG-Q37 rubric).

The model-facing prompts are rendered from ``LIMITS``; ``grader.py`` enforces exactly ``LIMITS`` (nothing else is a pass/fail rule); a judge is told
that format and length are checked elsewhere and must never fail an answer for them. A limit that is not in ``LIMITS`` is not enforced by anything.
"""
from __future__ import annotations

LIMITS: dict[str, dict[str, int]] = {
    "commit_message": {"subject_max_chars": 72, "body_max_words": 150},
    "pr_description": {"summary_min_words": 8, "summary_max_words": 120, "plan_min_bullets": 1, "plan_max_bullets": 8},
}

_L = LIMITS


def commit_prompt(diff: str) -> str:
    c = _L["commit_message"]
    return ("Write a git commit message for the diff below. Output ONLY the message, with no preamble and no code fences. "
            f"Format: a subject line in the imperative mood of at most {c['subject_max_chars']} characters; if you add a body, leave a blank line after the subject "
            f"and keep the body to at most {c['body_max_words']} words. "
            "Mention only files, functions, flags and other identifiers that appear in the diff, and do not claim that tests were run or passed.\n\n" + diff)


def pr_prompt(title: str, diff: str) -> str:
    p = _L["pr_description"]
    return (f"Write a pull request description for the diff below. The PR title is: {title}\n"
            "Output ONLY the description, with no preamble. Use exactly two markdown sections: "
            f"'## Summary' (what changed and why, at most {p['summary_max_words']} words) and "
            f"'## Test plan' (a bulleted list of {p['plan_min_bullets']} to {p['plan_max_bullets']} steps to verify the change). "
            "Mention only files, functions, flags and other identifiers that appear in the diff, and do not claim that tests were run or passed.\n\n" + diff)
