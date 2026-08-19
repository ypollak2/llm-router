#!/usr/bin/env python3
"""CHZ-SS-01 — no clamped savings subtraction in a money module.

AUD-06: "TOTAL saved is a sum of wins, not a net -- losses clamped to zero before
aggregation." Invariant I-2 ("unknown/adverse never becomes favourable") is
recorded FALSE because of it.

WP-04/WP-05 fixed ONE site (hooks/session-end.py) and pinned it with
tests/economics/test_savings_sign.py, which loads that one file. ELEVEN other
surfaces kept the clamp, including cost.get_team_savings, whose output team.py
broadcasts to Slack/Discord, and dashboard's
``net_saved = max(0, gross - overhead)`` -- AUD-06's sentence verbatim.

13_HISTORICAL_DEFECT_PATTERNS.md records the $15/$75 price bug being fixed
locally FOUR separate times and returning every time, "because no fix was ever
made structural". Editing eleven call sites would be the fifth. This is the
structural half: the twelfth surface fails CI instead of a user's dashboard.

WHAT IT MATCHES
---------------
A call to ``max`` whose first argument is a literal zero and whose remaining
arguments contain a SUBTRACTION -- i.e. ``max(0, a - b)`` / ``max(0.0, x - y)``.
That is the shape of a clamped net.

BLIND SPOTS, STATED HERE RATHER THAN DISCOVERED LATER
------------------------------------------------------
This is an AST shape match, and it can be defeated without trying:

  * ``delta = a - b`` on one line and ``max(0.0, delta)`` on the next. The
    subtraction is no longer inside the call. THIS IS NOT HYPOTHETICAL --
    execution_ledger.py:582 has exactly that shape, and it is the one
    legitimate exemption, which means an ILLEGITIMATE one would hide equally
    well.
  * ``if x < 0: x = 0``, ``abs()``, or a clamp inside a helper this script
    never reads.
  * SQL that does the clamping, or a template that renders a negative as "0.00".

So a clean CHZ-SS-01 means "no clamped subtraction is spelled inline in these
modules". It does NOT mean "no surface hides a loss". Anyone reading a pass here
as the latter has the same misunderstanding that let Gate 7 certify AUD-06 in the
first place: a check that answers a narrow question, read as answering a broad
one.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Modules whose job is to compute or display money. Zero tolerance inside these.
#: Deliberately a NAMED LIST, not "everything": a wide net produces noise, and a
#: noisy gate gets disabled, which is worse than a narrow one that is trusted.
MONEY_MODULES = (
    "src/llm_router/cost.py",
    "src/llm_router/router.py",
    "src/llm_router/digest.py",
    "src/llm_router/retrospective.py",
    "src/llm_router/route_server.py",
    "src/llm_router/tools/dashboard.py",
    "src/llm_router/dashboard_data.py",
    "src/llm_router/summary.py",
    "src/llm_router/team.py",
    "src/llm_router/execution_ledger.py",
    "src/llm_router/hooks/session-end.py",
)

#: (module, line-content substring) pairs allowed to clamp, each with a reason.
#: An exemption must name a metric that is upside-only BY DEFINITION and has a
#: signed sibling -- see llm_router.savings.GROSS_POTENTIAL_RATIONALE. Keep this
#: list short: every entry is a place a real clamp could hide.
EXEMPTIONS = {
    (
        "src/llm_router/execution_ledger.py",
        "saving = max(0.0, delta)",
    ): (
        "potential_savings_usd is documented as 'Σ max(0, baseline_eq − actual) "
        "over ALL routes' -- an upside-only metric, paired with the SIGNED "
        "net_realized_savings_usd on the same accumulator"
    ),
}


def _is_zero(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value == 0
    )


def _has_subtraction(node: ast.expr) -> bool:
    return any(
        isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)
        for n in ast.walk(node)
    )


#: Words that mark a quantity as a SAVINGS figure. The first draft of this lint
#: matched `max(0, <any subtraction>)` and flagged string padding
#: (`max(0, 60 - len(text))`), a timespan (`latest_ts - earliest_ts`) and nine
#: budget counters (`_pending_spend - _reservation`) -- all correct code. A gate
#: that cries wolf gets disabled, which is worse than a narrow one that is
#: trusted, so the match now requires savings vocabulary.
_SAVINGS_WORDS = ("saved", "saving", "avoided", "benefit")
#: `baseline` in an operand marks a counterfactual subtraction even when the
#: target is anonymous (e.g. a bare `return round(max(0.0, base - actual), 6)`).
_COUNTERFACTUAL_WORDS = ("baseline", "baseline_equivalent", "potential_cost")


def _names_in(node: ast.AST) -> str:
    """Every identifier, attribute and string constant under ``node``, lowered."""
    parts: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            parts.append(n.id)
        elif isinstance(n, ast.Attribute):
            parts.append(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append(n.value)
    return " ".join(parts).lower()


def _target_text(tree: ast.Module, call: ast.Call) -> str:
    """What the clamped value is ASSIGNED to, keyed by, or passed as.

    Needed because the name carrying the meaning usually sits outside the call:
    `saved_usd = max(...)`, `"saved": max(...)`, `saved_usd=max(...)`.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            if call in set(ast.walk(node.value)) if node.value else False:
                tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
                return " ".join(_names_in(t) for t in tgts)
        elif isinstance(node, ast.keyword) and call in set(ast.walk(node.value)):
            return (node.arg or "").lower()
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if v is not None and call in set(ast.walk(v)) and k is not None:
                    return _names_in(k)
    return ""


def violations() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for rel in MONEY_MODULES:
        path = REPO / rel
        if not path.exists():
            continue
        src = path.read_text()
        lines = src.splitlines()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "max"):
                continue
            if len(node.args) < 2 or not _is_zero(node.args[0]):
                continue

            arg_text = " ".join(_names_in(a) for a in node.args[1:])
            target = _target_text(tree, node)
            blob = f"{arg_text} {target}"
            is_savings = any(w in blob for w in _SAVINGS_WORDS)
            is_counterfactual = any(w in arg_text for w in _COUNTERFACTUAL_WORDS)

            if _has_subtraction_in := any(_has_subtraction(a) for a in node.args[1:]):
                # `max(0, baseline - actual)` — the classic clamped net.
                if not (is_savings or is_counterfactual):
                    continue
            else:
                # `max(0, total_saved)` with the subtraction on an EARLIER line.
                # dashboard.py's `net_saved = max(0, total_saved)` is the purest
                # AUD-06 in the codebase -- gross minus overhead, then clamped --
                # and a shape-match on the call alone cannot see it. Catching it
                # needs the NAME, which is why savings vocabulary is load-bearing
                # here rather than a convenience.
                if not is_savings:
                    continue

            line = lines[node.lineno - 1].strip()
            if any(rel == m and frag in line for (m, frag) in EXEMPTIONS):
                continue
            out.append((rel, node.lineno, line))
    return out


def main() -> int:
    # Guards the guard: if the scan matches nothing anywhere, a clean result is
    # indistinguishable from a broken matcher. The same shape as the probe that
    # reported "0/6 reproductions" while measuring nothing, and the reason
    # test_the_scan_finds_something exists in three test files.
    present = [m for m in MONEY_MODULES if (REPO / m).exists()]
    if len(present) < 5:
        print(
            f"CHZ-SS-01 FAIL: only {len(present)} of {len(MONEY_MODULES)} money "
            "modules found — the module list is stale and this lint is checking "
            "almost nothing.",
            file=sys.stderr,
        )
        return 1

    bad = violations()
    if not bad:
        print(f"CHZ-SS-01 OK: no clamped savings subtraction in {len(present)} money modules.")
        return 0

    print("CHZ-SS-01 FAIL — a savings subtraction is clamped to zero (AUD-06):\n", file=sys.stderr)
    for rel, lineno, line in bad:
        print(f"  {rel}:{lineno}\n      {line}", file=sys.stderr)
    print(
        "\nUse llm_router.savings.net_saved(baseline, actual), which is signed. A loss "
        "must reach the user: the clamp is exactly what stopped them finding out."
        "\nIf the metric is upside-only BY DEFINITION and has a signed sibling, add "
        "it to EXEMPTIONS with that reason.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
