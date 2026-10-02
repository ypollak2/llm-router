"""A value in ``~/.llm-router/.env`` must reach every process that reads it.

Regression for the 2026-10 semantic audit: ``LLM_ROUTER_SEMANTIC_HISTORY=shadow``
sat in ``~/.llm-router/.env`` while ``llm-router semantic status``, the MCP
server and the proxy all ran with HISTORY off, because only four hook scripts
carried a private copy of a ``.env`` loader. These tests drive the shared
loader, each process entry point and the hook scripts, and assert on what
``os.environ`` / ``semantic.modes`` actually contain afterwards.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import env_loader

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "src" / "llm_router" / "hooks"

KEY = "LLM_ROUTER_SEMANTIC_HISTORY"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandboxed LLM_ROUTER_HOME + HOME + cwd, none of them the operator's."""
    router_home = tmp_path / "router-home"
    router_home.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(router_home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.chdir(cwd)
    # Register restoration for every key these tests may set, so a leak from
    # one test cannot reach the next (setenv then delenv records "absent").
    for k in (KEY, "LLM_ROUTER_FROM_HOME", "LLM_ROUTER_FROM_USER", "XAI_API_KEY",
              "LLM_ROUTER_FROM_PROJECT", "PYTHONPATH", "LLM_ROUTER_FAKE_URL",
              "LLM_ROUTER_FAKE_BASE", "LLM_ROUTER_ENFORCE"):
        monkeypatch.setenv(k, "x")
        monkeypatch.delenv(k)
    return type("Home", (), {"router": router_home, "user": user_home, "cwd": cwd})


def _env(path: Path, text: str) -> None:
    path.write_text(text)


# ── the loader ───────────────────────────────────────────────────────────────


def test_dotenv_value_reaches_os_environ(home):
    _env(home.router / ".env", f"{KEY}=shadow\n")
    applied = env_loader.load_dotenv_files()
    assert os.environ[KEY] == "shadow"
    assert KEY in applied


def test_real_environment_overrides_dotenv(home, monkeypatch):
    _env(home.router / ".env", f"{KEY}=shadow\n")
    monkeypatch.setenv(KEY, "off")
    applied = env_loader.load_dotenv_files()
    assert os.environ[KEY] == "off"
    assert KEY not in applied


def test_semantic_modes_see_a_dotenv_value_after_loading(home):
    """The audit's actual symptom: modes.current().history stayed off."""
    from llm_router.semantic import modes

    _env(home.router / ".env", f"{KEY}=shadow\n")
    before = modes.current().history
    env_loader.load_dotenv_files()
    after = modes.current().history
    assert before != after
    assert after.value == "shadow"


def test_parsing_skips_comments_blanks_and_strips_quotes(home):
    _env(home.router / ".env",
         "# a comment\n\nnot a pair\nLLM_ROUTER_FROM_HOME = 'quoted'\n")
    env_loader.load_dotenv_files()
    assert os.environ["LLM_ROUTER_FROM_HOME"] == "quoted"


def test_earlier_file_wins_and_user_home_env_is_read(home):
    _env(home.router / ".env", "LLM_ROUTER_FROM_HOME=router\n")
    _env(home.user / ".env", "LLM_ROUTER_FROM_HOME=user\nXAI_API_KEY=k\n")
    env_loader.load_dotenv_files()
    assert os.environ["LLM_ROUTER_FROM_HOME"] == "router"
    assert os.environ["XAI_API_KEY"] == "k"


def test_loading_twice_is_a_no_op(home):
    _env(home.router / ".env", f"{KEY}=shadow\n")
    env_loader.load_dotenv_files()
    assert env_loader.load_dotenv_files() == {}


def test_load_into_a_dict_leaves_os_environ_alone(home):
    _env(home.router / ".env", f"{KEY}=shadow\n")
    target: dict[str, str] = {}
    env_loader.load_dotenv_files(target=target)
    assert target[KEY] == "shadow"
    assert KEY not in os.environ


def test_missing_and_unreadable_files_are_not_an_error(home):
    (home.router / ".env").mkdir()  # a directory where a file is expected
    assert env_loader.load_dotenv_files() == {}


# ── a binary .env must not crash anything ───────────────────────────────────


def test_a_binary_dotenv_is_ignored_not_fatal(home):
    (home.router / ".env").write_bytes(b"\xff\xfe\x00LLM_ROUTER_FROM_HOME=x\n\x80")
    _env(home.user / ".env", "LLM_ROUTER_FROM_USER=ok\n")
    assert env_loader.load_dotenv_files() == {"LLM_ROUTER_FROM_USER": "ok"}


def test_cli_help_survives_a_binary_dotenv(home, monkeypatch):
    from llm_router import cli

    (home.router / ".env").write_bytes(b"\xff\xfe\x00\x80")
    monkeypatch.setattr(sys, "argv", ["llm-router", "--version"])
    cli.main()  # must not raise UnicodeDecodeError


@pytest.mark.parametrize("hook", ["session-start", "agent-route", "stop-enforce", "auto-route"])
def test_hook_fallback_loaders_survive_a_binary_dotenv(home, hook, monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_router.env_loader", None)
    (home.router / ".env").write_bytes(b"\xff\xfe\x00\x80")
    _import_hook(hook)  # import runs the fallback loader; must not raise


# ── SEC-002/003: the project's own .env is repository content ────────────────


def test_project_dotenv_may_set_llm_router_settings_and_api_keys(home):
    _env(home.cwd / ".env", "LLM_ROUTER_FROM_PROJECT=yes\nXAI_API_KEY=pk\n")
    env_loader.load_dotenv_files()
    assert os.environ["LLM_ROUTER_FROM_PROJECT"] == "yes"
    assert os.environ["XAI_API_KEY"] == "pk"


def test_project_dotenv_cannot_inject_process_or_endpoint_variables(home):
    _env(home.cwd / ".env",
         "PYTHONPATH=/evil\nLLM_ROUTER_FAKE_URL=http://evil\n"
         "LLM_ROUTER_FAKE_BASE=http://evil\nLLM_ROUTER_FROM_PROJECT=ok\n")
    env_loader.load_dotenv_files()
    assert "PYTHONPATH" not in os.environ
    assert "LLM_ROUTER_FAKE_URL" not in os.environ
    assert "LLM_ROUTER_FAKE_BASE" not in os.environ
    assert os.environ["LLM_ROUTER_FROM_PROJECT"] == "ok"


def test_user_dotenv_is_trusted_for_any_key(home):
    _env(home.router / ".env", "LLM_ROUTER_FAKE_URL=http://localhost:11434\n")
    env_loader.load_dotenv_files()
    assert os.environ["LLM_ROUTER_FAKE_URL"] == "http://localhost:11434"


# ── process entry points ─────────────────────────────────────────────────────


def test_cli_main_applies_dotenv_before_dispatch(home, monkeypatch):
    from llm_router import cli

    _env(home.router / ".env", f"{KEY}=shadow\n")
    monkeypatch.setattr(sys, "argv", ["llm-router", "--version"])
    cli.main()
    assert os.environ[KEY] == "shadow"


def test_proxy_command_applies_dotenv(home):
    from llm_router.proxy import server as proxy_server

    _env(home.router / ".env", f"{KEY}=shadow\n")
    assert proxy_server.cmd_proxy(["--help"]) == 0
    assert os.environ[KEY] == "shadow"


def test_a_fresh_cli_process_honours_dotenv(home):
    """End to end, in a real child process: what `llm-router ...` actually sees."""
    _env(home.router / ".env", f"{KEY}=shadow\n")
    code = (
        "import sys, os; sys.argv = ['llm-router', '--version'];"
        "from llm_router import cli; cli.main();"
        "from llm_router.semantic import modes;"
        "print(os.environ.get('" + KEY + "'), modes.current().history.value)"
    )
    child_env = {k: v for k, v in os.environ.items() if k != KEY}
    child_env.update(LLM_ROUTER_HOME=str(home.router), HOME=str(home.user),
                     PYTHONPATH=str(REPO / "src"))
    out = subprocess.run([sys.executable, "-c", code], cwd=home.cwd, env=child_env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "shadow shadow"


# ── hook scripts ─────────────────────────────────────────────────────────────


def _import_hook(name: str):
    """Import a hyphenated hook script, then undo what its import did to os.environ."""
    path = HOOKS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"hook_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
        seen = dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod, seen


# Hooks that used to carry a private loader, and hooks that had none at all.
_HOOKS_THAT_LOAD_DOTENV = [
    "auto-route", "session-start", "agent-route", "stop-enforce",
    "enforce-route", "status-bar", "bash-compress", "subagent-start",
    "session-end", "usage-refresh", "agent-depth-release", "cc-usage-track",
    "agent-error", "codex-post-tool", "codex-stop", "gemini-cli-auto-route",
    "gemini-cli-post-tool", "gemini-cli-session-end", "opencode-post-tool",
    "playwright-compress", "response-router", "session-end-clawcode",
    "status-bar-clawcode",
]


@pytest.mark.parametrize("hook", _HOOKS_THAT_LOAD_DOTENV)
def test_every_hook_applies_dotenv_at_import(home, hook):
    _env(home.router / ".env", f"{KEY}=shadow\n")
    _, seen = _import_hook(hook)
    assert seen.get(KEY) == "shadow", f"{hook}.py ignored ~/.llm-router/.env"


@pytest.mark.parametrize("hook", _HOOKS_THAT_LOAD_DOTENV)
def test_every_hook_lets_the_environment_win(home, hook, monkeypatch):
    _env(home.router / ".env", f"{KEY}=shadow\n")
    monkeypatch.setenv(KEY, "off")
    _, seen = _import_hook(hook)
    assert seen[KEY] == "off"


@pytest.mark.parametrize("hook", ["session-start", "agent-route", "stop-enforce", "auto-route"])
def test_hooks_filter_the_project_dotenv(home, hook):
    """session-start and agent-route used to trust the project .env wholesale."""
    _env(home.cwd / ".env", "PYTHONPATH=/evil\nLLM_ROUTER_FROM_PROJECT=ok\n")
    _, seen = _import_hook(hook)
    assert seen.get("PYTHONPATH") != "/evil"
    if hook != "stop-enforce":  # stop-enforce never read the project .env
        assert seen.get("LLM_ROUTER_FROM_PROJECT") == "ok"


@pytest.mark.parametrize("hook", ["session-start", "agent-route", "stop-enforce", "auto-route"])
def test_hooks_fall_back_to_user_files_only_without_the_package(home, hook, monkeypatch):
    """A plugin bundle run by a bare interpreter cannot import llm_router."""
    monkeypatch.setitem(sys.modules, "llm_router.env_loader", None)  # import raises
    _env(home.router / ".env", f"{KEY}=shadow\n")
    _env(home.cwd / ".env", "LLM_ROUTER_FROM_PROJECT=untrusted\n")
    _, seen = _import_hook(hook)
    assert seen.get(KEY) == "shadow"
    assert "LLM_ROUTER_FROM_PROJECT" not in seen


def test_session_start_loader_still_accepts_an_explicit_target(home):
    mod, _ = _import_hook("session-start")
    _env(home.router / ".env", "LLM_ROUTER_FROM_HOME=here\n")
    target: dict[str, str] = {"PRESET": "keep"}
    mod._load_dotenv(target)
    assert target["LLM_ROUTER_FROM_HOME"] == "here"
    assert target["PRESET"] == "keep"
    assert "LLM_ROUTER_FROM_HOME" not in os.environ
