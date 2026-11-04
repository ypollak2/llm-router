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

Best effort, and linear. Markdown has more ways to write a link than any pattern lists, so URLs are removed by what they
ARE (a scheme and what follows it, wherever it sits in the text, after the character references and backslash
escapes that could spell it are undone), not by the markup around them; the markup that remains cannot name a
destination. Nothing here backtracks: every pattern is a single bounded or non-overlapping scan, and a text longer
than ``MAX_CHARS`` (the longest a PR body may be) is cut before any of it runs (the engine refuses such a text
before it gets here). GitHub's own sanitiser is not consulted.
"""

from __future__ import annotations

import html
import re
from typing import NamedTuple

#: The longest text this module works on (GitHub's limit for a PR body). Longer input is cut before any pattern runs.
MAX_CHARS = 65_536

#: A GitHub mention: ``@`` not preceded by a word character (so ``a@b.com`` is left alone), a user name
#: of up to 39 characters, optionally ``/team``.
_MENTION = re.compile(r"(?<![\w@`/.])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9_.-]+)?)")
#: The nine closing keywords GitHub recognises, followed by an issue or PR reference or its URL.
_CLOSING = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b([ \t\r\n]{0,8}:?[ \t\r\n]{0,8})"  # bounded: no backtracking on a long run
    r"((?:[\w.-]+/[\w.-]+)?#\d+|https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+)",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
#: The titles that claim a result: test plan, tests, testing, test results, how to test/verify, verification,
#: validation, manual testing, QA. Prefix emoji or punctuation is allowed (``## :white_check_mark: Testing``).
_TITLE = (r"(?:(?:[A-Za-z]{2,12}[ \t]+){0,2}(?:tests?|testing|tested)(?:[ \t]+(?:plan|results?|strategy|coverage|steps|notes|commands?|infrastructure))?"
          r"|(?:how[ \t]+(?:i|we)[ \t]+|how[ \t]+to[ \t]+|what[ \t]+was[ \t]+)(?:tested?|verified?)"
          r"|(?:[A-Za-z]{2,12}[ \t]+)?(?:verif(?:y|ied|ication)|validat(?:e|ed|ion)|qa)|checks?[ \t]+(?:run|performed)|results?)")
_LEAD = r"(?:(?::\w+:|[^\w\s#*_`])[ \t]*)*"
#: A heading (``##``) or bold line (``**``) with such a title, any text after it; or the title alone on a line; or
#: ``Title: text`` on one line.
_TEST_HEADED = re.compile(rf"^\s{{0,3}}(?:#{{1,6}}[ \t]*|(?:\*\*|__)[ \t]*){_LEAD}{_TITLE}(?![A-Za-z0-9])", re.IGNORECASE)
_TEST_ALONE = re.compile(rf"^\s{{0,3}}{_LEAD}{_TITLE}[ \t]*:?[ \t]*$", re.IGNORECASE)
_TEST_INLINE = re.compile(rf"^\s{{0,3}}{_LEAD}{_TITLE}[ \t]*:[ \t]*\S", re.IGNORECASE)
#: A character reference is undone only when it spells a character a URL is made of (so ``&#104;ttps&#58;//`` becomes visible to
#: the scan below, while ``&lt;`` and ``&amp;`` stay as written).
_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:/.\\-_~?#@!$*+,;=%()[]")
#: Any scheme that fetches or runs something, with what follows it: ``scheme://`` up to a space or a delimiter for the network
#: ones; ``scheme:`` glued to its body, up to a space, for ``data:``, ``javascript:`` and the like (their bodies hold quotes and
#: brackets; the word "data:" in prose, with a space after it, stays).
_URL = re.compile(
    r"(?<![A-Za-z0-9+.-])(?:(?:https?|ftps?|sftp|ssh|git|file|wss?)://[^\s<>\"'`)\]]*"
    r"|(?:data|javascript|vbscript|mailto|tel):[^\s<>]+)", re.IGNORECASE)
#: A protocol-relative target (``//host/x``) only where a target can start: after ``( < " ' = [``.
_PROTO_RELATIVE = re.compile(r"(?<=[(<\"'=\[])//[^\s<>\"'`)\]]*")
#: Tags that fetch, run or embed. A tag is matched as ``<name`` + at most 512 characters that contain neither ``<`` nor ``>`` +
#: ``>``, so a failed match never runs past the next ``<``. Other angle brackets (``Vec<String>``) are not touched.
_DANGEROUS_TAGS = frozenset({
    "a", "img", "picture", "source", "iframe", "script", "style", "link", "meta", "base", "form", "input", "button", "object",
    "embed", "video", "audio", "svg", "math", "track", "area", "map", "frame", "frameset", "applet", "template",
})
_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)[^<>]{0,512}>")

TEST_PLAN_PLACEHOLDER = "_Not filled in: the local model does not run tests. The author adds what was actually run._"


class Cleaned(NamedTuple):
    text: str
    mentions: int
    closing: int
    test_plan: int
    urls: int = 0
    truncated: bool = False


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


def _undo_references(text: str) -> str:
    """Spell out the character references and backslash escapes that could hide a URL, and nothing else."""
    if "&" in text:
        def one(m: re.Match) -> str:
            ch = html.unescape(m.group(0))
            return ch if len(ch) == 1 and ch in _URL_CHARS else m.group(0)

        text = _ENTITY.sub(one, text)
    if "\\" in text:
        text = text.replace("\\/", "/").replace("\\:", ":")
    return text


def strip_remote(text: str) -> tuple[str, int]:
    """Remove every URL that fetches or runs something (``http``, ``https``, ``ftp``, ``data:``, ``javascript:``, ...,
    protocol-relative ones, in any markup or none), every markdown image marker and every embedding HTML tag; keep the visible
    text of a link. Relative links and anchors stay. Linear: no pattern here can backtrack, and removing a URL can
    make the text around it meet, so the scan repeats until nothing is left (at most three times)."""
    n = 0
    for _ in range(3):
        before = n
        text = _undo_references(text)
        text, k = _URL.subn("", text)
        n += k
        text, k = _PROTO_RELATIVE.subn("", text)
        n += k
        if "![" in text:
            n += text.count("![")
            text = text.replace("![", "[")
        def drop_tag(m: re.Match) -> str:
            nonlocal n
            if m.group(1).lower() in _DANGEROUS_TAGS:
                n += 1
                return ""
            return m.group(0)

        text = _TAG.sub(drop_tag, text)
        if n == before:
            break
    return text, n


def clean(text: str, *, pr_body: bool) -> Cleaned:
    """``text`` with remote targets, mentions and closing keywords neutralised and, for a PR body, the model's test sections
    replaced. A text longer than ``MAX_CHARS`` is cut first and ``truncated`` says so (the engine refuses such a text
    before calling this; the cut only keeps the work linear for anything else)."""
    cut = len(text) > MAX_CHARS
    if cut:
        text = text[:MAX_CHARS]
    t, urls = strip_remote(text)
    t, mentions = strip_mentions(t)
    t, closing = strip_closing(t)
    plan = 0
    if pr_body:
        t, plan = replace_test_plan(t)
    return Cleaned(t, mentions, closing, plan, urls, cut)
