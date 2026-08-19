"""DASH-1a — the session summary must not double-count Codex, nor invent tokens.

Two discrepancies from ``Docs/correctness-reset/06_DASHBOARD_DISCREPANCIES.md``:

* **D2 (Codex double-count).** A Codex call is logged to *both* the ``usage``
  table (``cost.log_usage`` forces ``cost_usd = 0`` for free providers,
  cost.py:706) *and* the dedicated ``codex_usage`` table. The session summary
  renders Codex from ``codex_usage`` via ``_format_codex_section`` **and** again
  in the free-provider section (because ``codex`` was in ``_FREE_PROVIDERS``).
  The free section must exclude Codex — the dedicated panel owns it.

* **D3 (fabricated tokens).** Codex reports no token counts, so the free
  section *estimated* them from unrelated paid-call averages and derived a
  dollar "saved" figure from the invention. A savings number must never rest on
  fabricated tokens — show ``—`` and claim ``$0`` when the volume is unknown.

Both are exercised behaviourally against the real hook functions.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Import the hyphenated session-end hook as a module (same pattern as
# tests/test_session_report.py).
_HOOK_DIR = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
sys.path.insert(0, str(_HOOK_DIR))
_spec = importlib.util.spec_from_file_location("session_end", _HOOK_DIR / "session-end.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


def _strip(text: str) -> str:
    """Drop ANSI colour codes so substring assertions are robust."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task_type TEXT,
            model TEXT,
            provider TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            success INTEGER DEFAULT 1
        )
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(se, "DB_PATH", str(db))
    return db


def _insert(db, rows):
    conn = sqlite3.connect(str(db))
    now = se._session_start_iso(time.time())
    for r in rows:
        conn.execute(
            "INSERT INTO usage (timestamp, task_type, model, provider, "
            "input_tokens, output_tokens, cost_usd, success) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (now, r.get("task_type", "code"), r["model"], r["provider"],
             r.get("input_tokens", 0), r.get("output_tokens", 0),
             r.get("cost_usd", 0.0)),
        )
    conn.commit()
    conn.close()


# ── D2: Codex is owned by the dedicated panel, never the free section ─────────

def test_codex_excluded_from_free_split(usage_db):
    """Fail-before: ``codex`` ∈ ``_FREE_PROVIDERS`` → a Codex ``usage`` row lands
    in the free split and is rendered twice (here + ``_format_codex_section``).
    Pass-after: the free split excludes Codex entirely."""
    _insert(usage_db, [
        {"provider": "codex",  "model": "gpt-5-codex",  "input_tokens": 800, "output_tokens": 400},
        {"provider": "ollama", "model": "qwen3-coder",  "input_tokens": 600, "output_tokens": 300},
    ])
    start = time.time() - 3600
    _paid, _cc, free = se._query_session_data(start)

    providers = {r.get("provider") for r in free}
    assert "codex" not in providers, "Codex must not appear in the free split (owned by _format_codex_section)"
    assert "ollama" in providers, "non-dedicated free providers must still appear"


def test_codex_absent_from_rendered_free_section(usage_db):
    """The rendered free section must carry no Codex line."""
    _insert(usage_db, [
        {"provider": "codex",  "model": "gpt-5-codex", "input_tokens": 800, "output_tokens": 400},
        {"provider": "ollama", "model": "qwen3-coder", "input_tokens": 600, "output_tokens": 300},
    ])
    start = time.time() - 3600
    paid, _cc, free = se._query_session_data(start)
    text = _strip("\n".join(se._format_free_section(free, paid)))
    assert "codex" not in text, "free section must not render a Codex line"
    assert "ollama" in text


# ── D3: never derive a savings figure from fabricated tokens ──────────────────

def test_free_section_does_not_fabricate_tokens():
    """A free provider with no reported tokens must show ``—`` and claim $0 —
    not tokens estimated from unrelated paid-call averages.

    Fail-before: the estimator sets tokens = avg_paid_tokens × calls, tags the
    row ``~est`` and reports a non-zero ``$`` saved. Pass-after: ``—`` / $0."""
    free_rows = [{"provider": "ollama", "input_tokens": 0, "output_tokens": 0}]
    paid_rows = [{"input_tokens": 4000, "output_tokens": 2000}]  # would drive the estimate
    text = _strip("\n".join(se._format_free_section(free_rows, paid_rows)))

    assert "~est" not in text, "must not tag rows as estimated — no fabrication"
    assert "—" in text, "unknown token volume must render as an em-dash"
    # The header total and the provider row must both claim exactly $0.
    assert "$0.0000 gross saved" in text, "no savings may be claimed from unknown tokens"
    assert "0.0001" not in text and "0.001" not in text, "no fabricated dollar figure"


def test_free_section_still_credits_real_tokens():
    """Guard the other direction: a free provider WITH real tokens is still
    credited (the fix removes fabrication, not honest savings)."""
    free_rows = [{"provider": "ollama", "input_tokens": 10_000, "output_tokens": 5_000}]
    text = _strip("\n".join(se._format_free_section(free_rows, [])))
    assert "~est" not in text
    assert "—" not in text, "real tokens must render numerically, not as —"
    assert "$0.0000 gross saved" not in text, "real token volume must credit a positive baseline"
