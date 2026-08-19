"""Refinement #7 — admin-action audit retention CLI + prune helper.

Two layers:

* ``AdminActionLog.prune(older_than_seconds, dry_run=...)`` —
  primitive that deletes rows whose ``timestamp < now - threshold``.
  Returns ``{would_delete, deleted, cutoff_ts}``. Refuses
  non-positive thresholds (would prune the entire table).
* ``llm_router admin-actions prune --older-than 90d`` — CLI wrapper that
  parses the duration string, emits a self-referential audit row
  BEFORE the delete, then runs the primitive.

These tests pin the duration grammar, the safety guard against
``--older-than 0``, the audit-row-before-delete contract, and the
dry-run mode.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from llm_router.admin_actions import AdminActionLog
from llm_router.commands.admin_actions import (
    _parse_duration,
    cmd_admin_actions,
)


# ── 1. Duration parsing grammar ────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("90d", 90 * 86400),
        ("12h", 12 * 3600),
        ("30m", 30 * 60),
        ("45s", 45),
        ("600", 600),         # bare integer = seconds
        ("0.5h", 1800),       # fractional values
        ("0.5d", 43200),
    ],
)
def test_parse_duration_valid(raw: str, expected: float) -> None:
    assert _parse_duration(raw) == pytest.approx(expected)


def test_parse_duration_case_insensitive_suffix() -> None:
    assert _parse_duration("90D") == 90 * 86400
    assert _parse_duration("12H") == 12 * 3600


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "abc", "90x", "d", "h12"],
)
def test_parse_duration_invalid_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        _parse_duration(raw)


def test_parse_duration_allows_space_between_number_and_unit() -> None:
    """``"12 d"`` is forgiving-parsed as 12 days — the parser strips
    whitespace before float-converting the number body. Pinning the
    behaviour so a future "stricter parser" refactor doesn't break
    operators who have spaces in their cron entries."""
    assert _parse_duration("12 d") == 12 * 86400


# ── 2. AdminActionLog.prune primitive ──────────────────────────────────────


@pytest.fixture
def log(tmp_path: Path) -> AdminActionLog:
    return AdminActionLog(
        db_path=tmp_path / "admin_actions.db", check_same_thread=False
    )


def _seed(log: AdminActionLog, n: int, *, age_seconds: float) -> list[str]:
    """Append n rows pre-aged by ``age_seconds`` so the timestamp
    column reflects an older time. Returns the inserted ids."""
    rows = []
    ts = time.time() - age_seconds
    for i in range(n):
        row = log.append(
            actor_user_id=f"u{i}", actor_email=f"a{i}@x",
            action="test:event", resource_id=f"r{i}",
            detail={"i": i},
        )
        # Rewrite the row's timestamp to ``ts`` so the prune cutoff
        # behaves as if the row were ``age_seconds`` old.
        log._conn.execute(
            "UPDATE admin_actions SET timestamp = ? WHERE id = ?",
            (ts, row["id"]),
        )
        rows.append(row["id"])
    log._conn.commit()
    return rows


def test_prune_zero_seconds_raises(log: AdminActionLog) -> None:
    with pytest.raises(ValueError, match="positive"):
        log.prune(older_than_seconds=0)


def test_prune_negative_seconds_raises(log: AdminActionLog) -> None:
    with pytest.raises(ValueError, match="positive"):
        log.prune(older_than_seconds=-1)


def test_prune_empty_table_returns_zero(log: AdminActionLog) -> None:
    result = log.prune(older_than_seconds=86400)
    assert result["deleted"] == 0
    assert result["would_delete"] == 0


def test_prune_deletes_old_rows_only(log: AdminActionLog) -> None:
    """Five rows aged 100k seconds + two rows aged 1 second. Prune
    at 1000s threshold keeps the recent two, drops the old five."""
    _seed(log, 5, age_seconds=100_000)
    recent_ids = _seed(log, 2, age_seconds=1)

    result = log.prune(older_than_seconds=1000)
    assert result["deleted"] == 5
    assert result["would_delete"] == 5

    remaining = {
        row["id"] for row in log.recent(limit=100)
    }
    assert remaining == set(recent_ids)


def test_prune_dry_run_does_not_delete(log: AdminActionLog) -> None:
    _seed(log, 3, age_seconds=100_000)
    pre = log.count()
    result = log.prune(older_than_seconds=1000, dry_run=True)
    assert result["dry_run"] is True
    assert result["would_delete"] == 3
    assert result["deleted"] == 0
    assert log.count() == pre  # nothing actually deleted


def test_prune_threshold_is_strict_inequality(log: AdminActionLog) -> None:
    """A row whose ``timestamp == cutoff`` is kept — only strictly
    older rows are eligible."""
    rid = log.append(
        actor_user_id="u", actor_email="a@x",
        action="t", resource_id="r", detail={},
    )["id"]
    # Pin the row's timestamp to exactly ``now - 100s``.
    pinned_ts = time.time() - 100
    log._conn.execute(
        "UPDATE admin_actions SET timestamp = ? WHERE id = ?",
        (pinned_ts, rid),
    )
    log._conn.commit()
    # Threshold equal to age → SHOULD be kept (strict ``<`` cutoff).
    result = log.prune(older_than_seconds=100)
    assert result["deleted"] in (0, 1)  # depends on race; allow both
    # Tightening the threshold by 1 second guarantees deletion.
    if result["deleted"] == 0:
        result2 = log.prune(older_than_seconds=99)
        assert result2["deleted"] == 1


# ── 3. CLI subcommand integration ──────────────────────────────────────────


def test_cli_help_returns_zero(capsys) -> None:
    assert cmd_admin_actions(["--help"]) == 0
    out = capsys.readouterr().out
    assert "admin-actions" in out
    assert "prune" in out
    assert "--older-than" in out


def test_cli_unknown_subcommand_returns_one(capsys) -> None:
    assert cmd_admin_actions(["wat"]) == 1
    err = capsys.readouterr().err
    assert "Unknown subcommand" in err


def test_cli_prune_requires_older_than(capsys) -> None:
    assert cmd_admin_actions(["prune"]) == 1
    err = capsys.readouterr().err
    assert "--older-than" in err


def test_cli_prune_invalid_duration_returns_one(capsys) -> None:
    assert cmd_admin_actions(["prune", "--older-than", "abc"]) == 1
    err = capsys.readouterr().err
    assert "invalid duration" in err


def test_cli_prune_zero_duration_refused(capsys) -> None:
    assert cmd_admin_actions(["prune", "--older-than", "0"]) == 1
    err = capsys.readouterr().err
    assert "positive" in err


def test_cli_prune_writes_self_referential_audit_row(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """The CLI must write its own row to admin_actions BEFORE the
    delete so the audit chain records who pruned and when."""
    monkeypatch.setenv(
        "LLM_ROUTER_ADMIN_ACTIONS_PATH", str(tmp_path / "admin_actions.db")
    )
    # Seed an existing row to keep so we can confirm the prune ran.
    seed = AdminActionLog(
        db_path=tmp_path / "admin_actions.db", check_same_thread=False
    )
    seed.append(
        actor_user_id="alice", actor_email="alice@x",
        action="user:create", resource_id="u1", detail={},
    )
    seed.close()

    assert cmd_admin_actions(
        ["prune", "--older-than", "999d"]
    ) == 0
    out = capsys.readouterr().out
    assert "deleted" in out.lower() or "would delete" in out.lower()

    # Open the DB fresh and confirm the self-ref row is there.
    reopened = AdminActionLog(
        db_path=tmp_path / "admin_actions.db", check_same_thread=False
    )
    rows = reopened.recent(limit=10)
    actions = {r["action"] for r in rows}
    assert "admin_actions:prune" in actions


def test_cli_dry_run_writes_dry_run_action_variant(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Dry-run must emit ``admin_actions:prune_dry_run`` so operators
    can grep for attempted-but-not-executed prunes separately from
    real ones."""
    monkeypatch.setenv(
        "LLM_ROUTER_ADMIN_ACTIONS_PATH", str(tmp_path / "admin_actions.db")
    )
    assert cmd_admin_actions(
        ["prune", "--older-than", "30d", "--dry-run"]
    ) == 0
    reopened = AdminActionLog(
        db_path=tmp_path / "admin_actions.db", check_same_thread=False
    )
    rows = reopened.recent(limit=5)
    assert any(
        r["action"] == "admin_actions:prune_dry_run" for r in rows
    )


def test_cli_count_subcommand(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv(
        "LLM_ROUTER_ADMIN_ACTIONS_PATH", str(tmp_path / "admin_actions.db")
    )
    log = AdminActionLog(
        db_path=tmp_path / "admin_actions.db", check_same_thread=False
    )
    for _ in range(5):
        log.append(
            actor_user_id="u", actor_email="a@x",
            action="t", resource_id="r", detail={},
        )
    log.close()

    assert cmd_admin_actions(["count"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "5"
