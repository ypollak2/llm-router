"""LC-12h repair 2 (D-298), SHOULD items: label laundering, exemption counts, the override strip, the history counter.

Every secret is generated at run time (tests/secret_fixtures.py); no model, no network.
"""
# ruff: noqa: F811
from __future__ import annotations

import pytest

from llm_router import local_privacy
from tests import secret_fixtures as fx
from tests.test_lc12h_repair3 import high


@pytest.fixture(autouse=True)
def _clean_counts():
    local_privacy.secret_counts.clear()
    yield
    local_privacy.secret_counts.clear()


def _laundering_phrases(key: str) -> list[str]:
    return [f"fake? no. {key}", f"my dummy account's key {key}",
            f"replace the placeholder with my real key {key}", f"this is not fake\nx\n{key}",
            f"this is a placeholder, not a real key: {key}", f"dummy value for illustration only\n{key}"]


def test_the_reviewers_laundering_phrases_are_refused():
    for seed, name in enumerate(("openai_key", "github_token", "aws_key_id"), 1):
        key = fx.real_shaped(name, seed)
        for text in _laundering_phrases(key):
            assert local_privacy.scan_secrets(text), text.replace(key, "<key>")
    assert local_privacy.secret_counts.get("exempted_by_label:OpenAI API key", 0) == 0


def test_the_placeholder_corpus_is_still_not_refused():
    cases = fx.placeholders()
    assert len(cases) >= 30
    assert [t[:40] for t in cases if local_privacy.scan_secrets(t)] == []


def test_an_exemption_by_filename_cue_is_counted_by_category():
    key = fx.real_shaped("openai_key", 11)
    assert local_privacy.scan_secrets(f"# .env.example\nOPENAI_API_KEY={key}") == []
    assert sum(v for k, v in local_privacy.secret_counts.items() if k.startswith("exempted_by_label:")) == 1
    assert not any(key in k for k in local_privacy.secret_counts)           # category only


def test_a_free_text_word_beside_a_filename_cue_still_needs_the_cue():
    key = fx.real_shaped("github_token", 12)
    assert high(f"fake token {key}") == ["github_token"]
    assert high(f"see config.env.sample\nT={key}") == []


def test_split_override_strips_only_when_a_secret_is_found():
    key = fx.real_shaped("github_token", 13)
    assert local_privacy.split_override("cloud: explain closures") == ("cloud: explain closures", False)
    assert local_privacy.split_override("Cloud :   hello", "system text") == ("Cloud :   hello", False)
    assert local_privacy.split_override(f"cloud: my token {key}") == (f"my token {key}", True)
    assert local_privacy.split_override("cloud: use the ctx", None, f"ctx {key}") == ("use the ctx", True)
    assert local_privacy.split_override(f"no prefix {key}") == (f"no prefix {key}", False)
    assert local_privacy.split_override(None) == (None, False)


def test_history_with_a_secret_shape_is_counted_by_category_only():
    key = fx.real_shaped("github_token", 14)
    msgs = [{"role": "user", "content": f"my token is {key}"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": f"again {key}"}]}]
    local_privacy.note_history_secrets(msgs)
    assert dict(local_privacy.secret_counts) == {"history_secret_shape:GitHub token": 1}   # once per category
    local_privacy.secret_counts.clear()
    local_privacy.note_history_secrets([{"role": "user", "content": "nothing here"}])
    local_privacy.note_history_secrets(None)
    local_privacy.note_history_secrets("not a list")
    assert dict(local_privacy.secret_counts) == {}


def test_assistant_tool_call_arguments_are_not_scanned():
    key = fx.real_shaped("github_token", 15)
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": f'{{"t": "{key}"}}'}}]}]
    assert local_privacy.scan_secrets(messages=msgs) == []     # documented residual (SECURITY.md)
    local_privacy.note_history_secrets(msgs)
    assert dict(local_privacy.secret_counts) == {}
