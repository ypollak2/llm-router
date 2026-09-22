"""Nothing leaves the machine unscrubbed — R2.

`alerts.emit_alert` POSTs its `detail` dict to `LLM_ROUTER_ALERT_WEBHOOK`.
The audit captured a live HTTP POST carrying a **Postgres DSN with a plaintext
password** — `budget_backend`'s Postgres-fallback path feeds `str(err)` into
the detail, and a driver error is all it takes. The same string was echoed
unredacted in the structlog critical line, because no pattern in the canonical
table matched a DB URL at all.

This is the one finding in the audit where data leaves the host.

THESE TESTS ASSERT THE WIRE BODY, not the log line. A test that only checks
what was logged would have passed throughout the period the webhook was
leaking — the log and the wire were two independent paths, which is to say
neither was covered.
"""

from __future__ import annotations

import json

import pytest

from llm_router import alerts
from llm_router.secret_scrubber import scrub_text

DSN = "postgresql://svcuser:AUDITcanaryPASSWORD@db.internal:5432/prod"
APIKEY = "sk-ant-api03-AUDITcanary00000000000000000000000000000000"


@pytest.fixture
def captured_wire(monkeypatch):
    """Intercept at urlopen: everything below it is the network."""
    sent: list[bytes] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def _fake_urlopen(request, *a, **kw):
        sent.append(request.data)
        return _Resp()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv("LLM_ROUTER_ALERT_WEBHOOK", "https://hook.example/test")
    monkeypatch.delenv("LLM_ROUTER_ALERTS_DISABLED", raising=False)
    return sent


# ── the leak ─────────────────────────────────────────────────────────────────

def test_a_dsn_password_does_not_reach_the_wire(captured_wire):
    """The captured incident, as a test."""
    alerts.emit_alert("budget_backend_unavailable",
                      detail={"error": f"could not connect: {DSN}"})
    assert captured_wire, "nothing was sent — the test proved nothing"
    body = captured_wire[-1].decode("utf-8")
    assert "AUDITcanaryPASSWORD" not in body, (
        f"a plaintext DB password left the machine:\n  {body[:200]}"
    )


def test_an_api_key_does_not_reach_the_wire(captured_wire):
    alerts.emit_alert("provider_auth_failed", detail={"error": f"401 {APIKEY}"})
    body = captured_wire[-1].decode("utf-8")
    assert APIKEY[:24] not in body


def test_a_secret_in_a_dict_KEY_does_not_reach_the_wire(captured_wire):
    """Keys carry secrets too — a caller keying by connection string is real."""
    alerts.emit_alert("pool_exhausted", detail={DSN: "unreachable"})
    body = captured_wire[-1].decode("utf-8")
    assert "AUDITcanaryPASSWORD" not in body


def test_a_nested_secret_does_not_reach_the_wire(captured_wire):
    alerts.emit_alert("nested", detail={"outer": {"inner": [f"x {DSN}"]}})
    body = captured_wire[-1].decode("utf-8")
    assert "AUDITcanaryPASSWORD" not in body


def test_an_exception_object_in_the_detail_is_scrubbed(captured_wire):
    """`str(err)` is the documented feeder; an exception object is the same
    text one `repr()` away."""
    alerts.emit_alert("boom", detail={"exc": RuntimeError(f"connect {DSN}")})
    body = captured_wire[-1].decode("utf-8")
    assert "AUDITcanaryPASSWORD" not in body


# ── anti-vacuity ─────────────────────────────────────────────────────────────

def test_the_alert_still_carries_actionable_detail(captured_wire):
    """Scrubbing everything would pass every test above and make alerting
    useless. The host and database must survive so an operator can act."""
    alerts.emit_alert("budget_backend_unavailable",
                      detail={"error": f"could not connect: {DSN}"})
    body = json.loads(captured_wire[-1].decode("utf-8"))
    text = json.dumps(body)
    assert body["event"] == "budget_backend_unavailable"
    assert "db.internal" in text, "the host was destroyed along with the password"
    assert "5432" in text
    assert "could not connect" in text


def test_the_wire_is_actually_exercised(captured_wire):
    """If urlopen were never reached, every assertion above is vacuous."""
    alerts.emit_alert("ping", detail={"ok": True})
    assert len(captured_wire) == 1
    assert json.loads(captured_wire[-1].decode("utf-8"))["detail"]["ok"] is True


def test_no_webhook_configured_sends_nothing(captured_wire, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ALERT_WEBHOOK", raising=False)
    alerts.emit_alert("quiet", detail={"error": DSN})
    assert not captured_wire


# ── the pattern that was missing entirely ────────────────────────────────────

@pytest.mark.parametrize("dsn", [
    "postgresql://u:pw@h:5432/d",
    "postgres://u:pw@h/d",
    "mysql://root:pw@localhost/db",
    "mongodb+srv://u:pw@cluster.net/db",
    "redis://:pw@h:6379/0",
])
def test_the_canonical_table_now_covers_db_urls(dsn):
    """No pattern matched a DB URL before R2 — not one variant."""
    assert "pw@" not in scrub_text(f"error: {dsn}")


def test_a_plain_url_is_untouched():
    """Anti-vacuity for the new pattern: it must not eat ordinary URLs."""
    plain = "https://api.example.com/v1/models"
    assert scrub_text(plain) == plain
