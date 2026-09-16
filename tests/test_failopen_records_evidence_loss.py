"""When the hook loses evidence, it must say so.

N13. `hooks/auto-route.py` had 45 broad handlers whose entire body was `pass` or
`return None`. The ratchet in test_failopen_ratchet.py stops that number growing;
this lowers it, for the subset where silence actually costs something.

The distinction is deliberate and most of the file is NOT in scope. A trace emit
that fails must not take the turn down, and recording a trace failure through the
recorder could recurse. An optional-feature import that is absent is not an
incident. What matters is the handler that swallows the loss of EVIDENCE — a
savings row, a lineage entry, a coverage mark, a session event — or that silently
changes ROUTING.

Those are the cases where "it just didn't happen" is indistinguishable from "it
happened and nobody counted it", which is the failure this session hit four times
in measurement alone.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/auto-route.py"
SOURCE = HOOK.read_text()

# Every site where a swallowed failure loses evidence or changes routing.
EVIDENCE_TAGS = [
    "COVERAGE-UNOBSERVED", "COVERAGE-OBSERVED", "LINEAGE-RECENT",
    "SESSION-POINTER", "SESSION-RECORD", "ASSISTANT-TURNS",
    "SAVINGS-LOG", "SAVINGS-DB", "DRAFT-MEMORABLE",
    "SESSION-ROUTED-QA", "DIRECT-SAMPLE", "EXECUTION-SIGNAL",
    "EXECUTION-LEDGER",
]


@pytest.mark.parametrize("tag", EVIDENCE_TAGS)
def test_each_evidence_site_records_its_failure(tag):
    assert f'"CHZ-FO-HOOK-{tag}"' in SOURCE, (
        f"{tag} no longer records. A lost {tag.lower().replace('-', ' ')} that "
        "nobody counts is indistinguishable from one that never happened."
    )


def test_the_recorders_name_the_exception():
    """`failopen.record(tag)` without the exception records that something broke
    and not what. The whole value is the second argument."""
    for m in re.finditer(r'_fo\.record\("CHZ-FO-HOOK-[A-Z-]+"([^)]*)\)', SOURCE):
        assert "_exc" in m.group(1), f"recorded without the exception: {m.group(0)}"


def test_the_handlers_still_swallow():
    """Recording must not turn a fail-open into a fail-closed.

    The hook runs before the user sees anything; an exception escaping here
    costs the turn. Every recorded site must still fall through.
    """
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            body_src = ast.get_source_segment(SOURCE, h) or ""
            if "CHZ-FO-HOOK-" not in body_src:
                continue
            assert not any(isinstance(n, ast.Raise) for n in ast.walk(h)), (
                f"handler at line {h.lineno} re-raises; the hook must fail open"
            )


def test_recording_cannot_itself_break_the_hook():
    """A failing recorder must not be what takes the turn down."""
    import llm_router.failopen as failopen
    # record() is the only entry point these sites use; it must swallow its own
    # errors rather than propagating into a handler that exists to be safe.
    src = Path(failopen.__file__).read_text()
    fn = src.split("def record(", 1)[1].split("\ndef ", 1)[0]
    assert "except" in fn, (
        "failopen.record has no internal guard; a failure inside the recorder "
        "would escape from a handler whose entire purpose is to not escape"
    )


def test_trace_and_display_sites_are_deliberately_not_recorded():
    """Scope is a decision, not an oversight.

    Recording a trace failure through the recorder can recurse, and an absent
    optional feature is not an incident. If this ever changes, it should be a
    choice someone made rather than a sweep.
    """
    assert "CHZ-FO-HOOK-TRACE" not in SOURCE
    assert "CHZ-FO-HOOK-DEBUG-LOG" not in SOURCE
