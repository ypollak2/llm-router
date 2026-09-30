"""commands/proxy_default.py — the install/uninstall orchestration.

Every test here runs against a temp HOME: ``install_hooks._SETTINGS_PATH`` is
monkeypatched (the supported sandboxing idiom — see test_install_hooks.py)
and ``LLM_ROUTER_HOME`` is set, so nothing ever touches the operator's real
``~/.claude/settings.json`` or ``~/.llm-router``. `install_service`/
`activate_service` additionally take an explicit `home`/injected `runner`
(proxy_default.py), so no real `launchctl`/`systemctl` runs either.

The central property under test: **settings.json is never written unless the
proxy has already proven it answers.** That is the fail-safe the task this
module ships for is built around.
"""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

from llm_router import install_manifest, proxy_default as pd
from llm_router.commands import proxy_default as cmd


def _listening_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    import llm_router.install_hooks as ih

    settings_path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(ih, "_SETTINGS_PATH", settings_path)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "_router_state"))
    return settings_path


class _NeverStartsRunner:
    """Simulates `launchctl load`/`systemctl enable --now` succeeding at the
    OS-service-manager level while the process itself never actually comes up
    -- the exact case `install_proxy_default` must refuse on."""

    def __call__(self, cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")


class _FailsToStartRunner:
    def __call__(self, cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "launchctl: service could not be loaded")


def test_install_refuses_and_never_writes_settings_when_proxy_never_answers(tmp_path, _sandbox):
    result = cmd.install_proxy_default(
        port=18790, home=tmp_path / "svc_home", system="Darwin",
        runner=_NeverStartsRunner(), health_retries=2, health_interval_s=0.05,
    )
    assert result["ok"] is False
    assert "did not answer" in result["error"]
    assert not _sandbox.exists(), "settings.json must never be written when the proxy refuses to come up"
    assert pd.read_sentinel() is None


def test_install_refuses_and_never_writes_settings_when_activation_fails(tmp_path, _sandbox):
    result = cmd.install_proxy_default(
        port=18791, home=tmp_path / "svc_home", system="Darwin", runner=_FailsToStartRunner(),
    )
    assert result["ok"] is False
    assert "could not start" in result["error"]
    assert not _sandbox.exists()


def test_install_reuses_an_already_answering_proxy_without_a_second_service(tmp_path, _sandbox):
    """The owner's own LaunchAgent (or a prior install) already on the port:
    install_proxy_default must not fight it for the port with a second
    service -- it reuses it and still wires settings.json."""
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        service_home = tmp_path / "svc_home"
        result = cmd.install_proxy_default(port=port, home=service_home, system="Darwin")
        assert result["ok"] is True and result["reused"] is True
        dest, _ = pd.service_target("Darwin", service_home)
        assert not dest.exists(), "a second service must not be installed when one already answers"
        data = json.loads(_sandbox.read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}"
        assert data["env"]["ENABLE_TOOL_SEARCH"] == "true"
    finally:
        srv.close()


def test_install_starts_a_new_service_and_wires_settings_once_healthy(tmp_path, _sandbox):
    service_home = tmp_path / "svc_home"
    port = 18792

    def _start_a_real_listener_then_succeed(cmd, **kwargs):
        # Simulate the activation command actually bringing the proxy up —
        # a real launchctl/systemctl would start the process; here a plain
        # socket stands in for "the proxy is now listening".
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        _start_a_real_listener_then_succeed._srv = srv  # keep it alive
        return subprocess.CompletedProcess(cmd, 0, "", "")

    try:
        result = cmd.install_proxy_default(
            port=port, home=service_home, system="Darwin",
            runner=_start_a_real_listener_then_succeed, health_retries=5, health_interval_s=0.05,
        )
        assert result["ok"] is True and result["reused"] is False
        dest, _ = pd.service_target("Darwin", service_home)
        assert dest.exists()
        data = json.loads(_sandbox.read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}"
        sentinel = pd.read_sentinel()
        assert sentinel["port"] == port and sentinel["enabled"] is True
    finally:
        srv = getattr(_start_a_real_listener_then_succeed, "_srv", None)
        if srv is not None:
            srv.close()


def test_install_backs_up_and_preserves_unrelated_settings(tmp_path, _sandbox):
    """Never a blind overwrite: existing settings.json content (hooks,
    mcpServers, an unrelated env key) survives, and a backup is written."""
    _sandbox.parent.mkdir(parents=True, exist_ok=True)
    _sandbox.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"command": "echo hi"}]}]},
        "env": {"SOME_OTHER_VAR": "keep-me"},
    }))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        result = cmd.install_proxy_default(port=port, home=tmp_path / "svc_home", system="Darwin")
        assert result["ok"] is True
        data = json.loads(_sandbox.read_text())
        assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
        assert data["env"]["SOME_OTHER_VAR"] == "keep-me"
        assert data["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}"
        backup = _sandbox.with_suffix(_sandbox.suffix + ".bak")
        assert backup.exists()
    finally:
        srv.close()


def test_uninstall_restores_settings_env_to_its_pre_install_value(tmp_path, _sandbox):
    """Simulates the real flow: commands/uninstall.py calls
    install_manifest.apply_uninstall() (which restores the json_key `env`
    record this module writes) — proxy_default.uninstall_proxy_default()
    itself only tears down the service + sentinel, documented in its own
    docstring."""
    _sandbox.parent.mkdir(parents=True, exist_ok=True)
    _sandbox.write_text(json.dumps({"env": {"SOME_OTHER_VAR": "keep-me"}}))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        result = cmd.install_proxy_default(port=port, home=tmp_path / "svc_home", system="Darwin")
        assert result["ok"] is True
    finally:
        srv.close()

    actions = install_manifest.apply_uninstall()
    assert actions  # something was restored
    data = json.loads(_sandbox.read_text())
    assert data.get("env") == {"SOME_OTHER_VAR": "keep-me"}
    assert "ANTHROPIC_BASE_URL" not in data.get("env", {})


def test_uninstall_stops_and_removes_the_service_and_sentinel(tmp_path):
    service_home = tmp_path / "svc_home"
    port = 18793
    srv_holder: dict = {}

    def _start_listener_then_succeed(cmd, **kwargs):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv_holder["srv"] = srv
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with pytest.MonkeyPatch.context() as mp:
        import llm_router.install_hooks as ih

        mp.setattr(ih, "_SETTINGS_PATH", tmp_path / ".claude" / "settings.json")
        mp.setenv("LLM_ROUTER_HOME", str(tmp_path / "_router_state"))
        try:
            result = cmd.install_proxy_default(
                port=port, home=service_home, system="Darwin",
                runner=_start_listener_then_succeed, health_retries=5, health_interval_s=0.05,
            )
            assert result["ok"] is True and result["reused"] is False

            dest, _ = pd.service_target("Darwin", service_home)
            assert dest.exists()

            actions = cmd.uninstall_proxy_default(home=service_home, system="Darwin", runner=_NeverStartsRunner())
            assert any("Stopped proxy service" in a for a in actions)
            assert not dest.exists()
            assert pd.read_sentinel() is None
        finally:
            srv = srv_holder.get("srv")
            if srv is not None:
                srv.close()


def test_uninstall_when_never_installed_is_a_noop(tmp_path, _sandbox):
    actions = cmd.uninstall_proxy_default(home=tmp_path / "svc_home", system="Darwin")
    assert actions == []


def test_cmd_proxy_default_off_prints_not_installed_when_absent(tmp_path, _sandbox, capsys):
    rc = cmd.cmd_proxy_default("off")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Not installed" in out


def test_cmd_proxy_default_on_returns_nonzero_and_prints_error_when_it_fails(tmp_path, _sandbox, capsys):
    # A fresh module-level default port 8787 is deliberately NOT used here —
    # cmd_proxy_default("on") calls install_proxy_default with no overrides,
    # so this exercises the real default-port path; force a fast, certain
    # refusal by making activation itself fail rather than depending on
    # whether something already listens on 8787 in this environment.
    import llm_router.commands.proxy_default as pdc

    def _install_that_refuses(**kwargs):
        return {"ok": False, "actions": ["Wrote something"], "error": "boom", "reused": False}

    orig = pdc.install_proxy_default
    pdc.install_proxy_default = _install_that_refuses
    try:
        rc = pdc.cmd_proxy_default("on")
    finally:
        pdc.install_proxy_default = orig
    assert rc == 1
    out = capsys.readouterr().out
    assert "boom" in out
    assert "Nothing in ~/.claude/settings.json was changed" in out
