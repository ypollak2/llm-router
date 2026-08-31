"""Synthetic new-session test for the three session-UX features from #12.

These tests fabricate the inputs a fresh Claude Code session would pass
through the llm_router hooks (SessionStart JSON for warm-up; a series of
UserPromptSubmit payloads for the mini-summary counter; a legacy
model_tracking.jsonl for the meta-bucket relabel) and exercise the
hook code paths in-process. No actual ``claude`` invocation is needed
— the same functions that fire on every real session-start /
prompt-submit get called directly, with assertions on their
side-effects (subprocess argv, return values, persisted counter file,
adapted-row schema).

Why in-process rather than via subprocess: the three features under
test are pure-Python functions wrapped by the hook's ``main()``. Calling
them directly lets us monkey-patch external dependencies (``subprocess.Popen``
for warm-up, ``Path.home()`` for counter isolation) and inspect
return values precisely. The hook's ``main()`` is exercised separately
by the rest of the test suite.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_HOOKS_SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks"


def _load_hook_module(name: str, path: Path):
    """Hook scripts have hyphens in their filenames (``session-start.py``),
    so we can't ``import`` them — use importlib to load by path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Feature 1: Ollama background warm-up ────────────────────────────────────


def test_warm_ollama_bg_spawns_curl_with_right_url_and_model(monkeypatch) -> None:
    """SessionStart's warm-up function must fire a detached curl POST to
    Ollama's generate endpoint with the configured model.

    Verifies the argv contains the warm-up model and base URL, and that
    the spawn detaches via ``start_new_session=True`` so the hook
    doesn't wait on the curl call.
    """
    mod = _load_hook_module(
        "_ss_hook", _HOOKS_SRC / "session-start.py"
    )
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_WARMUP", raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP_MODEL", "qwen3.5:latest")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return type("FakeProc", (), {"pid": 12345})()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod._warm_ollama_bg()

    assert "argv" in captured, "expected subprocess.Popen to be called"
    argv = captured["argv"]
    assert argv[0] == "curl"
    assert "http://localhost:11434/api/generate" in argv
    # The JSON payload sits as the last positional argument after -d.
    payload_idx = argv.index("-d") + 1
    payload = json.loads(argv[payload_idx])
    assert payload["model"] == "qwen3.5:latest"
    assert payload["stream"] is False
    # Detached: hook can't be blocked by curl latency.
    assert captured["kwargs"].get("start_new_session") is True


def test_warm_ollama_bg_respects_opt_out_env_var(monkeypatch) -> None:
    """Setting LLM_ROUTER_OLLAMA_WARMUP=off must suppress the curl spawn."""
    mod = _load_hook_module(
        "_ss_hook_off", _HOOKS_SRC / "session-start.py"
    )
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP", "off")

    called = {"n": 0}

    def fake_popen(argv, **kwargs):
        called["n"] += 1
        return type("FakeProc", (), {"pid": 0})()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod._warm_ollama_bg()
    assert called["n"] == 0


# ── Feature 2: Mini-summary widget every N prompts ──────────────────────────


@pytest.fixture
def auto_route_module(monkeypatch, tmp_path: Path):
    """Load the auto-route hook with HOME redirected to a tmp dir so the
    counter file (``~/.llm-router/session_prompt_counts.json``) doesn't
    interfere with the real one or with parallel tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir()
    mod = _load_hook_module(
        f"_ar_hook_{tmp_path.name}", _HOOKS_SRC / "auto-route.py"
    )
    # Re-bind the module-level Path that captured the *real* HOME at import.
    mod._PROMPT_COUNTS = tmp_path / ".llm-router" / "session_prompt_counts.json"
    return mod


def test_prompt_counter_increments_per_session(auto_route_module, tmp_path) -> None:
    """``_bump_session_prompt_count`` returns the monotonically-increasing
    count for the given session_id and isolates per-session state."""
    bump = auto_route_module._bump_session_prompt_count
    for i in range(1, 6):
        assert bump("session-A") == i
    # Different session_id starts fresh — does not see session-A's count.
    assert bump("session-B") == 1
    # session-A continues from where it left off.
    assert bump("session-A") == 6
    # State persisted to disk.
    persisted = json.loads(auto_route_module._PROMPT_COUNTS.read_text())
    assert persisted == {"session-A": 6, "session-B": 1}


def test_mini_summary_fires_on_multiples_of_n(auto_route_module) -> None:
    """The widget should trigger on prompt #10, #20, #30 — and only those.

    Drives 25 simulated prompts through the counter and asserts the
    Nth-prompt predicate matches the documented contract: ``n > 0 and
    n % LLM_ROUTER_MINI_SUMMARY_EVERY == 0``.
    """
    bump = auto_route_module._bump_session_prompt_count
    fired = []
    every = 10
    for _ in range(25):
        n = bump("session-X")
        if n > 0 and n % every == 0:
            fired.append(n)
    assert fired == [10, 20]


def test_build_mini_summary_returns_compact_block(auto_route_module, monkeypatch) -> None:
    """The widget format is the contract — three lines, leading bullseye
    emoji, mentions route count + top tier + top task + the ``llm_router
    summary`` follow-up command."""
    # Inject a fake LineageStore.recent so we don't depend on real data.
    fake_rows = [
        {"timestamp": 1.0, "model_tier": "cheap", "task_type": "query", "cost_usd": 0.001},
        {"timestamp": 2.0, "model_tier": "cheap", "task_type": "query", "cost_usd": 0.001},
        {"timestamp": 3.0, "model_tier": "mid",   "task_type": "code",  "cost_usd": 0.003},
    ]

    class _FakeStore:
        def recent(self, limit: int = 200) -> list[dict]:
            return fake_rows

    monkeypatch.setattr(
        "llm_router.lineage.LineageStore", lambda *a, **kw: _FakeStore()
    )
    block = auto_route_module._build_mini_summary()
    assert block is not None
    assert block.startswith("📊 llm_router session check")
    assert "routes: 3" in block
    # Most common tier is "cheap" (2 vs 1), most common task is "query" (2 vs 1)
    assert "top tier: cheap" in block
    assert "top task: query" in block
    assert "llm-router summary" in block  # follow-up command pointer
    assert block.count("\n") == 2  # exactly 3 lines


def test_build_mini_summary_returns_none_when_no_rows(auto_route_module, monkeypatch) -> None:
    """Empty store -> None — caller skips injection. Don't show a widget
    with zero data; that's noise, not signal."""
    class _EmptyStore:
        def recent(self, limit: int = 200) -> list[dict]:
            return []

    monkeypatch.setattr(
        "llm_router.lineage.LineageStore", lambda *a, **kw: _EmptyStore()
    )
    assert auto_route_module._build_mini_summary() is None


# ── Feature 3: Meta-bucket relabel in LineageStore.recent() ─────────────────


def test_recent_relabels_unknown_model_to_llm_router_internal(tmp_path: Path) -> None:
    """When the legacy adapter sees ``selected_model='unknown'`` or
    ``task_type`` in {coordination, introspect}, it must rewrite the
    row to model='llm_router-internal' / tier='meta' / provider='meta' so
    the dashboard shows them in an honest bucket."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from llm_router.lineage import LineageStore

    legacy = tmp_path / "model_tracking.jsonl"
    legacy.write_text("\n".join([
        # Real route — should NOT be relabeled.
        json.dumps({
            "timestamp": 100.0, "task_type": "query", "complexity": "simple",
            "selected_model": "gemini-2.5-flash", "provider": "gemini",
            "cost_usd_estimate": 0.0001,
        }),
        # selected_model="unknown" → relabel.
        json.dumps({
            "timestamp": 101.0, "task_type": "code", "complexity": "moderate",
            "selected_model": "unknown", "provider": "unknown",
        }),
        # task_type="coordination" → relabel even with a real model.
        json.dumps({
            "timestamp": 102.0, "task_type": "coordination", "complexity": "moderate",
            "selected_model": "codex/gpt-4o", "provider": "codex",
        }),
        # task_type="introspect" → relabel.
        json.dumps({
            "timestamp": 103.0, "task_type": "introspect", "complexity": "simple",
            "selected_model": "claude/sonnet", "provider": "anthropic",
        }),
    ]))

    store = LineageStore(router_dir=tmp_path)
    rows = store.recent(limit=10)
    # Most-recent first → introspect, coordination, unknown, gemini.
    by_task = {r["task_type"]: r for r in rows}

    # Real route: untouched.
    assert by_task["query"]["model_chosen"] == "gemini-2.5-flash"
    assert by_task["query"]["model_tier"] == "cheap"
    assert by_task["query"]["host"] == "gemini"
    # selected_model="unknown" → relabel
    assert by_task["code"]["model_chosen"] == "llm_router-internal"
    assert by_task["code"]["model_tier"] == "meta"
    assert by_task["code"]["host"] == "meta"
    # task_type="coordination" → relabel even though there was a real model
    assert by_task["coordination"]["model_chosen"] == "llm_router-internal"
    assert by_task["coordination"]["model_tier"] == "meta"
    # task_type="introspect" → relabel
    assert by_task["introspect"]["model_chosen"] == "llm_router-internal"
    assert by_task["introspect"]["model_tier"] == "meta"


def test_recent_does_not_relabel_genuine_routing_failures(tmp_path: Path) -> None:
    """A row where the model string is genuinely unknown (provider field
    set to something real but selected_model is empty) should NOT be
    swept under the meta label — it's a real failure mode that should
    surface as such. Only the specific 'unknown' literal triggers the
    relabel."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from llm_router.lineage import LineageStore

    legacy = tmp_path / "model_tracking.jsonl"
    legacy.write_text(json.dumps({
        "timestamp": 200.0, "task_type": "query", "complexity": "simple",
        "selected_model": "",  # empty string, not "unknown"
        "provider": "gemini",
    }))
    rows = LineageStore(router_dir=tmp_path).recent(limit=5)
    assert len(rows) == 1
    # Empty model string -> tier_for_model returns Tier.UNKNOWN, NOT relabeled.
    assert rows[0]["model_chosen"] == ""
    assert rows[0]["model_tier"] == "unknown"
    assert rows[0]["host"] == "gemini"
