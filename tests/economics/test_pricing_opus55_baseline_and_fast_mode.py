"""Opus 5.5 as the savings baseline, and fast-mode pricing (owner decision 2026-09-29).

Every expected rate below is copied from the RAW pricing page
(``curl -sL https://platform.claude.com/docs/en/about-claude/pricing.md``),
re-checked 2026-09-29:

* "Fast mode pricing": Claude Opus 5.5 $8 / $40; Claude Opus 5 / Claude Opus 4.8
  $10 / $50. "Prompt caching multipliers apply on top of fast mode pricing."
* "Prompt caching": 5m write 1.25x, 1h write 2x, cache read 0.1x — except
  "0.05x on Claude Opus 5.5".

Before this change the ``-fast`` ids were aliases to their base models, so a
fast-mode call was priced at HALF its real rate ($5/$25 instead of $10/$50), and
``claude-opus-5-5-fast`` was not priced at all.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from llm_router import pricing

_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _ROOT / "src" / "llm_router"


@pytest.mark.parametrize(
    ("model", "base_in", "cw_5m", "cw_1h", "cache_read", "out"),
    [
        # Opus 5.5 fast keeps Opus 5.5's 0.05x cache read: 0.05 x $8 = $0.40.
        ("claude-opus-5-5-fast", 8.00, 10.00, 16.00, 0.40, 40.00),
        # Standard 0.1x cache read on the $10 fast input rate.
        ("claude-opus-5-fast", 10.00, 12.50, 20.00, 1.00, 50.00),
        ("claude-opus-4-8-fast", 10.00, 12.50, 20.00, 1.00, 50.00),
    ],
)
def test_fast_mode_rates_with_stacked_cache_multipliers(
    model, base_in, cw_5m, cw_1h, cache_read, out
) -> None:
    assert pricing.resolve(model) == model
    assert pricing.input_rate(model) == pytest.approx(base_in)
    assert pricing.output_rate(model) == pytest.approx(out)
    assert pricing.cache_write_rate(model) == pytest.approx(cw_5m)
    assert pricing.cache_write_1h_rate(model) == pytest.approx(cw_1h)
    assert pricing.cache_read_rate(model) == pytest.approx(cache_read)


@pytest.mark.parametrize(
    ("fast", "base"),
    [
        ("claude-opus-5-5-fast", "claude-opus-5-5"),
        ("claude-opus-5-fast", "claude-opus-5"),
        ("claude-opus-4-8-fast", "claude-opus-4-8"),
    ],
)
def test_fast_mode_is_exactly_twice_the_base_rate(fast, base) -> None:
    """The published fast tier is 2x the base rate on every component."""
    fr, br = pricing.rates_per_m(fast), pricing.rates_per_m(base)
    assert fr is not None and br is not None
    for key, value in br.items():
        assert fr[key] == pytest.approx(2 * value), key


def test_opus_5_5_is_the_savings_baseline() -> None:
    assert pricing.SAVINGS_BASELINE_MODEL == "claude-opus-5-5"
    assert pricing.savings_baseline_model() == "claude-opus-5-5"
    assert pricing.savings_baseline_rates() == (4.0, 20.0)


@pytest.mark.parametrize("alias", ["opus", "claude-opus", "anthropic/claude-opus"])
def test_opus_aliases_resolve_to_opus_5_5(alias) -> None:
    assert pricing.resolve(alias) == "claude-opus-5-5"


def test_statusline_host_rate_is_the_baseline() -> None:
    """The statusline prices the host as ``price_for("opus")``."""
    spec = importlib.util.spec_from_file_location("_sb_o55", _SRC / "hooks" / "status-bar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.HOST_INPUT_PER_M, mod.HOST_OUTPUT_PER_M) == pricing.savings_baseline_rates()


def test_latest_opus_is_opus_5_5() -> None:
    from llm_router import cost

    assert cost.LATEST_OPUS_MODEL == "claude-opus-5-5"
    assert cost._OPUS_PRICING["claude-opus-5-5"] == pricing.savings_baseline_rates()


def test_fail_open_fallbacks_match_the_baseline() -> None:
    """The import-failure fallbacks must be the current baseline's list price,
    not the previous one — otherwise a broken import silently moves the figure."""
    from llm_router import digest

    expected = pricing.savings_baseline_rates()
    assert (digest._HOST_IN_PER_M_FALLBACK, digest._HOST_OUT_PER_M_FALLBACK) == expected

    spec = importlib.util.spec_from_file_location(
        "_ss_o55", _SRC / "hooks" / "session-start.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod._HOST_IN_PER_M_FALLBACK, mod._HOST_OUT_PER_M_FALLBACK) == expected

    # session-end's fallback only runs when cost.py fails to import, so it is
    # pinned by source rather than by forcing an ImportError.
    for path in (_SRC / "hooks" / "session-end.py", _ROOT / "hooks" / "session-end.py"):
        text = path.read_text()
        fb_in = re.search(r"except Exception:.*?HOST_INPUT_PER_M\s*=\s*([\d.]+)", text, re.S)
        fb_out = re.search(r"except Exception:.*?HOST_OUTPUT_PER_M\s*=\s*([\d.]+)", text, re.S)
        assert fb_in and fb_out, path
        assert (float(fb_in.group(1)), float(fb_out.group(1))) == expected, path
