"""The 14-day MODELS panel must say WHAT IT COVERS, not just how wide its window is.

Audit doc 27. `routing_decisions` is written only by `llm_route` and `llm_auto`. The whole
`llm(task=…)` family calls `route_and_call()` without `classification_data`, and
`router.py` guards the analytics write with `if classification_data:` — so the dominant
traffic never lands there and nothing records the omission.

Measured 2026-08-16: 643 rows into `usage` in 24 hours, **zero** into `routing_decisions`,
whose newest row was three days old. The panel rendered "MODELS 14-day mix" over that
frozen data as though it described all routing. The figures an operator would have read —
38.6% hermes3, 35.6% gpt-4o, 13.9% gemini-flash — describe one tool's traffic.

THE FIX IS TO NAME THE DIFFERENCE, NOT TO WIDEN THE QUERY. Adding the missing traffic would
move every historical percentage silently, which is the same failure as the 69.4%
gpt-4o-mini figure that turned out to be test pollution inflating a denominator.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SUMMARY = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "ui" / "session_summary.py"


def _mod():
    spec = importlib.util.spec_from_file_location("session_summary", _SUMMARY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _render(**kw) -> str:
    """Render the summary to plain text so assertions read like what a user sees."""
    import io

    from rich.console import Console

    m = _mod()
    buf = io.StringIO()
    cls = next(
        v for k, v in vars(m).items()
        if isinstance(v, type) and hasattr(v, "print_dashboard") and k != "type"
    )
    inst = cls(console=Console(file=buf, width=100, force_terminal=False, no_color=True))
    inst.print_dashboard(**kw)
    return buf.getvalue()


class TestThePanelDeclaresItsCoverage:
    def test_the_heading_says_classified_routes_only(self):
        out = _render(
            timestamp="t",
            model_breakdown={"ollama/hermes3:8b": 62.0, "openai/gpt-4o": 38.0},
            model_breakdown_note="classified routes only",
        )
        assert "14-day mix" in out
        assert "classified routes only" in out, (
            "without this the panel presents one tool's routing as all routing — the "
            "exact reading that made 38.6% hermes3 look like a fleet-wide figure"
        )

    def test_an_empty_window_says_so_rather_than_rendering_nothing(self):
        """Rows exist but none inside the window.

        Rendering nothing reads as "no routing happened", which is false and worse than
        saying the panel has no fresh data — the observed case was three days with zero
        classified routes while `usage` took 643 rows.
        """
        out = _render(
            timestamp="t",
            model_breakdown=None,
            model_breakdown_note="no classified routes since 2026-08-13",
        )
        assert "no classified routes since 2026-08-13" in out, (
            "silence reads as 'no routing happened'. Absence of DATA and absence of "
            "ACTIVITY are different facts, and a panel that cannot tell them apart is "
            "the RED2-02 shape this codebase keeps finding"
        )

    def test_a_missing_note_still_renders(self):
        """Backwards compatible: callers that do not supply a note are unaffected."""
        out = _render(timestamp="t", model_breakdown={"openai/gpt-4o": 100.0})
        assert "14-day mix" in out


class TestTheSessionPanelIsPreferredWhenAvailable:
    def test_session_models_wins_over_the_14_day_mix(self):
        """`session_models` comes from `usage`, which IS complete and live.

        The 14-day mix is the fallback. When this session has real per-model data, that is
        what an operator should see — sourced from the table that records every routed
        call rather than the one covering a single tool.
        """
        out = _render(
            timestamp="t",
            session_models=[
                {"model": "ollama/hermes3:8b", "calls": 12, "tokens": 4000,
                 "cost": 0.0, "saved": 0.0},
            ],
            model_breakdown={"openai/gpt-4o": 100.0},
            model_breakdown_note="classified routes only",
        )
        assert "hermes3" in out, "the live per-session data must be what is shown"
        assert "14-day mix" not in out, (
            "the fallback panel must not also render — two model panels at once invites "
            "exactly the comparison that started this investigation"
        )
