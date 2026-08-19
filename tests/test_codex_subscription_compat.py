"""Codex CLI invocation must match what ChatGPT-subscription auth accepts.

Production failure mode that motivated this fix:

1. Daemon called Codex with ``-m gpt-4o-mini`` (per the legacy default
   chain) -- Codex CLI v0.133 rejects it on ChatGPT auth with HTTP 400
   "The 'gpt-4o-mini' model is not supported when using Codex with a
   ChatGPT account". Process exits non-zero.
2. Daemon called Codex with ``-C <non-git-cwd>`` and no
   ``--skip-git-repo-check`` -- Codex CLI refuses with "Not inside a
   trusted directory..." and exits non-zero.
3. Router logs both as ``Codex exited 1`` (stderr suppressed) and falls
   through to paid providers, silently incurring cost.

Pins:

1. Default model list is ChatGPT-subscription-safe.
2. Env override extends the list for API-tier users.
3. Default invocation includes ``--skip-git-repo-check``.
4. Routing profile table stays in sync with the supported set.
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router import codex_agent
from llm_router.codex_agent import (
    CODEX_MODELS,
    CodexResult,
    _load_codex_models,
    run_codex,
)


# 1. Default list is ChatGPT-subscription-safe


def test_default_codex_models_is_chatgpt_subscription_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_ROUTER_CODEX_MODELS", raising=False)
    assert _load_codex_models() == ["gpt-5.5", "gpt-5.4"]


def test_default_excludes_known_rejected_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_ROUTER_CODEX_MODELS", raising=False)
    rejected = {"gpt-4o", "gpt-4o-mini", "o3", "o4-mini"}
    assert rejected.isdisjoint(set(_load_codex_models()))


def test_module_constant_matches_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_CODEX_MODELS", raising=False)
    assert CODEX_MODELS == _load_codex_models()


# 2. Env override


def test_env_override_replaces_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_CODEX_MODELS", "gpt-5.5,o3,gpt-4o")
    assert _load_codex_models() == ["gpt-5.5", "o3", "gpt-4o"]


def test_env_override_strips_whitespace_and_drops_empties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CODEX_MODELS", "  gpt-5.5 , , gpt-5.4 ,")
    assert _load_codex_models() == ["gpt-5.5", "gpt-5.4"]


def test_empty_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_CODEX_MODELS", "   ")
    assert _load_codex_models() == ["gpt-5.5", "gpt-5.4"]


# 3. Invocation includes --skip-git-repo-check


class _FakeProc:
    returncode = 0

    async def wait(self) -> None:
        pass

    @property
    def stdout(self):
        return self._stdout_gen()

    @property
    def stderr(self):
        return self._stderr_gen()

    async def _stdout_gen(self):
        # Emit one plain-text line so run_codex collects content
        import json as _json
        yield _json.dumps({"type": "item.completed", "item": {"text": "OK"}}).encode() + b"\n"

    async def _stderr_gen(self):
        return
        yield  # make it an async generator


@pytest.fixture
def _mock_codex_invoke(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    async def _fake_invoke(*args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/fake/codex")
    monkeypatch.setattr(
        codex_agent.asyncio, "create_subprocess_exec", _fake_invoke,
    )
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    return captured


def test_run_codex_args_include_skip_git_repo_check(
    _mock_codex_invoke: dict, tmp_path,
) -> None:
    result: CodexResult = asyncio.run(
        run_codex("hello", working_dir=str(tmp_path)),
    )
    assert result.success, (
        f"Mocked run did not succeed: exit_code={result.exit_code} "
        f"content={result.content!r}"
    )
    assert "--skip-git-repo-check" in _mock_codex_invoke["args"]


def test_run_codex_default_model_is_gpt_5_5(
    _mock_codex_invoke: dict, tmp_path,
) -> None:
    asyncio.run(run_codex("hello", working_dir=str(tmp_path)))
    args = _mock_codex_invoke["args"]
    m_idx = args.index("-m")
    assert args[m_idx + 1] == "gpt-5.5"


def test_run_codex_explicit_model_is_passed_through(
    _mock_codex_invoke: dict, tmp_path,
) -> None:
    asyncio.run(run_codex("hello", model="gpt-5.4", working_dir=str(tmp_path)))
    args = _mock_codex_invoke["args"]
    m_idx = args.index("-m")
    assert args[m_idx + 1] == "gpt-5.4"


# 4. Routing profile table cannot drift


def test_routing_profile_table_only_uses_supported_codex_models() -> None:
    from llm_router.tools.routing import _SELECT_AGENT_DEFAULT, _SELECT_AGENT_MAP

    supported = set(_load_codex_models())
    offenders: list[str] = []

    def _check(primary: str, model: str, label: str) -> None:
        if primary == "codex" and model not in supported:
            offenders.append(f"{label}: model={model!r}")

    for key, (p_agent, p_model, f_agent, f_model) in _SELECT_AGENT_MAP.items():
        _check(p_agent, p_model, f"{key} primary")
        _check(f_agent, f_model, f"{key} fallback")

    d_p_agent, d_p_model, d_f_agent, d_f_model = _SELECT_AGENT_DEFAULT
    _check(d_p_agent, d_p_model, "default primary")
    _check(d_f_agent, d_f_model, "default fallback")

    assert not offenders, (
        "Routing table requests Codex models outside the supported set "
        f"({supported}); these will fail with HTTP 400 on ChatGPT auth:\n  "
        + "\n  ".join(offenders)
    )
