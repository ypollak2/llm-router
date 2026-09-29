"""Phase 0.1 — `scripts/release/outcome_gate.py`, the release gate over the
outcome audit.

It fails a release when:
  * the corrected `used` rate drops below the previous release's recorded
    value, or
  * an inflated source leaks into a headline figure: a door_call counted as
    used, a dispatch flag counted as used, a flat per-turn agentic credit, or a
    headline that does not match a recount of the rows.

On the first run (no history) it records a baseline and passes.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str = "_outcome_gate_under_test"):
    path = REPO / "scripts" / "release" / "outcome_gate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


G = _load()


def _rows(n_used: int, n_total: int, signal: str = "tool_result_reused"):
    rows = []
    for i in range(n_total):
        used = i < n_used
        rows.append({
            "unit_id": f"routed_mcp:s:{i}", "kind": "routed_mcp",
            "label": "used" if used else "unused",
            "signal": signal if used else "tool_result_not_reused",
            "confidence": "medium", "human_reviewed": False,
        })
    return rows


def _summary(rows, release="1.0.0"):
    used = sum(r["label"] == "used" for r in rows)
    return {
        "release": release,
        "headline": {"used": used, "attempted": len(rows),
                     "rate": used / len(rows) if rows else None,
                     "too_few_to_tell": len(rows) < 50},
        "excluded_inflated_sources": {
            "door_call_verified_used": {"n": 5, "counted_as_used": 0},
            "agentic_flat_credits": {"n": 3, "counted_as_used": 0},
            "codex_dispatch_flags": {"n": 2, "counted_as_used": 0},
        },
        "judge": {"enabled": False, "status": "OFF"},
    }


def _write(tmp_path, rows, summary, release):
    d = tmp_path / "art"
    d.mkdir(exist_ok=True)
    (d / f"outcome_audit_{release}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (d / f"outcome_audit_{release}.summary.json").write_text(json.dumps(summary))
    return d


def _run(tmp_path, rows, summary, release, history=None, extra=()):
    d = _write(tmp_path, rows, summary, release)
    hist = tmp_path / "history.json"
    if history is not None:
        hist.write_text(json.dumps(history))
    rc = G.main(["--artifact-dir", str(d), "--release", release, "--history", str(hist), *extra])
    return rc, hist


def test_first_run_records_a_baseline_and_passes(tmp_path, capsys):
    rows = _rows(2, 100)
    rc, hist = _run(tmp_path, rows, _summary(rows), "1.0.0")
    assert rc == 0, capsys.readouterr().out
    data = json.loads(hist.read_text())
    assert data["releases"][-1]["release"] == "1.0.0"
    assert data["releases"][-1]["used"] == 2
    assert "baseline recorded" in capsys.readouterr().out


def test_drop_below_previous_release_fails(tmp_path, capsys):
    history = {"releases": [{"release": "1.0.0", "used_rate": 0.05, "used": 5, "attempted": 100}]}
    rows = _rows(2, 100)
    rc, _ = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "dropped below" in out


def test_equal_or_higher_than_previous_passes(tmp_path, capsys):
    history = {"releases": [{"release": "1.0.0", "used_rate": 0.02, "used": 2, "attempted": 100}]}
    rows = _rows(3, 100)
    rc, hist = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history)
    assert rc == 0, capsys.readouterr().out
    assert [r["release"] for r in json.loads(hist.read_text())["releases"]] == ["1.0.0", "1.1.0"]


def test_rerun_of_the_same_release_compares_to_the_one_before(tmp_path, capsys):
    history = {"releases": [
        {"release": "1.0.0", "used_rate": 0.02, "used": 2, "attempted": 100},
        {"release": "1.1.0", "used_rate": 0.50, "used": 50, "attempted": 100},
    ]}
    rows = _rows(3, 100)
    rc, _ = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history)
    assert rc == 0, capsys.readouterr().out


def test_door_call_counted_as_used_fails(tmp_path, capsys):
    rows = _rows(2, 100, signal="door_call")
    rc, _ = _run(tmp_path, rows, _summary(rows), "1.0.0")
    out = capsys.readouterr().out
    assert rc == 1
    assert "door_call" in out


def test_codex_dispatch_flag_counted_as_used_fails(tmp_path, capsys):
    rows = _rows(2, 100, signal="agent_route_codex_delegated")
    rc, _ = _run(tmp_path, rows, _summary(rows), "1.0.0")
    assert rc == 1
    assert "agent_route_codex_delegated" in capsys.readouterr().out


def test_unlisted_signal_counted_as_used_fails(tmp_path, capsys):
    """An allowlist, not a denylist: a new signal must be declared
    content-bearing before it can put a unit in the numerator."""
    rows = _rows(2, 100, signal="some_new_heuristic")
    rc, _ = _run(tmp_path, rows, _summary(rows), "1.0.0")
    assert rc == 1


def test_flat_agentic_credit_in_headline_fails(tmp_path, capsys):
    rows = _rows(2, 100)
    s = _summary(rows)
    s["excluded_inflated_sources"]["agentic_flat_credits"]["counted_as_used"] = 3
    rc, _ = _run(tmp_path, rows, s, "1.0.0")
    out = capsys.readouterr().out
    assert rc == 1
    assert "agentic_flat_credits" in out


def test_headline_that_disagrees_with_the_rows_fails(tmp_path, capsys):
    rows = _rows(2, 100)
    s = _summary(rows)
    s["headline"]["used"] = 7  # something other than the rows fed the headline
    rc, _ = _run(tmp_path, rows, s, "1.0.0")
    assert rc == 1
    assert "recount" in capsys.readouterr().out


def test_nothing_to_audit_fails_without_a_reason(tmp_path, capsys):
    rc, _ = _run(tmp_path, [], _summary([]), "1.0.0")
    assert rc == 1


def test_too_few_to_tell_fails_without_a_reason(tmp_path, capsys):
    rows = _rows(1, 10)
    rc, _ = _run(tmp_path, rows, _summary(rows), "1.0.0")
    out = capsys.readouterr().out
    assert rc == 1
    assert "too few to tell" in out


def test_skip_reason_is_explicit_and_printed(tmp_path, capsys):
    rc, _ = _run(tmp_path, [], _summary([]), "1.0.0",
                 extra=("--skip-outcome", "fresh machine, no transcripts"))
    assert rc == 0
    assert "fresh machine, no transcripts" in capsys.readouterr().out


def test_skip_does_not_excuse_a_leak(tmp_path, capsys):
    rows = _rows(2, 100, signal="door_call")
    rc, _ = _run(tmp_path, rows, _summary(rows), "1.0.0", extra=("--skip-outcome", "x"))
    assert rc == 1


def test_missing_artifacts_fail(tmp_path, capsys):
    rc = G.main(["--artifact-dir", str(tmp_path / "nope"), "--release", "1.0.0",
                 "--history", str(tmp_path / "h.json")])
    assert rc == 1


def test_pre_release_verify_runs_the_audit_and_the_gate():
    sh = (REPO / "scripts" / "release" / "pre-release-verify.sh").read_text()
    assert "scripts/release/outcome_audit.py" in sh
    assert "scripts/release/outcome_gate.py" in sh
    assert "--skip-outcome" in sh


def test_rerun_of_a_middle_release_compares_to_its_predecessor_not_a_later_one(tmp_path, capsys):
    history = {"releases": [
        {"release": "1.0.0", "used_rate": 0.02, "used": 2, "attempted": 100},
        {"release": "1.1.0", "used_rate": 0.03, "used": 3, "attempted": 100},
        {"release": "1.2.0", "used_rate": 0.50, "used": 50, "attempted": 100},
    ]}
    rows = _rows(3, 100)
    rc, hist = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history)
    assert rc == 0, capsys.readouterr().out
    assert [r["release"] for r in json.loads(hist.read_text())["releases"]] == ["1.0.0", "1.1.0", "1.2.0"]


def test_stale_headline_rate_fails(tmp_path, capsys):
    rows = _rows(2, 100)
    s = _summary(rows)
    s["headline"]["rate"] = 0.5
    rc, _ = _run(tmp_path, rows, s, "1.0.0")
    assert rc == 1
    assert "headline rate" in capsys.readouterr().out
