"""R2 / G-RESEARCH-NOKEY — research-output trust contract.

A research answer's citations are only trustworthy when a web-grounded model
(Perplexity sonar) produced them. When no web backend is available and a non-web
model answers, its 'citations' are model-generated and may be fabricated, so the
output must (a) lead with a prominent UNVERIFIED banner and (b) never present the
citations under an authoritative 'Sources:' header.
"""
from __future__ import annotations

from llm_router.tools.text import (
    _apply_research_trust_contract,
    _is_web_grounded,
)


def test_web_grounded_detection():
    assert _is_web_grounded("perplexity/sonar")
    assert _is_web_grounded("perplexity/sonar-pro")
    assert not _is_web_grounded("ollama/qwen3-coder:30b")
    assert not _is_web_grounded("openai/o3")
    assert not _is_web_grounded(None)


def test_web_grounded_answer_renders_sources():
    body = "Answer body."
    out = _apply_research_trust_contract(
        body,
        ["https://arxiv.org/abs/2406.18665"],
        "perplexity/sonar",
        no_perplexity=False,
    )
    assert "**Sources:**" in out
    assert "UNVERIFIED" not in out
    assert "2406.18665" in out


def test_non_web_answer_is_flagged_unverified_and_hides_sources():
    body = "RouterBench is arXiv:2403.12345."  # a fabricated id, as happened live
    out = _apply_research_trust_contract(
        body,
        ["https://arxiv.org/abs/2403.12345 (fabricated)"],
        "ollama/qwen3-coder:30b",
        no_perplexity=True,
    )
    # Leads with the unverified banner
    assert out.lstrip().startswith("> ⚠️ **UNVERIFIED")
    # Never presents fabricated citations as authoritative sources
    assert "**Sources:**" not in out
    # Citations, if shown, are explicitly quarantined as unverified
    assert "Unverified references" in out
    assert "_(unverified" in out
    # Points the user at the fix
    assert "PERPLEXITY_API_KEY" in out


def test_non_web_answer_without_citations_still_warns():
    out = _apply_research_trust_contract(
        "Some answer.", [], "openai/o3", no_perplexity=False
    )
    assert "UNVERIFIED" in out
    assert "**Sources:**" not in out
    assert "Unverified references" not in out  # nothing to quarantine
    # no_perplexity is False here → don't nag about the env var
    assert "Set PERPLEXITY_API_KEY" not in out
