"""A draft that did not replace Claude's turn must not be credited as a saving.

`log_direct_savings` used to run unconditionally, ~39 lines before the
`_turn_blocked` check that decides whether the routed answer actually replaced
Claude's turn. In echo mode — the default — nothing is replaced, so every
credited dollar was for a turn Claude then answered at full price. Measured on
2026-09-12: $0.426410 booked against drafts the debug log recorded as
DRAFT UNUSED.

The fix is not "drop the row". A missing row is indistinguishable from the hook
never having run, and those need different fixes. A non-realized route is
recorded, at zero, tagged `echo`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest


@dataclass
class _Model:
    provider: str
    model: str


@dataclass
class _Result:
    """Duck-typed DirectResult: the fields log_direct_savings reads."""
    model: _Model
    input_tokens: int
    output_tokens: int
    latency_ms: int = 10


@pytest.fixture
def savings_log(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path / ".llm-router" / "savings_log.jsonl"


def _rows(path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _route(realized: bool):
    from llm_router.hooks.savings_logger import log_direct_savings
    log_direct_savings(
        result=_Result(_Model("ollama", "qwen3-coder:30b"), 800, 400),
        task_type="code",
        complexity="moderate",
        session_id="sess-realized",
        realized=realized,
    )


def test_echo_route_is_recorded_at_zero(savings_log):
    """Not substituted → a row exists, credited nothing, tagged echo."""
    _route(realized=False)
    rows = _rows(savings_log)
    assert len(rows) == 1, "the route must still be recorded, not dropped"
    assert rows[0]["estimated_saved"] == 0.0, rows[0]
    assert rows[0]["mode"] == "echo", rows[0]


def test_blocking_route_is_credited(savings_log):
    """Substituted → real credit, tagged block. The fix must not zero everything."""
    _route(realized=True)
    rows = _rows(savings_log)
    assert len(rows) == 1
    assert rows[0]["estimated_saved"] > 0.0, rows[0]
    assert rows[0]["mode"] == "block", rows[0]


def test_echo_route_does_not_touch_the_headline_ledger(savings_log, monkeypatch):
    """`record_reclaimed` drives the headline savings number; echo must not call it."""
    calls = []

    class _Spend:
        def record(self, **kw):
            calls.append(("record", kw))

        def record_reclaimed(self, **kw):
            calls.append(("record_reclaimed", kw))

    import llm_router.session_spend as ss
    monkeypatch.setattr(ss, "get_session_spend", lambda: _Spend())

    _route(realized=False)
    kinds = [c[0] for c in calls]
    assert "record" in kinds, "the local call really happened; spend is still true"
    assert "record_reclaimed" not in kinds, (
        "echo mode credited the headline savings ledger: " + repr(calls)
    )


def test_call_site_passes_the_verdict():
    """The hook must compute _turn_blocked BEFORE crediting, and pass it through.

    Ordering is the whole defect, so it is asserted on the source rather than
    only on behaviour — a future edit that moves the call back above the check
    would otherwise pass every test above.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/auto-route.py"
    text = src.read_text()
    assert "realized=_turn_blocked" in text, "the verdict is not passed to the logger"
    assert text.index("_turn_blocked = _render_mode") < text.index("realized=_turn_blocked"), (
        "log_direct_savings is credited before _turn_blocked is known"
    )
