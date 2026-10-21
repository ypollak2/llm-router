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
                             turn_index=turn, stratum="FSR"[i % 3],
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
                  max_usd=100, model_id=label.PINNED_MODEL_ID, others=99,
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
    assert label.run_judge(options(run), call=second) == 0
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


@pytest.mark.parametrize("usage,expected", [
    ({"claude-sonnet-4-5-20250929": {}}, True),
    ({"claude-opus-4-1": {}}, False), ({}, False),
])
def test_claude_call_isolated_command_and_model_check(
        make_run, tmp_path, monkeypatch, usage, expected):
    make_run(n=1)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://invalid.example")
    monkeypatch.setenv("MY_SECRET_X", "synthetic-secret")
    monkeypatch.setattr(label.tempfile, "tempdir", str(tmp_path))
    directories = []

    def fake(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        directories.append(cwd)
        assert cwd.is_dir() and list(cwd.iterdir()) == []
        assert cwd.is_relative_to(tmp_path)
        for flag in ("--no-session-persistence", "--tools", "--setting-sources"):
            assert flag in cmd
        assert cmd[cmd.index("--model") + 1] == label.PINNED_MODEL_ID
        assert "ANTHROPIC_BASE_URL" not in kwargs["env"]
        assert "MY_SECRET_X" not in kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            dict(result="x", total_cost_usd=0.5, modelUsage=usage)))

    monkeypatch.setattr("llm_router.redo_label.subprocess.run", fake)
    result = label.claude_call("ZQXPREV", "ZQXPROMPT", label.PINNED_MODEL_ID)
    assert result == dict(result="x", cost_usd=0.5, model_ok=expected)
    assert len(directories) == 1 and not directories[0].exists()


def test_calibration_has_six_distinct_positive_and_negative_examples():
    pairs = label.load_calibration()
    assert len(pairs) == len({p["cal_id"] for p in pairs}) == 6
    assert {p["expected"] for p in pairs} == {"redo", "not_redo"}
    assert any("bug-report" in p["cal_id"] for p in pairs)


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
    run = make_run(n=5, missing=("session", "turn"))
    rc, output = run_tui(owner_args(), ["n"] * 5, run.env)
    rows = owner_rows(run)
    assert rc == 0 and output.count(MARKER) == 5
    assert len(rows) == 7
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
    assert output.count(b"\x1b[2J") == output.count(MARKER)


def test_state_contains_metadata_only_and_private_label_files(make_run):
    run = make_run(missing=("session", "turn"))
    assert label.run_judge(options(run), call=FakeClaude()) == 0
    assert run_tui(owner_args(), ["n"] * 48, run.env)[0] == 0
    files = [p for p in run.state.rglob("*") if p.is_file()]
    assert {p.name for p in files} == {
        "manifest.jsonl", "frozen.json", "labels_judge.jsonl",
        "labels_owner.jsonl", "owner_sample.json"}
    for path in files:
        assert b"ZQX" not in path.read_bytes()
        if path.name.startswith("labels_"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert all(set(row) == SCHEMA for row in jsonl(path))


def test_label_rows_are_compatible_with_redo_score(make_run):
    pytest.importorskip("llm_router.redo_score")
    run = make_run(n=4, missing=("session", "turn"))
    assert label.run_judge(options(run), call=FakeClaude()) == 0
    assert run_tui(owner_args(), ["y", "n", "u", "n"], run.env)[0] == 0
    for row in judge_rows(run) + owner_rows(run):
        assert set(row) == SCHEMA
        assert row["label"] in ("redo", "not_redo", "unclear", "lost")
        assert row["source"] in ("owner", "judge")


def test_cli_wiring_and_invalid_runs_write_nothing(make_run, capsys):
    run = make_run(n=1)
    before = {p: p.read_bytes() for p in run.state.rglob("*") if p.is_file()}
    assert "redo-label" in cli._KNOWN_SUBCOMMANDS
    for name in ("nope", "../x"):
        assert label.cmd_redo_label(["--run", name, "--judge"]) == 2
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
