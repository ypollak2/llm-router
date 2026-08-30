"""Regression: GH#62 — doctor/verify report Ollama healthy without ever
checking whether the specific model(s) the LLM-first ensemble classifier will
actually request (`ensemble._primary_model()` / `_secondary_model()`) are among
the models `/api/tags` says are installed.

Symptom from the report: `ensemble.py` hardcodes
``DEFAULT_PRIMARY = "ollama/qwen2.5:7b"`` (and a secondary tiebreak model),
neither of which was pulled on the reporter's machine. The ensemble classifier
failed on every call, silently degraded to the heuristic fallback, and both
`llm_router doctor` and `llm_router verify` printed "Ollama healthy" /
"✓ Ollama — N model(s)" the whole time — nothing compared the configured
model against the installed list.

This is a read-only, non-fatal check: the heuristic fallback still works, so a
missing ensemble model is a warning, not a failure (doctor/verify exit 0).
"""

from __future__ import annotations

import json


from llm_router import ensemble
from llm_router.commands import doctor as doc
from llm_router.commands import verify


def _tags_payload(names: list[str]) -> bytes:
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _fake_urlopen_factory(names: list[str]):
    """Only answers /api/tags; anything else looks unreachable (mirrors a
    sandbox with no local daemons running, which is what the rest of doctor's
    checks already tolerate)."""

    def _fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", str(req))
        if url.endswith("/api/tags"):
            return _FakeResp(_tags_payload(names))
        raise OSError("mocked: unreachable")

    return _fake_urlopen


# ── ensemble.py: the matching primitive, unit-tested in isolation ──────────


def test_model_installed_exact_match():
    assert ensemble.model_installed("ollama/qwen2.5:7b", ["qwen2.5:7b", "llama3.1:8b"])


def test_model_installed_missing():
    assert not ensemble.model_installed("ollama/qwen2.5:7b", ["llama3.1:8b"])


def test_model_installed_bare_name_matches_installed_latest_tag():
    """A configured bare name ('llama3') must match an installed ':latest'."""
    assert ensemble.model_installed("ollama/llama3", ["llama3:latest"])


def test_model_installed_explicit_latest_matches_installed_bare_name():
    """The reverse direction: configured ':latest' vs. an installed bare name."""
    assert ensemble.model_installed("ollama/llama3:latest", ["llama3"])


def test_model_installed_does_not_confuse_different_tags():
    """A real bug this must NOT introduce: 'foo:7b' should not match 'foo:latest'."""
    assert not ensemble.model_installed("ollama/foo:7b", ["foo:latest"])


def test_secondary_model_env_override(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_SECONDARY", "ollama/mymodel:latest")
    assert ensemble.secondary_model() == "ollama/mymodel:latest"


def test_secondary_model_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_SECONDARY", raising=False)
    assert ensemble.secondary_model() == ensemble.DEFAULT_SECONDARY


# ── doctor: the actual repro from the issue ─────────────────────────────────


def test_doctor_warns_when_ensemble_primary_is_not_installed(monkeypatch, capsys):
    """THE repro: /api/tags omits the primary the ensemble will request."""
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_PRIMARY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_SECONDARY", raising=False)
    monkeypatch.setattr(
        doc.urllib.request,
        "urlopen",
        _fake_urlopen_factory(["qwen3.5:latest", "llama3.1:8b"]),
    )

    code, issues = doc._run_doctor()
    text = capsys.readouterr().out

    assert ensemble.DEFAULT_PRIMARY.removeprefix("ollama/") in text
    assert "ollama pull" in text
    assert "LLM_ROUTER_ENSEMBLE_PRIMARY" in text
    assert any("ensemble" in i.lower() and "primary" in i.lower() for i in issues), issues
    # Non-fatal in doctor's own vocabulary: rendered as a warning (⚠), not a
    # hard failure (✗) — the heuristic fallback still works. (The process exit
    # code is a whole-run aggregate and may be non-zero for unrelated reasons
    # in this environment, so it is not asserted here.)
    ensemble_line = next(line for line in text.splitlines() if "ensemble" in line.lower() and "primary" in line.lower())
    assert "⚠" in ensemble_line
    assert "✗" not in ensemble_line


def test_doctor_warns_when_ensemble_secondary_is_not_installed(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/qwen3.5")
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_SECONDARY", raising=False)
    monkeypatch.setattr(
        doc.urllib.request,
        "urlopen",
        _fake_urlopen_factory(["qwen3.5:latest", "llama3.1:8b"]),
    )

    code, issues = doc._run_doctor()
    text = capsys.readouterr().out

    assert "qwen2.5-coder" in text  # DEFAULT_SECONDARY, bare form
    assert "LLM_ROUTER_ENSEMBLE_SECONDARY" in text
    assert any("ensemble" in i.lower() and "secondary" in i.lower() for i in issues), issues
    ensemble_line = next(line for line in text.splitlines() if "ensemble" in line.lower() and "secondary" in line.lower())
    assert "⚠" in ensemble_line
    assert "✗" not in ensemble_line


def test_doctor_no_warning_when_both_models_are_installed(monkeypatch, capsys):
    """No false positives: both configured models present → no ensemble warning."""
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/qwen3.5")
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_SECONDARY", "ollama/qwen3.6:27b")
    monkeypatch.setattr(
        doc.urllib.request,
        "urlopen",
        _fake_urlopen_factory(["qwen3.5:latest", "qwen3.6:27b", "llama3.1:8b"]),
    )

    code, issues = doc._run_doctor()
    text = capsys.readouterr().out.lower()

    assert "ensemble primary" not in text
    assert "ensemble secondary" not in text
    assert not any("ensemble" in i.lower() for i in issues), issues


def test_doctor_bare_vs_latest_edge_does_not_false_positive(monkeypatch, capsys):
    """The :latest/bare-name edge, exercised through doctor end-to-end: the
    installed list uses Ollama's real ':latest' suffix, the override is bare."""
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/qwen3.5")  # bare
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_SECONDARY", "ollama/qwen3.6:27b")
    monkeypatch.setattr(
        doc.urllib.request,
        "urlopen",
        _fake_urlopen_factory(["qwen3.5:latest", "qwen3.6:27b"]),  # tagged
    )

    code, issues = doc._run_doctor()
    text = capsys.readouterr().out.lower()

    assert "ensemble primary" not in text
    assert not any("ensemble" in i.lower() for i in issues), issues


# ── verify: mirrored one-line version ───────────────────────────────────────


def test_verify_check_ollama_flags_missing_ensemble_model(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_PRIMARY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_SECONDARY", raising=False)
    monkeypatch.setattr(
        verify.urllib.request,
        "urlopen",
        _fake_urlopen_factory(["qwen3.5:latest", "llama3.1:8b"]),
    )

    success, msg = verify.check_ollama()

    assert success is True, "missing ensemble model must stay non-fatal"
    assert ensemble.DEFAULT_PRIMARY.removeprefix("ollama/") in msg
    assert "LLM_ROUTER_ENSEMBLE_PRIMARY" in msg or "ollama pull" in msg


def test_verify_check_ollama_silent_when_models_present(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/qwen3.5")
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_SECONDARY", "ollama/qwen3.6:27b")
    monkeypatch.setattr(
        verify.urllib.request,
        "urlopen",
        _fake_urlopen_factory(["qwen3.5:latest", "qwen3.6:27b"]),
    )

    success, msg = verify.check_ollama()

    assert success is True
    assert "ensemble" not in msg.lower()
