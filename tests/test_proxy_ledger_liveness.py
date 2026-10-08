"""P0.14-a: a proxy ledger that stopped writing must be visible.

Incident 2026-10-07 12:47 to 2026-10-08 14:24: ``proxy_calls.jsonl`` wrote 0 rows for 25 h
because every session ran with a project-level ``ANTHROPIC_BASE_URL`` pointing at
api.anthropic.com. ``kpi`` and ``doctor`` said nothing.

Pinned here, each red on main (no ``proxy_liveness`` module, no ``proxy_liveness`` card field):

* zero rows + turns          -> WARN (kpi text and JSON)
* rows present               -> no WARN
* no turns                   -> no WARN (an empty set must not raise an alarm)
* project settings override  -> doctor names the path and the HOST, never the secret
* no override                -> doctor reports nothing
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from llm_router import hook_latency as hl
from llm_router import paths
from llm_router.commands import doctor, kpi
from llm_router.proxy import ledger as pl

NOW = 1_790_000_000.0
HOUR = 3600.0
SECRET = "sk-ant-SECRETKEY123"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    yield
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _proxy_rows(n: int, *, age_h: float = 1.0) -> None:
    pl.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    with pl.ledger_path().open("a", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"ts": NOW - age_h * HOUR - i, "session_id": "s1", "model": "m"}) + "\n")


def _turns(n: int, *, age_h: float = 2.0) -> None:
    for i in range(n):
        assert hl.record("auto-route", "UserPromptSubmit", 12.0, now=NOW - age_h * HOUR - i)


def _card() -> dict:
    return kpi.compute_scorecard(days=7, now=NOW)


# -- kpi ------------------------------------------------------------------------


def test_zero_rows_with_turns_warns_in_text_and_json():
    _turns(5)
    card = _card()
    live = card["proxy_liveness"]
    assert live["proxy_rows_24h"] == 0
    assert live["hook_turns_24h"] == 5
    assert live["warn"] is True
    assert json.loads(json.dumps(card, default=str))["proxy_liveness"]["warn"] is True
    text = kpi.render_scorecard(card)
    assert "proxy_rows_24h: 0" in text
    assert "WARN" in text and "0 rows" in text


def test_zero_rows_with_routing_decisions_only_also_warns():
    db = paths.state_path("usage.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    from llm_router.cost import CREATE_ROUTING_DECISIONS_TABLE

    conn = sqlite3.connect(db)
    conn.execute(CREATE_ROUTING_DECISIONS_TABLE)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 3 * HOUR))
    conn.execute("INSERT INTO routing_decisions (timestamp, task_type) VALUES (?, 'code')", (stamp,))
    conn.commit()
    conn.close()
    live = _card()["proxy_liveness"]
    assert live["routing_decisions_24h"] == 1 and live["hook_turns_24h"] == 0
    assert live["warn"] is True


def test_rows_present_gives_no_warn():
    _proxy_rows(3)
    _turns(5)
    card = _card()
    assert card["proxy_liveness"]["proxy_rows_24h"] == 3
    assert card["proxy_liveness"]["warn"] is False
    text = kpi.render_scorecard(card)
    assert "proxy_rows_24h: 3" in text
    assert "WARN" not in text.split("proxy_rows_24h")[1].splitlines()[0]
    assert "wrote 0 rows" not in text


def test_no_turns_gives_no_warn_on_an_empty_set():
    card = _card()
    live = card["proxy_liveness"]
    assert live["proxy_rows_24h"] == 0 and not live["hook_turns_24h"]
    assert live["warn"] is False
    assert "wrote 0 rows" not in kpi.render_scorecard(card)


def test_rows_older_than_24h_do_not_count_and_non_turn_hooks_are_not_turns():
    _proxy_rows(4, age_h=30)
    for i in range(6):
        hl.record("agent-route", "PreToolUse", 3.0, now=NOW - HOUR - i)
    live = _card()["proxy_liveness"]
    assert live["proxy_rows_24h"] == 0
    assert live["hook_turns_24h"] == 0
    assert live["warn"] is False


# -- doctor ---------------------------------------------------------------------


def _settings(path: Path, env: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}), encoding="utf-8")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    _settings(home / ".claude" / "settings.json", {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})
    return home, proj


def test_override_is_detected_with_path_and_host_but_no_secret(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    f = proj / ".claude" / "settings.local.json"
    _settings(f, {"ANTHROPIC_BASE_URL": f"https://user:{SECRET}@api.anthropic.com/v1?key={SECRET}"})
    found = plv.find_overrides(proj, home)
    assert found == [{"path": str(f), "host": "api.anthropic.com"}]
    findings = plv.doctor_findings(proj, home, now=NOW)
    text = "\n".join(findings)
    assert str(f) in text and "api.anthropic.com" in text
    assert SECRET not in text and "user:" not in text


def test_override_in_settings_json_and_nested_dir_is_detected(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _settings(proj / ".claude" / "settings.json", {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
    _settings(proj / "sub" / "app" / ".claude" / "settings.local.json",
              {"ANTHROPIC_BASE_URL": "https://gw.example.com:8443/x"})
    hosts = sorted(o["host"] for o in plv.find_overrides(proj, home))
    assert hosts == ["api.anthropic.com", "gw.example.com:8443"]


def test_no_override_means_no_report(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _settings(proj / ".claude" / "settings.local.json", {"OTHER": "x"})          # no base url key
    _settings(proj / "sub" / ".claude" / "settings.json", {"ANTHROPIC_BASE_URL": "http://localhost:8787"})  # same proxy
    assert plv.find_overrides(proj, home) == []
    assert plv.doctor_findings(proj, home, now=NOW) == []


def test_no_report_when_proxy_is_not_the_default(tmp_path, monkeypatch):
    from llm_router import proxy_liveness as plv

    home, proj = tmp_path / "h", tmp_path / "p"
    proj.mkdir()
    _settings(home / ".claude" / "settings.json", {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
    _settings(proj / ".claude" / "settings.local.json", {"ANTHROPIC_BASE_URL": "https://other.example.com"})
    _turns(3)
    assert plv.doctor_findings(proj, home, now=NOW) == []


def test_doctor_reports_a_silent_ledger_when_proxy_is_default(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _turns(4)
    findings = plv.doctor_findings(proj, home, now=NOW)
    assert len(findings) == 1 and "0 rows" in findings[0] and "127.0.0.1:8787" in findings[0]
    _proxy_rows(2)
    assert plv.doctor_findings(proj, home, now=NOW) == []


def test_doctor_run_prints_the_override_and_counts_it_as_an_issue(tree, capsys):
    """P0.14-b: assert on the issues, not on the exit code (other checks make it non-zero anyway)."""
    home, proj = tree
    f = proj / ".claude" / "settings.local.json"
    _settings(f, {"ANTHROPIC_BASE_URL": f"https://api.anthropic.com/?k={SECRET}"})
    code, issues = doctor._run_doctor()
    out = capsys.readouterr().out
    assert str(f) in out and "api.anthropic.com" in out and SECRET not in out
    bypass = [i for i in issues if i.startswith("proxy bypass:")]
    assert len(bypass) == 1
    assert str(f) in bypass[0] and "api.anthropic.com" in bypass[0] and SECRET not in bypass[0]


def test_doctor_run_without_override_adds_no_bypass_issue(tree, capsys):
    code, issues = doctor._run_doctor()
    assert [i for i in issues if i.startswith("proxy bypass:")] == []


# -- P0.14-b: privacy, null-not-zero, windows, short silence -------------------------


def test_a_bare_key_in_base_url_is_never_printed_as_a_host(tree, capsys):
    """Reviewer plant: a key placed directly in ANTHROPIC_BASE_URL was printed as a lowercased host."""
    from llm_router import proxy_liveness as plv

    home, proj = tree
    planted = "SECRETKEY999-bare-key"
    f = proj / ".claude" / "settings.local.json"
    _settings(f, {"ANTHROPIC_BASE_URL": planted})
    assert plv.find_overrides(proj, home) == [{"path": str(f), "host": "(unparseable)"}]
    text = "\n".join(plv.doctor_findings(proj, home, now=NOW))
    assert "(unparseable)" in text and str(f) in text
    assert planted.lower() not in text.lower()
    doctor._run_doctor()
    assert planted.lower() not in capsys.readouterr().out.lower()


@pytest.mark.parametrize("value, shown", [
    ("https://api.anthropic.com", "api.anthropic.com"),
    ("API.Anthropic.COM:8443/x?k=1", "api.anthropic.com:8443"),
    ("http://localhost:8787", "localhost:8787"),
    ("127.0.0.1:8787", "127.0.0.1:8787"),
    ("http://[::1]:8787/v1", "[::1]:8787"),
    ("https://u:p@gw.example.com", "gw.example.com"),
])
def test_real_hosts_are_still_printed(value, shown):
    from llm_router import proxy_liveness as plv

    assert plv._host_of(value) == shown


@pytest.mark.parametrize("value", [
    "SECRETKEY999-bare-key", "sk-ant-api03-abc", "https://sk-ant-api03-abc", "host_with_underscore.com",
    "a..b.com", "-bad.example.com", "bad-.example.com", "1234.5678", "x" * 64 + ".com", "http://",
])
def test_non_hostnames_are_unparseable(value):
    from llm_router import proxy_liveness as plv

    assert plv._host_of(value) == "(unparseable)"


def test_unreadable_hook_count_is_null_not_zero_and_gives_no_warn(monkeypatch):
    """Mutant: ``except -> return 0`` made an unreadable count a measured zero."""
    from llm_router import proxy_liveness as plv

    def boom(**_kw):
        raise OSError("hook ledger unreadable")

    monkeypatch.setattr(hl, "read_rows", boom)
    live = plv.liveness(now=NOW, proxy_rows=[])
    assert live["hook_turns_24h"] is None
    assert live["routing_decisions_24h"] is None        # no usage.db either
    assert live["proxy_rows_24h"] == 0
    assert live["warn"] is False and live["message"] is None
    assert "hook turns unreadable" in kpi._proxy_liveness_lines(live)[0]


def test_unreadable_decision_count_is_null_not_zero_and_gives_no_warn():
    db = paths.state_path("usage.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a sqlite database" * 50)
    live = _card()["proxy_liveness"]
    assert live["routing_decisions_24h"] is None
    assert live["hook_turns_24h"] == 0
    assert live["warn"] is False


def test_future_dated_proxy_rows_are_not_counted_and_newest_is_clamped():
    _proxy_rows(3, age_h=-5)                 # five hours AFTER now
    _turns(4)
    live = _card()["proxy_liveness"]
    assert live["proxy_rows_24h"] == 0
    assert live["warn"] is True
    assert live["newest_proxy_ts"] is not None and live["newest_proxy_ts"] <= NOW


def test_future_dated_decision_rows_are_not_counted():
    conn = _decisions_db()
    _decision(conn, NOW + 5 * HOUR)
    _decision(conn, NOW - 3 * HOUR)
    conn.commit()
    conn.close()
    assert _card()["proxy_liveness"]["routing_decisions_24h"] == 1


def _decisions_db() -> sqlite3.Connection:
    db = paths.state_path("usage.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    from llm_router.cost import CREATE_ROUTING_DECISIONS_TABLE

    conn = sqlite3.connect(db)
    conn.execute(CREATE_ROUTING_DECISIONS_TABLE)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(routing_decisions)")}
    if "reason_code" not in cols:
        conn.execute("ALTER TABLE routing_decisions ADD COLUMN reason_code TEXT")
    return conn


def _decision(conn: sqlite3.Connection, ts: float, reason: str | None = None) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))
    conn.execute("INSERT INTO routing_decisions (timestamp, task_type, reason_code) VALUES (?, 'code', ?)",
                 (stamp, reason))


def test_sidecar_backfill_rows_are_not_turns():
    conn = _decisions_db()
    for i in range(4):
        _decision(conn, NOW - 3 * HOUR - i, "sidecar_backfill")
    _decision(conn, NOW - 3 * HOUR, "routed")
    conn.commit()
    conn.close()
    assert _card()["proxy_liveness"]["routing_decisions_24h"] == 1
    conn = _decisions_db()
    conn.execute("DELETE FROM routing_decisions WHERE reason_code = 'routed'")
    conn.commit()
    conn.close()
    live = _card()["proxy_liveness"]
    assert live["routing_decisions_24h"] == 0 and live["warn"] is False   # backfill alone is no alarm


# -- short silence (doctor, 2 h) ---------------------------------------------------


def test_short_silence_reports_zero_proxy_rows_in_2h_with_3_turns(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _proxy_rows(5, age_h=5)            # alive 5 h ago, silent for the last 2 h; 24 h window is fine
    _turns(3, age_h=1)
    findings = plv.doctor_findings(proj, home, now=NOW)
    assert len(findings) == 1
    assert "last 2 h" in findings[0] and "3 hook turns" in findings[0] and "127.0.0.1:8787" in findings[0]


def test_short_silence_needs_three_turns_and_no_recent_rows(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _proxy_rows(5, age_h=5)
    _turns(2, age_h=1)                                             # 2 < 3
    assert plv.doctor_findings(proj, home, now=NOW) == []
    _turns(1, age_h=1.5)                                           # now 3
    assert len(plv.doctor_findings(proj, home, now=NOW)) == 1
    _proxy_rows(1, age_h=0.5)                                      # a row inside the last 2 h
    assert plv.doctor_findings(proj, home, now=NOW) == []


def test_short_silence_empty_set_reports_nothing(tree):
    """0 turns, 0 rows: an empty set must not raise an alarm."""
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _proxy_rows(5, age_h=5)
    assert plv.short_silence(now=NOW)["warn"] is False
    assert plv.doctor_findings(proj, home, now=NOW) == []
    _turns(5, age_h=3)                                             # turns outside the 2 h window
    assert plv.doctor_findings(proj, home, now=NOW) == []


def test_short_silence_unreadable_turn_count_is_null_and_silent(tree, monkeypatch):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _proxy_rows(5, age_h=5)

    def boom(**_kw):
        raise OSError("x")

    monkeypatch.setattr(hl, "read_rows", boom)
    s = plv.short_silence(now=NOW)
    assert s["hook_turns"] is None and s["warn"] is False
    assert plv.doctor_findings(proj, home, now=NOW) == []


def test_short_silence_is_not_repeated_when_the_24h_warn_already_fired(tree):
    from llm_router import proxy_liveness as plv

    home, proj = tree
    _turns(4, age_h=1)                                             # 0 rows in 24 h and 4 turns in 2 h
    findings = plv.doctor_findings(proj, home, now=NOW)
    assert len(findings) == 1 and "last 24 h" in findings[0]


def test_doctor_run_counts_the_short_silence_as_an_issue(tree):
    real = time.time()
    pl.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    pl.ledger_path().write_text(json.dumps({"ts": real - 5 * HOUR, "session_id": "s1", "model": "m"}) + "\n",
                                encoding="utf-8")
    for i in range(3):
        assert hl.record("auto-route", "UserPromptSubmit", 12.0, now=real - HOUR - i)
    code, issues = doctor._run_doctor()
    bypass = [i for i in issues if i.startswith("proxy bypass:")]
    assert len(bypass) == 1 and "last 2 h" in bypass[0]
