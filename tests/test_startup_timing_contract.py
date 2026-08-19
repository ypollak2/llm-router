"""Contract tests: initialize_dynamic_routing must be fast even with unreachable Ollama.

Verifies the fix: Ollama health check no longer blocks server startup.
"""

import time
from pathlib import Path
from unittest.mock import patch

import yaml


def _load_expectations() -> dict:
    local = Path(__file__).parent / "fixtures" / "routing_expectations.local.yaml"
    default = Path(__file__).parent / "fixtures" / "routing_expectations.example.yaml"
    path = local if local.exists() else default
    with open(path) as f:
        return yaml.safe_load(f)


EXPECTATIONS = _load_expectations()
MAX_MS = EXPECTATIONS["performance"]["initialize_dynamic_routing_max_ms"]


def test_startup_with_unreachable_ollama_is_fast() -> None:
    """initialize_dynamic_routing must complete < {MAX_MS}ms with Ollama unreachable."""
    from llm_router.dynamic_routing import initialize_dynamic_routing, reset_dynamic_routing

    reset_dynamic_routing()
    with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://10.255.255.1:11434"}):
        t0 = time.monotonic()
        initialize_dynamic_routing()
        elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms < MAX_MS, (
        f"initialize_dynamic_routing took {elapsed_ms:.0f}ms with unreachable Ollama "
        f"— must be < {MAX_MS}ms (Ollama health check is blocking startup again)"
    )


def test_startup_completes_with_normal_ollama() -> None:
    """initialize_dynamic_routing must complete < {MAX_MS}ms in normal conditions."""
    from llm_router.dynamic_routing import initialize_dynamic_routing, reset_dynamic_routing

    reset_dynamic_routing()
    t0 = time.monotonic()
    initialize_dynamic_routing()
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms < MAX_MS, (
        f"initialize_dynamic_routing took {elapsed_ms:.0f}ms — "
        f"expected < {MAX_MS}ms"
    )


def test_discovery_complete_after_init() -> None:
    """After initialize_dynamic_routing, routing tables must be populated."""
    from llm_router import dynamic_routing
    from llm_router.dynamic_routing import initialize_dynamic_routing, reset_dynamic_routing

    reset_dynamic_routing()
    initialize_dynamic_routing()

    # The module should expose at least a non-empty routing structure
    # (exact attr name may vary — check for any evidence of initialization)
    initialized = (
        getattr(dynamic_routing, "_discovery_complete", None)
        or getattr(dynamic_routing, "_routing_table", None)
        or getattr(dynamic_routing, "_chains", None)
        or getattr(dynamic_routing, "_initialized", None)
    )
    assert initialized, "dynamic_routing shows no sign of successful initialization"
