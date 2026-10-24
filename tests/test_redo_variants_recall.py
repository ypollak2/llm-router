"""Recall candidates from the label definition only; all prompts are synthetic."""
from __future__ import annotations

from llm_router import redo_signal as rs

PREVIOUS = "explain the synthetic cache"
REPEAT = "list every function in the synthetic billing module and explain each one"

# Phrases absent from v2, covering every genuinely new regex branch. Some requested families
# already match v2 and are exercised separately below.
NEW_PHRASES = (
    "still wrong",
    "still not fixed",
    "still not the requested file",
    "still isn't the requested format",
    "I meant the second file",
    "same error",
    "same problem",
    "same issue",
    "same result",
    "Same error again.",
    "the same error as before",
    "still getting the same error",
    "I get the same error",
    "I'm getting the same error after your fix",
    "I still get the same problem",
    "undo that",
    "undo this",
    "please undo that",
    "revert that",
    "revert it",
    "revert this",
    "revert the last change",
    "revert your change",
    "revert your last edit",
    "please revert that",
    "can you revert this",
    "just revert it",
    "can you please revert that",
    "go back to the original request",
    "please go back to the original request",
    "can you go back to the original request",
    "go back to the previous version",
    "go back to your earlier approach",
    "go back to how it was",
    "roll that back",
    "that broke the requested behavior",
    "that made it worse",
)

BASELINE_PHRASES = (
    "that's not what I asked",
    "that's not what I wanted",
    "that's not what I meant",
    "that's not what I said",
    "not what I asked",
    "no, I meant the second file",
    "that didn't work",
    "it didn't work",
    "that did not work",
    "it did not work",
    "you didn't include the second part",
    "you did not include the second part",
    "still doesn't produce the requested format",
    "try again",
    "please run that again",
    "that is wrong",
    "you missed the second part",
)

NEGATIVES = (
    "the CLI crashes when I run install with no config",
    "the proxy fails on startup after the update",
    "tests fail in CI on windows",
    "add a unit test for the parser",
    "now implement the export command",
    "also add a test",
    "ok thanks",
    "looks good, commit it",
    "not now, later",
    "not now",
    "not yet, later",
    "not yet",
    "not today",
    "not really",
    "not sure yet, let me think",
    "thanks for the explanation",
    "yes",
    "continue",
    "ok",
    "the log mentions revert that after the update",
    "the documentation says go back to the menu",
    "the product has the same configuration as before",
    "revert documentation explains the option",
    "revert a configuration entry",
    "undo a migration",
    "go back tomorrow",
    "noteworthy improvement",
    "wrongly formatted logs are a product bug",
    "stillness is a useful concept",
    "not a priority later",
    "not a priority until much later",
)

# v2 already flags these benign prompts (opening "no" or the phrase "not right"). Removing them would
# contradict the required superset property. The new short-negative rule itself excludes them.
INHERITED_BENIGN = (
    "not right now",
    "no worries",
    "no problem, thanks",
    "no thanks",
    "no thank you",
    "no need",
    "no rush",
    "no, thanks",
    "no, thank you",
    "no later",
)

SHORT_NEGATIVES = (
    "no",
    "wrong",
    "not that one",
    "not quite",
    "not it",
)

# Review of PR #708: product reports, new questions, status and refinements that the first v3c/v3d
# draft flagged. Design section 1 labels each 0, so no variant may flag them.
PRECISION_NEGATIVES = (
    "same error appears when the server restarts",
    "is this the same issue as #123?",
    "the same result is expected for both",
    "go back to the plan and do task 3",
    "I meant to ask: does it support Windows?",
    "still not merged",
    "still not sure",
    "revert your branch before rebasing",
    "still, can you also add a --json flag",
    "still need to push it",
    "still on it?",
    "not merged yet",
)

# Planned positives dropped by the tightening, kept here so the choice is explicit: each needs a token
# the tightened patterns no longer accept ("the same error remains" is not a rejection framing on its own;
# bare "still" is a status word), and precision is the binding gate constraint.
DROPPED_BY_TIGHTENING = (
    "Good, but the same error remains; fix the second part",
    "still the old file",
)


def test_variant_registry_and_frozen_default():
    assert set(rs.VARIANTS) == {"v2", "v3a", "v3b", "v3c", "v3d"}
    assert rs.PATTERN_VERSION == rs.BASELINE_VARIANT == "v2"
    assert rs.V3D_SHORT_MAX_WORDS == 6
    for prompt in (NEW_PHRASES + BASELINE_PHRASES + NEGATIVES + INHERITED_BENIGN + SHORT_NEGATIVES
                   + PRECISION_NEGATIVES + DROPPED_BY_TIGHTENING):
        assert rs._signals(prompt, PREVIOUS) == rs._signals(prompt, PREVIOUS, "v2")
    assert "reask" not in rs.VARIANTS["v3c"]


def test_new_phrase_families_raise_recall():
    for prompt in NEW_PHRASES:
        assert rs._signals(prompt, PREVIOUS, "v2") == [], prompt
        for variant in ("v3c", "v3d"):
            assert rs._signals(prompt, PREVIOUS, variant) == [rs.SIGNAL_CONTRADICTION], prompt


def test_overlapping_phrase_families_keep_one_contradiction():
    for prompt in BASELINE_PHRASES:
        assert rs._signals(prompt, PREVIOUS, "v2") == [rs.SIGNAL_CONTRADICTION], prompt
        for variant in ("v3c", "v3d"):
            assert rs._signals(prompt, PREVIOUS, variant) == [rs.SIGNAL_CONTRADICTION], prompt


def test_product_reports_new_tasks_and_acknowledgements():
    for prompt in NEGATIVES:
        for variant in ("v2", "v3c", "v3d"):
            assert rs._signals(prompt, PREVIOUS, variant) == [], (variant, prompt)


def test_precision_negatives_from_review_never_flag():
    for prompt in PRECISION_NEGATIVES + DROPPED_BY_TIGHTENING:
        assert not rs._short_negative_v3d(prompt.lower()), prompt
        for variant in ("v2", "v3c", "v3d"):
            assert rs._signals(prompt, PREVIOUS, variant) == [], (variant, prompt)


def test_v2_signals_survive_a_new_phrase_hit():
    """A prompt that fires v2's reask or repeat AND a new v3c phrase keeps every v2 signal
    (kills the mutation that replaces the signal list with [contradiction])."""
    long_redo = "still wrong: list every function in the synthetic billing module and explain each one"
    cases = [
        ("claude: still wrong", PREVIOUS, ["reask"]),
        ("native: still getting the same error", PREVIOUS, ["reask"]),
        ("opus: still not fixed", PREVIOUS, ["reask"]),
        (long_redo, long_redo, ["repeat"]),
        ("claude: " + long_redo, "claude: " + long_redo, ["reask", "repeat"]),
    ]
    for prompt, previous, baseline in cases:
        assert rs._signals(prompt, previous, "v2") == baseline, prompt
        for variant in ("v3c", "v3d"):
            got = rs._signals(prompt, previous, variant)
            assert set(baseline) <= set(got), (variant, prompt)
            assert rs.SIGNAL_CONTRADICTION in got, (variant, prompt)
    assert rs._signals("wrong", PREVIOUS + " x", "v3d") == [rs.SIGNAL_CONTRADICTION]
    assert rs._signals("claude: nope", PREVIOUS, "v3d") == ["reask"]


def test_short_negative_addition_and_benign_exclusions():
    for prompt in SHORT_NEGATIVES:
        assert rs._signals(prompt, PREVIOUS, "v3c") == [], prompt
        assert rs._signals(prompt, PREVIOUS, "v3d") == [rs.SIGNAL_CONTRADICTION], prompt
    for prompt in ("nope", "still wrong", "no that's the old file"):
        assert rs._signals(prompt, PREVIOUS, "v3d") == [rs.SIGNAL_CONTRADICTION], prompt
    for prompt in INHERITED_BENIGN:
        assert not rs._short_negative_v3d(prompt), prompt
        for variant in ("v2", "v3c", "v3d"):
            assert rs._signals(prompt, PREVIOUS, variant) == [rs.SIGNAL_CONTRADICTION], prompt
    for prompt in NEGATIVES:
        assert not rs._short_negative_v3d(prompt), prompt


def test_short_negative_word_limit_and_token_boundaries():
    six = "not that file that was requested"
    seven = "not that file that was originally requested"
    assert len(rs._words(six)) == rs.V3D_SHORT_MAX_WORDS
    assert len(rs._words(seven)) == rs.V3D_SHORT_MAX_WORDS + 1
    assert rs._signals(six, PREVIOUS, "v3d") == [rs.SIGNAL_CONTRADICTION]
    assert rs._signals(seven, PREVIOUS, "v3d") == []
    # "no " is already a v2 contradiction at any length; only the new rule has a word cap.
    long_no = "no this is a completely different task now"
    assert not rs._short_negative_v3d(long_no)
    assert rs._signals(long_no, PREVIOUS, "v3d") == rs._signals(long_no, PREVIOUS, "v2")
    assert rs._signals(" \nWRONG", PREVIOUS, "v3d") == [rs.SIGNAL_CONTRADICTION]


def test_phrase_window_normalisation_and_command_anchoring():
    for variant in ("v3c", "v3d"):
        assert rs._signals("Good, but that’s not what I said", PREVIOUS, variant) == [
            rs.SIGNAL_CONTRADICTION]
        assert rs._signals(" \nPLEASE REVERT THAT", PREVIOUS, variant) == [rs.SIGNAL_CONTRADICTION]
        for phrase in ("still getting the same error", "that made it worse", "still wrong"):
            inside = "x" * (rs.FAILURE_WINDOW - len(phrase) - 1) + " " + phrase
            outside = "x" * rs.FAILURE_WINDOW + " " + phrase
            assert rs._signals(inside, PREVIOUS, variant) == [rs.SIGNAL_CONTRADICTION]
            assert rs._signals(outside, PREVIOUS, variant) == []
        for command in ("revert that", "undo this", "go back to", "roll that back"):
            assert rs._signals("the product documentation mentions " + command, PREVIOUS, variant) == []


def test_mixed_pairs_are_strict_supersets_including_transcript_indices():
    pairs = [(prompt, PREVIOUS) for prompt in (
        NEW_PHRASES + BASELINE_PHRASES + NEGATIVES + INHERITED_BENIGN + SHORT_NEGATIVES
        + PRECISION_NEGATIVES)]
    pairs.extend((prefix + "explain the synthetic cache", PREVIOUS)
                 for prefix in ("claude: ", "native: ", "opus: "))
    # v2 reask or repeat combined with a new phrase: the per-pair superset must hold signal by signal.
    pairs.extend((prefix + phrase, PREVIOUS)
                 for prefix in ("claude: ", "native: ", "opus: ") for phrase in NEW_PHRASES[:5])
    pairs.extend((phrase + " " + REPEAT, phrase + " " + REPEAT) for phrase in NEW_PHRASES[:5])
    pairs.extend([(REPEAT, REPEAT), (REPEAT + " now", REPEAT), ("ok", "ok")])
    assert len(pairs) >= 40
    flagged = {variant: set() for variant in ("v2", "v3c", "v3d")}
    for index, (prompt, previous) in enumerate(pairs):
        baseline = rs._signals(prompt, previous, "v2")
        broader = rs._signals(prompt, previous, "v3c")
        broadest = rs._signals(prompt, previous, "v3d")
        assert set(baseline) <= set(broader) <= set(broadest), prompt
        assert [s for s in broader if s in baseline] == baseline, prompt
        for variant in flagged:
            flagged[variant].update((index, signal) for signal in rs._signals(prompt, previous, variant))
        entries = [
            {"type": "user", "message": {"content": previous}},
            {"type": "assistant", "message": {"content": "synthetic answer"}},
            {"type": "user", "message": {"content": prompt}},
        ]
        assert set(rs.redo_flags(entries, "v2")) <= set(rs.redo_flags(entries, "v3c"))
        assert set(rs.redo_flags(entries, "v3c")) <= set(rs.redo_flags(entries, "v3d"))
    assert flagged["v2"]
    assert flagged["v2"] < flagged["v3c"] < flagged["v3d"]
    for variant in ("v3c", "v3d"):
        first_only = [{"type": "user", "message": {"content": "still wrong"}}]
        assert rs.redo_flags(first_only, variant) == []
