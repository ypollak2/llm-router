"""What a model-written commit message or PR body may not do once it is published.

The local model's text passes the router's checks (a file is named, a Summary exists) and nothing
else. Three things in it have an effect outside the text, and none of them was chosen by anyone:

* ``@name`` notifies that person or team when the text is posted (a PR body, an issue comment);
* ``Fixes #12`` / ``closes owner/repo#12`` / ``resolves <issue url>`` closes that issue when the
  change reaches the default branch;
* a ``Test plan`` / ``Testing`` / ``Verification`` section is the model's guess at what was run, which reads as a
  statement that it was (the router's own check wants a Test plan, so the model always writes one);
* a remote image (``![x](https://...)``) is fetched by whoever opens the PR, with whatever the URL's query carries,
  and a link to a page that is not this repository sends the reader somewhere the author never looked at.

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
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b([ \t]{0,3}:?[ \t]{0,3})"  # bounded: no backtracking on long runs of spaces
    r"((?:[\w.-]+/[\w.-]+)?#\d+|https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+)",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
#: The titles that claim a result: test plan, tests, testing, test results, how to test/verify, verification,
#: validation, manual testing, QA. Prefix emoji or punctuation is allowed (``## :white_check_mark: Testing``).
_TITLE = (r"(?:test(?:s|ing|ed)?(?:[ \t]+(?:plan|results?|strategy|coverage|steps|notes|commands?))?"
          r"|how[ \t]+to[ \t]+(?:test|verify)|verif(?:y|ied|ication)|validat(?:e|ed|ion)|qa|manual[ \t]+testing)")
_LEAD = r"(?:(?::\w+:|[^\w\s#*_`])[ \t]*)*"
#: A heading (``##``) or bold line (``**``) with such a title, any text after it; or the title alone on a line; or
#: ``Title: text`` on one line.
_TEST_HEADED = re.compile(rf"^\s{{0,3}}(?:#{{1,6}}[ \t]*|(?:\*\*|__)[ \t]*){_LEAD}{_TITLE}\b", re.IGNORECASE)
_TEST_ALONE = re.compile(rf"^\s{{0,3}}{_LEAD}{_TITLE}[ \t]*:?[ \t]*$", re.IGNORECASE)
_TEST_INLINE = re.compile(rf"^\s{{0,3}}{_LEAD}{_TITLE}[ \t]*:[ \t]*\S", re.IGNORECASE)
#: Remote targets: ``http(s)://``, protocol-relative ``//`` and ``data:``.
_REMOTE = r"(?:https?:|data:|//)"
_IMAGE = re.compile(rf"!\[([^\]]*)\]\(\s*<?{_REMOTE}[^)]*\)", re.IGNORECASE)
_LINK = re.compile(rf"\[([^\]]*)\]\(\s*<?{_REMOTE}[^)]*\)", re.IGNORECASE)
_REF_DEF = re.compile(rf"(?m)^\s{{0,3}}\[[^\]\n]+\]:[ \t]*<?{_REMOTE}\S*.*\n?", re.IGNORECASE)
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_LINK = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)

TEST_PLAN_PLACEHOLDER = "_Not filled in: the local model does not run tests. The author adds what was actually run._"


class Cleaned(NamedTuple):
    text: str
    mentions: int
    closing: int
    test_plan: int
    urls: int = 0


def strip_mentions(text: str) -> tuple[str, int]:
    return _MENTION.subn(lambda m: m.group(1), text)


def strip_closing(text: str) -> tuple[str, int]:
    return _CLOSING.subn(lambda m: f"refs{m.group(2) or ' '}{m.group(3)}", text)


def replace_test_plan(text: str) -> tuple[str, int]:
    """Keep each test / testing / verification heading, replace what the model wrote under it.

    A section runs to the next markdown heading; a ``Title: text`` line runs to the next blank line."""
    lines = text.split("\n")
    out: list[str] = []
    n = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TEST_HEADED.match(line) or _TEST_ALONE.match(line):
            j = i + 1
            while j < len(lines) and not _HEADING.match(lines[j]):
                j += 1
            out.append(line)
            body = [ln for ln in lines[i + 1:j] if ln.strip()]
            if body:
                out += ["", TEST_PLAN_PLACEHOLDER, ""] if j < len(lines) else ["", TEST_PLAN_PLACEHOLDER]
                n += 1
            else:
                out += lines[i + 1:j]
            i = j
            continue
        if _TEST_INLINE.match(line):
            j = i + 1
            while j < len(lines) and lines[j].strip():
                j += 1
            out.append(line.split(":", 1)[0] + ": " + TEST_PLAN_PLACEHOLDER)
            n += 1
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out), n


def strip_remote(text: str) -> tuple[str, int]:
    """Remove remote images and links (and their reference definitions), keeping the visible text of a link and the
    alt text of an image. Targets inside the repository (relative paths, anchors) are left alone."""
    n = 0

    def drop(m: re.Match) -> str:
        nonlocal n
        n += 1
        return m.group(1) if m.lastindex else ""

    text = _IMAGE.sub(drop, text)
    text = _HTML_IMG.sub(drop, text)
    text = _LINK.sub(drop, text)
    text = _HTML_LINK.sub(drop, text)
    text, k = _REF_DEF.subn("", text)
    return text, n + k


def clean(text: str, *, pr_body: bool) -> Cleaned:
    """``text`` with mentions and closing keywords neutralised and, for a PR body, the model's test plan replaced."""
    t, urls = strip_remote(text)
    t, mentions = strip_mentions(t)
    t, closing = strip_closing(t)
    plan = 0
    if pr_body:
        t, plan = replace_test_plan(t)
    return Cleaned(t, mentions, closing, plan, urls)
