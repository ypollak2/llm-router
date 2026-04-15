"""Tests for standalone hook JSON IPC helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTO_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _load_hook_module(path: Path, name_prefix: str):
    spec = importlib.util.spec_from_file_location(f"{name_prefix}_{uuid.uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_auto_route_atomic_write_preserves_existing_target_until_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    auto_route = _load_hook_module(AUTO_ROUTE_HOOK, "auto_route_hook")

    target = tmp_path / ".llm-router" / "pending_route_test.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    old_data = {"expected_tool": "llm_query", "issued_at": 1.0}
    new_data = {"expected_tool": "llm_code", "issued_at": 2.0}
    target.write_text(json.dumps(old_data), encoding="utf-8")

    observed_before_swap: dict | None = None
    real_replace = auto_route.os.replace

    def wrapped_replace(src: str, dst: str | Path) -> None:
        nonlocal observed_before_swap
        observed_before_swap = json.loads(Path(dst).read_text(encoding="utf-8"))
        real_replace(src, dst)

    monkeypatch.setattr(auto_route.os, "replace", wrapped_replace)

    auto_route._write_json_atomic(target, new_data)

    assert observed_before_swap == old_data
    assert json.loads(target.read_text(encoding="utf-8")) == new_data
    assert list(target.parent.glob("*.tmp")) == []


def test_enforce_route_retries_partial_pending_state_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    enforce_route = _load_hook_module(ENFORCE_ROUTE_HOOK, "enforce_route_hook")

    session_id = "sess-retry"
    pending_path = tmp_path / ".llm-router" / f"pending_route_{session_id}.json"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text('{"expected_tool":', encoding="utf-8")

    valid_data = {
        "expected_tool": "llm_query",
        "task_type": "query",
        "complexity": "simple",
        "issued_at": time.time(),
        "expires_at": time.time() + 60,
        "session_id": session_id,
    }
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        pending_path.write_text(json.dumps(valid_data), encoding="utf-8")

    monkeypatch.setattr(enforce_route.time, "sleep", fake_sleep)

    pending = enforce_route._read_pending(session_id)

    # _read_pending adds _remaining_seconds field for routing window visibility
    assert pending is not None
    assert pending["expected_tool"] == valid_data["expected_tool"]
    assert pending["task_type"] == valid_data["task_type"]
    assert pending["complexity"] == valid_data["complexity"]
    assert pending["session_id"] == valid_data["session_id"]
    assert "_remaining_seconds" in pending  # Added by _read_pending
    assert sleeps == [0.01]
