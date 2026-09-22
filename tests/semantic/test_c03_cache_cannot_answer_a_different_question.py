"""C-03 — the semantic cache must not answer a question it was not asked.

The 2026-09-21 audit measured, against this project's own embedding model
(`nomic-embed-text`) at its own shipped threshold (0.95):

    "retry 3 times"     vs "retry 30 times"     0.9925
    "timeout 30"        vs "timeout 300"        0.9903
    "increase by 10%"   vs "decrease by 10%"    0.9764

All three were cache hits. On a hit the model is never called, so the wrong
answer is returned fast, confidently, and is indistinguishable from a right one.
This is not hypothetical for this project: a recorded incident had one
passport's answer served for another.

The important part of the fix is what it is *not*. Raising the threshold cannot
solve this — the worst pair scores 0.9925, so any cutoff strict enough to
exclude it would exclude nearly every genuine duplicate too. Cosine similarity
over sentence embeddings measures topic, and magnitude and direction are exactly
what that representation compresses away. So the threshold is defence in depth
and `_discriminator` is the fix.

These tests are deliberately hermetic: no Ollama, no network. The guard is pure
and deterministic, which is the point of putting the correctness burden there
rather than on a similarity score.
"""

from __future__ import annotations

import json

import pytest

from llm_router import semantic_cache as sc


# The audit's measured pairs, as prompts. Each pair scored ABOVE the old 0.95
# threshold and was therefore served from cache.
COLLIDING_PAIRS = [
    ("retry 3 times", "retry 30 times", "magnitude"),
    ("set the timeout to 30", "set the timeout to 300", "magnitude"),
    ("increase the batch size by 10%", "decrease the batch size by 10%", "direction"),
]


@pytest.mark.parametrize("a, b, kind", COLLIDING_PAIRS)
def test_measured_collisions_are_vetoed(a, b, kind):
    """The three pairs the audit measured must never be treated as equivalent."""
    veto = sc._equivalence_veto(sc._discriminator(a), sc._discriminator(b))
    assert veto is not None, (
        f"{kind} collision not caught: {a!r} would still be answered with the "
        f"cached response for {b!r}"
    )


@pytest.mark.parametrize("a, b, kind", COLLIDING_PAIRS)
def test_veto_is_symmetric(a, b, kind):
    """Order must not decide correctness — whichever arrived first."""
    assert sc._equivalence_veto(sc._discriminator(b), sc._discriminator(a)) is not None


def test_genuine_duplicates_still_hit():
    """Anti-over-correction: the guard must not veto everything.

    A guard that refuses every pair would pass every test above while destroying
    the feature. These are the cases the cache exists for.
    """
    equivalent = [
        ("how do I list files", "How do I list files?"),
        ("what does this function do", "what does this function do!"),
        ("explain the retry logic", "Explain the RETRY logic"),
    ]
    for a, b in equivalent:
        veto = sc._equivalence_veto(sc._discriminator(a), sc._discriminator(b))
        assert veto is None, f"genuine duplicate wrongly vetoed: {a!r} vs {b!r} -> {veto}"


def test_unknown_discriminator_fails_closed():
    """A row written before this column existed is UNKNOWN, not equivalent.

    Defaulting legacy rows to an empty discriminator would read as "no numbers,
    no direction" and silently re-admit the exact collisions above. They must
    miss and age out via TTL instead.
    """
    veto = sc._equivalence_veto(None, sc._discriminator("retry 3 times"))
    assert veto is not None
    assert "no discriminator" in veto


def test_discriminator_carries_no_prompt_text():
    """The cache lives in the shared usage.db. Prompts must not land there.

    `persist_redact` exists precisely to keep prompt text out of this database;
    a guard that stored the prompt to compare it would undo that.
    """
    prompt = "reset the password for alice@example.com to hunter2 and retry 3 times"
    disc = sc._discriminator(prompt)
    blob = json.dumps(disc)
    for leaked in ("alice", "example.com", "hunter2", "password", "reset"):
        assert leaked not in blob, f"discriminator leaked {leaked!r} from the prompt"
    assert disc["nums"] == ["2", "3"]


def test_shipped_threshold_is_above_the_measured_floor():
    """Defence in depth: the default must at least exclude the weakest pair.

    0.9764 was the "increase/decrease" collision. The default must be above it.
    This does NOT make the threshold sufficient — see the module docstring — it
    stops the default from sitting under a collision the audit actually measured.
    """
    assert sc.DEFAULT_THRESHOLD > 0.9764, (
        f"DEFAULT_THRESHOLD={sc.DEFAULT_THRESHOLD} sits below a measured collision"
    )


def test_cache_can_be_switched_off_per_process(monkeypatch):
    """There was no off switch. A wrong answer had no immediate remedy."""
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    assert sc._cache_disabled() is True
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "0")
    assert sc._cache_disabled() is True
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_CACHE", raising=False)
    assert sc._cache_disabled() is False


async def test_check_returns_none_when_disabled(monkeypatch):
    """The switch must short-circuit before any embedding or DB work."""
    from llm_router.classify import TaskType

    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        return [0.1] * 768

    monkeypatch.setattr(sc, "_get_embedding", _boom)
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")

    assert await sc.check("retry 3 times", TaskType.CODE) is None
    assert not called, "disabled cache still computed an embedding"


def test_eviction_entry_point_exists():
    """A single poisoned entry must be removable without clearing the cache."""
    assert callable(getattr(sc, "evict", None)), (
        "semantic_cache.evict is missing — the only remedies for a wrong cached "
        "answer would be the 24h TTL or clearing everything"
    )


def test_guard_is_not_vacuous():
    """A guard that never fires has not been shown to work (repo rule).

    Confirms `_discriminator` actually extracts the two features the veto rests
    on; if it returned empty structures, every veto above would be unreachable
    and every test in this file would pass for the wrong reason.
    """
    d = sc._discriminator("increase the retry count from 3 to 30")
    assert d["nums"] == ["3", "30"], f"numbers not extracted: {d}"
    assert d["pol"], f"direction not extracted: {d}"

    neutral = sc._discriminator("what does this do")
    assert neutral["nums"] == [] and neutral["pol"] == [], (
        "the discriminator reports features for a prompt that has none, so it "
        "would veto genuine duplicates"
    )
