# SPDX-License-Identifier: MIT
"""Tests for the Firewall-v2 SHA-256 contamination audit."""

from __future__ import annotations

from llm_router.contamination_audit import audit, normalize, prompt_hash


def test_normalize_is_idempotent():
    x = "  Hello   WORLD \n"
    assert normalize(x) == normalize(normalize(x))
    assert normalize(x) == "hello world"


def test_hash_collides_under_reformatting():
    # Case, whitespace, and unicode-form differences must collide.
    assert prompt_hash("Solve  X=2") == prompt_hash("solve x=2")
    assert prompt_hash("a\tb") == prompt_hash("a b")


def test_clean_when_disjoint():
    rep = audit(["write a function", "what is a pointer"], ra_prompts=["prove the theorem"])
    assert rep.mode == "compared"
    assert rep.clean is True
    assert rep.overlap_count == 0


def test_contamination_detected():
    train = ["Prove the theorem", "explain recursion"]
    ra = ["prove the  theorem", "unrelated"]  # first collides after normalization
    rep = audit(train, ra_prompts=ra)
    assert rep.clean is False
    assert rep.overlap_count == 1
    assert len(rep.overlap_sample) == 1
    assert rep.overlap_sample[0] == prompt_hash("Prove the theorem")


def test_ra_hashes_take_precedence():
    train = ["hello world"]
    rep = audit(train, ra_hashes={prompt_hash("hello world")}, ra_prompts=["something else"])
    assert rep.clean is False  # used ra_hashes, not ra_prompts


def test_by_construction_mode():
    rep = audit(["only self generated"])
    assert rep.mode == "by_construction"
    assert rep.clean is True
    assert rep.ra_hash_count == 0
