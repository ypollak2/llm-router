"""P11: deterministic summary gate with a carry-forward pinned-facts block (plan row A6).

Flag LLM_ROUTER_SUMMARY_GATE, default off; only called from ``mr_summary.shorten``, so it has effect only
with LLM_ROUTER_PROXY_MR on. Pure functions over the text the map call actually receives (after the
per-block cut), so a fact the model never saw is never required.

Flow in ``shorten``: ``extract`` the chunk's facts BEFORE the map call; if their compact rendering exceeds
``pin_budget()`` chars the chunk is skipped (stays verbatim, no map call). After the map, every fact
``missing`` from the summary is appended verbatim as a ``pinned:`` block inside the chunk-summary wrapper
(``pin_block``); the gate then re-checks the FINAL wrapper text, so what is approved is what is served.
No function here returns fact text to the ledger: callers record counts only.

Facts:
  path   absolute path, relative path with an extension, 7-40 char hex hash (>=1 digit and >=1 letter),
         TICKET-123 (not UTF-8, SHA-256, ... see TICKET_STOP), msg_/toolu_/req_ id
  error  a line (after any ``[role]`` / ``[tool_result]`` render prefix) that starts with ``XxxError:`` /
         ``XxxException:`` (optionally behind pytest's ``E ``), ``Error:``, ``FAILED``, ``ERROR``,
         ``error:`` / ``error[E0308]:``, ``fatal:``, or the unindented line that ends a ``Traceback``;
         whitespace collapsed, first 100 chars, compared case-insensitively
  exit   NON-ZERO ``Exit code N`` / ``exit code|status N`` / ``exited with code N``; satisfied by
         ``exit|rc|status|code`` (whole word) then N
  test   pytest node id ``file.py::name[...]`` and ``test_*`` names on FAILED/ERROR lines or in
         ``___ test_x ___`` headers

Every pattern is linear: starts are anchored with a lookbehind, whitespace runs are bounded, and the
scanned text is capped at MAX_SCAN_CHARS (lines at MAX_LINE). Limits: Windows paths are not recognised;
matching is by string, not meaning.
"""

from __future__ import annotations

import os
import re

KINDS = ("path", "error", "exit", "test")
DEFAULT_PIN_BUDGET_CHARS = 800
MAX_SCAN_CHARS = 400_000
MAX_LINE = 8_000
ERROR_CHARS = 100

#: Path pattern sources. ABS and REL are also the source of mr_summary's extractive() path regex.
PATH_ABS_SRC = r'(?<![\w/.\-~:])(?:~|\.{1,2})?(?:/[\w.@+\-]+){2,}'
PATH_REL_SRC = r'(?<![\w/.\-~:])[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]{1,6}\b'
_HASH = r'(?<!\w)(?=[0-9a-f]{0,39}\d)(?=[0-9a-f]{0,39}[a-f])[0-9a-f]{7,40}(?!\w)'
_TICKET = r'(?<![\w\-])[A-Z][A-Z0-9]{1,9}-\d{1,6}(?![\w\-])'
_MSGID = r'(?<!\w)(?:msg|toolu|req)_[A-Za-z0-9]{8,}(?!\w)'
PATH_RE = re.compile('|'.join('(?:%s)' % p for p in (PATH_ABS_SRC, PATH_REL_SRC, _HASH, _TICKET, _MSGID)))

#: Ticket-shaped tokens that are standards, encodings or models, not ticket ids.
TICKET_STOP = frozenset({
    "UTF", "SHA", "ISO", "HTTP", "HTTPS", "GPT", "X", "TLS", "SSL", "RFC", "IPV", "MD", "AES", "RSA", "PEP",
    "WCAG", "EN", "USB", "UTC", "GMT", "CP", "SO", "SQL", "PY", "PYTHON", "NODE", "LLAMA", "QWEN", "GEMMA",
    "CLAUDE", "OPUS", "SONNET", "HAIKU", "ECDSA", "HMAC", "BASE", "IEEE", "ASCII", "UNICODE", "IP", "TCP", "UDP"})
_TICKET_FULL = re.compile(r'^([A-Z][A-Z0-9]{1,9})-\d{1,6}$')

_PREFIX = re.compile(r'^\[[^\]\n]{1,40}\] ')
_ERROR_LINE = re.compile(
    r"^(?:E\s{1,8})?(?:[A-Za-z_][\w.]*(?:Error|Exception)\b\s{0,4}(?::|\()|Error:|FAILED\b|ERROR\b|error(?:\[\w+\])?:|fatal:)")
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\)")
_EXIT = re.compile(
    r"(?:^Exit code:?[ \t]{0,8}|\bexit(?:ed)?[ \t]{1,8}(?:with[ \t]{1,8})?(?:code|status)[ \t]{0,8}[:=]?[ \t]{0,8})(-?\d{1,6})(?!\d)",
    re.I)
_NODE_ID = re.compile(r"(?<![\w./\-])[\w./\-]+\.py::[\w:\[\].\-]+")
_TEST_NAME = re.compile(r"\btest_\w+\b(?!\.py)")
_TEST_HEADER = re.compile(r"^_{3,} (\S{1,120}) _{3,}$")
_OPEN, _CLOSE = "<system-reminder>", "</system-reminder>"

Facts = dict[str, dict[str, str]]  # kind -> {comparison key: text as it is pinned}


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_SUMMARY_GATE", "").strip().lower() in ("1", "true", "yes", "on")


def pin_budget() -> int:
    """Env-registered LLM_ROUTER_PIN_BUDGET_CHARS: max chars of a chunk's facts, one compact line each."""
    try:
        v = int(os.environ.get("LLM_ROUTER_PIN_BUDGET_CHARS", "").strip())
    except ValueError:
        return DEFAULT_PIN_BUDGET_CHARS
    return v if v > 0 else DEFAULT_PIN_BUDGET_CHARS


def strip_reminders(text: str) -> str:
    """Drop ``<system-reminder>...</system-reminder>`` spans (linear: the search pointer only moves forward)."""
    out, pos = [], 0
    while True:
        i = text.find(_OPEN, pos)
        if i < 0:
            break
        j = text.find(_CLOSE, i)
        if j < 0:
            break
        out.append(text[pos:i])
        pos = j + len(_CLOSE)
    out.append(text[pos:])
    return "".join(out)


def _norm(s: str) -> str:
    return " ".join(s.split())


def _is_stopped(p: str) -> bool:
    m = _TICKET_FULL.match(p)
    return bool(m) and m.group(1) in TICKET_STOP


def extract(text: str) -> Facts:
    """Facts of ``text`` (the map input), de-duplicated, first-seen order. Pure, linear in len(text)."""
    text = strip_reminders(text[:MAX_SCAN_CHARS])
    facts: Facts = {k: {} for k in KINDS}
    for m in PATH_RE.finditer(text):
        p = m.group(0).rstrip('.,;:)')
        if p and not _is_stopped(p):
            facts["path"][p] = p
    for m in _NODE_ID.finditer(text):
        t = m.group(0).rstrip('.,;:)')
        facts["test"][t] = t
    after_tb = False
    for raw in text.split("\n"):
        raw = raw[:MAX_LINE]
        ln = _PREFIX.sub("", raw.strip(), count=1)
        if not ln:
            continue
        for e in _EXIT.finditer(ln):
            if int(e.group(1)) != 0:
                facts["exit"][e.group(1)] = e.group(1)
        if after_tb and not raw[:1].isspace():
            disp = _norm(ln)[:ERROR_CHARS]
            facts["error"][disp.lower()] = disp  # the exception line that ends a traceback
            after_tb = False
        if _TRACEBACK.match(ln):
            after_tb = True
            continue
        if _ERROR_LINE.match(ln[:400]):
            disp = _norm(ln[:400])[:ERROR_CHARS]
            facts["error"][disp.lower()] = disp
            after_tb = False
            if ln.startswith(("FAILED", "ERROR")):
                for t in _TEST_NAME.findall(_NODE_ID.sub(" ", ln[:400])):
                    facts["test"][t] = t
        if len(ln) <= 200:
            h = _TEST_HEADER.match(ln)
            if h and h.group(1).startswith("test_"):
                facts["test"][h.group(1)] = h.group(1)
    return facts


def _line(kind: str, key: str, disp: str) -> str:
    return "exit code " + key if kind == "exit" else disp


def over_budget(facts: Facts) -> bool:
    """True iff the facts, one compact line each, do not fit ``pin_budget()`` chars."""
    return sum(len(_line(k, key, d)) + 1 for k in KINDS for key, d in facts[k].items()) > pin_budget()


def _has(kind: str, key: str, text: str, low: str) -> bool:
    if kind == "error":
        return key in low
    if kind == "exit":
        return re.search(r"\b(?:exit|rc|status|code)\b[^\d\n]{0,12}" + re.escape(key) + r"(?!\d)", low) is not None
    # path and test: a longer or truncated token does not satisfy it
    return re.search(r"(?<![\w/~.\-])" + re.escape(key) + r"(?![\w/\-]|\.\w)", text) is not None


def missing(facts: Facts, summary: str) -> dict[str, list[str]]:
    """Per kind, the keys of the facts that ``summary`` (whitespace-normalised) does not contain."""
    s = _norm(summary)
    low = s.lower()
    return {k: [key for key in facts[k] if not _has(k, key, s, low)] for k in KINDS}


def counts(miss: dict[str, list[str]]) -> dict[str, int]:
    return {k: len(miss.get(k, ())) for k in KINDS}


def pin_block(facts: Facts, miss: dict[str, list[str]]) -> str:
    """``"\\npinned:\\n<one fact per line>"`` for the missing facts, verbatim; ``""`` when none are missing."""
    lines = [_line(k, key, facts[k][key]) for k in KINDS for key in miss.get(k, ())]
    return "\npinned:\n" + "\n".join(lines) if lines else ""
