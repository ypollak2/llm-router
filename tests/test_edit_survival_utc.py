"""edit_survival must give the same verdicts in every host time zone.

``_commits_touching_since`` used to hand git a zone-less ``--since``, which git reads as LOCAL
time. East of UTC (the owner's Mac is on Asia/Jerusalem) a survived edit was scored as redo;
west of UTC a redone edit could be scored as survived. CI runs in UTC, so only this
parametrised test sees it.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from llm_router import edit_survival as es

ZONES = ["UTC", "Asia/Jerusalem", "America/New_York", "Pacific/Kiritimati"]
COMMIT_TS = 1_700_000_000  # 2023-11-14, so Jerusalem is +2 and New York -5 (no DST edge)


def _git(args, cwd, env):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def zone(request, monkeypatch):
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def repo(tmp_path, zone):
    d = tmp_path / "repo"
    d.mkdir()
    iso = datetime.fromtimestamp(COMMIT_TS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {**os.environ, "TZ": zone, "GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}
    _git(["init", "-q"], d, env)
    (d / "foo.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "foo.py"], d, env)
    _git(["commit", "-q", "-m", "c"], d, env)
    return d


@pytest.mark.parametrize("zone", ZONES, indirect=True)
@pytest.mark.parametrize("offset, expected", [(+100, 0), (-100, 1)])
def test_commit_count_is_zone_independent(repo, zone, offset, expected):
    # edit logged 100 s after the only commit -> survived (0); 100 s before it -> redone (1)
    assert es._commits_touching_since(repo, repo / "foo.py", COMMIT_TS + offset) == expected


@pytest.mark.parametrize("zone", ZONES, indirect=True)
def test_judge_row_verdict_is_zone_independent(repo, zone):
    survived = es.judge_row({"file": str(repo / "foo.py"), "model": "m", "ts": COMMIT_TS + 100})
    redone = es.judge_row({"file": str(repo / "foo.py"), "model": "m", "ts": COMMIT_TS - 100})
    assert (survived.verdict, redone.verdict) == ("survived", "redone")
