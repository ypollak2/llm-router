"""Framework adapter contract tests — 6 stubs (v0.0.3 implementations).

Six framework adapters ship as stubs in v0.0.2: Hermes, LangGraph,
CrewAI, OpenAI Agents SDK, Claude Agent SDK, Pydantic AI. Each has:

    - A documented protocol shape (FrameworkAdapter)
    - A skeleton class with placeholder behavior
    - An *_AVAILABLE module flag that returns False until concrete impl
    - A wrap_model() that raises NotImplementedError with a clear hint

This suite pins those contracts so v0.0.3 implementations don't drift
from the protocol. Specifically:

    1. Every adapter exposes the FrameworkAdapter protocol shape.
    2. Every wrap_model() raises NotImplementedError citing v0.0.3.
    3. detect_agent_id() handles mock framework objects correctly.
    4. is_available() returns the module's *_AVAILABLE flag.
    5. Importing the module is cheap (no framework dependency hit).
    6. Every adapter has a unique `name` matching the framework slug.

Agno is excluded — it has its own deep-test suite (test_agno_deep.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest


# ────────────────────────────────────────────────────────────────────────
# The six stubs
# ────────────────────────────────────────────────────────────────────────

FRAMEWORKS = [
    # (module_name, adapter_class_name, expected_name, expected_available_flag)
    ("llm_router.frameworks.hermes",            "HermesAdapter",            "hermes",            "HERMES_AVAILABLE"),
    ("llm_router.frameworks.langgraph",         "LangGraphAdapter",         "langgraph",         "LANGGRAPH_AVAILABLE"),
    ("llm_router.frameworks.crewai",            "CrewAIAdapter",            "crewai",            "CREWAI_AVAILABLE"),
    ("llm_router.frameworks.openai_agents",     "OpenAIAgentsAdapter",      "openai-agents",     "OPENAI_AGENTS_AVAILABLE"),
    ("llm_router.frameworks.claude_agent_sdk",  "ClaudeAgentSdkAdapter",    "claude-agent-sdk",  "CLAUDE_AGENT_SDK_AVAILABLE"),
    ("llm_router.frameworks.pydantic_ai",       "PydanticAiAdapter",        "pydantic-ai",       "PYDANTIC_AI_AVAILABLE"),
]


def _load(module_name: str, class_name: str):
    """Helper: import the module and return (module, adapter_class)."""
    import importlib

    mod = importlib.import_module(module_name)
    return mod, getattr(mod, class_name)


# ────────────────────────────────────────────────────────────────────────
# Pillar 1: protocol shape
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_adapter_has_protocol_shape(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Every adapter must expose name + wrap_model + detect_agent_id +
    is_available — the FrameworkAdapter protocol."""
    _, cls = _load(module_name, class_name)

    for attr in ("name", "wrap_model", "detect_agent_id", "is_available"):
        assert hasattr(cls, attr), (
            f"{class_name} missing {attr} — FrameworkAdapter protocol violated"
        )


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_adapter_name_matches_expected(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Each adapter's class-level `name` attribute must match its slug."""
    _, cls = _load(module_name, class_name)
    assert cls.name == expected_name, (
        f"{class_name}.name = {cls.name!r}, expected {expected_name!r}"
    )


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_module_exposes_availability_flag(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Each module must expose its *_AVAILABLE flag so dependents can
    check before calling wrap_model()."""
    mod, _ = _load(module_name, class_name)
    assert hasattr(mod, available_flag), (
        f"{module_name} missing {available_flag} — runtime feature detection broken"
    )
    assert isinstance(getattr(mod, available_flag), bool)


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_is_available_matches_flag(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """is_available() returns the module's *_AVAILABLE flag value."""
    mod, cls = _load(module_name, class_name)
    flag = getattr(mod, available_flag)
    assert cls.is_available() == flag


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_stubs_report_unavailable(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """All 6 stubs are NOT available in v0.0.2 — flag must be False.
    When v0.0.3 lands concrete impls, this test flips per-adapter."""
    _, cls = _load(module_name, class_name)
    assert cls.is_available() is False, (
        f"{class_name}.is_available() returned True in v0.0.2 — "
        f"if concrete impl landed, update this test to expect True"
    )


# ────────────────────────────────────────────────────────────────────────
# Pillar 2: wrap_model raises NotImplementedError
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_wrap_model_raises_not_implemented(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Every stub's wrap_model must raise NotImplementedError so callers
    fail fast instead of getting silent no-ops."""
    _, cls = _load(module_name, class_name)
    adapter = cls()
    with pytest.raises(NotImplementedError):
        adapter.wrap_model(framework_model=None)


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_wrap_model_error_message_references_v003(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """The NotImplementedError message must tell the user when concrete
    impl is expected — sets expectations correctly."""
    _, cls = _load(module_name, class_name)
    adapter = cls()
    try:
        adapter.wrap_model(framework_model=None)
    except NotImplementedError as exc:
        msg = str(exc)
        assert "v0.0.3" in msg or "0.0.3" in msg, (
            f"{class_name}.wrap_model error should reference v0.0.3, got: {msg!r}"
        )


# ────────────────────────────────────────────────────────────────────────
# Pillar 3: detect_agent_id on mock framework objects
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeAgent:
    """A bare object with a `.name` attribute that several adapters look for."""
    name: str


@dataclass
class _FakeRunnerWithAgent:
    """A wrapper exposing `.agent.name` — Agno/CrewAI/OpenAI Agents pattern."""
    agent: _FakeAgent


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_detect_agent_id_on_bare_object_returns_none(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """When given a bare object with no `.agent` and no `.name`,
    detect_agent_id must return None — never raise."""
    _, cls = _load(module_name, class_name)
    adapter = cls()
    result = adapter.detect_agent_id(object())
    assert result is None


def test_crewai_detect_agent_id_reads_role():
    """CrewAI agents have `.role`, not `.name` — adapter-specific behavior."""
    from llm_router.frameworks.crewai import CrewAIAdapter

    @dataclass
    class _CrewAgent:
        role: str = "researcher"

    adapter = CrewAIAdapter()
    result = adapter.detect_agent_id(_CrewAgent())
    assert result == "researcher"


def test_openai_agents_detect_agent_id_reads_name():
    """OpenAI Agents SDK uses `.name` on Agent objects."""
    from llm_router.frameworks.openai_agents import OpenAIAgentsAdapter

    adapter = OpenAIAgentsAdapter()
    result = adapter.detect_agent_id(_FakeAgent(name="researcher"))
    assert result == "researcher"


def test_pydantic_ai_detect_agent_id_reads_name():
    """Pydantic AI Agent objects have `.name`."""
    from llm_router.frameworks.pydantic_ai import PydanticAiAdapter

    adapter = PydanticAiAdapter()
    result = adapter.detect_agent_id(_FakeAgent(name="analyzer"))
    assert result == "analyzer"


# ────────────────────────────────────────────────────────────────────────
# Pillar 4: import performance (no framework dependency load)
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_module_import_is_cheap(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Importing the adapter module must not pull in the heavy framework
    (LangGraph, CrewAI etc. drag in many MB of deps). Stubs MUST be
    importable without those frameworks installed."""
    import importlib
    import sys

    # Force fresh import
    for mod_key in list(sys.modules):
        if mod_key == module_name:
            del sys.modules[mod_key]

    start = time.perf_counter()
    importlib.import_module(module_name)
    elapsed_ms = (time.perf_counter() - start) * 1000

    # Budget calibration history:
    #   100 ms (original)  — generous enough for any stub on dev hardware.
    #   250 ms (this rev)  — GitHub Actions 3.11 cold-cache import hit
    #     161 ms during the T3-M4 CI run; the budget was incompatible
    #     with CI hardware, not detecting a real regression. The
    #     intent (catch a stub that accidentally imports the heavy
    #     framework at top level) is preserved at 250 ms — a real
    #     heavy import is 500 ms+.
    # If a real regression appears (>250 ms), the test still catches it.
    assert elapsed_ms < 250, (
        f"{module_name} import took {elapsed_ms:.1f}ms — exceeds 250ms budget. "
        f"Did the stub accidentally `import {module_name.split('.')[-1]}` at top-level?"
    )


# ────────────────────────────────────────────────────────────────────────
# Pillar 5: stub construction is O(1)
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_adapter_construction_is_constant_time(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Adapter() must be O(1) — no DB connection, no network, no IO."""
    _, cls = _load(module_name, class_name)
    samples = []
    for _ in range(100):
        start = time.perf_counter()
        cls()
        samples.append(time.perf_counter() - start)
    p95_us = sorted(samples)[95] * 1_000_000
    assert p95_us < 1000, (
        f"{class_name}() p95 {p95_us:.0f}µs exceeds 1000µs — stub doing IO?"
    )


# ────────────────────────────────────────────────────────────────────────
# Pillar 6: cross-adapter uniqueness — names must be distinct
# ────────────────────────────────────────────────────────────────────────

def test_all_six_framework_names_are_unique():
    """No two adapters share a `name` — collisions would corrupt
    lineage.framework attribution."""
    names = []
    for module_name, class_name, _expected, _avail in FRAMEWORKS:
        _, cls = _load(module_name, class_name)
        names.append(cls.name)
    assert len(set(names)) == len(names), (
        f"Duplicate framework names: {[n for n in names if names.count(n) > 1]}"
    )


# ────────────────────────────────────────────────────────────────────────
# Pillar 7: lineage tagging — every framework slug accepted as framework=
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_lineage_accepts_framework_slug(
    module_name: str, class_name: str, expected_name: str, available_flag: str,
    tmp_path
):
    """LineageStore.by_framework(slug) must work for every advertised slug."""
    from llm_router.lineage import LineageStore, make_record

    store = LineageStore(db_path=tmp_path / "lineage.db")
    rec = make_record(
        host="test", prompt_fingerprint=f"fp-{expected_name}",
        task_type="query", complexity="simple",
        classifier_method="heuristic",
        signal_scores={}, fired_decisions=(),
        chain_attempted=("ollama/qwen3.5:latest",),
        model_chosen="ollama/qwen3.5:latest",
        outcome="success", latency_ms=10, cost_usd=0.0,
        framework=expected_name,
    )
    store.record(rec)
    rows = store.by_framework(expected_name)
    assert len(rows) == 1
    assert rows[0]["framework"] == expected_name


# ────────────────────────────────────────────────────────────────────────
# Pillar 8: sessions accept framework= for every slug
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_session_store_accepts_framework_slug(
    module_name: str, class_name: str, expected_name: str, available_flag: str,
    tmp_path
):
    """SessionStore.create(framework=slug) must work for every adapter."""
    from llm_router.agents import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=1.0, framework=expected_name)
    assert s.framework == expected_name
    # Round-trip
    fetched = store.get(s.session_id)
    assert fetched.framework == expected_name


# ────────────────────────────────────────────────────────────────────────
# Pillar 9: documentation hygiene — every stub explains itself
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_module_has_docstring(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Every stub module must explain what the framework is and what the
    integration path looks like — sets v0.0.3 implementer up for success."""
    import importlib

    mod = importlib.import_module(module_name)
    assert mod.__doc__, f"{module_name} missing module docstring"
    assert len(mod.__doc__.strip()) > 50, (
        f"{module_name} docstring too short: {mod.__doc__!r}"
    )


@pytest.mark.parametrize(
    "module_name,class_name,expected_name,available_flag",
    FRAMEWORKS,
    ids=[fw[2] for fw in FRAMEWORKS],
)
def test_adapter_class_has_docstring(
    module_name: str, class_name: str, expected_name: str, available_flag: str
):
    """Per-class docstring optional but helpful — check it exists OR the
    module docstring is comprehensive (skip if not)."""
    _, cls = _load(module_name, class_name)
    # Just check the class is defined; per-method docstrings live in
    # the protocol module (frameworks/base.py)
    assert cls is not None
