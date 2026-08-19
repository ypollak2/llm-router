"""TST-001 regression: prevent silent test exclusion via collect_ignore.

Pre-fix, ``tests/conftest.py`` carried a 9-entry ``collect_ignore`` list
that silently dropped 206 tests at collection time. The original
justification — that those files imported lineage symbols that did not
exist — was stale: the symbols were restored in commit ``5c6c386``
(PR #10), but the exclusion list was never cleaned up. The README's
"766 tests passing" badge ran against a suite that excluded integrity,
performance, observability, session-summary rendering, framework
scenarios, and lineage roundtrips.

This meta-test ensures the exclusion list stays empty. If a future
change reintroduces silent skips, this test must fail with a message
that explains why broad exclusion is unsafe — and that the project
prefers per-test, reason-tagged skips (the ``_KNOWN_BROKEN_TESTS``
mechanism) so failures stay visible.

See: Docs/audit/HIGH_PRIORITY_WORK_PLAN.md F-TST-001
"""
from __future__ import annotations


def test_collect_ignore_is_empty() -> None:
    """``conftest.collect_ignore`` must remain empty (TST-001).

    Adding a path to ``collect_ignore`` removes the file from the test
    universe entirely — no skip marker, no reason, no record of why the
    file was dropped. Prefer ``_KNOWN_BROKEN_TESTS`` (substring + reason
    pairs) so failures remain visible at ``pytest -v`` time and a future
    reader can see why each skip exists.
    """
    from tests import conftest

    ignored = list(getattr(conftest, "collect_ignore", []))
    assert ignored == [], (
        "tests/conftest.py::collect_ignore must remain empty (TST-001). "
        "To skip an individual test, add (substring, reason) to "
        "_KNOWN_BROKEN_TESTS instead — that path keeps the skip visible "
        "in pytest -v output. Found stale exclusions: "
        f"{ignored}"
    )


def test_known_broken_entries_carry_reasons() -> None:
    """Every ``_KNOWN_BROKEN_TESTS`` entry must document *why* it skips.

    The whole point of the per-test skip mechanism is that the reason
    travels with the skip marker. An empty reason would defeat that
    contract.
    """
    from tests.conftest import _KNOWN_BROKEN_TESTS

    bad: list[str] = []
    for entry in _KNOWN_BROKEN_TESTS:
        substring, reason = entry
        if not isinstance(substring, str) or not substring.strip():
            bad.append(f"empty substring in entry: {entry!r}")
        if not isinstance(reason, str) or not reason.strip():
            bad.append(f"empty reason for substring {substring!r}")

    assert not bad, (
        "Every _KNOWN_BROKEN_TESTS entry must carry a non-empty "
        "substring AND a non-empty reason. Issues found:\n  - "
        + "\n  - ".join(bad)
    )
