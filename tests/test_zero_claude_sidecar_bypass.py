"""Regression: CHZ-AUD-005 — sidecar pre-execution must not bypass zero-Claude.

Audit finding (commit 174941677a88, v0.8.7):
  The sidecar fast-path in ``hooks/auto-route.py`` (~L2602-2635) emits
  ``contextForAgent`` and ``sys.exit(0)`` BEFORE the zero-Claude guard at
  ~L3170. So with ``LLM_ROUTER_ZERO_CLAUDE=1`` AND ``LLM_ROUTER_SIDECAR_PREFETCH=1``,
  a sidecar-matching prompt still injects pre-executed data into Claude's
  context — i.e. Claude IS invoked, violating strict zero-Claude mode.

Contract (see test_zero_claude_bypass.py): zero-Claude → ``decision: block``;
Claude must never receive an advisory ``contextForAgent`` turn.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"

SIDECAR_CANARY = "SIDECAR_CANARY_DATA_ZC"


def _load_hook():
    spec = importlib.util.spec_from_file_location("auto_route_sidecar_zc", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_sidecar_zc"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_hook(prompt: str, home: Path, monkeypatch, capsys, extra_env: dict) -> dict | None:
    """Drive auto-route.main() in-process with the sidecar forced to fire."""
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    for k in list(os.environ):
        if k.startswith("LLM_ROUTER"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    # No real providers → any fall-through routes fail-closed, no network.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("LLM_ROUTER_DISABLE_LLM_CLASSIFIERS", "1")
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)

    # Force the sidecar to fire deterministically with a canary payload.
    import llm_router.sidecar as sc
    monkeypatch.setattr(sc, "is_enabled", lambda: True)
    monkeypatch.setattr(sc, "classify", lambda _p: "routing_distribution")
    monkeypatch.setattr(
        sc, "execute",
        lambda _h, _p: sc.PreExecutionResult(
            handler="routing_distribution", context=SIDECAR_CANARY, duration_ms=1
        ),
    )

    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "session_id": "zc-sidecar",
        })),
    )
    mod = _load_hook()
    try:
        mod.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_zero_claude_blocks_sidecar_prefetch(tmp_path, monkeypatch, capsys):
    """CHZ-AUD-005: zero-Claude + sidecar prefetch must NOT leak to Claude."""
    out = _run_hook(
        "show me my routing today", tmp_path, monkeypatch, capsys,
        extra_env={"LLM_ROUTER_ZERO_CLAUDE": "1", "LLM_ROUTER_SIDECAR_PREFETCH": "1"},
    )
    assert out is not None, "hook produced no output → native Claude turn"
    dumped = json.dumps(out)
    assert SIDECAR_CANARY not in dumped, (
        "zero-Claude leaked: sidecar-executed data was injected into Claude context"
    )
    assert "contextForAgent" not in dumped, (
        "zero-Claude leaked: hook emitted an advisory contextForAgent turn"
    )
    assert out.get("decision") == "block", (
        f"zero-Claude must block, got {out.get('decision')!r}"
    )


def test_sidecar_still_fires_without_zero_claude(tmp_path, monkeypatch, capsys):
    """Guard against over-fixing: sidecar must still work when NOT zero-Claude."""
    out = _run_hook(
        "show me my routing today", tmp_path, monkeypatch, capsys,
        extra_env={"LLM_ROUTER_SIDECAR_PREFETCH": "1"},
    )
    assert out is not None
    assert SIDECAR_CANARY in json.dumps(out), (
        "sidecar should still pre-execute when zero-Claude is OFF"
    )
