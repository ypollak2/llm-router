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


# ── real estimators: admission cap AND the backend overflow guard ─────────────
from llm_router import local_context_guard as lcg  # noqa: E402

NUM_CTX = 131072


def _filled(target_admit_tokens: int) -> dict:
    """A request whose admission estimate (local_mode.estimate_prompt_tokens on the Ollama payload)
    is just under target, built from prose-like filler (bisection on the real estimator)."""
    def mk(n):
        return {"system": "S", "messages": [{"role": "user", "content": "fix the reconcile_invoice bug " * n}]}
    lo, hi = 1, 40_000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if local_mode.estimate_prompt_tokens(ps._okf_payload(mk(mid), NUM_CTX)) <= target_admit_tokens:
            lo = mid
        else:
            hi = mid - 1
    return mk(lo)


def _measure(send):
    p = ps._okf_payload(send, NUM_CTX)
    return local_mode.estimate_prompt_tokens(p), lcg.estimate_payload_tokens(p)


@pytest.mark.parametrize("gap", [10, 500, 3_000, 10_000, 20_000, 40_000])
def test_block_never_pushes_past_the_cap_or_the_overflow_guard(window, fake_retrieval, monkeypatch, gap):
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    cap = local_mode.prompt_cap_tokens()
    budget = lcg.effective_window(num_ctx=NUM_CTX)[0] - lcg.DEFAULT_RESERVED_OUTPUT_TOKENS
    send = _filled(cap - gap)
    admit, guard = _measure(send)
    assert admit <= cap
    ceiling, src = ps.okf_ceiling(_cfg(), send, NUM_CTX)
    assert src == "window" and ceiling <= cap
    out, info = okf_context.attach(send, send, ceiling_tokens=ceiling, fits=ps.okf_fits(cap, NUM_CTX))
    admit2, guard2 = _measure(out)
    assert admit2 <= cap, (gap, admit, admit2, info)
    if guard <= budget:  # a request the guard already refuses is not made worse by this check
        assert guard2 <= budget, (gap, guard, guard2, info)
        lcg.check_overflow(ps._okf_payload(out, NUM_CTX), num_ctx=NUM_CTX)  # does not raise
    if not info["injected"]:
        assert out is send
    if gap >= 10_000:
        assert info["injected"] is True, info  # the test must not pass only because nothing was attached


def test_the_ceiling_only_shrinks_and_hits_zero_room_at_the_edge(window, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    cap = local_mode.prompt_cap_tokens()
    small = _req(400)
    assert ps.okf_ceiling(_cfg(), small, NUM_CTX)[0] <= cap
    edge = _filled(cap - 5)
    ceiling, _ = ps.okf_ceiling(_cfg(), edge, NUM_CTX)
    assert ceiling - estimate_tokens(edge) < okf_context.MIN_ROOM_TOKENS


@pytest.mark.parametrize("tokens", [5_000, 12_000, 20_000])
def test_requests_in_the_old_over_budget_range_get_a_block(window, fake_retrieval, monkeypatch, tokens):
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    cap = local_mode.prompt_cap_tokens()
    send = _filled(tokens)
    ceiling, _ = ps.okf_ceiling(_cfg(), send, NUM_CTX)
    out, info = okf_context.attach(send, send, ceiling_tokens=ceiling, fits=ps.okf_fits(cap, NUM_CTX))
    assert info["injected"] is True, info
    off, off_info = okf_context.attach(send, send, ceiling_tokens=5000)
    assert off_info["injected"] is False


# ── end to end: the ledger row says which ceiling decided, and attach got it ──
import json  # noqa: E402

from tests.test_proxy_okf_context import (  # noqa: E402,F401  (fixtures are used by name)
    _app, _Backend, _post, _repo, _request, _Upstream, home, policy,
)


@pytest.mark.parametrize("flag,src", [("", "agent"), ("1", "window")])
async def test_row_okf_records_the_ceiling_and_attach_received_it(
        home, tmp_path, policy, monkeypatch, flag, src):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", flag)
    if flag:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", "qwen3-coder:30b")
        monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", "131072")
    seen = {}
    real = okf_context.attach

    def spy(send, body, **kw):
        seen.update(kw)
        return real(send, body, **kw)

    monkeypatch.setattr(okf_context, "attach", spy)
    repo = _repo(tmp_path, "repo-a")
    await _post(_app(tmp_path, _Upstream(), _Backend()), _request(repo))
    row = json.loads((tmp_path / "proxy_calls.jsonl").read_text().splitlines()[-1])
    assert row["okf"]["ceiling_src"] == src and row["okf"]["ceiling_tokens"] == seen["ceiling_tokens"] > 0
    assert (seen["fits"] is not None) == (src == "window")
    if src == "window":
        assert row["okf"]["ceiling_tokens"] <= local_mode.prompt_cap_tokens()


async def test_a_failing_ceiling_setup_is_failopen_and_the_step_is_still_served_locally(
        home, tmp_path, policy, monkeypatch):  # noqa: F811
    from llm_router import failopen

    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    seen = {}
    real = okf_context.attach

    def boom(*a, **k):
        raise RuntimeError("estimator exploded")

    def spy(send, body, **kw):
        seen.update(kw)
        return real(send, body, **kw)

    monkeypatch.setattr(ps, "okf_ceiling", boom)
    monkeypatch.setattr(okf_context, "attach", spy)
    codes = []
    monkeypatch.setattr(failopen, "record", lambda code, *a, **k: codes.append(code))
    repo = _repo(tmp_path, "repo-a")
    backend, up = _Backend(), _Upstream()
    await _post(_app(tmp_path, up, backend), _request(repo))
    assert up.requests == [] and len(backend.calls) == 1, "served locally, not handed to Claude"
    assert "LR-FO-PROXY-OKF-CEILING" in codes
    assert seen["fits"] is None
    row = json.loads((tmp_path / "proxy_calls.jsonl").read_text().splitlines()[-1])
    assert row["okf"]["ceiling_src"] == "agent_fallback"
