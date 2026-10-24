"""PR3b anti-gaming guards; synthetic transcripts and isolated state only."""
# ruff: noqa: F811
import hashlib
import json

import pytest

from llm_router import redo_sample as sample, redo_score as score, redo_signal
from tests.test_redo_tune import (
    env,  # noqa: F401 -- pytest fixture dependency
    log_rows,
    run_sample,
    score_tune,
    sessions,
    state,
    tuned,  # noqa: F401 -- pytest fixture dependency
)


def row(candidate="v3a", f1=0.8, sha=None):
    return dict(candidate=candidate, detector_sha256=score.detector_sha256() if sha is None else sha,
                overall={"unweighted": {"f1": f1}})


def write_log(env, rows):
    target = state(env) / "tune_log.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(item, allow_nan=False) + "\n" for item in rows))
    return target


def freeze(detector="v3a", run="frozen"):
    return run_sample("--freeze", "--run", run, "--detector", detector, "--allow-below-threshold")


@pytest.fixture
def pool(env):
    sessions(env, "gate", 3)
    return env


@pytest.mark.parametrize("logged", [
    None,
    [],
    [row(sha="stale")],
    [row(), row("v2", 0.9)],
    [row(), row("v3b", 0.9)],
    [row(), row("v2", 0.8)],
    [row(), row(f1=None)],
    [row(), row(sha="stale")],
])
def test_freeze_refuses_without_current_strict_winner(pool, capsys, logged):
    if logged is not None:
        write_log(pool, logged)
    assert freeze() == 2
    assert not (state(pool) / "frozen").exists()
    output = capsys.readouterr().out
    assert "Refused:" in output and "argmax-F1" in output
    assert str(pool.parent) not in output


@pytest.mark.parametrize("content", [
    b"not json\n", b"[]\n", b'{"candidate": "v3a"}\n',
    b'{"candidate":"v3a","overall":{"unweighted":{"f1":NaN}}}\n', b"\xff",
])
def test_invalid_log_fails_closed(pool, content):
    target = write_log(pool, [])
    target.write_bytes(content)
    assert freeze() == 2
    assert not (state(pool) / "frozen").exists()


def test_unreadable_log_fails_closed(pool):
    target = state(pool) / "tune_log.jsonl"
    target.mkdir(parents=True)
    assert freeze() == 2
    assert not (state(pool) / "frozen").exists()


def test_latest_rows_win_and_provenance_is_exact(pool):
    chosen = row()
    target = write_log(pool, [row("v2", 0.95), row("v2", 0.5), row(f1=0.1), chosen,
                              row("v3b", None)])
    content = target.read_bytes()
    assert freeze() == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    canonical = json.dumps(chosen, sort_keys=True, allow_nan=False).encode()
    assert frozen["tune_row_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert frozen["tune_log_sha256"] == hashlib.sha256(content).hexdigest()
    assert frozen["tune_log_bytes"] == len(content)
    assert frozen["detector_sha256"] == score.detector_sha256()


def test_baseline_needs_no_log_and_records_null_provenance(pool):
    assert freeze("v2") == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    for key in ("tune_row_sha256", "tune_log_sha256", "tune_log_bytes"):
        assert key in frozen and frozen[key] is None
    assert not (state(pool) / "tune_log.jsonl").exists()


def test_census_needs_no_tune_log(pool):
    assert run_sample("--census", "--detector", "v3a") == 0
    assert not state(pool).exists()


@pytest.fixture
def frozen_run(pool):
    target = write_log(pool, [row("v2", 0.4), row()])
    assert freeze() == 0
    directory = state(pool) / "frozen"
    manifest = [json.loads(line) for line in (directory / "manifest.jsonl").read_text().splitlines()]
    (directory / "labels.jsonl").write_text("".join(
        json.dumps(dict(pair_id=item["pair_id"], label="not_redo", source="owner")) + "\n"
        for item in manifest))
    return directory, target


@pytest.mark.parametrize("fault", ["deleted", "rewritten", "variant", "row", "sha", "length"])
def test_score_refuses_broken_provenance_without_spending(frozen_run, capsys, fault):
    directory, target = frozen_run
    frozen_path = directory / "frozen.json"
    frozen = json.loads(frozen_path.read_text())
    if fault == "deleted":
        target.unlink()
    elif fault == "rewritten":
        target.write_bytes(target.read_bytes().replace(b"0.4", b"0.3"))
    else:
        key, value = {
            "variant": ("detector_variant", "v3b"),
            "row": ("tune_row_sha256", "0" * 64),
            "sha": ("detector_sha256", "stale"),
            "length": ("tune_log_bytes", True),
        }[fault]
        frozen[key] = value
        frozen_path.write_text(json.dumps(frozen))
    capsys.readouterr()
    assert score.cmd_redo_score(["--run", "frozen", "--reps", "2"]) == 2
    output = capsys.readouterr().out
    if fault == "sha":
        assert output == (
            "Refused: the detector source (redo_signal.py) changed after this run was frozen "
            "(for example a run frozen before PR #706/this change added detector variants). "
            "The frozen test half stays unspent: re-freeze a new run with the current detector; "
            "do not edit the old run.\n")
    else:
        assert output == "Refused: frozen tune-log provenance is invalid or unavailable.\n"
    assert not (directory / "test.used").exists()
    assert not (directory / "result.json").exists()


@pytest.mark.parametrize("append", [False, True])
def test_score_accepts_intact_prefix_even_after_new_scores(frozen_run, append):
    directory, target = frozen_run
    if append:
        score._append_tune_log(target, row("v3b", 0.99))
    assert score.cmd_redo_score(["--run", "frozen", "--dry-score", "--reps", "2"]) == 0
    assert not (directory / "test.used").exists()


def test_baseline_score_needs_no_tune_log(pool):
    assert freeze("v2") == 0
    directory = state(pool) / "frozen"
    (directory / "labels.jsonl").write_text("")
    assert score.cmd_redo_score(["--run", "frozen", "--dry-score", "--reps", "2"]) == 0


def test_widening_cap_tracks_source_edits_and_allows_same_pair(tuned, monkeypatch, capsys):
    env, _, _ = tuned
    monkeypatch.setattr(score, "detector_sha256", lambda: "a" * 64)
    assert score_tune(env, "v2") == 0
    assert score_tune(env, "v3a") == 0
    assert score_tune(env, "v3a") == 0
    monkeypatch.setattr(score, "detector_sha256", lambda: "b" * 64)
    assert score_tune(env, "v3a") == 0
    assert score_tune(env, "v3a") == 0
    before = (state(env) / "tune_log.jsonl").read_bytes()
    monkeypatch.setattr(score, "detector_sha256", lambda: "c" * 64)
    capsys.readouterr()
    assert score_tune(env, "v3a") == 2
    assert capsys.readouterr().out == (
        "Refused: at most 2 widenings (2 distinct candidate+detector-sha pairs already scored).\n")
    assert (state(env) / "tune_log.jsonl").read_bytes() == before
    for item in log_rows(env):
        assert item["detector_sha256"] in ("a" * 64, "b" * 64)
        assert item["variant_params"] == {
            "variant": item["candidate"], "description": redo_signal.VARIANTS[item["candidate"]]}


def test_legacy_rows_consume_widenings(tuned, capsys):
    env, _, _ = tuned
    legacy = [row("v3a"), row("v3b")]
    for item in legacy:
        del item["detector_sha256"]
    target = write_log(env, legacy)
    before = target.read_bytes()
    assert score_tune(env, "v3a") == 2
    assert "2 distinct candidate+detector-sha pairs" in capsys.readouterr().out
    assert target.read_bytes() == before


def test_prior_run_refusal_names_runs_and_recovery_without_paths(pool, capsys):
    names = [f"broken-{letter}" for letter in "gfedcba"]
    for name in names:
        (state(pool) / name).mkdir(parents=True, exist_ok=True)
    with pytest.raises(sample.PriorRunUnreadable) as raised:
        sample.load_prior_runs({"tune"})
    assert raised.value.runs == sorted(names)
    assert raised.value.count == 7
    capsys.readouterr()
    assert freeze("v2") == 2
    output = capsys.readouterr().out
    assert "unreadable prior run directory(ies)" in output
    assert "broken-a, broken-b, broken-c, broken-d, broken-e and 2 more" in output
    assert "broken-f" not in output and "broken-g" not in output
    assert "a run dir under redo_validation/ is incomplete; move it aside or remove it" in output
    assert "/" not in output.replace("redo_validation/", "")
    assert not (state(pool) / "frozen").exists()
