"""P3.6-c PR3: redo detector tune mode. Synthetic transcripts only; no live state, network or timing."""
from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timezone

import pytest

from llm_router import redo_label, redo_sample as sample, redo_score as score, redo_signal, session_kind

NOW = 1_800_000_000.0
SENTINEL = "ZQXSENTINEL"


def record(text, ts, role="user"):
    return {"type": role, "timestamp": datetime.fromtimestamp(
        ts, timezone.utc).isoformat(), "message": {"content": text}}


def write(root, sid, prompts):
    project = root / "project"
    project.mkdir(exist_ok=True)
    entries = []
    for i, prompt in enumerate(prompts):
        entries.extend([record(prompt, NOW - 1000 + i),
                        record(f"{SENTINEL}answer{i}", NOW - 999 + i, "assistant")])
    (project / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in entries))


def words(n, tag="w"):
    return " ".join(f"{SENTINEL}{tag}{i}" for i in range(n))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    real = sample.collect

    def collect(**kwargs):
        kwargs.update(now=NOW, resolve=lambda sid: session_kind.Resolution("organic", "tag"),
                      served_loader=lambda sid: [])
        return real(**kwargs)

    monkeypatch.setattr(sample, "collect", collect)
    return root


def state(env):
    return env.parent / "state" / "redo_validation"


def sessions(env, prefix, n):
    """n sessions of three prompts: two eligible pairs each, no detector signal."""
    sids = [f"private-{prefix}-{i}" for i in range(n)]
    for sid in sids:
        write(env, sid, [f"{SENTINEL}one {sid}", f"{SENTINEL}two {sid}", f"{SENTINEL}three {sid}"])
    return sids


def run_sample(*args):
    return sample.cmd_redo_sample(["--allow-empty-exclusion", *args])


def pair_keys(run_dir, sids):
    """(sid, turn) pairs of ``sids`` present in a run's manifest, recomputed with the run's salt."""
    frozen = json.loads((run_dir / "frozen.json").read_text())
    ids = {json.loads(line)["pair_id"] for line in (run_dir / "manifest.jsonl").read_text().splitlines()}
    return {(sid, k) for sid in sids for k in range(1, 4)
            if hashlib.sha256(f"{sid}|{k}|{frozen['salt']}".encode()).hexdigest()[:16] in ids}


def no_text_anywhere(env, capsys_out=""):
    assert SENTINEL not in capsys_out and "private-" not in capsys_out
    for path in state(env).rglob("*"):
        if path.is_file():
            content = path.read_text()
            assert SENTINEL not in content and "private-" not in content


# ---- sampling and disjointness ----

def test_tune_run_shape_and_no_supply_gate(env, capsys):
    sessions(env, "a", 3)
    assert run_sample("--tune", "--run", "t1") == 0       # no flagged supply: no gate, no flag needed
    out = capsys.readouterr().out
    run = state(env) / "tune-t1"
    frozen = json.loads((run / "frozen.json").read_text())
    assert frozen["purpose"] == "tune" and frozen["seed"] == sample.DEFAULT_TUNE_SEED != sample.DEFAULT_SEED
    assert frozen["run_id"] == "tune-t1" and frozen["no_go"] is None
    assert frozen["detector_variant"] == "v2"
    assert frozen["manifest_sha256"] == hashlib.sha256((run / "manifest.jsonl").read_bytes()).hexdigest()
    assert len((run / "manifest.jsonl").read_text().splitlines()) == 6
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in run.iterdir())
    no_text_anywhere(env, out)
    assert run_sample("--tune", "--run", "tune-t1") == 2   # exists (prefix is not doubled)
    assert run_sample("--tune", "--run", "t2", "--seed", "7") == 0
    assert json.loads((state(env) / "tune-t2" / "frozen.json").read_text())["seed"] == 7


def test_tune_requires_exclusion_list_decision(env, capsys):
    sessions(env, "a", 1)
    assert sample.cmd_redo_sample(["--tune", "--run", "t1"]) == 5
    assert "Refused" in capsys.readouterr().out
    assert not state(env).exists()


def test_tune_honours_local_m09_exclusion_file(env, tmp_path):
    sids = sessions(env, "a", 2)
    key = sample.pair_key(sids[0], 1)
    path = tmp_path / "excl.txt"
    path.write_text(key + "\n")
    assert sample.cmd_redo_sample(["--tune", "--run", "t1", "--exclusion-file", str(path)]) == 0
    run = state(env) / "tune-t1"
    frozen = json.loads((run / "frozen.json").read_text())
    assert frozen["counts"]["excluded_m09"] == 1
    assert (sids[0], 1) not in pair_keys(run, sids)
    assert len(pair_keys(run, sids)) == 3


def test_tune_excludes_test_pairs_and_earlier_tune_pairs(env):
    first = sessions(env, "first", 3)
    assert run_sample("--freeze", "--run-id", "test1", "--allow-below-threshold") == 0
    second = sessions(env, "second", 3)
    assert run_sample("--tune", "--run", "t1") == 0
    third = sessions(env, "third", 3)
    assert run_sample("--tune", "--run", "t2") == 0
    test_dir, t1, t2 = state(env) / "test1", state(env) / "tune-t1", state(env) / "tune-t2"
    allsids = first + second + third
    in_test, in_t1, in_t2 = (pair_keys(d, allsids) for d in (test_dir, t1, t2))
    assert len(in_test) == 6 and len(in_t1) == 6 and len(in_t2) == 6
    assert in_test.isdisjoint(in_t1) and in_test.isdisjoint(in_t2) and in_t1.isdisjoint(in_t2)
    assert {sid for sid, _ in in_t1} == set(second) and {sid for sid, _ in in_t2} == set(third)
    f1, f2 = (json.loads((d / "frozen.json").read_text()) for d in (t1, t2))
    assert (f1["excluded_prior_test"], f1["excluded_prior_tune"], f1["n_prior_runs"]) == (6, 0, 1)
    assert (f2["excluded_prior_test"], f2["excluded_prior_tune"], f2["n_prior_runs"]) == (6, 6, 2)


def test_freeze_excludes_tune_pairs_and_records_them(env):
    first = sessions(env, "first", 3)
    assert run_sample("--tune", "--run", "t1") == 0
    second = sessions(env, "second", 3)
    assert run_sample("--freeze", "--run-id", "test1", "--allow-below-threshold",
                      "--detector", "v3a") == 0
    test_dir, tune_dir = state(env) / "test1", state(env) / "tune-t1"
    allsids = first + second
    in_test, in_tune = pair_keys(test_dir, allsids), pair_keys(tune_dir, allsids)
    assert len(in_tune) == 6 and len(in_test) == 6 and in_test.isdisjoint(in_tune)
    assert {sid for sid, _ in in_test} == set(second)
    frozen = json.loads((test_dir / "frozen.json").read_text())
    assert frozen["purpose"] == "test" and frozen["detector_variant"] == "v3a"
    assert frozen["detector_version"] == redo_signal.PATTERN_VERSION
    assert frozen["excluded_prior_tune"] == 6 and frozen["n_prior_runs"] == 1


def test_freeze_ignores_other_test_runs_but_census_matches_freeze(env, capsys):
    sessions(env, "first", 2)
    assert run_sample("--tune", "--run", "t1") == 0
    sessions(env, "second", 2)
    capsys.readouterr()
    assert sample.cmd_redo_sample(["--census", "--allow-empty-exclusion"]) == 0
    out = capsys.readouterr().out
    assert "excluded_prior_tune: 4" in out and "eligible_after_exclusion: 4" in out
    capsys.readouterr()
    no_text_anywhere(env, out)


def test_unreadable_prior_run_refuses_everywhere(env, capsys):
    sessions(env, "a", 1)
    bad = state(env) / "tune-broken"
    bad.mkdir(parents=True)
    (bad / "frozen.json").write_text("{not json")
    for args in (["--tune", "--run", "t1"], ["--freeze", "--run-id", "x", "--allow-below-threshold"]):
        assert run_sample(*args) == 2
        assert "unreadable prior run" in capsys.readouterr().out
    assert not (state(env) / "tune-t1").exists() and not (state(env) / "x").exists()


def test_detector_flag_rejected_with_tune_and_unknown_value(env):
    with pytest.raises(SystemExit):
        run_sample("--tune", "--run", "t1", "--detector", "v3a")
    with pytest.raises(SystemExit):
        run_sample("--freeze", "--detector", "v9")


# ---- candidate variants ----

def test_v2_is_the_default_and_unchanged():
    assert redo_signal.PATTERN_VERSION == "v2" and redo_signal.BASELINE_VARIANT == "v2"
    prev = words(9)
    for prompt in (prev, "claude: go", "that didn't work, try again", words(5), f"opus: {prev}"):
        assert redo_signal._signals(prompt, prev) == redo_signal._signals(prompt, prev, "v2")
    assert redo_signal._signals(prev, prev) == ["repeat"]       # 9 words, J = 1: v2 floor is 8
    entries = [record(words(9), NOW), record("x", NOW, "assistant"), record(words(9), NOW + 1)]
    assert redo_signal.redo_flags(entries) == [(1, "repeat")]
    assert redo_signal.redo_flags(entries, "v3b") == []
    with pytest.raises(ValueError):
        redo_signal._signals("x", None, "v9")


def test_v3a_tightens_repeat_only():
    twelve, eleven = words(12), words(11)
    assert redo_signal._signals(twelve, twelve, "v3a") == ["repeat"]
    assert redo_signal._signals(eleven, eleven, "v3a") == []            # below the 12-word floor
    assert redo_signal._signals(eleven, eleven, "v2") == ["repeat"]
    base = [f"{SENTINEL}x{i}" for i in range(60)]
    changed = base[:30] + ["different"] + base[31:]
    j = redo_signal.five_gram_jaccard(" ".join(base), " ".join(changed))
    assert 0.8 <= j < 0.9
    assert redo_signal._signals(" ".join(changed), " ".join(base), "v2") == ["repeat"]
    assert redo_signal._signals(" ".join(changed), " ".join(base), "v3a") == []
    for prompt in ("claude: go", "that didn't work", "native: again"):
        assert redo_signal._signals(prompt, None, "v3a") == redo_signal._signals(prompt, None, "v2")


def test_v3b_demotes_repeat_to_a_corroborating_signal():
    text = words(12)
    assert redo_signal._signals(text, text, "v2") == ["repeat"]
    assert redo_signal._signals(text, text, "v3b") == []
    both = "that didn't work " + text
    assert redo_signal._signals(both, both, "v3b") == ["contradiction", "repeat"]
    assert redo_signal._signals("claude: " + text, "claude: " + text, "v3b") == ["reask", "repeat"]
    assert redo_signal._signals("claude: go", None, "v3b") == ["reask"]


# ---- scoring: planted pairs ----

def planted(env):
    """One two-prompt session per planted pair; returns {name: sid} and the labels by name."""
    sets = {
        "reask_redo": (words(4, "p"), "claude: " + words(3, "q")),
        "contr_redo": (words(4, "r"), "that didn't work, " + words(3, "s")),
        "contr_not": (words(4, "t"), "that is wrong " + words(3, "u")),
        "repeat12_redo": (words(12, "v"), words(12, "v")),
        "repeat9_not": (words(9, "x"), words(9, "x")),
        "plain_redo": (words(4, "y"), words(3, "z")),
        "plain_unclear": (words(4, "m"), words(3, "n")),
        "plain_unlabelled": (words(4, "o"), words(3, "k")),
    }
    sids = {}
    for name, prompts in sets.items():
        sids[name] = f"private-{name}"
        write(env, sids[name], list(prompts))
    labels = dict(reask_redo="redo", contr_redo="redo", contr_not="not_redo", repeat12_redo="redo",
                  repeat9_not="not_redo", plain_redo="redo", plain_unclear="unclear")
    return sids, labels


def write_labels(run, sids, labels, name="labels_owner.jsonl", source="owner"):
    salt = json.loads((run / "frozen.json").read_text())["salt"]
    rows = [dict(pair_id=hashlib.sha256(f"{sids[n]}|1|{salt}".encode()).hexdigest()[:16],
                 label=lab, source=source) for n, lab in labels.items()]
    (run / name).write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def tuned(env):
    sids, labels = planted(env)
    assert run_sample("--tune", "--run", "t1") == 0
    run = state(env) / "tune-t1"
    write_labels(run, sids, labels)
    return env, run, sids


def score_tune(env, candidate, run="t1", *extra):
    return score.cmd_redo_score(["--tune", "--run", run, "--candidate", candidate,
                                 "--projects-dir", str(env), *extra])


def log_rows(env):
    return [json.loads(x) for x in (state(env) / "tune_log.jsonl").read_text().splitlines()]


def counts(block, key="unweighted"):
    return tuple(block[key][k] for k in ("tp", "fp", "fn"))


@pytest.mark.parametrize("candidate,overall,reask,contr,repeat", [
    ("v2", (3, 2, 1), (1, 0, 3), (1, 1, 3), (1, 1, 3)),
    ("v3a", (3, 1, 1), (1, 0, 3), (1, 1, 3), (1, 0, 3)),
    ("v3b", (2, 1, 2), (1, 0, 3), (1, 1, 3), (0, 0, 4)),
])
def test_per_signal_counts_on_planted_pairs(tuned, capsys, candidate, overall, reask, contr, repeat):
    env, run, _ = tuned
    assert score_tune(env, candidate) == 0
    out = capsys.readouterr().out
    row = log_rows(env)[-1]
    assert row["candidate"] == candidate and row["tune_run"] == "tune-t1"
    assert (row["n_scored"], row["n_unclear"], row["n_lost"]) == (6, 1, 1)
    assert counts(row["overall"]) == overall
    assert counts(row["per_signal"]["reask"]) == reask
    assert counts(row["per_signal"]["contradiction"]) == contr
    assert counts(row["per_signal"]["repeat"]) == repeat
    assert counts(row["overall"], "weighted") == tuple(float(x) for x in overall)   # all strata fully drawn
    tp, fp, fn = overall
    assert row["overall"]["unweighted"]["precision"] == tp / (tp + fp)
    assert row["overall"]["unweighted"]["recall"] == tp / (tp + fn)
    assert row["overall"]["unweighted"]["f1"] == 2 * tp / (2 * tp + fp + fn)
    assert f"candidate: {candidate}" in out and "overall: tp=" in out
    no_text_anywhere(env, out)
    assert "labels" not in out and not (run / "test.used").exists() and not (run / "result.json").exists()


def test_tune_score_is_repeatable_and_owner_overrides_judge(tuned):
    env, run, sids = tuned
    write_labels(run, sids, dict(contr_not="redo", repeat9_not="redo"), "labels_judge.jsonl", "judge")
    assert score_tune(env, "v2") == 0
    assert score_tune(env, "v2") == 0
    first, second = log_rows(env)
    assert first["overall"] == second["overall"] and counts(first["overall"]) == (3, 2, 1)
    assert not (run / "test.used").exists()
    assert sorted(p.name for p in run.iterdir()) == [
        "frozen.json", "labels_judge.jsonl", "labels_owner.jsonl", "manifest.jsonl"]


def test_labels_flag_replaces_default_label_files(tuned, tmp_path):
    env, run, sids = tuned
    other = tmp_path / "other.jsonl"
    only = dict(reask_redo="redo")
    write_labels(run, sids, only, "other_labels.jsonl")
    (tmp_path / "other.jsonl").write_text((run / "other_labels.jsonl").read_text())
    assert score_tune(env, "v2", "t1", "--labels", str(other)) == 0
    row = log_rows(env)[-1]
    assert (row["n_scored"], row["n_lost"]) == (1, 7)


def test_weighted_counts_use_stratum_weights():
    rows = [dict(pair_id="a", weight_N_h_combined=4, n_h_combined=2),     # weight 2
            dict(pair_id="b", weight_N_h_combined=10, n_h_combined=1),    # weight 10
            dict(pair_id="c", weight_N_h_combined=1, n_h_combined=1)]
    out = score.score_tune_candidate(
        rows, dict(a="redo", b="redo", c="not_redo"), dict(a=("repeat",), b=(), c=("repeat",)))
    assert counts(out["overall"]) == (1, 1, 1) and counts(out["overall"], "weighted") == (2.0, 1.0, 10.0)
    assert out["overall"]["weighted"]["recall"] == round(2 / 12, 4)
    empty = score.score_tune_candidate(rows, {}, {})
    assert (empty["n_scored"], empty["n_lost"]) == (0, 3) and empty["overall"]["unweighted"]["f1"] is None


# ---- guards ----

def test_two_widenings_then_third_distinct_candidate_refuses(tuned, monkeypatch, capsys):
    env, run, _ = tuned
    assert score_tune(env, "v2") == 0                    # the baseline is not a widening
    assert score_tune(env, "v3a") == 0
    assert score_tune(env, "v3b") == 0
    monkeypatch.setitem(redo_signal.VARIANTS, "v3c", "a third, hypothetical candidate")
    capsys.readouterr()
    before = (state(env) / "tune_log.jsonl").read_text()
    assert score.cmd_redo_score(["--tune", "--run", "t1", "--candidate", "v3c",
                                 "--projects-dir", str(env)]) == 2
    out = capsys.readouterr().out
    assert "at most 2 widenings" in out and "tp=" not in out
    assert (state(env) / "tune_log.jsonl").read_text() == before
    assert score_tune(env, "v3a") == 0 and score_tune(env, "v2") == 0   # known candidates may repeat
    assert [r["candidate"] for r in log_rows(env)] == ["v2", "v3a", "v3b", "v3a", "v2"]
    assert stat.S_IMODE((state(env) / "tune_log.jsonl").stat().st_mode) == 0o600
    no_text_anywhere(env)


def test_tune_scoring_refuses_a_test_run_and_writes_nothing(env, capsys):
    sessions(env, "a", 2)
    assert run_sample("--freeze", "--run-id", "test1", "--allow-below-threshold") == 0
    run = state(env) / "test1"
    before = sorted(p.name for p in run.iterdir())
    for run_id in ("test1", "tune-test1"):
        assert score.cmd_redo_score(["--tune", "--run", run_id, "--projects-dir", str(env)]) == 2
    assert "run purpose is not tune" in capsys.readouterr().out
    assert sorted(p.name for p in run.iterdir()) == before
    assert not (state(env) / "tune_log.jsonl").exists()
    # a legacy frozen.json without a purpose is a test run
    legacy = state(env) / "tune-legacy"
    legacy.mkdir()
    frozen = json.loads((run / "frozen.json").read_text())
    del frozen["purpose"]
    (legacy / "frozen.json").write_text(json.dumps(frozen))
    (legacy / "manifest.jsonl").write_bytes((run / "manifest.jsonl").read_bytes())
    assert score.cmd_redo_score(["--tune", "--run", "tune-legacy", "--projects-dir", str(env)]) == 2


def test_single_use_scoring_refuses_a_tune_run(tuned, capsys):
    env, run, _ = tuned
    assert score.cmd_redo_score(["--run", "tune-t1"]) == 2
    assert "tune run: use --tune" in capsys.readouterr().out
    assert not (run / "test.used").exists() and not (run / "result.json").exists()


def test_candidate_flags_need_tune(tuned):
    with pytest.raises(SystemExit):
        score.cmd_redo_score(["--run", "tune-t1", "--candidate", "v3a"])


def test_empty_label_set_is_not_a_scored_candidate(tuned, capsys):
    env, run, _ = tuned
    (run / "labels_owner.jsonl").write_text("")
    assert score_tune(env, "v3a") == 3
    assert "no scored pairs" in capsys.readouterr().out
    assert not (state(env) / "tune_log.jsonl").exists()


def test_invalid_and_missing_inputs_refuse(tuned, capsys):
    env, run, _ = tuned
    assert score.cmd_redo_score(["--tune", "--run", "nope", "--projects-dir", str(env)]) == 2
    (run / "labels_owner.jsonl").write_text(json.dumps(dict(pair_id="zz", label="redo", source="owner")) + "\n")
    assert score_tune(env, "v2") == 2
    (run / "labels_owner.jsonl").write_text("")
    (state(env) / "tune_log.jsonl").write_text("not json\n")
    assert score_tune(env, "v2") == 2
    assert "tune log unreadable" in capsys.readouterr().out


def test_missing_transcript_counts_as_lost_not_not_redo(tuned):
    env, run, sids = tuned
    (env / "project" / f"{sids['repeat12_redo']}.jsonl").unlink()
    assert score_tune(env, "v2") == 0
    row = log_rows(env)[-1]
    assert (row["n_scored"], row["n_lost"]) == (5, 2)
    assert counts(row["overall"]) == (2, 2, 1)       # the lost redo is neither a TP nor an FN


# ---- redo-label works on a tune dir unchanged ----

def test_redo_label_loads_and_resolves_a_tune_dir(tuned):
    env, run, sids = tuned
    run_dir, rows, frozen = redo_label.load_run("tune-t1")
    assert run_dir == run and frozen["purpose"] == "tune" and len(rows) == len(sids)
    views = redo_label.resolve_views(rows, frozen, env)
    assert set(views) == {r["pair_id"] for r in rows}
