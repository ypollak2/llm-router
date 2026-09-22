"""R17 — a worthless verifier reached HIGH confidence.

`len(answer) > 5` was validated against three short bad answers, caught all
three, scored `detected == total`, and was classified HIGH. It then accepted a
confidently wrong long answer. Real kill rate on the corpus: **0%**.

Nothing in the validator was broken. The probe-set floor (`MIN_BAD_ANSWERS`,
distinctness, not-equal-to-good) checks the shape of the OPERATOR'S probes, and
those can only ever demonstrate that a verifier rejects the wrong answers the
operator happened to think of. A length check passes that test honestly.

The missing question is whether the verifier rejects an answer it has no reason
to accept. `UNIVERSAL_DECOYS` asks it: fluent prose answering a different
question, a refusal, a bare token, long filler. A verifier that accepts any of
them is discriminating on some property that is not the task.

The two corpora below are the acceptance criterion from the plan: known-
worthless verifiers must not exceed LOW, known-good ones must still reach HIGH.
The second half matters as much as the first — a rule that caps everything at
LOW would pass the first corpus and destroy the mechanism.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth.mutants import (  # noqa: E402
    HIGH,
    LOW,
    UNUSABLE,
    UNIVERSAL_DECOYS,
    classify,
    validate_snippet_verifier,
)

#: Verifiers that measure something, but not the task.
#:
#: A verifier snippet is a SCRIPT, not a function: `run_verifier` appends it to
#: a preamble, runs it with the candidate answer in `BENCH_ANSWER`, and treats
#: exit code 0 as acceptance. The first draft of this file wrote `def
#: verify(answer): ...` snippets — which define a function, call nothing, and
#: therefore exit 0 for EVERY input. Every verifier in both corpora accepted
#: every decoy, which read as a broken decoy probe and was a broken fixture.
WORTHLESS = {
    "length": 'assert len(os.environ.get("BENCH_ANSWER", "")) > 5\n',
    "non_empty": 'assert os.environ.get("BENCH_ANSWER", "").strip()\n',
    "always_true": 'assert True\n',
    "word_count": 'assert len(_answer_words()) >= 2\n',
}

#: Verifiers keyed to the task: "What is 17 * 3? Answer in words." -> "fifty one"
GOOD = {
    "exact": 'assert os.environ["BENCH_ANSWER"].strip().lower() == "fifty one"\n',
    "contains": 'assert "fifty one" in os.environ["BENCH_ANSWER"].lower()\n',
    "normalised": (
        'a = os.environ["BENCH_ANSWER"].lower().replace("-", " ").split()\n'
        'assert a[:2] == ["fifty", "one"]\n'
    ),
}

#: The task is "What is 17 * 3? Answer in words." The good answer has to be
#: LONGER than the length check's threshold and the bad answers SHORTER, or
#: `len(answer) > 5` fails its own baseline and is rejected as UNUSABLE before
#: it can demonstrate the defect. That is exactly the configuration the audit
#: found in the wild, and the first draft of this file got it backwards.
#:
#: Two words, not "fifty-one": the preamble's `_answer_words` regex keeps the
#: hyphen, so the hyphenated form is ONE word and the word-count check failed
#: its baseline too. Both worthless verifiers have to CLEAR every existing rule
#: or they do not test the new one.
GOOD_ANSWER = "fifty one"
#: Three distinct short wrong answers — enough to clear the probe-set floor, so
#: confidence is decided by discrimination and not by probe shape.
BAD_ANSWERS = ["no", "42", "??"]


def _validate(snippet: str):
    v = validate_snippet_verifier(
        task_id="t", snippet=snippet,
        good_answer=GOOD_ANSWER, bad_answers=BAD_ANSWERS,
    )
    conf, why = classify(v, strategy_is_mechanical=True, contract_complete=True)
    return v, conf, why


@pytest.mark.parametrize("name", sorted(WORTHLESS))
def test_a_worthless_verifier_cannot_exceed_low(name):
    """The plan's criterion verbatim: cannot EXCEED low.

    UNUSABLE is also acceptable and is stricter — `always_true` and the
    non-empty check accept all three bad answers, so the existing
    `detected == 0` rule already rejects them outright and the decoy probe is
    not what stops them.
    """
    v, conf, why = _validate(WORTHLESS[name])
    assert conf in {LOW, UNUSABLE}, (
        f"{name!r} reached {conf}: {why}\n"
        f"decoys accepted: {v.accepted_decoys}"
    )


#: The two that PASS every existing rule — good baseline, all probes detected,
#: probe set well-formed, contract complete — and were therefore HIGH before
#: R17. These are the ones the decoy probe has to catch; the others were
#: already caught.
CAUGHT_ONLY_BY_DECOYS = ["length", "word_count"]


@pytest.mark.parametrize("name", CAUGHT_ONLY_BY_DECOYS)
def test_the_decoy_probe_is_what_catches_these(name):
    v, conf, why = _validate(WORTHLESS[name])
    assert v.baseline_passed is True, "premise: it accepts the good answer"
    assert v.detected == v.total == len(BAD_ANSWERS), (
        f"premise: {name!r} catches every probe the operator supplied — that "
        "was never the problem, and if it no longer does, this fixture has "
        "stopped demonstrating the defect"
    )
    assert v.weak_probe_set is None, "premise: the probe set is well-formed"
    assert conf == LOW and "wrong for every task" in why, (
        f"{name!r} reached {conf} ({why}) — every pre-R17 rule passes it, so "
        "without the decoy probe this is a HIGH-confidence worthless verifier"
    )


@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_real_verifier_still_reaches_high(name):
    """The half that stops the fix from being 'cap everything at LOW'."""
    v, conf, why = _validate(GOOD[name])
    assert v.accepted_decoys == [], (
        f"{name!r} accepted a universal decoy: {v.accepted_decoys}. Either the "
        "verifier is weaker than it looks, or a decoy is unfair and should be "
        "replaced."
    )
    assert conf == HIGH, f"{name!r} only reached {conf}: {why}"


def test_the_decoys_are_wrong_for_the_task_they_are_used_against():
    """Anti-vacuity: a decoy the good verifier would accept proves nothing."""
    exact = GOOD["exact"]
    from groundtruth.verifiers import run_verifier

    for decoy, why in UNIVERSAL_DECOYS:
        ok, _ = run_verifier(exact, decoy)
        assert not ok, f"decoy {decoy[:40]!r} is accepted by a correct verifier ({why})"
        assert GOOD_ANSWER not in decoy, (
            f"decoy {decoy[:40]!r} contains the good answer, so a content-keyed "
            "verifier would accept it and be wrongly capped at LOW"
        )


def test_a_verifier_that_crashes_on_a_decoy_is_not_punished():
    """An error is a rejection, not an acceptance.

    Treating a crash as acceptance would cap the strictest verifiers at LOW for
    being strict — the opposite of the intended effect.
    """
    strict = (
        'a = os.environ["BENCH_ANSWER"].strip().lower()\n'
        'if " " not in a:\n'
        '    raise ValueError("expected two number words")\n'
        'assert a == "fifty one"\n'
    )
    v, conf, why = _validate(strict)
    assert v.accepted_decoys == [], why
    assert conf == HIGH, f"a strict verifier was downgraded to {conf}: {why}"
