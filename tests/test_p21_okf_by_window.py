"""P21: the OKF ceiling follows the local window (LLM_ROUTER_OKF_BY_WINDOW, default off).

Offline: retrieval is faked; only the ceiling arithmetic and the server's choice of ceiling run.
"""
from __future__ import annotations

import pytest

from llm_router import context_injection, local_models
from llm_router.local_agent import DEFAULT_MAX_PROMPT_TOKENS, LocalAgentConfig
from llm_router.local_agent.compact import estimate_tokens
from llm_router.proxy import local_mode, okf_context
from llm_router.proxy import server as ps

BLOCK_CHARS = 4000  # ~1000 tokens, within MAX_BLOCK_TOKENS


def _req(chars: int) -> dict:
    return {"system": "S", "messages": [{"role": "user", "content": "x" * chars}]}


@pytest.fixture
def fake_retrieval(monkeypatch):
    monkeypatch.setattr(context_injection, "enabled", lambda: True)
    monkeypatch.setattr(okf_context, "session_scope", lambda body: "/repo")
    monkeypatch.setattr(okf_context, "tier_text", lambda body: "fix reconcile_invoice")
    monkeypatch.setattr(
        context_injection, "inject_system_prompt",
        lambda *a, **k: "<knowledge_context>" + "k" * BLOCK_CHARS + "</knowledge_context>")


@pytest.fixture
def window(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_OKF_BY_WINDOW", raising=False)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", "qwen3-coder:30b")
    monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", "131072")
    assert local_models.raised_window() == 131072


def _cfg(max_prompt=5000):
    return ps.ProxyConfig(upstream="http://127.0.0.1:9", local_agent=LocalAgentConfig(max_prompt_tokens=max_prompt))


def test_flag_off_ceiling_is_the_agent_target(window):
    assert ps.okf_ceiling(_cfg(4321)) == (4321, "agent")
    assert ps.okf_ceiling(ps.ProxyConfig(upstream="http://127.0.0.1:9")) == (DEFAULT_MAX_PROMPT_TOKENS, "agent")


def test_flag_on_ceiling_is_the_window_prompt_cap(window, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    ceiling, src = ps.okf_ceiling(_cfg(4321))
    assert src == "window" and ceiling == local_mode.prompt_cap_tokens() > 100_000


def test_flag_on_without_raised_window_uses_the_base_cap(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_WINDOW_STEP", raising=False)
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "on")
    assert ps.okf_ceiling(_cfg()) == (local_mode.PROMPT_CAP_TOKENS, "window")


def test_a_20k_token_request_is_over_budget_off_and_gets_a_block_on(window, fake_retrieval, monkeypatch):
    send = _req(80_000)  # ~20k tokens
    assert 19_000 < estimate_tokens(send) < 21_000
    ceiling, _ = ps.okf_ceiling(_cfg())
    out, info = okf_context.attach(send, send, ceiling_tokens=ceiling)
    assert info["reason"] == okf_context.REASON_NO_ROOM and out is send

    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    ceiling, _ = ps.okf_ceiling(_cfg())
    out, info = okf_context.attach(send, send, ceiling_tokens=ceiling)
    assert info["injected"] is True and "<knowledge_context>" in out["system"]
    assert info["tokens"] <= okf_context.MAX_BLOCK_TOKENS
    assert estimate_tokens(out) <= ceiling


def test_block_cap_still_holds_with_a_huge_ceiling(fake_retrieval, monkeypatch):
    monkeypatch.setattr(context_injection, "inject_system_prompt",
                        lambda *a, **k: "<knowledge_context>" + "k" * 40_000 + "</knowledge_context>")
    out, info = okf_context.attach(_req(100), _req(100), ceiling_tokens=120_000)
    assert info["injected"] is False and info["reason"] == okf_context.REASON_OVER_BUDGET


@pytest.mark.parametrize("gap", [10, 150, 199, 200, 400, 1_300, 5_000])
def test_never_exceeds_the_cap_at_the_boundary(window, fake_retrieval, monkeypatch, gap):
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    cap, _ = ps.okf_ceiling(_cfg())
    send = _req(1)
    send["messages"][0]["content"] = "x" * (4 * (cap - gap - estimate_tokens({"system": "S", "messages": [{"role": "user", "content": ""}]})))
    assert abs((cap - estimate_tokens(send)) - gap) <= 2
    out, info = okf_context.attach(send, send, ceiling_tokens=cap)
    assert estimate_tokens(out) <= cap
    if gap <= 10:
        assert info["injected"] is False and info["reason"] == okf_context.REASON_NO_ROOM
    if gap >= 1_300:
        assert info["injected"] is True


def test_row_fields_in_both_modes(window, monkeypatch):
    for flag, src in (("", "agent"), ("1", "window")):
        monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", flag)
        ceiling, got = ps.okf_ceiling(_cfg())
        assert got == src and isinstance(ceiling, int) and ceiling > 0


# ── end to end: the ledger row says which ceiling decided ─────────────────────
import json  # noqa: E402

from tests.test_proxy_okf_context import (  # noqa: E402,F401  (fixtures are used by name)
    _app, _Backend, _post, _repo, _request, _Upstream, home, policy,
)


@pytest.mark.parametrize("flag,src", [("", "agent"), ("1", "window")])
async def test_row_okf_records_the_ceiling(home, tmp_path, policy, monkeypatch, flag, src):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", flag)
    repo = _repo(tmp_path, "repo-a")
    await _post(_app(tmp_path, _Upstream(), _Backend()), _request(repo))
    row = json.loads((tmp_path / "proxy_calls.jsonl").read_text().splitlines()[-1])
    assert row["okf"]["ceiling_src"] == src and row["okf"]["ceiling_tokens"] > 0
