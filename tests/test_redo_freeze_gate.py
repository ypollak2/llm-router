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
    write_labels,
)

LABELS = "l" * 64
OTHER_LABELS = "o" * 64


def row(candidate="v3a", f1=0.8, sha=None, run="tune-t1", labels=LABELS):
    return dict(candidate=candidate, detector_sha256=score.detector_sha256() if sha is None else sha,
                tune_run=run, tune_manifest_sha256="m" * 64, labels_sha256=labels,
                overall={"unweighted": {"f1": f1}})


def legacy(item):
    del item["labels_sha256"]
    return item


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


MESSAGES = {
    "no_log": "Refused: non-baseline freeze needs a tune log and none exists; "
              "score the baseline and the candidate with redo-score --tune first.\n",
    "no_row": "Refused: the tune log has no row for this candidate; "
              "score it with redo-score --tune first.\n",
    "stale_sha": "Refused: none of the candidate's 1 tune row(s) was scored at the current detector "
                 "sha256 (redo_signal.py changed after scoring).\n",
    "labels_mismatch": "Refused: the selected tune row has no labels digest or tune-set identity "
                       "(written before labels binding), so its tune set cannot be matched.\n",
    "no_baseline": "Refused: the selected tune set has no v2 baseline row with a defined F1 (same tune "
                   "run and labels digest); score the baseline on the same labels first.\n",
    "not_argmax": "Refused: the candidate is not the tune F1 argmax: 1 of 1 competitor(s) in its tune set "
                  "score higher.\n",
    "not_argmax_2": "Refused: the candidate is not the tune F1 argmax: 1 of 2 competitor(s) in its tune "
                    "set score higher.\n",
    "tie": "Refused: the candidate ties 1 of 1 competitor(s) on tune F1 in its tune set; "
           "a tie fails closed.\n",
    "tie_2": "Refused: the candidate ties 1 of 2 competitor(s) on tune F1 in its tune set; "
             "a tie fails closed.\n",
    "undefined": "Refused: the candidate's tune F1 is undefined.\n",
}


@pytest.mark.parametrize("logged,cause", [
    (None, "no_log"),
    ([], "no_row"),
    ([row("v2", 0.1)], "no_row"),
    ([row("v2", 0.1), row(sha="stale")], "stale_sha"),
    ([row("v2", 0.1), legacy(row())], "labels_mismatch"),
    ([row()], "no_baseline"),
    ([row(), row("v2", 0.1, labels=OTHER_LABELS)], "no_baseline"),
    ([row(), row("v2", 0.1, run="tune-t2")], "no_baseline"),
    ([row(), row("v2", None)], "no_baseline"),
    ([row(), row("v2", 0.9)], "not_argmax"),
    ([row(), row("v3b", 0.9), row("v2", 0.1)], "not_argmax_2"),
    ([row(), row("v2", 0.8)], "tie"),
    ([row("v2", 0.1), row(), row(sha="stale")], "tie_2"),
    ([row("v2", 0.1), row(), row(f1=None)], "undefined"),
])
def test_freeze_refuses_without_current_strict_winner(pool, capsys, logged, cause):
    if logged is not None:
        write_log(pool, logged)
    assert freeze() == 2
    assert not (state(pool) / "frozen").exists()
    output = capsys.readouterr().out
    assert output == MESSAGES[cause]
    assert str(pool.parent) not in output


def test_refusal_messages_are_distinct_per_cause():
    assert len(set(MESSAGES.values())) == len(MESSAGES)


def test_competitor_rescored_lower_is_ranked_by_its_max(pool, capsys):
    # Latest-row ranking would compare v3b's 0.2 and accept; the max (0.9) refuses.
    write_log(pool, [row("v2", 0.1), row("v3b", 0.9), row("v3b", 0.2), row()])
    assert freeze() == 2
    assert capsys.readouterr().out == MESSAGES["not_argmax_2"]
    assert not (state(pool) / "frozen").exists()


def test_candidate_cannot_rescore_itself_higher(pool, capsys):
    # The candidate is ranked by its minimum over its own rows at the current sha in the set.
    write_log(pool, [row("v2", 0.5), row(f1=0.1), row(f1=0.9)])
    assert freeze() == 2
    assert capsys.readouterr().out == MESSAGES["not_argmax"]


def test_mixed_tune_runs_and_labels_are_not_compared(pool):
    write_log(pool, [row("v2", 0.1), row(), row("v3b", 0.95, run="tune-t2"), row("v2", 0.99, run="tune-t2"),
                     row("v3b", 0.99, labels=OTHER_LABELS), row("v2", 0.99, labels=OTHER_LABELS)])
    assert freeze() == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    assert (frozen["tune_run"], frozen["tune_labels_sha256"]) == ("tune-t1", LABELS)
    assert frozen["tune_manifest_sha256"] == "m" * 64


def test_exploit_rescoring_competitors_on_other_labels_cannot_crown_a_loser(tuned, tmp_path, capsys):
    """Review finding: re-score v2 and v3a on an all-not_redo labels file so their LATEST F1 is 0, then
    freeze v3b (the real-labels loser). Latest-row ranking accepted this (rc 0)."""
    env, run, sids = tuned
    for candidate in ("v2", "v3a", "v3b"):
        assert score_tune(env, candidate) == 0
    real = log_rows(env)
    assert real[0]["labels_sha256"] == score.labels_sha256([run / "labels_owner.jsonl"])
    assert all(item["tune_manifest_sha256"] == real[0]["tune_manifest_sha256"] for item in real)
    f1 = {item["candidate"]: item["overall"]["unweighted"]["f1"] for item in real}
    assert f1["v3b"] < f1["v2"] < f1["v3a"]
    write_labels(run, sids, {name: "not_redo" for name in sids}, "rigged.jsonl")
    rigged = run / "rigged.jsonl"
    for candidate in ("v2", "v3a"):
        assert score_tune(env, candidate, "t1", "--labels", str(rigged)) == 0
    assert all(item["overall"]["unweighted"]["f1"] == 0.0 for item in log_rows(env)[3:])
    assert log_rows(env)[3]["labels_sha256"] != real[0]["labels_sha256"]
    capsys.readouterr()
    assert freeze("v3b") == 2
    assert capsys.readouterr().out == (
        "Refused: the candidate is not the tune F1 argmax: 2 of 2 competitor(s) in its tune set "
        "score higher.\n")
    assert not (state(env) / "frozen").exists()
    # Rigging the candidate too only moves it into the rigged set, where everything ties at 0.
    assert score_tune(env, "v3b", "t1", "--labels", str(rigged)) == 0
    capsys.readouterr()
    assert freeze("v3b") == 2
    assert capsys.readouterr().out == (
        "Refused: the candidate ties 2 of 2 competitor(s) on tune F1 in its tune set; "
        "a tie fails closed.\n")
    # The real winner freezes once re-scored on the real labels (same pair: no widening spent).
    assert score_tune(env, "v3a") == 0
    assert freeze("v3a") == 0
    frozen = json.loads((state(env) / "frozen" / "frozen.json").read_text())
    assert frozen["tune_labels_sha256"] == real[0]["labels_sha256"]
    assert frozen["tune_run"] == "tune-t1"


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
    target = write_log(pool, [row("v2", 0.95, labels=OTHER_LABELS), row("v2", 0.5),
                              row(f1=0.1, labels=OTHER_LABELS), chosen, row("v3b", None)])
    content = target.read_bytes()
    assert freeze() == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    canonical = json.dumps(chosen, sort_keys=True, allow_nan=False).encode()
    assert frozen["tune_row_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert frozen["tune_log_sha256"] == hashlib.sha256(content).hexdigest()
    assert frozen["tune_log_bytes"] == len(content)
    assert frozen["detector_sha256"] == score.detector_sha256()
    assert frozen["tune_labels_sha256"] == LABELS and frozen["tune_run"] == "tune-t1"


def test_baseline_needs_no_log_and_records_null_provenance(pool):
    assert freeze("v2") == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    assert set(score.TUNE_PROVENANCE_KEYS) == {
        "tune_row_sha256", "tune_log_sha256", "tune_log_bytes", "tune_run", "tune_manifest_sha256",
        "tune_labels_sha256"}
    for key in score.TUNE_PROVENANCE_KEYS:
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


GENERIC = "Refused: frozen tune-log provenance is invalid or unavailable.\n"
SCORE_MESSAGES = {
    "deleted": GENERIC, "rewritten": GENERIC, "row": GENERIC, "length": GENERIC,
    "variant": "Refused: frozen tune-log provenance no longer passes the freeze gate: the tune log has no "
               "row for this candidate; score it with redo-score --tune first.\n",
    "labels": "Refused: frozen tune labels digest or tune set does not match the selected tune row.\n",
    "tune_run": "Refused: frozen tune labels digest or tune set does not match the selected tune row.\n",
    "to_baseline": "Refused: a baseline freeze carries tune provenance; frozen.json was edited.\n",
    "sha": (
        "Refused: the detector source (redo_signal.py) changed after this run was frozen "
        "(for example a run frozen before PR #706 or PR #708 edited redo_signal.py). "
        "The frozen test half stays unspent: re-freeze a new run with the current detector; "
        "do not edit the old run.\n"),
}


@pytest.mark.parametrize("fault", sorted(SCORE_MESSAGES))
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
            "to_baseline": ("detector_variant", "v2"),
            "labels": ("tune_labels_sha256", OTHER_LABELS),
            "tune_run": ("tune_run", "tune-t2"),
            "row": ("tune_row_sha256", "0" * 64),
            "sha": ("detector_sha256", "stale"),
            "length": ("tune_log_bytes", True),
        }[fault]
        frozen[key] = value
        frozen_path.write_text(json.dumps(frozen))
    capsys.readouterr()
    assert score.cmd_redo_score(["--run", "frozen", "--reps", "2"]) == 2
    assert capsys.readouterr().out == SCORE_MESSAGES[fault]
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
