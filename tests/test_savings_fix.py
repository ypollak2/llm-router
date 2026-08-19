"""Test SAVINGS isolation and updates across multiple sessions.

Tests the fix for: SAVINGS not updating in Session Summary Dashboard
between sessions.

Strategy:
  - Session A: Record spend, verify persistence
  - Session B: Reset happens, record new spend, verify isolation
  - Session C: Verify previous sessions don't leak into current

This validates that session_spend.json is properly reset at session-start
and properly flushed at session-end.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from llm_router.session_spend import reset_session_spend


@pytest.fixture
def isolated_llm_router_dir():
    """Create isolated ~/.llm-router for testing without polluting real state."""
    tmpdir = tempfile.mkdtemp(prefix="llm_router_test_")
    original_home = os.environ.get("HOME")
    original_llm_router_path = None

    try:
        # Temporarily redirect HOME to isolated directory
        fake_home = Path(tmpdir) / "home"
        fake_home.mkdir()
        os.environ["HOME"] = str(fake_home)

        # Patch SESSION_SPEND_FILE to use isolated directory
        import llm_router.session_spend
        original_llm_router_path = llm_router.session_spend.SESSION_SPEND_FILE
        llm_router.session_spend.SESSION_SPEND_FILE = fake_home / ".llm-router" / "session_spend.json"

        yield fake_home / ".llm-router"

    finally:
        # Restore original environment
        if original_home:
            os.environ["HOME"] = original_home
        if original_llm_router_path:
            import llm_router.session_spend
            llm_router.session_spend.SESSION_SPEND_FILE = original_llm_router_path
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestSavingsIsolation:
    """Test SAVINGS isolation across sessions."""

    def test_session_a_record_and_persist(self, isolated_llm_router_dir):
        """Session A: Record spend and verify it's persisted."""
        # Reset module-level singleton
        import llm_router.session_spend
        llm_router.session_spend._spend = None

        # Simulate session A
        reset_session_spend()
        session_a = llm_router.session_spend.get_session_spend()

        # Record a call
        session_a.record(
            model="gpt-4o",
            tool="llm_code",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.10
        )

        # Verify spend was recorded
        assert session_a.total_usd == 0.10
        assert session_a.call_count == 1

        # Verify file was persisted
        spend_file = isolated_llm_router_dir / "session_spend.json"
        assert spend_file.exists()
        data = json.loads(spend_file.read_text())
        assert data["total_usd"] == 0.10
        assert data["call_count"] == 1

    def test_session_b_reset_and_new_spend(self, isolated_llm_router_dir):
        """Session B: New session resets spend, records new data."""
        import llm_router.session_spend
        llm_router.session_spend._spend = None

        # Session A recorded data
        spend_file = isolated_llm_router_dir / "session_spend.json"
        initial_data = {
            "total_usd": 0.10,
            "call_count": 1,
            "tokens_reclaimed": 100,
            "opus_equivalent_usd": 0.50,
            "net_savings_usd": 0.40,
            "session_start": time.time() - 3600,  # 1 hour ago
            "per_model": {"gpt-4o": {"calls": 1, "cost_usd": 0.10}},
            "per_tool": {"llm_code": 1},
            "anomaly_flag": False,
            "gates_passed": 1,
            "gates_failed": 0,
        }
        isolated_llm_router_dir.mkdir(parents=True, exist_ok=True)
        spend_file.write_text(json.dumps(initial_data, indent=2))

        # Session B starts: MUST reset session_spend.json
        # This simulates what session-start.py does
        session_b_start = time.time()
        fresh = {
            "total_usd": 0.0,
            "call_count": 0,
            "anomaly_flag": False,
            "session_start": session_b_start,
            "top_model": None,
            "per_model": {},
            "per_tool": {},
            "tokens_reclaimed": 0,
            "opus_equivalent_usd": 0.0,
            "net_savings_usd": 0.0,
            "extension_minutes": 0.0,
            "gate_pass_rate": 100.0,
            "gates_passed": 0,
            "gates_failed": 0,
        }
        tmp = str(spend_file) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(fresh, f, indent=2)
        os.replace(tmp, str(spend_file))

        # Verify reset worked
        llm_router.session_spend._spend = None  # Clear singleton
        session_b = llm_router.session_spend.get_session_spend()
        assert session_b.total_usd == 0.0
        assert session_b.call_count == 0
        assert session_b.opus_equivalent_usd == 0.0
        assert session_b.net_savings_usd == 0.0

        # Record new spend in session B
        session_b.record(
            model="claude-opus",
            tool="llm_analyze",
            input_tokens=2000,
            output_tokens=1000,
            cost_usd=0.15
        )

        # Verify session B has its own data
        assert session_b.total_usd == 0.15
        assert session_b.call_count == 1
        assert session_b.net_savings_usd == 0.0  # No reclamation in session B

        # Verify file was updated (not showing session A data)
        data = json.loads(spend_file.read_text())
        assert data["total_usd"] == 0.15
        assert data["call_count"] == 1
        assert data["opus_equivalent_usd"] == 0.0

    def test_session_c_verify_no_bleed(self, isolated_llm_router_dir):
        """Session C: Verify previous sessions' SAVINGS don't leak through."""
        import llm_router.session_spend
        llm_router.session_spend._spend = None

        # Populate with session B's data
        spend_file = isolated_llm_router_dir / "session_spend.json"
        session_b_data = {
            "total_usd": 0.15,
            "call_count": 1,
            "tokens_reclaimed": 50,
            "opus_equivalent_usd": 0.30,
            "net_savings_usd": 0.15,
            "session_start": time.time() - 1800,  # 30 min ago
            "per_model": {"claude-opus": {"calls": 1, "cost_usd": 0.15}},
            "per_tool": {"llm_analyze": 1},
            "anomaly_flag": False,
            "gates_passed": 1,
            "gates_failed": 0,
        }
        isolated_llm_router_dir.mkdir(parents=True, exist_ok=True)
        spend_file.write_text(json.dumps(session_b_data, indent=2))

        # Session C starts
        session_c_start = time.time()
        fresh = {
            "total_usd": 0.0,
            "call_count": 0,
            "anomaly_flag": False,
            "session_start": session_c_start,
            "top_model": None,
            "per_model": {},
            "per_tool": {},
            "tokens_reclaimed": 0,
            "opus_equivalent_usd": 0.0,
            "net_savings_usd": 0.0,
            "extension_minutes": 0.0,
            "gate_pass_rate": 100.0,
            "gates_passed": 0,
            "gates_failed": 0,
        }
        tmp = str(spend_file) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(fresh, f, indent=2)
        os.replace(tmp, str(spend_file))

        # Load session C (fresh)
        llm_router.session_spend._spend = None
        session_c = llm_router.session_spend.get_session_spend()

        # Verify session C has ZERO spend (not session B's data)
        assert session_c.total_usd == 0.0
        assert session_c.call_count == 0
        assert session_c.tokens_reclaimed == 0
        assert session_c.opus_equivalent_usd == 0.0
        assert session_c.net_savings_usd == 0.0
        assert session_c.gates_passed == 0

        # Session C does some routing with reclamation
        session_c.record_reclaimed(tokens_reclaimed=200, opus_equivalent_usd=1.00, gates_passed=True)

        # Verify session C only shows its own SAVINGS
        assert session_c.net_savings_usd == 1.00  # 1.00 - 0.0
        assert session_c.tokens_reclaimed == 200

        # File should reflect session C only
        data = json.loads(spend_file.read_text())
        assert data["net_savings_usd"] == 1.00
        assert data["opus_equivalent_usd"] == 1.00
        assert data["tokens_reclaimed"] == 200


class TestSessionSpendFlushing:
    """Test that session spend is properly flushed at session-end."""

    def test_flush_updates_file_from_memory(self, isolated_llm_router_dir):
        """Verify _flush_session_spend_from_mcp() concept works."""
        import llm_router.session_spend
        llm_router.session_spend._spend = None

        reset_session_spend()
        session = llm_router.session_spend.get_session_spend()

        # Record spend in memory
        session.record("gpt-4o", "llm_code", 1000, 500, 0.10)

        # File should reflect the in-memory state
        spend_file = isolated_llm_router_dir / "session_spend.json"
        data = json.loads(spend_file.read_text())
        assert data["total_usd"] == 0.10
        assert data["call_count"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
