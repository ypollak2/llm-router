"""Verifier PR B: the verify record, its join in northstar.units(), and the SHADOW guarantee
(no NS/D1/D2 or other KPI number moves because verify records exist)."""
from __future__ import annotations

import json
import os
import stat

import pytest

from llm_router import northstar as ns
from llm_router.commands import kpi
from llm_router.toolkit.verify_unit import UnitResult
from tests.test_northstar import (SID_MAIN, _bulk_user_prompts, _home_dir, _project, _user,
                                  _write_jsonl)

NOW = 1_800_100_000.0


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude_projects"))


def _codex_row(ts, outcome="delegated", **kw):
    return {"ts": ts, "lever": "agent_route_codex", "model": "codex/gpt-5.5", "outcome": outcome,
            "task_type": "code", "session_id": SID_MAIN, "session_kind": "organic", **kw}


def _setup(tmp_path, extra_rows=()):
    """One session, two codex units (delegated t1, failed t2) + 60 user prompts."""
    rows = [_codex_row(1_800_000_050.0), _codex_row(1_800_000_060.0, "codex_failed"), *extra_rows]
    p = _home_dir(tmp_path) / "north_star_units.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{SID_MAIN}.jsonl",
                 [_user(SID_MAIN, "delegate this", 1_800_000_000)]
                 + _bulk_user_prompts(SID_MAIN, 60, start_ts=1_800_000_100))
    return p, proj


def _units(proj):
    return list(ns.units(days=None, session_id=SID_MAIN, root=proj.parent, verify_records=True))


def _codex(units):
    return sorted((u for u in units if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX), key=lambda u: u["ts"])


def _vrow(uid, status="pass_f2p", reason="v1_f2p", **kw):
    v = {"verify_level": "V1", "verify_status": status, "verify_reason": reason,
         "verify_n_candidates": 3, "verify_n_f2p": 1, "verify_ms": 1200, "verify_sandboxed": True,
         "verify_flags": [], **kw}
    return {"unit_id": uid, "verify": v}


def _append(path, *rows, raw=()):
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        for line in raw:
            fh.write(line + "\n")


# ── unit_id ──────────────────────────────────────────────────────────────────

def test_unit_id_is_stable_distinct_and_independent_of_outcome(tmp_path):
    _, proj = _setup(tmp_path)
    a, b = _codex(_units(proj))
    assert a["unit_id"] and a["unit_id"].startswith("u_") and len(a["unit_id"]) == 18
    assert a["unit_id"] != b["unit_id"]
    assert a["outcome"] != b["outcome"]
    assert a["unit_id"] == _codex(_units(proj))[0]["unit_id"]  # stable across reads
    assert ns.unit_id(SID_MAIN, ns.UNIT_AGENT_ROUTE_CODEX, 1_800_000_050.0) == a["unit_id"]
    assert ns.unit_id(None, "x", 1.0) is None and ns.unit_id("s", "x", None) is None


# ── join ─────────────────────────────────────────────────────────────────────

def test_verify_joins_to_the_right_unit_only(tmp_path):
    p, proj = _setup(tmp_path)
    a, b = _codex(_units(proj))
    assert "verify" not in a and "verify" not in b
    _append(p, _vrow(a["unit_id"]))
    ua, ub = _codex(_units(proj))
    assert ua["verify"]["verify_status"] == "pass_f2p" and ua["verify"]["verify_level"] == "V1"
    assert ua["verify"]["verify_n_candidates"] == 3 and ua["verify"]["verify_sandboxed"] is True
    assert "verify" not in ub
    assert not any("verify" in u for u in _units(proj) if u["kind"] != ns.UNIT_AGENT_ROUTE_CODEX)
    # outcome untouched by a pass_f2p record (SHADOW)
    assert (ua["outcome"], ua["signal"]) == (a["outcome"], a["signal"])


def test_duplicate_verify_records_last_wins(tmp_path):
    p, proj = _setup(tmp_path)
    a, _ = _codex(_units(proj))
    _append(p, _vrow(a["unit_id"], "unavailable", "verify_unavailable:sandbox_unproven",
                     verify_level=None, verify_sandboxed=False),
            _vrow(a["unit_id"], "pass_p2p", "verified_weak"))
    ua, _ = _codex(_units(proj))
    assert ua["verify"]["verify_status"] == "pass_p2p"


def test_malformed_verify_records_are_ignored_not_fatal(tmp_path):
    p, proj = _setup(tmp_path)
    a, b = _codex(_units(proj))
    _append(p, _vrow(a["unit_id"]), raw=["{not json", "[]", "null"])
    _append(p, {"unit_id": b["unit_id"], "verify": "pass"},                 # verify not a dict
            {"unit_id": b["unit_id"], "verify": {"verify_status": "used"}},   # unknown status
            {"unit_id": 7, "verify": _vrow("x")["verify"]},                   # unit_id not a str
            {"verify": _vrow("x")["verify"]})                                 # no unit_id
    ua, ub = _codex(_units(proj))
    assert ua["verify"]["verify_status"] == "pass_f2p"
    assert "verify" not in ub


def test_malformed_later_record_does_not_erase_an_earlier_good_one(tmp_path):
    p, proj = _setup(tmp_path)
    a, _ = _codex(_units(proj))
    _append(p, _vrow(a["unit_id"]), {"unit_id": a["unit_id"], "verify": {"verify_status": "bogus"}})
    assert _codex(_units(proj))[0]["verify"]["verify_status"] == "pass_f2p"


def test_orphan_verify_record_creates_no_unit_and_changes_nothing(tmp_path):
    p, proj = _setup(tmp_path)
    before = _units(proj)
    _append(p, _vrow("u_deadbeefdeadbeef"))
    after = _units(proj)
    assert after == before and len(after) == len(before)
    assert not any("verify" in u for u in after)


# ── SHADOW: KPI byte-identical ───────────────────────────────────────────────

def _scorecard():
    return kpi.compute_scorecard(days=100000, now=NOW)


def test_kpi_numbers_are_byte_identical_with_and_without_verify_records(tmp_path):
    bulk = [_codex_row(1_800_000_200.0 + i, "delegated" if i % 3 else "codex_failed") for i in range(60)]
    p, proj = _setup(tmp_path, bulk)
    base = _scorecard()
    for k in ("NS", "D1", "D2"):  # a real percentage, not "too few to tell"
        assert "%" in base["kpis"][k]["value"], base["kpis"][k]
    base_text = kpi.render_scorecard(base)
    assert base["kpis"]["D2"]["value"] and base["verify_shadow"] is None

    a, b = _codex(_units(proj))[:2]
    _append(p, _vrow(a["unit_id"], "pass_f2p"), _vrow(b["unit_id"], "pass_p2p", "verified_weak"),
            _vrow("u_orphan0000000000", "fail", "v1_tests_fail"))
    with_v = _scorecard()

    dump = lambda d: json.dumps(d, sort_keys=True)  # noqa: E731
    assert dump(with_v["kpis"]) == dump(base["kpis"])          # every KPI, NS/D1/D2 included
    assert dump(with_v["joins"]) == dump(base["joins"])
    assert with_v["verify_shadow"] == {"verified": 1, "weak": 1, "failed": 0, "unavailable": 0}
    text = kpi.render_scorecard(with_v)
    line = "verify (shadow): 1 verified, 1 weak, 0 failed, 0 unavailable"
    assert line in text
    stripped = "\n".join(ln for ln in text.splitlines() if "verify (shadow)" not in ln)
    assert stripped == base_text                                # nothing else in the render moved
    lines = text.splitlines()
    d2 = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("D2 "))
    d3 = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("D3 "))
    shadow = next(i for i, ln in enumerate(lines) if "verify (shadow)" in ln)
    assert d2 < shadow < d3                                     # shown in D2's block, after its lines


# ── privacy ──────────────────────────────────────────────────────────────────

def test_only_reason_codes_and_counts_are_written(tmp_path):
    p, proj = _setup(tmp_path)
    a, _ = _codex(_units(proj))
    secret = "AssertionError: password=hunter2 in /Users/me/proj; ran `pytest -k secret`"
    res = UnitResult(verify_status="fail", reason=secret, n_candidates=2, n_f2p=0, ms=50,
                     sandboxed=True, flags=["tests_disappeared", secret, "line\nbreak"])
    ns.record_verify(a["unit_id"], res)
    text = p.read_text()
    last = json.loads(text.splitlines()[-1])
    assert set(last) == {"unit_id", "verify"}
    assert set(last["verify"]) == {"verify_level", "verify_status", "verify_reason",
                                   "verify_n_candidates", "verify_n_f2p", "verify_ms",
                                   "verify_sandboxed", "verify_flags"}
    assert last["verify"]["verify_reason"] == "invalid_reason"
    assert last["verify"]["verify_flags"] == ["tests_disappeared"]
    for needle in ("hunter2", "/Users/me", "pytest", "AssertionError", "line\\nbreak"):
        assert needle not in text
    # a hand-written row with free text is also scrubbed at read time
    _append(p, _vrow(a["unit_id"], reason="has spaces and /paths", verify_flags=["a b"]))
    v = _codex(_units(proj))[0]["verify"]
    assert v["verify_reason"] == "invalid_reason" and v["verify_flags"] == []
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_record_verify_round_trips_a_real_unit_result(tmp_path):
    p, proj = _setup(tmp_path)
    a, _ = _codex(_units(proj))
    ns.record_verify(a["unit_id"], UnitResult(verify_status="unavailable",
                                              reason="verify_unavailable:sandbox_unproven"))
    v = _codex(_units(proj))[0]["verify"]
    assert (v["verify_level"], v["verify_status"], v["verify_sandboxed"]) == (None, "unavailable", False)
    assert v["verify_reason"] == "verify_unavailable:sandbox_unproven"
    with pytest.raises(ValueError):
        ns.verify_row(a["unit_id"], UnitResult(verify_status="used", reason="x"))


def test_pass_f2p_model_from_verify_unit_is_recordable_and_joins(tmp_path):
    """PR A added pass_f2p_model (a model-added test that fails on the baseline); a schema that did
    not know it made record_verify raise and the unit's verdict was lost."""
    p, proj = _setup(tmp_path)
    a, _ = _codex(_units(proj))
    ns.record_verify(a["unit_id"], UnitResult(verify_status="pass_f2p_model", reason="f2p_model", sandboxed=True))
    v = _codex(_units(proj))[0]["verify"]
    assert (v["verify_status"], v["verify_level"], v["verify_sandboxed"]) == ("pass_f2p_model", "V1", True)


def test_verify_shadow_counts_every_status_in_its_own_bucket(tmp_path):
    """pass_f2p and pass_f2p_model are both 'verified'; fail is counted (not dropped); the rest are
    weak / unavailable. Removing the pass_f2p_model case or the fail case must turn this red."""
    rows = [_codex_row(1_800_000_200.0 + i) for i in range(6)]
    p, proj = _setup(tmp_path, rows)
    ids = [u["unit_id"] for u in _codex(_units(proj)) if u["ts"] and u["outcome"] == "unknown"][-6:]
    statuses = ["pass_f2p", "pass_f2p_model", "pass_p2p", "fail", "unavailable", "not_applicable"]
    _append(p, *[_vrow(uid, st, "x") for uid, st in zip(ids, statuses)])
    assert kpi._verify_shadow(100000) == {"verified": 2, "weak": 1, "failed": 1, "unavailable": 2}
    line = "verify (shadow): 2 verified, 1 weak, 1 failed, 2 unavailable"
    assert line in kpi.render_scorecard(kpi.compute_scorecard(days=100000, now=NOW))
