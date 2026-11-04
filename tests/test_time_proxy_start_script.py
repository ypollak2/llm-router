"""scripts/time_proxy_start.py -- the harness behind the PROXY-START-HARDEN numbers.

It starts a real proxy process, so these tests exercise only its guards and its tree builder; the timing run
itself is the script's own output (quoted in changelog.d/PROXY-START-HARDEN.fixed.md).
"""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "time_proxy_start.py"


def _load():
    spec = importlib.util.spec_from_file_location("time_proxy_start", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_tree_makes_exactly_the_requested_private_tree(tmp_path):
    mod = _load()
    root = tmp_path / ".llm-router"
    mod.build_tree(root, files=25, dirs=4)
    files = [p for p in root.rglob("*") if p.is_file()]
    dirs = [p for p in root.rglob("*") if p.is_dir()]
    assert len(files) == 25 and len(dirs) == 4, "an empty or short tree would time nothing"
    assert {stat.S_IMODE(p.stat().st_mode) for p in files} == {0o600}
    assert {stat.S_IMODE(p.stat().st_mode) for p in dirs} == {0o700}


def test_the_throwaway_home_must_be_under_the_system_temp_dir(tmp_path):
    mod = _load()
    assert mod.under_system_temp(tmp_path)
    # not Path.home(): the suite's conftest points HOME at a temp dir
    assert not mod.under_system_temp(Path("/usr/local"))
    assert not mod.under_system_temp(Path("/"))


def test_free_port_is_never_the_live_proxy_or_its_shim():
    mod = _load()
    assert mod.LIVE_PORTS == (8787, 8797)
    for _ in range(20):
        assert mod.free_port() not in mod.LIVE_PORTS


def test_the_run_environment_never_points_at_the_real_home(tmp_path, monkeypatch):
    """one_run builds its env from the throwaway home: HOME, LLM_ROUTER_HOME and CODEX_HOME all sit under it."""
    mod = _load()
    seen = {}

    class _Dead:
        pid = 0

        def poll(self):
            return 1  # exits at once: the loop in one_run ends without a connection

        def wait(self, timeout=None):
            return 1

    def fake_popen(cmd, **kw):
        seen["env"], seen["cwd"], seen["cmd"] = kw["env"], kw["cwd"], cmd
        return _Dead()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mod.os, "killpg", lambda *a: None)
    out = mod.one_run("python", tmp_path, http=False)
    assert out == {"listen_s": None, "first_http_s": None}
    home = str(tmp_path)
    assert seen["env"]["HOME"] == home
    assert seen["env"]["LLM_ROUTER_HOME"].startswith(home) and seen["env"]["CODEX_HOME"].startswith(home)
    port = int(seen["cmd"][seen["cmd"].index("--port") + 1])
    assert port not in mod.LIVE_PORTS
