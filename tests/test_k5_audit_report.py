"""K5 — `llm-router doctor --audit` makes CLASS-A structurally visible.

The recurring defect this audit kept finding is a mechanism that is built,
correct, durable, and read by nobody:

    failopen.record()                    58 writers, 0 readers
    execution_ledger.dropped_event_count docstring claimed a reader it lacked
    session_store.lock_timeout_count     tests only
    prompt_capture.counters()            assembled into status(), imported by nothing
    dashboard_data.query_realized_savings NO CALLER AT ALL

Every one was found by an auditor reading source, which is the expensive way.

One command that prints EVERY registered counter means the next one is visible
without anyone going looking — and because it renders the R12 registry, a
counter reaches this output by being DECLARED rather than by someone
remembering to add a section.

It is also deliberately unflattering. On the machine this was written, the
first run reported 3 of 20 savings surfaces canonical, 262 of 285 money rows of
unknown provenance, consent never asked, and no replayer. An audit report that
only prints good news is a marketing page.
"""

from __future__ import annotations

import ast
import contextlib
import io
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.commands.doctor import cmd_doctor

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_doctor(["--audit"])
    assert rc == 0
    return buf.getvalue()


def test_the_audit_report_names_every_registered_counter(report):
    """The registry is the contract. A counter cannot be quietly left out."""
    from llm_router import counter_registry

    missing = [c.id for c in counter_registry.REGISTRY if c.id not in report]
    assert not missing, (
        f"registered counter(s) absent from `doctor --audit`: {missing}. "
        "The report renders the registry precisely so this cannot happen — if "
        "it can, the report is a hand-maintained list again."
    )


def test_it_reports_the_sections_an_auditor_would_reconstruct_by_hand(report):
    for section in ("Instrumentation counters", "Savings, canonically",
                    "Provenance split", "Ground Truth scope"):
        assert section in report, f"the report has no {section!r} section"


def test_the_savings_figure_carries_its_qualifiers(report):
    """R7 applies here too: no bare dollar amounts, even in a diagnostic."""
    assert "avoided" in report, "the savings line has no qualifier"
    assert "n=" in report, "the savings line has no denominator"
    assert "provenance-filtered" in report


def test_unmigrated_savings_surfaces_are_named_individually(report):
    """Not a count — the names. A number invites rounding it down."""
    from llm_router.savings import SURFACES

    unmigrated = [s for s in SURFACES if not s.canonical]
    if not unmigrated:
        pytest.skip("every surface is canonical; nothing to name")
    for s in unmigrated[:5]:
        assert s.id in report, (
            f"{s.id} still computes its own savings figure and the audit "
            "report does not say so"
        )


def test_the_report_states_the_ground_truth_scope(report):
    """The scope must travel with the data, not live in a docstring."""
    assert "replayer available" in report
    assert "BY DESIGN" in report, (
        "the report states that repo tasks are excluded without saying it is "
        "deliberate, which reads as a bug rather than a scoping decision"
    )


def test_a_broken_section_degrades_rather_than_killing_the_report(monkeypatch, tmp_path):
    """One unavailable subsystem must not blank the rest.

    Turning a partial outage into total blindness is the exact failure mode
    the counters exist to prevent, and a report that dies on the first
    exception reproduces it at the reporting layer.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.commands import doctor

    import llm_router.counter_registry as cr

    def _boom():
        raise RuntimeError("subsystem down")

    monkeypatch.setattr(cr, "readings", _boom)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor.cmd_doctor(["--audit"])
    out = buf.getvalue()
    assert "UNAVAILABLE: RuntimeError" in out, "the failure was not reported"
    assert "Provenance split" in out, "one bad section killed the whole report"


def test_the_audit_flag_is_wired_by_ast_not_by_prose():
    """A-10: asserting on source text would pass with the branch deleted."""
    tree = ast.parse((SRC / "llm_router" / "commands" / "doctor.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "cmd_doctor"
    )
    comparisons = [
        ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Compare)
    ]
    assert any("'--audit' in args" in c for c in comparisons), (
        f"cmd_doctor does not branch on --audit: {comparisons}"
    )
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "_render_audit_report" in calls, (
        "the --audit branch does not call the report renderer"
    )
