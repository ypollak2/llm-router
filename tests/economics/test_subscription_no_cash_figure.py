"""WP-05: a subscription user is never shown a cash-savings figure, and every
displayed number states its provenance.

README (line ~138) already tells the truth -- "on a Claude Pro/Max subscription
the value is quota runway, NOT cash" -- but the session-end hook rendered
"$0.0413 actual  $0.9820 baseline  96% saved" to every user regardless. For a
subscriber that dollar figure is money they never had the option of spending:
their Claude usage is already paid for by the subscription, so the counterfactual
is quota consumed, not cash outlaid. Printing it as cash overstates the product's
value in exactly the direction the audit exists to catch.

Provenance: the baseline projection leans on _LEGACY_FALLBACK_OUTPUT for every
model the calibration corpus lacks a profile for -- which is every model except
claude-sonnet-4-6 (finding #12). Such a figure is `estimated`, not `measured`,
and must say so.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "llm_router"

#: Any bare dollar amount, e.g. "$0.9820" or "$12.30".
_CASH_RE = re.compile(r"\$\d")

_PROVENANCE_TAGS = ("measured", "estimated", "unknown")


@pytest.fixture
def hook():
    spec = importlib.util.spec_from_file_location("_session_end_sub", _SRC / "hooks" / "session-end.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tools_fixture() -> dict[str, dict]:
    return {
        "llm_query": {
            "count": 12,
            "in": 40_000,
            "out": 8_000,
            "cost": 0.0031,
            "models": {"ollama/qwen3-coder:30b": 12},
        },
    }


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_subscription_mode_shows_no_cash_savings_figure(hook, monkeypatch):
    """No cash BASELINE and no cash SAVED figure.

    Actual spend on external paid providers is deliberately still shown: that is
    money genuinely leaving the user's account, and hiding it would understate
    what routing costs. What a subscriber must never see is a dollar figure
    presented as money routing SAVED them, because their Claude usage was
    already bought by the subscription.
    """
    monkeypatch.setattr(hook, "_is_subscription_mode", lambda: True)
    rendered = _strip_ansi("\n".join(hook._format_routing_section(_tools_fixture())))
    headline = rendered.splitlines()[0]

    assert not _CASH_RE.search(headline), (
        "subscription users must not be shown a cash savings figure:\n" + headline
    )
    for banned in ("baseline  $", "$ baseline", "saved"):
        assert banned not in headline.lower(), headline


def test_subscription_mode_still_shows_actual_external_spend(hook, monkeypatch):
    """Real cash leaving the account stays visible even for a subscriber."""
    monkeypatch.setattr(hook, "_is_subscription_mode", lambda: True)
    rendered = _strip_ansi("\n".join(hook._format_routing_section(_tools_fixture())))

    assert _CASH_RE.search(rendered), "external spend must remain visible"


def test_subscription_mode_still_reports_the_value_as_quota(hook, monkeypatch):
    """Suppressing cash must not suppress the benefit -- otherwise the honest
    fix reads as "routing achieved nothing"."""
    monkeypatch.setattr(hook, "_is_subscription_mode", lambda: True)
    rendered = _strip_ansi("\n".join(hook._format_routing_section(_tools_fixture()))).lower()

    assert "quota" in rendered
    assert "48.0k" in rendered or "48000" in rendered or "48k" in rendered


def test_pay_per_token_mode_still_shows_cash(hook, monkeypatch):
    """The cash figure is correct for a pay-per-token user and must survive."""
    monkeypatch.setattr(hook, "_is_subscription_mode", lambda: False)
    rendered = _strip_ansi("\n".join(hook._format_routing_section(_tools_fixture())))

    assert _CASH_RE.search(rendered), "pay-per-token users should still see cash"


@pytest.mark.parametrize("subscription", [True, False])
def test_every_rendered_figure_states_its_provenance(hook, monkeypatch, subscription):
    monkeypatch.setattr(hook, "_is_subscription_mode", lambda: subscription)
    rendered = _strip_ansi("\n".join(hook._format_routing_section(_tools_fixture()))).lower()

    assert any(tag in rendered for tag in _PROVENANCE_TAGS), (
        "no measured|estimated|unknown tag on the routing figures:\n" + rendered
    )


def test_baseline_without_a_calibration_profile_is_tagged_estimated(hook, monkeypatch):
    """Finding #12: the savings baseline has no calibration profile, so its
    projection uses a fallback output estimate. That is `estimated`."""
    monkeypatch.setattr(hook, "_is_subscription_mode", lambda: False)
    rendered = _strip_ansi("\n".join(hook._format_routing_section(_tools_fixture()))).lower()

    assert "estimated" in rendered, rendered
    assert "measured" not in rendered, (
        "a fallback-projected baseline must not claim to be measured:\n" + rendered
    )


def test_subscription_helper_fails_closed_to_pay_per_token(hook, monkeypatch):
    """If config is unreadable the hook must not crash, and must not silently
    decide the user is a subscriber (which would hide a legitimate cash figure)."""
    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(hook, "_load_config_for_subscription", _boom)
    assert hook._is_subscription_mode() is False
