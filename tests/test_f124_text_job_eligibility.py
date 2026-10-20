"""F124: a pre-registered text-job verdict is recorded with provenance and shown, never acted on.

Every test uses tmp_path (the prereg directory is built here; no real prereg, no real home).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from llm_router import eligibility, eligibility_routing as er
from llm_router.commands import eligibility as cmd
from llm_router.resolver import eligibility_store as store
from llm_router.types import Complexity, TaskType

MODEL = "fixture-coder:9b"


def _prereg(tmp_path, *, verdict="PASS", k=60, n=70, jobs=("commit_message", "pr_description"),
            frozen_sha=None, used_sha=None, bar_point=0.8, job_k=(32, 28)):
    d = tmp_path / "prereg"
    (d / "run").mkdir(parents=True)
    manifest = {"local_model": MODEL, "rules": {"label": "PREREG-FIX", "bar_point": bar_point,
                                                "bar_lb": 0.6, "guard_job_min": 0.7}}
    mp = d / "MANIFEST-FIX.json"
    mp.write_text(json.dumps(manifest))
    sha = hashlib.sha256(mp.read_bytes()).hexdigest()
    (d / "FROZEN.txt").write_text(f"frozen_at_utc=x\nmanifest_sha256={frozen_sha or sha}\n")
    (d / "run" / "USED.json").write_text(json.dumps({"manifest_sha256": used_sha or sha}))
    qwen = {"overall": {"k": k, "n": n}}
    for job, jk in zip(("commit_message", "pr_description"), job_k):
        qwen[f"job={job}"] = {"k": jk, "n": 35}
    result = {"verdict": verdict, "arms": {"qwen": qwen},
              "local_eligibility_actions": [{"task_type": "text_job", "model": MODEL,
                                             "verdict": "eligible", "jobs": list(jobs)}]}
    (d / "run" / "result.json").write_text(json.dumps(result))
    return d, sha


def test_grade_text_job_bar():
    assert eligibility.grade_text_job({"k": 60, "n": 70}) == "passed"
    assert eligibility.grade_text_job({"k": 55, "n": 70}) == "failed"      # 0.786 < 0.80
    assert eligibility.grade_text_job({"k": 4, "n": 5}) == "failed"        # point ok, Wilson LB < 0.60
    for bad in (None, {}, {"k": 5}, {"k": True, "n": 5}, {"k": 6, "n": 5}, {"k": 0, "n": 0}, {"k": 1.0, "n": 2}):
        assert eligibility.grade_text_job(bad) == "unknown"


def test_build_lanes_from_a_valid_prereg(tmp_path):
    d, sha = _prereg(tmp_path)
    model, lanes = cmd.build_lanes(d, today="2026-10-20")
    assert model == MODEL and sorted(lanes) == ["commit_message", "pr_description"]
    lane = lanes["pr_description"]
    assert (lane["grade"], lane["verdict"], lane["eligible"]) == ("passed", "eligible", True)
    assert (lane["k"], lane["n"], lane["prereg"], lane["manifest_sha256"]) == (60, 70, "PREREG-FIX", sha)
    assert (lane["job_k"], lane["job_n"], lane["recorded_at"]) == (28, 35, "2026-10-20")


@pytest.mark.parametrize("kw,needle", [
    ({"frozen_sha": "0" * 64}, "does not match"),
    ({"used_sha": "1" * 64}, "does not match"),
    ({"verdict": "INCONCLUSIVE"}, "not PASS"),
    ({"verdict": "FAIL"}, "not PASS"),
    ({"jobs": ()}, "no valid text jobs"),
    ({"jobs": ("commit_message", "poetry")}, "no valid text jobs"),
    ({"k": 50}, "D-241"),
    ({"bar_point": 0.5}, "not the D-241 bar"),
    ({"job_k": (32, 20)}, "guard"),
])
def test_writer_refuses_bad_provenance(tmp_path, kw, needle):
    d, _ = _prereg(tmp_path, **kw)
    with pytest.raises(cmd.Refused, match=needle):
        cmd.build_lanes(d)


def test_manifest_edited_after_freeze_is_refused(tmp_path):
    d, _ = _prereg(tmp_path)
    mp = d / "MANIFEST-FIX.json"
    mp.write_text(mp.read_text() + " ")
    with pytest.raises(cmd.Refused, match="does not match"):
        cmd.build_lanes(d)


def test_dry_run_writes_nothing_and_apply_writes_0600(tmp_path, capsys):
    d, _ = _prereg(tmp_path)
    target = tmp_path / "state" / "local_eligibility.json"
    assert cmd.cmd_eligibility(["record", "--prereg", str(d), "--file", str(target)]) == 0
    assert not target.exists() and not target.parent.exists()
    assert "nothing written" in capsys.readouterr().out
    assert cmd.cmd_eligibility(["record", "--prereg", str(d), "--file", str(target), "--apply"]) == 0
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    data = json.loads(target.read_text())
    assert sorted(data[MODEL]["text_job"]) == ["commit_message", "pr_description"]
    assert not list(target.parent.glob("*.tmp"))
    assert store.text_job_lines(path=target) == [
        f"text_job: commit_message, pr_description — qualified (PREREG-FIX, 60/70) [{MODEL}]"]


def test_refusal_exits_nonzero_and_writes_nothing(tmp_path, capsys):
    d, _ = _prereg(tmp_path, verdict="FAIL")
    target = tmp_path / "e.json"
    assert cmd.cmd_eligibility(["record", "--prereg", str(d), "--file", str(target), "--apply"]) == 1
    assert not target.exists() and "refused" in capsys.readouterr().err


def test_cli_usage(capsys):
    assert cmd.cmd_eligibility(["--help"]) == 0
    assert cmd.cmd_eligibility([]) == 2
    assert cmd.cmd_eligibility(["record"]) == 2
    assert cmd.cmd_eligibility(["record", "--bogus"]) == 2


def test_backward_compatible_with_existing_entries(tmp_path):
    target = tmp_path / "e.json"
    old = {"qwen3.8:fx": {"qa": {"simple": {"grade": "failed", "verdict": "failed", "eligible": False,
                                           "k": 5, "n": 100, "gap_pts": 80.0, "lb_pts": -90.0}},
                          "fitness": {"x": 1}}}
    target.write_text(json.dumps(old))
    d, _ = _prereg(tmp_path)
    model, lanes = cmd.build_lanes(d)
    store.record_competence(model, "text_job", lanes, path=target)
    data = json.loads(target.read_text())
    assert data["qwen3.8:fx"] == old["qwen3.8:fx"]
    assert "text_job" in data[MODEL]
    assert store.text_job_lines(path=target)       # text_job entries do not hide qa-only models
    only_old = tmp_path / "old.json"
    only_old.write_text(json.dumps(old))
    assert store.text_job_lines(path=only_old) == []


def test_record_competence_text_job_validation(tmp_path):
    d, _ = _prereg(tmp_path)
    _, lanes = cmd.build_lanes(d)
    good = lanes["commit_message"]
    t = tmp_path / "e.json"
    for bad in ({**good, "grade": "eligible"}, {**good, "manifest_sha256": "abc"},
                {**good, "prereg": ""}, {**good, "verdict": "failed"}, {**good, "k": 10}):
        with pytest.raises(ValueError):
            store.record_competence(MODEL, "text_job", {"commit_message": bad}, path=t)
    with pytest.raises(ValueError):
        store.record_competence(MODEL, "text_job", {"simple": good}, path=t)
    with pytest.raises(ValueError):
        store.record_competence(MODEL, "nonsense", {"simple": good}, path=t)
    assert not t.exists()


def test_readers_ignore_unknown_and_malformed(tmp_path):
    t = tmp_path / "e.json"
    good = {"grade": "passed", "verdict": "eligible", "eligible": True, "k": 60, "n": 70,
            "prereg": "P", "manifest_sha256": "a" * 64}
    t.write_text(json.dumps({
        "m1": {"text_job": {"commit_message": {**good, "k": 3},          # grade disagrees with counts
                            "pr_description": {"grade": "passed"},       # no counts
                            "summarize_output": "junk", "other_job": good}},
        "m2": "junk", "m3": {"text_job": ["x"]}}))
    assert store.text_job_lines(path=t) == []
    t.write_text("not json")
    assert store.text_job_lines(path=t) == []
    assert store.text_job_lines(path=tmp_path / "absent.json") == []


def test_failed_text_job_lane_is_shown_as_failed(tmp_path):
    lane = {"grade": "failed", "verdict": "failed", "eligible": False, "k": 40, "n": 70,
            "prereg": "P", "manifest_sha256": "a" * 64}
    t = tmp_path / "e.json"
    store.record_competence(MODEL, "text_job", {"commit_message": lane}, path=t)
    assert store.text_job_lines(path=t) == [f"text_job: commit_message — failed (P, 40/70) [{MODEL}]"]


def test_filter_chain_unchanged_for_text_jobs(tmp_path, monkeypatch):
    """A passed text_job record neither filters nor reorders any chain."""
    t = tmp_path / "local_eligibility.json"
    d, _ = _prereg(tmp_path)
    model, lanes = cmd.build_lanes(d)
    store.record_competence(model, "text_job", lanes, path=t)
    monkeypatch.setattr(er, "eligibility_path", lambda: t)
    er.reset_cache()
    chain = [f"ollama/{MODEL}", "gemini/gemini-2.5-flash"]
    for tt in (TaskType.QUERY, TaskType.CODE, TaskType.GENERATE):
        for cx in (Complexity.SIMPLE, Complexity.MODERATE):
            assert er.filter_chain(list(chain), tt, cx) == chain
    assert er.lane_key("text_job", "simple") is None
    assert "text_job" not in er.TASK_LANE.values()
    from llm_router.proxy import local_gate
    assert local_gate.eligible_model(json.loads(t.read_text()), "text_job", "commit_message") is None
    er.reset_cache()


def test_status_and_doctor_show_the_lane(tmp_path, monkeypatch):
    t = tmp_path / "local_eligibility.json"
    d, _ = _prereg(tmp_path)
    store.record_competence(*cmd.build_lanes(d)[:1], "text_job", cmd.build_lanes(d)[1], path=t)
    monkeypatch.setattr(store, "eligibility_path", lambda: t)
    from llm_router.ui.status_premium import PremiumStatusCommand
    text = str(PremiumStatusCommand().render_text_job_qualification())
    assert "commit_message, pr_description" in text and "qualified (PREREG-FIX, 60/70)" in text
    monkeypatch.setattr(store, "eligibility_path", lambda: tmp_path / "none.json")
    assert str(PremiumStatusCommand().render_text_job_qualification()) == ""
