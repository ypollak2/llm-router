"""LC-10l: status distinguishes judged runs from probes. Fakes only."""
import json
from pathlib import Path

import pytest

from llm_router.commands import local_check as C
from llm_router.local_check import attempts as A
from llm_router.local_check import session as SE
from tests.test_lc5_scoring import BANK, Clock, FakeAdapter, check, fails
from tests.test_lc6_cli import MODEL, deps, run


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))


def attempts_line(tmp_path):
    d, sink, _ = deps(tmp_path)
    assert run(["status"], d) == C.EXIT_OK
    lines = [line for line in sink.text.splitlines() if line.startswith("attempts:")]
    assert len(lines) == 1
    return lines[0]


def test_status_probe_only_has_no_judged_last(tmp_path):
    out = SE.run_check(check(tmp_path, no_judge=True))
    assert out.result.verdict == "PROBE"
    line = attempts_line(tmp_path)
    assert line == (
        f"attempts: {MODEL} (bank v{BANK.version}): 0 of 2 judged draws used"
        "; no judged run yet; 1 programmatic-only run(s) (--no-judge) not counted"
        "; latest probe result: PROBE 70/70 (probe, not a judged verdict)"
    )
    assert "last " not in line


def test_status_judged_pass_then_probe_fail(tmp_path):
    clock = Clock()
    judged = SE.run_check(check(tmp_path, clock=clock))
    assert judged.result.verdict == "PASS"
    clock.t += 3600
    probe = SE.run_check(check(tmp_path, FakeAdapter(fails(70, "progfail")), clock=clock, no_judge=True))
    assert probe.result.verdict == "NOT_PASSED"
    line = attempts_line(tmp_path)
    assert line == (
        f"attempts: {MODEL} (bank v{BANK.version}): 1 of 2 judged draws used"
        "; last PASS 70/70 2027-01-15"
        "; 1 programmatic-only run(s) (--no-judge) not counted"
        "; latest probe result: NOT_PASSED 0/70 (probe, not a judged verdict)"
    )


def test_status_judged_only_has_no_probe_suffix(tmp_path):
    out = SE.run_check(check(tmp_path))
    assert out.result.verdict == "PASS"
    assert attempts_line(tmp_path) == (
        f"attempts: {MODEL} (bank v{BANK.version}): 1 of 2 judged draws used"
        "; last PASS 70/70 2027-01-15"
    )


def test_status_inconclusive_rerun_keeps_earlier_verdict(tmp_path):
    clock = Clock()
    first = SE.run_check(check(tmp_path, FakeAdapter(fails(10)), clock=clock))
    assert first.result.verdict == "PASS"
    assert (first.result.k, first.result.n_assigned) == (60, 70)
    clock.t += A.COOLDOWN_S + 1
    second = SE.run_check(check(tmp_path, FakeAdapter({**fails(10), **fails(25, draw=2)}), clock=clock))
    assert second.result.verdict == "INCONCLUSIVE"
    assert attempts_line(tmp_path) == (
        f"attempts: {MODEL} (bank v{BANK.version}): 2 of 2 judged draws used"
        "; last INCONCLUSIVE 105/140 2027-01-16 (not recorded; recorded verdict PASS kept)"
    )


def inject_first(tmp_path, entry_fn):
    """Prepend a synthetic earlier attempt-log entry for the config of the existing log (earlier ts, same key)."""
    log = A.AttemptLog(tmp_path / "runs")
    last = log.entries()[0]
    early = entry_fn(last)
    log.path.write_text(json.dumps(early) + "\n" + log.path.read_text())


def test_status_burnt_invalid_then_inconclusive_has_no_kept_suffix(tmp_path):
    out = SE.run_check(check(tmp_path, FakeAdapter(fails(18)), clock=Clock()))
    assert out.result.verdict == "INCONCLUSIVE"
    inject_first(tmp_path, lambda e: {"config_key": e["config_key"], "config": e["config"], "draw": 1, "ts": 0.5, "date": "2027-01-14",
                                      "state": "burnt", "strike": True, "consumed": True, "verdict": "INVALID", "rows": [], "k": None})
    line = attempts_line(tmp_path)
    assert "2 of 2 judged draws used" in line and "last INCONCLUSIVE" in line
    assert "kept" not in line and "not recorded" not in line


def test_status_not_passed_then_inconclusive_names_recorded_verdict(tmp_path):
    clock = Clock()
    first = SE.run_check(check(tmp_path, FakeAdapter({i.id: "progfail" for i in BANK.draw(1)[42:57]}), clock=clock))   # 42 ok, then 15 programmatic failures
    assert first.result.verdict == "NOT_PASSED"
    clock.t += A.COOLDOWN_S + 1
    second = SE.run_check(check(tmp_path, FakeAdapter(fails(5, draw=2)), clock=clock))      # 42 + 65 = 107 of 140: between kfail and kmin
    assert second.status == "scored", second.reason
    assert second.result.verdict == "INCONCLUSIVE"
    assert "last INCONCLUSIVE" in attempts_line(tmp_path)
    assert attempts_line(tmp_path).endswith("(not recorded; recorded verdict NOT_PASSED kept)")


def test_status_removed_record_then_inconclusive_has_no_kept_suffix(tmp_path):
    clock = Clock()
    assert SE.run_check(check(tmp_path, FakeAdapter(fails(10)), clock=clock)).result.verdict == "PASS"
    clock.t += A.COOLDOWN_S + 1
    assert SE.run_check(check(tmp_path, FakeAdapter({**fails(10), **fails(25, draw=2)}), clock=clock)).result.verdict == "INCONCLUSIVE"
    assert "recorded verdict PASS kept" in attempts_line(tmp_path)
    (tmp_path / "elig.json").unlink()
    line = attempts_line(tmp_path)
    assert "last INCONCLUSIVE" in line and "kept" not in line


def test_status_inconclusive_only_has_no_kept_verdict_suffix(tmp_path):
    out = SE.run_check(check(tmp_path, FakeAdapter(fails(18)), clock=Clock()))
    assert out.result.verdict == "INCONCLUSIVE"
    assert attempts_line(tmp_path) == (
        f"attempts: {MODEL} (bank v{BANK.version}): 1 of 2 judged draws used"
        "; last INCONCLUSIVE 52/70 2027-01-15"
    )


@pytest.mark.parametrize("model", ["fake-3n-e4b", "fake3b"])
def test_size_glued_to_letters_is_unknown(model):
    assert C.estimate_run_seconds(model, 1, {"gpu": "x"}) == (None, "")


def test_stale_no_judge_wording_is_absent():
    root = Path(__file__).resolve().parents[1]
    paths = list(root.glob("README*.md"))
    for directory in ("src", "docs", "changelog.d"):
        paths.extend((root / directory).rglob("*"))
    for path in paths:
        if path.is_file():
            assert "can record a FAIL" not in path.read_text(encoding="utf-8", errors="replace"), str(path)
