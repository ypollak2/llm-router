"""PLAN v16 P2.8 task 2: one runtime_offer() text, installs only on approval."""

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
    monkeypatch.setattr(ro, "runtime_offer", lambda system=None: SENTINEL)


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
    assert "llm_setup" in text and "GEMINI_API_KEY" in text and "LM Studio" in text


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
    runner = FakeRunner(code=3)
    out = ro.install_runtime(confirm=True, runner=runner, system="Darwin")
    assert runner.calls == ["brew install ollama"]
    assert "brew install ollama" in out and "3" in out


def test_install_runner_exception_is_reported_not_raised():
    runner = FakeRunner(exc=RuntimeError("boom"))
    out = ro.install_runtime(confirm=True, runner=runner, system="Windows")
    assert len(runner.calls) == 1 and "boom" in out


def test_llm_setup_install_runtime(monkeypatch):
    from llm_router.tools import setup

    runner = FakeRunner()
    monkeypatch.setattr(setup, "_RUNTIME_RUNNER", runner)
    asyncio.run(setup.llm_setup(action="install_runtime"))
    assert runner.calls == []
    out = asyncio.run(setup.llm_setup(action="install_runtime", confirm=True))
    assert len(runner.calls) == 1 and "exit code 0" in out


def test_llm_setup_unknown_action_lists_install_runtime():
    from llm_router.tools import setup

    assert "install_runtime" in asyncio.run(setup.llm_setup(action="nope"))


def _detect(*, binaries=(), paths=(), urls=()):
    return ro.detect_runtimes(
        which=lambda n: f"/bin/{n}" if n in binaries else None,
        path_exists=lambda p: p in paths,
        http_ok=lambda u: u in urls,
    )


def test_detect_each_runtime():
    assert _detect(binaries=("ollama",)) == ["ollama"]
    assert _detect(urls=("http://localhost:11434/api/tags",)) == ["ollama"]
    assert _detect(paths=("/Applications/LM Studio.app",)) == ["lmstudio"]
    assert _detect(urls=("http://localhost:1234/v1/models",)) == ["lmstudio"]
    assert _detect(binaries=("mlx_lm.generate",)) == ["mlx"]
    assert _detect(binaries=("llama-server",)) == ["llamacpp"]
    assert _detect(urls=("http://localhost:8080/health",)) == ["llamacpp"]


def test_detect_nothing_and_never_raises():
    assert _detect() == []

    def boom(*_a):
        raise OSError("x")

    assert ro.detect_runtimes(which=boom, path_exists=boom, http_ok=boom) == []


# ── by symbol: each of the six sites takes its text from runtime_offer() ──────


def test_quickstart_site(sentinel, monkeypatch, capsys):
    from llm_router import quickstart

    monkeypatch.setattr(quickstart, "_ask_yes", lambda *a, **k: False)
    monkeypatch.setattr(quickstart, "_has_any_api_key", lambda: False)
    monkeypatch.setattr(quickstart, "_has_ollama", lambda: False)
    monkeypatch.setattr(quickstart, "detect_hosts", lambda: ["claude"])
    quickstart.main()
    assert SENTINEL in capsys.readouterr().out


def _onboard(monkeypatch, tmp_path, *, detect, answers, runner, env_keys=()):
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

    onboard._run_onboard(input_fn=input_fn, runner=runner, detect=lambda: detect)


def test_onboard_warn_message_site(sentinel, monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    # key present, no runtime: warn line, flow continues, hooks install is stubbed out
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=True: [])
    _onboard(monkeypatch, tmp_path, detect=[], answers=[], runner=runner, env_keys=("GEMINI_API_KEY",))
    assert SENTINEL in capsys.readouterr().out
    assert runner.calls == []


def test_onboard_final_message_site_declined(sentinel, monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    _onboard(monkeypatch, tmp_path, detect=[], answers=["n"], runner=runner)
    out = capsys.readouterr().out
    assert out.count(SENTINEL) == 2  # warn line and final message
    assert runner.calls == []


def test_onboard_eof_never_installs(sentinel, monkeypatch, tmp_path):
    runner = FakeRunner()
    _onboard(monkeypatch, tmp_path, detect=[], answers=[], runner=runner)
    assert runner.calls == []


def test_onboard_yes_installs_once(monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(ro, "_system_key", lambda system=None: "linux")
    _onboard(monkeypatch, tmp_path, detect=[], answers=["y"], runner=runner)
    assert runner.calls == [ro.OLLAMA_INSTALL_COMMANDS["linux"]]


def test_onboard_other_runtime_counts_as_local_backend(monkeypatch, tmp_path, capsys):
    runner = FakeRunner()
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=True: [])
    _onboard(monkeypatch, tmp_path, detect=["lmstudio"], answers=[], runner=runner)
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
