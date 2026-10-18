"""AGT A.6: the framework adapters are thin ``base_url`` wrappers (no network, no framework installed).

Each adapter is exercised against a stand-in for its framework's module (``sys.modules``), so what is
asserted is exactly what llm-router controls: the gateway URL, the key, the model name and the
protocol. The real frameworks run in examples/ (CI: .github/workflows/examples.yml).
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import sys
import types
from pathlib import Path

import pytest

from llm_router.frameworks import _gateway

FRAMEWORK_DIR = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "frameworks"
GATEWAY = "http://127.0.0.1:17999"


@pytest.fixture(autouse=True)
def gateway_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_URL", GATEWAY + "/v1")
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))      # no gateway.token from a real state dir


class _Recorder:
    """A framework class: records its constructor arguments."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


def _fake_modules(monkeypatch, **modules):
    """Install stand-in modules: {"pkg.sub": {"Name": cls}}."""
    for dotted, attrs in modules.items():
        mod = types.ModuleType(dotted)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, dotted, mod)
        parent = dotted.rpartition(".")[0]
        if parent and parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))


def test_no_adapter_raises_not_implemented():
    """A.6-a: 0 NotImplementedError adapters ship. A scan, so a new stub cannot slip in."""
    files = sorted(FRAMEWORK_DIR.glob("*.py"))
    assert len(files) >= 8, files          # an empty glob would pass vacuously
    for path in files:
        assert "NotImplementedError" not in path.read_text(encoding="utf-8"), path.name
    assert not (FRAMEWORK_DIR / "hermes.py").exists()


@pytest.mark.parametrize("slug", ["langgraph", "crewai", "openai_agents", "claude_agent_sdk", "pydantic_ai"])
def test_every_adapter_module_names_its_example(slug):
    tree = ast.parse((FRAMEWORK_DIR / f"{slug}.py").read_text(encoding="utf-8"))
    assert f"examples/{slug}/" in ast.get_docstring(tree)


def test_urls_follow_the_configured_gateway(monkeypatch):
    assert _gateway.gateway_root() == GATEWAY
    assert _gateway.openai_base_url() == GATEWAY + "/v1"
    assert _gateway.anthropic_base_url() == GATEWAY
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_URL", GATEWAY)           # no /v1 given
    assert _gateway.openai_base_url() == GATEWAY + "/v1"


def test_api_key_is_the_token_when_set_else_a_placeholder(monkeypatch):
    assert _gateway.gateway_api_key() == _gateway.PLACEHOLDER_KEY
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", "tok-123")
    assert _gateway.gateway_api_key() == "tok-123"


@pytest.mark.parametrize("given,expected", [
    (None, "auto"), ("gpt-4o", "gpt-4o"),
    (types.SimpleNamespace(model_name="m1"), "m1"), (types.SimpleNamespace(model="m2"), "m2"),
])
def test_model_name(given, expected):
    assert _gateway.model_name(given) == expected


def test_model_name_refuses_an_object_it_cannot_read():
    with pytest.raises(TypeError, match="cannot read a model name"):
        _gateway.model_name(object())


def test_module_present_separates_installed_from_missing():
    assert _gateway.module_present("json") is True
    assert _gateway.module_present("llm_router_no_such_module_a6") is False


def test_langgraph_builds_a_streaming_chat_model_on_the_gateway(monkeypatch):
    from llm_router.frameworks.langgraph import LangGraphAdapter

    _fake_modules(monkeypatch, langchain_openai={"ChatOpenAI": _Recorder})
    model = LangGraphAdapter().wrap_model("gpt-4o")
    assert model.kwargs == {"model": "gpt-4o", "base_url": GATEWAY + "/v1",
                            "api_key": _gateway.PLACEHOLDER_KEY, "streaming": True}


def test_crewai_prefixes_the_provider_once(monkeypatch):
    from llm_router.frameworks.crewai import CrewAIAdapter

    _fake_modules(monkeypatch, crewai={"LLM": _Recorder})
    assert CrewAIAdapter().wrap_model("gpt-4o").kwargs["model"] == "openai/gpt-4o"
    assert CrewAIAdapter().wrap_model("openai/gpt-4o").kwargs["model"] == "openai/gpt-4o"
    kwargs = CrewAIAdapter().wrap_model().kwargs
    assert kwargs["model"] == "openai/auto" and kwargs["base_url"] == GATEWAY + "/v1"


def test_openai_agents_wraps_an_async_client_on_the_gateway(monkeypatch):
    from llm_router.frameworks.openai_agents import OpenAIAgentsAdapter

    _fake_modules(monkeypatch, agents={"OpenAIChatCompletionsModel": _Recorder},
                  openai={"AsyncOpenAI": _Recorder})
    model = OpenAIAgentsAdapter().wrap_model("gpt-4o")
    assert model.kwargs["model"] == "gpt-4o"
    assert model.kwargs["openai_client"].kwargs == {
        "base_url": GATEWAY + "/v1", "api_key": _gateway.PLACEHOLDER_KEY}


def test_pydantic_ai_builds_a_chat_model_over_a_gateway_provider(monkeypatch):
    from llm_router.frameworks.pydantic_ai import PydanticAiAdapter

    _fake_modules(monkeypatch,
                  **{"pydantic_ai.models.openai": {"OpenAIChatModel": _Recorder},
                     "pydantic_ai.providers.openai": {"OpenAIProvider": _Recorder}})
    model = PydanticAiAdapter().wrap_model("gpt-4o")
    assert model.args == ("gpt-4o",)
    assert model.kwargs["provider"].kwargs == {
        "base_url": GATEWAY + "/v1", "api_key": _gateway.PLACEHOLDER_KEY}


def test_claude_agent_sdk_sets_the_process_env_on_the_options(monkeypatch):
    from llm_router.frameworks.claude_agent_sdk import ClaudeAgentSdkAdapter

    @dataclasses.dataclass
    class Options:
        model: str | None = None
        env: dict = dataclasses.field(default_factory=dict)
        max_turns: int | None = None

    _fake_modules(monkeypatch, claude_agent_sdk={"ClaudeAgentOptions": Options})
    fresh = ClaudeAgentSdkAdapter().wrap_model("claude-haiku-4-5")
    assert fresh.model == "claude-haiku-4-5"
    assert fresh.env == {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_API_KEY": _gateway.PLACEHOLDER_KEY}

    mine = Options(env={"KEEP": "1"}, max_turns=3)
    out = ClaudeAgentSdkAdapter().wrap_model(mine)
    assert out.max_turns == 3 and out.model == "auto"
    assert out.env["KEEP"] == "1" and out.env["ANTHROPIC_BASE_URL"] == GATEWAY
    assert mine.env == {"KEEP": "1"}                                # the caller's options are not mutated


@pytest.mark.parametrize("module,cls,package", [
    ("langgraph", "LangGraphAdapter", "langchain-openai"),
    ("crewai", "CrewAIAdapter", "crewai"),
    ("openai_agents", "OpenAIAgentsAdapter", "openai-agents"),
    ("claude_agent_sdk", "ClaudeAgentSdkAdapter", "claude-agent-sdk"),
    ("pydantic_ai", "PydanticAiAdapter", "pydantic-ai-slim[openai]"),
])
def test_a_missing_framework_is_an_import_error_that_names_the_package(monkeypatch, module, cls, package):
    adapter = getattr(importlib.import_module(f"llm_router.frameworks.{module}"), cls)()
    for name in ("langchain_openai", "crewai", "agents", "openai", "claude_agent_sdk", "pydantic_ai",
                 "pydantic_ai.models.openai", "pydantic_ai.providers.openai"):
        monkeypatch.setitem(sys.modules, name, None)               # None => the import raises ImportError
    with pytest.raises(ImportError, match="pip install") as err:
        adapter.wrap_model("gpt-4o")
    assert package in str(err.value)


def test_langgraph_detect_agent_id_reads_the_node_name():
    from llm_router.frameworks.langgraph import LangGraphAdapter

    adapter = LangGraphAdapter()
    assert adapter.detect_agent_id({"metadata": {"langgraph_node": "plan"}}) == "plan"
    assert adapter.detect_agent_id({"metadata": {}}) is None
    assert adapter.detect_agent_id(object()) is None
