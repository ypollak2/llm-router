"""P2.12 task 4 / NFR-UPG: a 15.4.0 HOME upgraded keeps its state; every host's hooks move together."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from llm_router import install_hooks as ih
from llm_router import seats
from llm_router.commands import install
from tests.fixtures import home_15_4_0 as fx


_SRC_OF = {"codex-auto-route.py": "auto-route.py"}  # installed under a host-specific name


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    state = home / ".llm-router"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.chdir(home)
    fx.build(home)
    before = {n: _sha(state / n) for n in fx.PRESERVED}
    stale = {n: (state / "claude-home" / "hooks" / n).read_text() for _s, n, _e, _t in ih._HOOK_DEFS}
    monkeypatch.setattr(seats, "refresh_seats", lambda *a, **k: (_ for _ in ()).throw(OSError("no probe")))
    from llm_router import host_detect
    detect = host_detect.detect_hosts  # wired hosts must not depend on the developer's PATH
    monkeypatch.setattr(host_detect, "detect_hosts",
                        lambda **k: detect(**{"home": home, "which": lambda b: None, **k}))
    wired = install.run_full(interactive=False)
    return home, state, before, stale, wired


def test_state_survives_the_upgrade(upgraded):
    _home, state, before, _stale, _wired = upgraded
    assert len(before) >= 5
    for name, digest in before.items():
        assert _sha(state / name) == digest, name
    con = sqlite3.connect(state / "usage.db")
    assert con.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == fx.USAGE_ROWS
    con.close()


def test_every_wired_host_is_at_the_new_hook_version(upgraded):
    home, state, _before, stale, wired = upgraded
    assert {"Claude Code", "Codex CLI", "Gemini CLI"} <= set(wired)
    assert all(fx.OLD_HOOK == v for v in stale.values()) and stale  # premise: all were old
    compared = 0
    for src_name, dst_name, _e, _t in ih._HOOK_DEFS:
        src, dst = ih._HOOKS_SRC / src_name, state / "claude-home" / "hooks" / dst_name
        assert ih._hook_version(dst) == ih._hook_version(src), dst_name
        assert dst.read_text() != fx.OLD_HOOK, dst_name  # the stale body is gone
        compared += 1
    for name in ("codex-auto-route.py", "codex-post-tool.py", "codex-stop.py",
                 "gemini-cli-auto-route.py", "gemini-cli-post-tool.py", "gemini-cli-session-end.py"):
        dst = state / "hooks" / name
        assert ih._hook_version(dst) == ih._hook_version(ih._HOOKS_SRC / _SRC_OF.get(name, name)), name
        assert dst.read_text() != fx.OLD_HOOK, name
        compared += 1
    assert compared >= 20, compared
    print(f"upgrade: {compared} hook files at the packaged version")
    rows = wired.receipt["hosts"]  # P212-1: the receipt agrees with the files on disk
    for host in ("claude-code", "codex", "gemini-cli"):
        bundled = {src: ih._hook_version(ih._HOOKS_SRC / src) for src in install._receipt_hook_files(host)}
        assert rows[host]["status"] == "wired" and rows[host]["hook_version"] == bundled and bundled, host
    assert install.read_receipt() == wired.receipt


def test_user_keys_in_host_configs_survive(upgraded):
    home, state, *_ = upgraded
    assert json.loads((state / "claude-home" / "settings.json").read_text())["theme"] == "dark-mine"
    assert json.loads((home / ".gemini" / "settings.json").read_text())["theme"] == "gemini-mine"
    assert 'model = "mine"' in (home / ".codex" / "config.toml").read_text()
