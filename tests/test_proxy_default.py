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
    assert "launchctl load" in mac_cmd

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
