"""QHYG3: a scan window that starts exactly on the ``s`` of ``-sk-`` still sees the ``-`` before it.

SECDETb review (#803, rv-803): ``engine._windows`` cut the text at multiples of 14,336, so a window could begin on the
``s`` of a ``-sk-<48>`` blob. The ``-`` fell outside the window, the lookbehind that tells an ``sk-`` inside a
base64url run from one at a token start could not see it, and the window refused a string the whole-text scan does not
(the 256-character end cap applies after ``-``). Every value is drawn at run time (tests/secret_fixtures.py).
"""
from __future__ import annotations

import random

from llm_router import engine
from tests import secret_fixtures as fx

STEP = engine._SCAN_WINDOW - engine._SCAN_OVERLAP


def _blob(seed: int) -> str:
    rng = random.Random(f"qhyg3:{seed}")
    key = "sk-" + fx._with_digit(fx._draw(rng, fx.B62, 48))
    return key + fx._draw(rng, fx.B64URL, 300)       # a 348-character run: past the 256 end cap


def test_windows_cover_the_text_and_carry_one_character_of_left_context():
    text = "a" * (3 * STEP)
    wins = list(engine._windows(text))
    assert len(wins) >= 3
    for offset, window in wins:
        assert window == text[offset:offset + len(window)]
    assert [o for o, _ in wins][0] == 0
    assert all(o == STEP * i - 1 for i, (o, _) in enumerate(wins) if i), [o for o, _ in wins]


def test_a_dash_sk_blob_is_read_the_same_wherever_a_window_starts():
    blob = _blob(1)
    short = "x " + "-" + blob + " y"
    assert len(short) < engine._SCAN_WINDOW
    base = engine.secret_findings(short)
    assert "openai_key" not in base, "premise: after '-' a run past 256 characters is not a key"
    for lead in (STEP - 2, STEP - 1, STEP, 2 * STEP - 2, 2 * STEP - 1):   # the 's' lands on, and next to, a window start
        text = "." * lead + "-" + blob + " " + "." * engine._SCAN_WINDOW
        assert len(text) > engine._SCAN_WINDOW
        assert engine.secret_findings(text) == base, lead


def test_control_the_same_blob_at_a_token_start_is_still_refused_across_a_window_start():
    blob = _blob(2)
    text = "." * (STEP - 1) + " " + blob + " " + "." * engine._SCAN_WINDOW   # ' ' before the 's': a token start
    assert engine.secret_findings(text).get("openai_key") is True
