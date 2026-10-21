"""LC-10l: status distinguishes judged runs from probes. Fakes only."""
from pathlib import Path

import pytest

from llm_router.commands import local_check as C
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


@pytest.mark.parametrize("model", ["fake-3n-e4b", "fake3b"])
def test_size_glued_to_letters_is_unknown(model):
    assert C.estimate_run_seconds(model, 1, {"gpu": "x"}) == (None, "")


def test_stale_no_judge_wording_is_absent():
    root = Path(__file__).resolve().parents[1]
    for directory in ("src", "docs"):
        for path in (root / directory).rglob("*"):
            if path.is_file():
                assert "can record a FAIL" not in path.read_text(encoding="utf-8", errors="replace"), str(path)
