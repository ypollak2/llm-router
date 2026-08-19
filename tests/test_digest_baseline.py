"""AC-4 / INV-COST-004 regression: digest baseline uses the canonical host price.

`digest._host_baseline` used to hardcode its own Opus price constants (15/75) and
label the result a "Sonnet baseline" — an independent, drifting, mislabeled copy
of the host price. It now reads cost.py's single canonical
``_HOST_INPUT_PER_M`` / ``_HOST_OUTPUT_PER_M`` at call time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router import cost, digest

_SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"


def test_digest_host_baseline_follows_canonical_prices(monkeypatch):
    """Fail-before: with the old hardcoded 15/75, overriding the canonical
    constants would NOT change digest's baseline. Pass-after: it tracks them.
    """
    monkeypatch.setattr(cost, "_HOST_INPUT_PER_M", 3.0, raising=False)
    monkeypatch.setattr(cost, "_HOST_OUTPUT_PER_M", 9.0, raising=False)
    # 1M input + 1M output at $3/$9 per M → $3 + $9 = $12.00
    assert digest._host_baseline(1_000_000, 1_000_000) == pytest.approx(12.0)


def test_digest_host_baseline_matches_cost_module_exactly(monkeypatch):
    """digest and cost must agree token-for-token — one canonical price."""
    monkeypatch.setattr(cost, "_HOST_INPUT_PER_M", 5.0, raising=False)
    monkeypatch.setattr(cost, "_HOST_OUTPUT_PER_M", 25.0, raising=False)
    in_tok, out_tok = 123_456, 78_910
    expected = (in_tok * cost._HOST_INPUT_PER_M
                + out_tok * cost._HOST_OUTPUT_PER_M) / 1_000_000
    assert digest._host_baseline(in_tok, out_tok) == pytest.approx(expected)


def test_digest_host_baseline_fails_open(monkeypatch):
    """A broken canonical read must not raise — falls back to a list price."""
    monkeypatch.delattr(cost, "_HOST_INPUT_PER_M", raising=False)
    # Missing attr triggers the except branch → fallback constants, no exception.
    val = digest._host_baseline(1_000_000, 0)
    assert val == pytest.approx(digest._HOST_IN_PER_M_FALLBACK)


def test_digest_source_no_sonnet_mislabel_and_uses_canonical():
    """Source guard (mirrors test_sessionstart_digest_uses_opus_not_sonnet_baseline):
    digest no longer mislabels the host baseline 'Sonnet' and references the
    canonical host constant rather than a private hardcoded price."""
    txt = (_SRC / "digest.py").read_text()
    assert "Sonnet" not in txt, "digest must not label the host baseline 'Sonnet'"
    assert "_HOST_INPUT_PER_M" in txt  # sources the canonical host rate
