"""Hermetic tests for `llm_router gc` — sweep stale session shards, never protected files."""
import os
import time
from pathlib import Path

import pytest

from llm_router.commands.gc import main as gc_main, SHARD_PREFIXES

PROTECTED = ["usage.db", "usage.db-wal", "admin_actions.db", "config.yaml", ".env", "agent_calls.json"]


@pytest.fixture
def shard_dir(tmp_path):
    old = time.time() - 30 * 86400
    for name in PROTECTED:
        (tmp_path / name).write_text("keep")
    stale = ["last_route_a.json", "tool_history_b.json", "violations_c.json", "routing.json.bak2"]
    fresh = ["turn_blocks_d.json", "agent_depth_e.json", "last_classification_f.json"]
    for name in stale:
        p = tmp_path / name
        p.write_text("x")
        os.utime(p, (old, old))
    for name in fresh:
        (tmp_path / name).write_text("x")
    return tmp_path, set(stale), set(fresh)


def _names(d: Path):
    return {p.name for p in d.iterdir()}


def test_dry_run_removes_nothing(shard_dir, capsys):
    d, stale, fresh = shard_dir
    rc = gc_main(["--root", str(d), "--ttl-days", "7"])
    assert rc == 0
    assert stale | fresh | set(PROTECTED) <= _names(d)
    out = capsys.readouterr().out
    assert "dry run" in out
    for name in stale:
        assert f"would remove: {name}" in out


def test_apply_removes_only_stale_shards(shard_dir):
    d, stale, fresh = shard_dir
    rc = gc_main(["--root", str(d), "--ttl-days", "7", "--apply"])
    assert rc == 0
    remaining = _names(d)
    assert not (stale & remaining), "stale shards must be deleted"
    assert fresh <= remaining, "fresh shards inside TTL must survive"
    assert set(PROTECTED) <= remaining, "protected files must never be touched"


def test_protected_files_survive_even_if_stale(shard_dir):
    d, _, _ = shard_dir
    old = time.time() - 90 * 86400
    for name in PROTECTED:
        os.utime(d / name, (old, old))
    gc_main(["--root", str(d), "--ttl-days", "1", "--apply"])
    assert set(PROTECTED) <= _names(d)


def test_missing_root_is_noop(tmp_path):
    rc = gc_main(["--root", str(tmp_path / "nope"), "--apply"])
    assert rc == 0


def test_shard_prefixes_do_not_match_protected():
    for name in PROTECTED:
        assert not any(name.startswith(pfx) for pfx in SHARD_PREFIXES)
