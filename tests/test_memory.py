"""Tests for the v6.1 memory system — learned routing profiles."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from llm_router.memory.profiles import (
    LearnedRoute,
    build_learned_profile,
    fetch_corrections_history,
    load_learned_profile,
    save_learned_profile,
)


class TestFetchCorrectionsHistory:
    """Test correction history retrieval."""

    def test_fetch_empty_database(self, temp_router_dir):
        """Test fetching from empty database."""
        history = fetch_corrections_history()
        assert history == []

    def test_fetch_no_db(self, monkeypatch):
        """Test behavior when database doesn't exist."""
        monkeypatch.setattr(
            "llm_router.memory.profiles.DB_PATH",
            Path("/nonexistent/usage.db")
        )
        history = fetch_corrections_history()
        assert history == []

    def test_fetch_with_corrections(self, temp_router_dir):
        """Test retrieving stored corrections."""
        # Create corrections table and insert test data
        db_path = temp_router_dir / "usage.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE corrections (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                original_tool TEXT,
                original_model TEXT,
                corrected_model TEXT,
                reason TEXT,
                session_id TEXT
            )
        """)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO corrections (timestamp, original_tool, original_model, corrected_model, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "code", "gpt-4o-mini", "claude-opus-4-6", "need better reasoning")
        )
        conn.commit()
        conn.close()

        # Monkeypatch to use temp db
        import llm_router.memory.profiles as mp
        original_db = mp.DB_PATH
        mp.DB_PATH = db_path

        try:
            history = fetch_corrections_history(days=1)
            assert len(history) == 1
            assert history[0]["original_tool"] == "code"
            assert history[0]["corrected_model"] == "claude-opus-4-6"
        finally:
            mp.DB_PATH = original_db


class TestBuildLearnedProfile:
    """Test learned profile builder."""

    def test_build_empty_corrections(self, temp_router_dir):
        """Test building profile from empty corrections."""
        profile = build_learned_profile()
        assert profile == {}

    def test_build_below_threshold(self, temp_router_dir):
        """Test that routes below confidence threshold are excluded."""
        # Create corrections with < 3 entries
        db_path = temp_router_dir / "usage.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE corrections (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                original_tool TEXT,
                original_model TEXT,
                corrected_model TEXT,
                reason TEXT,
                session_id TEXT
            )
        """)
        now = datetime.now(timezone.utc).isoformat()
        for i in range(2):  # 2 corrections, below threshold of 3
            conn.execute(
                "INSERT INTO corrections (timestamp, original_tool, original_model, corrected_model, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, "code", "gpt-4o-mini", "claude-opus-4-6", f"reason {i}")
            )
        conn.commit()
        conn.close()

        import llm_router.memory.profiles as mp
        original_db = mp.DB_PATH
        mp.DB_PATH = db_path

        try:
            profile = build_learned_profile()
            # Should be empty because 2 < 3
            assert profile == {}
        finally:
            mp.DB_PATH = original_db

    def test_build_at_threshold(self, temp_router_dir):
        """Test profile building at confidence threshold."""
        db_path = temp_router_dir / "usage.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE corrections (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                original_tool TEXT,
                original_model TEXT,
                corrected_model TEXT,
                reason TEXT,
                session_id TEXT
            )
        """)
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):  # Exactly 3 = threshold
            conn.execute(
                "INSERT INTO corrections (timestamp, original_tool, original_model, corrected_model, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, "security_review", "gpt-4o-mini", "claude-opus-4-6", f"reason {i}")
            )
        conn.commit()
        conn.close()

        import llm_router.memory.profiles as mp
        original_db = mp.DB_PATH
        mp.DB_PATH = db_path

        try:
            profile = build_learned_profile()
            assert "security_review" in profile
            assert profile["security_review"].confidence == 3
            assert profile["security_review"].model == "claude-opus-4-6"
            assert profile["security_review"].source == "corrections"
        finally:
            mp.DB_PATH = original_db

    def test_build_multiple_routes(self, temp_router_dir):
        """Test building profile with multiple task types."""
        db_path = temp_router_dir / "usage.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE corrections (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                original_tool TEXT,
                original_model TEXT,
                corrected_model TEXT,
                reason TEXT,
                session_id TEXT
            )
        """)
        now = datetime.now(timezone.utc).isoformat()

        # Add 3 corrections for "code"
        for i in range(3):
            conn.execute(
                "INSERT INTO corrections (timestamp, original_tool, original_model, corrected_model, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, "code", "gpt-4o-mini", "claude-opus-4-6", f"reason {i}")
            )

        # Add 3 corrections for "analyze"
        for i in range(3):
            conn.execute(
                "INSERT INTO corrections (timestamp, original_tool, original_model, corrected_model, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, "analyze", "gpt-4o", "claude-sonnet-4-6", f"reason {i}")
            )

        conn.commit()
        conn.close()

        import llm_router.memory.profiles as mp
        original_db = mp.DB_PATH
        mp.DB_PATH = db_path

        try:
            profile = build_learned_profile()
            assert len(profile) == 2
            assert "code" in profile
            assert "analyze" in profile
            assert profile["code"].model == "claude-opus-4-6"
            assert profile["analyze"].model == "claude-sonnet-4-6"
        finally:
            mp.DB_PATH = original_db


class TestSaveAndLoadLearnedProfile:
    """Test persistence of learned profiles."""

    def test_save_empty_profile(self, temp_router_dir):
        """Test saving empty profile."""
        import llm_router.memory.profiles as mp
        original_file = mp.LEARNED_ROUTES_FILE
        mp.LEARNED_ROUTES_FILE = temp_router_dir / "learned_routes.json"

        try:
            path = save_learned_profile({})
            assert path.exists()
            data = json.loads(path.read_text())
            assert data == {}
        finally:
            mp.LEARNED_ROUTES_FILE = original_file

    def test_save_and_load_profile(self, temp_router_dir):
        """Test round-trip persistence."""
        import llm_router.memory.profiles as mp
        original_file = mp.LEARNED_ROUTES_FILE
        mp.LEARNED_ROUTES_FILE = temp_router_dir / "learned_routes.json"

        try:
            profile = {
                "security_review": LearnedRoute(
                    model="claude-opus-4-6",
                    confidence=3,
                    source="corrections",
                    last_correction=datetime.now(timezone.utc).isoformat(),
                ),
                "architecture": LearnedRoute(
                    model="claude-sonnet-4-6",
                    confidence=2,
                    source="corrections",
                    last_correction=datetime.now(timezone.utc).isoformat(),
                ),
            }

            # Save
            save_learned_profile(profile)

            # Load
            loaded = load_learned_profile()
            assert len(loaded) == 2
            assert "security_review" in loaded
            assert loaded["security_review"].model == "claude-opus-4-6"
            assert loaded["security_review"].confidence == 3
        finally:
            mp.LEARNED_ROUTES_FILE = original_file

    def test_load_nonexistent_file(self, temp_router_dir):
        """Test loading when file doesn't exist."""
        import llm_router.memory.profiles as mp
        original_file = mp.LEARNED_ROUTES_FILE
        mp.LEARNED_ROUTES_FILE = temp_router_dir / "nonexistent.json"

        try:
            loaded = load_learned_profile()
            assert loaded == {}
        finally:
            mp.LEARNED_ROUTES_FILE = original_file

    def test_load_invalid_json(self, temp_router_dir):
        """Test loading corrupted JSON file."""
        import llm_router.memory.profiles as mp
        original_file = mp.LEARNED_ROUTES_FILE
        learned_file = temp_router_dir / "learned_routes.json"
        mp.LEARNED_ROUTES_FILE = learned_file

        try:
            learned_file.write_text("{ INVALID JSON")
            loaded = load_learned_profile()
            assert loaded == {}
        finally:
            mp.LEARNED_ROUTES_FILE = original_file
