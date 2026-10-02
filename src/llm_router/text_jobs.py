"""Self-contained, checkable text jobs: commit messages, PR descriptions, and
long tool-output summaries, built to be served by a local model with a Claude
fallback on failure.

Why these three (owner request 2026-10-02, repo audit of ~/.llm-router's real
ledgers): a local model's measured failure mode on this machine is guessing
facts it cannot see — context-dependent questions it answers wrong 3-24% of
the time. A job whose entire input is handed to it in the prompt (a diff, a
commit list, a log) cannot suffer that failure mode the same way, which is why
these three are in scope and open-ended "answer this question about the repo"
work is not.

Docstrings were evaluated and deliberately left out of this module: a
docstring update is edit-shaped work on a known file — exactly what
``llm_edit`` (``edit.py``) already does, with the same exact-match-and-syntax-
check machinery this module reimplements for free-text output. Building a
second mechanism for it would be two implementations of one idea.

This module is pure logic — prompt construction and acceptance checking, no
network calls, no ledger writes — so it can be tested without mocking an LLM
or touching disk. ``llm_text_job`` in ``tools/text.py`` is the thin MCP-tool
wrapper that drives the retry loop and records outcomes, mirroring
``llm_edit``'s shape in ``edit.py``.

Acceptance is deliberately a cheap, regex-based check run on the ORIGINAL
input, not a second model call grading the first — the same reasoning
``edit.py``'s ``check_syntax`` and ``tools/local_task.py``'s acceptance-check
docstring both give: the thing that did the work does not get to grade it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Job identifiers this module understands.
JOB_COMMIT_MESSAGE = "commit_message"
JOB_PR_DESCRIPTION = "pr_description"
JOB_SUMMARIZE_OUTPUT = "summarize_output"

JOB_NAMES = (JOB_COMMIT_MESSAGE, JOB_PR_DESCRIPTION, JOB_SUMMARIZE_OUTPUT)

#: Cap on input handed to the model. Mirrors edit.py's _MAX_FILE_BYTES: large
#: inputs are truncated with a note rather than sent whole or refused outright.
MAX_INPUT_CHARS = 40_000

#: Initial attempt plus retries-with-feedback, same ceiling as edit.py.
MAX_JOB_ATTEMPTS = 3

_PLACEHOLDER_SUBJECTS = frozenset({
    "wip", "update", "updates", "fix", "fixes", "changes", "misc", "stuff",
    "cleanup", "minor changes", "fix bug", "various fixes", "tweaks", ".",
})

_DIFF_PATH_RE = re.compile(r"(?m)^diff --git a/(\S+) b/(\S+)")
_DIFF_NEWPATH_RE = re.compile(r"(?m)^\+\+\+ b/(\S+)")
_BARE_FILENAME_RE = re.compile(r"\b[\w./-]+\.\w{1,5}\b")

_ERROR_KEYWORD_RE = re.compile(r"(?i)\b(error|exception|traceback|failed|failure)\b")
#: "0 failed" / "0 errors" is a PASSING result, not a signal. Masked out before
#: `_ERROR_KEYWORD_RE` runs so a clean "N passed, 0 failed" summary isn't forced
#: to fabricate error language just to satisfy the checker (CHZ-AUD false-reject
#: found by an independent reviewer on 2026-10-02: a correct, error-free summary
#: of such a log was rejected 3/3 attempts because "failed" is a substring of it).
_ZERO_FAILURE_RE = re.compile(r"(?i)\b0\s+(?:failed|failures?|errors?)\b")
_FILE_LINE_RE = re.compile(
    r"[\w./\\-]+\.(?:py|js|ts|tsx|go|rs|java|rb|c|cpp|cc|hpp|h|cs|php|swift|sql|sh|kt|scala|"
    r"yaml|yml|json|toml):\d+"
)
#: Widened from `\D{0,3}` (reviewer repro: "exited with code 137" needs ~12 chars
#: of non-digit slack between the keyword and the number; the tight version
#: silently dropped that whole phrasing, including from the raw side, which let
#: a summary drop it unnoticed).
_EXIT_CODE_RE = re.compile(r"(?i)\b(?:exit(?:ed)?\s*(?:code|status)?|returncode)\D{0,16}?(\d+)\b")


@dataclass
class TextJobResult:
    """Outcome of one ``run_text_job`` attempt (one model call + one check).

    Attributes:
        text: The model's raw output for this attempt.
        ok: Whether the acceptance check passed.
        reason: Why the check failed, or ``None`` if it passed.
    """

    text: str
    ok: bool
    reason: str | None = None


@dataclass
class TextJobOutcome:
    """The decided result of the whole retry loop.

    Attributes:
        text: The final (accepted, or last-rejected for diagnosis) text.
        accepted: True iff some attempt passed its acceptance check.
        reasons: One entry per rejected attempt, oldest first.
        attempts: How many model calls were made.
    """

    text: str
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    attempts: int = 0


def truncate_input(text: str, max_chars: int = MAX_INPUT_CHARS) -> tuple[str, bool]:
    """Cap *text* at *max_chars*. Returns ``(text, truncated)``."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def changed_files(diff_text: str) -> list[str]:
    """File paths touched by a unified diff. Best-effort: an unparseable or
    empty diff yields an empty list rather than raising."""
    paths = [m.group(2) for m in _DIFF_PATH_RE.finditer(diff_text)]
    if not paths:
        paths = [m.group(1) for m in _DIFF_NEWPATH_RE.finditer(diff_text)]
    return [p for p in paths if p and p != "/dev/null"]


def _grounding_tokens(reference_text: str) -> set[str]:
    """Lowercase file-stem tokens a generated message should plausibly
    mention: diff paths first, then any bare ``name.ext``-shaped tokens in
    the reference text (covers a plain commit-subject list with no diff)."""
    files = changed_files(reference_text)
    if not files:
        files = _BARE_FILENAME_RE.findall(reference_text)
    return {Path(f).stem.lower() for f in files if Path(f).stem}


def _mentions_any_stem(text: str, stems: set[str]) -> bool:
    """Word-boundary match, not substring: a bare ``in`` check let the stem
    ``os`` (from ``os.py``) pass on the word "acr-OS-s", crediting a message
    with grounding it never had (found by an independent reviewer, 2026-10-02).
    Short/common stems can still coincidentally match a real word at a
    boundary; that residual risk is accepted, the "os" inside "across" is not."""
    low = text.lower()
    return any(re.search(rf"\b{re.escape(stem)}\b", low) for stem in stems)


def build_commit_message_prompt(diff_text: str, *, hint: str | None = None,
                                 feedback: str | None = None) -> tuple[str, str]:
    """Returns ``(system_prompt, user_prompt)`` for a commit-message job."""
    system_prompt = (
        "You write git commit messages. Return ONLY the commit message text — "
        "no prose before or after, no markdown fences. First line is a concise "
        "subject under 72 characters; a blank line and body may follow if useful."
    )
    prompt = f"## Diff\n```diff\n{diff_text}\n```"
    if hint:
        prompt += f"\n\n## Context\n{hint}"
    if feedback:
        prompt += f"\n\n## Your previous answer was rejected\n{feedback}\nAnswer again, fixing that."
    return system_prompt, prompt


def check_commit_message(diff_text: str, candidate: str) -> TextJobResult:
    """Acceptance check: non-placeholder, length-bounded, and grounded in a
    file the diff actually touched."""
    stripped = candidate.strip()
    reasons: list[str] = []
    first_line = stripped.splitlines()[0].strip() if stripped else ""

    if not first_line:
        reasons.append("empty commit message")
    if len(first_line) > 100:
        reasons.append(f"subject line too long ({len(first_line)} chars, max 100)")
    if first_line.rstrip(".").lower() in _PLACEHOLDER_SUBJECTS:
        reasons.append(f"generic placeholder subject: {first_line!r}")

    stems = _grounding_tokens(diff_text)
    if not stems:
        reasons.append("could not identify any changed file from the diff; cannot verify groundedness")
    elif not _mentions_any_stem(stripped, stems):
        reasons.append("message does not mention any changed file")

    return TextJobResult(text=candidate, ok=not reasons, reason="; ".join(reasons) or None)


def build_pr_description_prompt(context_text: str, *, hint: str | None = None,
                                 feedback: str | None = None) -> tuple[str, str]:
    """Returns ``(system_prompt, user_prompt)`` for a PR-description job.

    ``context_text`` is expected to be self-contained: commit subjects and/or
    a diff/diffstat — everything the model needs is in this one string.
    """
    system_prompt = (
        "You write pull request descriptions. Return ONLY the PR body in "
        "markdown — no prose outside it. It MUST contain a '## Summary' "
        "section and a '## Test plan' section."
    )
    prompt = f"## Commits and changes\n{context_text}"
    if hint:
        prompt += f"\n\n## Context\n{hint}"
    if feedback:
        prompt += f"\n\n## Your previous answer was rejected\n{feedback}\nAnswer again, fixing that."
    return system_prompt, prompt


def check_pr_description(context_text: str, candidate: str) -> TextJobResult:
    """Acceptance check: has a Summary and a Test plan section, is non-trivial
    in length, and references at least one changed file or commit subject."""
    stripped = candidate.strip()
    reasons: list[str] = []

    if len(stripped) < 40:
        reasons.append(f"too short ({len(stripped)} chars, need >= 40)")
    if not re.search(r"(?im)^\s*#{0,3}\s*summary\b", stripped):
        reasons.append("missing a Summary section")
    if not re.search(r"(?im)test\s*plan|testing|how to test", stripped):
        reasons.append("missing a test plan / testing section")

    stems = _grounding_tokens(context_text)
    if stems and not _mentions_any_stem(stripped, stems):
        reasons.append("description does not reference any changed file or commit")

    return TextJobResult(text=candidate, ok=not reasons, reason="; ".join(reasons) or None)


def build_summarize_output_prompt(raw_output: str, *, hint: str | None = None,
                                   feedback: str | None = None) -> tuple[str, str]:
    """Returns ``(system_prompt, user_prompt)`` for a log/test-output summary job."""
    system_prompt = (
        "You summarize command output (test runs, build logs, CI output). "
        "Return ONLY the summary. You MUST preserve every error/exception line, "
        "every file:line reference, and every exit/return code mentioned in the "
        "input — verbatim or near-verbatim — even while shortening everything else."
    )
    prompt = f"## Command output\n```\n{raw_output}\n```"
    if hint:
        prompt += f"\n\n## Context\n{hint}"
    if feedback:
        prompt += f"\n\n## Your previous answer was rejected\n{feedback}\nAnswer again, fixing that."
    return system_prompt, prompt


def _critical_signals(text: str) -> tuple[set[str], set[str], bool]:
    """``(file:line refs, exit-code mentions, has an error/failure keyword)``."""
    file_lines = set(_FILE_LINE_RE.findall(text))
    exit_codes = {m.group(0) for m in _EXIT_CODE_RE.finditer(text)}
    # "0 failed" / "0 errors" is a passing result, not an error signal — mask it
    # out before the keyword scan so a correct "N passed, 0 failed" summary
    # isn't forced to invent error language it shouldn't contain.
    scrubbed = _ZERO_FAILURE_RE.sub("", text)
    has_error_kw = bool(_ERROR_KEYWORD_RE.search(scrubbed))
    return file_lines, exit_codes, has_error_kw


def check_summarize_output(raw_output: str, candidate: str) -> TextJobResult:
    """Acceptance check: every file:line ref and exit-code mention in the raw
    output must still appear in the summary, and if the raw output signals an
    error/failure, the summary must say so too."""
    stripped = candidate.strip()
    reasons: list[str] = []

    if len(stripped) < 20:
        reasons.append(f"summary too short to be useful ({len(stripped)} chars)")

    raw_files, raw_codes, raw_has_error = _critical_signals(raw_output)
    cand_files, cand_codes, cand_has_error = _critical_signals(stripped)

    missing_files = raw_files - cand_files
    if missing_files:
        reasons.append(f"dropped file:line references: {sorted(missing_files)[:5]}")

    missing_codes = raw_codes - cand_codes
    if missing_codes:
        reasons.append(f"dropped exit-code mentions: {sorted(missing_codes)[:5]}")

    if raw_has_error and not cand_has_error:
        reasons.append("raw output mentions error/failure/exception but the summary does not")

    return TextJobResult(text=candidate, ok=not reasons, reason="; ".join(reasons) or None)


#: job name -> (prompt builder, acceptance checker). Both take
#: ``(reference_text, candidate_or_hint...)`` — see each builder/checker above.
_JOBS: dict[str, tuple] = {
    JOB_COMMIT_MESSAGE: (build_commit_message_prompt, check_commit_message),
    JOB_PR_DESCRIPTION: (build_pr_description_prompt, check_pr_description),
    JOB_SUMMARIZE_OUTPUT: (build_summarize_output_prompt, check_summarize_output),
}


def build_prompt(job: str, input_text: str, *, hint: str | None = None,
                  feedback: str | None = None) -> tuple[str, str]:
    """Dispatch to the job's prompt builder. Raises ``KeyError`` for an
    unknown job — callers validate ``job in JOB_NAMES`` first."""
    builder, _ = _JOBS[job]
    return builder(input_text, hint=hint, feedback=feedback)


def check_output(job: str, input_text: str, candidate: str) -> TextJobResult:
    """Dispatch to the job's acceptance checker."""
    _, checker = _JOBS[job]
    return checker(input_text, candidate)
