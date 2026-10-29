"""P2.12 (P212-3): the Claude Code plugin's SessionStart install path, and no double-wiring.

Real code paths against a tmp HOME (HOME, ``Path.home``, ``LLM_ROUTER_HOME`` and the cwd point
into tmp_path; the seat probe is replaced), the same fixture as test_p212_receipt.py. The hook
is loaded from the bundled file the plugin runs, with an injected spawner so no process starts.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import host_detect, seats
from llm_router import install_hooks as ih
from llm_router.commands import install

REPO = Path(__file__).resolve().parent.parent


def _point_at(home: Path, monkeypatch) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(home)
    detect = host_detect.detect_hosts
    monkeypatch.setattr(host_detect, "detect_hosts",
                        lambda **k: detect(**{"home": home, "which": lambda b: None, **k}))
    monkeypatch.setattr(seats, "refresh_seats",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no seat probe in tests")))
    return home


def _add_codex(home: Path) -> None:
    d = host_detect._config_dir(host_detect._HOSTS["codex"][1], home, sys.platform, os.environ)
    d.mkdir(parents=True, exist_ok=True)
    (d / "host-own-state.json").write_text("{}")


def _llm_router_hook_commands(settings_file: Path) -> list[str]:
    if not settings_file.is_file():
        return []
    hooks = json.loads(settings_file.read_text()).get("hooks") or {}
    return [s for s in install._json_strings(hooks) if "llm_router" in s or "llm-router" in s]


def _register_plugin(home: Path, root: Path = REPO, enabled: bool | None = True,
                     scope: str = "user", project: Path | None = None) -> None:
    """What Claude Code leaves behind after `/plugin install llm-router@ypollak2`.
    ``enabled=None`` leaves enabledPlugins out of settings.json altogether."""
    plugins = ih.claude_dir() / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    entry = {"scope": scope, "installPath": str(root)}
    if project is not None:
        entry["projectPath"] = str(project)
    (plugins / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"llm-router@ypollak2": [entry]}}))
    if enabled is not None:
        ih.settings_path().parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(ih.settings_path().read_text()) if ih.settings_path().is_file() else {}
        existing["enabledPlugins"] = {"llm-router@ypollak2": enabled}
        ih.settings_path().write_text(json.dumps(existing))


@pytest.fixture
def home(tmp_path, monkeypatch):
    return _point_at(tmp_path / "home", monkeypatch)


def _hook():
    spec = importlib.util.spec_from_file_location("plugin_install_hook", REPO / "hooks" / "plugin-install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_plugin(tmp_path: Path, version: str) -> Path:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    return root


def _env(tmp_path: Path, root: Path) -> dict:
    return {"CLAUDE_PLUGIN_ROOT": str(root), "LLM_ROUTER_HOME": str(tmp_path / "state")}


def _wired_receipt(tmp_path: Path, version: str) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "install_receipt.json").write_text(json.dumps({"package_version": version}))


# ── the bundle ────────────────────────────────────────────────────────────────

def test_plugin_hooks_json_runs_the_installer_hook_and_the_cli_does_not():
    claude = json.loads((REPO / "hooks" / "hooks.json").read_text())
    commands = [h["command"] for g in claude["hooks"]["SessionStart"] for h in g["hooks"]]
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/plugin-install.py" in commands
    assert "plugin-install" not in (REPO / ".codex-plugin" / "hooks.json").read_text()
    assert "plugin-install.py" not in {src for src, _d, _e, _m in ih._HOOK_DEFS}


def test_cli_install_registers_no_plugin_install_hook(home):
    ih.install()
    assert "plugin-install" not in ih.settings_path().read_text()
    assert not (ih.hooks_dst() / "llm_router-plugin-install.py").exists()


def test_bundle_is_up_to_date():
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "build_plugin_bundle.py"), "--check"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (REPO / "commands" / "setup.md").is_file()


# ── the SessionStart hook ─────────────────────────────────────────────────────

def test_matching_versions_are_silent(tmp_path):
    root = _fake_plugin(tmp_path, "9.9.9")
    _wired_receipt(tmp_path, "9.9.9")
    spawned, out = [], io.StringIO()
    _hook().run(_env(tmp_path, root), lambda argv, env, cwd: spawned.append(argv), out)
    assert spawned == [] and out.getvalue() == ""


@pytest.mark.timing
def test_mismatch_without_consent_asks_once_and_spawns_nothing(tmp_path):
    root = _fake_plugin(tmp_path, "9.9.9")
    _wired_receipt(tmp_path, "9.9.8")
    spawned, first, second = [], io.StringIO(), io.StringIO()
    hook = _hook()
    t0 = time.perf_counter()
    hook.run(_env(tmp_path, root), lambda argv, env, cwd: spawned.append(argv), first)
    assert time.perf_counter() - t0 < 0.2
    hook.run(_env(tmp_path, root), lambda argv, env, cwd: spawned.append(argv), second)
    lines = first.getvalue().splitlines()
    assert len(lines) == 1 and "/llm-router:setup" in lines[0] and "9.9.9" in lines[0]
    assert second.getvalue() == "" and spawned == []
    state_file = tmp_path / "state" / "plugin_install_state.json"
    assert oct(state_file.stat().st_mode & 0o777) == "0o600"


@pytest.mark.timing
def test_mismatch_with_consent_spawns_one_detached_install_once(tmp_path):
    root = _fake_plugin(tmp_path, "9.9.9")
    _wired_receipt(tmp_path, "9.9.8")
    (tmp_path / "state" / "plugin_install_consent.json").write_text('{"consent": true}')
    spawned, out = [], io.StringIO()
    hook = _hook()
    t0 = time.perf_counter()
    hook.run(_env(tmp_path, root), lambda argv, env, cwd: spawned.append((argv, env, cwd)), out)
    assert time.perf_counter() - t0 < 0.2
    assert [a for a, _e, _c in spawned] == [["npx", "-y", "llm-routing@9.9.9", "install", "--auto", "--via-plugin"]]
    assert spawned[0][2] == tmp_path / "state" and spawned[0][2] != Path.cwd()  # never the project dir
    assert spawned[0][1]["CLAUDE_PLUGIN_ROOT"] == str(root) and out.getvalue() == ""
    hook.run(_env(tmp_path, root), lambda argv, env, cwd: spawned.append((argv, env, cwd)), out)
    assert len(spawned) == 1  # once per version: a failing install is not retried every session


def test_no_receipt_counts_as_a_mismatch(tmp_path):
    root = _fake_plugin(tmp_path, "9.9.9")
    out = io.StringIO()
    _hook().run(_env(tmp_path, root), lambda *a, **k: pytest.fail("spawned without consent"), out)
    assert "/llm-router:setup" in out.getvalue()


def test_hook_runs_as_the_script_the_plugin_executes(tmp_path):
    root = _fake_plugin(tmp_path, "9.9.9")
    _wired_receipt(tmp_path, "9.9.8")
    res = subprocess.run([sys.executable, str(REPO / "hooks" / "plugin-install.py")],
                         env={**os.environ, **_env(tmp_path, root)}, capture_output=True, text=True, timeout=10)
    assert res.returncode == 0 and "/llm-router:setup" in res.stdout, res.stderr


def test_failed_spawn_is_retried_after_backoff_capped_and_cleared_by_a_matching_receipt(tmp_path):
    root = _fake_plugin(tmp_path, "9.9.9")
    _wired_receipt(tmp_path, "9.9.8")  # the stubbed spawn never produces a receipt: it "failed"
    (tmp_path / "state" / "plugin_install_consent.json").write_text('{"consent": true}')
    hook, spawned, clock = _hook(), [], [1000.0]

    def run():
        out = io.StringIO()
        hook.run(_env(tmp_path, root), lambda argv, env, cwd: spawned.append(argv), out, now=lambda: clock[0])
        return out.getvalue()

    assert run() == "" and len(spawned) == 1                      # first attempt
    clock[0] += 3600
    msg = run()                                                    # inside the backoff: no spawn, but say so
    assert len(spawned) == 1 and msg.count("\n") == 1 and "/llm-router:setup" in msg
    clock[0] += hook.RETRY_AFTER_S
    assert "/llm-router:setup" in run() and len(spawned) == 2      # retried after the backoff
    clock[0] += hook.RETRY_AFTER_S
    run()
    assert len(spawned) == 3
    clock[0] += hook.RETRY_AFTER_S
    assert "/llm-router:setup" in run() and len(spawned) == 3      # capped at MAX_ATTEMPTS
    _wired_receipt(tmp_path, "9.9.9")                              # the install finally landed
    assert run() == ""
    state = json.loads((tmp_path / "state" / "plugin_install_state.json").read_text())
    assert "spawn_version" not in state


def test_spawn_that_raises_is_recorded_and_reported_not_fatal(tmp_path, capsys):
    root = _fake_plugin(tmp_path, "9.9.9")
    _wired_receipt(tmp_path, "9.9.8")
    (tmp_path / "state" / "plugin_install_consent.json").write_text('{"consent": true}')

    def boom(argv, env, cwd):
        raise FileNotFoundError("npx")

    assert _hook().run(_env(tmp_path, root), boom, io.StringIO()) == 0
    assert "skipped" in capsys.readouterr().err
    state = json.loads((tmp_path / "state" / "plugin_install_state.json").read_text())
    assert state["spawn_attempts"] == 1


def test_real_spawn_runs_in_the_state_dir(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    _hook()._detached(["npx"], {"A": "b"}, tmp_path / "state")
    assert seen["cwd"] == str(tmp_path / "state") and seen["cwd"] != str(project)


# ── --via-plugin: no double wiring ────────────────────────────────────────────

def test_via_plugin_leaves_no_llm_router_hooks_in_settings_and_wires_the_other_hosts(home, monkeypatch):
    _add_codex(home)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))

    result = install.run_full(interactive=False, flags=["--via-plugin"])

    rows = result.receipt["hosts"]
    assert rows["claude-code"]["status"] == "wired" and rows["claude-code"]["via"] == "plugin", rows
    assert rows["claude-code"]["hook_version"], rows
    assert rows["codex"]["status"] == "wired" and rows["codex"]["hook_version"], rows
    assert _llm_router_hook_commands(ih.settings_path()) == []
    assert "llm_router" not in (json.loads(ih.settings_path().read_text()).get("mcpServers") or {})
    assert json.loads(consent_text(home))["consent"] is True
    assert install.receipt_failures(result.receipt) == []


def consent_text(home: Path) -> str:
    return install.consent_path().read_text()


def test_via_plugin_digest_equals_the_cli_digest_on_a_twin_home(tmp_path, monkeypatch):
    a = _point_at(tmp_path / "a", monkeypatch)
    _add_codex(a)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    plugin = install.run_full(interactive=False, flags=["--via-plugin"]).receipt

    b = _point_at(tmp_path / "b", monkeypatch)
    _add_codex(b)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    cli = install.run_full(interactive=False).receipt

    assert plugin["digest"] == cli["digest"]
    assert plugin["hosts"]["claude-code"]["via"] == "plugin" and cli["hosts"]["claude-code"]["via"] == "settings"


def test_via_plugin_without_a_plugin_is_an_error_row_not_a_silent_cli_wiring(home, capsys):
    result = install.run_full(interactive=False, flags=["--via-plugin"])
    row = result.receipt["hosts"]["claude-code"]
    assert row["status"] == "error" and "--via-plugin" in row["reason"]
    assert _llm_router_hook_commands(ih.settings_path()) == []
    assert install.cmd_install(["--auto", "--via-plugin"]) == 1


# ── a plugin user who also runs `npm i -g` (npm postinstall = `install --auto`) ──

def test_auto_install_detects_the_plugin_and_does_not_wire_hooks_twice(home, capsys):
    _register_plugin(home)

    rc = install.cmd_install(["--auto"])  # exactly what npm/install.js spawns

    receipt = install.read_receipt()
    assert rc == 0
    assert receipt["hosts"]["claude-code"]["via"] == "plugin", receipt["hosts"]
    assert _llm_router_hook_commands(ih.settings_path()) == []
    assert "llm_router" not in (json.loads(ih.settings_path().read_text()).get("mcpServers") or {})


def test_without_a_plugin_auto_install_still_wires_settings(home):
    install.cmd_install(["--auto"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "settings"
    assert _llm_router_hook_commands(ih.settings_path())


def test_a_disabled_plugin_does_not_suppress_the_cli_wiring(home):
    _register_plugin(home, enabled=False)
    install.cmd_install(["--auto"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "settings"
    assert _llm_router_hook_commands(ih.settings_path())


def test_a_missing_enabled_plugins_key_is_not_enabled(home):
    _register_plugin(home, enabled=None)
    install.cmd_install(["--auto"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "settings"
    assert _llm_router_hook_commands(ih.settings_path())


def test_another_projects_entry_does_not_suppress_cli_wiring(home, tmp_path):
    other = tmp_path / "other-project"
    other.mkdir()
    _register_plugin(home, scope="project", project=other)
    install.cmd_install(["--auto"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "settings"
    assert _llm_router_hook_commands(ih.settings_path())


def test_this_projects_entry_counts(home):
    _register_plugin(home, scope="project", project=Path.cwd())
    install.cmd_install(["--auto"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "plugin"


def test_skip_names_the_plugin_path_and_no_plugin_forces_cli_wiring(home, capsys):
    _register_plugin(home)
    install.cmd_install(["--auto"])
    out = capsys.readouterr().out
    assert str(REPO) in out and "--no-plugin" in out
    install.cmd_install(["--auto", "--no-plugin"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "settings"


def test_a_stale_install_path_is_not_a_plugin(home, tmp_path):
    _register_plugin(home, root=tmp_path / "gone")
    install.cmd_install(["--auto"])
    assert install.read_receipt()["hosts"]["claude-code"]["via"] == "settings"


def test_earlier_cli_hooks_plus_plugin_are_reported_not_silently_doubled(home, capsys):
    ih.install()  # a CLI install first
    _register_plugin(home)
    install.cmd_install(["--auto"])
    assert "fires twice" in capsys.readouterr().out
