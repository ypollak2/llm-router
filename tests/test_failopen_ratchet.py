"""New silent failure handlers may not be added to the routing path.

C3 of docs/ACTIONS_REMEDIATION_RUN.md, narrowed on evidence. The external gap
review (P1-3) says broad fail-open handling can hide loss of routing and evidence.
Verified: `hooks/auto-route.py` contains 32 BROAD handlers (bare,
`Exception` or `BaseException`) whose entire body is `pass` or `return None`,
with no `failopen.record`. The core modules — router.py, cost.py,
classify.py and both chain builders — are already clean, so the problem is
localised, not systemic.

Retrofitting all 58 at once would be a large blind change, and many are genuinely
cosmetic (a trace emit that fails must not take the turn down, and recording it
could recurse). What matters is that the number goes DOWN and never up. This is a
ratchet: it pins today's count and fails if a new one appears.

To fix a site, wrap it:

    except Exception as exc:             # noqa: BLE001
        from llm_router import failopen
        failopen.record("CHZ-FO-<WHAT-WAS-LOST>", exc)
        ...

then lower the number below. Raising the number needs a reason in the commit.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src/llm_router"

# Measured 2026-09-14 at 45; lowered to 32 on 2026-09-15 when the 13 sites
# that lose EVIDENCE or change ROUTING were given failopen.record
# (tests/test_failopen_records_evidence_loss.py). The remaining 32 are trace
# emits, display, and optional-feature imports, where recording would be noise
# and could recurse. Lower this as more are fixed; never raise it silently. Lower this as sites are fixed; never raise it silently.
BASELINE = {"hooks/auto-route.py": 32}

# These carry the routing decision and its evidence. A silent failure here loses a
# route or a receipt with no trace that it happened.
MUST_STAY_CLEAN = [
    "router.py", "cost.py", "classify.py",
    "chain_builder.py", "hooks/chain_builder.py",
]


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """`except:` / `except Exception` / `except BaseException` only.

    A NARROW typed catch is not a fail-open: `except OSError: pass` in
    `_safe_unlink`, `except ValueError: return None` parsing a retry-after header,
    and `except asyncio.CancelledError: pass` after cancelling a task are all the
    correct contract for those calls. Counting them made this check fire on six
    pieces of good code in router.py and cost.py; the defect is swallowing
    ANYTHING, not handling something specific.
    """
    t = handler.type
    if t is None:                       # bare `except:`
        return True
    names = [t] if not isinstance(t, ast.Tuple) else list(t.elts)
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException")
               for n in names)


def _silent_handlers(path: Path) -> list[int]:
    """Broad handlers whose whole body is `pass` or `return None`, unrecorded."""
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        body = node.body
        if len(body) != 1:
            continue
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            out.append(node.lineno)
        elif isinstance(stmt, ast.Return) and (
            stmt.value is None
            or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
        ):
            out.append(node.lineno)
    return out


@pytest.mark.parametrize("rel,cap", sorted(BASELINE.items()))
def test_silent_handlers_do_not_increase(rel, cap):
    found = _silent_handlers(SRC / rel)
    assert len(found) <= cap, (
        f"{rel} now has {len(found)} silent failure handlers, up from {cap}. "
        f"New one(s) near line(s) {sorted(set(found))[cap:]}. A failure that "
        "returns None with no failopen.record is a lost route nobody can count."
    )


@pytest.mark.parametrize("rel", MUST_STAY_CLEAN)
def test_the_decision_and_evidence_modules_stay_clean(rel):
    found = _silent_handlers(SRC / rel)
    assert not found, (
        f"{rel} gained a silent handler at line(s) {found}. This module carries "
        "the routing decision or its evidence; a failure here must be recorded."
    )


def test_the_ratchet_is_measuring_something():
    # A ratchet whose detector finds nothing passes everything.
    assert _silent_handlers(SRC / "hooks/auto-route.py"), (
        "the detector found zero handlers — it has stopped working, and would "
        "now pass any file at all"
    )
