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
    assert "evil.example" not in out.text and "img.example" not in out.text and "x.example" not in out.text
    assert "cdn.example" not in out.text and "data:image" not in out.text
    assert "the docs" in out.text and "chart" in out.text and "here" in out.text
    assert "[local](docs/a.md)" in out.text and "[anchor](#top)" in out.text and "![inline](./local.png)" in out.text
    assert out.urls == 7 and out.text.endswith("end")  # link, 3 images, <img>, <a>, the reference definition


def test_a_bare_url_in_prose_is_left_as_text():
    assert sanitize.clean("see https://example.com/page for more", pr_body=True).text == "see https://example.com/page for more"


def test_long_runs_of_spaces_do_not_make_the_closing_keyword_pattern_quadratic():
    # If this backtracked it would take minutes: finishing is the assertion (the suite's own timeout is the clock).
    out = sanitize.clean("Fixes" + " " * 400_000 + "#1", pr_body=True)
    assert out.closing == 0 and out.text.endswith("#1")
    assert sanitize.clean("Fixes   #1", pr_body=False).text == "refs   #1"
