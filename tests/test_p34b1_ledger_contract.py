"""P3.4-B1: the ledger contract (Figure / show / window_summary v2 / provider_class, D-205).

Evidence: F70 printed "subscription 0 / paid 74" for a Claude Max user because the classifier
knew three hard-coded provider names (Max calls carry provider ``anthropic``) and printed
"latency 0 ms" because a NULL latency was read as 0. The tests below fail on that code.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import ledger_view as lv
from llm_router import seats as seats_mod

NOW = 1_790_000_000.0
MIN = 60.0


def _seats(*, claude=None, codex=None, gemini=None, keys=None, age_s=60.0) -> seats_mod.Seats:
    def seat(kind):
        return seats_mod.Seat(kind=kind, plan="x") if kind else seats_mod.Seat()
    return seats_mod.Seats(
        claude=seat(claude), codex=seat(codex), gemini=seat(gemini), api_keys=dict(keys or {}),
        detected_at=datetime.fromtimestamp(NOW - age_s, timezone.utc).isoformat())


# ── classifier ────────────────────────────────────────────────────────────────

def test_proxy_oauth_rows_are_subscription_with_a_claude_seat_and_no_keys():
    seats = _seats(claude="claude.ai", keys={"ANTHROPIC_API_KEY": False})
    assert lv.provider_class("anthropic", auth="oauth", seats=seats) == "subscription"
    assert lv.provider_class("anthropic", seats=seats) == "subscription"      # no row evidence
    assert lv.provider_class("claude", seats=seats) == "subscription"          # alias


def test_api_key_only_is_paid():
    seats = _seats(keys={"ANTHROPIC_API_KEY": True, "OPENAI_API_KEY": True})
    assert lv.provider_class("anthropic", seats=seats) == "paid"
    assert lv.provider_class("openai", seats=seats) == "paid"
    assert lv.provider_class("anthropic", auth="api_key", seats=None) == "paid"   # row evidence


def test_api_key_login_seat_is_paid_not_subscription():
    seats = _seats(claude="api-key")
    assert lv.provider_class("anthropic", seats=seats) == "paid"


def test_no_seats_file_and_no_row_evidence_is_unclassified_never_paid_or_subscription():
    for provider in ("anthropic", "openai", "codex", "gemini_cli", "unknown", "mystery"):
        assert lv.provider_class(provider, seats=None) == "unclassified"
    assert lv.provider_class("ollama", seats=None) == "local"                  # vocabulary
    assert lv.provider_class("subscription", seats=None) == "subscription"     # usage evidence
    assert lv.provider_class("anthropic", reason="claude_code_subscription") == "subscription"


def test_seat_aliases_codex_and_gemini_cli():
    seats = _seats(codex="chatgpt", gemini="google")
    assert lv.provider_class("codex", seats=seats) == "subscription"
    assert lv.provider_class("gemini_cli", seats=seats) == "subscription"
    assert lv.provider_class("anthropic", seats=seats) == "unclassified"       # no claude seat


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    return tmp_path


def _proxy(home: Path, rows: list[dict]) -> Path:
    p = home / "proxy_calls.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _rows():
    return [{"ts": NOW - 5 * MIN, "decision": "forwarded", "auth": "oauth"},
            {"ts": NOW - 6 * MIN, "decision": "forwarded", "auth": "oauth"},
            {"ts": NOW - 7 * MIN, "decision": "forwarded", "auth": "api_key"},
            {"ts": NOW - 8 * MIN, "decision": "forwarded"},                     # no evidence
            {"ts": NOW - 9 * MIN, "decision": "served", "auth": "oauth"}]       # local wins


def test_window_summary_splits_max_oauth_rows_out_of_paid(home):
    """Fails on the pre-B1 classifier: every anthropic row landed in "paid"."""
    _proxy(home, _rows())
    (home / "seats.json").write_text(json.dumps(_seats(claude="claude.ai").to_dict()))
    lw = lv.window_summary("24h", now=NOW)
    assert (lw.local_n, lw.subscription_n, lw.paid_n, lw.unclassified_n) == (1, 3, 1, 0)
    assert "unclassified" not in lw.line()


def test_no_seats_file_shows_unclassified_separately(home):
    _proxy(home, _rows())
    lw = lv.window_summary("24h", now=NOW)
    assert (lw.local_n, lw.subscription_n, lw.paid_n, lw.unclassified_n) == (1, 2, 1, 1)
    assert "/ unclassified 1" in lw.line()
    assert lw.sources["seats.json"] == {"present": False, "rows": None, "newest_ts": None,
                                        "stale": None}


def test_stale_seats_are_flagged_in_sources(home, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    _proxy(home, _rows())
    old = _seats(claude="claude.ai", age_s=25 * 3600).to_dict()
    (home / "seats.json").write_text(json.dumps(old))
    lw = lv.window_summary("24h", now=NOW, seats=seats_mod.Seats.from_dict(old))
    assert lw.sources["seats.json"]["stale"] is True
    fresh = lv.window_summary("24h", now=NOW, seats=_seats(claude="claude.ai"))
    assert fresh.sources["seats.json"]["stale"] is False


def test_stale_flag_when_seats_file_is_read_from_disk(home, monkeypatch):
    _proxy(home, _rows())
    (home / "seats.json").write_text(json.dumps(_seats(claude="claude.ai", age_s=0).to_dict()))
    monkeypatch.setattr(time, "time", lambda: time.mktime(time.gmtime()) + 3 * 86400)
    assert lv.window_summary("24h", now=NOW).sources["seats.json"]["stale"] is True


# ── Figure / show ─────────────────────────────────────────────────────────────

def _fig(**kw):
    base = dict(value=3.5, n=4, window="today", as_of="t", source="s")
    base.update(kw)
    return lv.Figure(**base)


def test_show_returns_none_unless_value_n_and_window_are_known():
    assert lv.show(_fig(), "${:,.2f}") == "$3.50"
    assert lv.show(_fig(value=0), "{}") == "0"                  # a measured zero is shown
    assert lv.show(_fig(value=None)) is None
    assert lv.show(_fig(n=None)) is None
    assert lv.show(_fig(window="")) is None
    assert lv.show(None) is None


# ── latency ───────────────────────────────────────────────────────────────────

def _usage_db(home: Path, latencies: list) -> Path:
    """routing_decisions rows with the given latency_ms (None = not recorded)."""
    from llm_router import cost
    import asyncio

    async def make():
        await (await cost._get_db()).close()
    asyncio.run(make())
    db = home / "usage.db"
    con = sqlite3.connect(db)
    ts = datetime.fromtimestamp(NOW - 10 * MIN, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for lat in latencies:
        con.execute("INSERT INTO routing_decisions (timestamp, task_type, final_provider, success, "
                    "provenance, latency_ms) VALUES (?,?,?,?,?,?)",
                    (ts, "query", "openai", 1, "runtime", lat))
    con.commit()
    con.close()
    return db


def test_latency_percentiles_use_only_rows_with_a_recorded_latency(home):
    db = _usage_db(home, [None, 0.0, 100.0, 200.0, 300.0, 400.0])
    lw = lv.window_summary("24h", now=NOW, db_path=db)
    assert (lw.latency_n_recorded, lw.latency_n_total) == (4, 6)
    assert lw.latency_p50.value == 200.0 and lw.latency_p50.n == 4
    assert lw.latency_p99.value == 400.0


def test_all_null_latency_is_none_with_zero_recorded_never_zero(home):
    db = _usage_db(home, [None, None, None])
    lw = lv.window_summary("24h", now=NOW, db_path=db)
    assert lw.latency_p50.value is None and lw.latency_p95.value is None
    assert (lw.latency_n_recorded, lw.latency_n_total) == (0, 3)
    assert "0 of 3" in lw.latency_p50.na_reason
    assert lv.show(lw.latency_p50) is None


# ── unknown is None; round trip; legacy form ──────────────────────────────────

def test_no_sources_means_none_not_zero(home):
    lw = lv.window_summary("24h", now=NOW)
    assert lw.has_data is False
    assert lw.n is None and lw.local_n is None and lw.paid_n is None
    assert lw.saved_usd is None and lw.saved_n is None
    assert lw.latency_n_recorded is None


def test_empty_window_with_sources_is_a_counted_zero(home):
    _proxy(home, [{"ts": NOW - 30 * 86400, "decision": "served"}])
    lw = lv.window_summary("24h", now=NOW)
    assert lw.has_data and lw.n == 0 and lw.paid_n == 0
    assert lw.saved_usd is None            # no usage database: saved is unknown, not $0


def test_to_json_from_json_round_trip(home):
    _proxy(home, _rows())
    db = _usage_db(home, [None, 10.0])
    lw = lv.window_summary("24h", now=NOW, db_path=db, seats=_seats(claude="claude.ai"))
    again = lv.LedgerWindow.from_json(lw.to_json())
    assert again == lw and again.v == 2
    assert again.to_json() == lw.to_json()
    with pytest.raises(ValueError):
        lv.LedgerWindow.from_json(json.dumps({"v": 1}))


def test_new_signature_and_legacy_form_agree(home):
    _proxy(home, _rows())
    legacy = lv.window_summary("24h", now=NOW, seats=None)
    new = lv.window_summary(NOW - 24 * 3600, NOW, name="24h", seats=None)
    assert new.line() == legacy.line()
    assert lv.window_summary(None, scope="all", now=NOW, seats=None).window == "lifetime"
    assert lv.window_summary(None, scope="7d", now=NOW, seats=None).window == "7d"
    assert lv.window_summary(None, scope="day", now=NOW, seats=None).window == "today"
    with pytest.raises(ValueError):
        lv.window_summary(None, scope="bogus")


def test_session_scope_new_form_filters_by_session_id(home):
    _proxy(home, [{"ts": NOW - MIN, "decision": "served", "session_id": "a"},
                  {"ts": NOW - MIN, "decision": "served", "session_id": "b"}])
    lw = lv.window_summary(NOW - 3600, NOW, "session", session_id="a", seats=None)
    assert lw.n == 1 and lw.scope == "session_id"


def test_sources_report_presence_rows_and_newest(home):
    _proxy(home, _rows())
    db = _usage_db(home, [1.0])
    src = lv.window_summary("24h", now=NOW, db_path=db, seats=None).sources
    assert src["proxy_calls.jsonl"]["present"] and src["proxy_calls.jsonl"]["rows"] == 5
    assert src["routing_decisions"]["present"] and src["routing_decisions"]["rows"] == 1
    assert src["technical_ops.jsonl"]["rows"] is None
    assert src["technical_ops.jsonl"]["present"] is False


# ── D-205: no plan or model literal in ledger_view.py ─────────────────────────

def test_ledger_view_names_no_plan_or_model():
    text = Path(lv.__file__).read_text(encoding="utf-8")
    bad = re.findall(r"""["'](?:max|pro|plus|team|enterprise|free|opus|sonnet|haiku|gpt[-\w.]*|"""
                     r"""o[134](?:-\w+)?|gemini-[\w.]+|claude-[\w.-]+)["']""", text, re.I)
    assert not bad, f"hard-coded plan / model names in ledger_view.py (D-205): {bad}"
    assert not re.search(r"\b(opus|sonnet|haiku)\b", text, re.I)
    assert not hasattr(lv, "SUBSCRIPTION_PROVIDERS")
    assert set(lv.SEAT_ALIASES) == {"anthropic", "claude", "codex", "gemini_cli"}   # the vocabulary
