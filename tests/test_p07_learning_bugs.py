"""P0.7 (plan v16) — learning and classifier-gate bugs.

Three bugs, one regression test group each. Every test here fails on
origin/main da31df7 and passes with the fix.

P0.7-a  ``build_learned_profile`` keyed learned routes by the TOOL name the
        router chose (``original_tool = "llm_code"``) while the hook looks them
        up by TASK TYPE (``_check_learned_override("code", ...)``). The keys
        never met, so no correction ever overrode a route.
P0.7-b  ``analyze_facts`` reported ``classification_accuracy = 1.0`` for a
        session with decisions and 0 corrections — a perfect score measured
        from nothing. It is now ``None``, rendered "not measurable (no
        corrections)".
P0.7-c  The hook's LLM classifier layer was gated by
        ``DISABLE_LLM_CLASSIFIERS = _ollama_reachable or not _has_api_key``:
        off exactly when Ollama is reachable, on (and sending prompts to a
        cloud API) when it is not. It is now controlled only by
        ``LLM_ROUTER_HOOK_LLM_LAYER`` (default off).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOK_DIR = _REPO / "src" / "llm_router" / "hooks"

# A prompt below the heuristic confidence threshold that hits no fast path, so
# classify_prompt falls through to the LLM layers (2 and 3).
_AMBIGUOUS = "purple elephants dancing slowly tonight maybe"


def _load_hook(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _HOOK_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated state dir and HOME so no real ~/.llm-router or .env leaks in."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "LLM_ROUTER_HOOK_LLM_LAYER",
        "LLM_ROUTER_CLASSIFY_LOCAL_ONLY",
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS",
        "LLM_ROUTER_CONFIDENCE_THRESHOLD",
    ):
        monkeypatch.delenv(key, raising=False)
    return state


# ── P0.7-a: learned-route key ───────────────────────────────────────────────


def _write_corrections(state: Path, rows: list[tuple[str, str]]) -> None:
    from llm_router.cost import CREATE_CORRECTIONS_TABLE

    conn = sqlite3.connect(state / "usage.db")
    conn.execute(CREATE_CORRECTIONS_TABLE)
    for original_tool, corrected_model in rows:
        conn.execute(
            "INSERT INTO corrections (original_tool, original_model, corrected_tool,"
            " corrected_model, reason, session_id) VALUES (?, ?, ?, ?, ?, ?)",
            (original_tool, "ollama/qwen", "llm_code", corrected_model, "fixture", "s1"),
        )
    conn.commit()
    conn.close()


def test_p07a_session_end_profile_feeds_the_hook_override(home):
    """End to end: corrections keyed ``llm_code`` → session-end builds the
    profile → the hook's ``_check_learned_override('code', ...)`` fires."""
    _write_corrections(home, [("llm_code", "openai/gpt-4o")] * 3)

    session_end = _load_hook("p07_session_end", "session-end.py")
    session_end._build_and_save_learned_profile()

    saved = json.loads((home / "learned_routes.json").read_text())
    assert "code" in saved, saved
    assert "llm_code" not in saved, saved

    auto_route = _load_hook("p07_auto_route_a", "auto-route.py")
    routes = auto_route._load_learned_routes()
    override = auto_route._check_learned_override("code", routes)
    assert override is not None, routes
    tool, suffix = override
    assert tool == "llm_code"


def test_p07a_every_tool_key_maps_to_its_task_type(home):
    from llm_router.memory.profiles import build_learned_profile

    rows = []
    for tool in ("llm_code", "llm_query", "llm_research", "llm_generate", "llm_analyze"):
        rows += [(tool, "openai/gpt-4o")] * 3
    rows += [("mystery_tool", "openai/gpt-4o")] * 3  # unknown: kept as-is
    _write_corrections(home, rows)

    profile = build_learned_profile()
    assert set(profile) == {"code", "query", "research", "generate", "analyze", "mystery_tool"}


def test_p07a_reader_accepts_a_legacy_tool_keyed_file(home):
    """A learned_routes.json written before the fix (key ``llm_code``) still
    overrides a ``code`` route for one release."""
    (home / "learned_routes.json").write_text(json.dumps({
        "llm_code": {"model": "openai/gpt-4o", "confidence": 3,
                     "source": "corrections", "last_correction": "2026-10-07"},
    }))
    auto_route = _load_hook("p07_auto_route_legacy", "auto-route.py")
    routes = auto_route._load_learned_routes()
    assert auto_route._check_learned_override("code", routes) is not None

    from llm_router.memory.profiles import load_learned_profile

    assert "code" in load_learned_profile()


def test_p07a_task_type_key_wins_over_a_legacy_key(home):
    route = {"confidence": 3, "source": "corrections", "last_correction": "2026-10-07"}
    # Task-type key first in the file, so a reader that lets the later key win
    # would pick the legacy entry.
    (home / "learned_routes.json").write_text(json.dumps({
        "code": {**route, "model": "openai/gpt-4o"},
        "llm_code": {**route, "model": "gemini/gemini-2.5-flash"},
    }))
    from llm_router.memory.profiles import load_learned_profile

    assert load_learned_profile()["code"].model == "openai/gpt-4o"


def test_p07a_below_threshold_still_does_not_fire(home):
    _write_corrections(home, [("llm_code", "openai/gpt-4o")] * 2)
    from llm_router.memory.profiles import build_learned_profile

    assert build_learned_profile() == {}


# ── P0.7-b: retrospective accuracy at 0 corrections ─────────────────────────


def _decisions(n: int) -> list[dict]:
    return [
        {"timestamp": f"2026-10-07T10:0{i}:00+00:00", "task_type": "code",
         "final_model": "ollama/qwen", "cost_usd": 0.0}
        for i in range(n)
    ]


def test_p07b_zero_corrections_is_not_measurable():
    from llm_router.retrospective import _format_accuracy_pct, analyze_facts

    facts = analyze_facts(_decisions(4), [])
    assert facts["classification_accuracy"] is None
    assert facts["measured"] is True  # the decisions are real; accuracy is not
    assert _format_accuracy_pct(facts) == "not measurable (no corrections)"


def test_p07b_with_corrections_is_still_a_number():
    from llm_router.retrospective import _format_accuracy_pct, analyze_facts

    facts = analyze_facts(_decisions(4), [{"decision_id": "x"}])
    assert facts["classification_accuracy"] == pytest.approx(0.75)
    assert _format_accuracy_pct(facts) == "75%"


def test_p07b_retrospective_file_says_not_measurable(home):
    from llm_router.retrospective import analyze_facts, write_retrospective_file

    facts = analyze_facts(_decisions(3), [])
    path = write_retrospective_file({"facts": facts, "gaps": [], "root_causes": [], "actions": []})
    text = path.read_text()
    assert "not measurable (no corrections)" in text
    assert "100%" not in text


# ── P0.7-c: hook LLM-layer gate ─────────────────────────────────────────────


class _Reachable:
    """Stands in for a reachable Ollama ``/api/tags`` response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _classify_with_spies(monkeypatch, *, ollama_reachable: bool, name: str):
    def fake_urlopen(*args, **kwargs):
        if ollama_reachable:
            return _Reachable()
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    auto_route = _load_hook(name, "auto-route.py")
    calls = {"ollama": 0, "api": 0}

    def fake_ollama(text):
        calls["ollama"] += 1
        return "analyze"

    def fake_api(text):
        calls["api"] += 1
        return "analyze"

    monkeypatch.setattr(auto_route, "classify_with_ollama", fake_ollama)
    monkeypatch.setattr(auto_route, "classify_with_gemini", fake_api)
    monkeypatch.setattr(auto_route, "classify_with_openai", fake_api)
    scores = auto_route.score_categories(_AMBIGUOUS)
    assert max(scores.values()) < auto_route.CONFIDENCE_THRESHOLD, scores
    result = auto_route.classify_prompt(_AMBIGUOUS)
    return result, calls


def test_p07c_default_off_with_ollama_reachable(home, monkeypatch):
    result, calls = _classify_with_spies(monkeypatch, ollama_reachable=True, name="p07c_1")
    assert calls == {"ollama": 0, "api": 0}
    assert result["method"] != "ollama"


def test_p07c_default_off_without_ollama_and_with_an_api_key(home, monkeypatch):
    """da31df7 turned the layer ON here and sent the prompt to a cloud API."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    result, calls = _classify_with_spies(monkeypatch, ollama_reachable=False, name="p07c_2")
    assert calls == {"ollama": 0, "api": 0}


def test_p07c_flag_on_calls_the_ollama_layer(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOOK_LLM_LAYER", "on")
    result, calls = _classify_with_spies(monkeypatch, ollama_reachable=True, name="p07c_3")
    assert calls["ollama"] == 1
    assert result["method"] == "ollama"


def test_p07c_old_variables_no_longer_turn_the_layer_on(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DISABLE_LLM_CLASSIFIERS", "false")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFY_LOCAL_ONLY", "false")
    result, calls = _classify_with_spies(monkeypatch, ollama_reachable=True, name="p07c_4")
    assert calls == {"ollama": 0, "api": 0}


def test_p07c_flag_on_keeps_the_cloud_api_layer_opt_in(home, monkeypatch):
    """With the layer on, a prompt reaches a cloud API only when the user also
    opted out of local-only classification (privacy-first default)."""
    monkeypatch.setenv("LLM_ROUTER_HOOK_LLM_LAYER", "on")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")

    def no_ollama(text):
        return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Reachable())
    auto_route = _load_hook("p07c_5", "auto-route.py")
    api_calls = []
    monkeypatch.setattr(auto_route, "classify_with_ollama", no_ollama)
    monkeypatch.setattr(auto_route, "classify_with_gemini", lambda t: api_calls.append(t) or None)
    monkeypatch.setattr(auto_route, "classify_with_openai", lambda t: api_calls.append(t) or None)
    auto_route.classify_prompt(_AMBIGUOUS)
    assert api_calls == []

    monkeypatch.setenv("LLM_ROUTER_CLASSIFY_LOCAL_ONLY", "false")
    auto_route = _load_hook("p07c_6", "auto-route.py")
    monkeypatch.setattr(auto_route, "classify_with_ollama", no_ollama)
    monkeypatch.setattr(auto_route, "classify_with_gemini", lambda t: api_calls.append(t) or None)
    monkeypatch.setattr(auto_route, "classify_with_openai", lambda t: api_calls.append(t) or None)
    auto_route.classify_prompt(_AMBIGUOUS)
    assert len(api_calls) == 2  # gemini, then openai


def test_p07c_flag_is_registered():
    from llm_router.env_registry import ENV_REGISTRY

    assert ENV_REGISTRY["LLM_ROUTER_HOOK_LLM_LAYER"][1] == "hooks/auto-route.py"
    assert "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS" not in ENV_REGISTRY
