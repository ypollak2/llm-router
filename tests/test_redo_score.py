"""Synthetic frozen draws only; no owner transcripts or live state."""
import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from llm_router import cli, paths, redo_score as scoring, redo_signal


def fixture(groups, populations=None, concentrated=False):
    rows, labels = [], []
    for h, values in groups.items():
        for value in values:
            i = len(rows)
            pair = f"private-pair-{i}"
            rows.append(dict(pair_id=pair, session_hash=f"private-session-{i}",
                             stratum=h, signals=["reask", "repeat"] if h == "F" else []))
            if value is not None:
                labels.append(dict(pair_id=pair, label=value, source="judge"))
    frozen = dict(run_id="X", detector_version=redo_signal.PATTERN_VERSION,
                  detector_sha256=hashlib.sha256(Path(redo_signal.__file__).read_bytes()).hexdigest(),
                  manifest_sha256="", window={"days": 30}, concentrated=concentrated,
                  strata={h: dict(N=(populations or {}).get(h, len(groups.get(h, []))),
                                  n=len(groups.get(h, []))) for h in ("F", "S", "R")})
    return rows, frozen, labels


def evaluate(groups, **kwargs):
    rows, frozen, labels = fixture(groups, **kwargs)
    return scoring.score(rows, frozen, labels, reps=300)


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    directory = paths.state_path("redo_validation", "X")
    directory.mkdir(parents=True)
    return directory


def write_run(run, groups=None, **kwargs):
    rows, frozen, labels = fixture(groups or {"F": ["redo"] * 16 + ["not_redo"] * 4}, **kwargs)
    manifest = "".join(json.dumps(row) + "\n" for row in rows)
    frozen["manifest_sha256"] = hashlib.sha256(manifest.encode()).hexdigest()
    (run / "manifest.jsonl").write_text(manifest)
    (run / "frozen.json").write_text(json.dumps(frozen))
    (run / "labels.jsonl").write_text("".join(json.dumps(row) + "\n" for row in labels))
    return rows, frozen, labels


def invoke(*extra):
    return scoring.cmd_redo_score(["--run", "X", "--reps", "100", *extra])


def snapshot(run):
    return {p.name: p.read_bytes() for p in run.iterdir()}


def test_precision_and_signals():
    result = evaluate({"F": ["redo"] * 2 + ["not_redo"] * 3})
    p = result["precision"]
    assert (p["tp"], p["fp"], p["n"]) == (2, 3, 5)
    assert p["point"] == 0.4
    assert (p["lo"], p["hi"]) == pytest.approx((0.118, 0.769), abs=0.001)
    for signal in ("reask", "repeat"):
        assert result["precision_by_signal"][signal]["tp"] == 2
        assert result["precision_by_signal"][signal]["fp"] == 3
        assert result["precision_by_signal"][signal]["n"] == 5
    assert result["precision_by_signal"]["contradiction"]["point"] is None


def test_weighted_recall():
    rows, frozen, labels = fixture(
        {"F": ["redo"] * 2 + ["not_redo"] * 2,
         "S": ["redo"] + ["not_redo"] * 3, "R": ["redo"] + ["not_redo"] * 9},
        populations={"F": 10, "S": 20, "R": 100})
    result = scoring.score(rows, frozen, labels, reps=1000)
    recall = result["recall"]
    assert recall["recall_point"] == 0.25
    assert recall["ci_lo"] <= 0.25 <= recall["ci_hi"]
    assert result == scoring.score(rows, frozen, labels, reps=1000)


def test_cluster_bootstrap_and_empty_stratum():
    rows, frozen, labels = fixture({"F": ["redo"] * 2, "S": ["not_redo"] * 2})
    for row in rows:
        row["session_hash"] = "one-private-session"
    recall = scoring.score(rows, frozen, labels, reps=20)["recall"]
    assert recall["ci_lo"] == recall["ci_hi"] == 1
    frozen["strata"]["R"]["N"] = 1
    recall = scoring.score(rows, frozen, labels, reps=20)["recall"]
    assert recall["recall_point"] is None
    assert recall["reason"] == "not estimable (empty stratum)"
    rows, frozen, labels = fixture({"F": ["redo"], "S": ["redo"], "R": ["redo"]})
    recall = scoring.score(rows, frozen, labels, reps=100)["recall"]
    assert recall["n_discarded"] > 50
    assert recall["ci_lo"] is recall["ci_hi"] is None


def test_concentrated(run, capsys):
    write_run(run, concentrated=True)
    assert invoke() == 0
    text = (run / "result.json").read_text()
    result = json.loads(text)
    assert result["recall"] == {
        "estimable": False, "reason": "not estimable (concentrated draw)"}
    assert "recall_point" not in text
    assert result["precision"]["tp"] == 16
    assert result["precision_basis"] == "capped per-session F draw"
    assert "recall: not estimable (concentrated draw)" in capsys.readouterr().out


@pytest.mark.parametrize("tp,fp,passed", [(15, 4, False), (16, 4, True),
                                        (14, 0, False), (15, 0, True)])
def test_gate_boundaries(tp, fp, passed):
    result = evaluate({"F": ["redo"] * tp + ["not_redo"] * fp})
    assert result["gate"]["passed"] is passed
    assert result["gate"]["verdict"] == ("pass" if passed else "fail")


def test_unclear_sensitivity():
    result = evaluate({"F": ["redo"] * 15 + ["not_redo"] * 3 + ["unclear"] * 2})
    assert result["gate"]["verdict"] == "inconclusive"
    assert result["gate"]["passed"] is False
    a = result["sensitivity"]["unclear_as_redo"]
    b = result["sensitivity"]["unclear_as_not_redo"]
    assert a["gate"]["passed"] and not b["gate"]["passed"]
    assert a["precision"]["tp"] == 17
    assert b["precision"]["fp"] == 5
    assert "recall" in a and a["counts"]["unclear"] == 0
    result = evaluate({"F": ["redo"] * 20 + ["unclear"]})
    assert result["gate"]["verdict"] == "pass"


@pytest.mark.parametrize("missing,verdict", [(1, "void"), (2, "void")])
def test_lost_boundary(missing, verdict):
    result = evaluate({"F": ["redo"] * (20 - missing) + [None] * missing})
    assert result["label_counts"]["lost"] == missing
    assert result["label_source_counts"]["none"] == missing
    assert result["lost_fraction"] == missing / 20
    assert result["gate"]["verdict"] == verdict


def test_reviewer_probe_and_lost_sensitivity():
    result = evaluate({"F": ["redo"] * 18 + [None] * 7,
                       "S": ["not_redo"] * 125})
    assert result["lost_fraction"] == 7 / 150
    assert result["precision"]["point"] == 1
    assert result["gate"]["verdict"] == "void"
    assert result["gate"]["passed"] is False
    assert result["gate"]["reason"] == "flagged-stratum label lost"
    sensitivity = result["sensitivity"]["lost_as_not_redo"]
    assert scoring._gate(result["precision"])["passed"]
    assert not sensitivity["gate"]["passed"]
    assert sensitivity["precision"]["fp"] == 7
    assert sensitivity["counts"]["lost"] == 0
    assert "recall" in sensitivity
    assert result["completeness_rule"] == scoring.COMPLETENESS_RULE


@pytest.mark.parametrize("missing,verdict", [(7, "pass"), (8, "void")])
def test_nonflagged_lost_boundary(missing, verdict):
    result = evaluate({"F": ["redo"] * 25,
                       "S": ["not_redo"] * (125 - missing) + [None] * missing})
    assert result["gate"]["verdict"] == verdict


def test_lost_sensitivity_gate_flip():
    rows, _, labels = fixture({"F": ["redo"] * 18 + [None] * 7})
    by_pair = {label["pair_id"]: label["label"] for label in labels}
    base = [(row, by_pair.get(row["pair_id"], "lost")) for row in rows]
    altered = [(row, "not_redo" if label == "lost" else label)
               for row, label in base]
    assert scoring._gate(scoring._precision(base))["passed"]
    assert not scoring._gate(scoring._precision(altered))["passed"]


@pytest.mark.parametrize("unclear,warn", [(1, False), (2, True)])
def test_unclear_validity(unclear, warn):
    result = evaluate({"F": ["redo"] * (10 - unclear) + ["unclear"] * unclear,
                       "S": [None]})
    for name in ("F", "overall"):
        assert result["validity"][name] == {
            "unclear": unclear, "labelled": 10, "rate": unclear / 10}
    assert len(result["warnings"]) == (2 if warn else 0)
    empty = evaluate({})
    assert empty["validity"]["F"]["rate"] is None
    assert empty["validity"]["overall"]["rate"] is None
    assert empty["warnings"] == []


def test_overall_unclear_warning_and_print(capsys):
    result = evaluate({"F": ["redo"] * 20, "S": ["unclear"] * 3})
    assert len(result["warnings"]) == 1
    assert "overall" in result["warnings"][0]
    scoring._print_result(result)
    assert result["warnings"][0] in capsys.readouterr().out


def test_precision_bootstrap_deterministic_and_clustered():
    rows, frozen, labels = fixture({"F": ["redo"] * 16 + ["not_redo"] * 4})
    for row in rows:
        row["session_hash"] = "one-session"
    result = scoring.score(rows, frozen, labels, seed=12, reps=40)
    bootstrap = result["precision"]["bootstrap"]
    assert bootstrap == dict(lo=0.8, hi=0.8, reps=40, seed=12, n_discarded=0)
    assert result == scoring.score(rows, frozen, labels, seed=12, reps=40)
    assert result["gate"] == scoring.score(
        rows, frozen, labels, seed=99, reps=1)["gate"]
    empty = scoring._precision_bootstrap([], seed=12, reps=40)
    assert empty == dict(lo=None, hi=None, reps=40, seed=12, n_discarded=40)


def test_dry_coverage_only(run, capsys):
    write_run(run, {"F": ["redo"] * 18 + ["unclear"] + [None]})
    before = snapshot(run)
    assert invoke("--dry-score") == 0
    output = capsys.readouterr().out.lower()
    for forbidden in ("precision", "recall", "wilson", "gate", "pass", "fail", "verdict"):
        assert forbidden not in output
    assert "f: labelled=19 unclear=1 lost=1" in output
    assert "manifest sha: match" in output
    assert "detector sha: match" in output
    assert "agreement n: 0" in output
    assert snapshot(run) == before


@pytest.mark.parametrize("dry", [False, True])
def test_spent_run_message(run, capsys, dry):
    write_run(run)
    (run / "test.used").write_text("spent")
    before = snapshot(run)
    assert invoke(*(["--dry-score"] if dry else [])) == 2
    assert capsys.readouterr().out == (
        "Refused: run is spent (test.used exists) and has no result.json; "
        "fail-safe, it cannot be re-scored.\n")
    assert snapshot(run) == before


def test_owner_override_and_agreement():
    rows, frozen, labels = fixture({"F": ["redo", "not_redo", "redo"]})
    labels += [dict(pair_id=rows[i]["pair_id"], label=value, source="owner")
               for i, value in enumerate(["redo", "redo", "not_redo"])]
    result = scoring.score(rows, frozen, labels, reps=20)
    assert result["precision"]["tp"] == 2
    assert result["label_source_counts"] == {"owner": 3, "judge": 0, "none": 0}
    a = result["owner_judge_agreement"]
    assert (a["n"], a["agree"]) == (3, 1)
    assert a["percent"] == pytest.approx(100 / 3)
    assert a["kappa"] == pytest.approx(-0.5)
    labels = [dict(pair_id=rows[0]["pair_id"], label="redo", source=s)
              for s in ("owner", "judge")]
    assert scoring.score(rows, frozen, labels, reps=20)["owner_judge_agreement"]["kappa"] is None


def test_single_use_privacy_modes_and_dry(run, capsys):
    rows, _, _ = write_run(run)
    before = snapshot(run)
    assert invoke("--dry-score") == 0
    assert snapshot(run) == before
    assert invoke() == 0
    after = snapshot(run)
    assert invoke() == 2
    assert snapshot(run) == after
    capsys.readouterr()
    assert invoke("--dry-score") == 2
    assert snapshot(run) == after
    output = capsys.readouterr().out
    assert output == "Refused: run already used.\n"
    for row in rows:
        assert row["pair_id"] not in output
        assert row["session_hash"] not in output
    assert "reask" not in output and "repeat" not in output
    for name in ("test.used", "result.json"):
        assert stat.S_IMODE((run / name).stat().st_mode) == 0o600
    assert "NaN" not in (run / "result.json").read_text()


@pytest.mark.parametrize("fault", ["detector", "manifest", "unknown", "label", "source",
                                   "conflict", "missing", "unreadable"])
def test_refusals_write_nothing(run, fault, capsys):
    rows, frozen, labels = write_run(run)
    if fault == "detector":
        frozen["detector_sha256"] = "wrong"
        (run / "frozen.json").write_text(json.dumps(frozen))
    elif fault == "manifest":
        with (run / "manifest.jsonl").open("a") as stream:
            stream.write("\n")
    elif fault in ("missing", "unreadable"):
        (run / "labels.jsonl").unlink()
        if fault == "unreadable":
            (run / "labels.jsonl").mkdir()
    else:
        if fault == "unknown":
            labels[0]["pair_id"] = "unknown-private-pair"
        elif fault == "label":
            labels[0]["label"] = "bad-private-label"
        elif fault == "source":
            labels[0]["source"] = "bad-private-source"
        else:
            labels.append(dict(pair_id=rows[0]["pair_id"], label="lost", source="judge"))
        (run / "labels.jsonl").write_text("".join(json.dumps(row) + "\n" for row in labels))
    before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    for extra in ([], ["--dry-score"]):
        assert invoke(*extra) == 2
        assert not (run / "test.used").exists()
        assert not (run / "result.json").exists()
    assert before == {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 2
    assert "private" not in output and "wrong" not in output


def test_repeated_labels_paths(run):
    rows, _, labels = write_run(run)
    other = run / "owner.jsonl"
    other.write_text(json.dumps(dict(pair_id=rows[0]["pair_id"], label="not_redo",
                                     source="owner")) + "\n")
    assert invoke("--labels", str(run / "labels.jsonl"), "--labels", str(other)) == 0
    result = json.loads((run / "result.json").read_text())
    assert result["precision"]["tp"] == 15
    assert result["label_source_counts"]["owner"] == 1


def test_cli_dispatch_and_inert_help(run, monkeypatch, capsys):
    assert "redo-score" in cli._KNOWN_SUBCOMMANDS
    assert "redo-score" in cli._OWN_HELP_ANYWHERE
    for flag in ("--help", "-h"):
        monkeypatch.setattr(sys, "argv", ["llm-router", "redo-score", flag])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert "usage: llm-router redo-score" in capsys.readouterr().out
        assert list(run.iterdir()) == []
    seen = []
    monkeypatch.setattr(scoring, "cmd_redo_score", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["llm-router", "redo-score", "--run", "X"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen == [["--run", "X"]]
