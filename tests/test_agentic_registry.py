"""Tests for the self-calibrating agentic model registry (Fix #3).

Mocked so they need no running Ollama — they pin the logic that keeps the
registry from drifting: best-of-N capability, rank ordering, cache round-trip,
and automatic re-probe when the installed model set changes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from llm_router import agentic_registry as reg


def test_best_of_n_passes_on_any_success(monkeypatch):
    """A flaky-but-capable model (fails then passes) is PASS, not benched."""
    calls = {"n": 0}

    def fake_probe(model, timeout=90):
        calls["n"] += 1
        return calls["n"] >= 2  # fail first attempt, pass second

    monkeypatch.setattr(reg, "probe_model", fake_probe)
    assert reg.probe_capable("flaky:model", attempts=3) is True


def test_best_of_n_fails_when_never_passes(monkeypatch):
    """A truly incapable model (e.g. an embedding model) fails all attempts."""
    monkeypatch.setattr(reg, "probe_model", lambda m, timeout=90: False)
    assert reg.probe_capable("nomic-embed-text:latest", attempts=3) is False


def test_best_of_n_early_exits(monkeypatch):
    """A reliable model costs exactly one probe (no wasted runs)."""
    calls = {"n": 0}

    def fake_probe(model, timeout=90):
        calls["n"] += 1
        return True

    monkeypatch.setattr(reg, "probe_model", fake_probe)
    assert reg.probe_capable("good:model", attempts=3) is True
    assert calls["n"] == 1


def test_rank_orders_pass_unknown_fail():
    verdicts = {"good": True, "bad": False}
    assert reg.rank("good", verdicts) == 0   # verified → first
    assert reg.rank("mystery", verdicts) == 1  # unknown → middle
    assert reg.rank("bad", verdicts) == 2    # known-failer → last


def test_cache_roundtrip_and_reprobe_on_model_change(monkeypatch, tmp_path):
    """Cache is reused when the model set is unchanged, re-probed when it changes."""
    cache = tmp_path / "agentic_models.json"
    monkeypatch.setattr(reg, "CACHE_PATH", cache)

    installed = ["hermes3:8b"]
    probe_calls = {"n": 0}

    monkeypatch.setattr(reg, "list_installed_models", lambda: list(installed))

    def fake_capable(model, attempts=3, timeout=90):
        probe_calls["n"] += 1
        return True

    monkeypatch.setattr(reg, "probe_capable", fake_capable)

    # First call → probes once, writes cache.
    v1 = reg.get_registry()
    assert v1 == {"hermes3:8b": True}
    assert probe_calls["n"] == 1
    assert cache.exists()

    # Second call, same model set → served from cache, no new probe.
    v2 = reg.get_registry()
    assert v2 == {"hermes3:8b": True}
    assert probe_calls["n"] == 1

    # Pull a new model → hash changes → re-probe both.
    installed.append("qwen3-coder:30b")
    v3 = reg.get_registry()
    assert set(v3) == {"hermes3:8b", "qwen3-coder:30b"}
    assert probe_calls["n"] == 3  # 1 + 2 for the re-probe


def test_best_agentic_model_is_dynamic_per_user(monkeypatch):
    """The pick is derived from each machine's registry — never hardcoded."""
    # User A: strong coder present → qwen3-coder wins (prefer order).
    monkeypatch.setattr(reg, "get_registry",
                        lambda **k: {"qwen3-coder:30b": True, "hermes3:8b": True, "nomic-embed:latest": False})
    assert reg.best_agentic_model() == "ollama/qwen3-coder:30b"

    # User B: totally different install → picks what THEY have.
    monkeypatch.setattr(reg, "get_registry", lambda **k: {"hermes3:8b": True})
    assert reg.best_agentic_model() == "ollama/hermes3:8b"

    # User C: a verified model matching no prefer token is still eligible.
    monkeypatch.setattr(reg, "get_registry", lambda **k: {"llama3:70b": True})
    assert reg.best_agentic_model() == "ollama/llama3:70b"

    # User D: nothing verified → no pin.
    monkeypatch.setattr(reg, "get_registry", lambda **k: {"nomic-embed:latest": False})
    assert reg.best_agentic_model() == ""


def test_best_agentic_model_prefers_larger_size(monkeypatch):
    """Same family, both verified → larger parameter count wins the tiebreak."""
    monkeypatch.setattr(reg, "get_registry",
                        lambda **k: {"qwen3-coder:7b": True, "qwen3-coder:30b": True})
    assert reg.best_agentic_model() == "ollama/qwen3-coder:30b"


def test_best_agentic_model_never_probes(monkeypatch):
    """Hot-path safety: best_agentic_model must call get_registry with allow_probe=False."""
    seen = {}
    def spy(**kwargs):
        seen.update(kwargs)
        return {"hermes3:8b": True}
    monkeypatch.setattr(reg, "get_registry", spy)
    reg.best_agentic_model()
    assert seen.get("allow_probe") is False


def test_populate_in_background_spawns_when_models_present(monkeypatch):
    calls = {"popen": 0}
    monkeypatch.setattr(reg, "list_installed_models", lambda: ["hermes3:8b"])
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.__setitem__("popen", calls["popen"] + 1))
    assert reg.populate_in_background() is True
    assert calls["popen"] == 1


def test_populate_in_background_skips_without_models(monkeypatch):
    monkeypatch.setattr(reg, "list_installed_models", lambda: [])
    assert reg.populate_in_background() is False
