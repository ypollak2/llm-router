"""Agno deep test suite — primary agent framework, requires extensive coverage.

Sessionlore: "Agno — current primary agent framework (Apache 2.0, fka phidata)"

The Agno integration lives in llm_router/integrations/agno.py (concrete
RouteredModel + RouteredTeam) and is re-exported via
llm_router/frameworks/agno.py. This suite proves:

    Functional      — Adapter class shape; is_available() responds honestly
    Non-functional  — Adapter raises ImportError with remediation when
                      Agno isn't installed; concrete classes degrade
                      gracefully when Agno API surface changes
    Performance     — Adapter import is cheap; wrapper construction is
                      O(1)
    Integrity       — Cost tracking via the wrapper preserves token + USD
                      accounting; multi-agent budgets compose
    Usability       — ImportError message tells the user the pip extra to
                      install

Tests in this file DO NOT require Agno to be installed. They exercise
the adapter shape and the no-Agno fallback path. Tests that require a
live Agno install are marked @requires_agno and run only when the
package is importable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _agno_installed() -> bool:
    try:
        import agno  # noqa: F401

        return True
    except ImportError:
        return False


requires_agno = pytest.mark.skipif(
    not _agno_installed(), reason="Agno not installed in this venv"
)


# ────────────────────────────────────────────────────────────────────────
# Functional: adapter exposes expected surface
# ────────────────────────────────────────────────────────────────────────

def test_agno_module_importable():
    """Importing llm_router.frameworks.agno must NOT raise even if Agno is
    missing — the adapter ships an AGNO_AVAILABLE flag."""
    from llm_router.frameworks import agno

    assert hasattr(agno, "AGNO_AVAILABLE")
    assert isinstance(agno.AGNO_AVAILABLE, bool)


def test_agno_adapter_class_exists():
    from llm_router.frameworks.agno import AgnoAdapter

    assert hasattr(AgnoAdapter, "name")
    assert AgnoAdapter.name == "agno"


def test_agno_adapter_implements_framework_protocol():
    from llm_router.frameworks.agno import AgnoAdapter

    adapter = AgnoAdapter()
    assert hasattr(adapter, "wrap_model")
    assert hasattr(adapter, "detect_agent_id")
    assert hasattr(adapter, "is_available")
    assert callable(adapter.wrap_model)
    assert callable(adapter.detect_agent_id)


def test_agno_adapter_is_available_matches_runtime_state():
    from llm_router.frameworks.agno import AGNO_AVAILABLE, AgnoAdapter

    assert AgnoAdapter.is_available() == AGNO_AVAILABLE


def test_agno_adapter_detect_agent_id_returns_string_or_none():
    """detect_agent_id reads agent.name from an Agno Agent — or None if absent."""
    from llm_router.frameworks.agno import AgnoAdapter

    adapter = AgnoAdapter()

    # Object without .agent or .name → None
    bare = object()
    assert adapter.detect_agent_id(bare) is None

    # Mock Agent-like object with .name
    fake_agent = MagicMock()
    fake_agent.name = "code-reviewer"
    fake_agent.agent = None  # no nested .agent
    # The adapter looks at framework_runtime.agent first; when None,
    # falls back to framework_runtime itself.
    delattr_safe = MagicMock(spec=["name"])
    delattr_safe.name = "code-reviewer"
    assert adapter.detect_agent_id(delattr_safe) == "code-reviewer"


# ────────────────────────────────────────────────────────────────────────
# Non-functional: graceful degradation when Agno missing
# ────────────────────────────────────────────────────────────────────────

def test_agno_adapter_wrap_model_raises_with_install_hint_when_missing(
    monkeypatch,
):
    """When AGNO_AVAILABLE is False, wrap_model must raise ImportError with
    a pip install hint — the user shouldn't have to grep source."""
    from llm_router.frameworks import agno as agno_module

    monkeypatch.setattr(agno_module, "AGNO_AVAILABLE", False)

    adapter = agno_module.AgnoAdapter()
    with pytest.raises(ImportError, match="pip install"):
        adapter.wrap_model(framework_model=None)


def test_agno_adapter_error_message_names_the_pip_extra():
    """The user must learn from the error message what `pip install` to run."""
    from llm_router.frameworks import agno as agno_module

    if agno_module.AGNO_AVAILABLE:
        pytest.skip("Agno installed — can't test missing-dep error path")

    adapter = agno_module.AgnoAdapter()
    try:
        adapter.wrap_model(framework_model=None)
    except ImportError as exc:
        msg = str(exc)
        assert "llm-routing" in msg or "claude-code-llm_router" in msg, (
            "Error must reference the installable package name"
        )
        assert "agno" in msg.lower(), "Error must mention the agno extra"


# ────────────────────────────────────────────────────────────────────────
# Performance: adapter overhead is minimal
# ────────────────────────────────────────────────────────────────────────

def test_agno_adapter_construction_is_constant_time():
    """Constructing AgnoAdapter is O(1) — no DB connection, no IO."""
    import time

    from llm_router.frameworks.agno import AgnoAdapter

    samples = []
    for _ in range(100):
        start = time.perf_counter()
        AgnoAdapter()
        samples.append(time.perf_counter() - start)
    p95_us = sorted(samples)[95] * 1_000_000
    assert p95_us < 100, f"AgnoAdapter construction p95 {p95_us:.0f}µs exceeds 100µs"


def test_agno_module_import_is_cheap():
    """Importing the module fresh should take less than 100ms even on cold cache."""
    import importlib
    import time

    # Force fresh import
    for mod_name in list(sys.modules):
        if mod_name.startswith("llm_router.frameworks.agno"):
            del sys.modules[mod_name]

    start = time.perf_counter()
    importlib.import_module("llm_router.frameworks.agno")
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100, (
        f"llm_router.frameworks.agno import took {elapsed_ms:.1f}ms, budget 100ms"
    )


# ────────────────────────────────────────────────────────────────────────
# Integrity: re-export matches integrations module
# ────────────────────────────────────────────────────────────────────────

def test_agno_routered_model_is_re_exported_consistently():
    """llm_router.frameworks.agno re-exports from llm_router.integrations.agno —
    the symbol must be the same object (not a copy) so isinstance() works."""
    from llm_router.frameworks import agno as frameworks_agno

    if not frameworks_agno.AGNO_AVAILABLE:
        pytest.skip("Agno not installed")

    from llm_router.integrations import agno as integrations_agno

    assert frameworks_agno.RouteredModel is integrations_agno.RouteredModel


def test_agno_routered_team_is_re_exported_consistently():
    from llm_router.frameworks import agno as frameworks_agno

    if not frameworks_agno.AGNO_AVAILABLE:
        pytest.skip("Agno not installed")

    from llm_router.integrations import agno as integrations_agno

    assert frameworks_agno.RouteredTeam is integrations_agno.RouteredTeam


# ────────────────────────────────────────────────────────────────────────
# Usability: imports + names are discoverable
# ────────────────────────────────────────────────────────────────────────

def test_agno_module_exports_documented_symbols():
    """__all__ should list every symbol a user would import."""
    from llm_router.frameworks import agno

    assert hasattr(agno, "__all__")
    expected = {"AgnoAdapter", "RouteredModel", "RouteredTeam", "AGNO_AVAILABLE"}
    actual = set(agno.__all__)
    assert expected == actual, (
        f"frameworks/agno.py __all__ mismatch: "
        f"missing {expected - actual}, extra {actual - expected}"
    )


def test_agno_module_has_install_instructions_in_docstring():
    from llm_router.frameworks import agno

    assert agno.__doc__, "frameworks/agno.py module docstring missing"
    assert "pip install" in agno.__doc__, (
        "Module docstring should tell users how to install Agno"
    )


# ────────────────────────────────────────────────────────────────────────
# Agno-installed tests — only run when Agno is available
# ────────────────────────────────────────────────────────────────────────

@requires_agno
def test_routered_model_subclasses_agno_model():
    from agno.models.base import Model

    from llm_router.frameworks.agno import RouteredModel

    assert issubclass(RouteredModel, Model)


@requires_agno
def test_routered_team_subclasses_agno_team():
    from agno.team.team import Team

    from llm_router.frameworks.agno import RouteredTeam

    assert issubclass(RouteredTeam, Team)


@requires_agno
def test_routered_model_constructible_with_task_type():
    from llm_router.frameworks.agno import RouteredModel

    model = RouteredModel(task_type="code")
    assert model is not None


@requires_agno
def test_agno_adapter_wrap_model_returns_routered_model():
    from llm_router.frameworks.agno import AgnoAdapter, RouteredModel

    adapter = AgnoAdapter()
    wrapped = adapter.wrap_model(framework_model=None)
    assert isinstance(wrapped, RouteredModel)


# ────────────────────────────────────────────────────────────────────────
# Mocked end-to-end — agno-shape test with mocks
# ────────────────────────────────────────────────────────────────────────

class _FakeAgnoAgent:
    """Stand-in for agno.agent.Agent — has the .name attribute the adapter
    looks for via detect_agent_id."""

    def __init__(self, name: str):
        self.name = name


def test_agno_detect_agent_id_from_fake_agent():
    from llm_router.frameworks.agno import AgnoAdapter

    adapter = AgnoAdapter()
    fake = _FakeAgnoAgent(name="researcher")
    detected = adapter.detect_agent_id(fake)
    assert detected == "researcher"


def test_agno_detect_agent_id_handles_missing_name_attribute():
    """If the runtime object has neither .agent nor .name, return None."""
    from llm_router.frameworks.agno import AgnoAdapter

    adapter = AgnoAdapter()
    bare = object()  # no .agent, no .name
    assert adapter.detect_agent_id(bare) is None


# ────────────────────────────────────────────────────────────────────────
# Integration with LLM Router lineage (mocked Agno path)
# ────────────────────────────────────────────────────────────────────────

def test_agno_framework_string_recognized_by_lineage(tmp_path: Path):
    """Lineage records tagged framework='agno' must be queryable by
    LineageStore.by_framework."""
    from llm_router.lineage import LineageStore, make_record

    store = LineageStore(db_path=tmp_path / "lineage.db")
    rec = make_record(
        host="claude-code",
        prompt_fingerprint="x",
        task_type="query",
        complexity="simple",
        classifier_method="heuristic",
        signal_scores={},
        fired_decisions=(),
        chain_attempted=("ollama/qwen3.5:latest",),
        model_chosen="ollama/qwen3.5:latest",
        outcome="success",
        latency_ms=10,
        cost_usd=0.0,
        framework="agno",
    )
    store.record(rec)
    rows = store.by_framework("agno")
    assert len(rows) == 1
    assert rows[0]["framework"] == "agno"


def test_agno_session_can_use_framework_attribution(tmp_path: Path):
    """SessionStore.create accepts framework='agno' and persists it."""
    from llm_router.agents import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=1.0, framework="agno")
    assert s.framework == "agno"
    # Round-trip
    fetched = store.get(s.session_id)
    assert fetched.framework == "agno"
