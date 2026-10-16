"""P2.2-A: `install --authority route|advise`, `--no-authority`, and the default-safe
constant PROXY_DEFAULT_ON_INSTALL (ships False: a plain install turns nothing on).

Fixture HOME: settings.json is a tmp file (install_hooks._SETTINGS_PATH), the state dir is a
tmp LLM_ROUTER_HOME, the proxy is a listening socket, and no launchctl runs (the proxy
is "already answering", the reuse path of install_proxy_default). The real ~/.claude is never touched.
"""

from __future__ import annotations

import json
import socket

import pytest

from llm_router import authority, install_manifest, proxy_default as pd
from llm_router.commands import install as cmd_install_mod
from llm_router.commands import proxy_default as cmd_pd

HINT = "Routing authority is OFF by default"


@pytest.fixture
def world(monkeypatch, tmp_path):
    import llm_router.install_hooks as ih

    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(ih, "_SETTINGS_PATH", settings)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LLM_ROUTER_AUTHORITY", raising=False)
    monkeypatch.setattr(authority, "_file_cache", None)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    real = cmd_pd.install_proxy_default
    calls: list[dict] = []

    def fake_install(**kw):
        calls.append(kw)
        return real(port=port, shim=False, home=tmp_path / "svc", system="Darwin")

    monkeypatch.setattr(cmd_pd, "install_proxy_default", fake_install)
    ran_plain: list[list[str]] = []
    monkeypatch.setattr(cmd_install_mod, "_run_install", lambda flags: ran_plain.append(list(flags)))
    yield {"settings": settings, "port": port, "calls": calls, "plain": ran_plain,
           "mode_file": tmp_path / "state" / "authority"}
    srv.close()


def _base_url(settings):
    return json.loads(settings.read_text())["env"]["ANTHROPIC_BASE_URL"] if settings.exists() else None


@pytest.mark.parametrize("mode", ["route", "advise"])
def test_authority_flag_installs_the_proxy_default_and_sets_the_mode(world, mode):
    assert cmd_install_mod.cmd_install(["--authority", mode]) == 0
    assert _base_url(world["settings"]) == f"http://127.0.0.1:{world['port']}"
    assert world["mode_file"].read_text() == mode + "\n"
    assert pd.read_sentinel()["enabled"] is True
    assert world["plain"] == [], "the flag runs the proxy-default step, not a hook reinstall"
    assert world["calls"] == [{}], "install_proxy_default runs with its own defaults (fail-open shim included)"


def test_a_refused_proxy_install_exits_nonzero_sets_no_mode_and_leaves_settings(world, monkeypatch, capsys):
    monkeypatch.setattr(cmd_pd, "install_proxy_default",
                        lambda **kw: {"ok": False, "actions": [], "error": "proxy never answered", "reused": False})
    assert cmd_install_mod.cmd_install(["--authority", "route"]) == 1
    assert not world["settings"].exists() and not world["mode_file"].exists()
    assert "proxy never answered" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--authority"], ["--authority", "turbo"], ["--authority", "--no-authority"],
                                  ["--authority", "route", "--no-authority"]])
def test_bad_authority_arguments_exit_2_and_change_nothing(world, argv):
    assert cmd_install_mod.cmd_install(argv) == 2
    assert world["calls"] == [] and not world["settings"].exists() and not world["mode_file"].exists()


def test_plain_install_ships_with_authority_off(world, capsys):
    assert cmd_install_mod.PROXY_DEFAULT_ON_INSTALL is False
    world["settings"].parent.mkdir(parents=True)
    world["settings"].write_bytes(b'{"env": {"KEEP": "1"}}\n')
    before = world["settings"].read_bytes()
    assert cmd_install_mod.cmd_install([]) == 0
    out = capsys.readouterr().out
    assert HINT in out and "llm-router install --authority route" in out
    assert world["plain"] == [[]], "the ordinary install still ran"
    assert world["calls"] == [] and world["settings"].read_bytes() == before
    assert pd.read_sentinel() is None and not world["mode_file"].exists()


def test_switch_on_makes_a_fresh_install_route(world, monkeypatch, capsys):
    monkeypatch.setattr(cmd_install_mod, "PROXY_DEFAULT_ON_INSTALL", True)
    assert cmd_install_mod.cmd_install([]) == 0
    assert HINT not in capsys.readouterr().out
    assert _base_url(world["settings"]) == f"http://127.0.0.1:{world['port']}"
    assert world["mode_file"].read_text() == "route\n"


def test_switch_on_leaves_an_install_that_already_names_a_base_url(world, monkeypatch):
    monkeypatch.setattr(cmd_install_mod, "PROXY_DEFAULT_ON_INSTALL", True)
    world["settings"].parent.mkdir(parents=True)
    world["settings"].write_bytes(b'{"env": {"ANTHROPIC_BASE_URL": "https://corp.example/v1"}}\n')
    before = world["settings"].read_bytes()
    assert cmd_install_mod.cmd_install([]) == 0
    assert world["calls"] == [] and world["settings"].read_bytes() == before and not world["mode_file"].exists()


def test_switch_on_leaves_an_unparseable_settings_file_alone(world, monkeypatch):
    monkeypatch.setattr(cmd_install_mod, "PROXY_DEFAULT_ON_INSTALL", True)
    world["settings"].parent.mkdir(parents=True)
    world["settings"].write_bytes(b"{not json")
    assert cmd_install_mod.cmd_install([]) == 0
    assert world["calls"] == [] and world["settings"].read_bytes() == b"{not json"


def test_switch_on_leaves_an_existing_proxy_default_install_alone(world, monkeypatch):
    monkeypatch.setattr(cmd_install_mod, "PROXY_DEFAULT_ON_INSTALL", True)
    pd.write_sentinel(port=8787, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin")
    assert cmd_install_mod.cmd_install([]) == 0
    assert world["calls"] == []


def test_no_authority_wins_over_the_switch(world, monkeypatch, capsys):
    monkeypatch.setattr(cmd_install_mod, "PROXY_DEFAULT_ON_INSTALL", True)
    assert cmd_install_mod.cmd_install(["--no-authority"]) == 0
    assert world["calls"] == [] and not world["settings"].exists() and HINT not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--check"], ["--project"], ["--host", "cursor"]])
def test_other_install_modes_neither_hint_nor_switch_on(world, monkeypatch, capsys, argv):
    monkeypatch.setattr(cmd_install_mod, "PROXY_DEFAULT_ON_INSTALL", True)
    assert cmd_install_mod.cmd_install(argv) == 0
    assert world["calls"] == [] and HINT not in capsys.readouterr().out


def test_help_documents_the_new_flags(capsys):
    assert cmd_install_mod.cmd_install(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--authority route|advise" in out and "--no-authority" in out


def test_uninstall_restores_the_previous_env_block(world):
    world["settings"].parent.mkdir(parents=True)
    world["settings"].write_text(json.dumps({"env": {"SOME_OTHER_VAR": "keep-me"}}))
    assert cmd_install_mod.cmd_install(["--authority", "route"]) == 0
    assert install_manifest.apply_uninstall()
    assert json.loads(world["settings"].read_text()).get("env") == {"SOME_OTHER_VAR": "keep-me"}

