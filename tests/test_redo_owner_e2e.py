"""P3.6-c PR3f: the whole owner pass on synthetic data (tune draw, judge, owner TUI via pty, score, freeze), and
the owner sample being fixed once a canonical tune scoring exists. No live state, network or real model."""
# ruff: noqa: F811
from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import redo_label as label, redo_score as score
from tests.test_redo_label import MARKER, SCRIPT, FakeClaude, MODEL
from tests.test_redo_tune import (
    env,  # noqa: F401 -- pytest fixture dependency
    log_rows,
    run_sample,
    score_tune,
    sessions,
    state,
    words,
    write,
)

# Planted second prompts (the first of each pair is filler) and the owner's reading of them.
PLANTED = {
    "reask": ("claude: " + words(3, "q"), "y"),
    "plain": ("that didn't work, " + words(3, "s"), "y"),
    "v3c_only": ("the same error again", "y"),
    "v3d_only": ("wrong answer here", "y"),
}


@pytest.fixture(autouse=True)
def _judge_environment(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(label.shadow_frontier, "quota_skip", lambda *a, **k: None)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(str(Path(p or os.getcwd()).resolve()) for p in sys.path))


def owner_key(screen: bytes) -> str:
    """The owner's reading of the pair on screen: planted rejections are redo, everything else is not."""
    message = screen.rsplit(b"THIS MESSAGE:", 1)[-1].decode("utf-8", "replace")
    return "y" if any(text.split(",")[0].split(":")[0] in message for text, _ in PLANTED.values()) else "n"


def owner_tui(args, env_vars, key=owner_key):
    """Drive ``redo-label --owner`` through a real pty; ``key(screen)`` answers each prompt."""
    master, slave = pty.openpty()
    output, consumed, proc = bytearray(), 0, None
    try:
        proc = subprocess.Popen([sys.executable, "-c", SCRIPT, *args], stdin=slave, stdout=slave,
                                stderr=slave, env=env_vars)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 30
        while True:
            position = output.find(MARKER, consumed)
            if position >= 0:
                os.write(master, key(bytes(output[consumed:position])).encode())
                consumed = position + len(MARKER)
                deadline = time.monotonic() + 30
            if deadline < time.monotonic():
                pytest.fail(f"owner TUI stalled: {bytes(output)[-300:]!r}")
            if select.select([master], [], [], 0.2)[0]:
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
        return proc.wait(timeout=30), bytes(output)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        if slave is not None:
            os.close(slave)
        os.close(master)


def judge_options(run_id):
    run_dir, manifest, frozen = label.load_run(run_id)
    return argparse.Namespace(run=run_id, run_dir=run_dir, manifest=manifest, frozen=frozen,
                              projects_dir=Path(os.environ["CLAUDE_PROJECTS_DIR"]), seed=360, max_calls=50,
                              max_usd=100, model_id=MODEL, others=20, allow_api_key=False, force_quota=False,
                              force_streak=False, force_unresolved=False, judge=True, owner=False)


@pytest.fixture
def pass_done(env):
    """Tune draw (30 pairs: 4 planted rejections, 26 filler), fake judge, owner pass with 20 spot checks."""
    sessions(env, "fill", 13)
    for name, (prompt, _) in PLANTED.items():
        write(env, f"private-{name}", [words(4, name), prompt])
    assert run_sample("--tune", "--run", "t1") == 0
    run = state(env) / "tune-t1"
    assert label.run_judge(judge_options("tune-t1"), call=FakeClaude()) == 0
    rc, output = owner_tui(["--run", "tune-t1", "--owner", "--others", "20"], os.environ.copy())
    assert rc == 0 and b"owner pass complete" in output and b"spot_checks=20" in output
    assert score.owner_done_valid(run)
    return env, run


def test_owner_pass_chain_scores_and_freezes_the_argmax(pass_done, capsys):
    env, run = pass_done
    for candidate in ("v2", "v3c", "v3d"):
        assert score_tune(env, candidate) == 0
    capsys.readouterr()
    rows = log_rows(env)
    assert [r["candidate"] for r in rows] == ["v2", "v3c", "v3d"]
    assert all(r["owner_done"] is True and r["n_lost"] == 0 for r in rows)
    f1 = [r["overall"]["unweighted"]["f1"] for r in rows]
    assert f1[0] < f1[1] < f1[2]                   # the planted rejections give a strict argmax
    assert score.canonical_tune_set(rows, "tune-t1") is not None
    assert run_sample("--freeze", "--run-id", "f1", "--allow-below-threshold", "--detector", "v3d") == 0
    frozen = json.loads((state(env) / "f1" / "frozen.json").read_text())
    assert frozen["detector_variant"] == "v3d" and frozen["tune_run"] == "tune-t1"


def test_owner_sample_is_fixed_after_a_canonical_scoring(pass_done, capsys):
    env, run = pass_done
    for candidate in ("v2", "v3c", "v3d"):
        assert score_tune(env, candidate) == 0
    capsys.readouterr()
    before = {name: (run / name).read_bytes() for name in
              (score.OWNER_SAMPLE_FILE, score.OWNER_DONE_FILE, score.OWNER_LABEL_FILE)}
    rc, output = owner_tui(["--run", "tune-t1", "--owner", "--others", "25"], os.environ.copy())
    assert rc == 2 and b"owner sample is fixed once a canonical tune scoring exists" in output
    assert b"void the run to change it" in output
    assert {name: (run / name).read_bytes() for name in before} == before
    assert score.owner_done_valid(run)
    rc, output = owner_tui(["--run", "tune-t1", "--owner", "--others", "20"], os.environ.copy())
    assert rc == 0 and b"fixed once" not in output                       # the same sample is no change
    assert run_sample("--freeze", "--run-id", "f1", "--allow-below-threshold", "--detector", "v3d") == 0


def test_owner_sample_may_still_grow_before_a_canonical_scoring(pass_done, capsys):
    env, run = pass_done
    assert score_tune(env, "v2") == 0                      # canonical row exists...
    log = state(env) / "tune_log.jsonl"
    log.write_text("")                                      # ...removed: no canonical scoring now
    rc, output = owner_tui(["--run", "tune-t1", "--owner", "--others", "25"], os.environ.copy())
    assert rc == 0 and b"fixed once" not in output and b"spot_checks=25" in output
