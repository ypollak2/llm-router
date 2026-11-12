"""One-model policy: every caller of the shared model sends keep_alive -1 and the proxy's
num_ctx (docs/bugs/CLSCTX-1.md). Without the policy nothing changes."""
from __future__ import annotations

import json
import urllib.request
from unittest.mock import patch

import pytest

MODEL = "qwen3.6:35b-a3b-coding"


@pytest.fixture
def one_model(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", MODEL)
    monkeypatch.setenv("LLM_ROUTER_PROXY_NUM_CTX", "16384")   # env is ignored: the table value wins
    # a differing agent window must lose to the proxy's
    monkeypatch.setenv("LLM_ROUTER_AGENT_NUM_CTX", "8192")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "8192")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", "300")


@pytest.fixture
def no_policy(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROXY_NUM_CTX", raising=False)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", "300")


def test_helper(one_model, monkeypatch):
    from llm_router.toolkit.adapters.ollama import shared_runner_opts
    assert shared_runner_opts(MODEL) == (32768, -1)
    assert shared_runner_opts(f"ollama/{MODEL}") == (32768, -1)
    assert shared_runner_opts("other:7b") is None
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL")
    assert shared_runner_opts(MODEL) is None


def test_agent_num_ctx_yields_to_proxy(one_model, no_policy, monkeypatch):
    from llm_router.toolkit.adapters.ollama import num_ctx
    monkeypatch.setenv("LLM_ROUTER_AGENT_NUM_CTX", "8192")
    assert num_ctx(MODEL) == 8192            # no policy: unchanged
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", MODEL)
    assert num_ctx(MODEL) == 32768


def test_proxy_backend_keep_alive(one_model):
    from llm_router.proxy.backends import OllamaBackend
    assert OllamaBackend(f"ollama/{MODEL}", None, base_url="http://x", keep_alive=600).keep_alive == -1
    assert OllamaBackend("ollama/other:7b", None, base_url="http://x", keep_alive=600).keep_alive == 600


def test_proxy_backend_unchanged_without_policy(no_policy):
    from llm_router.proxy.backends import OllamaBackend
    assert OllamaBackend(f"ollama/{MODEL}", None, base_url="http://x", keep_alive=600).keep_alive == 600


def test_proxy_config_keep_alive(one_model):
    from llm_router.proxy.server import ProxyConfig
    assert ProxyConfig(model=f"ollama/{MODEL}").keep_alive == -1


def test_proxy_config_unchanged_without_policy(no_policy):
    from llm_router.proxy.server import ProxyConfig
    assert ProxyConfig(model=f"ollama/{MODEL}").keep_alive == 600


def test_warm_edit_payload(one_model):
    from llm_router import warm
    p = warm.warmup_payload(MODEL)
    assert (p["keep_alive"], p["options"]["num_ctx"]) == (-1, 32768)


def test_edit_keep_alive_unchanged_without_policy(no_policy):
    from llm_router import warm
    assert warm.edit_keep_alive(MODEL) == 300


def test_zero_claude_edit_sends_shared_opts(one_model):
    """zero_claude_edit passes the model so edit_keep_alive can see the policy (behavioural: capture the call)."""
    import time

    from llm_router import warm, zero_claude_edit
    from llm_router.hooks import direct_executor as de
    seen = []

    def fake_call(prompt, model, timeout, **kw):
        seen.append(kw.get("keep_alive"))
        return None, None                          # no response: the loop records a failed attempt and retries

    monkeypatch_ctx = pytest.MonkeyPatch()
    monkeypatch_ctx.setattr(de, "call_ollama", fake_call)
    try:
        zero_claude_edit.generate_edits("t", {"a.py": "x = 1\n"}, MODEL, time.monotonic() + 30)
    finally:
        monkeypatch_ctx.undo()
    assert seen and all(k == -1 for k in seen)
    assert warm.edit_keep_alive(MODEL) == -1


def test_direct_executor_payload(one_model, monkeypatch, open_url_via_urlopen):
    from llm_router.hooks import direct_executor as de
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(oa, "check_resident", lambda *a, **k: None)
    monkeypatch.setattr(oa, "ensure_fits", lambda *a, **k: None)
    sent = []

    def fake(req, *a, **k):
        sent.append(json.loads(req.data))
        raise OSError("stop")
    with patch.object(urllib.request, "urlopen", fake):
        de.call_ollama("hi", MODEL, 4, keep_alive=600)
    assert sent and sent[0]["keep_alive"] == -1 and sent[0]["options"]["num_ctx"] == 32768


def test_session_start_warmup_body(one_model, monkeypatch):
    from tests.test_local_model_url import SMALL, _load_hook
    ss = _load_hook("session-start.py")
    calls = []
    monkeypatch.setattr(ss.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(ss, "_detached_spawn_disabled", lambda: False)
    monkeypatch.setattr(ss, "_EDIT_WARMED_MODEL", None)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP", "on")
    monkeypatch.setenv("OLLAMA_BASE_URL", SMALL)
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(oa, "ensure_fits", lambda *a, **k: None)
    ss._warm_ollama_bg()
    body = json.loads(calls[0][calls[0].index("-d") + 1])
    assert body["keep_alive"] == -1 and body["options"]["num_ctx"] == 32768


def test_decision_classifier_keep_alive(one_model, monkeypatch):
    from llm_router import decision_classifier as dc
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_KEEP_ALIVE", "30m")
    assert dc.payload(MODEL, "x")["keep_alive"] == -1
    assert dc.payload("other:7b", "x")["keep_alive"] == "30m"


def test_helper_ignores_proxy_num_ctx_env(one_model):
    from llm_router import local_models
    from llm_router.toolkit.adapters.ollama import shared_runner_opts
    assert shared_runner_opts(MODEL) == (local_models.num_ctx(MODEL), -1) == (32768, -1)


def test_providers_explicit_ollama_num_ctx_loses_to_policy(one_model, monkeypatch):
    from llm_router.providers import _ollama_num_ctx
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    assert _ollama_num_ctx(f"ollama/{MODEL}") == 32768
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL")
    assert _ollama_num_ctx(f"ollama/{MODEL}") == 8192


def test_context_summary_body(one_model, monkeypatch, open_url_via_urlopen):
    from llm_router import context_summary as cs
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(oa, "check_resident", lambda *a, **k: None)
    sent = []

    def fake(req, *a, **k):
        sent.append(json.loads(req.data))
        raise OSError("stop")
    with patch.object(urllib.request, "urlopen", fake), pytest.raises(OSError):
        cs._generate(MODEL, "hi")
    assert sent[0]["keep_alive"] == -1 and sent[0]["options"]["num_ctx"] == 32768


def test_door_verdict_warm_payload_uses_shared_opts(one_model, monkeypatch):
    """door_verdict.warm() sends lc._payload; under the policy it carries the table window and -1."""
    from llm_router import local_classifier as lc
    p = lc._payload(MODEL, lc.Assembled("c", "warm-up"))
    assert (p["options"]["num_ctx"], p["keep_alive"]) == (32768, -1)


def test_proxy_warns_when_num_ctx_differs_from_table(caplog):
    from llm_router.proxy.server import ProxyConfig
    with caplog.at_level("WARNING"):
        ProxyConfig(model=f"ollama/{MODEL}", num_ctx=16384)
    assert "thrash" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        ProxyConfig(model=f"ollama/{MODEL}", num_ctx=32768)
    assert "thrash" not in caplog.text


# ---- LLM_ROUTER_SHARED_KEEP_ALIVE: bounded idle time for the shared runner -------------------

def _all_paths(monkeypatch):
    """keep_alive each shared-runner path would send, as {path: value} (no network)."""
    from llm_router import decision_classifier as dc
    from llm_router import local_classifier as lc
    from llm_router import warm
    from llm_router.hooks import direct_executor as de
    from llm_router.proxy.backends import OllamaBackend
    from llm_router.proxy.server import ProxyConfig
    from llm_router.toolkit.adapters import ollama as oa
    from llm_router.toolkit.adapters.ollama import shared_runner_opts
    monkeypatch.setattr(oa, "check_resident", lambda *a, **k: None)
    monkeypatch.setattr(oa, "ensure_fits", lambda *a, **k: None)
    out = {
        "helper": shared_runner_opts(MODEL)[1],
        "edit_keep_alive": warm.edit_keep_alive(MODEL),
        "warmup_payload": warm.warmup_payload(MODEL)["keep_alive"],
        "classifier": lc._keep_alive_for(MODEL),
        "classifier_payload": lc._payload(MODEL, lc.Assembled("c", "x"))["keep_alive"],
        "decision_classifier": dc.payload(MODEL, "x")["keep_alive"],
        "proxy_backend": OllamaBackend(f"ollama/{MODEL}", None, base_url="http://x", keep_alive=600).keep_alive,
        "proxy_config": ProxyConfig(model=f"ollama/{MODEL}").keep_alive,
    }
    sent = []

    def fake(req, *a, **k):
        sent.append(json.loads(req.data))
        raise OSError("stop")
    with patch.object(urllib.request, "urlopen", fake):
        de.call_ollama("hi", MODEL, 4, keep_alive=600)
        try:
            from llm_router import context_summary as cs
            cs._generate(MODEL, "hi")
        except OSError:
            pass
    out["direct_executor"], out["context_summary"] = sent[0]["keep_alive"], sent[1]["keep_alive"]
    return out


def test_shared_keep_alive_unset_is_minus_one_everywhere(one_model, monkeypatch, open_url_via_urlopen):
    monkeypatch.delenv("LLM_ROUTER_SHARED_KEEP_ALIVE", raising=False)
    got = _all_paths(monkeypatch)
    assert set(got.values()) == {-1}, got


def test_shared_keep_alive_two_hours_everywhere(one_model, monkeypatch, open_url_via_urlopen):
    monkeypatch.setenv("LLM_ROUTER_SHARED_KEEP_ALIVE", "2h")
    got = _all_paths(monkeypatch)
    assert set(got.values()) == {"2h"}, got


def test_shared_keep_alive_seconds_is_int(one_model, monkeypatch):
    from llm_router.toolkit.adapters.ollama import shared_runner_opts
    monkeypatch.setenv("LLM_ROUTER_SHARED_KEEP_ALIVE", "7200")
    assert shared_runner_opts(MODEL)[1] == 7200
    monkeypatch.setenv("LLM_ROUTER_SHARED_KEEP_ALIVE", "-1")
    assert shared_runner_opts(MODEL)[1] == -1


def test_shared_keep_alive_other_model_untouched(one_model, monkeypatch):
    from llm_router import warm
    monkeypatch.setenv("LLM_ROUTER_SHARED_KEEP_ALIVE", "2h")
    assert warm.edit_keep_alive("other:7b") == 300      # LOCAL_KEEP_ALIVE still rules non-shared models


@pytest.mark.parametrize("bad", ["0", "-5", "0s", "0h", "abc", "2 hours", "1.5", "h", "-2h"])
def test_shared_keep_alive_invalid_falls_back_to_minus_one_and_warns(bad, one_model, monkeypatch, caplog):
    from llm_router.toolkit.adapters import ollama as oa
    oa._SHARED_KA_WARNED.discard(bad)
    monkeypatch.setenv("LLM_ROUTER_SHARED_KEEP_ALIVE", bad)
    with caplog.at_level("WARNING"):
        assert oa.shared_runner_opts(MODEL)[1] == -1
    assert "LLM_ROUTER_SHARED_KEEP_ALIVE" in caplog.text


def test_shared_keep_alive_keeps_reload_window_pinned(one_model, monkeypatch):
    """A reload after expiry asks for the raised window: num_ctx is independent of keep_alive on every path."""
    from llm_router import local_classifier as lc
    from llm_router import warm
    from llm_router.toolkit.adapters.ollama import shared_runner_opts
    monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", "131072")
    monkeypatch.delenv("LLM_ROUTER_SHARED_KEEP_ALIVE", raising=False)
    pinned = (shared_runner_opts(MODEL)[0], warm.warmup_payload(MODEL)["options"]["num_ctx"],
              lc._payload(MODEL, lc.Assembled("c", "x"))["options"]["num_ctx"])
    monkeypatch.setenv("LLM_ROUTER_SHARED_KEEP_ALIVE", "2h")
    bounded = (shared_runner_opts(MODEL)[0], warm.warmup_payload(MODEL)["options"]["num_ctx"],
               lc._payload(MODEL, lc.Assembled("c", "x"))["options"]["num_ctx"])
    assert pinned == bounded == (131072, 131072, 131072)
