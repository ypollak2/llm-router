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


def test_env_override_wins_over_the_table(monkeypatch):
    # One window for EVERY local call: hook and direct_executor honour the override, so must this.
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "65536")
    a = O.OllamaAdapter("qwen3.6:35b-a3b-coding", base_url="http://127.0.0.1:11434")
    assert a.num_ctx == 65536 == O.num_ctx("qwen3.6:35b-a3b-coding")
    monkeypatch.setenv("LLM_ROUTER_AGENT_NUM_CTX", "49152")  # agent var outranks the local one
    assert O.OllamaAdapter("qwen3.6:35b-a3b-coding", base_url="http://127.0.0.1:11434").num_ctx == 49152


def test_explicit_window_beats_env_override(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "65536")
    a = O.OllamaAdapter("qwen3.6:35b-a3b-coding", base_url="http://127.0.0.1:11434", num_ctx=9000)
    assert a.num_ctx == 9000


def test_override_zero_omits_num_ctx_from_payload(monkeypatch):
    import io
    import json
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "0")
    a = O.OllamaAdapter("qwen3.6:35b-a3b-coding", base_url="http://127.0.0.1:11434")
    assert a.num_ctx is None
    sent = {}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        sent.update(json.loads(req.data))
        return _Resp(json.dumps({"message": {"content": "ok"}}).encode())

    monkeypatch.setattr(O.urllib.request, "urlopen", fake_urlopen)
    a.chat([{"role": "user", "content": "hi"}], [{"function": {"name": "t"}}], timeout_s=5)
    assert "num_ctx" not in sent["options"]
