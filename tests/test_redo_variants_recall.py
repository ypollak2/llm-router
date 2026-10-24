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
    # Review of PR #708, round 2.
    "i get the same error when the server restarts on prod; is that a known issue in nginx?",
    "that broke the build on staging yesterday too, before this pr. separate issue",
    "that made it worse for users on ie11 historically; add a note",
    "undo that last commit on the release branch, the release manager asked",
    "go back to the previous version of the spec doc and summarize what changed",
    "here is the snippet:\n```python\nx = compute()  # still wrong\n```\nadd a docstring to it",
    "> still wrong\n> that broke it\nwhat does the reviewer mean by this?",
    "~~~\nundo that\n~~~\nexplain this shell history",
    "i meant: also update the readme",
    "i meant, also add a changelog line",
    "revert that commit on the release branch",
    "I'm getting the same error in the staging logs from last week",
    # A bare "no" after "Want me to push it?" declines an offer (design section 1: y=0).
    "no",
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


# Tightened forms that must still flag (round 2): a rejection then punctuation or the end, the last
# change named, a short "that broke ...", and a rejection after pasted (fenced or quoted) output.
TIGHTENED_POSITIVES = (
    "undo that.",
    "undo that, it broke the tests",
    "undo your change",
    "revert it!",
    "revert your last",
    "go back to the previous version, then run the tests",
    "go back to how it was.",
    "roll that back, please",
    "that broke it",
    "that broke your fix",
    "ugh, that broke the requested behavior",
    "```\nTraceback: synthetic\n```\nstill wrong",
    "> synthetic quoted output\nI still get the same error",
    "i meant the second file: also it should be csv",
)


def test_tightened_forms_still_flag():
    for prompt in TIGHTENED_POSITIVES:
        assert rs._signals(prompt, PREVIOUS, "v2") == [], prompt
        for variant in ("v3c", "v3d"):
            assert rs._signals(prompt, PREVIOUS, variant) == [rs.SIGNAL_CONTRADICTION], (variant, prompt)


def test_short_form_word_limits_round_2():
    eight = "ok so that broke the requested csv output"
    nine = "ok so that broke the requested csv output again"
    assert len(rs._words(eight)) == rs.V3C_SHORT_MAX_WORDS
    assert rs._signals(eight, PREVIOUS, "v3c") == [rs.SIGNAL_CONTRADICTION]
    assert rs._signals(nine, PREVIOUS, "v3c") == []
    six_after = "I still get the same error after your fix to the parser"
    seven_after = "I still get the same error after your fix to the csv parser"
    assert rs._signals(six_after, PREVIOUS, "v3c") == [rs.SIGNAL_CONTRADICTION]
    assert rs._signals(seven_after, PREVIOUS, "v3c") == []
    # The ending rule sees the whole prompt: a cut window never fakes an end of prompt.
    padded = "x" * (rs.FAILURE_WINDOW - 30) + " i get the same error " + "when " + "y" * 200
    assert rs._signals(padded, PREVIOUS, "v3c") == []


def test_bare_no_declining_an_offer_is_not_a_redo():
    entries = [
        {"type": "user", "message": {"content": "commit the synthetic fix"}},
        {"type": "assistant", "message": {"content": "Committed. Want me to push it?"}},
        {"type": "user", "message": {"content": "no"}},
    ]
    for variant in ("v2", "v3c", "v3d"):
        assert rs.redo_flags(entries, variant) == [], variant
    assert not rs._short_negative_v3d("no") and not rs._short_negative_v3d("nope")


def test_unquoted_strips_fences_and_quote_lines_only():
    assert rs._unquoted("a\n```\nstill wrong\n```\nb") == "a\n\n\nb"
    assert rs._unquoted("a\n~~~\nx\n~~~") == "a"
    assert rs._unquoted("a\n```\nunclosed still wrong") == "a"
    assert rs._unquoted("  > quoted\nkept > not a quote") == "kept > not a quote"


_TOKENS = ("still", "wrong", "same", "error", "i", "get", "getting", "undo", "revert", "that", "this",
           "it", "go", "back", "to", "the", "previous", "version", "and", "summarize", "no", "nope",
           "meant", "also", ":", ",", ".", "when", "on", "broke", "made", "worse", "your", "change",
           "claude:", "please", "x", "fix", "didn't", "work", "\n")
_FENCED = ("still wrong", "undo that", "that broke it", "i still get the same error", "no",
           "go back to the previous version", "that's not what i asked", "try again")


def test_property_random_prompts_with_fences_keep_superset_and_ignore_quoted_text():
    """Seeded random prompts (N below): v2 <= v3c <= v3d signal by signal, including prompts with
    fences and quote lines; v3c's own addition never depends on fenced or quoted text."""
    import random

    rng = random.Random(708)
    n = 20_000
    checked = 0
    for _ in range(n):
        body = " ".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 14))).replace("\n ", "\n")
        hidden = rng.choice(_FENCED)
        fence = rng.choice(("```", "~~~"))
        for prompt in (body, f"{fence}\n{hidden}\n{fence}\n{body}", f"> {hidden}\n{body}",
                       f"{body}\n{fence}\n{hidden}"):
            previous = rng.choice((PREVIOUS, prompt))
            v2, v3c, v3d = (rs._signals(prompt, previous, v) for v in ("v2", "v3c", "v3d"))
            assert set(v2) <= set(v3c) <= set(v3d), prompt
            assert [s for s in v3c if s in v2] == v2, prompt
            checked += 1
        if body.lstrip().startswith(">"):
            continue
        plain = rs._redo_phrase_v3c(rs._unquoted(body.replace("’", "'").lstrip().lower()))
        for wrapped in (f"{fence}\n{hidden}\n{fence}\n{body}", f"> {hidden}\n{body}"):
            assert rs._redo_phrase_v3c(rs._unquoted(wrapped.lower())) == plain, wrapped
    assert checked == 4 * n
