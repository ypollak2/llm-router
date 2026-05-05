"""Regression tests for README SVG assets.

Ensures generated SVGs don't contain layout/animation bugs
that have caused visual issues in the past.
"""

import re
from pathlib import Path

import pytest

DOCS_README = Path(__file__).parent.parent / "docs" / "readme"


@pytest.fixture
def savings_light():
    return (DOCS_README / "savings-light.svg").read_text()


@pytest.fixture
def savings_dark():
    return (DOCS_README / "savings-dark.svg").read_text()


@pytest.fixture
def hero_light():
    return (DOCS_README / "hero-light.svg").read_text()


@pytest.fixture
def hero_dark():
    return (DOCS_README / "hero-dark.svg").read_text()


class TestSavingsNoClipping:
    """The '60-80%' number must never be clipped by the viewBox."""

    def test_big_stat_has_fill_box_transform(self, savings_light):
        """transform-box: fill-box ensures scale() uses the text center, not SVG origin."""
        assert "transform-box: fill-box" in savings_light
        assert "transform-origin: center center" in savings_light

    def test_big_stat_text_has_safe_margin(self, savings_light):
        """The big stat text x-center must leave >= 20px margin from viewBox left edge.

        At font-size 52, '60-80%' is ~180px wide. Centered at x=125 means
        leftmost pixel ~x=35, safely inside the viewBox (x=0, card rx=14).
        """
        match = re.search(r'class="big-stat">\s*<text x="(\d+)"', savings_light)
        assert match, "Could not find big-stat text element"
        x_center = int(match.group(1))
        assert x_center >= 115, f"big-stat x={x_center} too close to left edge, risk of clipping"

    def test_no_html_entities(self, savings_light):
        """SVG doesn't support HTML entities like &mdash;. Use Unicode numeric refs."""
        # &mdash; caused a parse error in browsers, truncating the SVG
        assert "&mdash;" not in savings_light
        assert "&ndash;" not in savings_light

    def test_dark_variant_matches_fixes(self, savings_dark):
        assert "transform-box: fill-box" in savings_dark
        assert "&mdash;" not in savings_dark


class TestSavingsNoHardcodedData:
    """Savings SVG must not contain historical dollar amounts or token counts."""

    def test_no_dollar_amounts(self, savings_light):
        # Previously contained "$2.82", "$4.13", "$0.00", "$6.95"
        dollar_pattern = re.findall(r'\$\d+\.\d{2}', savings_light)
        assert not dollar_pattern, f"Found hardcoded dollar amounts: {dollar_pattern}"

    def test_no_specific_token_counts(self, savings_light):
        # Previously contained "7.0M tokens", "8.6M tokens"
        assert "7.0M tokens" not in savings_light
        assert "8.6M tokens" not in savings_light


class TestHeroMotionPaths:
    """Route dots must use <animateMotion> with path-following, not CSS translateX."""

    def test_uses_animate_motion(self, hero_light):
        """Dots should use SVG-native <animateMotion> for path-following."""
        assert "<animateMotion" in hero_light

    def test_uses_mpath_references(self, hero_light):
        """Each animateMotion should reference a defined motion path via <mpath>."""
        assert '<mpath href="#pathFree"' in hero_light
        assert '<mpath href="#pathBudget"' in hero_light
        assert '<mpath href="#pathPremium"' in hero_light

    def test_no_css_translatex_for_dots(self, hero_light):
        """The old flowDot animation used translateX which broke diagonal routes."""
        assert "flowDot" not in hero_light
        assert "translateX(-20px)" not in hero_light

    def test_motion_paths_match_route_lines(self, hero_light):
        """Motion path endpoints should match the route line coordinates."""
        # Budget route line: (430,168) → (520,208)
        assert 'id="pathBudget" d="M430,168 L520,208"' in hero_light
        # Premium route line: (430,188) → (520,268)
        assert 'id="pathPremium" d="M430,188 L520,268"' in hero_light

    def test_dark_variant_has_same_motion(self, hero_dark):
        assert "<animateMotion" in hero_dark
        assert '<mpath href="#pathBudget"' in hero_dark


class TestViewBoxSafety:
    """All SVGs must have adequate viewBox for their content."""

    @pytest.mark.parametrize("svg_name", [
        "savings-light.svg", "savings-dark.svg",
        "hero-light.svg", "hero-dark.svg",
    ])
    def test_viewbox_exists(self, svg_name):
        svg = (DOCS_README / svg_name).read_text()
        assert 'viewBox="' in svg
