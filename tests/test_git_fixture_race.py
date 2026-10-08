"""Regression: the fixture repo helper must not let git spawn background maintenance.

`git commit` runs a detached `git maintenance run --auto` when the global config enables it; that
child creates and removes `.git/objects/maintenance.lock` and rewrites objects while a test
copytree()s the repo, failing with shutil.Error. See docs/BUGS.md (CI-1).
"""
from __future__ import annotations

import json

from tests import test_verify_unit as TV
from tests.toolkit_fixtures import make_source

HOSTILE = ("[gc]\n\tauto = 1\n[maintenance]\n\tauto = true\n"
           "[maintenance \"loose-objects\"]\n\tauto = 1\n")


def test_init_spawns_no_background_maintenance_even_under_a_hostile_global_config(tmp_path, monkeypatch):
    cfg, trace = tmp_path / "gitconfig", tmp_path / "trace.jsonl"
    cfg.write_text(HOSTILE)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = make_source(tmp_path / "r")
    for j in range(400):
        (root / f"f{j}.txt").write_text(str(j))
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    TV._init(root)
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    argvs = [e["argv"] for e in events if e.get("event") == "start"]
    assert any("commit" in a for a in argvs), "trace saw no commit: the check inspected nothing"
    assert not any("maintenance" in a or "gc" in a for a in argvs), argvs
