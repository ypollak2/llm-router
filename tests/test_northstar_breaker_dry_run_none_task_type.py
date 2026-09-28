"""Regression: `llm-router northstar --days 30 --breaker-dry-run` crashed on a
live install.

`commands/northstar.py::_print_breaker_dry_run` formatted
`row['task_type']:<12s`, and `quality_breaker.dry_run()` can legitimately
produce a row whose `task_type` is `None` (a unit with no task_type recorded
falls into the `(lever, None)` class — see `dry_run`'s `seen.add((lever,
u.get("task_type")))`). Formatting `None` with a string width spec raises
``TypeError: unsupported format string passed to NoneType.__format__``, so
the command crashed instead of printing a row.

The fix renders a `None` task_type as "-" before applying the format spec.
"""
from __future__ import annotations

from llm_router.commands.northstar import _print_breaker_dry_run


def _dry_run_row_with_none_task_type(*, days=None):
    return [{
        "lever": "codex_subagent",
        "task_type": None,
        "would_be": "closed",
        "failure_rate": 0.2,
        "n": 5,
        "unknown": 0,
        "reason": "below min_n",
    }]


def test_breaker_dry_run_does_not_crash_on_none_task_type(monkeypatch, capsys):
    from llm_router import quality_breaker as qb

    monkeypatch.setattr(qb, "dry_run", _dry_run_row_with_none_task_type)

    # Must not raise — this is the exact TypeError seen on the live install:
    # "unsupported format string passed to NoneType.__format__".
    _print_breaker_dry_run(30)

    out = capsys.readouterr().out
    assert "codex_subagent/-" in out, (
        f"a None task_type must render as '-', not crash or print 'None': {out!r}"
    )
    assert "None" not in out, f"a None field leaked into the printed row: {out!r}"
