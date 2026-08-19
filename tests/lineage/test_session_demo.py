"""End-to-end session lifecycle demonstration.

This test simulates a real Claude Code session with routing decisions
and verifies that the session hooks properly initialize, track, and
report on routing efficiency.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def test_full_session_lifecycle_with_report():
    """Simulate complete session: start → decisions → report."""
    from llm_router.lineage import decision_logger
    from llm_router.lineage.lineage_store import LineageStore
    from llm_router.hooks.lineage_integration import (
        init_session_lineage,
        format_routing_section,
    )
    import llm_router.hooks.lineage_integration as li

    print("\n" + "=" * 70)
    print("🚀 SESSION LIFECYCLE TEST — Simulating Real Claude Code Session")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        # ════════════════════════════════════════════════════════════════════
        # PHASE 1: SESSION START
        # ════════════════════════════════════════════════════════════════════
        print("\n📍 PHASE 1: SESSION START")
        print("-" * 70)

        original_state_dir = li.STATE_DIR
        li.STATE_DIR = tmpdir

        try:
            # Initialize session (what SessionStart hook does)
            init_session_lineage()
            print("✅ Lineage tracking initialized")

            # Verify marker file created
            marker = Path(tmpdir) / ".lineage_active"
            assert marker.exists()
            print(f"✅ Session marker created: {marker}")

            # Create fresh store for this session
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store
            print(f"✅ Store ready: JSONL={store.jsonl_file}, DB={store.db_file}")

            # ════════════════════════════════════════════════════════════════════
            # PHASE 2: USER MAKES ROUTING DECISIONS
            # ════════════════════════════════════════════════════════════════════
            print("\n📍 PHASE 2: ROUTING DECISIONS DURING SESSION")
            print("-" * 70)

            from llm_router.lineage import log_routing_decision

            request_id = "session-demo-001"

            # Decision 1: User calls `llm_router budget` → get_caps (simple)
            print("\n  Decision 1: User runs 'llm_router budget --list'")
            log_routing_decision(
                operation="get_caps",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
                input_tokens=45,
                output_tokens=25,
                cost_usd=0.00008,
                latency_ms=120.5,
                request_id=request_id,
            )
            print("    → get_caps → Gemini Flash ($0.00008)")

            # Decision 2: User writes code → llm_code (complex)
            print("  Decision 2: User asks 'write a function to validate emails'")
            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
                input_tokens=500,
                output_tokens=300,
                cost_usd=0.0045,
                latency_ms=2340.0,
                request_id=request_id,
            )
            print("    → llm_code → Sonnet ($0.0045)")

            # Decision 3: Another simple query
            print("  Decision 3: User runs 'llm_router status'")
            log_routing_decision(
                operation="get_spend",
                classification="query/simple",
                selected_model="claude-haiku-4-5",
                selection_reason="router_picked",
                input_tokens=30,
                output_tokens=20,
                cost_usd=0.00003,
                latency_ms=145.0,
                request_id=request_id,
            )
            print("    → get_spend → Haiku ($0.00003)")

            # Decision 4: Analyze existing code (moderate)
            print("  Decision 4: User asks 'analyze my code quality'")
            log_routing_decision(
                operation="llm_analyze",
                classification="analyze/moderate",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
                input_tokens=2000,
                output_tokens=800,
                cost_usd=0.028,
                latency_ms=3200.0,
                request_id=request_id,
            )
            print("    → llm_analyze → Sonnet ($0.028)")

            # Decision 5: Research query
            print("  Decision 5: User asks 'find best practices for error handling'")
            log_routing_decision(
                operation="llm_research",
                classification="research/moderate",
                selected_model="gemini-2.5-flash",
                selection_reason="fallback_after_ollama_timeout",
                fallback_chain=["ollama"],
                fallback_reason="ollama_timeout",
                input_tokens=200,
                output_tokens=150,
                cost_usd=0.00012,
                latency_ms=4500.0,
                request_id=request_id,
            )
            print("    → llm_research → Gemini Flash (fallback from Ollama) ($0.00012)")

            print(f"\n✅ Logged 5 routing decisions for request {request_id}")

            # ════════════════════════════════════════════════════════════════════
            # PHASE 3: VERIFY DATA STORED CORRECTLY
            # ════════════════════════════════════════════════════════════════════
            print("\n📍 PHASE 3: VERIFY STORAGE")
            print("-" * 70)

            # Check JSONL file
            jsonl_count = 0
            with open(store.jsonl_file) as f:
                for line in f:
                    jsonl_count += 1
            print(f"✅ JSONL file: {jsonl_count} decisions logged")

            # Check SQLite database
            from llm_router.lineage.lineage_query import LineageQuery

            query = LineageQuery(store=store)
            recent = query.get_recent(limit=100)
            print(f"✅ SQLite database: {len(recent)} decisions queryable")

            assert len(recent) == 5, "Should have 5 decisions"

            # ════════════════════════════════════════════════════════════════════
            # PHASE 4: ANALYZE ROUTING EFFICIENCY
            # ════════════════════════════════════════════════════════════════════
            print("\n📍 PHASE 4: ROUTING ANALYSIS")
            print("-" * 70)

            from llm_router.lineage.lineage_query import LineageQuery

            query = LineageQuery(store=store)

            # Get metrics
            metrics = {
                "model_count": len(query.get_token_usage_by_model()),
                "operations": len(query.get_model_usage_by_operation()),
                "total_tokens": sum(
                    v["tokens"] for v in query.get_token_usage_by_model().values()
                ),
                "total_cost": sum(
                    v["cost_usd"] for v in query.get_token_usage_by_model().values()
                ),
            }

            print("\n  Metrics:")
            print("    Operations: 5")
            print(f"    Models used: {metrics['model_count']}")
            print(f"    Total tokens: {metrics['total_tokens']:,}")
            print(f"    Total cost: ${metrics['total_cost']:.6f}")

            # Check for waste
            wasteful = query.find_wasteful_operations()
            print("\n  Waste Detection:")
            if wasteful:
                print(f"    ⚠️  Found {len(wasteful)} wasteful operations")
            else:
                print("    ✅ No wasteful routing detected (all appropriate)")

            # Show model distribution
            usage = query.get_token_usage_by_model()
            print("\n  Model Distribution:")
            for model, stats in sorted(
                usage.items(), key=lambda x: x[1]["cost_usd"], reverse=True
            ):
                print(f"    • {model:35} {stats['tokens']:5} tokens  ${stats['cost_usd']:.6f}")

            # ════════════════════════════════════════════════════════════════════
            # PHASE 5: SESSION END REPORT
            # ════════════════════════════════════════════════════════════════════
            print("\n📍 PHASE 5: SESSION END (What user sees)")
            print("-" * 70)

            section = format_routing_section(store=store)
            print(section)

            # ════════════════════════════════════════════════════════════════════
            # VERIFICATION
            # ════════════════════════════════════════════════════════════════════
            print("\n📍 VERIFICATION CHECKLIST")
            print("-" * 70)

            assert len(recent) == 5, "✅ All 5 decisions logged"
            print("✅ All 5 routing decisions captured")

            assert metrics["model_count"] == 3, "✅ 3 unique models used"
            print("✅ Model distribution accurate")

            assert metrics["total_tokens"] == 4070, "✅ Token count correct"
            print("✅ Token accounting correct")

            assert metrics["total_cost"] == pytest.approx(
                0.03273, abs=0.00001
            ), "✅ Cost calculation correct"
            print("✅ Cost calculation correct")

            assert len(wasteful) == 0, "✅ No waste (all appropriate)"
            print("✅ No wasteful routing detected")

            assert "ROUTING EFFICIENCY REPORT" in section
            print("✅ Session-end report formatted correctly")

            assert "5" in section or "Operations" in section
            print("✅ Report includes operation count")

            assert "✅ No wasteful" in section
            print("✅ Report shows clean status")

        finally:
            li.STATE_DIR = original_state_dir

    print("\n" + "=" * 70)
    print("✅ FULL SESSION TEST PASSED")
    print("=" * 70)
    print("\nWhat happened:")
    print("  1. SessionStart hook initialized lineage tracking")
    print("  2. 5 routing decisions logged during session")
    print("  3. Decisions stored in JSONL (real-time) and SQLite (analytics)")
    print("  4. SessionEnd hook generated efficiency report")
    print("  5. Report showed all models used and zero waste")
    print("\n🎯 Lineage tracking is working end-to-end!")
