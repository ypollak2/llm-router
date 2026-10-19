"""P2.7: shared prefixes and the real MCP completion door."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_router import codex_agent, config, failopen, model_discovery, prefixes, prompt_key
from llm_router.proxy import tiers
from llm_router.tools import consolidated as door
from llm_router.types import BudgetExceededError


@pytest.mark.parametrize("name", (*prefixes.PREFIX_NAMES, "native"))
def test_parse(name):
    expected = "claude" if name == "native" else name
    for text in (f"{name}: hi", f" \t{name.upper()} \t:\n  hi  "):
        override = prefixes.parse(text)
        assert override == prefixes.Override(expected, "hi")
        assert override.is_claude == (expected in {"claude", "opus", "sonnet", "haiku"})
        assert override.forces_external == (expected in {"local", "codex"})
        assert prefixes.strip_prefix(text) == "hi"


@pytest.mark.parametrize("text", [None, 3, "", "hi", "gpt: x", "foo: x",
                                       "opus:", "local: \n ", "native:"])
def test_ignored(text):
    assert prefixes.parse(text) is None
    assert prefixes.strip_prefix(text) == text


def test_examples(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "private"))
    target = tmp_path / "private" / "examples.jsonl"
    secret = "private prompt never stored verbatim"
    assert prefixes.record_example(secret, "local", "ollama/test", "mcp", now=0)
    row, = [json.loads(line) for line in target.read_text().splitlines()]
    assert row == dict(text_sha=prompt_key.key(secret), forced="local",
                       decided="ollama/test", door="mcp", ts=0)
    assert secret not in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    line = json.dumps(dict(row, text_sha="x" * 100, ts=time.time())) + "\n"  # recent: 180-day pruning must not eat the filler
    target.write_text(line * (2 * 1024 * 1024 // len(line) + 2))
    before = target.stat().st_size
    assert prefixes.record_example("newest", "codex", "codex/test", "mcp", path=target)
    rows = target.read_text().splitlines()
    assert target.stat().st_size < before
    assert json.loads(rows[-1])["text_sha"] == prompt_key.key("newest")
    assert len(rows) == (before // len(line) + 1) // 2 + 1


def test_unwritable(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("unwritable")
    monkeypatch.setattr(prefixes.os, "open", denied)
    record = []
    monkeypatch.setattr(failopen, "record", lambda code, exc: record.append(code))
    assert not prefixes.record_example("secret", "local", "host", "mcp",
                                       path=tmp_path / "examples.jsonl")
    assert record == ["LR-FO-PREFIX-EXAMPLE"]


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(door, "_ledger_exit", AsyncMock())
    breaker = []
    monkeypatch.setattr(door, "_quality_breaker_block",
                        lambda *args: breaker.append(args) or None)
    calls = {}
    for name in ("query", "code", "analyze", "generate", "research"):
        calls[name] = AsyncMock(return_value="answer")
        monkeypatch.setattr(door, f"llm_{name}", calls[name])
    cfg = SimpleNamespace(llm_router_claude_subscription=True,
                          available_providers=["anthropic"])
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(tiers, "tier_model", lambda name: "claude-opus-test")
    monkeypatch.setattr(model_discovery, "available_ollama_models", lambda: ["qwen:7b"])
    monkeypatch.setattr(codex_agent, "is_codex_available", lambda: True)
    monkeypatch.setattr(codex_agent, "CODEX_MODELS", ["codex/test"])
    return SimpleNamespace(calls=calls, config=cfg, breaker=breaker,
                           path=tmp_path / "examples.jsonl", ctx=SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,decided,routed", [
    ("local: hi", "ollama/qwen:7b", True),
    ("codex: hi", "codex/test", True),
    ("claude: hi", "claude", False),
    ("native: hi", "claude", False),
    ("opus: hi", "host", False),
    ("gpt: hi", None, True),
])
async def test_mcp(mcp, prompt, decided, routed):
    result = await door.llm(prompt, mcp.ctx)
    assert sum(call.await_count for call in mcp.calls.values()) == int(routed)
    assert bool(mcp.breaker) == routed
    if routed:
        args, kwargs = mcp.calls["query"].await_args
        assert args == ("hi" if decided else prompt, mcp.ctx)
        assert kwargs.get("model") == decided
        if decided is None:
            assert "model" not in kwargs
    else:
        assert "\n" not in result and "not routed" in result
    if decided:
        row, = [json.loads(line) for line in mcp.path.read_text().splitlines()]
        assert row == dict(text_sha=prompt_key.key("hi"), forced=prefixes.parse(prompt).name,
                           decided=decided, door="mcp", ts=row["ts"])
        assert prompt not in mcp.path.read_text()
    else:
        assert not mcp.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["local", "codex"])
async def test_refusal(mcp, monkeypatch, name):
    monkeypatch.setattr(model_discovery, "available_ollama_models", lambda: [])
    monkeypatch.setattr(codex_agent, "is_codex_available", lambda: False)
    result = await door.llm(f"{name}: hi", mcp.ctx)
    assert "refused" in result and "\n" not in result
    assert not any(call.called for call in mcp.calls.values())
    assert not mcp.breaker
    row = json.loads(mcp.path.read_text())
    assert row["decided"] == ("refused:no_local" if name == "local" else "refused:no_codex")
    assert row["text_sha"] == prompt_key.key("hi")
    assert row["forced"] == name and row["door"] == "mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("task,target", [
    ("auto", "query"), ("code", "code"), ("analyze", "analyze"),
    ("generate", "generate"), ("research", "query"),
])
async def test_api_dispatch(mcp, task, target):
    mcp.config.llm_router_claude_subscription = False
    assert await door.llm("opus: hi", mcp.ctx, task=task) == "answer"
    call = mcp.calls[target]
    assert call.await_args.args == ("hi", mcp.ctx)
    assert call.await_args.kwargs["model"] == "anthropic/claude-opus-test"
    assert sum(c.await_count for c in mcp.calls.values()) == 1
    row = json.loads(mcp.path.read_text())
    assert row["decided"] == "anthropic/claude-opus-test"
    assert row["text_sha"] == prompt_key.key("hi") and row["forced"] == "opus"


@pytest.mark.asyncio
async def test_cap(mcp):
    mcp.config.llm_router_claude_subscription = False
    mcp.calls["query"].side_effect = BudgetExceededError("cap")
    with pytest.raises(BudgetExceededError, match="cap"):
        await door.llm("opus: hi", mcp.ctx)
    assert sum(c.await_count for c in mcp.calls.values()) == 1
    assert mcp.calls["query"].await_args.kwargs["model"] == "anthropic/claude-opus-test"
    assert json.loads(mcp.path.read_text())["decided"] == "anthropic/claude-opus-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parse", "lookup"])
async def test_resolution_fail_open(mcp, monkeypatch, failure):
    def broken(*args):
        raise RuntimeError("lookup failed")
    if failure == "parse":
        monkeypatch.setattr(prefixes, "parse", broken)
    else:
        monkeypatch.setattr(model_discovery, "available_ollama_models", broken)
    reply = await door.llm("local: hi", mcp.ctx)
    if failure == "parse":
        args, kwargs = mcp.calls["query"].await_args
        assert args == ("local: hi", mcp.ctx) and "model" not in kwargs
        assert not mcp.path.exists()
    else:
        # A failed local lookup reads as "no healthy local model": refused, never sent elsewhere.
        assert "refused" in reply and not mcp.calls["query"].called


@pytest.mark.asyncio
async def test_quality_breaker_still_blocks(mcp, monkeypatch):
    monkeypatch.setattr(door, "_quality_breaker_block", lambda *args: "blocked")
    assert await door.llm("local: hi", mcp.ctx) == "blocked"
    assert not any(c.called for c in mcp.calls.values())
    assert json.loads(mcp.path.read_text())["decided"] == "ollama/qwen:7b"


@pytest.mark.parametrize("text", [
    "please explain opus: why", "Summarize this.\nlocal: foo", "see the claude: haiku: note",
])
def test_prefix_is_anchored_at_prompt_start(text):
    # A prefix word later in the text is ordinary prose (kills .match -> .search).
    assert prefixes.parse(text) is None
    assert prefixes.strip_prefix(text) == text
