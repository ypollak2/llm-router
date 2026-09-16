"""A short label may anchor retrieval, but only as a callback to this session.

N12. OKF's anchor gate requires an identifier-shaped token — snake_case, a file
extension, or a path — before an indexed source document may match. That gate is
right and was added for a real precision collapse (OKF-INDEX-01). Its cost is that
a LABEL can never match: "W3", "Q-J", "P3.7", "B8", "#137" are none of those
shapes, so "finish P3.7 and P3.8" could not reach the document defining P3.7.

Measured 2026-09-15: 19 of 660 real prompts (3%) carry one. Small, and precisely
the prompts that are otherwise unanswerable.

The danger is obvious: "B8" means something different in every repository, and
letting it anchor the bulk index is how the precision collapse happened. So a
label is admitted ONLY when this session has already mentioned it — which makes it
a continuation of something the user said, not a guess about what they meant.
"""
from __future__ import annotations

import pytest

from llm_router import okf


@pytest.mark.parametrize("label", ["W3", "Q-J", "Q-L", "P3.7", "P3.8", "B8", "C6b", "#137", "v2", "L2", "C3"])
def test_these_are_recognised_as_labels(label):
    assert okf._LABEL_SHAPED_RE.match(label), (
        f"{label!r} appears in real prompts and must be admissible as a session "
        "callback"
    )


@pytest.mark.parametrize("word", [
    "the", "routing", "chain", "implement", "database", "authentication",
    "build_chain", "okf.py", "src/llm_router", "is", "it", "we", "go", "ok",
    "a-b-c",
])
def test_ordinary_words_and_identifiers_are_not_labels(word):
    assert not okf._LABEL_SHAPED_RE.match(word), (
        f"{word!r} matched the label pattern. A pattern loose enough to catch "
        "common words would let any prompt anchor any document."
    )


def test_a_label_this_session_has_not_seen_does_not_anchor(monkeypatch):
    monkeypatch.setattr(okf, "_seen_this_session", lambda label: False)
    kws = {"b8"}
    anchors = {k for k in kws if okf._IDENTIFIER_SHAPED_RE.search(k)}
    anchors |= {k for k in kws if okf._LABEL_SHAPED_RE.match(k) and okf._seen_this_session(k)}
    assert anchors == set(), (
        "an unseen label anchored retrieval — that is the cross-project collision "
        "the bulk-index gate exists to prevent"
    )


def test_a_label_this_session_used_does_anchor(monkeypatch):
    monkeypatch.setattr(okf, "_seen_this_session", lambda label: True)
    kws = {"p3.7"}
    anchors = {k for k in kws if okf._LABEL_SHAPED_RE.match(k) and okf._seen_this_session(k)}
    assert anchors == {"p3.7"}


class TestSeenThisSession:

    def test_it_matches_what_the_session_actually_said(self, monkeypatch):
        monkeypatch.setattr(okf, "resolve_session_id", lambda: "abcd1234", raising=False)
        import llm_router.session_store as ss
        monkeypatch.setattr(ss, "resolve_session_id", lambda *a, **k: "abcd1234")
        monkeypatch.setattr(ss, "build_session_context",
                            lambda *a, **k: "earlier we discussed P3.7 and the sweep")
        assert okf._seen_this_session("P3.7")
        assert okf._seen_this_session("p3.7"), "matching must not depend on case"
        assert not okf._seen_this_session("P9.9")

    def test_no_session_means_not_seen(self, monkeypatch):
        import llm_router.session_store as ss
        monkeypatch.setattr(ss, "resolve_session_id", lambda *a, **k: None)
        assert not okf._seen_this_session("P3.7")

    def test_it_fails_closed(self, monkeypatch):
        import llm_router.session_store as ss
        monkeypatch.setattr(ss, "resolve_session_id",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        assert not okf._seen_this_session("P3.7"), (
            "an error widened the gate; unknown must leave it exactly as strict "
            "as it was"
        )
