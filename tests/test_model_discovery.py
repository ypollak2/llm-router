"""Which local models exist is asked once, answered live, and cached with a TTL.

There were two implementations. `auto-route` had a careful resolver evaluated
once at import; `chain_builder` — the function the draft chain actually called —
stopped at two env vars and fell back to a hardcoded "qwen3.5:latest". An
operator who configured qwen3.8 got qwen3.5 on all 69 of a day's draft calls,
silently, because the variable they set is read by a different subsystem.

The cache was the other half: written once, never refreshed, 14 hours stale and
already missing an installed model.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import model_discovery as md


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for v in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS",
              "LLM_ROUTER_DISCOVERY_TTL_HOURS"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def _cache(home, models, age_hours):
    (home / "discovery.json").write_text(json.dumps({
        "cached_at": time.time() - age_hours * 3600,
        "models": {f"ollama/{m}": {"model_id": f"ollama/{m}"} for m in models},
    }))


def test_env_override_wins(home, monkeypatch):
    _cache(home, ["cached:latest"], 0)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "chosen:latest")
    assert md.available_ollama_models() == ["chosen:latest"]


def test_fresh_cache_is_used_without_probing(home, monkeypatch):
    _cache(home, ["a:latest", "b:latest"], age_hours=1)
    monkeypatch.setattr(md, "probe_ollama", lambda: pytest.fail("probed a fresh cache"))
    assert md.available_ollama_models() == ["a:latest", "b:latest"]


def test_stale_cache_triggers_a_live_probe(home, monkeypatch):
    """A model pulled today must be usable today."""
    _cache(home, ["yesterday:latest"], age_hours=48)
    monkeypatch.setattr(md, "probe_ollama", lambda: ["today:latest"])
    assert md.available_ollama_models() == ["today:latest"]
    saved = json.loads((home / "discovery.json").read_text())
    assert "ollama/today:latest" in saved["models"], "the refresh was not cached"


def test_stale_cache_survives_an_unreachable_ollama(home, monkeypatch):
    """Yesterday's list beats nothing — but only after a probe was attempted."""
    _cache(home, ["yesterday:latest"], age_hours=48)
    monkeypatch.setattr(md, "probe_ollama", lambda: [])
    assert md.available_ollama_models() == ["yesterday:latest"]


def test_no_cache_and_no_ollama_returns_empty_not_a_guess(home, monkeypatch):
    """A hardcoded default that is not installed produces a chain that cannot
    run and an error that blames the model. Empty surfaces as 'no free-tier
    model available', which is true and already handled."""
    monkeypatch.setattr(md, "probe_ollama", lambda: [])
    assert md.available_ollama_models() == []


def test_embedding_models_are_never_offered_for_completion(home, monkeypatch):
    """Ollama lists embedding models alongside chat models; they cannot answer.

    Reading the list live — the whole point of this module — therefore surfaces
    models that fail the moment they are chosen, in a way that looks like a
    model failure rather than a selection bug.
    """
    assert md._is_completion_model("qwen3.8:latest")
    assert not md._is_completion_model("nomic-embed-text:latest")
    assert not md._is_completion_model("bge-large")
    assert not md._is_completion_model("all-minilm:l6")

    # and end to end, through a probe that returns both kinds
    monkeypatch.setattr(md.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    _cache(home, ["qwen3.8:latest", "nomic-embed-text:latest"], age_hours=1)
    assert md.available_ollama_models() == ["qwen3.8:latest"]


def test_chain_builder_and_the_hook_agree(home, monkeypatch):
    """The two callers that used to disagree must now give the same answer."""
    _cache(home, ["shared:latest"], age_hours=1)
    from llm_router.hooks.chain_builder import _ollama_models
    assert [m.model for m in _ollama_models()] == ["shared:latest"]


_MIXED = ["nimble:9b", "llmr-classifier:latest", "llmr-classifier-38:latest",
          "llamacpp:44a38603922e", "qwen3.5:latest"]


@pytest.fixture
def no_role_env(monkeypatch):
    for v in ("LLM_ROUTER_LOCAL_ALLOW_MODELS", "LLM_ROUTER_CLASSIFIER_MODEL", "LLM_ROUTER_DECISION_MODEL"):
        monkeypatch.delenv(v, raising=False)


def test_non_generators_never_reach_the_draft_chain(home, no_role_env):
    """HOOK-DRAFT-1: the hook's list kept nimble:9b (decision-only, HTTP 400 on generate)
    and the classifier aliases, so a moderate code draft could open on one of them.
    nimble is listed first, so first_installed() would have returned it as well."""
    _cache(home, _MIXED, age_hours=1)
    assert md.available_ollama_models() == ["qwen3.5:latest"]
    assert md.first_installed() == "qwen3.5:latest"
    from llm_router.hooks.chain_builder import _ollama_models
    assert [m.model for m in _ollama_models()] == ["qwen3.5:latest"]


def test_a_declared_decision_role_alone_excludes(home, no_role_env):
    """nimble is excluded by local_models.ROLES, not by a name prefix."""
    _cache(home, ["nimble:9b", "qwen3.5:latest"], age_hours=1)
    assert md.available_ollama_models() == ["qwen3.5:latest"]


def test_the_classifier_name_rule_alone_excludes(home, no_role_env):
    """llmr-classifier-38 has the default generate role; only the name rule removes it."""
    _cache(home, ["llmr-classifier-38:latest", "qwen3.5:latest"], age_hours=1)
    assert md.available_ollama_models() == ["qwen3.5:latest"]


def test_the_live_probe_filters_too(home, no_role_env, monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"models": [{"name": n} for n in _MIXED]}).encode()

    monkeypatch.setattr(md.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert md.probe_ollama() == ["qwen3.5:latest"]


def test_the_allow_list_still_admits_a_deliberately_named_import(home, no_role_env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_ALLOW_MODELS", "llamacpp:44a38603922e")
    _cache(home, ["llamacpp:44a38603922e", "qwen3.5:latest"], age_hours=1)
    assert md.available_ollama_models() == ["llamacpp:44a38603922e", "qwen3.5:latest"]


def test_the_hook_list_does_not_import_discover():
    """discover costs ~0.56 s to import; the per-prompt hook must not pay it for this rule."""
    import subprocess
    import sys

    code = ("import sys; from llm_router.hooks import chain_builder; chain_builder._ollama_models; "
            "from llm_router import model_discovery; model_discovery._is_completion_model('qwen3.5:latest'); "
            "print('llm_router.discover' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", out.stdout + out.stderr
