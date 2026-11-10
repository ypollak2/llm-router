"""Memory guard counts the context window (KV + state + compute), not only the weights.

Source numbers: qwen3.6:35b-a3b-coding engine log (10 full-attention layers, KV f16 20 MiB per 1k tokens,
recurrent state 188 MiB, compute ~0.5 GiB). Every server and the host are faked.
"""
from __future__ import annotations

import pytest

from tests.test_mem_guard import GB, GIB, SMALL, Fake, host, row

MIB = 1024 ** 2
QWEN = "qwen3.6:35b-a3b-coding"
_REAL_METAL_CAP = None
# GGUF metadata of the qwen35moe model: 40 blocks, every 4th keeps a KV cache, 2 KV heads, key/value length 256.
QWEN_INFO = {"model_info": {"general.architecture": "qwen35moe", "qwen35moe.block_count": 40,
                            "qwen35moe.full_attention_interval": 4, "qwen35moe.attention.head_count": 16,
                            "qwen35moe.attention.head_count_kv": 2, "qwen35moe.attention.key_length": 256,
                            "qwen35moe.attention.value_length": 256}}


def _grab_real():
    global _REAL_METAL_CAP
    from llm_router.toolkit.adapters import ollama as oa
    _REAL_METAL_CAP = oa._metal_cap_gib


_grab_real()


@pytest.fixture(autouse=True)
def _kv_env(monkeypatch):
    for var in ("LLM_ROUTER_LOCAL_MODEL", "LLM_ROUTER_LOCAL_MODEL_URL", "LLM_ROUTER_MEM_GUARD",
                "LLM_ROUTER_MEM_ON_PRESSURE", "LLM_ROUTER_MEM_RESERVE_GB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", SMALL)
    monkeypatch.setenv("LLM_ROUTER_MEM_HEADROOM", "1.0")
    from llm_router.toolkit.adapters import ollama as oa
    oa._PS_CACHE.clear()
    oa._RESERVED.clear()
    oa._SHOW_CACHE.clear()
    monkeypatch.setattr(oa, "_metal_cap_gib", lambda total: None)
    host(monkeypatch, 48.0, 40.0)
    yield
    oa._PS_CACHE.clear()
    oa._RESERVED.clear()


def _terms(monkeypatch, show, name, ctx):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": []}, {name: 20 * GB}, show)
    return oa._kv_terms(SMALL, name, ctx, 20 * GB)


def test_kv_derived_from_gguf_metadata_scales_with_ctx(monkeypatch):
    k32, src = _terms(monkeypatch, {QWEN: QWEN_INFO}, QWEN, 32768)
    k128, _ = _terms(monkeypatch, {QWEN: QWEN_INFO}, QWEN, 131072)
    assert src == "gguf"
    # 10 layers x 2 heads x (256+256) x 2 B = 20 KiB/token -> 640 MiB at 32768 (the logged value)
    assert (k128 - k32) == pytest.approx(20 * 1024 * (131072 - 32768) * 1.1)
    assert k32 == pytest.approx(640 * MIB * 1.1 + 192 * MIB + 512 * MIB)
    assert k128 / GIB == pytest.approx(2.5 * 1.1 + 0.1875 + 0.5)  # 128k: KV (+10% MTP draft) + state + compute


def test_registry_value_when_show_has_no_metadata(monkeypatch):
    k32, src = _terms(monkeypatch, {QWEN: {"size": 1}}, QWEN, 32768)
    assert src == "registry"
    assert k32 == pytest.approx(20 * 32.768 * 1.1 * MIB + 188 * MIB + 512 * MIB)


def test_unknown_model_gets_the_conservative_default(monkeypatch):
    k32, src = _terms(monkeypatch, {}, "mystery:70b", 32768)       # /api/show fails too
    assert src == "default"
    assert k32 == pytest.approx(128 * 32.768 * MIB + 512 * MIB)    # 4.0 GiB KV + 0.5 GiB compute


def test_128k_window_is_refused_where_32k_fits(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    # 20.7 GB weights; 8 GiB of the 48 GiB host already resident; generic model without metadata.
    Fake(monkeypatch, {"11434": [row("other:8b", 8)]}, {"big:35b": int(20.7 * GB)}, {})
    monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None: 32768)
    oa.ensure_fits("big:35b", SMALL, "t")                          # 20.7 + 4.5 + 8 = 33 GB < 38.4
    oa._PS_CACHE.clear()
    oa._RESERVED.clear()
    monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None: 131072)
    with pytest.raises(oa.MemoryBudgetExceeded) as exc:            # 20.7 + 16.5 + 8 = 45 GB > 38.4
        oa.ensure_fits("big:35b", SMALL, "t")
    assert exc.value.need_gb > 36


def test_target_size_is_weights_plus_window(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": []}, {QWEN: 20 * GB}, {QWEN: QWEN_INFO})
    for ctx, extra_gib in ((32768, 0.625 * 1.1 + 0.1875 + 0.5), (131072, 2.5 * 1.1 + 0.1875 + 0.5)):
        oa._PS_CACHE.clear()
        oa._RESERVED.clear()
        monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None, c=ctx: c)
        oa.ensure_fits(QWEN, SMALL, "t")
        reserved = oa._RESERVED[(QWEN, SMALL)][1]
        assert reserved / GIB == pytest.approx(20 * GB / GIB + extra_gib)


def test_resident_at_same_ctx_is_not_double_counted(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None: 131072)
    resident = dict(row(QWEN, 30), context_length=131072)          # /api/ps size already holds the KV
    Fake(monkeypatch, {"11434": [resident]}, {QWEN: 20 * GB}, {QWEN: QWEN_INFO})
    oa.ensure_fits(QWEN, SMALL, "t")                               # no refusal, no new reservation
    assert (QWEN, SMALL) not in oa._RESERVED


def test_resident_at_smaller_ctx_is_a_reload_counted_once(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None: 131072)
    resident = dict(row(QWEN, 22), context_length=32768)
    Fake(monkeypatch, {"11434": [resident]}, {QWEN: 20 * GB}, {QWEN: QWEN_INFO})
    oa.ensure_fits(QWEN, SMALL, "t")     # 22 GB old copy replaced by ~23.2 GiB new one: fits only if counted once
    assert oa._RESERVED[(QWEN, SMALL)][1] / GIB == pytest.approx(20 * GB / GIB + 2.75 + 0.6875)


def test_metal_cap_refuses_before_the_ram_rule(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(oa, "_metal_cap_gib", lambda total: 24.0)
    monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None: 131072)
    Fake(monkeypatch, {"11434": []}, {QWEN: 24 * GB}, {QWEN: QWEN_INFO})
    with pytest.raises(oa.MemoryBudgetExceeded) as exc:           # 22.4 + 3.2 = 25.5 GiB > 24 Metal, < 38.4 RAM cap
        oa.ensure_fits(QWEN, SMALL, "t")
    assert exc.value.limit_gb == 24.0


def test_metal_cap_uses_wired_limit_else_measured_fraction(monkeypatch):
    import subprocess
    import sys

    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(oa, "_metal_cap_gib", _REAL_METAL_CAP)
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, out["v"], "")

    out = {"v": "0\n"}
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(oa, "_WIRED_MB", [])
    assert oa._metal_cap_gib(48.0) == pytest.approx(48.0 * 0.78)
    out["v"] = "40960\n"
    assert oa._metal_cap_gib(48.0) == pytest.approx(48.0 * 0.78)    # cached: one sysctl per process
    assert len(calls) == 1
    monkeypatch.setattr(oa, "_WIRED_MB", [])
    assert oa._metal_cap_gib(48.0) == pytest.approx(40.0)           # wired limit overrides
    monkeypatch.setattr(sys, "platform", "linux")
    assert oa._metal_cap_gib(48.0) is None


def test_clamped_window_resident_model_is_not_refused(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    info = {"model_info": dict(QWEN_INFO["model_info"], **{"qwen35moe.context_length": 8192})}
    monkeypatch.setattr("llm_router.local_models.num_ctx", lambda m=None: 32768)
    resident = dict(row(QWEN, 30), context_length=8192)           # Ollama clamped 32768 to the trained 8192
    Fake(monkeypatch, {"11434": [resident]}, {QWEN: 20 * GB}, {QWEN: info})
    oa.ensure_fits(QWEN, SMALL, "t")
    assert (QWEN, SMALL) not in oa._RESERVED                      # treated as already resident, nothing added


def test_small_model_without_metadata_keeps_weights_x_headroom(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": []}, {"tiny:1b": 1 * GB}, {})        # /api/show fails
    extra, src = oa._kv_terms(SMALL, "tiny:1b", 32768, 1 * GB)
    assert (extra, src) == (0.0, "failopen")
    oa.ensure_fits("tiny:1b", SMALL, "t")
    assert oa._RESERVED[("tiny:1b", SMALL)][1] == 1 * GB                     # headroom 1.0, no 4.5 GiB default
    assert fake.calls


def test_model_info_is_fetched_once_per_model(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": []}, {QWEN: 20 * GB}, {QWEN: QWEN_INFO})
    for _ in range(3):
        oa._kv_terms(SMALL, QWEN, 32768, 20 * GB)
        oa._trained_ctx(SMALL, QWEN)
    assert sum(c.endswith("/api/show") for c in fake.calls) == 1
