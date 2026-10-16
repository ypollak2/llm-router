"""P2.13-A: the deterministic English + Hebrew grammar of `llm-router prefer`.

The 20 phrasings (and 4 refusals) live in tests/fixtures/prefer_phrasings.json,
committed before the grammar so the grammar could not be fitted to its own
tests. Expected rules are compared for exact equality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router import prefer_grammar as g

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "prefer_phrasings.json").read_text("utf-8"))
PHRASINGS = FIXTURE["phrasings"]
REFUSED = FIXTURE["refused"]


def _corpus_failures() -> list[str]:
    """Ids of phrasings that do not parse to their expected rules."""
    bad = []
    for item in PHRASINGS:
        result = g.parse(item["text"])
        if result.refusal is not None or result.rules != item["rules"]:
            bad.append(item["id"])
    return bad


def test_corpus_sizes_are_the_preregistered_ones():
    assert len(PHRASINGS) == 20
    assert len(REFUSED) == 4
    assert sum(1 for p in PHRASINGS if p["id"].startswith("EN-")) == 12
    assert sum(1 for p in PHRASINGS if p["id"].startswith("HE-")) == 8


@pytest.mark.parametrize("item", PHRASINGS, ids=[p["id"] for p in PHRASINGS])
def test_phrasing_parses_to_the_expected_rules(item):
    result = g.parse(item["text"])
    assert result.refusal is None
    assert result.rules == item["rules"]
    assert len(result.echo) == len(result.rules)
    assert all(item["text"] in line for line in result.echo)


def test_all_twenty_pass_together():
    assert _corpus_failures() == []


@pytest.mark.parametrize("text", REFUSED)
def test_refused_phrasing_gives_examples_and_no_rules(text):
    result = g.parse(text)
    assert result.rules == []
    assert result.refusal
    for example in g.EXAMPLES_EN + g.EXAMPLES_HE:
        assert example in result.refusal


@pytest.mark.parametrize("text", [
    "", "   ", "claude", "faster", "codex before", "before claude", "never", "no ",
    "cap claude", "cap claude 0%", "cap claude 101%", "cap codex 0/day", "only local for dancing",
    "codex before nonesuch", "no nonesuch", "prefer codex", "codex before claude before codex",
    "avoid gemini", "הגבל את claude ל-0%", "רק מקומי לריקוד", "בלי",
])
def test_other_unsupported_input_is_refused(text):
    result = g.parse(text)
    assert result.refusal and result.rules == []


def test_unknown_target_lists_known_names():
    refusal = g.parse("codex before nonesuch").refusal
    assert "nonesuch" in refusal and "claude" in refusal and "ollama" in refusal


@pytest.mark.parametrize("text, expected", [
    ("no paid apis", [{"kind": "no_paid_api"}]),
    ("Please No Paid API.", [{"kind": "no_paid_api"}]),
    ("prefer codex to claude", [{"kind": "order", "first": {"providers": ["codex"], "model": None},
                                 "then": {"providers": ["anthropic"], "model": None}}]),
    ("codex ahead of claude", [{"kind": "order", "first": {"providers": ["codex"], "model": None},
                                "then": {"providers": ["anthropic"], "model": None}}]),
    ("codex first, then claude", [{"kind": "order", "first": {"providers": ["codex"], "model": None},
                                   "then": {"providers": ["anthropic"], "model": None}}]),
    ("cap codex 20 per day", [{"kind": "cap", "target": {"providers": ["codex"], "model": None},
                               "unit": "day", "n": 20}]),
    ("do not use opus", [{"kind": "ban", "target": {"providers": ["anthropic"], "model": "claude-opus*"}}]),
    ("never claude sonnet", [{"kind": "ban", "target": {"providers": ["anthropic"], "model": "claude-sonnet*"}}]),
    ("no groq", [{"kind": "ban", "target": {"providers": ["groq"], "model": None}}]),
    ("only local for research", [{"kind": "local_only", "task": "research"}]),
    ("בלי תשלום", [{"kind": "no_paid_api"}]),
    ("בלי API משלם", [{"kind": "no_paid_api"}]),
    ("מקסימום 20 ביום ל codex", [{"kind": "cap", "target": {"providers": ["codex"], "model": None},
                                  "unit": "day", "n": 20}]),
    ("claude עד 80%", [{"kind": "cap", "target": {"providers": ["anthropic"], "model": None},
                        "unit": "pct", "n": 80}]),
    ("אל תשתמש ב-openai", [{"kind": "ban", "target": {"providers": ["openai"], "model": None}}]),
    ("לעולם לא gemini cli", [{"kind": "ban", "target": {"providers": ["gemini_cli"], "model": None}}]),
    ("רק לוקאלי לשאלות", [{"kind": "local_only", "task": "query"}]),
    ("הייקו לפני אופוס", [{"kind": "order",
                           "first": {"providers": ["anthropic"], "model": "claude-haiku*"},
                           "then": {"providers": ["anthropic"], "model": "claude-opus*"}}]),
    ("בקלוד אופוס לא", None),
])
def test_additional_forms(text, expected):
    result = g.parse(text)
    if expected is None:
        assert result.refusal
    else:
        assert result.refusal is None and result.rules == expected


def test_normalize_strips_niqqud_bidi_and_unifies_quotes():
    assert g.normalize("אַף פַּעַם לֹא opus") == "אף פעם לא opus"
    assert g.normalize("‏codex לפני claude‎") == "codex לפני claude"
    assert g.normalize("‪codex‬  BEFORE\tClaude") == "codex before claude"
    assert g.normalize("ג׳מיני") == "ג'מיני"
    assert g.normalize("don’t use gemini") == "don't use gemini"
    assert g.normalize("ל־80%") == "ל-80%"
    assert g.normalize("Claude,  Codex.") == "claude codex"


def test_decorated_hebrew_and_quotes_parse_like_plain():
    gem = [{"kind": "ban", "target": {"providers": ["gemini", "gemini_cli"], "model": None}}]
    assert g.parse("לא להשתמש ב־‏ג׳מיני").rules == gem
    assert g.parse("לֹא לְהִשְׁתַּמֵּשׁ בְּ-ג'מיני").rules == gem
    assert g.parse("never use’ gemini").refusal  # "never use" is not a listed form
    assert g.parse("don’t use gemini").rules == gem


def test_prefix_is_stripped_only_when_the_rest_is_a_known_word():
    assert g._strip_prefix("לקוד") == "קוד"
    assert g._strip_prefix("בקלוד") == "קלוד"
    assert g._strip_prefix("ב-gemini") == "gemini"
    assert g._strip_prefix("והמקומי") == "מקומי"
    assert g._strip_prefix("בלי") == "בלי"        # ב + לי: not a known word
    assert g._strip_prefix("לפני") == "לפני"
    assert g._strip_prefix("הייקו") == "הייקו"    # itself known; ה+ייקו is not
    assert g._strip_prefix("לזמן") == "לזמן"
    assert g._strip_prefix("lcode") == "lcode"     # Latin is never stripped
    assert g._strip_prefix("") == ""


def test_other_providers_exist_in_the_setup_registry():
    from llm_router.tools.setup import _PROVIDER_REGISTRY

    assert set(g.OTHER_PROVIDERS) <= set(_PROVIDER_REGISTRY)


def test_parse_never_raises_on_odd_input():
    for text in ("\x00", "💥", "a" * 5000, "‎‏", None):
        assert g.parse(text).rules == []  # type: ignore[arg-type]


# ── mutant G3: Hebrew prefix stripping removed ───────────────────────────────

def test_mutant_g3_prefix_stripping_removed_turns_the_corpus_red(monkeypatch):
    assert _corpus_failures() == []
    monkeypatch.setattr(g, "_strip_prefix", lambda word: word)
    failing = _corpus_failures()
    assert failing, "mutant G3 survived: no phrasing depends on prefix stripping"
    assert "HE-20" in failing
