"""Redo detector over Claude Code transcripts (PLAN M0.9, O3 redo source 4).

A *redo* is a human turn whose prompt corrects, complains about, repeats or re-asks the previous
turn. The proxy already sees three of these from the request body (``proxy/escalation.py``);
this module reads the transcript instead, so it also sees a plain "that didn't work" and a pasted
repeat. It is a DETECTOR: whether it counts toward O3 is decided by
``offload_share.REDO_SOURCE4_ENABLED``, which stays False until a follow-up PR cites a validation run
on blind labels (M0-7b).

Signals, all attached to the later prompt of a pair of consecutive human prompts:

* ``reask``         the prompt opens with ``claude:`` / ``native:`` / ``opus:`` (the owner's re-ask prefixes);
* ``contradiction`` the prompt opens with a correction (escalation's own regex), or a failure phrase
                    ("that is wrong", "didn't work", "try again" ...) occurs in its first
                    ``FAILURE_WINDOW`` characters;
* ``repeat``        the prompt's word 5-grams overlap the previous human prompt with Jaccard >= 0.8.

The first prompt of a session is never a redo. Only prompt text is matched: no model output and no tool
output. Nothing here writes a file.

Patterns for ``claude:``, ``opus:`` and the opening correction are the proxy's own objects
(``proxy/escalation.py``), imported, so there is one source. ``native:``, the failure phrases and the
repeat test have no proxy equivalent and live here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from llm_router.proxy.escalation import _CLAUDE_REASK_RE, _CONTRADICTION_RE, _OPUS_PIN_RE

# Bump when a pattern below changes. Validation results (docs and PROGRESS) quote this string.
# v0 first patterns; v1 and v2 are the two permitted widenings (PLAN M0.9), both made on the tune half of the
# blind-labelled pairs only: v1 more failure phrases and a 8-word floor for the repeat signal, v2 "run/do/try
# again" and "I'm missing". v2 is the version that was frozen for validation.
PATTERN_VERSION = "v2"

# Candidate variants for the tune step (P3.6-c PR 3; design section 5). v2 is the default everywhere and
# stays byte-identical. v3a and v3b are precision-first candidates written from the design's guidance
# ("demote or tighten repeat, which produced the v0 false positives and the 5-6 word false positives"), not
# fitted to data. v3c and v3d are the two recall-raising candidates, derived only from the label
# definition, without fitting. The widening cap counts distinct (candidate, detector sha256)
# evaluations: v3a/v3b/v3c/v3d are four candidates, but only two non-baseline ones from their union
# may ever be scored per tune log. The owner must pick which two.
# Dead end: once both widenings are used, ANY edit to this file (even a comment) changes the detector
# sha256, so no non-baseline variant can be tune-scored again (a new pair would be a third widening)
# and none can be frozen (freeze needs the candidate's row at the current sha). Only v2 stays freezable.
BASELINE_VARIANT = "v2"
VARIANTS = {
    "v2": "frozen M0.9 patterns",
    "v3a": "tighten repeat: at least 12 words and Jaccard >= 0.9; reask and contradiction as v2",
    "v3b": "demote repeat: reported only when reask or contradiction also fires; thresholds as v2",
    "v3c": "extra answer-directed correction phrases, reported as contradiction; all v2 signals retained",
    "v3d": "v3c plus short negative follow-ups of at most 6 words; all v2 signals retained",
}
V3A_REPEAT_MIN_WORDS = 12
V3A_JACCARD_MIN = 0.9
# The design screens at <=12 words; precision-first half of that is an unfitted choice.
V3D_SHORT_MAX_WORDS = 6

SIGNAL_REASK = "reask"
SIGNAL_CONTRADICTION = "contradiction"
SIGNAL_REPEAT = "repeat"

FAILURE_WINDOW = 200          # chars of the prompt searched for a failure phrase
JACCARD_MIN = 0.8
REPEAT_MIN_WORDS = 8          # a repeat of a very short prompt ("what's the status?") is not a redo
NGRAM = 5
ANSWER_TAIL_CHARS = 1500      # the end of the previous answer kept for a labelling pair
PROMPT_HEAD_CHARS = 1000      # the start of the prompt kept for a labelling pair

_NATIVE_REASK_RE = re.compile(r"^native:\s*", re.IGNORECASE)
_FAILURE_RE = re.compile(
    r"(that(?:'s| is| was) (?:wrong|incorrect|not (?:right|correct|what))"
    r"|(?:did not|didn't|doesn't|does not|isn't|is not|aren't|are not|wasn't|won't|can't|cannot|never) "
    r"(?:work|working|show|showing|help|fix|fixed|solve)"
    r"|not working"
    r"|try again"
    r"|\b(?:run|do|try|check|ask|test) (?:it |them |that |this )?again\b|please redo|\bre-?run\b|\bre-?do\b"
    r"|\b(?:i'm|i am) missing\b"
    r"|still (?:broken|failing|fails|not working|can't|cannot|don't|doesn't|the same|no\b)"
    r"|\b(?:failed|failing)\b"
    r"|you (?:didn't|did not|forgot|missed|never|haven't|have not|should have)"
    r"|(?:not|isn't) (?:what i|right|correct|good))")


# v3c widens only contradiction, using corrections of the previous ANSWER from the label definition
# (design section 1). Each phrase needs a rejection framing: "same error" only as "still getting / I get
# the same error" or as the whole short prompt, never as a product report ("same error appears when the
# server restarts"); "still not" only with working/fixed/right/correct; "i meant" not followed by "to"
# ("I meant to ask ..." is a new question). Commands require a prompt-opening imperative and a backward
# reference to the last change ("revert your change", "go back to the previous version"), never
# "revert your branch" or "go back to the plan". Overlap with v2 is intentional.
_REDO_PHRASE_RE_V3C = re.compile(
    r"\b(?:that's not what i (?:asked|wanted|meant|said)"
    r"|not what i asked"
    r"|still (?:wrong\b|(?:not|isn't) (?:working|fixed|right|correct)\b"
    r"|(?:not|isn't) the (?:requested|one i asked)\b)"
    r"|(?:that|it) (?:didn't|did not) work"
    r"|(?:still (?:getting|seeing|having)|i (?:still )?get|i'm (?:still )?getting|i am (?:still )?getting)"
    r" the same (?:error|problem|issue|result)\b"
    r"|you (?:didn't|did not)\b"
    r"|that (?:broke\b|made it worse\b))"
    r"|^(?:the )?same (?:error|problem|issue|result)(?: again| as before)?\s*[.!]*\s*$"
    r"|^(?:no,?\s*)?i meant\b(?! to\b)"
    r"|^(?:(?:please|can you|just)\s+)*(?:undo (?:that|this)\b"
    r"|revert (?:that|it|this|the last (?:change|edit)|your (?:change|changes|edit|edits|last))\b"
    r"|go back to (?:the|your) (?:previous|old|original|earlier) "
    r"(?:version|approach|code|request|answer|one)\b"
    r"|go back to how it was\b|roll that back\b)"
)

# Rejection tokens only: bare "still" ("still need to push it", "still on it?", "still, can you also add
# X") and bare "not" ("not merged yet") are status or refinement, not a rejection of the answer.
_V3D_REJECTION_RE = re.compile(
    r"^(?:no|nope|wrong|not (?:it|that|right|quite)"
    r"|still (?:wrong|broken|failing|not working|the same))\b")
_V3D_BENIGN_RE = re.compile(
    r"^(?:no(?:,\s*|\s+)(?:worries|problem|thanks|thank you|need|rush)"
    r"|not (?:now|yet|today|right now|really|sure|bad|a problem)"
    r"|no (?:idea|clue)"
    r"|still (?:working|going|running|waiting|thinking|here|looking|reading|testing))\b"
)


def _short_negative_v3d(text: str) -> bool:
    """Recognise a short rejection, conservatively excluding acknowledgements and deferrals.

    v2 already flags an opening ``no`` followed by punctuation or a space, and ``nope``
    (``escalation._CONTRADICTION_RE``), so v3d's distinct contribution is a bare "no" and the
    "wrong" / "not right" / "not that" / "still wrong" style short forms.

    Callers compute session signals only for prompts k>=1 (a previous prompt exists), providing
    the 'follows an assistant answer' context for this heuristic. No answer text is inspected.
    'Not really' and 'not sure' are conservatively treated as benign, as is any mention of 'later'.
    Exclusions apply only to this addition: inherited v2 false positives must remain for the superset.
    """
    return (
        len(_words(text)) <= V3D_SHORT_MAX_WORDS
        and _V3D_REJECTION_RE.match(text) is not None
        and _V3D_BENIGN_RE.match(text) is None
        and re.search(r"\blater\b", text) is None
    )

# ── transcript records ───────────────────────────────────────────────────────

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TAG_RES = [re.compile(r"<%s>.*?</%s>" % (t, t), re.DOTALL) for t in
            ("local-command-caveat", "ide_opened_file", "ide_selection", "command-name",
             "command-message", "command-args")]
_NONHUMAN_PREFIXES = ("<task-notification", "<local-command-", "<command-", "This session is being continued",
                      "Caveat: The messages below", "[Request interrupted")
_TYPED_SOURCES = {None, "typed", "queued"}


def _clean(text: str) -> str:
    text = _REMINDER_RE.sub("", text or "")
    for rx in _TAG_RES:
        text = rx.sub("", text)
    return text.strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _message(rec: Any) -> dict:
    msg = rec.get("message") if isinstance(rec, dict) else None
    return msg if isinstance(msg, dict) else {}


def _ts(rec: dict) -> float | None:
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _human_text(rec: Any) -> str | None:
    """The cleaned prompt text when ``rec`` is a prompt the developer typed, else None."""
    if not isinstance(rec, dict) or rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") and origin["kind"] != "human":
        return None
    if rec.get("promptSource") not in _TYPED_SOURCES:        # sdk, system, suggestion_accepted ...
        return None
    content = _message(rec).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                         for b in content):
        return None
    raw = _content_text(content)
    if raw.lstrip().startswith(_NONHUMAN_PREFIXES) or re.match(r"\[\d+ prior ", raw.lstrip()):
        return None
    text = _clean(raw)
    return text or None


@dataclass(frozen=True)
class HumanPrompt:
    entry_index: int
    ts: float | None
    text: str


def human_prompts(entries: Iterable[Any]) -> list[HumanPrompt]:
    out: list[HumanPrompt] = []
    for i, rec in enumerate(entries):
        try:
            text = _human_text(rec)
        except Exception:  # noqa: BLE001 -- a malformed record is not a prompt
            text = None
        if text is not None:
            out.append(HumanPrompt(i, _ts(rec), text))
    return out


# ── the three signals ────────────────────────────────────────────────────────

def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _grams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + NGRAM]) for i in range(len(words) - NGRAM + 1)}


def five_gram_jaccard(a: str, b: str) -> float | None:
    """Jaccard similarity of the two texts' word 5-gram sets; None when either has fewer than 5 words."""
    ga, gb = _grams(_words(a)), _grams(_words(b))
    if not ga or not gb:
        return None
    return len(ga & gb) / len(ga | gb)


def _signals(prompt: str, previous: str | None, variant: str = BASELINE_VARIANT) -> list[str]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown detector variant: {variant}")
    text = prompt.replace("’", "'").lstrip()
    lowered = text.lower()
    out: list[str] = []
    if _CLAUDE_REASK_RE.match(lowered) or _NATIVE_REASK_RE.match(lowered) or _OPUS_PIN_RE.match(lowered):
        out.append(SIGNAL_REASK)
    if _CONTRADICTION_RE.match(lowered[:80]) or _FAILURE_RE.search(lowered[:FAILURE_WINDOW]):
        out.append(SIGNAL_CONTRADICTION)
    if variant in ("v3c", "v3d") and SIGNAL_CONTRADICTION not in out:
        if (_REDO_PHRASE_RE_V3C.search(lowered[:FAILURE_WINDOW])
                or (variant == "v3d" and _short_negative_v3d(lowered))):
            out.append(SIGNAL_CONTRADICTION)
    min_words, jaccard_min = REPEAT_MIN_WORDS, JACCARD_MIN
    if variant == "v3a":
        min_words, jaccard_min = V3A_REPEAT_MIN_WORDS, V3A_JACCARD_MIN
    if previous is not None and len(_words(prompt)) >= min_words:
        j = five_gram_jaccard(prompt, previous)
        if j is not None and j >= jaccard_min and not (variant == "v3b" and not out):
            out.append(SIGNAL_REPEAT)
    return out


def redo_flag_times(entries: Iterable[Any],
                    variant: str = BASELINE_VARIANT) -> list[tuple[float | None, int, str]]:
    """``(prompt timestamp, turn_index, signal)`` for every flagged human prompt. ``turn_index`` counts the
    session's human prompts from 0; a prompt with turn_index k is a redo of turn k-1."""
    prompts = human_prompts(list(entries))
    out: list[tuple[float | None, int, str]] = []
    for k in range(1, len(prompts)):
        for sig in _signals(prompts[k].text, prompts[k - 1].text, variant):
            out.append((prompts[k].ts, k, sig))
    return out


def redo_flags(transcript_entries: Iterable[Any],
               variant: str = BASELINE_VARIANT) -> list[tuple[int, str]]:
    """``(turn_index, signal)`` for every flagged human prompt of one session's transcript records."""
    return [(k, sig) for _, k, sig in redo_flag_times(transcript_entries, variant)]


# ── labelling pairs ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pair:
    turn_index: int        # of the later prompt, as in redo_flags
    ts: float | None       # of the later prompt
    answer_tail: str       # end of the previous turn's last assistant text, <= ANSWER_TAIL_CHARS
    prompt: str            # start of the later prompt, <= PROMPT_HEAD_CHARS


def turn_pairs(entries: Iterable[Any]) -> list[Pair]:
    """Consecutive human-prompt pairs with the previous turn's final answer text. A turn with no assistant
    text before the next prompt yields no pair (nothing to show a labeller)."""
    pairs: list[Pair] = []
    n_prompts = 0
    last_answer = ""
    for rec in entries:
        try:
            text = _human_text(rec)
            if text is not None:
                if n_prompts and last_answer:
                    pairs.append(Pair(n_prompts, _ts(rec), last_answer[-ANSWER_TAIL_CHARS:],
                                      text[:PROMPT_HEAD_CHARS]))
                n_prompts += 1
                last_answer = ""
            elif (isinstance(rec, dict) and rec.get("type") == "assistant" and not rec.get("isSidechain")):
                said = _content_text(_message(rec).get("content")).strip()
                if said:
                    last_answer = said
        except Exception:  # noqa: BLE001
            continue
    return pairs


# ── loading a session's transcript ───────────────────────────────────────────

def _slim(rec: dict) -> dict:
    """Keep only what the detector reads: no tool output, no thinking, no usage blobs."""
    msg = _message(rec)
    content = msg.get("content")
    if isinstance(content, list):
        content = [b if (isinstance(b, dict) and b.get("type") in ("text",)) else
                   {"type": b.get("type")} for b in content if isinstance(b, dict)]
    return {"type": rec.get("type"), "timestamp": rec.get("timestamp"), "origin": rec.get("origin"),
            "promptSource": rec.get("promptSource"), "isSidechain": rec.get("isSidechain"),
            "isMeta": rec.get("isMeta"), "message": {"content": content}}


def read_transcript(path: Path) -> list[dict]:
    """The user/assistant records of one transcript file, slimmed. Unreadable lines are skipped."""
    import json

    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"user"' not in line and '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("type") in ("user", "assistant"):
                    out.append(_slim(rec))
    except OSError:
        return []
    return out


def session_flags_loader(projects_dir: Path) -> Callable[[str], list[tuple[float, str]]]:
    """``load(session_id) -> [(prompt ts, signal)]``: reads ``<projects_dir>/*/<session_id>.jsonl`` on demand
    and caches per session. A missing or unreadable transcript yields no flags. Flags without a timestamp
    are dropped (they cannot be placed on the proxy ledger's clock)."""
    index: dict[str, Path] = {}
    cache: dict[str, list[tuple[float, str]]] = {}

    def locate(sid: str) -> Path | None:
        if not index:
            try:
                for proj in Path(projects_dir).iterdir():
                    if proj.is_dir():
                        for f in proj.glob("*.jsonl"):
                            index.setdefault(f.stem, f)
            except OSError:
                pass
            index.setdefault("", Path(""))     # mark scanned even when nothing was found
        return index.get(sid)

    def load(sid: str) -> list[tuple[float, str]]:
        if sid in cache:
            return cache[sid]
        path = locate(sid)
        flags: list[tuple[float, str]] = []
        if path is not None:
            flags = [(t, sig) for t, _, sig in redo_flag_times(read_transcript(path)) if t is not None]
        cache[sid] = flags
        return flags

    return load
