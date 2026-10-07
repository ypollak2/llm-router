"""session-start.py's `_check_proxy_default_health()` — warns at session
start when `llm-router install --proxy-default` is installed but the proxy
isn't answering.

Deliberately stdlib-only (a raw TCP connect, not
`llm_router.proxy_default.proxy_health`) so it degrades the same way the rest
of this hook does when `llm_router` itself fails to import — see the
function's own docstring. Run in-process via importlib, matching
tests/test_session_start_pxpipe.py's `_load_hook_module()` helper.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
from pathlib import Path


from llm_router import proxy_default as pd

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "session-start.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("session_start_hook_proxy_default", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _listening_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


def test_hook_label_literal_matches_proxy_default_label():
    """The hook hardcodes proxy_default.LABEL as a literal (stdlib-only, see
    its docstring) — this is the guard the docstring promises against the two
    drifting apart."""
    _load_hook_module()  # importable/parses cleanly
    src = HOOK_PATH.read_text()
    assert f'label = "{pd.LABEL}"' in src
    assert f'shim_label = "{pd.SHIM_LABEL}"' in src


def test_noop_when_never_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    mod = _load_hook_module()
    assert mod._check_proxy_default_health() == ""


def test_noop_when_answering(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        (tmp_path / "proxy_default.json").write_text(json.dumps({"port": port}))
        mod = _load_hook_module()
        assert mod._check_proxy_default_health() == ""
    finally:
        srv.close()


def test_warns_with_recovery_command_when_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    # A closed listening socket's port is very likely free again immediately,
    # which is enough for this test's purpose without asserting exclusivity.
    srv = _listening_socket()
    port = srv.getsockname()[1]
    srv.close()
    (tmp_path / "proxy_default.json").write_text(json.dumps({"port": port}))
    mod = _load_hook_module()
    msg = mod._check_proxy_default_health()
    assert f"127.0.0.1:{port}" in msg
    assert "install --proxy-default off" in msg
    assert pd.LABEL in msg


def test_malformed_sentinel_is_not_treated_as_a_dead_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    (tmp_path / "proxy_default.json").write_text("not valid json {{{")
    mod = _load_hook_module()
    assert mod._check_proxy_default_health() == ""


def test_defaults_to_the_documented_port_when_sentinel_omits_it(monkeypatch, tmp_path):
    """8787 (pd.DEFAULT_PORT) may genuinely be occupied by the owner's own
    live LaunchAgent on a dev machine, so this must not depend on real
    connectivity to it — `socket.create_connection` is stubbed to fail for
    every port, isolating the assertion to "what port did the hook check"."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    (tmp_path / "proxy_default.json").write_text(json.dumps({}))
    seen_ports = []

    def _fail(addr, timeout=None):
        seen_ports.append(addr[1])
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", _fail)
    mod = _load_hook_module()
    msg = mod._check_proxy_default_health()
    assert seen_ports == [pd.DEFAULT_PORT]
    assert f"127.0.0.1:{pd.DEFAULT_PORT}" in msg


def _dead_port() -> int:
    srv = _listening_socket()
    port = srv.getsockname()[1]
    srv.close()
    return port


def test_warns_routing_bypassed_when_shim_answers_but_main_proxy_is_dead(monkeypatch, tmp_path):
    """docs/BUGS.md #11 review: with the fail-open shim, `port` is the shim's and
    it always accepts. Probing it alone read healthy while the main proxy was
    dead and every call bypassed routing."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    shim = _listening_socket()
    try:
        port, upstream = shim.getsockname()[1], _dead_port()
        (tmp_path / "proxy_default.json").write_text(
            json.dumps({"port": port, "upstream_port": upstream, "shim_label": pd.SHIM_LABEL})
        )
        msg = _load_hook_module()._check_proxy_default_health()
        assert f"127.0.0.1:{upstream}" in msg
        assert "bypassed" in msg
        assert f"gui/$(id -u)/{pd.LABEL}" in msg
    finally:
        shim.close()


def test_noop_when_shim_and_main_proxy_both_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    shim, main = _listening_socket(), _listening_socket()
    try:
        (tmp_path / "proxy_default.json").write_text(json.dumps(
            {"port": shim.getsockname()[1], "upstream_port": main.getsockname()[1]}
        ))
        assert _load_hook_module()._check_proxy_default_health() == ""
    finally:
        shim.close()
        main.close()


def test_dead_shim_names_the_shim_service(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    main = _listening_socket()
    try:
        port = _dead_port()
        (tmp_path / "proxy_default.json").write_text(
            json.dumps({"port": port, "upstream_port": main.getsockname()[1]})
        )
        msg = _load_hook_module()._check_proxy_default_health()
        assert f"127.0.0.1:{port}" in msg
        assert f"gui/$(id -u)/{pd.SHIM_LABEL}" in msg
        assert "will fail" in msg
    finally:
        main.close()
