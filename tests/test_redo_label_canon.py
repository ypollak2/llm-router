"""P3.6-c PR3d: canonical tune labels need the owner pass; freeze re-reads labels on disk. Synthetic only."""
# ruff: noqa: F811
from __future__ import annotations

import json

import pytest

from llm_router import redo_score as score, redo_signal as rs
from tests.test_redo_tune import (
    env,  # noqa: F401 -- pytest fixture dependency
    log_rows,
    planted,
    record_owner_done,
    run_sample,
    score_tune,
    sessions,
    state,
    write_labels,
)

CHANGED = ("Refused: labels on disk changed since canonical scoring; restore the original label files "
           "or void the run.\n")


@pytest.fixture
def study(env):
    """A live tune run whose planted pairs are all labelled by the judge; the owner has not labelled yet."""
    sids, owner_labels = planted(env)
    assert run_sample("--tune", "--run", "t1") == 0
    run = state(env) / "tune-t1"
    judge = dict(owner_labels, plain_unlabelled="not_redo", contr_not="redo")  # judge differs on one pair
    write_labels(run, sids, judge, "labels_judge.jsonl", "judge")
    sessions(env, "second", 3)                                              # a test pool for freeze
    return env, run, sids, owner_labels


def owner_pass(run, sids, labels):
    write_labels(run, sids, labels)       # also records owner_done.json


def freeze(detector="v3a", run="f1"):
    return run_sample("--freeze", "--run-id", run, "--allow-below-threshold", "--detector", detector)


def test_judge_first_scoring_is_non_canonical_and_does_not_lock(study, capsys):
    env, run, sids, owner = study
    assert score_tune(env, "v2") == 0
    assert "NON-CANONICAL" in capsys.readouterr().out
    [row] = log_rows(env)
    assert row["owner_done"] is False and row["n_lost"] == 0
    assert score.canonical_tune_set(log_rows(env), "tune-t1") is None
    owner_pass(run, sids, owner)          # the owner later writes labels that differ from the judge's
    assert score_tune(env, "v2") == 0     # no lock-out: the judge-only scoring fixed nothing
    capsys.readouterr()
    assert log_rows(env)[-1]["owner_done"] is True
    assert score.canonical_tune_set(log_rows(env), "tune-t1") is not None


def test_owner_file_without_marker_is_not_canonical(study, capsys):
    env, run, sids, owner = study
    write_labels(run, sids, owner)
    (run / score.OWNER_DONE_FILE).unlink()      # labels exist but the pass was never recorded complete
    assert score_tune(env, "v2") == 0
    assert log_rows(env)[-1]["owner_done"] is False


def test_stale_marker_after_further_owner_label_is_not_canonical(study):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    assert score.owner_done_valid(run)
    with (run / "labels_owner.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(pair_id="x" * 16, label="redo", source="owner")) + "\n")
    assert not score.owner_done_valid(run)


def test_honest_flow_freezes_the_argmax(study, capsys):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    for candidate in ("v2", "v3a", "v3b"):
        assert score_tune(env, candidate) == 0
    capsys.readouterr()
    assert freeze("v3a") == 0
    frozen = json.loads((state(env) / "f1" / "frozen.json").read_text())
    assert frozen["detector_variant"] == "v3a" and frozen["tune_labels_sha256"]


def test_freeze_refuses_when_owner_labels_changed_after_canonical_scoring(study, capsys):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    for candidate in ("v2", "v3a", "v3b"):
        assert score_tune(env, candidate) == 0
    capsys.readouterr()
    flipped = dict(owner, contr_redo="not_redo", reask_redo="not_redo")
    owner_pass(run, sids, flipped)         # owner relabels after the canonical scoring (marker re-recorded)
    assert freeze("v3a") == 2
    assert capsys.readouterr().out == CHANGED
    assert not (state(env) / "f1").exists()


def test_freeze_refuses_when_judge_labels_changed_after_canonical_scoring(study, capsys):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    for candidate in ("v2", "v3a"):
        assert score_tune(env, candidate) == 0
    capsys.readouterr()
    write_labels(run, sids, dict(owner, plain_unlabelled="redo"), "labels_judge.jsonl", "judge")
    assert freeze("v3a") == 2
    assert capsys.readouterr().out == CHANGED


def test_judge_only_scores_then_owner_flip_cannot_freeze(study, capsys):
    """Review of PR #708: score v2/v3a/v3b on judge-only labels, then owner labels flip the order; the
    freeze must not bind the judge-label winner."""
    env, run, sids, owner = study
    for candidate in ("v2", "v3a", "v3b"):
        assert score_tune(env, candidate) == 0
    capsys.readouterr()
    owner_pass(run, sids, owner)
    assert freeze("v3a") == 2
    out = capsys.readouterr().out
    assert out.startswith("Refused: the live tune run has complete scorings but none was made after")
    assert not (state(env) / "f1").exists()


def test_labels_subset_without_owner_file_cannot_be_canonical(study):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    assert score_tune(env, "v2", "t1", "--labels", str(run / "labels_judge.jsonl")) == 0
    assert log_rows(env)[-1]["owner_done"] is False


def test_owner_done_marker_holds_counts_and_a_digest_only(study):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    record = json.loads((run / score.OWNER_DONE_FILE).read_text())
    assert set(record) == {"n_labelled", "labels_owner_sha256", "others", "owner_sample_sha256"}
    assert record["n_labelled"] == len(owner) and record["others"] == score.OWNER_MIN_SPOT_CHECKS


def test_marker_needs_the_spot_check_minimum(study):
    """Design section 3 step 2: every F pair plus 20 S/R spot checks; fewer completes no owner pass."""
    env, run, sids, owner = study
    write_labels(run, sids, owner)
    (run / score.OWNER_DONE_FILE).unlink()
    assert record_owner_done(run, score.OWNER_MIN_SPOT_CHECKS - 1) is None
    assert not (run / score.OWNER_DONE_FILE).exists() and not score.owner_done_valid(run)
    assert record_owner_done(run, score.OWNER_MIN_SPOT_CHECKS) is not None
    assert score.owner_done_valid(run)


def test_marker_with_too_few_spot_checks_is_invalid(study):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    path = run / score.OWNER_DONE_FILE
    record = json.loads(path.read_text())
    path.write_text(json.dumps(dict(record, others=score.OWNER_MIN_SPOT_CHECKS - 1)) + "\n")
    assert not score.owner_done_valid(run)          # below the minimum, and no longer the recorded sample
    sample_path = run / score.OWNER_SAMPLE_FILE
    saved = json.loads(sample_path.read_text())
    sample_path.write_text(json.dumps(dict(saved, others=saved["others"][:-1])) + "\n")
    others, digest = score.owner_sample_state(run)
    path.write_text(json.dumps(dict(record, others=others, owner_sample_sha256=digest)) + "\n")
    assert not score.owner_done_valid(run)          # matches the shrunk sample, still below the minimum


def test_changed_owner_sample_invalidates_the_marker(study):
    env, run, sids, owner = study
    owner_pass(run, sids, owner)
    assert score.owner_done_valid(run)
    sample_path = run / score.OWNER_SAMPLE_FILE
    saved = json.loads(sample_path.read_text())
    sample_path.write_text(json.dumps(dict(saved, others=saved["others"] + ["extra0000000000"])) + "\n")
    assert not score.owner_done_valid(run)
    sample_path.unlink()
    assert not score.owner_done_valid(run)


def test_v3c_same_error_is_linear_and_bounded():
    huge = "still getting the same error " * 7200                  # about 200k chars
    assert len(huge) > 200_000
    assert rs._redo_phrase_v3c(huge.lower()) is False             # far too long to end in six words
    assert rs._signals(huge, "previous prompt", "v3d") is not None
    tail = ("filler " * 200 + "still getting the same error")
    assert rs._redo_phrase_v3c(tail) is False                      # starts after the first 200 chars
    assert rs._redo_phrase_v3c("still getting the same error after the fix") is True


def test_v3d_benign_important_needed():
    for text in ("not that important, skip it", "not needed", "not necessary here", "not urgent"):
        assert rs._short_negative_v3d(text) is False
    assert rs._short_negative_v3d("not that") is True
