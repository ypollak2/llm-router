"""Synthetic transcripts, injected judges, and real terminal interactions."""
import argparse
import errno
import hashlib
import json
import os
import pty
import random
import re
import select
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import cli, paths, redo_label as label
from llm_router.file_lock import exclusive_lock

MODEL = "test-model-1"
NOW = 1_800_000_000.0
MARKER = b"[q] quit  > "
SCHEMA = {"pair_id", "label", "source"}
SCRIPT = ("import sys; from llm_router.redo_label import cmd_redo_label; "
          "sys.exit(cmd_redo_label(sys.argv[1:]))")


def record(text, ts=NOW - 100, role="user"):
    return {"type": role, "timestamp": datetime.fromtimestamp(
        ts, timezone.utc).isoformat(), "message": {"content": text}}


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()] if path.exists() else []


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.fixture
def clean_judge_environment(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "ANTHROPIC_CUSTOM_HEADERS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clean_judge_environment(clean_judge_environment):
    pass


@pytest.fixture(autouse=True)
def _no_quota(monkeypatch):
    monkeypatch.setattr(label.shadow_frontier, "quota_skip", lambda *a, **k: None)


@pytest.fixture
def make_run(tmp_path, monkeypatch):
    def build(n=48, missing=(), text=None):
        home, projects = tmp_path / "home", tmp_path / "projects"
        home.mkdir(exist_ok=True)
        (projects / "proj").mkdir(parents=True, exist_ok=True)
        for key, value in (("HOME", home), ("LLM_ROUTER_HOME", tmp_path / "state"),
                           ("CLAUDE_PROJECTS_DIR", projects)):
            monkeypatch.setenv(key, str(value))
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
            str(Path(p or os.getcwd()).resolve()) for p in sys.path))
        salt, rows, prompts, lost = "synthetic-salt", [], {}, set()
        for i in range(n + len(missing)):
            sid, turn = f"session-{i}", 2
            kind = missing[i - n] if i >= n else None
            if kind == "turn":
                turn = 99
            pair_id = digest(f"{sid}|{turn}|{salt}")
            rows.append(dict(pair_id=pair_id, session_hash=digest(f"{salt}|{sid}"),
                             turn_index=turn, stratum="S" if kind else "FSR"[i % 3],
                             signal=[], session_kind="organic"))
            messages = []
            for k in range(4):
                prompt = (text if text is not None else
                          f"ZQXPROMPT{i}T{k}")
                answer = text if text is not None else f"ZQXANSWER{i}T{k}"
                messages.extend((record(prompt, NOW + k * 2),
                                 record(answer, NOW + k * 2 + 1, "assistant")))
            if kind != "session":
                write_jsonl(projects / "proj" / f"{sid}.jsonl", messages)
            if kind:
                lost.add(pair_id)
            else:
                prompts[pair_id] = text if text is not None else f"ZQXPROMPT{i}T2"
        run_dir = paths.state_path("redo_validation", "run1")
        run_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(run_dir / "manifest.jsonl", rows)
        (run_dir / "frozen.json").write_text(json.dumps(
            dict(salt=salt, seed=360, strata=["F", "S", "R"])))
        return argparse.Namespace(run_dir=run_dir, rows=rows, projects=projects,
                                  prompts=prompts, lost=lost,
                                  state=tmp_path / "state", env=os.environ.copy())
    return build


def options(run, **changes):
    run_dir, manifest, frozen = label.load_run("run1")
    values = dict(run="run1", run_dir=run_dir, manifest=manifest, frozen=frozen,
                  projects_dir=run.projects, seed=360, max_calls=10,
                  max_usd=100, model_id=MODEL, others=99,
                  allow_api_key=False, force_quota=False, force_streak=False,
                  force_unresolved=False,
                  judge=True, owner=False)
    values.update(changes)
    return argparse.Namespace(**values)


def owner_args(others=99):
    return ["--run", "run1", "--owner", "--others", str(others)]


class FakeClaude:
    def __init__(self, *, miss_calibration=False, model_ok=True,
                 cost=0.01, garbage=False):
        self.cal = label.load_calibration()
        self.miss_calibration, self.model_ok = miss_calibration, model_ok
        self.cost, self.garbage, self.calls = cost, garbage, []

    def __call__(self, brief, item, model_id):
        answers, real, calibration = {}, [], []
        for ident, body in re.findall(
                r"(?ms)^ITEM (i\d+)\n(.*?)(?=\n\nITEM i\d+\n|\Z)", item):
            prompt = body.split("Developer message: ", 1)[1]
            match = next((c for c in self.cal if c["prompt"] == prompt), None)
            answer = match["expected"] if match else "not_redo"
            if match:
                calibration.append(match["cal_id"])
                if self.miss_calibration and match == self.cal[0]:
                    answer = "not_redo" if answer == "redo" else "redo"
            else:
                real.append(prompt)
            answers[ident] = answer
        self.calls.append(dict(brief=brief, item=item, model=model_id,
                               real=real, calibration=calibration))
        return dict(result="garbage" if self.garbage else
                    json.dumps({"labels": answers}),
                    cost_usd=self.cost, model_ok=self.model_ok)


def judge_rows(run):
    return jsonl(run.run_dir / "labels_judge.jsonl")


def owner_rows(run):
    return jsonl(run.run_dir / "labels_owner.jsonl")


def run_tui(args, keys_by_prompt, env):
    master, slave = pty.openpty()
    proc, output = None, bytearray()
    keys, sent, consumed = list(keys_by_prompt), 0, 0
    try:
        proc = subprocess.Popen([sys.executable, "-c", SCRIPT, *args],
                                stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 20
        while True:
            position = output.find(MARKER, consumed)
            if position >= 0 and sent < len(keys):
                os.write(master, keys[sent].encode())
                sent += 1
                consumed = position + len(MARKER)
                deadline = time.monotonic() + 20
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(f"TUI did not complete its next step: {bytes(output)!r}")
            ready, _, _ = select.select([master], [], [], min(remaining, 0.2))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            elif proc.poll() is not None:
                break
        return proc.wait(timeout=20), bytes(output)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)
        os.close(master)
        if slave is not None:
            os.close(slave)


def test_judge_labels_all_pairs_and_reports_only_counts(make_run, capsys):
    run = make_run(missing=("session", "turn"))
    fake = FakeClaude()
    assert label.run_judge(options(run), call=fake) == 0
    rows, output = judge_rows(run), capsys.readouterr().out
    assert len(rows) == len(run.rows)
    assert all(set(row) == SCHEMA and row["source"] == "judge" for row in rows)
    assert {r["pair_id"] for r in rows if r["label"] == "lost"} == run.lost
    assert all(r["label"] == "not_redo" for r in rows if r["pair_id"] not in run.lost)
    assert len(fake.calls) == 3
    assert sum(len(c["real"]) for c in fake.calls) == 48
    for call in fake.calls:
        assert len(call["calibration"]) == 6
        assert 0 < len(call["real"]) <= 20
        assert label.LABEL_DEFINITION in call["brief"]
    assert "remaining: 0" in output and "labelled: 50" in output
    assert "ZQX" not in output
    assert not any(row["pair_id"] in output for row in run.rows)


@pytest.mark.parametrize("kwargs,message", [
    ({"miss_calibration": True}, "calibration miss"),
    ({"model_ok": False}, "model mismatch"),
    ({"garbage": True}, "unparseable reply"),
])
def test_rejected_judge_batch_writes_no_labels(make_run, capsys, kwargs, message):
    run, fake = make_run(), FakeClaude(**kwargs)
    assert label.run_judge(options(run), call=fake) == 4
    assert len(fake.calls) == 1
    assert judge_rows(run) == []
    assert message in capsys.readouterr().out


def test_call_cap_and_resume_process_each_real_item_once(make_run, capsys):
    run, first = make_run(), FakeClaude()
    assert label.run_judge(options(run, max_calls=1), call=first) == 4
    before = judge_rows(run)
    assert len(first.calls) == 1 and len(before) == 20
    assert "remaining: 28" in capsys.readouterr().out
    second = FakeClaude()
    assert label.run_judge(options(run, max_calls=3), call=second) == 0
    seen = [p for fake in (first, second) for c in fake.calls for p in c["real"]]
    assert len(seen) == len(set(seen)) == len(run.prompts)
    assert set(seen) == set(run.prompts.values())
    rows = judge_rows(run)
    assert rows[:20] == before
    assert len(rows) == len({r["pair_id"] for r in rows}) == 48


def test_spend_cap_keeps_valid_batch(make_run, capsys):
    run, fake = make_run(), FakeClaude(cost=5)
    assert label.run_judge(options(run, max_usd=3), call=fake) == 4
    assert len(fake.calls) == 1 and len(judge_rows(run)) == 20
    assert "spend cap" in capsys.readouterr().out
    second = FakeClaude(cost=5)
    assert label.run_judge(options(run, max_usd=3), call=second) == 4
    assert not second.calls
    output = capsys.readouterr().out
    assert "spend cap" in output and "usd: 0.0000" in output


def test_call_cap_is_cumulative(make_run, capsys):
    run, first = make_run(), FakeClaude()
    assert label.run_judge(options(run, max_calls=2), call=first) == 4
    assert len(first.calls) == 2 and len(judge_rows(run)) == 40
    capsys.readouterr()
    second = FakeClaude()
    assert label.run_judge(options(run, max_calls=2), call=second) == 4
    assert not second.calls and len(judge_rows(run)) == 40
    output = capsys.readouterr().out
    assert "stopped: call cap" in output and "calls: 0" in output


def test_ledger_private_counts_only(make_run):
    run = make_run(n=1)
    assert label.run_judge(options(run), call=FakeClaude(cost=0.5)) == 0
    path = run.run_dir / "judge_spend.json"
    assert json.loads(path.read_text()) == {
        "calls": 1, "usd": 0.5, "rejected_streak": 0, "estimated_calls": 0}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(run.run_dir.glob(".judge-spend-*"))


def test_missing_ledger_is_zero(tmp_path):
    assert label.read_ledger(tmp_path) == {"calls": 0, "usd": 0.0, "rejected_streak": 0,
                                           "estimated_calls": 0}


BAD_LEDGERS = ["{", "[]", '{"calls": -1}', '{"calls": "1", "usd": 0, "rejected_streak": 0}',
               '{"calls": 0, "usd": -1, "rejected_streak": 0}',
               '{"calls": 0, "usd": NaN, "rejected_streak": 0}',
               '{"calls": 0, "usd": 0, "rejected_streak": -2}',
               '{"calls": 0, "usd": true, "rejected_streak": 0}']


@pytest.mark.parametrize("payload", BAD_LEDGERS)
def test_corrupt_ledger_refuses_without_writing(make_run, capsys, payload):
    run = make_run(n=1)
    path = run.run_dir / "judge_spend.json"
    path.write_text(payload)
    fake = FakeClaude()
    assert label.run_judge(options(run), call=fake) == 2
    assert not fake.calls and path.read_text() == payload
    assert not (run.run_dir / "judge_meta.json").exists()
    assert not (run.run_dir / "labels_judge.jsonl").exists()
    out = capsys.readouterr().out
    assert "Refused: invalid judge_spend.json (" in out and payload not in out


def test_recursive_ledger_refuses_without_writing(make_run, capsys):
    run = make_run(n=1)
    path = run.run_dir / "judge_spend.json"
    path.write_text("[" * 100000 + "]" * 100000)
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    fake = FakeClaude()
    assert label.run_judge(options(run), call=fake) == 2
    assert capsys.readouterr().out.strip() == (
        "Refused: invalid judge_spend.json (RecursionError).")
    assert not fake.calls
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before


def test_cmd_refuses_held_lock(make_run, capsys):
    run = make_run(n=1)
    lock_path = run.run_dir / ".lock"
    os.close(os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600))
    status = {}
    with exclusive_lock(lock_path, timeout=0, status=status) as acquired:
        assert acquired and status["state"] == "acquired"
        before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
        assert label.cmd_redo_label([
            "--run", "run1", "--judge", "--model-id", MODEL]) == 2
        assert capsys.readouterr().out.strip() == (
            "Refused: another redo-label process holds this run")
        assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before
    for name in ("judge_meta.json", "labels_judge.jsonl", "judge_spend.json"):
        assert not (run.run_dir / name).exists()


def test_cmd_refuses_when_lock_file_cannot_be_created(make_run, monkeypatch, capsys):
    run = make_run(n=1)
    real_open = os.open

    def deny(path, *args, **kwargs):
        if Path(path).name == ".lock":
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(label.os, "open", deny)
    assert label.cmd_redo_label(["--run", "run1", "--judge", "--model-id", MODEL]) == 2
    assert capsys.readouterr().out.strip() == "Refused: cannot lock this run directory (PermissionError)"
    for name in ("judge_meta.json", "labels_judge.jsonl", "judge_spend.json"):
        assert not (run.run_dir / name).exists()


def test_cmd_refuses_when_locking_unavailable(make_run, monkeypatch, capsys):
    run = make_run(n=1)  # run dir must exist for the lock step

    def unavailable(path, timeout=0.0, status=None):
        import contextlib

        @contextlib.contextmanager
        def ctx():
            status["state"] = "unavailable"
            yield False
        return ctx()

    monkeypatch.setattr(label, "exclusive_lock", unavailable)
    assert label.cmd_redo_label(["--run", "run1", "--judge", "--model-id", MODEL]) == 2
    assert capsys.readouterr().out.strip() == "Refused: cannot lock this run directory (unavailable)"
    assert not (run.run_dir / "judge_meta.json").exists()


@pytest.mark.parametrize("raises", [False, True])
def test_cmd_releases_lock(make_run, monkeypatch, raises):
    run = make_run(n=1)

    def judge(opts):
        status = {}
        with exclusive_lock(opts.run_dir / ".lock", timeout=0, status=status) as acquired:
            assert not acquired and status["state"] == "held"
        if raises:
            raise RuntimeError("synthetic failure")
        return 0

    monkeypatch.setattr(label, "run_judge", judge)
    args = ["--run", "run1", "--judge", "--model-id", MODEL]
    if raises:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            label.cmd_redo_label(args)
    else:
        assert label.cmd_redo_label(args) == 0
    lock_path = run.run_dir / ".lock"
    assert lock_path.read_bytes() == b""
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    status = {}
    with exclusive_lock(lock_path, timeout=0, status=status) as acquired:
        assert acquired and status["state"] == "acquired"


@pytest.mark.parametrize("payload,stop", [
    (json.dumps({"calls": 2, "usd": 0.01, "rejected_streak": 0,
                 "estimated_calls": 0}), "stopped: call cap"),
    ("{", "stopped: invalid judge_spend.json (JSONDecodeError)"),
])
def test_judge_rereads_ledger_between_batches(make_run, monkeypatch, capsys,
                                             payload, stop):
    run = make_run(n=label.BATCH_SIZE + 1)
    fake = FakeClaude()
    original = label.append_label
    appended = 0

    def append_with_external_write(*args):
        nonlocal appended
        original(*args)
        appended += 1
        if appended == label.BATCH_SIZE:
            # Simulate an external update after the first batch's ledger writes.
            (run.run_dir / "judge_spend.json").write_text(payload)

    monkeypatch.setattr(label, "append_label", append_with_external_write)
    assert label.run_judge(options(run, max_calls=2), call=fake) == 4
    assert len(fake.calls) == 1
    assert len(judge_rows(run)) == label.BATCH_SIZE
    assert stop in capsys.readouterr().out
    assert (run.run_dir / "judge_spend.json").read_text() == payload


def test_cap_counts_the_upcoming_call(make_run, capsys):
    run = make_run(n=1)
    label.write_ledger(run.run_dir, {"calls": 0, "usd": 2.9, "rejected_streak": 0,
                                     "estimated_calls": 0})
    fake = FakeClaude()
    assert label.run_judge(options(run, max_usd=3.0), call=fake) == 4
    assert not fake.calls and "stopped: spend cap" in capsys.readouterr().out
    assert label.read_ledger(run.run_dir)["usd"] == 2.9


def test_real_cost_replaces_estimate_once(make_run):
    run = make_run(n=1)
    label.write_ledger(run.run_dir, {"calls": 0, "usd": 1.0, "rejected_streak": 0,
                                     "estimated_calls": 0})
    assert label.run_judge(options(run), call=FakeClaude(cost=0.25)) == 0
    ledger = label.read_ledger(run.run_dir)
    assert ledger["usd"] == 1.25 and ledger["estimated_calls"] == 0


def test_crash_mid_call_keeps_the_estimate(make_run):
    run = make_run(n=1)
    seen = {}

    def crash(*args):
        seen.update(label.read_ledger(run.run_dir))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        label.run_judge(options(run), call=crash)
    assert seen == {"calls": 1, "usd": 0.5, "rejected_streak": 0, "estimated_calls": 1}
    assert label.read_ledger(run.run_dir) == seen


def test_failed_call_is_counted_before_return(make_run):
    run = make_run(n=1)

    def fail(*args):
        raise RuntimeError("private")

    assert label.run_judge(options(run), call=fail) == 4
    assert label.read_ledger(run.run_dir) == {
        "calls": 1, "usd": 0.5, "rejected_streak": 0, "estimated_calls": 1}
    assert judge_rows(run) == []


def test_rejected_streak_refuses_and_force_preserves_until_accepted(make_run, capsys):
    run = make_run(n=1)
    for streak in (1, 2):
        fake = FakeClaude(miss_calibration=True, cost=0.5)
        assert label.run_judge(options(run), call=fake) == 4
        assert len(fake.calls) == 1 and judge_rows(run) == []
        assert label.read_ledger(run.run_dir) == {
            "calls": streak, "usd": streak * 0.5, "rejected_streak": streak,
            "estimated_calls": 0}
    capsys.readouterr()
    refused = FakeClaude(miss_calibration=True)
    assert label.run_judge(options(run), call=refused) == 2
    assert not refused.calls
    assert ("Refused: 2 consecutive rejected batches; rerun with --force-streak to continue."
            in capsys.readouterr().out)
    forced = FakeClaude(miss_calibration=True)
    assert label.run_judge(options(run, force_streak=True), call=forced) == 4
    assert len(forced.calls) == 1
    assert label.read_ledger(run.run_dir)["rejected_streak"] == 3
    accepted = FakeClaude()
    assert label.run_judge(options(run, force_streak=True), call=accepted) == 0
    assert len(accepted.calls) == 1
    assert label.read_ledger(run.run_dir)["rejected_streak"] == 0


def test_quota_gate_and_force(make_run, monkeypatch, capsys):
    run, fake = make_run(n=1), FakeClaude()
    monkeypatch.setattr(label.shadow_frontier, "quota_skip", lambda *a: "quota_weekly")
    assert label.run_judge(options(run), call=fake) == 4
    assert not fake.calls and judge_rows(run) == []
    output = capsys.readouterr().out
    assert "stopped: quota (quota_weekly)" in output and "calls: 0" in output
    assert label.read_ledger(run.run_dir)["calls"] == 0
    assert label.run_judge(options(run, force_quota=True), call=fake) == 0
    assert len(fake.calls) == 1


@pytest.mark.parametrize("usage,expected", [
    ({MODEL + "-20250929": {}}, True),
    ({"claude-opus-4-1": {}}, False), ({}, False),
])
def test_claude_call_isolated_command_and_model_check(
        make_run, tmp_path, monkeypatch, usage, expected):
    make_run(n=1)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://invalid.example")
    monkeypatch.setenv("MY_SECRET_X", "synthetic-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-auth")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "synthetic-header")
    monkeypatch.setenv("ANTHROPIC_OTHER", "synthetic-other")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-oauth")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_OTHER", "synthetic-other")
    monkeypatch.setattr(label.tempfile, "tempdir", str(tmp_path))
    directories = []

    def fake(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        directories.append(cwd)
        assert cwd.is_dir() and list(cwd.iterdir()) == []
        assert cwd.is_relative_to(tmp_path)
        for flag in ("--no-session-persistence", "--tools", "--setting-sources"):
            assert flag in cmd
        assert cmd[cmd.index("--model") + 1] == MODEL
        assert "ANTHROPIC_BASE_URL" not in kwargs["env"]
        assert "MY_SECRET_X" not in kwargs["env"]
        assert not any(key.startswith("ANTHROPIC_") for key in kwargs["env"])
        assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-oauth"
        assert "CLAUDE_CODE_OAUTH_OTHER" not in kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            dict(result="x", total_cost_usd=0.5, modelUsage=usage)))

    monkeypatch.setattr("llm_router.redo_label.subprocess.run", fake)
    result = label.claude_call("ZQXPREV", "ZQXPROMPT", MODEL)
    assert result == dict(result="x", cost_usd=0.5, model_ok=expected)
    assert len(directories) == 1 and not directories[0].exists()


def test_calibration_has_six_distinct_positive_and_negative_examples():
    pairs = label.load_calibration()
    assert len(pairs) == len({p["cal_id"] for p in pairs}) == 6
    assert {p["expected"] for p in pairs} == {"redo", "not_redo"}
    assert any("bug-report" in p["cal_id"] for p in pairs)


def test_build_batch_shuffles_calibration_positions():
    real = [(f"pair-{n}", label.PairView("previous", "answer", f"prompt-{n}"))
            for n in range(20)]
    patterns = set()
    for seed in range(30):
        _, id_map = label.build_batch(real, label.load_calibration(), random.Random(seed))
        assert list(id_map) == [f"i{n:02d}" for n in range(1, 27)]
        patterns.add(tuple(n for n, (kind, _) in enumerate(id_map.values())
                           if kind == "cal"))
    assert len(patterns) > 1
    assert patterns != {tuple(range(6))}
    assert patterns != {tuple(range(20, 26))}


@pytest.mark.parametrize("extra", [False, True])
def test_reply_requires_exact_ids(make_run, capsys, extra):
    labels = {"i01": "redo", "i02": "not_redo"}
    if extra:
        labels["unknown"] = "redo"
    else:
        labels.pop("i02")
    with pytest.raises(ValueError):
        label.parse_labels(json.dumps({"labels": labels}), ["i01", "i02"])
    run, fake = make_run(n=1), FakeClaude()

    def incomplete(brief, item, model_id):
        reply = fake(brief, item, model_id)
        labels = json.loads(reply["result"])["labels"]
        if extra:
            labels["unknown"] = "redo"
        else:
            labels.pop(next(iter(labels)))
        reply["result"] = json.dumps({"labels": labels})
        return reply

    assert label.run_judge(options(run), call=incomplete) == 4
    assert len(fake.calls) == 1 and judge_rows(run) == []
    assert "unparseable reply" in capsys.readouterr().out
    assert label.read_ledger(run.run_dir)["rejected_streak"] == 1


def test_owner_labels_display_order_ignores_judge_and_resumes(make_run):
    run = make_run(n=8)
    judge_marker = "ZQXJUDGEPRIVATE"
    (run.run_dir / "labels_judge.jsonl").write_text(json.dumps(
        dict(pair_id=judge_marker, label="redo", source="judge")) + "\n")
    rc, output = run_tui(owner_args(), ["y", "n", "u", "q"], run.env)
    rows = owner_rows(run)
    assert rc == 0 and len(rows) == 3
    assert [r["label"] for r in rows] == ["redo", "not_redo", "unclear"]
    assert all(set(r) == SCHEMA and r["source"] == "owner" for r in rows)
    order = label.owner_sample(run.rows, 360, 99, None)
    random.Random(361).shuffle(order)
    assert [r["pair_id"] for r in rows] == order[:3]
    displayed = re.findall(rb"THIS MESSAGE:\r?\n\r?\n([^\r\n]+)", output)
    assert displayed == [run.prompts[p].encode() for p in order[:4]]
    assert b"judge" not in output.lower() and judge_marker.encode() not in output
    assert b"ZQX" not in (run.run_dir / "labels_owner.jsonl").read_bytes()
    rc, output = run_tui(owner_args(), ["n"] * 5, run.env)
    assert rc == 0 and output.count(MARKER) == 5
    final = owner_rows(run)
    assert final[:3] == rows
    assert len(final) == len({r["pair_id"] for r in final}) == 8
    assert all(run.prompts[r["pair_id"]].encode() not in output for r in rows)


def test_owner_records_missing_pairs_as_lost_without_display(make_run):
    run = make_run(n=48, missing=("session", "turn"))
    rc, output = run_tui(owner_args(), ["n"] * 48, run.env)
    rows = owner_rows(run)
    assert rc == 0 and output.count(MARKER) == 48
    assert len(rows) == 50
    assert {r["pair_id"] for r in rows if r["label"] == "lost"} == run.lost
    assert all(r["label"] == "lost" for r in rows if r["pair_id"] in run.lost)


def test_owner_sample_persists_all_frontier_and_three_others(make_run):
    run = make_run()
    frontier = {r["pair_id"] for r in run.rows if r["stratum"] == "F"}
    path = run.run_dir / "owner_sample.json"
    assert run_tui(owner_args(3), ["q"], run.env)[0] == 0
    saved = json.loads(path.read_text())
    assert set(saved) == {"seed", "others"} and saved["seed"] == 360
    assert len(saved["others"]) == len(set(saved["others"])) == 3
    assert not frontier.intersection(saved["others"])
    sample = label.owner_sample(run.rows, 360, 3, saved["others"])
    assert set(sample) == frontier | set(saved["others"])
    assert run_tui(owner_args(3), ["q"], run.env)[0] == 0
    assert json.loads(path.read_text()) == saved
    initial = label.owner_sample(run.rows, 360, 3, None)
    assert initial == label.owner_sample(run.rows, 360, 3, None)
    larger = label.owner_sample(run.rows, 360, 7, saved["others"])
    assert larger[:len(initial)] == initial
    assert len(larger) == len(frontier) + 7


def test_owner_refuses_non_tty_input_or_output_without_writes(make_run, capsys):
    run = make_run(n=4)
    assert label.cmd_redo_label(owner_args()) == 3
    assert "Refused" in capsys.readouterr().out
    master, slave = pty.openpty()
    try:
        proc = subprocess.run([sys.executable, "-c", SCRIPT, *owner_args()],
                              stdin=slave, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=run.env, timeout=20)
        assert proc.returncode == 3 and b"Refused" in proc.stdout
    finally:
        os.close(master)
        os.close(slave)
    assert not (run.run_dir / "labels_owner.jsonl").exists()
    assert not (run.run_dir / "owner_sample.json").exists()


def test_owner_sanitizes_terminal_controls(make_run):
    run = make_run(n=2, text="ZQXPROMPT\x1b[2J\x1b]0;evil\x07")
    rc, output = run_tui(owner_args(), ["n", "n"], run.env)
    assert rc == 0 and output.count(MARKER) == 2
    assert b"\x1b]0;" not in output and b"\x07" not in output
    assert output.count(b"\x1b[2J") == output.count(MARKER) + 1


def test_owner_done_marker_only_after_the_whole_pass(make_run):
    run = make_run(n=30)                          # 10 F pairs and 20 S/R pairs
    rc, _ = run_tui(owner_args(20), ["n", "q"], run.env)
    assert rc == 0 and not (run.run_dir / "owner_done.json").exists()
    rc, output = run_tui(owner_args(20), ["n"] * 29, run.env)
    assert rc == 0 and b"owner pass complete: n_labelled=30 spot_checks=20" in output
    record = json.loads((run.run_dir / "owner_done.json").read_text())
    assert set(record) == {"n_labelled", "labels_owner_sha256", "others", "owner_sample_sha256"}
    assert record["n_labelled"] == 30 and record["others"] == 20


def test_owner_screen_shows_the_label_definition(make_run):
    """RLUI: the owner pressed y on 25/25 pairs twice after misreading what y means; every screen now
    states the y/n definition above the key prompt."""
    run = make_run(n=2)
    rc, output = run_tui(owner_args(), ["n", "n"], run.env)
    assert rc == 0 and output.count(MARKER) == 2
    flat = b" ".join(output.split())
    y_line = (b"y = THIS MESSAGE says the previous answer was wrong or incomplete and asks to fix, "
              b"redo or retry it")
    n_line = b"n = anything else: new task, follow-up question, 'keep going', approval, thanks"
    assert flat.count(y_line) == flat.count(n_line) == 2
    screens = flat.split(b"[y] redo [n] not redo [u] unclear [q] quit >")[:-1]
    assert len(screens) == 2                      # each screen: blocks, then the definition, then keys
    assert all(screen.rfind(b"THIS MESSAGE:") < screen.rfind(y_line) < screen.rfind(n_line)
               for screen in screens)


@pytest.mark.parametrize("key, shown", [("n", b"n (not redo)"), ("y", b"y (redo)")])
def test_all_identical_owner_pass_warns_but_records_as_given(make_run, key, shown):
    run = make_run(n=30)                          # 10 F pairs and 20 S/R pairs
    rc, output = run_tui(owner_args(20), [key] * 30, run.env)
    flat = b" ".join(output.split())
    assert rc == 0 and b"WARNING: all 30 owner labels in this pass are " + shown in flat
    assert b"llm-router redo-label --run run1 --owner --others 20" in flat
    assert b"owner pass complete: n_labelled=30 spot_checks=20" in flat
    record = json.loads((run.run_dir / "owner_done.json").read_text())
    assert record["n_labelled"] == 30
    assert {r["label"] for r in owner_rows(run)} == {"redo" if key == "y" else "not_redo"}


def test_mixed_owner_pass_prints_no_warning(make_run):
    run = make_run(n=30)
    rc, output = run_tui(owner_args(20), ["y"] + ["n"] * 29, run.env)
    assert rc == 0 and b"owner pass complete: n_labelled=30" in output
    assert b"WARNING" not in output


@pytest.mark.parametrize("others", [0, 1])
def test_owner_pass_without_the_spot_check_minimum_writes_no_marker(make_run, others):
    """Design section 3 step 2: every F pair plus 20 S/R spot checks. --others 0 or 1 labels the F pairs
    but records no complete owner pass."""
    run = make_run(n=30)
    rc, output = run_tui(owner_args(others), ["n"] * (10 + others), run.env)
    assert rc == 0 and len(owner_rows(run)) == 10 + others
    assert b"owner pass NOT recorded complete" in output
    assert not (run.run_dir / "owner_done.json").exists()


def test_minimum_message_reports_the_available_pairs_when_it_cannot_be_reached(make_run):
    run = make_run(n=15)                          # 5 F pairs and 10 S/R pairs: 20 spot checks cannot exist
    rc, output = run_tui(owner_args(20), ["n"] * 15, run.env)
    assert rc == 0 and not (run.run_dir / "owner_done.json").exists()
    assert b"holds only 10 S/R pairs" in output and b"cannot reach the minimum of 20" in output
    assert b"Rerun" not in output and b"rerun" not in output


def _drain_pty(master: int, *, idle: float = 0.5, overall: float = 15.0) -> bytes:
    """Everything the writer left in a pty master. One ``os.read`` returns whatever the line
    discipline has handed over so far -- on Linux a partial chunk (CI runs 40212431331 and
    40214069230) -- so read until EOF/EIO, or until the master has been quiet for ``idle``
    seconds (the writer has already returned), or ``overall`` seconds have passed."""
    chunks: list[bytes] = []
    deadline = time.monotonic() + overall
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], idle)
        if not ready:
            break
        try:
            data = os.read(master, 65536)
        except OSError as err:
            if err.errno == errno.EIO:  # Linux: slave side closed
                break
            raise
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def test_minimum_message_names_the_unverifiable_sample(make_run, monkeypatch):
    from llm_router import redo_score as score
    run = make_run(n=30)
    monkeypatch.setattr(score, "owner_sample_state", lambda run_dir: None)
    options_ = options(run, owner=True, judge=False, others=20)
    for pair_id in {r["pair_id"] for r in run.rows}:
        label.append_label(run.run_dir / "labels_owner.jsonl", pair_id, "not_redo", "owner")
    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "w", closefd=False) as tty_out, open(slave, "rb", closefd=False) as tty_in:
            assert label.run_owner(options_, stdin=tty_in, stdout=tty_out) == 0
        text = _drain_pty(master).decode()
    finally:
        os.close(master)
        os.close(slave)
    assert "cannot be verified" in text and "NOT recorded complete" in text


def test_raised_others_invalidates_the_old_marker(make_run):
    from llm_router import redo_score as score
    run = make_run(n=45)                          # 15 F pairs and 30 S/R pairs
    assert run_tui(owner_args(20), ["n"] * 35, run.env)[0] == 0
    assert score.owner_done_valid(run.run_dir)
    assert run_tui(owner_args(25), ["q"], run.env)[0] == 0      # sample extended, nothing labelled
    assert not score.owner_done_valid(run.run_dir)
    rc, output = run_tui(owner_args(25), ["n"] * 5, run.env)    # the extended sample is labelled
    assert rc == 0 and b"spot_checks=25" in output
    assert score.owner_done_valid(run.run_dir)


def test_state_contains_metadata_only_and_private_label_files(make_run):
    run = make_run(missing=("session", "turn"))
    assert label.run_judge(options(run), call=FakeClaude()) == 0
    assert run_tui(owner_args(), ["n"] * 48, run.env)[0] == 0
    files = [p for p in run.state.rglob("*") if p.is_file()]
    assert {p.name for p in files} == {
        "manifest.jsonl", "frozen.json", "labels_judge.jsonl",
        "labels_owner.jsonl", "owner_done.json", "owner_sample.json", "judge_meta.json", "judge_spend.json", ".lock"}
    for path in files:
        assert b"ZQX" not in path.read_bytes()
        if path.name in {"judge_meta.json", "judge_spend.json", ".lock"}:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        if path.name.startswith("labels_"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert all(set(row) == SCHEMA for row in jsonl(path))


def test_label_rows_are_compatible_with_redo_score(make_run):
    pytest.importorskip("llm_router.redo_score")
    run = make_run(n=48, missing=("session", "turn"))
    assert label.run_judge(options(run), call=FakeClaude()) == 0
    assert run_tui(owner_args(), ["y", "n", "u"] + ["n"] * 45, run.env)[0] == 0
    for row in judge_rows(run) + owner_rows(run):
        assert set(row) == SCHEMA
        assert row["label"] in ("redo", "not_redo", "unclear", "lost")
        assert row["source"] in ("owner", "judge")


def test_cli_wiring_and_invalid_runs_write_nothing(make_run, capsys):
    run = make_run(n=1)
    before = {p: p.read_bytes() for p in run.state.rglob("*") if p.is_file()}
    assert "redo-label" in cli._KNOWN_SUBCOMMANDS
    for name in ("nope", "../x"):
        assert label.cmd_redo_label(["--run", name, "--judge", "--model-id", MODEL]) == 2
        assert "Refused" in capsys.readouterr().out
    assert {p: p.read_bytes() for p in run.state.rglob("*") if p.is_file()} == before


def test_resolve_views_bounds_long_transcript_text(make_run):
    text = "ZQXPREV" + "x" * 5000
    run = make_run(n=2, text=text)
    opts = options(run)
    views = label.resolve_views(opts.manifest, opts.frozen, run.projects)
    assert set(views) == set(run.prompts)
    for view in views.values():
        assert isinstance(view, label.PairView)
        assert view.prev_head == text[:500] and len(view.prev_head) <= 500
        assert view.answer_tail == text[-1500:] and len(view.answer_tail) <= 1500
        assert view.prompt == text[:2000] and len(view.prompt) <= 2000


@pytest.mark.parametrize("mode", ["judge", "owner"])
@pytest.mark.parametrize("corpus", ["wrong", "empty", "zero", "fraction"])
def test_corpus_refusal_writes_nothing(make_run, tmp_path, mode, corpus):
    run = make_run(n=1, missing=("session",))
    projects = run.projects
    if corpus in ("wrong", "empty"):
        projects = tmp_path / "other"
        if corpus == "empty":
            (projects / "proj").mkdir(parents=True)
    elif corpus == "zero":
        (projects / "proj" / "session-0.jsonl").write_text("")
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    if mode == "judge":
        fake = FakeClaude()
        assert label.run_judge(options(run, projects_dir=projects), call=fake) == 2
        assert not fake.calls
    else:
        args = owner_args() + ["--projects-dir", str(projects)]
        rc, output = run_tui(args, [], run.env)
        assert rc == 2 and b"Refused:" in output
        assert b"ZQX" not in output
    # The empty 0600 run lock is the only file an owner-mode refusal may add.
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()
            if p.name != ".lock"} == before


def test_force_overrides_only_fraction(make_run, tmp_path):
    run = make_run(n=1, missing=("session",))
    opts = options(run)
    views = label.resolve_views(opts.manifest, opts.frozen, run.projects)
    assert label.check_corpus(run.rows, views, run.projects, 0) is not None
    assert label.check_corpus(run.rows, views, run.projects, 0, force_unresolved=True) is None
    assert label.check_corpus(run.rows, {}, run.projects, 0, force_unresolved=True) is not None
    assert label.check_corpus(run.rows, views, tmp_path / "missing", 0, force_unresolved=True)
    empty = tmp_path / "empty"
    (empty / "proj").mkdir(parents=True)
    assert label.check_corpus(run.rows, views, empty, 0, force_unresolved=True)


@pytest.mark.parametrize("mode", ["judge", "owner"])
def test_session_read_errors_counted_and_reported(make_run, monkeypatch, capsys, mode):
    import io

    run = make_run()
    original = label.redo_signal.read_transcript

    def read(path):
        if path.stem == "session-1":
            raise OSError("ZQXPRIVATE")
        return original(path)

    monkeypatch.setattr(label.redo_signal, "read_transcript", read)
    opts = options(run)
    stats = {}
    views = label.resolve_views(opts.manifest, opts.frozen, run.projects, stats)
    assert stats == {"errors": 1, "sessions": 48}
    assert len(views) == 47
    if mode == "judge":
        assert label.run_judge(opts, call=FakeClaude()) == 0
        output = capsys.readouterr().out
        rows = judge_rows(run)
    else:
        class Terminal:
            def isatty(self):
                return True

            def fileno(self):
                return 0

        class Output(io.StringIO):
            def isatty(self):
                return True

        stdout = Output()
        monkeypatch.setattr(label.termios, "tcgetattr", lambda fd: [])
        monkeypatch.setattr(label.termios, "tcsetattr", lambda *args: None)
        monkeypatch.setattr(label.tty, "setcbreak", lambda *args: None)
        monkeypatch.setattr(label.os, "read", lambda *args: b"n")
        assert label.run_owner(opts, stdin=Terminal(), stdout=stdout) == 0
        output = stdout.getvalue()
        rows = owner_rows(run)
    assert "read errors: 1" in output
    assert "ZQXPRIVATE" not in output
    assert sum(row["label"] == "lost" for row in rows) == 1


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_judge_credentials_refused_without_writes(make_run, monkeypatch, capsys, key):
    run = make_run()
    monkeypatch.setenv(key, "ZQXSECRET")
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    fake = FakeClaude()
    assert label.run_judge(options(run), call=fake) == 2
    assert not fake.calls
    output = capsys.readouterr().out
    assert "Refused: ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is set" in output
    assert "ZQX" not in output
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before
    assert label.run_judge(options(run, allow_api_key=True), call=fake) == 0
    assert json.loads((run.run_dir / "judge_meta.json").read_text()) == {
        "model_id": MODEL, "allow_api_key": True}


@pytest.mark.parametrize("base", [None, "own", "https://ZQX.example"])
def test_judge_base_url_preflight(make_run, monkeypatch, capsys, base):
    from llm_router.proxy_default import DEFAULT_PORT

    run = make_run()
    if base is not None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL",
                           f"http://127.0.0.1:{DEFAULT_PORT}" if base == "own" else base)
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    fake = FakeClaude()
    rc = label.run_judge(options(run), call=fake)
    if base and base != "own":
        assert rc == 2 and not fake.calls
        assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before
        output = capsys.readouterr().out
        assert "Refused: ANTHROPIC_BASE_URL points at a custom host" in output
        assert "ZQX" not in output
    else:
        assert rc == 0 and fake.calls


def test_allowlist_api_opt_in():
    env = {"ANTHROPIC_API_KEY": "key", "ANTHROPIC_AUTH_TOKEN": "auth",
           "ANTHROPIC_CUSTOM_HEADERS": "headers", "ANTHROPIC_OTHER": "other",
           "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "LC_ALL": "C"}
    assert label.judge_env_allowlist(env) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "LC_ALL": "C",
        "LLM_ROUTER_SESSION_KIND": "research"}
    allowed = label.judge_env_allowlist(env, allow_api_key=True)
    assert allowed["ANTHROPIC_API_KEY"] == "key"
    assert allowed["ANTHROPIC_AUTH_TOKEN"] == "auth"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in allowed
    assert "ANTHROPIC_OTHER" not in allowed


def test_judge_model_metadata_and_mismatch_without_writes(make_run, capsys):
    run = make_run(missing=("session",))
    assert label.run_judge(options(run, max_calls=1), call=FakeClaude()) == 4
    meta_path = run.run_dir / "judge_meta.json"
    assert json.loads(meta_path.read_text()) == {"model_id": MODEL, "allow_api_key": False}
    assert stat.S_IMODE(meta_path.stat().st_mode) == 0o600
    assert "ZQX" not in meta_path.read_text()
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    fake = FakeClaude()
    assert label.run_judge(options(run, model_id="different"), call=fake) == 2
    assert not fake.calls
    assert "Refused: --model-id differs from the id recorded for this run." in (
        capsys.readouterr().out)
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before
    assert label.run_judge(options(run), call=FakeClaude()) == 0


def test_judge_requires_model_id(make_run, capsys):
    run = make_run()
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    with pytest.raises(SystemExit) as exc:
        label.cmd_redo_label(["--run", "run1", "--judge"])
    assert exc.value.code == 2
    assert "--model-id is required with --judge" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before


def test_claude_call_passes_api_opt_in(make_run, monkeypatch):
    make_run(n=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "synthetic-header")

    def fake(cmd, **kwargs):
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == "synthetic-key"
        assert "ANTHROPIC_CUSTOM_HEADERS" not in kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            dict(result="x", modelUsage={MODEL: {}})))

    monkeypatch.setattr(label.subprocess, "run", fake)
    assert label.claude_call("brief", "item", MODEL, allow_api_key=True)["model_ok"]


@pytest.mark.parametrize("others", [["--oth", "5"], ["--others=5"]])
def test_judge_rejects_owner_flags_without_writes(make_run, others):
    run = make_run()
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    with pytest.raises(SystemExit) as exc:
        label.cmd_redo_label(["--run", "run1", "--judge", "--model-id", "m", *others])
    assert exc.value.code == 2
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before


def test_help_documents_escalation_and_cumulative_caps(capsys):
    with pytest.raises(SystemExit) as exc:
        label.cmd_redo_label(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "cumulative" in output
    assert "rerun --owner with a larger --others" in output
    assert "--model-id" in output and "--allow-api-key" in output


@pytest.mark.parametrize("exc_type", [SystemExit, KeyboardInterrupt])
def test_interrupted_call_is_precounted(make_run, exc_type):
    run = make_run(n=1)

    def crash(*args):
        # Simulate a crash after inspecting the durable pre-write.
        assert json.loads((run.run_dir / "judge_spend.json").read_text())["calls"] == 1
        raise exc_type

    with pytest.raises(exc_type):
        label.run_judge(options(run), call=crash)
    assert label.read_ledger(run.run_dir)["calls"] == 1
    assert judge_rows(run) == []


@pytest.mark.parametrize("cost", [None, 0, -1, float("nan"), float("inf"),
                                 -float("inf"), "0.2", True])
def test_unknown_cost_is_estimated(make_run, capsys, cost):
    run, fake = make_run(n=1), FakeClaude(cost=cost)

    def reply(*args):
        result = fake(*args)
        if cost is None:
            result.pop("cost_usd")
        return result

    assert label.run_judge(options(run), call=reply) == 0
    assert label.read_ledger(run.run_dir) == {
        "calls": 1, "usd": 0.50, "rejected_streak": 0, "estimated_calls": 1}
    assert "estimated calls: 1" in capsys.readouterr().out


def test_real_cost_is_exact(make_run, capsys):
    run = make_run(n=1)
    assert label.run_judge(options(run), call=FakeClaude(cost=0.123)) == 0
    ledger = label.read_ledger(run.run_dir)
    assert ledger["usd"] == 0.123 and ledger["estimated_calls"] == 0
    assert "estimated calls:" not in capsys.readouterr().out


def test_unknown_cost_hits_spend_cap(make_run, capsys):
    run, fake = make_run(), FakeClaude(cost=None)
    assert label.run_judge(options(run, max_usd=0.6), call=fake) == 4
    assert len(fake.calls) == 1
    assert label.read_ledger(run.run_dir)["usd"] == 0.5
    output = capsys.readouterr().out
    assert "stopped: spend cap" in output and "estimated calls: 1" in output
    assert label.run_judge(options(run, max_usd=0.6), call=fake) == 4
    assert len(fake.calls) == 1
    assert "estimated calls:" not in capsys.readouterr().out


def test_old_ledger_defaults_estimated_calls_to_zero(tmp_path):
    old = {"calls": 2, "usd": 0.25, "rejected_streak": 1}
    (tmp_path / "judge_spend.json").write_text(json.dumps(old))
    assert label.read_ledger(tmp_path) == {**old, "estimated_calls": 0}
    label.write_ledger(tmp_path, label.read_ledger(tmp_path))
    assert json.loads((tmp_path / "judge_spend.json").read_text()) == {
        **old, "estimated_calls": 0}


@pytest.mark.parametrize("value", [True, 1.0, "1", None, -1])
def test_ledger_rejects_invalid_estimated_calls(tmp_path, value):
    data = {"calls": 2, "usd": 1, "rejected_streak": 0, "estimated_calls": value}
    (tmp_path / "judge_spend.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        label.read_ledger(tmp_path)


@pytest.mark.parametrize("missing,force,refused", [
    (6, False, True), (5, False, False), (6, True, False)])
def test_unresolved_fraction_boundary(make_run, missing, force, refused):
    run = make_run(n=100 - missing, missing=("session",) * missing)
    opts = options(run)
    views = label.resolve_views(run.rows, opts.frozen, run.projects)
    reason = label.check_corpus(run.rows, views, run.projects, 0,
                                force_unresolved=force)
    assert (reason is not None) == refused
    if refused:
        assert "unresolved fraction exceeds 5%" in reason
    fake = FakeClaude()
    assert label.run_judge(options(run, force_unresolved=force), call=fake) == (
        2 if refused else 0)
    assert bool(fake.calls) != refused


@pytest.mark.parametrize("force", [False, True])
def test_unresolved_f_pair_never_waived(make_run, force):
    run = make_run(n=99, missing=("session",))
    run.rows[-1]["stratum"] = "F"
    write_jsonl(run.run_dir / "manifest.jsonl", run.rows)
    opts = options(run, force_unresolved=force)
    views = label.resolve_views(run.rows, opts.frozen, run.projects)
    assert "unresolved F pairs (1 of 100 pairs)" in label.check_corpus(
        run.rows, views, run.projects, 0, force_unresolved=force)
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    fake = FakeClaude()
    assert label.run_judge(opts, call=fake) == 2
    assert not fake.calls
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before


@pytest.mark.parametrize("reason", ["quota_weekly", "quota_unknown", "quota_stale"])
@pytest.mark.parametrize("flag", ["force_quota", "force_streak", "force_unresolved"])
def test_quota_override_is_independent(make_run, monkeypatch, reason, flag):
    run, fake = make_run(n=1), FakeClaude()
    monkeypatch.setattr(label.shadow_frontier, "quota_skip", lambda *a: reason)
    assert label.run_judge(options(run, **{flag: True}), call=fake) == (
        0 if flag == "force_quota" else 4)
    assert len(fake.calls) == (1 if flag == "force_quota" else 0)


@pytest.mark.parametrize("flag", ["force_quota", "force_streak", "force_unresolved"])
def test_streak_override_is_independent(make_run, flag):
    run, fake = make_run(n=1), FakeClaude()
    label.write_ledger(run.run_dir, {
        "calls": 0, "usd": 0.0, "rejected_streak": 2, "estimated_calls": 0})
    assert label.run_judge(options(run, **{flag: True}), call=fake) == (
        0 if flag == "force_streak" else 2)
    assert len(fake.calls) == (1 if flag == "force_streak" else 0)


@pytest.mark.parametrize("flag", ["force_quota", "force_streak", "force_unresolved"])
def test_fraction_override_is_independent(make_run, flag):
    run, fake = make_run(n=94, missing=("session",) * 6), FakeClaude()
    assert label.run_judge(options(run, **{flag: True}), call=fake) == (
        0 if flag == "force_unresolved" else 2)
    assert bool(fake.calls) == (flag == "force_unresolved")


@pytest.mark.parametrize("mode", ["--judge", "--owner"])
def test_legacy_force_returns_two_without_writes(make_run, capsys, mode):
    run = make_run(n=1)
    before = {p: p.read_bytes() for p in run.state.rglob("*") if p.is_file()}
    assert label.cmd_redo_label(["--run", "run1", mode, "--force"]) == 2
    assert capsys.readouterr().out.strip() == (
        "--force was split: use --force-quota, --force-streak and/or --force-unresolved")
    assert {p: p.read_bytes() for p in run.state.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("payload,error", [
    ("{", "JSONDecodeError"), ("[]", "ValueError"), ("null", "ValueError"),
    ("{}", "ValueError"), ('{"model_id": 1}', "ValueError"),
    ('{"model_id": null}', "ValueError"),
])
def test_invalid_judge_metadata_refuses_without_writes(make_run, capsys, payload, error):
    run, fake = make_run(n=1), FakeClaude()
    (run.run_dir / "judge_meta.json").write_text(payload)
    before = {p: p.read_bytes() for p in run.run_dir.iterdir()}
    assert label.run_judge(options(run), call=fake) == 2
    assert capsys.readouterr().out.strip() == (
        f"Refused: invalid judge_meta.json ({error}).")
    assert not fake.calls
    assert {p: p.read_bytes() for p in run.run_dir.iterdir()} == before


@pytest.mark.parametrize("stage", ["read_transcript", "turn_pairs"])
def test_resolve_views_retries_duplicate_after_error(make_run, monkeypatch, stage):
    run = make_run(n=1)
    good = run.projects / "proj" / "session-0.jsonl"
    duplicate = run.projects / "aaa" / good.name
    duplicate.parent.mkdir()
    duplicate.write_bytes(good.read_bytes())
    original = getattr(label.redo_signal, stage)
    attempts = []

    def fail_first(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("ZQXPRIVATE")
        return original(*args)

    monkeypatch.setattr(label.redo_signal, stage, fail_first)
    stats = {}
    views = label.resolve_views(run.rows, options(run).frozen, run.projects, stats)
    assert set(views) == set(run.prompts)
    assert len(attempts) == 2 and stats == {"errors": 1, "sessions": 2}


@pytest.mark.parametrize("cost", [None, 0, -1])
def test_claude_call_unknown_cost(make_run, monkeypatch, cost):
    make_run(n=1)
    data = dict(result="x", modelUsage={MODEL: {}})
    if cost is not None:
        data["total_cost_usd"] = cost
    monkeypatch.setattr(label.subprocess, "run", lambda cmd, **kwargs:
                        subprocess.CompletedProcess(cmd, 0, json.dumps(data)))
    assert label.claude_call("brief", "item", MODEL)["cost_usd"] is None


def test_split_flags_documented_in_both_help_surfaces(capsys):
    with pytest.raises(SystemExit) as exc:
        label.cmd_redo_label(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    usage = next(line for line in cli.__doc__.splitlines()
                 if "llm-router redo-label " in line)
    for flag in ("--allow-api-key", "--force-quota", "--force-streak", "--force-unresolved"):
        assert flag in output and flag in usage
    assert "F-pair rule is never waived" in output
