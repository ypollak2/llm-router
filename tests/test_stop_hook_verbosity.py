"""The Stop hook's output must be tunable — it fires every turn, not at session end.

WHY THIS EXISTS (CHZ-STOP-01)
=============================

`session-end.py` is registered as a **`Stop`** hook, and `Stop` fires after every
agent response. The filename says session-end; the cadence is per-turn. So the
heaviest output this project produces was printing after every single response,
with no toggle — the only workaround being to unregister the hook, which loses
the information entirely rather than quieting it.

The defect is the MISMATCH, not the block's size. At session-end cadence a full
boxed summary is proportionate. At per-turn cadence it is not.

    full       the boxed summary, unchanged
    condensed  one line, and only when something happened   (default)
    disabled   nothing; `llm_router summary` on demand

WHY condensed IS THE DEFAULT, decided rather than adopted: a default should match
the frequency of the event that triggers it. `full` is one env var away and
byte-identical for anyone who preferred it, so the cost of being wrong here is a
single setting; the cost of leaving it as-is is every user, every turn.

Two properties that are easy to get wrong and are asserted below:

  * condensed emits NOTHING when there is nothing to report. A per-turn line
    saying "no activity" is the same defect one size smaller.
  * an unrecognised value falls back rather than raising. This code runs after
    every turn; a hook that fails closed on a misspelled env var would break the
    session it exists to summarise.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "session-end.py"

# CAPTURED FROM REAL OUTPUT, not written by hand.
# The previous fixture was invented from memory and had the label/money order
# BACKWARDS ("$1.23 lifetime" instead of "lifetime $1.23"). Every test passed
# against it while _condense matched nothing in production and printed a bare
# "682 routed" every turn. A fixture that is a guess tests the guess.
_BOXED = '│  ROUTING  today  44 decisions           SAVINGS  all sessions      │\n│    🔄 fallback      16   36%              lifetime $2299.39        │\n│    🔨 build-fast     7   16%              today    $159.74         │\n│     5h ━━──────────────  16%                                       │\n│     weekly ━━━━━━──────────  39%                                   │'

_EMPTY = "  " + "═" * 40 + "\n  No session activity detected\n  " + "═" * 40


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_se", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_se"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_is_condensed(hook, monkeypatch):
    """Unset must not mean `full` — that is the state being fixed."""
    monkeypatch.delenv(hook._STOP_HOOK_ENV, raising=False)
    assert hook._stop_hook_mode() == "condensed"


@pytest.mark.parametrize("mode", ["full", "condensed", "disabled"])
def test_each_mode_resolves(hook, monkeypatch, mode: str):
    monkeypatch.setenv(hook._STOP_HOOK_ENV, mode)
    assert hook._stop_hook_mode() == mode


@pytest.mark.parametrize("value", ["FULL", " Disabled ", "CONDENSED"])
def test_modes_are_case_and_space_insensitive(hook, monkeypatch, value: str):
    monkeypatch.setenv(hook._STOP_HOOK_ENV, value)
    assert hook._stop_hook_mode() == value.strip().lower()


@pytest.mark.parametrize("value", ["quiet", "off", "1", "yes", "", "ful"])
def test_unknown_values_fall_back_instead_of_raising(hook, monkeypatch, value: str):
    """A hook that dies on a typo breaks the session it only summarises."""
    monkeypatch.setenv(hook._STOP_HOOK_ENV, value)
    assert hook._stop_hook_mode() == "condensed"


def test_condensed_reports_today_lifetime_and_quota(hook):
    """The three numbers worth seeing every turn, each labelled.

    Asserted against a CAPTURED fixture. The invented one had label/money the
    wrong way round, so these assertions passed while production printed a bare
    route count.
    """
    line = hook._condense(_BOXED)
    assert "44 routed" in line
    assert "today $159.74" in line, "today's savings missing or unlabelled"
    assert "lifetime $2299.39" in line, "lifetime savings missing or unlabelled"
    assert "quota used" in line, "quota missing"
    assert chr(10) not in line, "condensed must be ONE line — it prints every turn"


def test_quota_matches_the_status_line_convention(hook):
    """CONSUMED, not remaining — the same direction the status line reports.

    This showed REMAINING for one revision. The arithmetic was right and the
    decision was wrong: the status line shows consumed, so one quantity appeared
    as 39% there and 61% here, and the first person to see both asked whether the
    numbers were real. Two surfaces agreeing beats either being individually
    better, so this asserts the DIRECTION, which is the part that regressed.
    """
    line = hook._condense(_BOXED)
    assert "5h 16%" in line, "5h should report consumed, matching the status line"
    assert "wk 39%" in line, "weekly should report consumed, matching the status line"
    assert "84%" not in line and "61%" not in line, "inverted values leaked back in"



def test_condensed_says_nothing_when_there_is_nothing(hook):
    """A per-turn 'no activity' line is the same defect, one size smaller."""
    assert hook._condense(_EMPTY) == ""


def test_condensed_is_derived_from_the_rendered_summary(hook):
    """Figures are extracted, not recomputed, so the two modes cannot disagree.

    If condensed ever recalculated spend independently, it could report a
    different number than `full` for the same session — a reporting bug that
    would be very hard to notice and impossible to trust.
    """
    altered = _BOXED.replace("$159.74", "$9.99")
    line = hook._condense(altered)
    assert "today $9.99" in line
    assert "159.74" not in line
