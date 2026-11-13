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
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

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


# ── BIND W3: not_applicable rows and the audit version ───────────────────────

def _rows_with_na(n_used: int, n_total: int, n_na: int, na_signal: str = "probe_fixed_reply"):
    rows = _rows(n_used, n_total)
    for i in range(n_na):
        rows.append({"unit_id": f"routed_mcp:s:na{i}", "kind": "routed_mcp", "label": "not_applicable",
                     "signal": na_signal, "confidence": "high", "human_reviewed": False})
    return rows


def _summary_v(rows, release="1.0.0", version=2):
    attempted = [r for r in rows if r["label"] != "not_applicable"]
    used = sum(r["label"] == "used" for r in rows)
    s = _summary(attempted, release)
    s["audit_version"] = version
    s["headline"].update(used=used, attempted=len(attempted), rate=used / len(attempted) if attempted else None,
                         not_applicable=len(rows) - len(attempted))
    s["not_applicable"] = {"n": len(rows) - len(attempted), "by_signal": {"probe_fixed_reply": len(rows) - len(attempted)},
                           "used_rate_if_included": used / len(rows) if rows else None}
    return s


def test_not_applicable_rows_are_outside_the_denominator(tmp_path, capsys):
    rows = _rows_with_na(3, 100, 40)
    rc, hist = _run(tmp_path, rows, _summary_v(rows), "1.0.0")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "40 unit(s) are not_applicable" in out
    assert "counted as attempted: 2.143%" in out and "used rate 3/100 = 3.000%" in out    # both rates are shown
    entry = json.loads(hist.read_text())["releases"][-1]
    assert (entry["used"], entry["attempted"], entry["not_applicable"], entry["audit_version"]) == (3, 100, 40, 2)


def test_a_headline_that_counts_not_applicable_as_attempted_fails_the_recount(tmp_path, capsys):
    rows = _rows_with_na(3, 100, 40)
    s = _summary_v(rows)
    s["headline"]["attempted"] = 140
    s["headline"]["rate"] = 3 / 140
    rc, _ = _run(tmp_path, rows, s, "1.0.0")
    assert rc == 1 and "recount" in capsys.readouterr().out


def test_not_applicable_needs_an_allowed_reason(tmp_path, capsys):
    rows = _rows_with_na(3, 100, 5, na_signal="tool_result_not_reused")      # hiding an unused unit
    rc, _ = _run(tmp_path, rows, _summary_v(rows), "1.0.0")
    out = capsys.readouterr().out
    assert rc == 1 and "not an allowed not_applicable reason" in out


def test_skip_does_not_excuse_an_unallowed_not_applicable(tmp_path):
    rows = _rows_with_na(0, 3, 5, na_signal="made_up")
    rc, _ = _run(tmp_path, rows, _summary_v(rows), "1.0.0", extra=("--skip-outcome", "x"))
    assert rc == 1


V1_ENTRY = {"release": "15.4.0", "used_rate": 2 / 873, "used": 2, "attempted": 873}     # no audit_version: v1


def test_a_cross_version_comparison_is_refused_by_default(tmp_path, capsys):
    rows = _rows(10, 100)
    rc, hist = _run(tmp_path, rows, _summary_v(rows, "16.0.1"), "16.0.1", {"releases": [V1_ENTRY]})
    out = capsys.readouterr().out
    assert rc == 1
    assert "AUDIT VERSION MISMATCH" in out and "audit v2" in out and "audit v1" in out
    assert "--cross-version rebaseline" in out and "--cross-version compare" in out
    assert [e["release"] for e in json.loads(hist.read_text())["releases"]] == ["15.4.0"]     # nothing written


def test_rebaseline_passes_without_comparing_and_records_why(tmp_path, capsys):
    rows = _rows(0, 100)           # 0% is far below 15.4.0's 0.229%: a rebaseline must not compare
    rc, hist = _run(tmp_path, rows, _summary_v(rows, "16.0.1"), "16.0.1", {"releases": [V1_ENTRY]},
                    extra=("--cross-version", "rebaseline"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "REBASELINED" in out and "NOT compared" in out
    entry = json.loads(hist.read_text())["releases"][-1]
    assert entry["audit_version"] == 2
    assert entry["cross_version"]["policy"] == "rebaseline" and entry["cross_version"]["against"]["release"] == "15.4.0"
    assert entry["cross_version"]["against"]["audit_version"] == 1


def test_compare_across_versions_is_labelled_and_can_fail(tmp_path, capsys):
    low, high = _rows(0, 100), _rows(5, 100)
    rc, hist = _run(tmp_path, low, _summary_v(low, "16.0.1"), "16.0.1", {"releases": [V1_ENTRY]},
                    extra=("--cross-version", "compare"))
    out = capsys.readouterr().out
    assert rc == 1 and "CROSS-VERSION" in out and "not like for like" in out
    rc, hist = _run(tmp_path, high, _summary_v(high, "16.0.1"), "16.0.1", {"releases": [V1_ENTRY]},
                    extra=("--cross-version", "compare"))
    out = capsys.readouterr().out
    assert rc == 0 and "PASS (CROSS-VERSION" in out
    assert json.loads(hist.read_text())["releases"][-1]["cross_version"]["policy"] == "compare"


def test_a_rate_exactly_equal_to_the_recorded_one_passes_like_for_like_and_across_versions(tmp_path, capsys):
    """Only a LOWER rate fails, in both comparisons: the same version (a later release) and --cross-version compare."""
    same_version = {"releases": [{"release": "1.0.0", "audit_version": 2, "used_rate": 0.03, "used": 3, "attempted": 100}]}
    rows = _rows(3, 100)
    rc, _ = _run(tmp_path, rows, _summary_v(rows, "1.0.1"), "1.0.1", same_version)
    assert rc == 0, capsys.readouterr().out
    rows = _rows(2, 873)                                                   # exactly 15.4.0's recorded 2 of 873
    (tmp_path / "x").mkdir()
    rc, _ = _run(tmp_path / "x", rows, _summary_v(rows, "16.0.1"), "16.0.1", {"releases": [V1_ENTRY]},
                 extra=("--cross-version", "compare"))
    assert rc == 0 and "PASS (CROSS-VERSION" in capsys.readouterr().out


def test_after_a_rebaseline_the_next_release_compares_like_for_like(tmp_path, capsys):
    history = {"releases": [V1_ENTRY, {"release": "16.0.1", "audit_version": 2, "used_rate": 0.05, "used": 5, "attempted": 100}]}
    rows = _rows(3, 100)           # below the v2 entry, above nothing else
    rc, _ = _run(tmp_path, rows, _summary_v(rows, "16.0.2"), "16.0.2", history)
    out = capsys.readouterr().out
    assert rc == 1 and "dropped below 16.0.1" in out and "MISMATCH" not in out
    rows = _rows(6, 100)
    rc, hist = _run(tmp_path, rows, _summary_v(rows, "16.0.2"), "16.0.2", history)
    assert rc == 0 and "MISMATCH" not in capsys.readouterr().out


def test_entries_and_summaries_without_a_version_are_version_1(tmp_path, capsys):
    assert G.audit_version({}) == 1 and G.audit_version({"audit_version": 2}) == 2 and G.audit_version({"audit_version": "x"}) == 1
    history = {"releases": [{"release": "1.0.0", "used_rate": 0.02, "used": 2, "attempted": 100}]}
    rows = _rows(3, 100)
    rc, _ = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history)     # neither has a version: v1 vs v1
    assert rc == 0 and "MISMATCH" not in capsys.readouterr().out


def test_a_person_may_label_a_unit_not_applicable(tmp_path, capsys):
    rows = _rows_with_na(3, 100, 1)
    rows[-1].update(signal="human_review", human_reviewed=True)
    rc, _ = _run(tmp_path, rows, _summary_v(rows), "1.0.0")
    assert rc == 0, capsys.readouterr().out
    rows[-1].update(human_reviewed=False)                       # a machine claiming a person's reading
    rc, _ = _run(tmp_path, rows, _summary_v(rows), "1.0.0")
    assert rc == 1 and "not an allowed not_applicable reason" in capsys.readouterr().out


# ── pre-release-verify.sh forwards a one-time rebaseline (owner decision D-359) ─

PRV = REPO / "scripts" / "release" / "pre-release-verify.sh"


def _prv_harness(tmp_path, history, rows=None):
    """The script's own colour set, argument parser and step 13 (cut out of the real file by
    its markers), run against a stub `uv`: the audit step is a no-op and the gate step runs the
    REAL outcome_gate.py on a temp artifact and history, after echoing the arguments the script
    passed. The rest of the script (git, version sync, the test suites) is not run."""
    sh = PRV.read_text(encoding="utf-8")
    colours = sh[sh.index("RED="):sh.index('SKIP_QUALITY_REASON=""')]
    parse = sh[sh.index('SKIP_QUALITY_REASON=""'):sh.index("# P3.2:")]
    step13 = sh[sh.index("# 13. Outcome audit"):sh.index('echo -e "${GREEN}========================================${NC}"')]
    assert "--outcome-cross-version" in parse and "outcome_gate.py" in step13       # the cut found the real code
    rows = rows if rows is not None else _rows(5, 100)
    art = _write(tmp_path, rows, _summary_v(rows, "9.9.9"), "9.9.9")
    hist = tmp_path / "history.json"
    if history is not None:                                  # None: keep the history a previous run left
        hist.write_text(json.dumps(history))
    stub = textwrap.dedent('''
        uv() {
            shift 2                                  # "run python"
            local script="$1"; shift
            case "$script" in
                scripts/release/outcome_audit.py) echo "STUB audit: $*" ;;
                scripts/release/outcome_gate.py)
                    echo "GATE ARGS: $*"
                    "$PYBIN" "$REPO/$script" --artifact-dir "$ART" --release 9.9.9 --history "$HIST" "$@" ;;
                *) echo "unexpected uv call: $script" >&2; return 99 ;;
            esac
        }
    ''')
    script = tmp_path / "step13.sh"
    script.write_text("set -e\n" + colours + stub + parse + step13)
    env = {**os.environ, "PYBIN": sys.executable, "REPO": str(REPO), "ART": str(art), "HIST": str(hist)}
    return script, env, hist


def _run_prv(tmp_path, history, *args, rows=None):
    script, env, hist = _prv_harness(tmp_path, history, rows)
    p = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=120)
    return p.returncode, p.stdout + p.stderr, hist


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@needs_bash
def test_a_version_mismatch_fails_the_release_script_without_the_flag(tmp_path):
    rc, out, hist = _run_prv(tmp_path, {"releases": [V1_ENTRY]})
    assert rc == 1, out
    assert "AUDIT VERSION MISMATCH" in out and "Outcome gate failed" in out
    assert "--outcome-cross-version rebaseline|compare" in out          # the refusal names the flag the script takes
    assert "--cross-version" not in out.split("GATE ARGS:")[1].splitlines()[0]         # no flag given, none forwarded
    assert [e["release"] for e in json.loads(hist.read_text())["releases"]] == ["15.4.0"]       # nothing written


@needs_bash
def test_the_rebaseline_flag_is_forwarded_and_records_the_new_baseline(tmp_path):
    rc, out, hist = _run_prv(tmp_path, {"releases": [V1_ENTRY]}, "--outcome-cross-version", "rebaseline")
    assert rc == 0, out
    assert "GATE ARGS: --cross-version rebaseline" in out and "REBASELINED" in out
    entry = json.loads(hist.read_text())["releases"][-1]
    assert entry["release"] == "9.9.9" and entry["audit_version"] == 2
    assert entry["cross_version"]["policy"] == "rebaseline" and entry["cross_version"]["against"]["release"] == "15.4.0"


@needs_bash
def test_the_compare_flag_is_forwarded_beside_skip_outcome(tmp_path):
    rc, out, _ = _run_prv(tmp_path, {"releases": [V1_ENTRY]}, "--skip-outcome", "fresh machine",
                          "--outcome-cross-version", "compare")
    assert rc == 0, out
    assert "GATE ARGS: --skip-outcome fresh machine --cross-version compare" in out
    assert "PASS (CROSS-VERSION" in out


@needs_bash
@pytest.mark.parametrize("args", [("--outcome-cross-version",), ("--outcome-cross-version", "refuse"),
                                  ("--outcome-cross-version", "yes")])
def test_the_flag_takes_rebaseline_or_compare_and_nothing_else(tmp_path, args):
    rc, out, hist = _run_prv(tmp_path, {"releases": [V1_ENTRY]}, *args)
    assert rc == 1 and "takes rebaseline or compare" in out and "GATE ARGS" not in out
    assert [e["release"] for e in json.loads(hist.read_text())["releases"]] == ["15.4.0"]


@needs_bash
def test_a_same_version_history_needs_no_flag_and_none_is_forwarded(tmp_path):
    history = {"releases": [{"release": "16.0.0", "audit_version": 2, "used_rate": 0.02, "used": 2, "attempted": 100}]}
    rc, out, _ = _run_prv(tmp_path, history)               # 5 of 100 against a recorded 2 of 100, both version 2
    assert rc == 0, out
    assert "--cross-version" not in out.split("GATE ARGS:")[1].splitlines()[0] and "MISMATCH" not in out


def test_the_script_header_documents_the_flag():
    head = PRV.read_text(encoding="utf-8").split("set -e")[0]
    assert "[--outcome-cross-version rebaseline|compare]" in head                             # the usage line
    assert "# --outcome-cross-version rebaseline|compare is forwarded to step 13" in head    # and its paragraph
    assert "D-359" in head and "FAILS" in head and "nothing passes silently" in head


# ── a recorded baseline is sticky (the PR #816 review, S1) ───────────────────

def _first_baseline(tmp_path, rows=None):
    """Rebaseline release 1.0.0 once (history: 15.4.0 scored by audit v1) and return its rows, history and artifact."""
    rows = rows if rows is not None else _rows(9, 291)
    rc, hist = _run(tmp_path, rows, _summary_v(rows, "1.0.0"), "1.0.0", {"releases": [V1_ENTRY]},
                    extra=("--cross-version", "rebaseline"))
    assert rc == 0
    return rows, hist, tmp_path / "art" / "outcome_audit_1.0.0.jsonl"


def test_the_entry_records_release_version_rate_n_and_the_artifact_hash(tmp_path):
    import hashlib
    rows, hist, jsonl = _first_baseline(tmp_path)
    e = json.loads(hist.read_text())["releases"][-1]
    assert (e["release"], e["audit_version"], e["used"], e["attempted"]) == ("1.0.0", 2, 9, 291)
    assert e["used_rate"] == pytest.approx(9 / 291)
    assert e["artifact_sha256"] == hashlib.sha256(jsonl.read_bytes()).hexdigest()


def test_a_rerun_of_the_baseline_release_needs_no_flag_and_changes_nothing(tmp_path, capsys):
    rows, hist, _ = _first_baseline(tmp_path)
    capsys.readouterr()
    before = hist.read_bytes()
    rc, _ = _run(tmp_path, rows, _summary_v(rows, "1.0.0"), "1.0.0")          # same artifact, default policy, no flag
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "MISMATCH" not in out and "is the recorded v2 baseline" in out
    assert hist.read_bytes() == before


def test_a_worse_rerun_fails_under_every_policy_and_never_replaces_the_baseline(tmp_path, capsys):
    """The review's reproduction: a 0/291 artifact with --cross-version rebaseline overwrote 3.093% and passed."""
    _, hist, _ = _first_baseline(tmp_path)
    before = hist.read_bytes()
    worse = _rows(0, 291)
    for extra in ((), ("--cross-version", "rebaseline"), ("--cross-version", "compare")):
        capsys.readouterr()
        rc, _ = _run(tmp_path, worse, _summary_v(worse, "1.0.0"), "1.0.0", extra=extra)
        out = capsys.readouterr().out
        assert rc == 1 and "dropped below" in out and "NOT replaced" in out, (extra, out)
        assert hist.read_bytes() == before


def test_a_better_rerun_passes_but_the_baseline_is_kept(tmp_path, capsys):
    _, hist, _ = _first_baseline(tmp_path)
    before = hist.read_bytes()
    better = _rows(12, 291)
    capsys.readouterr()
    rc, _ = _run(tmp_path, better, _summary_v(better, "1.0.0"), "1.0.0")
    out = capsys.readouterr().out
    assert rc == 0 and "kept, not replaced" in out, out
    assert hist.read_bytes() == before


def test_a_different_artifact_with_exactly_the_same_rate_passes_and_the_baseline_is_kept(tmp_path, capsys):
    """Only a LOWER rate fails: 9 of 291 again, from other rows, is not a drop."""
    _, hist, jsonl = _first_baseline(tmp_path)
    before = hist.read_bytes()
    same_rate = _rows(9, 291, signal="local_task_effect_kept")
    capsys.readouterr()
    rc, _ = _run(tmp_path, same_rate, _summary_v(same_rate, "1.0.0"), "1.0.0")
    out = capsys.readouterr().out
    assert rc == 0 and "kept, not replaced" in out and "dropped below" not in out, out
    assert jsonl.read_bytes() != b"" and hist.read_bytes() == before


def test_force_baseline_replaces_it_and_records_what_it_replaced(tmp_path, capsys):
    rows, hist, jsonl = _first_baseline(tmp_path)
    old = json.loads(hist.read_text())["releases"][-1]
    worse = _rows(0, 291)
    capsys.readouterr()
    rc, _ = _run(tmp_path, worse, _summary_v(worse, "1.0.0"), "1.0.0", extra=("--force-baseline", "window moved"))
    out = capsys.readouterr().out
    assert rc == 0 and "WARNING" in out and "window moved" in out, out
    e = json.loads(hist.read_text())["releases"][-1]
    assert (e["used"], e["attempted"]) == (0, 291)
    assert e["replaced"]["used"] == 9 and e["replaced"]["artifact_sha256"] == old["artifact_sha256"]
    assert e["replaced"]["reason"] == "window moved" and e["artifact_sha256"] != old["artifact_sha256"]


def test_a_baseline_recorded_before_hashes_existed_is_never_replaced_either(tmp_path, capsys):
    history = {"releases": [V1_ENTRY, {"release": "1.0.0", "audit_version": 2, "used_rate": 0.5, "used": 50, "attempted": 100}]}
    rows = _rows(5, 100)
    rc, hist = _run(tmp_path, rows, _summary_v(rows, "1.0.0"), "1.0.0", history)
    assert rc == 1 and "dropped below" in capsys.readouterr().out
    assert json.loads(hist.read_text())["releases"][-1]["used"] == 50


def test_a_later_release_keeps_the_older_rerun_rule_and_force_is_not_for_it(tmp_path, capsys):
    """Only the baseline is sticky: 1.0.1 is compared with 1.0.0 and a re-run replaces its own entry."""
    history = {"releases": [V1_ENTRY,
                            {"release": "1.0.0", "audit_version": 2, "used_rate": 0.03, "used": 3, "attempted": 100},
                            {"release": "1.0.1", "audit_version": 2, "used_rate": 0.05, "used": 5, "attempted": 100}]}
    rows = _rows(4, 100)                                        # below its own earlier 5%, above 1.0.0's 3%
    rc, hist = _run(tmp_path, rows, _summary_v(rows, "1.0.1"), "1.0.1", history, extra=("--force-baseline", "x"))
    out = capsys.readouterr().out
    assert rc == 0 and "is not the v2 baseline release; ignored" in out, out
    assert [e["used"] for e in json.loads(hist.read_text())["releases"]][-2:] == [3, 4]


@needs_bash
def test_through_the_release_script_a_rerun_needs_no_flag_and_a_worse_one_cannot_overwrite(tmp_path):
    rc, out, hist = _run_prv(tmp_path, {"releases": [V1_ENTRY]}, "--outcome-cross-version", "rebaseline")
    assert rc == 0, out
    recorded = hist.read_bytes()
    rc, out, _ = _run_prv(tmp_path, None)                         # an RC re-run: no flag, the same release
    assert rc == 0 and "MISMATCH" not in out and "is the recorded v2 baseline" in out, out
    rc, out, _ = _run_prv(tmp_path, None, "--outcome-cross-version", "rebaseline", rows=_rows(0, 100))
    assert rc == 1 and "dropped below" in out and "NOT replaced" in out, out
    assert hist.read_bytes() == recorded



# --- --tolerance-alpha: a drop must exceed sampling noise (default 0 = unchanged) ---

def _old_drop(k1, n1, k2, n2):
    return k1 / n1 < k2 / n2          # the point comparison the gate had before


def test_alpha_zero_decides_exactly_like_the_old_point_comparison():
    grid = [(k1, n1, k2, n2) for n1 in (50, 100, 873) for n2 in (50, 100, 873)
            for k1 in range(0, n1 + 1, max(1, n1 // 25)) for k2 in range(0, n2 + 1, max(1, n2 // 25))]
    assert len(grid) > 1000
    for k1, n1, k2, n2 in grid:
        assert G.dropped(k1, n1, k2, n2, k1 / n1, k2 / n2) == _old_drop(k1, n1, k2, n2)
        assert G.dropped(k1, n1, k2, n2, k1 / n1, k2 / n2, 0.0) == _old_drop(k1, n1, k2, n2)


def test_alpha_never_fails_something_the_point_comparison_passes():
    for k1 in range(0, 101, 5):
        for k2 in range(0, 101, 5):
            if not _old_drop(k1, 100, k2, 100):
                assert not G.dropped(k1, 100, k2, 100, k1 / 100, k2 / 100, 0.05)


def test_a_one_point_dip_passes_with_alpha_and_fails_without(tmp_path, capsys):
    history = {"releases": [{"release": "1.0.0", "used_rate": 0.50, "used": 50, "attempted": 100}]}
    rows = _rows(49, 100)
    rc, _ = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history)
    assert rc == 1, capsys.readouterr().out
    rc, _ = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history, extra=["--tolerance-alpha", "0.05"])
    assert rc == 0, capsys.readouterr().out


def test_a_big_drop_still_fails_with_alpha(tmp_path, capsys):
    history = {"releases": [{"release": "1.0.0", "used_rate": 0.50, "used": 50, "attempted": 100}]}
    rows = _rows(30, 100)
    rc, _ = _run(tmp_path, rows, _summary(rows, "1.1.0"), "1.1.0", history, extra=["--tolerance-alpha", "0.05"])
    assert rc == 1 and "dropped below" in capsys.readouterr().out


def test_alpha_out_of_range_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        G.main(["--tolerance-alpha", "1.5"])


SIM_SEED, SIM_TRIALS, ALPHA = 20261113, 20000, 0.05


def _sim(p_base, p_cur, n, alpha):
    import random
    rng = random.Random(SIM_SEED)
    fails = 0
    for _ in range(SIM_TRIALS):
        k2 = sum(rng.random() < p_base for _ in range(n))
        k1 = sum(rng.random() < p_cur for _ in range(n))
        fails += G.dropped(k1, n, k2, n, k1 / n, k2 / n, alpha)
    return fails / SIM_TRIALS


@pytest.mark.parametrize("p", [0.5, 0.8])
def test_simulated_false_fail_rate_drops_to_about_alpha(p):
    n = 100                                                 # the n of the gate's own tests
    old, new = _sim(p, p, n, 0.0), _sim(p, p, n, ALPHA)
    print(f"false-fail p={p} n={n} trials={SIM_TRIALS} seed={SIM_SEED}: alpha=0 {old:.4f}  alpha={ALPHA} {new:.4f}")
    assert old > 0.35                                       # the point comparison fails ~45% of equal releases
    assert new <= ALPHA + 0.02


@pytest.mark.parametrize("p,n,floor", [(0.5, 100, 0.35), (0.8, 100, 0.40), (0.5, 873, 0.95), (0.8, 873, 0.95)])
def test_simulated_power_against_a_ten_point_drop(p, n, floor):
    # At n=100 a 10-point drop is only ~41-49% detectable (both samples are noisy); at n=873 (15.4.0's n) ~99%.
    power = _sim(p, p - 0.10, n, ALPHA)
    print(f"power p={p}->{p - 0.10:.1f} n={n} trials={SIM_TRIALS} seed={SIM_SEED} alpha={ALPHA}: {power:.4f}")
    assert power > floor
