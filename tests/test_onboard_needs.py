"""PLAN v16 P2.8 task 3: at most three needs questions, each with default and skip."""

from __future__ import annotations

import dataclasses
import json

import pytest

from llm_router import onboard_needs as on


def _feed(*answers):
    it = iter(answers)

    def f(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return f


def test_exactly_three_questions_with_default_and_skip():
    assert len(on.QUESTIONS) == on.MAX_QUESTIONS == 3
    for q in on.QUESTIONS:
        assert q["default"] in q["choices"] and q["skip"] and q["prompt"]
    assert "he" in on.QUESTIONS[1]["choices"]


def test_blank_answers_are_defaults():
    a = on.ask_needs(_feed("", "", ""), lambda *x: None)
    assert a == on.NeedsAnswers()
    assert a.skipped == ()


def test_eof_is_defaults_recorded_as_skipped():
    a = on.ask_needs(_feed(), lambda *x: None)
    assert a.main_use == "mixed" and a.skipped == ("main_use", "languages", "priority")


def test_skip_records_field():
    a = on.ask_needs(_feed("code", "s", "skip"), lambda *x: None)
    assert a.main_use == "code" and a.languages == ("en",)
    assert a.skipped == ("languages", "priority")


def test_invalid_input_is_default():
    a = on.ask_needs(_feed("zzz", "klingon", "??"), lambda *x: None)
    assert a == on.NeedsAnswers()


def test_hebrew_explicit_and_comma_list():
    a = on.ask_needs(_feed("", "en, he", ""), lambda *x: None)
    assert a.languages == ("en", "he") and a.requires_hebrew()
    assert not on.NeedsAnswers().requires_hebrew()


def test_each_answer_changes_the_recommender_input():
    assert on.NeedsAnswers(main_use="code").task_weights() != on.NeedsAnswers(main_use="writing").task_weights()
    assert on.NeedsAnswers(languages=("he",)).requires_hebrew() != on.NeedsAnswers(languages=("en",)).requires_hebrew()
    assert on.NeedsAnswers(priority="speed").quality_weight() != on.NeedsAnswers(priority="quality").quality_weight()
    assert on.NeedsAnswers(main_use="code").as_recommender_input() != on.NeedsAnswers().as_recommender_input()


def test_recommender_input_shape_matches_recommend():
    d = on.NeedsAnswers(main_use="code", languages=("he",), priority="quality").as_recommender_input()
    assert d == {"main_use": "code", "languages": ["he"], "priority": "quality"}
    json.dumps(d)


def test_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        on.NeedsAnswers().main_use = "code"  # type: ignore[misc]
