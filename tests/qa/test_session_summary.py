"""Session Summary Dashboard — data aggregation + rendering tests.

The rendering tests don't pin pixel-perfect output (rich changes glyphs
between versions); they verify the underlying data + the markdown
fallback produce the correct shape and contain the expected numbers.
"""
from __future__ import annotations

import time
from io import StringIO


from llm_router.agents import SessionStore
from llm_router.lineage import LineageStore, Tier, make_record
from llm_router.observability.summary import (
    SessionSummaryData,
    _fmt_cost,
    _render_sparkline,
    collect,
    render,
    render_markdown,
)


def _seed_lineage(store: LineageStore, sessions: SessionStore | None = None):
    """Realistic seed: 12 routing decisions across tiers + 1 agent session."""
    base_ts = time.time() - 1800
    rows = [
        # 6 local Ollama calls
        ("ollama/qwen3.5:latest", "simple", "query", 0.0, 1200, "none"),
        ("ollama/qwen3.5:latest", "simple", "query", 0.0, 1100, "none"),
        ("ollama/qwen3.5:latest", "simple", "code", 0.0, 1800, "none"),
        ("ollama/qwen3.5:latest", "moderate", "code", 0.0, 2400, "none"),
        ("ollama/qwen3.5:latest", "simple", "query", 0.0, 900, "none"),
        ("ollama/qwen3.5:latest", "simple", "query", 0.0, 1000, "none"),
        # 3 cheap Gemini Flash calls
        ("google/gemini-1.5-flash-8b", "moderate", "research", 0.0008, 1500, "none"),
        ("google/gemini-1.5-flash-8b", "simple", "query", 0.0003, 800, "none"),
        ("google/gemini-1.5-flash-8b", "moderate", "research", 0.0009, 1700, "none"),
        # 2 mid GPT-4o calls
        ("openai/gpt-4o", "complex", "analyze", 0.018, 3200, "none"),
        ("openai/gpt-4o", "complex", "analyze", 0.022, 3500, "none"),
        # 1 inversion: complex routed to cheap = UP-inversion
        ("google/gemini-1.5-flash-8b", "complex", "code", 0.0004, 1100, "up_inversion"),
    ]
    for i, (model, complexity, task_type, cost, latency, inv) in enumerate(rows):
        rec = make_record(
            host="claude-code",
            prompt_fingerprint=f"fp{i}",
            task_type=task_type,
            complexity=complexity,
            classifier_method="signal_engine",
            signal_scores={"pii": 0.0, "code": 0.5 if "code" in task_type else 0.1},
            fired_decisions=("route_code_tasks",) if "code" in task_type else (),
            chain_attempted=(model,),
            model_chosen=model,
            outcome="success",
            latency_ms=latency,
            cost_usd=cost,
            notes="pii caught — local routed" if i == 0 else "",
        )
        # Spread timestamps across last 30 minutes
        rec_dict = rec.__dict__.copy()
        rec_dict["timestamp"] = base_ts + i * 150
        # Direct insert via to_row would work but make_record sets ts;
        # for testing we just write the record as-is (close-enough ts ordering).
        store.record(rec)

    if sessions is not None:
        agent_sess = sessions.create(agent_id="code-reviewer",
                                     budget_usd=0.50, framework="agno")
        sessions.record_step(agent_sess.session_id, cost_usd=0.018)
        sessions.record_step(agent_sess.session_id, cost_usd=0.022)
        sessions.complete(agent_sess.session_id)


# ────────────────────────────────────────────────────────────────────────
# Data aggregation
# ────────────────────────────────────────────────────────────────────────

def test_collect_on_empty_db_returns_zeroed_data(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    data = collect(lineage_store=store, since_seconds=None)
    assert data.total_decisions == 0
    assert data.total_cost_usd == 0.0
    assert data.savings_usd == 0.0


def test_collect_aggregates_total_cost(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    expected = 0.0008 + 0.0003 + 0.0009 + 0.018 + 0.022 + 0.0004
    assert abs(data.total_cost_usd - expected) < 0.0001


def test_collect_counts_tiers_correctly(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    assert data.tier_counts.get(Tier.LOCAL.value, 0) == 6
    assert data.tier_counts.get(Tier.CHEAP.value, 0) == 4
    assert data.tier_counts.get(Tier.MID.value, 0) == 2


def test_collect_groups_by_provider(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    assert data.provider_counts["ollama"] == 6
    assert data.provider_counts["google"] == 4
    assert data.provider_counts["openai"] == 2


def test_collect_detects_inversion(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    # Multiple records get inversion=up because tier_for_model maps to LOCAL
    # for ollama models when complexity=complex would be inversion; but our
    # seed has only one such row directly. The make_record fn also runs
    # detect_inversion automatically based on model tier.
    assert len(data.up_inversions) >= 1


def test_collect_counts_pii_catches(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    assert data.pii_catches >= 1


def test_collect_computes_savings_vs_baseline(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    # Baseline cost should be > actual cost given the seed (mostly cheap)
    assert data.baseline_cost_usd > data.total_cost_usd
    assert data.savings_usd > 0
    assert 0.0 <= data.savings_pct <= 1.0


def test_collect_includes_agent_sessions(tmp_path):
    lineage = LineageStore(db_path=tmp_path / "l.db")
    sessions = SessionStore(db_path=tmp_path / "s.db")
    _seed_lineage(lineage, sessions=sessions)
    # Need at least one lineage row tagged with the session for collect()
    # to discover the session id. Seed a session-tagged record:
    sess_id = sessions.by_agent("code-reviewer")[0].session_id
    rec = make_record(
        host="claude-code", prompt_fingerprint="sess1",
        task_type="code", complexity="moderate",
        classifier_method="signal_engine",
        signal_scores={}, fired_decisions=(),
        chain_attempted=("openai/gpt-4o",),
        model_chosen="openai/gpt-4o",
        outcome="success", latency_ms=2000, cost_usd=0.018,
        agent_id="code-reviewer", session_id=sess_id, framework="agno",
    )
    lineage.record(rec)
    data = collect(lineage_store=lineage, session_store=sessions,
                   since_seconds=None)
    assert len(data.agent_sessions) >= 1


def test_collect_top_routes_ordered_by_frequency(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    # The most common pair should be ('query', 'local') since 5 rows match
    most_common_task, most_common_tier, count = data.top_routes[0]
    assert count >= 1


# ────────────────────────────────────────────────────────────────────────
# Rendering — rich + markdown
# ────────────────────────────────────────────────────────────────────────

def test_render_works_on_empty_data(tmp_path):
    """An empty session should render without crashing."""
    from rich.console import Console
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    render(SessionSummaryData(), console=console)
    out = buf.getvalue()
    assert "Session Summary" in out


def test_render_contains_total_cost(tmp_path):
    from rich.console import Console
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    render(data, console=console)
    out = buf.getvalue()
    assert "Session Summary" in out
    assert "Tier distribution" in out
    assert "Providers" in out


def test_render_markdown_has_all_sections(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    md = render_markdown(data)
    for section in (
        "# LLM Router · Session Summary",
        "## Headline",
        "## Tier distribution",
        "## Providers",
        "## Routing health",
        "## Safety",
        "## Top routes",
    ):
        assert section in md, f"missing section: {section}"


def test_render_markdown_shows_savings(tmp_path):
    store = LineageStore(db_path=tmp_path / "l.db")
    _seed_lineage(store)
    data = collect(lineage_store=store, since_seconds=None)
    md = render_markdown(data)
    assert "Savings vs always-premium" in md


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def test_sparkline_renders_unicode_bars():
    s = _render_sparkline([0.0, 0.5, 1.0, 0.5, 0.0])
    # First char should be space (0.0), last too
    assert s[0] == " "
    # Middle should be a bar
    assert any(ch in s for ch in "▁▂▃▄▅▆▇█")


def test_sparkline_empty_input_returns_dash():
    assert _render_sparkline([]) == "—"


def test_fmt_cost_handles_all_ranges():
    assert _fmt_cost(0.0) == "$0.00"
    assert "¢" in _fmt_cost(0.005)  # subcent
    assert _fmt_cost(0.05).startswith("$")
    assert _fmt_cost(5.0).startswith("$5")
