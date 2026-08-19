"""CHZ-FO-02 must catch the fail-open that CHZ-FO-01 provably could not.

Audit #32. `lint_fail_open.py` asked "does this handler leave a trace?". The redaction
fail-open — the defect that sent unredacted prompts to external models — logged
impeccably, so the gate saw nothing wrong with it. Measured at the time:

    with the fail-CLOSED code : 0 violations
    with the fail-OPEN code   : 0 violations      <-- the version that leaked

That is why #32 refused to widen `PROTECTED` as the "natural follow-up": adding modules to
a check that cannot see the defect produces a green result an auditor would reasonably
read as coverage. A guard that cannot fail is worse than no guard, because it is believed.

CHZ-FO-02 asks the other question: does the handler hand its caller something
indistinguishable from success?

THE FIXTURE BELOW IS THE ORIGINAL DEFECT, kept as a literal. If someone later loosens the
rule, this test goes red with the exact code that shipped the leak — not with a synthetic
approximation of it.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint_fail_open.py"


def _gate():
    spec = importlib.util.spec_from_file_location("lint_fail_open", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _only_handler(src: str) -> ast.ExceptHandler:
    handlers = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1, f"fixture must have exactly one handler, got {len(handlers)}"
    return handlers[0]


#: The original redaction fail-open, verbatim in shape. It logs, and returns the caller's
#: own prompt — which is exactly what the success path returns when nothing needed
#: redacting. Indistinguishable, by construction.
THE_ORIGINAL_FAIL_OPEN = '''
def redact(prompt):
    try:
        return _redactor.apply(prompt)
    except Exception as err:
        log.warning("redaction_failed", error=str(err))
        return prompt, {}
'''

#: `execution_ledger.record_event` on the loss path. Returns a CONSTANT where the success
#: path returns True, so the caller can test it — and RED5-02 made all seven call sites
#: bind that boolean. A failure reported, not hidden.
A_SENTINEL_RETURN = '''
def record_event(ev):
    try:
        return _write(ev)
    except Exception as exc:
        _log.warning("LEDGER_EVENT_DROPPED %s", exc)
        return False
'''

#: The same shape as the fail-open, but instrumented. The degradation is counted under an
#: event code, so it appears on the dashboard instead of only in a log nobody greps.
THE_SAME_SHAPE_BUT_RECORDED = '''
def inject(prompt, provider):
    try:
        return prompt + _context_for(provider)
    except Exception as e:
        failopen.record("CHZ-FO-ROUTER-CLI-CONTEXT", e)
        log.debug("context injection unavailable: %s", e)
        return prompt
'''


class TestTheCheckAsksTheRightQuestion:
    def test_the_ORIGINAL_fail_open_is_caught(self):
        """The whole point. CHZ-FO-01 said clean on this exact code."""
        g = _gate()
        h = _only_handler(THE_ORIGINAL_FAIL_OPEN)
        assert g._returns_live_data(h), "returning the caller's own prompt is live data"
        assert not g._records_a_failopen(h), "it logged; it did not record"

    def test_CHZ_FO_01_still_says_this_handler_is_FINE(self):
        """Pins the gap itself, so nobody 'fixes' CHZ-FO-02 by folding it into CHZ-FO-01.

        The two checks answer different questions and both are worth keeping: a silent
        handler is a problem even when it returns a sentinel, and a live-data return is a
        problem even when it logs.
        """
        g = _gate()
        assert g._leaves_a_trace(_only_handler(THE_ORIGINAL_FAIL_OPEN)), (
            "CHZ-FO-01 considers this traced — that is the documented limitation, not a "
            "bug to be fixed here"
        )

    def test_a_constant_return_is_a_SIGNAL_not_a_fail_open(self):
        """`return False` where success returns True. The caller can test it."""
        g = _gate()
        assert not g._returns_live_data(_only_handler(A_SENTINEL_RETURN)), (
            "flagging sentinel returns would make the gate noisy and train people to "
            "ignore it — execution_ledger deliberately signals loss this way"
        )

    def test_recording_the_degradation_clears_it(self):
        g = _gate()
        h = _only_handler(THE_SAME_SHAPE_BUT_RECORDED)
        assert g._returns_live_data(h), "still returns live data"
        assert g._records_a_failopen(h), "but the degradation is now counted"


class TestTheGateIsCleanAndCanStillFail:
    def test_the_protected_modules_pass_CHZ_FO_02_today(self):
        """Landed at ZERO violations, not on a grandfathered baseline.

        A baseline that carries existing violations forward is the antipattern already
        filed as audit #22 (the G4 ratchet). It was affordable to land clean here because
        the check flagged exactly 1 site of 96 broad handlers, and that one was fixed.
        """
        assert _gate().scan_returns() == []

    def test_the_check_is_not_vacuous(self, tmp_path):
        """A gate that cannot fail is the eleventh instance this audit has found. Prove
        this one fails on a real violation rather than trusting that it would."""
        g = _gate()
        bad = tmp_path / "bad.py"
        bad.write_text(THE_ORIGINAL_FAIL_OPEN)
        # scan_returns resolves paths against REPO, so point REPO at tmp_path.
        g.REPO = tmp_path
        # Each entry is a TUPLE of candidate paths for one module — the gate
        # accepts alternatives so a relocated module stays covered instead of
        # reading as MISSING.
        findings = g.scan_returns((("bad.py",),))
        assert len(findings) == 1, findings
        assert "without failopen.record" in findings[0]

    def test_a_missing_protected_module_is_reported_not_skipped(self, tmp_path):
        """Silently skipping an unreadable file is how a gate reports clean while blind."""
        g = _gate()
        g.REPO = tmp_path
        findings = g.scan_returns((("does_not_exist.py",),))
        assert findings and "MISSING" in findings[0]
