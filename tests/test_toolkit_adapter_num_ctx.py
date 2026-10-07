"""M3.4: the toolkit's Ollama adapter reads its window from local_models.num_ctx(model)."""
import pytest

from llm_router import local_models
from llm_router.toolkit.adapters import ollama as O


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_OLLAMA_NUM_CTX", "LLM_ROUTER_LOCAL_NUM_CTX", "LLM_ROUTER_AGENT_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("model", list(local_models.NUM_CTX) + ["ollama/qwen3.6:35b-a3b-coding", "unlisted:7b", None])
def test_adapter_window_is_the_table_value(model):
    a = O.OllamaAdapter(model or "", base_url="http://127.0.0.1:11434")
    assert a.num_ctx == local_models.num_ctx(model or None) == O.default_num_ctx(model)


def test_qwen36_is_32768():
    assert O.OllamaAdapter("qwen3.6:35b-a3b-coding", base_url="http://127.0.0.1:11434").num_ctx == 32768


def test_explicit_window_still_wins():
    assert O.OllamaAdapter("qwen3.6:35b-a3b-coding", base_url="http://127.0.0.1:11434", num_ctx=9000).num_ctx == 9000


def test_no_private_copy_of_the_table():
    assert not hasattr(O, "_LARGE_WINDOW_FAMILIES") and not hasattr(O, "TOOLKIT_NUM_CTX")
