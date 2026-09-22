"""R8 — capture is on, and it is asked for.

The audit's finding was not that prompt capture is dangerous. It was that the
product CLAIMED to preserve task success while the only mechanism that could
measure that was off by default — so the claim rested on nothing.

Two ways out were defensible: turn capture on and measure, or drop the claim.
The decision taken was to turn it on, WITH EXPLICIT CONSENT AT INSTALL, because
the thing being switched on reads the operator's prompts.

The properties that make that honest rather than a formality, each asserted
below:

  SILENCE IS REFUSAL      a non-interactive install records no consent and
                          capture stays off. Defaulting to on when nobody could
                          answer is consent manufactured by the absence of a
                          human.
  CONSENT IS AN EVENT     not a flag. The record carries when, which terms, and
                          what was said — because a flag cannot answer "did
                          anyone actually agree to this?", and that is the only
                          question that matters if it turns out they did not.
  TERMS ARE VERSIONED     changing what is captured changes what was agreed to.
  REFUSAL IS RECORDED     "they said no" and "they were never asked" are
                          different facts and must stay distinguishable.
  UNREADABLE IS NOT YES   a corrupt record fails closed.
"""

from __future__ import annotations

import json

import pytest

from llm_router import ground_truth_consent as gt


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    yield


def test_never_asked_is_not_consent():
    assert gt.read_consent() is None
    assert gt.has_current_consent() is False


def test_a_non_interactive_install_records_a_refusal_without_prompting():
    """The property that matters most. CI cannot agree to anything."""
    asked = []

    def _reader(_prompt):
        asked.append(_prompt)
        return "y"

    c = gt.ask(interactive=False, reader=_reader, printer=lambda *_: None)
    assert not asked, "it prompted in a non-interactive session"
    assert c.granted is False
    assert c.source == "non-interactive"
    assert gt.has_current_consent() is False


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("Y", True), ("yes", True), ("", True),   # default is yes
    ("n", False), ("no", False), ("N", False), ("x", False),
])
def test_the_answer_is_taken_literally(answer, expected):
    c = gt.ask(interactive=True, reader=lambda _p: answer, printer=lambda *_: None)
    assert c.granted is expected
    assert gt.has_current_consent() is expected


def test_an_interrupted_prompt_is_a_refusal():
    """Could not be asked -> was not asked -> no."""
    def _reader(_p):
        raise KeyboardInterrupt

    c = gt.ask(interactive=True, reader=_reader, printer=lambda *_: None)
    assert c.granted is False
    assert c.source == "non-interactive"


def test_consent_is_a_record_not_a_flag():
    gt.ask(interactive=True, reader=lambda _p: "y", printer=lambda *_: None)
    d = json.loads(gt.consent_path().read_text())
    for field in ("granted", "terms_version", "recorded_at", "source"):
        assert field in d, f"the consent record cannot answer '{field}'"
    assert d["recorded_at"] > 0


def test_changing_the_terms_invalidates_prior_consent(monkeypatch):
    """Consent to one thing is not consent to a different thing."""
    gt.ask(interactive=True, reader=lambda _p: "y", printer=lambda *_: None)
    assert gt.has_current_consent() is True

    monkeypatch.setattr(gt, "TERMS_VERSION", gt.TERMS_VERSION + 1)
    assert gt.has_current_consent() is False, (
        "consent given against older terms still counts under new ones"
    )


def test_revocation_is_immediate_and_recorded_as_a_refusal():
    gt.ask(interactive=True, reader=lambda _p: "y", printer=lambda *_: None)
    assert gt.has_current_consent() is True

    gt.revoke()
    assert gt.has_current_consent() is False
    c = gt.read_consent()
    assert c is not None and c.granted is False and c.source == "revoked", (
        "revocation deleted the record. 'They said no' and 'they were never "
        "asked' are different facts and both need to survive."
    )


def test_an_unreadable_record_fails_closed():
    gt.consent_path().parent.mkdir(parents=True, exist_ok=True)
    gt.consent_path().write_text("{ this is not json")
    assert gt.read_consent() is None
    assert gt.has_current_consent() is False


def test_the_terms_say_what_is_captured_and_how_to_stop_it():
    """A consent screen that does not say what it is asking for is not consent."""
    t = gt.TERMS
    for required in ("prompt", "~/.llm-router", "LOCAL ONLY",
                     "scrubbed", "revoke"):
        assert required in t, f"the terms do not mention {required!r}"
    assert "upload" in t.lower() or "server" in t.lower(), (
        "the terms do not address where the data goes, which is the first "
        "thing anyone reading them wants to know"
    )


def test_onboarding_asks_and_writes_the_flag_either_way():
    """AST, not source text.

    The flag must be written on BOTH branches: an existing `=1` from a previous
    install has to be turned OFF by a later refusal, not silently survive it.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/llm_router/commands/onboard.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = {ast.unparse(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    assert "ground_truth_consent.ask" in calls, (
        "onboarding no longer asks for consent"
    )

    # The env line is written unconditionally from a ternary, not inside an
    # `if granted:` branch.
    joined = ast.dump(tree)
    assert "LLM_ROUTER_GROUND_TRUTH=" in src.read_text(encoding="utf-8")
    fstrings = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.JoinedStr)
        and "LLM_ROUTER_GROUND_TRUTH" in ast.unparse(n)
    ]
    assert fstrings, "the flag is not written from onboarding"
    assert any(
        isinstance(v, ast.IfExp)
        for f in fstrings
        for n in ast.walk(f)
        for v in ([n] if isinstance(n, ast.IfExp) else [])
    ), (
        "LLM_ROUTER_GROUND_TRUTH is not written from a conditional expression, "
        "so it is probably written only when consent was granted — leaving a "
        "previous install's `=1` in place after a refusal"
    )
    assert joined  # keep the parse referenced
