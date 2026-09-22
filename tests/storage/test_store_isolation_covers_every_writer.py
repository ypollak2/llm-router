"""T00 / M-04 — every store that writes to ~/.llm-router must honour the test sandbox.

The audit of 2026-09-21 found `LLM_ROUTER_HOME` is not universally honoured: 120
sites compose ``~/.llm-router`` directly and five modules honour five different
override variables. That is not a tidiness problem. It is how seven synthetic
rows from a development session reached the operator's real routing ledger and
had to be removed by hand.

`paths.py` already exists as the canonical resolver (RED2-07) and already
documents this exact incident. These tests assert the remaining writers actually
use it, so a store cannot silently escape the sandbox again.

Deliberately a *path* assertion, not a filesystem-mutation assertion: the live
routing hook writes to the real ~/.llm-router continuously while the suite runs,
so any guard based on mtimes of the real directory would be flaky by
construction — it would fail according to what the developer was doing.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _assert_sandboxed(resolved: Path, tmp_path: Path, store: str) -> None:
    real_home = Path.home() / ".llm-router"
    assert real_home not in resolved.parents and resolved != real_home, (
        f"{store} resolved to {resolved}, inside the operator's real "
        f"{real_home}, while the isolation fixture was active. This is the "
        f"M-04 escape: a test writing here mutates real router state."
    )
    assert tmp_path in resolved.parents, (
        f"{store} resolved to {resolved}, which is outside the per-test "
        f"sandbox {tmp_path}."
    )


def test_routing_quality_ledger_is_sandboxed(tmp_path):
    """The North Star ledger. Reads LLM_ROUTER_ROUTING_LEDGER, not LLM_ROUTER_HOME."""
    from llm_router import routing_quality

    _assert_sandboxed(routing_quality._default_ledger(), tmp_path, "routing_quality ledger")


def test_quota_tracker_usage_json_is_sandboxed(tmp_path):
    """USAGE_JSON is a class attribute evaluated at import time, so no env var
    set after import can move it. Ten hooks consume this file."""
    from llm_router.quota_tracker import QuotaTracker

    _assert_sandboxed(Path(QuotaTracker.USAGE_JSON), tmp_path, "quota_tracker usage.json")


def test_result_cache_dir_is_sandboxed(tmp_path):
    """_ROUTER_DIR was a module-level constant evaluated at import time."""
    from llm_router import result_cache

    _assert_sandboxed(Path(result_cache._router_dir()), tmp_path, "result_cache dir")


@pytest.mark.parametrize(
    "module_name, attr",
    [
        ("llm_router.trace", "_trace_path"),
        ("llm_router.hooks.tool_intercept", None),
    ],
)
def test_env_honouring_writers_stay_sandboxed(tmp_path, module_name, attr):
    """These two already read LLM_ROUTER_HOME at call time. Pinned so a future
    refactor to an import-time constant is caught here rather than in the
    operator's home directory."""
    mod = importlib.import_module(module_name)
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "LLM_ROUTER_HOME" in src, (
        f"{module_name} no longer reads LLM_ROUTER_HOME; it can now escape the sandbox."
    )


def test_this_suite_is_not_vacuous(tmp_path):
    """Anti-vacuity guard, per the repo rule that a check reporting zero failures
    must be shown to have found something to check.

    `_assert_sandboxed` must actually reject a real-home path. If this fails,
    every other assertion in this file is decorative."""
    real = Path.home() / ".llm-router" / "routing_quality.jsonl"
    with pytest.raises(AssertionError, match="real"):
        _assert_sandboxed(real, tmp_path, "deliberate known-positive")
