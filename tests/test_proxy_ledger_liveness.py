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
    home, proj = tree
    f = proj / ".claude" / "settings.local.json"
    _settings(f, {"ANTHROPIC_BASE_URL": f"https://api.anthropic.com/?k={SECRET}"})
    code, issues = doctor._run_doctor()
    out = capsys.readouterr().out
    assert str(f) in out and "api.anthropic.com" in out and SECRET not in out
    assert any("api.anthropic.com" in i for i in issues)
    assert code != 0
