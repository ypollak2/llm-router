"""PR3b anti-gaming guards; synthetic transcripts and isolated state only."""
# ruff: noqa: F811
import hashlib
import json
import shutil

import pytest

from llm_router import redo_sample as sample, redo_score as score, redo_signal
from tests.test_redo_tune import (
    env,  # noqa: F401 -- pytest fixture dependency
    log_rows,
    planted,
    run_sample,
    score_tune,
    sessions,
    state,
    tuned,  # noqa: F401 -- pytest fixture dependency
    write_labels,
)

LABELS = "l" * 64
OTHER_LABELS = "o" * 64
SCORED = "s" * 64
PROJECTS = "p" * 64


def row(candidate="v3a", f1=0.8, sha=None, run="tune-t1", labels=LABELS, scored=SCORED, n_lost=0):
    return dict(candidate=candidate, detector_sha256=score.detector_sha256() if sha is None else sha,
                tune_run=run, tune_manifest_sha256="m" * 64, labels_sha256=labels,
                scored_pairs_sha256=scored, projects_dir_sha256=PROJECTS, n_unclear=0, n_lost=n_lost,
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
    """The study's one live tune run ``tune-t1`` (its own sessions), then a test pool."""
    sessions(env, "tune", 2)
    assert run_sample("--tune", "--run", "t1") == 0
    sessions(env, "gate", 3)
    return env


MESSAGES = {
    "no_log": "Refused: non-baseline freeze needs a tune log and none exists; "
              "score the baseline and the candidate with redo-score --tune first.\n",
    "no_row": "Refused: the tune log has no row for this candidate; "
              "score it with redo-score --tune first.\n",
    "stale_sha": "Refused: none of the candidate's 1 tune row(s) was scored at the current detector "
                 "sha256 (redo_signal.py changed after scoring).\n",
    "labels_mismatch": "Refused: the candidate's tune row has no labels digest, scored-pair digest or "
                       "tune-set identity (written before this binding), so its tune set cannot be "
                       "matched.\n",
    "no_complete": "Refused: the live tune run has no complete tune scoring (every manifest pair labelled "
                   "and re-read, n_lost 0); complete its labels, then score the baseline and candidates.\n",
    "incomplete": "Refused: incomplete tune scoring: the candidate's 1 row(s) on the live tune run lost "
                  "pairs (largest n_lost 2); every manifest pair must be labelled and its transcript "
                  "re-read.\n",
    "not_in_set": "Refused: the candidate has no row in the canonical tune set (the live tune run's first "
                  "complete scoring: same labels digest, scored pairs and projects dir); score it there.\n",
    "incomplete_comparison": "Refused: incomplete comparison: 1 other candidate(s) with tune rows at the "
                             "current detector sha256 have no row in the canonical tune set; score each "
                             "there before freezing.\n",
    "no_baseline": "Refused: the canonical tune set has no v2 baseline row with a defined F1; score the "
                   "baseline there first.\n",
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
    ([row("v2", 0.1, n_lost=1), row(n_lost=2)], "no_complete"),
    ([row("v2", 0.1), row(n_lost=2)], "incomplete"),
    # Same run, labels, projects dir and counts, but other scored pairs: not the canonical set.
    ([row("v2", 0.1), row(scored="x" * 64)], "not_in_set"),
    ([row("v2", 0.1), row(run="tune-t0")], "not_in_set"),
    ([row("v2", 0.1), row(), row("v3b", 0.5, run="tune-t0")], "incomplete_comparison"),
    ([row("v2", 0.1), row(), row("v3b", 0.5, scored="x" * 64)], "incomplete_comparison"),
    ([row()], "no_baseline"),
    ([row(), row("v2", 0.1, labels=OTHER_LABELS)], "no_baseline"),
    ([row(), row("v2", 0.1, run="tune-t2")], "no_baseline"),
    ([row(), row("v2", None)], "no_baseline"),
    ([row(), row("v2", 0.9)], "not_argmax"),
    ([row(), row("v3b", 0.9), row("v2", 0.1)], "not_argmax_2"),
    ([row(), row("v2", 0.8)], "tie"),
    ([row("v2", 0.1), row(), row("v3b", 0.8, sha="stale")], "tie_2"),
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


def test_candidate_own_old_sha_row_is_not_a_competitor(pool):
    # Its own 0.9 at an older detector sha is not evidence against its current 0.5 (it still counts as
    # a widening at the cap).
    write_log(pool, [row("v2", 0.1), row(f1=0.9, sha="old"), row(f1=0.5)])
    assert freeze() == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    assert frozen["detector_variant"] == "v3a"


def test_freeze_needs_exactly_one_live_tune_run(pool, capsys):
    write_log(pool, [row("v2", 0.1), row()])
    shutil.copytree(state(pool) / "tune-t1", state(pool) / "tune-t0")    # a legacy second tune run
    capsys.readouterr()
    assert freeze() == 2
    assert capsys.readouterr().out == (
        "Refused: a non-baseline freeze needs exactly one live tune run; found 2 live and 0 voided.\n")
    for name in ("t0", "t1"):
        assert run_sample("--void-tune", "--run", name, "--reason", "synthetic test") == 0
    capsys.readouterr()
    assert freeze() == 2
    assert capsys.readouterr().out == (
        "Refused: a non-baseline freeze needs exactly one live tune run; found 0 live and 2 voided.\n")
    assert not (state(pool) / "frozen").exists()


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
    write_log(pool, [row("v2", 0.1), row(), row("v3b", 0.5), row("v3b", 0.95, run="tune-t2"),
                     row("v2", 0.99, run="tune-t2"), row("v3b", 0.99, labels=OTHER_LABELS),
                     row("v2", 0.99, labels=OTHER_LABELS), row("v2", 0.99, scored="x" * 64)])
    assert freeze() == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    assert (frozen["tune_run"], frozen["tune_labels_sha256"]) == ("tune-t1", LABELS)
    assert frozen["tune_manifest_sha256"] == "m" * 64


@pytest.fixture
def complete(env):
    """A tune run whose every manifest pair is labelled: complete scoring is possible (n_lost 0)."""
    sids, labels = planted(env)
    labels["plain_unlabelled"] = "not_redo"
    assert run_sample("--tune", "--run", "t1") == 0
    run = state(env) / "tune-t1"
    write_labels(run, sids, labels)
    return env, run, sids


def honest_scores(env):
    for candidate in ("v2", "v3a", "v3b"):
        assert score_tune(env, candidate) == 0
    rows = log_rows(env)
    assert all(item["n_lost"] == 0 and item["projects_dir_default"] for item in rows)
    assert len({score._tune_set(item) for item in rows}) == 1
    f1 = {item["candidate"]: item["overall"]["unweighted"]["f1"] for item in rows}
    assert f1["v3b"] < f1["v2"] < f1["v3a"]         # v3a is the honest winner, v3b a loser
    return rows


def test_positive_control_honest_single_run_freezes_the_true_argmax(complete, capsys):
    env, run, sids = complete
    rows = honest_scores(env)
    assert rows[0]["scored_pairs_sha256"] == score.scored_pairs_sha256(
        hashlib.sha256(f"{sids[name]}|1|{json.loads((run / 'frozen.json').read_text())['salt']}".encode())
        .hexdigest()[:16] for name in sids if name != "plain_unclear")
    assert freeze("v3a") == 0
    frozen = json.loads((state(env) / "frozen" / "frozen.json").read_text())
    assert (frozen["tune_run"], frozen["tune_labels_sha256"]) == ("tune-t1", rows[1]["labels_sha256"])
    assert frozen["tune_scored_pairs_sha256"] == rows[1]["scored_pairs_sha256"]
    assert frozen["tune_projects_dir_sha256"] == rows[1]["projects_dir_sha256"]
    assert frozen["voided_tune_runs"] == [] and frozen["n_voided_tune_runs"] == 0
    directory = state(env) / "frozen"
    (directory / "labels.jsonl").write_text("")
    assert score.cmd_redo_score(["--run", "frozen", "--dry-score", "--reps", "2"]) == 0
    for loser in ("v2", "v3b"):
        assert freeze(loser, run="frozen-" + loser) == (0 if loser == "v2" else 2)


def test_exploit_route_a_other_labels_file_is_refused_at_score_time(complete, capsys):
    """Route A: re-score v2 and the candidate on another labels file. The first complete scoring fixed
    the run's labels digest, so each such score refuses and logs nothing; v3b stays a loser."""
    env, run, sids = complete
    honest_scores(env)
    write_labels(run, sids, {name: "not_redo" for name in sids}, "rigged.jsonl")
    before = (state(env) / "tune_log.jsonl").read_bytes()
    capsys.readouterr()
    for candidate in ("v2", "v3b"):
        assert score_tune(env, candidate, "t1", "--labels", str(run / "rigged.jsonl")) == 2
        assert capsys.readouterr().out == (
            "Refused: labels changed for this tune run (its first complete scoring used other label "
            "files); void the run with redo-sample --void-tune and draw a new tune set.\n")
    assert (state(env) / "tune_log.jsonl").read_bytes() == before
    assert freeze("v3b") == 2
    assert capsys.readouterr().out == (
        "Refused: the candidate is not the tune F1 argmax: 2 of 2 competitor(s) in its tune set "
        "score higher.\n")
    assert not (state(env) / "frozen").exists()


def test_exploit_route_b_projects_dir_missing_transcripts_cannot_freeze(complete, tmp_path, capsys):
    """Route B: v3b scored from a copy of the projects dir that lacks 2 transcripts (one FP, one FN for
    every variant) reaches F1 0.8, above v3a's honest 0.75. Its row is logged with n_lost 2 and another
    scored-pair digest, so the freeze refuses as incomplete."""
    env, run, sids = complete
    for candidate in ("v2", "v3a"):
        assert score_tune(env, candidate) == 0
    copy = tmp_path / "curated"
    shutil.copytree(env, copy)
    for name in ("contr_not", "plain_redo"):
        (copy / "project" / f"{sids[name]}.jsonl").unlink()
    assert score.cmd_redo_score(["--tune", "--run", "t1", "--candidate", "v3b",
                                 "--projects-dir", str(copy)]) == 0
    honest, curated = log_rows(env)[1], log_rows(env)[2]
    assert curated["overall"]["unweighted"]["f1"] == 0.8 > honest["overall"]["unweighted"]["f1"] == 0.75
    assert (curated["n_scored"], curated["n_lost"]) == (5, 2)
    assert curated["scored_pairs_sha256"] != honest["scored_pairs_sha256"]
    assert curated["projects_dir_sha256"] != honest["projects_dir_sha256"]
    assert curated["projects_dir_default"] is False
    capsys.readouterr()
    assert freeze("v3b") == 2
    assert capsys.readouterr().out == MESSAGES["incomplete"]
    assert not (state(env) / "frozen").exists()


def test_exploit_route_d_second_tune_run_refused_then_void_needs_full_rescoring(complete, capsys):
    """Route D: draw a fresh tune run and score only v2 and the candidate there. A second --tune refuses
    while tune-t1 is live; after an explicit, recorded void the freeze still needs every current-sha
    candidate (v3a) scored on the new run."""
    env, run, sids = complete
    honest_scores(env)
    second = sessions(env, "second", 2)
    capsys.readouterr()
    assert run_sample("--tune", "--run", "t2") == 2
    assert capsys.readouterr().out == (
        "Refused: a live tune run already exists (tune-t1); one tune set per P3.6-c study: void it first "
        "with redo-sample --void-tune --run <id> --reason <text>.\n")
    assert not (state(env) / "tune-t2").exists()
    assert run_sample("--void-tune", "--run", "t1", "--reason", "synthetic: labels drift (D-0)") == 0
    assert run_sample("--tune", "--run", "t2") == 0
    t2 = state(env) / "tune-t2"
    frozen_t2 = json.loads((t2 / "frozen.json").read_text())
    assert frozen_t2["n_voided_tune_runs"] == 1
    assert [(v["run"], v["reason"]) for v in frozen_t2["voided_tune_runs"]] == [
        ("tune-t1", "synthetic: labels drift (D-0)")]
    salt = frozen_t2["salt"]
    (t2 / "labels_owner.jsonl").write_text("".join(
        json.dumps(dict(pair_id=hashlib.sha256(f"{sid}|{k}|{salt}".encode()).hexdigest()[:16],
                        label="redo" if k == 1 else "not_redo", source="owner")) + "\n"
        for sid in second for k in (1, 2)))
    assert score_tune(env, "v3b", "t1") == 2                # the voided run cannot be scored
    capsys.readouterr()
    for candidate in ("v2", "v3b"):
        assert score_tune(env, candidate, "t2") == 0
    capsys.readouterr()
    assert freeze("v3b") == 2
    assert capsys.readouterr().out == MESSAGES["incomplete_comparison"]
    assert not (state(env) / "frozen").exists()
    assert freeze("v2", run="baseline") == 0
    frozen = json.loads((state(env) / "baseline" / "frozen.json").read_text())
    assert frozen["n_voided_tune_runs"] == 1 and frozen["voided_tune_runs"][0]["run"] == "tune-t1"


def test_void_tune_validates_and_is_single_shot(complete, capsys):
    env, run, _ = complete
    assert run_sample("--void-tune", "--run", "t1") == 2
    assert "needs --reason" in capsys.readouterr().out
    assert run_sample("--void-tune", "--run", "t1", "--reason", "x" * 201) == 2
    assert run_sample("--void-tune", "--run", "t1", "--reason", "line\nbreak") == 2
    assert run_sample("--void-tune", "--run", "missing", "--reason", "ok") == 2
    assert not (run / sample.VOID_FILE).exists()
    assert run_sample("--void-tune", "--run", "t1", "--reason", "ok") == 0
    record = json.loads((run / sample.VOID_FILE).read_text())
    assert set(record) == {"run", "reason", "voided_utc"} and record["run"] == "tune-t1"
    capsys.readouterr()
    assert run_sample("--void-tune", "--run", "t1", "--reason", "again") == 2
    assert capsys.readouterr().out == "Refused: tune run is already voided.\n"
    with pytest.raises(SystemExit):
        run_sample("--tune", "--run", "t2", "--reason", "no void")


def test_tune_pool_from_non_default_projects_dir_needs_recorded_flag(env, tmp_path, capsys):
    sessions(env, "a", 2)
    other = tmp_path / "other"
    shutil.copytree(env, other)
    assert run_sample("--tune", "--run", "t1", "--projects-dir", str(other)) == 2
    assert capsys.readouterr().out == (
        "Refused: a tune pool is drawn from the census population (the default projects dir); a "
        "non-default --projects-dir needs --allow-nondefault-projects-dir, which is recorded.\n")
    assert not (state(env) / "tune-t1").exists()
    assert run_sample("--tune", "--run", "t1", "--projects-dir", str(other),
                      "--allow-nondefault-projects-dir") == 0
    frozen = json.loads((state(env) / "tune-t1" / "frozen.json").read_text())
    assert frozen["projects_dir_default"] is False and frozen["allow_nondefault_projects_dir"] is True
    assert frozen["projects_dir_transcripts"] == 2
    assert frozen["projects_dir_sha256"] == score.projects_dir_sha256(other)
    out = capsys.readouterr().out
    assert str(tmp_path) not in out and "run_dir" not in out


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
    target = write_log(pool, [row("v2", 0.5), row("v2", 0.95, labels=OTHER_LABELS),
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
    assert frozen["tune_scored_pairs_sha256"] == SCORED and frozen["tune_projects_dir_sha256"] == PROJECTS


def test_baseline_needs_no_log_and_records_null_provenance(pool):
    assert freeze("v2") == 0
    frozen = json.loads((state(pool) / "frozen" / "frozen.json").read_text())
    assert set(score.TUNE_PROVENANCE_KEYS) == {
        "tune_row_sha256", "tune_log_sha256", "tune_log_bytes", "tune_run", "tune_manifest_sha256",
        "tune_labels_sha256", "tune_scored_pairs_sha256", "tune_projects_dir_sha256"}
    for key in score.TUNE_PROVENANCE_KEYS:
        assert key in frozen and frozen[key] is None
    assert not (state(pool) / "tune_log.jsonl").exists()


def test_census_needs_no_tune_log(pool):
    assert run_sample("--census", "--detector", "v3a") == 0
    assert sorted(p.name for p in state(pool).iterdir()) == ["tune-t1"]     # the fixture's tune run only


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
    "scored": "Refused: frozen tune labels digest or tune set does not match the selected tune row.\n",
    "voided": "Refused: frozen tune-log provenance no longer passes the freeze gate: a non-baseline freeze "
              "needs exactly one live tune run; found 0 live and 1 voided.\n",
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
    elif fault == "voided":
        assert run_sample("--void-tune", "--run", "t1", "--reason", "after freeze") == 0
    else:
        key, value = {
            "variant": ("detector_variant", "v3b"),
            "to_baseline": ("detector_variant", "v2"),
            "labels": ("tune_labels_sha256", OTHER_LABELS),
            "tune_run": ("tune_run", "tune-t2"),
            "scored": ("tune_scored_pairs_sha256", "x" * 64),
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
