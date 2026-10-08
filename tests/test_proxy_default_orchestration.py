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
        port=18790, shim=False, home=tmp_path / "svc_home", system="Darwin",
        runner=_NeverStartsRunner(), health_retries=2, health_interval_s=0.05,
    )
    assert result["ok"] is False
    assert "did not answer" in result["error"]
    assert not _sandbox.exists(), "settings.json must never be written when the proxy refuses to come up"
    assert pd.read_sentinel() is None


def test_install_refuses_and_never_writes_settings_when_activation_fails(tmp_path, _sandbox):
    result = cmd.install_proxy_default(
        port=18791, shim=False, home=tmp_path / "svc_home", system="Darwin", runner=_FailsToStartRunner(),
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
            port=port, shim=False, home=service_home, system="Darwin",
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
                port=port, shim=False, home=service_home, system="Darwin",
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


# ── P0.10 (D-17 = A): the default install puts the fail-open shim on `port` ──
#
# The pre-P0.10 tests above pass `shim=False` explicitly: they pin the
# settings.json gate, which must hold in both layouts. The tests below pin the
# new default layout: main proxy on `upstream_port`, shim on `port`, and
# settings.json naming the shim's port only after BOTH answer.

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _BindsPerService:
    """Stands in for `launchctl load <plist>`: binds the port of whichever
    service the command names, so each health poll sees exactly what a real
    service manager would have started. `start` limits which labels come up."""

    def __init__(self, ports: dict[str, int], start: set[str]):
        self.ports, self.start, self.socks, self.calls = ports, start, [], []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        for label, port in self.ports.items():
            if f"{label}.plist" in cmd and label in self.start:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", port))
                srv.listen(128)
                self.socks.append(srv)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def close(self):
        for s in self.socks:
            s.close()


def test_default_install_starts_main_on_upstream_port_and_shim_on_settings_port(tmp_path, _sandbox):
    service_home = tmp_path / "svc_home"
    port, upstream = _free_port(), _free_port()
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL, pd.SHIM_LABEL})
    try:
        result = cmd.install_proxy_default(
            port=port, upstream_port=upstream, home=service_home, system="Darwin",
            runner=runner, health_retries=5, health_interval_s=0.05,
        )
        assert result["ok"] is True, result
        main_dest, _ = pd.service_target("Darwin", service_home)
        shim_dest, _ = pd.service_target("Darwin", service_home, label=pd.SHIM_LABEL)
        assert f"<string>--port</string><string>{upstream}</string>" in main_dest.read_text()
        shim_text = shim_dest.read_text()
        assert "<string>proxy-shim</string>" in shim_text
        assert f"<string>--port</string><string>{port}</string>" in shim_text
        assert f"<string>--upstream-port</string><string>{upstream}</string>" in shim_text
        # main first, then the shim: the shim must never come up pointing at nothing
        assert len(runner.calls) == 2
        assert f"launchctl load {main_dest}" in runner.calls[0]
        assert f"launchctl load {shim_dest}" in runner.calls[1]
        data = json.loads(_sandbox.read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}"
        sentinel = pd.read_sentinel()
        assert sentinel["port"] == port and sentinel["upstream_port"] == upstream
        assert sentinel["shim_label"] == pd.SHIM_LABEL
    finally:
        runner.close()


def test_default_install_refuses_when_the_shim_never_answers(tmp_path, _sandbox):
    port, upstream = _free_port(), _free_port()
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL})
    try:
        result = cmd.install_proxy_default(
            port=port, upstream_port=upstream, home=tmp_path / "svc_home", system="Darwin",
            runner=runner, health_retries=2, health_interval_s=0.05,
        )
        assert result["ok"] is False
        assert "fail-open shim" in result["error"] and "did not answer" in result["error"]
        assert not _sandbox.exists(), "settings.json must not name a port nothing answers on"
        assert pd.read_sentinel() is None
    finally:
        runner.close()


def test_default_install_refuses_when_the_main_proxy_never_answers(tmp_path, _sandbox):
    port, upstream = _free_port(), _free_port()
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.SHIM_LABEL})
    try:
        result = cmd.install_proxy_default(
            port=port, upstream_port=upstream, home=tmp_path / "svc_home", system="Darwin",
            runner=runner, health_retries=2, health_interval_s=0.05,
        )
        assert result["ok"] is False and "proxy service" in result["error"]
        shim_dest, _ = pd.service_target("Darwin", tmp_path / "svc_home", label=pd.SHIM_LABEL)
        assert not shim_dest.exists(), "the shim is installed only after the main proxy answers"
        assert not _sandbox.exists()
    finally:
        runner.close()


def test_reuse_of_an_occupied_port_says_no_shim_was_installed(tmp_path, _sandbox):
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        result = cmd.install_proxy_default(port=port, upstream_port=_free_port(),
                                           home=tmp_path / "svc_home", system="Darwin")
        assert result["ok"] is True and result["reused"] is True
        assert any("No fail-open shim installed" in a for a in result["actions"])
        shim_dest, _ = pd.service_target("Darwin", tmp_path / "svc_home", label=pd.SHIM_LABEL)
        assert not shim_dest.exists()
        assert pd.read_sentinel()["shim_label"] is None
    finally:
        srv.close()


def test_uninstall_stops_and_removes_both_services(tmp_path, _sandbox):
    service_home = tmp_path / "svc_home"
    port, upstream = _free_port(), _free_port()
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL, pd.SHIM_LABEL})
    try:
        result = cmd.install_proxy_default(
            port=port, upstream_port=upstream, home=service_home, system="Darwin",
            runner=runner, health_retries=5, health_interval_s=0.05,
        )
        assert result["ok"] is True
    finally:
        runner.close()
    main_dest, _ = pd.service_target("Darwin", service_home)
    shim_dest, _ = pd.service_target("Darwin", service_home, label=pd.SHIM_LABEL)
    stops = []

    def _record(cmd_, **kw):
        stops.append(cmd_)
        return subprocess.CompletedProcess(cmd_, 0, "", "")

    actions = cmd.uninstall_proxy_default(home=service_home, system="Darwin", runner=_record)
    assert stops == [f"launchctl unload {shim_dest}", f"launchctl unload {main_dest}"]
    assert any("Stopped fail-open shim service" in a for a in actions)
    assert any("Stopped proxy service" in a for a in actions)
    assert not main_dest.exists() and not shim_dest.exists()
    assert pd.read_sentinel() is None


def _install_both(service_home, port, upstream, runner):
    return cmd.install_proxy_default(
        port=port, upstream_port=upstream, home=service_home, system="Darwin",
        runner=runner, health_retries=5, health_interval_s=0.05,
    )


def test_rerunning_install_on_a_finished_shim_install_keeps_the_shim_in_the_sentinel(tmp_path, _sandbox):
    """Before the fix the re-run saw its own shim on the port, took the "reuse a
    foreign proxy" branch and rewrote the sentinel with shim_label None, so
    uninstall left the shim plist and a running shim behind."""
    service_home = tmp_path / "svc_home"
    port, upstream = _free_port(), _free_port()
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL, pd.SHIM_LABEL})
    try:
        assert _install_both(service_home, port, upstream, runner)["ok"] is True
        before = list(runner.calls)
        again = _install_both(service_home, port, upstream, runner)
        assert again["ok"] is True and again["reused"] is True, again
        assert any("Already installed" in a for a in again["actions"])
        assert runner.calls == before, "a finished install is not restarted"
        sentinel = pd.read_sentinel()
        assert sentinel["shim_label"] == pd.SHIM_LABEL and sentinel["upstream_port"] == upstream
        assert json.loads(_sandbox.read_text())["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}"
    finally:
        runner.close()
    shim_dest, _ = pd.service_target("Darwin", service_home, label=pd.SHIM_LABEL)
    cmd.uninstall_proxy_default(home=service_home, system="Darwin", runner=lambda c, **k: subprocess.CompletedProcess(c, 0, "", ""))
    assert not shim_dest.exists()


def test_uninstall_removes_a_shim_plist_that_no_sentinel_names(tmp_path, _sandbox):
    service_home = tmp_path / "svc_home"
    shim_dest, _ = pd.install_shim_service(system="Darwin", home=service_home, port=8787, upstream_port=8797)
    assert shim_dest.exists() and pd.read_sentinel() is None
    stops = []
    actions = cmd.uninstall_proxy_default(
        home=service_home, system="Darwin",
        runner=lambda c, **k: stops.append(c) or subprocess.CompletedProcess(c, 0, "", ""),
    )
    assert stops == [f"launchctl unload {shim_dest}"]
    assert not shim_dest.exists()
    assert any("Stopped fail-open shim service" in a for a in actions)
    assert not any("sentinel" in a for a in actions), "no sentinel existed, none is removed"


def test_uninstall_with_nothing_installed_does_nothing(tmp_path, _sandbox):
    assert cmd.uninstall_proxy_default(home=tmp_path / "svc_home", system="Darwin",
                                       runner=lambda c, **k: pytest.fail("ran " + c)) == []


def _seed_sentinel(port, upstream, shim_label=pd.SHIM_LABEL):
    pd.write_sentinel(port=port, steps=pd.DEFAULT_STEPS, tiers=pd.DEFAULT_TIERS, label=pd.LABEL,
                      system="Darwin", upstream_port=upstream, shim_label=shim_label)


def _listener(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(128)
    return s


def test_already_installed_needs_both_ports_healthy(tmp_path, _sandbox):
    port, upstream = _free_port(), _free_port()
    _seed_sentinel(port, upstream)
    shim_up = _listener(port)  # main proxy is down
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL})
    try:
        r = _install_both(tmp_path / "svc", port, upstream, runner)
        assert not any("Already installed" in a for a in r["actions"]), r
        assert not any("Found a proxy already answering" in a for a in r["actions"]), r
        assert any(pd.LABEL in c for c in runner.calls), "the dead main proxy is restarted"
    finally:
        runner.close()
        shim_up.close()


def test_own_layout_requires_matching_sentinel_label_and_upstream_port(tmp_path, _sandbox):
    port, upstream = _free_port(), _free_port()
    for label, up in ((None, upstream), (pd.SHIM_LABEL, upstream + 1)):
        _seed_sentinel(port, up, shim_label=label)
        a, b = _listener(port), _listener(upstream)
        try:
            r = _install_both(tmp_path / "svc", port, upstream, lambda c, **k: subprocess.CompletedProcess(c, 0, "", ""))
            assert not any("Already installed" in x for x in r["actions"]), (label, up, r)
            assert any("Found a proxy already answering" in x for x in r["actions"]), (label, up, r)
        finally:
            a.close()
            b.close()


def test_changed_plist_is_reported_not_silently_restarted(tmp_path, _sandbox):
    service_home = tmp_path / "svc"
    main_dest, _ = pd.service_target("Darwin", service_home)
    main_dest.parent.mkdir(parents=True)
    main_dest.write_text("<plist>hand edited, port 8787</plist>")
    port, upstream = _free_port(), _free_port()
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL, pd.SHIM_LABEL})
    try:
        r = _install_both(service_home, port, upstream, runner)
        notes = [a for a in r["actions"] if a.startswith("NOTE ")]
        assert len(notes) == 1 and pd.LABEL in notes[0] and "launchctl unload" in notes[0], r["actions"]
        assert "docs/proxy.md" in notes[0]
        assert not any(c.startswith("launchctl unload") for c in runner.calls), "never unloads by itself"
    finally:
        runner.close()


def test_unchanged_plist_gets_no_note(tmp_path, _sandbox):
    service_home = tmp_path / "svc"
    port, upstream = _free_port(), _free_port()
    pd.install_service(system="Darwin", home=service_home, port=upstream)
    runner = _BindsPerService({pd.LABEL: upstream, pd.SHIM_LABEL: port}, {pd.LABEL, pd.SHIM_LABEL})
    try:
        r = _install_both(service_home, port, upstream, runner)
        assert not any(a.startswith("NOTE ") for a in r["actions"]), r["actions"]
    finally:
        runner.close()
