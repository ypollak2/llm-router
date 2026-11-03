"""What a model-written commit message or PR body may not do once it is published.

The local model's text passes the router's checks (a file is named, a Summary exists) and nothing
else. Three things in it have an effect outside the text, and none of them was chosen by anyone:

* ``@name`` notifies that person or team when the text is posted (a PR body, an issue comment);
* ``Fixes #12`` / ``closes owner/repo#12`` / ``resolves <issue url>`` closes that issue when the
  change reaches the default branch;
* a ``Test plan`` section is the model's guess at what was run, which reads as a statement that it was.

They are neutralised, not rejected: a rejected text costs the model another attempt and the lane an
attempt in its demotion window, and the rest of the text is fine. The neutral forms keep the
information (``refs #12``, ``alice``) and lose the effect. Every function is pure and the counts are
returned so the ledger can say what was done without holding any text.
"""

from __future__ import annotations

import re
from typing import NamedTuple

#: A GitHub mention: ``@`` not preceded by a word character (so ``a@b.com`` is left alone), a user name
#: of up to 39 characters, optionally ``/team``.
_MENTION = re.compile(r"(?<![\w@`/.])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9_.-]+)?)")
#: The nine closing keywords GitHub recognises, followed by an issue or PR reference or its URL.
_CLOSING = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(\s*:?\s*)"
    r"((?:[\w.-]+/[\w.-]+)?#\d+|https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+)",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_TEST_PLAN = re.compile(r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:test(?:ing)?\s*plan|how\s+to\s+test)\b", re.IGNORECASE)

TEST_PLAN_PLACEHOLDER = "_Not filled in: the local model does not run tests. The author adds what was actually run._"


class Cleaned(NamedTuple):
    text: str
    mentions: int
    closing: int
    test_plan: int


def strip_mentions(text: str) -> tuple[str, int]:
    return _MENTION.subn(lambda m: m.group(1), text)


def strip_closing(text: str) -> tuple[str, int]:
    return _CLOSING.subn(lambda m: f"refs{m.group(2) or ' '}{m.group(3)}", text)


def replace_test_plan(text: str) -> tuple[str, int]:
    """Keep each ``Test plan`` heading, replace what the model wrote under it."""
    lines = text.split("\n")
    out: list[str] = []
    n = 0
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if _TEST_PLAN.match(lines[i]):
            j = i + 1
            while j < len(lines) and not _HEADING.match(lines[j]):
                j += 1
            body = [ln for ln in lines[i + 1:j] if ln.strip()]
            if body:
                out += ["", TEST_PLAN_PLACEHOLDER, ""] if j < len(lines) else ["", TEST_PLAN_PLACEHOLDER]
                n += 1
            else:
                out += lines[i + 1:j]
            i = j
            continue
        i += 1
    return "\n".join(out), n


def clean(text: str, *, pr_body: bool) -> Cleaned:
    """``text`` with mentions and closing keywords neutralised and, for a PR body, the model's test plan replaced."""
    t, mentions = strip_mentions(text)
    t, closing = strip_closing(t)
    plan = 0
    if pr_body:
        t, plan = replace_test_plan(t)
    return Cleaned(t, mentions, closing, plan)
