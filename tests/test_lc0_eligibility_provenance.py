"""LC-0: local-check provenance on the text_job lane, the user-run writer, freshness, display.

Every test uses tmp_path (no real home, no real eligibility file).
"""
from __future__ import annotations

import calendar
import json
import stat
import time

import pytest

from llm_router.resolver import eligibility_store as store

MODEL = "fixture-coder:9b"
SHA = "b" * 64
NOW = calendar.timegm(time.strptime("2026-10-20", "%Y-%m-%d"))
PROV = {"bank_version": "v1", "bank_sha256": SHA, "settings_hash": "s" * 16,
        "model_digest": "sha256:aaa", "runtime": "ollama", "run_id": "r1", "date": "2026-10-20",
        "judge": {"backend": "api", "model_served": "j-1", "mode": "key"},
        "scope": {"max_prompt_chars": 8000}}
PASS = {"k": 60, "n": 70, "verdict": "PASS"}


def _owner_style_file(tmp_path):
    prereg = {"grade": "passed", "verdict": "eligible", "eligible": True, "source": "prereg",
              "prereg": "PREREG-Q37", "manifest_sha256": "a" * 64, "k": 60, "n": 70,
              "point": 0.857, "wilson_lb": 0.75, "job_k": 32, "job_n": 35, "recorded_at": "2026-10-19"}
    data = {MODEL: {"text_job": {"commit_message": prereg, "pr_description": dict(prereg)}},
            "qwen3.8:fx": {"qa": {"simple": {"grade": "eligible", "verdict": "eligible", "eligible": True}},
                           "code": {"moderate": {"grade": "failed"}}, "future_field": {"x": 1}}}
    t = tmp_path / "e.json"
    t.write_text(json.dumps(data))
    return t, data


def test_backcompat_prereg_and_other_entries(tmp_path):
    t, data = _owner_style_file(tmp_path)
    assert store.text_job_lines(path=t) == [
        f"text_job: commit_message, pr_description — qualified (PREREG-Q37, 60/70) [{MODEL}]"]
    store.record_local_check("other:1b", {"commit_message": PASS}, PROV, path=t)
    after = json.loads(t.read_text())
    assert after[MODEL] == data[MODEL] and after["qwen3.8:fx"] == data["qwen3.8:fx"]


def test_local_check_record_roundtrip_and_perms(tmp_path):
    t = tmp_path / "e.json"
    assert store.record_local_check(MODEL, {"commit_message": PASS, "pr_description": PASS},
                                    {**PROV, "attempts": 2}, path=t) is True
    lane = json.loads(t.read_text())[MODEL]["text_job"]["commit_message"]
    assert lane["source"] == "local_check" and lane["attempts"] == 2 and lane["grade"] == "passed"
    assert lane["bank_sha256"] == SHA and lane["manifest_sha256"] == SHA and lane["model_digest"] == "sha256:aaa"
    assert stat.S_IMODE(t.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    assert store.record_local_check(MODEL, {"commit_message": {"k": 40, "n": 70, "verdict": "FAIL"}},
                                    PROV, path=t) is True
    assert json.loads(t.read_text())[MODEL]["text_job"]["commit_message"]["grade"] == "failed"
    store.record_local_check(MODEL, {"pr_description": PASS}, PROV, path=t)
    assert "attempts" in json.loads(t.read_text())[MODEL]["text_job"]["pr_description"]


def test_lane_validation(tmp_path):
    t = tmp_path / "e.json"
    store.record_local_check(MODEL, {"commit_message": PASS}, PROV, path=t)
    good = json.loads(t.read_text())[MODEL]["text_job"]["commit_message"]
    store.record_competence(MODEL, "text_job", {"pr_description": {**good, "future": {"a": 1}}}, path=t)
    for drop in ("bank_version", "bank_sha256", "settings_hash"):
        bad = {k: v for k, v in good.items() if k != drop}
        with pytest.raises(ValueError):
            store.record_competence(MODEL, "text_job", {"commit_message": bad}, path=t)
    for bad in ({**good, "bank_sha256": "xyz"}, {**good, "k": 10}, {**good, "source": "other"},
                {**good, "grade": "failed"}):
        with pytest.raises(ValueError):
            store.record_competence(MODEL, "text_job", {"commit_message": bad}, path=t)


def test_writer_refusals(tmp_path):
    t = tmp_path / "e.json"
    for res in ({"k": 40, "n": 70, "verdict": "PASS"}, {"k": 60, "n": 70, "verdict": "FAIL"},
                {"k": 6, "n": 5, "verdict": "PASS"}, {"k": 60, "n": 70, "verdict": "MAYBE"}, "junk"):
        with pytest.raises(ValueError):
            store.record_local_check(MODEL, {"commit_message": res}, PROV, path=t)
    with pytest.raises(ValueError):
        store.record_local_check(MODEL, {"tech_op": PASS}, PROV, path=t)
    for drop in ("bank_version", "bank_sha256", "settings_hash"):
        with pytest.raises(ValueError):
            store.record_local_check(MODEL, {"commit_message": PASS},
                                     {k: v for k, v in PROV.items() if k != drop}, path=t)
    # one bad job refuses the whole call: nothing written
    with pytest.raises(ValueError):
        store.record_local_check(MODEL, {"commit_message": PASS,
                                         "pr_description": {"k": 40, "n": 70, "verdict": "PASS"}}, PROV, path=t)
    assert not t.exists()


def test_inconclusive_and_invalid_never_written(tmp_path):
    t = tmp_path / "e.json"
    inc = {"k": 60, "n": 70, "verdict": "INCONCLUSIVE"}
    inv = {"k": 60, "n": 70, "verdict": "INVALID"}
    assert store.record_local_check(MODEL, {"commit_message": inc, "pr_description": inv}, PROV, path=t) is False
    assert not t.exists()
    assert store.record_local_check(MODEL, {"commit_message": inc, "pr_description": PASS}, PROV, path=t) is True
    assert list(json.loads(t.read_text())[MODEL]["text_job"]) == ["pr_description"]


def test_default_attempts_and_date(tmp_path):
    t = tmp_path / "e.json"
    p = {k: v for k, v in PROV.items() if k != "date"}
    store.record_local_check(MODEL, {"commit_message": PASS}, p, path=t)
    lane = json.loads(t.read_text())[MODEL]["text_job"]["commit_message"]
    assert lane["attempts"] == 1 and len(lane["date"]) == 10


def test_freshness():
    lane = {"model_digest": "d1", "date": "2026-01-01"}
    assert store.lane_is_fresh(lane, "d1", NOW)               # match, however old
    assert not store.lane_is_fresh(lane, "d2", NOW)           # mismatch
    un = {"date": "2026-10-01"}
    assert store.lane_is_fresh(un, "anything", NOW)           # unverified, 19 days
    assert not store.lane_is_fresh({"date": "2026-09-19"}, None, NOW)   # 31 days
    assert store.lane_is_fresh({"date": "2026-09-20"}, None, NOW)       # exactly 30
    assert store.lane_is_fresh(lane, None, calendar.timegm(time.strptime("2026-01-10", "%Y-%m-%d")))
    assert not store.lane_is_fresh(lane, None, NOW)           # digest uncheckable -> 30 d rule
    assert not store.lane_is_fresh({}, None, NOW) and not store.lane_is_fresh({"date": "junk"}, None, NOW)
    assert not store.lane_is_fresh({"date": "2026-11-01"}, None, NOW)   # future date


def test_display_lines(tmp_path):
    t = tmp_path / "e.json"
    store.record_local_check(MODEL, {"commit_message": PASS, "pr_description": PASS}, PROV, path=t)
    store.record_local_check("bad:1b", {"commit_message": {"k": 40, "n": 70, "verdict": "FAIL"}}, PROV, path=t)
    ok = f"{MODEL} — commit_message, pr_description passed 60/70 (bank v1, 2026-10-20)"
    failed = "bad:1b — commit_message failed 40/70 (bank v1, 2026-10-20)"
    assert store.text_job_lines(path=t, now=NOW) == [failed, ok]
    assert store.text_job_lines(path=t, current_digests={MODEL: "sha256:aaa"}, now=NOW)[1] == ok
    stale = store.text_job_lines(path=t, current_digests={MODEL: "sha256:zzz"}, now=NOW)
    assert stale[1] == f"{MODEL} — commit_message, pr_description stale — model changed"
    assert stale[0] == failed
    # unknown current digest for this model: shown without staleness
    assert store.text_job_lines(path=t, current_digests={"x": "d"}, now=NOW + 400 * 86400) == [failed, ok]
