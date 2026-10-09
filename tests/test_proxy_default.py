"""llm_router.proxy_default — service rendering, health probe, sentinel.

Covers the pure/file-local building blocks `commands/proxy_default.py`
orchestrates. Never invokes a real `launchctl`/`systemctl` (activate/
deactivate take an injectable `runner`) and never touches the real
`~/Library/LaunchAgents` (`home`/`system` are explicit parameters throughout
-- see the module docstring's comparison with gateway_service.py for why that
matters here specifically).
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from llm_router import proxy_default as pd


def _listening_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


# ── rendering ────────────────────────────────────────────────────────────

def test_launchd_plist_uses_given_paths_not_hardcoded():
    out = pd.render_launchd_plist(
        "/opt/venv/bin/python", Path("/home/alice"), port=8787, steps="off", tiers="conversation",
    )
    assert "/opt/venv/bin/python" in out
    assert f"<string>{pd.LABEL}</string>" in out
    assert "<string>--port</string><string>8787</string>" in out
    assert "<string>--steps</string><string>off</string>" in out
    assert "<string>--tiers</string><string>conversation</string>" in out
    assert "<key>KeepAlive</key><true/>" in out
    assert "/home/alice/.llm-router/logs/proxy.out.log" in out
    assert "yaliandrona" not in out and "yali.pollak" not in out


def test_systemd_user_unit_has_restart_on_failure():
    out = pd.render_systemd_user_unit("/opt/venv/bin/python", port=8787, steps="off", tiers="conversation")
    assert "ExecStart=/opt/venv/bin/python -m llm_router.cli proxy --port 8787 --steps off --tiers conversation" in out
    assert "Restart=on-failure" in out
    assert "WantedBy=default.target" in out


def test_service_target_per_platform(tmp_path):
    mac_dest, mac_cmd = pd.service_target("Darwin", tmp_path)
    assert mac_dest == tmp_path / "Library" / "LaunchAgents" / f"{pd.LABEL}.plist"
    assert "launchctl load" in mac_cmd and "launchctl kickstart -k" in mac_cmd

    lin_dest, lin_cmd = pd.service_target("Linux", tmp_path)
    assert lin_dest.name == "llm_router-proxy.service" and "systemd/user" in str(lin_dest)
    assert "systemctl --user" in lin_cmd

    with pytest.raises(RuntimeError):
        pd.service_target("Windows", tmp_path)


def test_install_service_write_false_does_not_touch_disk(tmp_path):
    dest, activate = pd.install_service(
        python="/opt/venv/bin/python", system="Darwin", home=tmp_path, write=False,
    )
    assert not dest.exists()
    dest2, _ = pd.install_service(python="/opt/venv/bin/python", system="Darwin", home=tmp_path, write=False)
    assert dest2 == dest
    assert not dest.exists()


def test_install_service_writes_under_the_given_home_only(tmp_path):
    dest, _ = pd.install_service(python="/opt/venv/bin/python", system="Darwin", home=tmp_path, port=9999)
    assert dest.exists()
    assert str(tmp_path) in str(dest)
    assert "9999" in dest.read_text()


# ── health ───────────────────────────────────────────────────────────────

def test_proxy_health_true_when_listening():
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        assert pd.proxy_health("127.0.0.1", port, timeout=0.3) is True
    finally:
        srv.close()


def test_proxy_health_false_when_nothing_listening():
    srv = _listening_socket()
    port = srv.getsockname()[1]
    srv.close()
    assert pd.proxy_health("127.0.0.1", port, timeout=0.3) is False


# ── activation / deactivation (never a real launchctl/systemctl) ──────────

class _FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[str] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def test_activate_service_success(tmp_path):
    dest, cmd = pd.service_target("Darwin", tmp_path)
    runner = _FakeRunner(returncode=0)
    ok, detail = pd.activate_service(dest, cmd, runner=runner)
    assert ok is True
    assert runner.calls == [cmd]


def test_activate_service_failure_reports_detail(tmp_path):
    dest, cmd = pd.service_target("Darwin", tmp_path)
    runner = _FakeRunner(returncode=1, stderr="boom")
    ok, detail = pd.activate_service(dest, cmd, runner=runner)
    assert ok is False and "boom" in detail


def test_activate_service_runner_exception_never_raises(tmp_path):
    dest, cmd = pd.service_target("Darwin", tmp_path)

    def _boom(*a, **kw):
        raise OSError("no launchctl")

    ok, detail = pd.activate_service(dest, cmd, runner=_boom)
    assert ok is False and "no launchctl" in detail


def test_deactivate_service_noop_when_plist_never_written(tmp_path):
    """Darwin: `launchctl unload` is skipped (returns ok) when the plist file
    doesn't exist -- there is nothing to unload, and running the command
    anyway would just report its own confusing error."""
    dest, _ = pd.service_target("Darwin", tmp_path)
    runner = _FakeRunner()
    ok, detail = pd.deactivate_service("Darwin", dest, runner=runner)
    assert ok is True
    assert runner.calls == []


def test_deactivate_service_unloads_when_plist_present(tmp_path):
    dest, _ = pd.install_service(python="/x/python", system="Darwin", home=tmp_path)
    assert dest.exists()
    runner = _FakeRunner(returncode=0)
    ok, detail = pd.deactivate_service("Darwin", dest, runner=runner)
    assert ok is True and len(runner.calls) == 1 and "launchctl unload" in runner.calls[0]


def test_deactivate_service_linux_always_runs_systemctl_disable(tmp_path):
    dest, _ = pd.service_target("Linux", tmp_path)
    runner = _FakeRunner(returncode=0)
    ok, _ = pd.deactivate_service("Linux", dest, runner=runner)
    assert ok is True
    assert "systemctl --user disable --now" in runner.calls[0]


# ── sentinel ─────────────────────────────────────────────────────────────

def test_sentinel_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert pd.read_sentinel() is None
    pd.write_sentinel(port=8787, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin")
    data = pd.read_sentinel()
    assert data["port"] == 8787 and data["tiers"] == "conversation" and data["enabled"] is True
    pd.remove_sentinel()
    assert pd.read_sentinel() is None


def test_remove_sentinel_when_absent_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    pd.remove_sentinel()  # must not raise


# ── P0.10: the fail-open shim's service files ────────────────────────────────

def test_shim_launchd_plist_is_supervised_and_points_at_the_upstream_port():
    out = pd.render_launchd_shim_plist("/opt/venv/bin/python", Path("/home/alice"),
                                       port=8787, upstream_port=8797)
    assert "<key>Label</key><string>com.llm_router.proxy-shim</string>" in out
    assert "<string>proxy-shim</string>" in out
    assert "<string>--port</string><string>8787</string>" in out
    assert "<string>--upstream-port</string><string>8797</string>" in out
    assert "<key>KeepAlive</key><true/>" in out and "<key>RunAtLoad</key><true/>" in out
    assert "/home/alice/.llm-router/logs/proxy-shim.err.log" in out


def test_shim_systemd_unit_restarts_on_failure():
    out = pd.render_systemd_shim_unit("/opt/venv/bin/python", port=8787, upstream_port=8797)
    assert "ExecStart=/opt/venv/bin/python -m llm_router.cli proxy-shim --port 8787 --upstream-port 8797" in out
    assert "Restart=on-failure" in out


def test_shim_service_target_per_platform(tmp_path):
    mac_dest, mac_cmd = pd.service_target("Darwin", tmp_path, label=pd.SHIM_LABEL)
    assert mac_dest == tmp_path / "Library" / "LaunchAgents" / "com.llm_router.proxy-shim.plist"
    lin_dest, lin_cmd = pd.service_target("Linux", tmp_path, label=pd.SHIM_LABEL)
    assert lin_dest.name == "llm_router-proxy-shim.service"
    assert lin_cmd.endswith("enable --now llm_router-proxy-shim")
    assert pd.deactivation_command("Linux", lin_dest, pd.SHIM_LABEL) == \
        "systemctl --user disable --now llm_router-proxy-shim"


def test_default_ports_put_the_shim_on_the_settings_port():
    assert pd.DEFAULT_PORT == 8787 and pd.DEFAULT_UPSTREAM_PORT == 8797
    from llm_router.proxy import failopen_shim

    assert failopen_shim.DEFAULT_PORT == pd.DEFAULT_PORT
    assert failopen_shim.DEFAULT_UPSTREAM_PORT == pd.DEFAULT_UPSTREAM_PORT


def test_install_shim_service_writes_under_the_given_home_only(tmp_path):
    dest, _ = pd.install_shim_service(python="/x/python", system="Darwin", home=tmp_path,
                                      port=9001, upstream_port=9002)
    assert dest.is_relative_to(tmp_path) and dest.exists()
    assert "<string>9002</string>" in dest.read_text()


_FAKE_LAUNCHCTL = """#!/bin/bash
# Reproduces macOS 26: `load` of a loaded job prints "Load failed: 5" and EXITS 0.
state="$FAKE_LC_DIR/loaded"; echo "$1" >> "$FAKE_LC_DIR/calls"
case "$1" in
  print) [ -e "$state" ] && exit 0; exit 113;;
  load) if [ -e "$state" ]; then echo "Load failed: 5: Input/output error" >&2; exit 0; fi
        touch "$state"; exit 0;;
  kickstart) [ -e "$state" ] && exit 0; exit 113;;
  *) exit 64;;
esac
"""


def _run_activation(tmp_path, cmd, loaded):
    import os
    import subprocess as sp

    bin_dir, st = tmp_path / "bin", tmp_path / "lc"
    bin_dir.mkdir(exist_ok=True)
    st.mkdir(exist_ok=True)
    lc = bin_dir / "launchctl"
    lc.write_text(_FAKE_LAUNCHCTL)
    lc.chmod(0o755)
    (st / "calls").write_text("")
    if loaded:
        (st / "loaded").write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LC_DIR": str(st)}
    r = sp.run(cmd, shell=True, env=env, capture_output=True, text=True, timeout=15)
    return r, (st / "calls").read_text().split()


def test_macos_activation_kickstarts_a_loaded_service(tmp_path):
    """`launchctl load` on a loaded job exits 0, so `load || kickstart` never kicks."""
    for label in (pd.LABEL, pd.SHIM_LABEL):
        dest, cmd = pd.service_target("Darwin", tmp_path, label=label)
        assert "bootout" not in cmd and "bootstrap" not in cmd
        r, calls = _run_activation(tmp_path, cmd, loaded=True)
        assert r.returncode == 0
        assert calls == ["print", "kickstart"], calls


def test_macos_activation_loads_an_unloaded_service(tmp_path):
    for label in (pd.LABEL, pd.SHIM_LABEL):
        dest, cmd = pd.service_target("Darwin", tmp_path, label=label)
        (tmp_path / label).mkdir()
        r, calls = _run_activation(tmp_path / label, cmd, loaded=False)
        assert r.returncode == 0
        assert calls == ["print", "load"], calls


def test_gateway_activation_shares_the_state_check(tmp_path, monkeypatch):
    from llm_router import gateway_service as gs
    monkeypatch.setenv("HOME", str(tmp_path))
    _, cmd = gs.gateway_service_target("Darwin")
    r, calls = _run_activation(tmp_path, cmd, loaded=True)
    assert calls == ["print", "kickstart"], calls


def test_no_restart_advice_uses_launchctl_bootout_or_bootstrap():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = [f for d in ("src", "hooks", "docs") for f in (root / d).rglob("*")
             if f.suffix in {".py", ".md"} and "spikes" not in f.parts and f.name != "BUGS.md"
             and "bugs" not in f.relative_to(root).parts]
    assert len(files) > 50, "the scan must find the repo's files"
    pat = re.compile(r"launchctl\s+(bootout|bootstrap)")
    hits = [f"{f.relative_to(root)}:{n}" for f in files
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1) if pat.search(line)]
    assert hits == []
