"""PR5 — session-end.py's "quota preserved" line must count only rows the
writer stamped mode="block" (realized), matching hooks/savings_logger.py's
mode="block" (realized) / mode="echo" (discarded draft) split. A row missing
"mode" entirely (every row the `usage` table's current SQL SELECT returns,
since the column doesn't exist there yet) is unmeasured, not realized
(S9 — unknown must not become the favourable answer).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
sys.path.insert(0, str(_HOOK_DIR))
_spec = importlib.util.spec_from_file_location("session_end_pr5", _HOOK_DIR / "session-end.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


def _strip(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _row(**kw):
    base = {"task_type": "query", "model": "ollama/x", "provider": "ollama",
             "input_tokens": 100, "output_tokens": 100, "cost_usd": 0.0}
    base.update(kw)
    return base


class TestAggregateModeSplit:
    def test_block_rows_are_realized(self):
        rows = [_row(mode="block")]
        tools = se._aggregate(rows)
        d = tools["query"]
        assert d["realized_count"] == 1
        assert d["realized_in"] == 100
        assert d["realized_out"] == 100
        assert d["unmeasured_count"] == 0

    def test_echo_rows_are_excluded_from_realized_and_unmeasured(self):
        rows = [_row(mode="echo")]
        tools = se._aggregate(rows)
        d = tools["query"]
        assert d["realized_count"] == 0
        assert d["realized_in"] == 0
        assert d["unmeasured_count"] == 0, (
            "an echo row IS measured — measured as not realized, not unknown"
        )

    def test_missing_mode_is_unmeasured_not_realized(self):
        rows = [_row()]  # no "mode" key at all — today's real usage-table shape
        tools = se._aggregate(rows)
        d = tools["query"]
        assert d["realized_count"] == 0
        assert d["unmeasured_count"] == 1

    def test_mixed_rows_only_block_counts_as_realized(self):
        rows = [
            _row(mode="block", input_tokens=100, output_tokens=50),
            _row(mode="echo", input_tokens=200, output_tokens=200),
            _row(input_tokens=300, output_tokens=300),  # missing mode
        ]
        tools = se._aggregate(rows)
        d = tools["query"]
        assert d["count"] == 3, "the raw activity count still includes every row"
        assert d["realized_count"] == 1
        assert d["realized_in"] + d["realized_out"] == 150
        assert d["unmeasured_count"] == 1


class TestQuotaPreservedLine:
    def test_quota_preserved_excludes_echo_tokens(self):
        """Fail-before: the old code reused the ALL-rows token count for
        'quota preserved', so an echoed draft's tokens (Claude answered the
        turn anyway) were credited as quota kept off the subscription."""
        rows = [
            _row(mode="block", input_tokens=1000, output_tokens=500),
            _row(mode="echo", input_tokens=9000, output_tokens=9000),
        ]
        tools = se._aggregate(rows)
        text = _strip("\n".join(se._format_routing_section(tools, subscription=True)))
        assert "1.5k quota preserved" in text, text
        assert "18.5k quota preserved" not in text
        # the raw activity line still shows every token, for context
        assert "19.5k tokens" in text

    def test_quota_preserved_excludes_missing_mode_rows(self):
        rows = [_row(input_tokens=5000, output_tokens=5000)]  # no mode
        tools = se._aggregate(rows)
        text = _strip("\n".join(se._format_routing_section(tools, subscription=True)))
        assert "0 quota preserved" in text, text
        assert "1 unmeasured" in text

    def test_quota_preserved_all_realized_matches_totals(self):
        rows = [_row(mode="block", input_tokens=1000, output_tokens=500)]
        tools = se._aggregate(rows)
        text = _strip("\n".join(se._format_routing_section(tools, subscription=True)))
        assert "1.5k tokens" in text
        assert "1.5k quota preserved" in text
        assert "unmeasured" not in text


class TestNonSubscriptionUnaffected:
    def test_dollar_branch_still_uses_all_rows(self):
        """Out of PR5's scope: the non-subscription $ branch (AUD-06's signed
        subtraction) is unchanged by the mode split."""
        rows = [_row(mode="echo", cost_usd=0.01, input_tokens=1000, output_tokens=500)]
        tools = se._aggregate(rows)
        text = _strip("\n".join(se._format_routing_section(tools, subscription=False)))
        assert "1 calls" in text
        assert "1.5k tokens" in text


class TestSyncImportPersistsMode:
    """External review, point (b): the SYNC savings_stats importer
    (session-end.py's own _sync_import_savings_log, distinct from
    cost.import_savings_log) dropped `mode` on write, so every row it
    imported landed mode=NULL in savings_stats — "realized-ness unknown" —
    which is why the live DB's confirmed-production rows were all NULL. This
    proves the writer now persists it, end to end: JSONL -> savings_stats.
    """

    def test_mode_is_persisted_block_and_echo_and_missing(self, monkeypatch, tmp_path):
        import json
        import sqlite3

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))

        db_path = tmp_path / "usage.db"
        # _sync_import_savings_log no-ops unless BOTH the log and the db
        # already exist (session-end.py:347) — seed an empty db file.
        sqlite3.connect(db_path).close()

        log_path = tmp_path / "savings_log.jsonl"
        entries = [
            {"timestamp": "2026-09-14T00:00:00+00:00", "session_id": "s1",
             "task_type": "query", "estimated_saved": 0.05, "external_cost": 0.0,
             "model": "ollama/x", "host": "claude_code", "mode": "block"},
            {"timestamp": "2026-09-14T00:00:01+00:00", "session_id": "s1",
             "task_type": "query", "estimated_saved": 0.0, "external_cost": 0.0,
             "model": "ollama/x", "host": "claude_code", "mode": "echo"},
            {"timestamp": "2026-09-01T00:00:00+00:00", "session_id": "s1",
             "task_type": "query", "estimated_saved": 0.02, "external_cost": 0.0,
             "model": "ollama/x", "host": "claude_code"},  # pre-mode record
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        se._sync_import_savings_log()

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT timestamp, mode FROM savings_stats ORDER BY timestamp"
            ).fetchall()
        finally:
            conn.close()
        modes = {ts: m for ts, m in rows}
        assert modes["2026-09-14T00:00:00+00:00"] == "block"
        assert modes["2026-09-14T00:00:01+00:00"] == "echo"
        assert modes["2026-09-01T00:00:00+00:00"] is None, (
            "a record written before `mode` existed must import as NULL, "
            "not as 'echo' or any other guessed value"
        )
        assert not log_path.exists(), "the claimed log must be fully drained"
