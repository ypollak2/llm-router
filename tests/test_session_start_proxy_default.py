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

import pytest


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


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch, tmp_path):
    """No real HOME settings, no real cwd settings, no inherited ANTHROPIC_BASE_URL."""
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.chdir(proj)


def _sentinel(tmp_path, monkeypatch, text, *, routed=True):
    """Write the sentinel; by default the session routes to its port (via the env)."""
    text = text if isinstance(text, str) else json.dumps(text)
    (tmp_path / "proxy_default.json").write_text(text)
    try:
        port = int(json.loads(text).get("port", 8787))
    except Exception:
        return
    if routed:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")


def _listening_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(8)  # several probes per test must not fill the accept backlog
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
        _sentinel(tmp_path, monkeypatch, json.dumps({"port": port}))
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
    _sentinel(tmp_path, monkeypatch, json.dumps({"port": port}))
    mod = _load_hook_module()
    msg = mod._check_proxy_default_health()
    assert f"127.0.0.1:{port}" in msg
    assert "install --proxy-default off" in msg
    assert pd.LABEL in msg


def test_malformed_sentinel_is_not_treated_as_a_dead_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _sentinel(tmp_path, monkeypatch, "not valid json {{{")
    mod = _load_hook_module()
    assert mod._check_proxy_default_health() == ""


def test_defaults_to_the_documented_port_when_sentinel_omits_it(monkeypatch, tmp_path):
    """8787 (pd.DEFAULT_PORT) may genuinely be occupied by the owner's own
    live LaunchAgent on a dev machine, so this must not depend on real
    connectivity to it — `socket.create_connection` is stubbed to fail for
    every port, isolating the assertion to "what port did the hook check"."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _sentinel(tmp_path, monkeypatch, json.dumps({}))
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
    """docs/BUGS.md P010-1 review: with the fail-open shim, `port` is the shim's and
    it always accepts. Probing it alone read healthy while the main proxy was
    dead and every call bypassed routing."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    shim = _listening_socket()
    try:
        port, upstream = shim.getsockname()[1], _dead_port()
        _sentinel(tmp_path, monkeypatch, 
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
        _sentinel(tmp_path, monkeypatch, json.dumps(
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
        _sentinel(tmp_path, monkeypatch, 
            json.dumps({"port": port, "upstream_port": main.getsockname()[1]})
        )
        msg = _load_hook_module()._check_proxy_default_health()
        assert f"127.0.0.1:{port}" in msg
        assert f"gui/$(id -u)/{pd.SHIM_LABEL}" in msg
        assert "will fail" in msg
    finally:
        main.close()


# -- PD-HEALTH-1: judge the session's own routing, not just the sentinel's port ----------


def _settings(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}))


def test_a_not_routed_session_is_not_told_the_proxy_is_dead(monkeypatch, tmp_path):
    """2026-10-08: no ANTHROPIC_BASE_URL in the session, proxy port dead -> the old
    "every API call will fail" warning was false. Now: routing-off line, no 'will fail'."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": _dead_port()}, routed=False)
    msg = _load_hook_module()._check_proxy_default_health()
    assert "routing is OFF" in msg and "will fail" not in msg
    assert "~/.claude/settings.json env.ANTHROPIC_BASE_URL is missing" in msg
    assert "llm-router install --proxy-default" in msg
    assert len(msg.strip().splitlines()) == 2  # the warning and its one restore line


def test_sentinel_enabled_but_settings_lost_the_key_warns_even_if_proxy_is_up(monkeypatch, tmp_path):
    """The opposite miss: settings.json lost the key, proxy answers, routing silently off."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": srv.getsockname()[1]}, routed=False)
        _settings(Path.home() / ".claude" / "settings.json", "")  # key present but empty
        assert "routing is OFF" in _load_hook_module()._check_proxy_default_health()
    finally:
        srv.close()


def test_a_project_override_naming_another_host_warns_without_printing_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": _dead_port()}, routed=False)
    _settings(Path.cwd() / ".claude" / "settings.local.json",
              "https://user:s3cr3tpw@api.example.com:9443/v1?key=s3cr3tq")
    msg = _load_hook_module()._check_proxy_default_health()
    assert "routing is OFF" in msg and "settings.local.json" in msg
    assert "api.example.com:9443" in msg
    assert "s3cr3t" not in msg and "user:" not in msg and "/v1" not in msg


def test_settings_precedence_when_env_is_absent(monkeypatch, tmp_path):
    """Local beats project beats user, so a local override to the proxy port counts as routed."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port}, routed=False)
        _settings(Path.home() / ".claude" / "settings.json", "https://api.anthropic.com")
        _settings(Path.cwd() / ".claude" / "settings.local.json", f"http://127.0.0.1:{port}")
        assert _load_hook_module()._check_proxy_default_health() == ""
    finally:
        srv.close()


def test_routed_and_dead_still_gets_the_failure_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    port = _dead_port()
    _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port})
    msg = _load_hook_module()._check_proxy_default_health()
    assert "will fail" in msg and f"127.0.0.1:{port}" in msg and "routing is OFF" not in msg


def test_not_installed_stays_silent_whatever_the_session_routes_to(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    assert _load_hook_module()._check_proxy_default_health() == ""


def test_routed_to_the_main_proxy_directly_counts_as_routed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    shim, main = _listening_socket(), _listening_socket()
    try:
        _sentinel(tmp_path, monkeypatch, {"port": shim.getsockname()[1],
                                          "upstream_port": main.getsockname()[1]}, routed=False)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://localhost:{main.getsockname()[1]}")
        assert _load_hook_module()._check_proxy_default_health() == ""
    finally:
        shim.close()
        main.close()


# --- PD-HEALTH-1 mutation gaps (independent review, 2026-10-08) --------------------------


def _msg() -> str:
    return _load_hook_module()._check_proxy_default_health()


def test_disabled_sentinel_is_silent_even_when_routed_and_dead(monkeypatch, tmp_path):
    """Mutant: skip the `enabled: false` check -> a deliberately disabled proxy warns."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _sentinel(tmp_path, monkeypatch, {"enabled": False, "port": _dead_port()})  # routed + dead
    assert _msg() == ""
    # and not-routed too: disabled means no routing expectation at all
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    assert _msg() == ""


@pytest.mark.parametrize("url", [
    "http://api.example.com:{port}",   # right port, wrong host
    "http://127.0.0.2:{port}",         # loopback-ish but not the proxy host
    "http://127.0.0.1:{other}",        # right host, wrong port
    "http://127.0.0.1",                # right host, no port
])
def test_only_loopback_on_the_proxy_port_counts_as_routed(monkeypatch, tmp_path, url):
    """Mutants: accept any host / accept any port in `_routes_to_local_port`.
    A live listener on the proxy port means a loosened check falls through to ''."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port}, routed=False)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url.format(port=port, other=port + 1))
        assert "routing is OFF" in _msg()
    finally:
        srv.close()


@pytest.mark.parametrize("local_to_proxy", [True, False])
def test_project_local_settings_beat_project_settings(monkeypatch, tmp_path, local_to_proxy):
    """Mutant: swap settings.local.json / settings.json precedence."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port}, routed=False)
        proxy, other = f"http://127.0.0.1:{port}", "https://api.anthropic.com"
        _settings(Path.cwd() / ".claude" / "settings.local.json", proxy if local_to_proxy else other)
        _settings(Path.cwd() / ".claude" / "settings.json", other if local_to_proxy else proxy)
        assert (_msg() == "") is local_to_proxy
    finally:
        srv.close()


def test_user_settings_are_the_last_fallback(monkeypatch, tmp_path):
    """Mutant: drop the ~/.claude/settings.json fallback -> a routed session reads as OFF."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port}, routed=False)
        _settings(Path.home() / ".claude" / "settings.json", f"http://127.0.0.1:{port}")
        assert _msg() == ""
    finally:
        srv.close()


def test_scheme_less_base_url_is_parsed_as_host_port(monkeypatch, tmp_path):
    """Mutant: urlsplit without the '//' prefix -> hostname is None for `127.0.0.1:PORT`."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port}, routed=False)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"127.0.0.1:{port}")
        assert _msg() == ""
    finally:
        srv.close()


def test_probe_connect_timeout_is_one_second(monkeypatch, tmp_path):
    """Mutant: 1 s -> 5 s. Asserts the timeout handed to the probe, not wall-clock: this
    hook blocks session start, so the budget is the contract."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": 8787})
    seen: list = []

    def _fake(addr, timeout=None, *a, **k):
        seen.append(timeout)
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", _fake)
    assert "will fail" in _msg()
    assert seen == [1.0]


def test_project_settings_are_read_from_claude_project_dir(monkeypatch, tmp_path):
    """Hooks receive CLAUDE_PROJECT_DIR; a cwd that drifted into a subdirectory must not
    hide the project's settings. Mutant: read os.getcwd() only."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        _sentinel(tmp_path, monkeypatch, {"enabled": True, "port": port}, routed=False)
        root = tmp_path / "root"
        sub = root / "pkg" / "deep"
        sub.mkdir(parents=True)
        _settings(root / ".claude" / "settings.json", f"http://127.0.0.1:{port}")
        monkeypatch.chdir(sub)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        assert _msg() == ""
        # without the variable the cwd is the fallback, and it has no settings -> OFF
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        assert "routing is OFF" in _msg()
        (sub / ".claude").mkdir()
        _settings(sub / ".claude" / "settings.json", f"http://127.0.0.1:{port}")
        assert _msg() == ""
    finally:
        srv.close()
