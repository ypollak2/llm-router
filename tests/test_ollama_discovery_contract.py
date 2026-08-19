"""Contract tests: Ollama model resolution must use dynamic discovery.

Verifies that _load_discovered_ollama_models() follows the documented
precedence order and never returns hardcoded stale model names when a
live source (env var or discovery.json) is available.
"""

import importlib.util
import json
import os
import types
from pathlib import Path

import pytest
import yaml


def _load_expectations() -> dict:
    local = Path(__file__).parent / "fixtures" / "routing_expectations.local.yaml"
    default = Path(__file__).parent / "fixtures" / "routing_expectations.example.yaml"
    path = local if local.exists() else default
    with open(path) as f:
        return yaml.safe_load(f)


EXPECTATIONS = _load_expectations()
FORBIDDEN = set(EXPECTATIONS["ollama_discovery"]["forbidden_resolved_values"])

_HOOK_PATH = Path(__file__).parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load_hook_module(env_overrides: dict, home_dir: Path) -> types.ModuleType:
    """Load auto-route.py as an isolated module under a synthetic environment.

    Uses importlib.util so we get a fresh module object each call without
    polluting sys.modules. Environment variables and HOME are temporarily
    replaced so _load_discovered_ollama_models() sees only what we pass.
    """
    old_env = os.environ.copy()
    old_home_attr = Path.__dict__.get("home")

    try:
        os.environ.clear()
        os.environ.update(env_overrides)
        # Redirect Path.home() to the tmp directory
        Path.home = staticmethod(lambda: home_dir)  # type: ignore[method-assign]

        spec = importlib.util.spec_from_file_location("_ar_test", _HOOK_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        if old_home_attr is not None:
            Path.home = old_home_attr  # type: ignore[method-assign]
        else:
            try:
                del Path.home  # type: ignore[misc]
            except AttributeError:
                pass


@pytest.fixture()
def tmp_home(tmp_path: Path) -> Path:
    llm_router = tmp_path / ".llm-router"
    llm_router.mkdir()
    return tmp_path


class TestPrecedence:
    def test_llm_router_ollama_model_wins(self, tmp_home: Path) -> None:
        mod = _load_hook_module(
            {"LLM_ROUTER_OLLAMA_MODEL": "llama3.1:8b", "OLLAMA_MODELS": "qwen3.5:latest"},
            tmp_home,
        )
        assert mod._DISCOVERED_OLLAMA == ["llama3.1:8b"]
        assert mod.OLLAMA_MODEL == "llama3.1:8b"

    def test_ollama_budget_models_second(self, tmp_home: Path) -> None:
        mod = _load_hook_module(
            {"OLLAMA_BUDGET_MODELS": "mistral:7b,phi3:mini", "OLLAMA_MODELS": "qwen3.5:latest"},
            tmp_home,
        )
        assert mod._DISCOVERED_OLLAMA == ["mistral:7b", "phi3:mini"]
        assert mod.OLLAMA_MODEL == "mistral:7b"

    def test_ollama_models_env_third(self, tmp_home: Path) -> None:
        mod = _load_hook_module({"OLLAMA_MODELS": "qwen3.5:latest,qwen3.6:27b"}, tmp_home)
        assert mod._DISCOVERED_OLLAMA == ["qwen3.5:latest", "qwen3.6:27b"]
        assert mod.OLLAMA_MODEL == "qwen3.5:latest"

    def test_discovery_json_fourth(self, tmp_home: Path) -> None:
        discovery = {
            "models": {
                "ollama/phi3:mini": {"provider": "ollama"},
                "ollama/qwen2.5:7b": {"provider": "ollama"},
                "openai/gpt-4o": {"provider": "openai"},
            }
        }
        (tmp_home / ".llm-router" / "discovery.json").write_text(json.dumps(discovery))
        mod = _load_hook_module({}, tmp_home)
        assert "phi3:mini" in mod._DISCOVERED_OLLAMA
        assert "qwen2.5:7b" in mod._DISCOVERED_OLLAMA
        assert "gpt-4o" not in mod._DISCOVERED_OLLAMA

    def test_empty_env_gives_non_empty_fallback(self, tmp_home: Path) -> None:
        mod = _load_hook_module({}, tmp_home)
        assert isinstance(mod.OLLAMA_MODEL, str)
        assert mod.OLLAMA_MODEL


class TestForbiddenValues:
    def test_env_never_returns_forbidden(self, tmp_home: Path) -> None:
        mod = _load_hook_module({"OLLAMA_MODELS": "qwen3.5:latest,qwen3.6:27b"}, tmp_home)
        for bad in FORBIDDEN:
            assert bad not in mod._DISCOVERED_OLLAMA, f"Forbidden model {bad!r} in resolved list"
        assert mod.OLLAMA_MODEL not in FORBIDDEN

    def test_discovery_json_never_returns_forbidden(self, tmp_home: Path) -> None:
        discovery = {"models": {"ollama/qwen3.5:latest": {}, "ollama/qwen2.5:7b": {}}}
        (tmp_home / ".llm-router" / "discovery.json").write_text(json.dumps(discovery))
        mod = _load_hook_module({}, tmp_home)
        assert mod.OLLAMA_MODEL not in FORBIDDEN


class TestDynamicListsSync:
    def test_ollama_models_and_code_models_match_discovered(self, tmp_home: Path) -> None:
        mod = _load_hook_module({"OLLAMA_MODELS": "qwen3.5:latest,qwen3.6:27b"}, tmp_home)
        assert mod.OLLAMA_MODELS == mod._DISCOVERED_OLLAMA
        assert mod.OLLAMA_CODE_MODELS == mod._DISCOVERED_OLLAMA
