"""P11: deterministic gate on a chunk summary (plan row A6, PLAN-SUMMARY-SPEED-256K-20261110).

Flag LLM_ROUTER_SUMMARY_GATE, default off; only called from ``mr_summary.shorten`` so it has effect only
with LLM_ROUTER_PROXY_MR on. A pure function: the facts of a chunk's raw messages are extracted by regex
and the gate passes iff every fact appears in the candidate summary (normalised containment) or in a
pinned list. It returns counts per fact kind and never any text (a fact is file content).

Facts (the path patterns are the research prototype's five, mr_prompt.PIN_RE):
  path   absolute path, relative path with an extension, 7-40 char hex hash, TICKET-123, msg_/toolu_/req_ id
  error  a line that opens with ``Traceback``'s terminating exception line, ``XxxError:`` / ``XxxException:``
         (optionally behind pytest's ``E `` prefix), ``FAILED``, ``ERROR``, ``error:`` / ``error[E0308]:``,
         ``fatal:``; whitespace collapsed, compared case-insensitively on the first 100 characters
  exit   a NON-ZERO exit code: ``Exit code N`` (Claude Code Bash header), ``exit code|status N``,
         ``exited with code N``; the summary satisfies it with ``exit``/``rc``/``status``/``code`` then N
  test   pytest node id ``file.py::name[...]`` and a ``test_*`` name on a FAILED/ERROR line or in a
         ``___ test_x ___`` failure header

Limits: Windows paths (``C:\\x\\y``) are not recognised (the prototype has no pattern for them); a system
reminder text is not scanned; a fact is matched as a string, not by meaning.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

KINDS = ("path", "error", "exit", "test")

_PATH_ABS = r'(?<![\w/.\-~:])(?:~|\.{1,2})?(?:/[\w.@+\-]+){2,}'
_PATH_REL = r'(?<![\w/.\-~:])[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]{1,6}\b'
_HASH = r'\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b'
_TICKET = r'\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b'
_MSGID = r'\b(?:msg|toolu|req)_[A-Za-z0-9]{8,}\b'
#: The one source of the five path/id patterns.
PATH_RE = re.compile('|'.join('(?:%s)' % p for p in (_PATH_ABS, _PATH_REL, _HASH, _TICKET, _MSGID)))

_ERROR_LINE = re.compile(
    r"^(?:E\s+)?(?:[A-Za-z_][\w.]*(?:Error|Exception)\b\s*(?::|\()|Error:|FAILED\b|ERROR\b|error(?:\[\w+\])?:|fatal:)")
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\)")
_EXIT = re.compile(r"(?:^Exit code:?\s*|\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\s*[:=]?\s*)(-?\d+)\b", re.I | re.M)
_NODE_ID = re.compile(r"[\w./\-]+\.py::[\w:\[\].\-]+")
_TEST_NAME = re.compile(r"\btest_\w+\b(?!\.py)")
_TEST_HEADER = re.compile(r"^_{3,}\s+(\S+)\s+_{3,}$")
_REMINDER = "<system-reminder>"
ERROR_CHARS = 100


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_SUMMARY_GATE", "").strip().lower() in ("1", "true", "yes", "on")


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in content)
    return "" if content is None else str(content)


def _texts(m: dict) -> list[str]:
    c = m.get("content")
    if isinstance(c, str):
        return [] if c.lstrip().startswith(_REMINDER) else [c]
    out: list[str] = []
    for b in c or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str) and not b["text"].lstrip().startswith(_REMINDER):
            out.append(b["text"])
        elif t == "tool_result":
            out.append(_result_text(b.get("content")))
        elif t == "tool_use":
            out.append(json.dumps(b.get("input"), ensure_ascii=False, default=str, separators=(", ", ": ")))
    return out


def _norm(s: str) -> str:
    return " ".join(s.split())


def extract_facts(messages: list[dict]) -> dict[str, list[str]]:
    """Facts of the messages by kind, de-duplicated, in first-seen order. Pure; no I/O."""
    facts: dict[str, dict[str, None]] = {k: {} for k in KINDS}
    for m in messages:
        for text in _texts(m):
            for e in PATH_RE.finditer(text):
                p = e.group(0).rstrip('.,;:)')
                if p:
                    facts["path"][p] = None
            for e in _EXIT.finditer(text):
                if int(e.group(1)) != 0:
                    facts["exit"][e.group(1)] = None
            for e in _NODE_ID.finditer(text):
                facts["test"][e.group(0).rstrip('.,;:)')] = None
            after_tb = False
            for raw in text.split("\n"):
                ln = raw.strip()
                if not ln:
                    continue
                if after_tb and not raw[:1].isspace():
                    facts["error"][_norm(ln)[:ERROR_CHARS].lower()] = None  # the exception line that ends a traceback
                    after_tb = False
                if _TRACEBACK.match(ln):
                    after_tb = True
                    continue
                if _ERROR_LINE.match(ln):
                    facts["error"][_norm(ln)[:ERROR_CHARS].lower()] = None
                    after_tb = False
                    if ln.startswith(("FAILED", "ERROR")):
                        for t in _TEST_NAME.findall(_NODE_ID.sub(" ", ln)):
                            facts["test"][t] = None
                h = _TEST_HEADER.match(ln)
                if h and h.group(1).startswith("test_"):
                    facts["test"][h.group(1)] = None
    return {k: list(v) for k, v in facts.items()}


@dataclass
class GateResult:
    passed: bool
    facts: dict[str, int] = field(default_factory=dict)    # facts found per kind
    missing: dict[str, int] = field(default_factory=dict)  # facts not in the summary or pinned list, per kind

    @property
    def missing_total(self) -> int:
        return sum(self.missing.values())


def check(messages: list[dict], summary: str, pinned: Iterable[str] = ()) -> GateResult:
    """Pass iff every fact of ``messages`` appears in ``summary`` (whitespace-normalised containment;
    errors case-insensitive; an exit code as ``exit|rc|status|code`` followed by the number) or in ``pinned``."""
    facts = extract_facts(messages)
    s = _norm(summary)
    sl = s.lower()
    pin = [_norm(p) for p in pinned]
    pin_l = [p.lower() for p in pin]
    res = GateResult(True)
    for kind in KINDS:
        miss = 0
        for f in facts[kind]:
            if kind == "exit":
                ok = bool(re.search(r"(?:exit|rc|status|code)\D{0,16}" + re.escape(f) + r"(?!\d)", sl)) or f in pin
            elif kind == "error":
                ok = f in sl or any(f in p for p in pin_l)
            else:
                ok = f in s or f in pin
            if not ok:
                miss += 1
        res.facts[kind] = len(facts[kind])
        res.missing[kind] = miss
    res.passed = res.missing_total == 0
    return res
