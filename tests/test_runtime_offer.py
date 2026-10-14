"""PLAN v16 P2.8 task 2: one runtime_offer() text, installs only on a human's approval."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import runtime_offer as ro

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL-RUNTIME-OFFER-7731"
P28B = r"install[^[:alnum:]]{1,3}(from )?ollama|brew install ollama|ollama\.(com|ai)"


class FakeRunner:
    def __init__(self, code: int = 0, exc: Exception | None = None):
        self.calls: list[str] = []
        self.code, self.exc = code, exc

    def __call__(self, command: str) -> int:
        self.calls.append(command)
        if self.exc:
            raise self.exc
        return self.code


@pytest.fixture
def sentinel(monkeypatch):
    monkeypatch.setattr(ro, "runtime_offer", lambda system=None, **_k: SENTINEL)


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
def test_offer_has_exactly_the_os_command(system):
    text = ro.runtime_offer(system)
    key = system.lower()
    assert ro.OLLAMA_INSTALL_COMMANDS[key] in text
    for other, cmd in ro.OLLAMA_INSTALL_COMMANDS.items():
        if other != key:
            assert cmd not in text


def test_offer_unknown_os_lists_all_and_mentions_alternatives():
    text = ro.runtime_offer("Plan9")
    for cmd in ro.OLLAMA_INSTALL_COMMANDS.values():
        assert cmd in text
    assert "llm-router onboard" in text and "GEMINI_API_KEY" in text and "LM Studio" in text
    assert "llm_setup" not in text and "confirm=True" not in text


def test_offer_without_api_key_line():
    text = ro.runtime_offer("Linux", api_keys=False)
    assert "GEMINI_API_KEY" not in text and ro.OLLAMA_INSTALL_COMMANDS["linux"] in text


def test_install_without_confirm_never_runs():
    runner = FakeRunner()
    assert ro.install_runtime(runner=runner, system="Linux") == ro.runtime_offer("Linux")
    assert ro.install_runtime(confirm=False, runner=runner, system="Linux")
    assert runner.calls == []


def test_install_confirm_must_be_literally_true():
    runner = FakeRunner()
    ro.install_runtime(confirm="yes", runner=runner, system="Linux")  # type: ignore[arg-type]
    assert runner.calls == []


def test_install_confirm_runs_once_with_os_command():
    runner = FakeRunner(code=0)
    out = ro.install_runtime(confirm=True, runner=runner, system="Darwin", which=lambda n: "/b/ollama")
    assert runner.calls == ["brew install ollama"]
    assert out.startswith("Installed Ollama")


def test_install_nonzero_exit_is_a_failure():
    out = ro.install_runtime(confirm=True, runner=FakeRunner(code=3), system="Darwin",
                             which=lambda n: "/b/ollama")
    assert out.startswith("Install failed") and "code 3" in out


def test_install_exit_zero_without_binary_is_not_success():
    # `curl ... | sh` with a failed download used to exit 0 and read as installed.
    out = ro.install_runtime(confirm=True, runner=FakeRunner(code=0), system="Linux",
                             which=lambda n: None)
    assert "still not on PATH" in out and not out.startswith("Installed")


def test_default_runner_uses_pipefail_for_a_pipe(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["shell"] = argv, kw["shell"]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ro.subprocess, "run", fake_run)
    monkeypatch.setattr(ro.shutil, "which", lambda n: "/bin/bash" if n == "bash" else None)
    ro._default_runner(ro.OLLAMA_INSTALL_COMMANDS["linux"])
    assert seen["argv"][:3] == ["bash", "-o", "pipefail"] and seen["shell"] is False
    ro._default_runner("brew install ollama")
    assert seen["argv"] == "brew install ollama" and seen["shell"] is True
    # Fail-closed check of the real thing: a failed producer fails the pipe.
    if shutil.which("bash"):
        monkeypatch.undo()
        assert ro._default_runner("false | cat") != 0


def test_install_runner_exception_is_reported_not_raised():
    runner = FakeRunner(exc=RuntimeError("boom"))
    out = ro.install_runtime(confirm=True, runner=runner, system="Windows")
    assert len(runner.calls) == 1 and "boom" in out


def test_llm_setup_install_runtime_never_runs(monkeypatch):
    # A flag the model sets is not a human's approval: the MCP tool only shows
    # the command, and has no parameter that would run it.
    import inspect

    from llm_router.tools import setup

    calls = []
    monkeypatch.setattr(ro, "_default_runner", lambda c: calls.append(c) or 0)
    monkeypatch.setattr(ro, "install_runtime", lambda **k: calls.append(k) or "")
    out = asyncio.run(setup.llm_setup(action="install_runtime"))
    assert calls == []
    assert ro.runtime_offer() in out and "not run from a tool call" in out
    assert "confirm" not in inspect.signature(setup.llm_setup).parameters


def test_llm_setup_unknown_action_lists_install_runtime():
    from llm_router.tools import setup

    assert "install_runtime" in asyncio.run(setup.llm_setup(action="nope"))


def _status(*, binaries=(), paths=(), urls=()):
    return ro.runtime_status(
        which=lambda n: f"/bin/{n}" if n in binaries else None,
        path_exists=lambda p: p in paths,
        http_ok=lambda u: u in urls,
    )


OLLAMA_URL = "http://localhost:11434/api/tags"
LMS_URL = "http://localhost:1234/v1/models"
P8080 = "http://localhost:8080/v1/models"


@pytest.fixture(autouse=True)
def _default_ports(monkeypatch):
    for key in ("OLLAMA", "LMSTUDIO", "LLAMACPP", "MLX"):
        monkeypatch.delenv(f"LLM_ROUTER_LOCAL_{key}_PORT", raising=False)


def test_detect_each_runtime():
    assert _status(binaries=("ollama",)) == {"ollama": "installed"}
    assert _status(urls=(OLLAMA_URL,)) == {"ollama": "running"}
    assert _status(paths=("/Applications/LM Studio.app",)) == {"lmstudio": "installed"}
    assert _status(urls=(LMS_URL,)) == {"lmstudio": "running"}
    assert _status(binaries=("mlx_lm.generate",)) == {"mlx": "installed"}
    assert _status(binaries=("mlx_lm.server",), urls=(P8080,)) == {"mlx": "running"}
    assert _status(binaries=("llama-server",)) == {"llamacpp": "installed"}
    assert _status(urls=(P8080,)) == {"llamacpp": "running"}
    assert ro.detect_runtimes(which=lambda n: None, path_exists=lambda p: False,
                              http_ok=lambda u: u == OLLAMA_URL) == ["ollama"]


def test_a_bare_health_endpoint_on_8080_is_not_a_runtime():
    # Any dev server answers /health; only an OpenAI /v1/models answer counts.
    assert _status(urls=("http://localhost:8080/health",)) == {}


def test_detect_honours_the_port_overrides(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_LMSTUDIO_PORT", "5000")
    assert _status(urls=("http://localhost:5000/v1/models",)) == {"lmstudio": "running"}
    assert _status(urls=(LMS_URL,)) == {}


def test_detect_nothing_and_never_raises():
    assert _status() == {}

    def boom(*_a):
        raise OSError("x")

    assert ro.runtime_status(which=boom, path_exists=boom, http_ok=boom) == {}


# ── by symbol: each of the six sites takes its text from runtime_offer() ──────


def test_quickstart_site(sentinel, monkeypatch, capsys):
    from llm_router import quickstart

    monkeypatch.setattr(quickstart, "_ask_yes", lambda *a, **k: False)
    monkeypatch.setattr(quickstart, "_has_any_api_key", lambda: False)
    monkeypatch.setattr(quickstart, "_has_ollama", lambda: False)
    monkeypatch.setattr(quickstart, "detect_hosts", lambda: ["claude"])
    quickstart.main()
    assert SENTINEL in capsys.readouterr().out


def _onboard(monkeypatch, tmp_path, *, detect, answers, runner, env_keys=(), tty=True):
    from llm_router.commands import onboard

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "PERPLEXITY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k in env_keys:
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(onboard.shutil, "which", lambda n: None)
    it = iter(answers)

    def input_fn(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    onboard._run_onboard(input_fn=input_fn, runner=runner, detect=lambda: detect,
                         interactive=lambda: tty)


def test_onboard_warn_message_site(sentinel, monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    # key present, no runtime: warn line, flow continues, hooks install is stubbed out
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=True: [])
    _onboard(monkeypatch, tmp_path, detect={}, answers=[], runner=runner, env_keys=("GEMINI_API_KEY",))
    assert SENTINEL in capsys.readouterr().out
    assert runner.calls == []


def test_onboard_final_message_site_declined(sentinel, monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    _onboard(monkeypatch, tmp_path, detect={}, answers=["n"], runner=runner)
    out = capsys.readouterr().out
    assert out.count(SENTINEL) == 1  # once, not again under "No routing backends"
    assert runner.calls == []


def test_onboard_eof_never_installs(sentinel, monkeypatch, tmp_path):
    runner = FakeRunner()
    _onboard(monkeypatch, tmp_path, detect={}, answers=[], runner=runner)
    assert runner.calls == []


def test_onboard_piped_yes_never_installs(sentinel, monkeypatch, tmp_path, capsys):
    # `yes | llm-router onboard`: stdin is not a terminal, so no prompt and no run.
    runner = FakeRunner()
    _onboard(monkeypatch, tmp_path, detect={}, answers=["y"] * 20, runner=runner, tty=False)
    assert runner.calls == []
    assert "Run the install command" not in capsys.readouterr().out


def test_onboard_yes_installs_once(monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(ro, "_system_key", lambda system=None: "linux")
    _onboard(monkeypatch, tmp_path, detect={}, answers=["y"], runner=runner)
    assert runner.calls == [ro.OLLAMA_INSTALL_COMMANDS["linux"]]


def test_onboard_installed_but_stopped_ollama_is_not_offered_an_install(
    sentinel, monkeypatch, tmp_path, capsys
):
    runner = FakeRunner()
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=True: [])
    _onboard(monkeypatch, tmp_path, detect={"ollama": "installed"}, answers=[], runner=runner)
    out = capsys.readouterr().out
    assert "ollama serve" in out and SENTINEL not in out and "budget" in out
    assert runner.calls == []


def test_onboard_stopped_other_runtime_is_not_a_backend(sentinel, monkeypatch, tmp_path, capsys):
    # An LM Studio install with its server off routes nothing: not budget.
    runner = FakeRunner()
    _onboard(monkeypatch, tmp_path, detect={"lmstudio": "installed"}, answers=["n"], runner=runner)
    out = capsys.readouterr().out
    assert "not running: lmstudio" in out and "No routing backends found" in out
    assert "budget" not in out and runner.calls == []


def test_onboard_other_runtime_counts_as_local_backend(monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=True: [])
    _onboard(monkeypatch, tmp_path, detect={"lmstudio": "running"}, answers=[], runner=runner)
    out = capsys.readouterr().out
    assert "lmstudio" in out and "budget" in out and "Needs:" in out
    assert runner.calls == []


def _empty_config(mock_config):
    from llm_router.types import RoutingProfile

    c = mock_config.return_value
    c.llm_router_profile = RoutingProfile.BUDGET
    c.llm_router_monthly_budget = 0
    c.available_providers = set()
    c.compaction_mode = "off"
    c.llm_router_agentic_model = ""
    c.compaction_threshold = 4000
    c.llm_router_claude_subscription = False
    c.llm_router_gemini_subscription = False
    c.ollama_models_for_profile.return_value = []
    c.all_ollama_models.return_value = []


def test_route_and_call_site(sentinel):
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    with (
        patch("llm_router.router.get_config") as mock_config,
        patch("llm_router.router.get_tracker"),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0),
        patch("llm_router.router.get_model_chain", return_value=["openai/gpt-4o"]),
    ):
        _empty_config(mock_config)
        with pytest.raises(ValueError) as exc:
            asyncio.run(route_and_call(TaskType.QUERY, "test"))
    assert SENTINEL in str(exc.value)
    assert "• " + SENTINEL not in str(exc.value)  # its own block, not inside a bullet


def test_route_and_call_with_ollama_configured_has_no_install_offer(sentinel):
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    with (
        patch("llm_router.router.get_config") as mock_config,
        patch("llm_router.router.get_tracker"),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0),
        patch("llm_router.router.get_model_chain", return_value=["openai/gpt-4o"]),
    ):
        _empty_config(mock_config)
        mock_config.return_value.available_providers = {"ollama"}
        with pytest.raises(ValueError) as exc:
            asyncio.run(route_and_call(TaskType.QUERY, "test"))
    assert "No providers available" in str(exc.value) and SENTINEL not in str(exc.value)


def test_route_and_stream_site(sentinel):
    from llm_router.router import route_and_stream
    from llm_router.types import TaskType

    async def consume():
        async for _ in route_and_stream(TaskType.QUERY, "test"):
            pass

    with (
        patch("llm_router.router.get_config") as mock_config,
        patch("llm_router.router.get_model_chain", return_value=[]),
    ):
        _empty_config(mock_config)
        with pytest.raises(ValueError) as exc:
            asyncio.run(consume())
    assert SENTINEL in str(exc.value)


def test_shell_hook_site(tmp_path):
    bash, py = shutil.which("bash"), shutil.which("python3")
    if not bash or not py:
        pytest.skip("bash/python3 not found")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, target in (("bash", bash), ("python3", py)):
        (bindir / name).symlink_to(target)
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "LLM_ROUTER_HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
    }
    hook = ROOT / "src" / "llm_router" / "hooks" / "start-ollama.sh"
    proc = subprocess.run(
        [str(bindir / "bash"), str(hook)], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 1
    assert ro.runtime_offer(None) in proc.stderr


# ── by text: the P2.8-b grep ─────────────────────────────────────────────────


def test_grep_finds_only_allowlist_and_the_offer_module():
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        pytest.skip("not a git checkout")
    proc = subprocess.run(
        ["git", "grep", "-nEi", P28B, "--", "src/", "scripts/", "hooks/", ".claude/"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, env={**os.environ},
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln]
    allow = ("scripts/bench/m18_r3.py:", "src/llm_router/qa_policy.py:")
    remaining = []
    for ln in lines:
        if ln.startswith("src/llm_router/runtime_offer.py:") or ln.startswith(allow):
            continue
        m = re.match(r"src/llm_router/router\.py:\d+:\s*#", ln)
        if m:
            continue
        remaining.append(ln)
    assert len(lines) >= 3, "the grep must still see the allowlisted hits"
    assert remaining == []
