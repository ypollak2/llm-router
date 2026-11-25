"""Paraphrase re-ask detector v1, SHADOW only (design 2026-11-25).

the release audit's prefix test counts a re-ask only when the next human prompt opens with ``claude:`` /
``native:`` / ``opus:``. A user who rewords the same request ("I meant ...", "that didn't work, ...",
the same ask in other words) is counted as ``used``. This module is the offline detector for that case.
It labels nothing: the release audit script calls it only when
``LLM_ROUTER_REASK_PARAPHRASE=shadow`` and reports the hits in separate counters.

Rule: ``D or (C and A)``.

* D  a correction phrase: ``redo_signal._CONTRADICTION_RE`` / ``_FAILURE_RE`` (imported, never edited),
     on the next prompt with fenced code and ``>`` quote lines removed (``redo_signal._unquoted``), so a
     pasted error text does not count.
* C  structure: both prompts name file paths, the next one names no path the previous did not, it does not
     open a new target ("now", "also", "next", "for the other ...", "same for ..."), and it follows within
     ``MAX_TURNS`` human turns or ``MAX_GAP_S`` seconds.
* A  wording: Jaccard of the unigram+bigram sets (stopwords dropped, crude suffix stemming) >= ``A_MIN``.

``A_MIN`` was picked on the synthetic tune set (``tests/fixtures/reask_paraphrase_tune.jsonl``), see
``tests/test_reask_detect.py``. Synthetic pairs are a sanity check, not a measured precision on real
prompts.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    from llm_router.redo_signal import _CONTRADICTION_RE, _FAILURE_RE, _unquoted
except ImportError:  # path-loaded by the release audit with src/ not on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from llm_router.redo_signal import _CONTRADICTION_RE, _FAILURE_RE, _unquoted

FLAG = "LLM_ROUTER_REASK_PARAPHRASE"
MAX_TURNS = 2
MAX_GAP_S = 600.0
A_MIN = 0.4           # top of the 0.2-0.4 plateau on the synthetic tune set (n=48); see the module docstring
FAILURE_WINDOW = 200  # same window redo_signal uses

REASON_D = "D_correction_phrase"
REASON_CA = "C_A_same_target_overlap"
REASON_NONE = "none"
REASON_EMPTY = "no_text"

_PATH_RE = re.compile(r"(?<![\w/.])((?:~?/|\./|\.\./)?(?:[\w.\-]+/)*[\w\-]+\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/])")
_NEW_TARGET_RE = re.compile(
    r"\b(?:now|also|next|then|additionally|another|other|instead)\b|\bsame (?:for|to|on|with)\b|\bas well\b",
    re.IGNORECASE)
_STOP = frozenset(
    "a an the to of in on at for and or but is are was were be it its this that these those i you we me my "
    "your our please can could would should will just so do does did with as by from".split())


def reask_mode() -> str:
    """``off`` (default) or ``shadow``; anything else is ``off``."""
    v = (os.environ.get("LLM_ROUTER_REASK_PARAPHRASE") or "off").strip().lower()
    return "shadow" if v == "shadow" else "off"


def extract_targets(text: str | None) -> frozenset[str]:
    """File-path-like tokens in a prompt, lower-cased. Version numbers ("3.11") are not paths: the
    extension must start with a letter."""
    return frozenset(m.group(1).lower() for m in _PATH_RE.finditer(_unquoted(text or "")))


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _features(text: str) -> set[str]:
    words = [_stem(w) for w in re.findall(r"[a-z0-9_]+", _unquoted(text).lower()) if w not in _STOP]
    return set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}


def overlap(a: str, b: str) -> float:
    fa, fb = _features(a), _features(b)
    return len(fa & fb) / len(fa | fb) if fa and fb else 0.0


def _is_correction(next_prompt: str) -> bool:
    text = _unquoted(next_prompt.replace("’", "'")).lower()
    return bool(_CONTRADICTION_RE.match(text[:80]) or _FAILURE_RE.search(text[:FAILURE_WINDOW]))


def is_paraphrase_reask(prev_prompt: str | None, next_prompt: str | None,
                        prev_targets: frozenset[str] | set[str] | None = None,
                        next_targets: frozenset[str] | set[str] | None = None,
                        gap_s: float | None = None, turns_between: int | None = None) -> tuple[bool, str]:
    """(is a paraphrase re-ask of ``prev_prompt``, reason code). Pure: no I/O, no logging of text."""
    if not (next_prompt or "").strip():
        return False, REASON_EMPTY
    if _is_correction(next_prompt):
        return True, REASON_D
    if not (prev_prompt or "").strip():
        return False, REASON_NONE
    pt = frozenset(prev_targets or ())
    nt = frozenset(next_targets or ())
    near = (turns_between is not None and turns_between <= MAX_TURNS) or (
        gap_s is not None and gap_s <= MAX_GAP_S)
    if not (pt and nt and near and nt <= pt):
        return False, REASON_NONE
    if _NEW_TARGET_RE.search(_unquoted(next_prompt)):
        return False, REASON_NONE
    if overlap(prev_prompt, next_prompt) >= A_MIN:
        return True, REASON_CA
    return False, REASON_NONE
