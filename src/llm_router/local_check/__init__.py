"""local-check: measure whether a user's local model is good enough for self-contained text jobs (commit_message, pr_description).

This package currently holds the pure, deterministic pieces: ``rubric`` (limits + prompt rendering), ``grader`` (programmatic grading),
``stats`` (Wilson / bar helpers) and ``grader_cases`` (self-test cases). No I/O at import; no routing or CLI hooks.
"""
