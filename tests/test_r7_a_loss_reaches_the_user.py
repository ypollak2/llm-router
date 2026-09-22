"""AUD-06, the surfaces the lint never scanned.

`llm_router.savings.net_saved` and `scripts/lint_savings_sign.py` were built
because `13_HISTORICAL_DEFECT_PATTERNS.md` records the $15/$75 price bug being
fixed locally four separate times and returning every time, "because no fix was
ever made structural."

The lint was the structural half. Its SCOPE was a hand-maintained tuple of
eleven module paths — so it was structural only for the files someone thought
of. Five money surfaces were never on it, carrying **eight clamped
subtractions** between them:

    commands/share.py:112             total_saved += max(0.0, base - cost)
    hooks/session-end-clawcode.py     x4
    hooks/status-bar.py:240           max(0.0, baseline - actual)
    hooks/status-bar-clawcode.py:92   max(0.0, baseline - actual)
    dashboard/server.py:157           round(max(0.0, baseline - external), 4)

`share.py` builds a card meant to be PUBLISHED. `dashboard/server.py` is the
number in the web UI's headline tile. Both would render a routing loss as
"$0.00 saved" — not wrong by a rounding error, but silent about the one fact a
user needs: routing cost them money.

R6 derives the lint's module list from `savings.SURFACES` instead. The two
mechanisms now close each other's gap: a new surface that is not registered
fails R6's discovery test, and a surface that IS registered is automatically
linted. Neither can be forgotten independently.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_the_clamp_lint_covers_every_registered_surface():
    """The scope, not the rule. The rule was already right."""
    sys.path.insert(0, str(REPO / "src"))
    from llm_router.savings import SURFACES

    # Import it as a module so its own `__file__`-relative REPO resolves.
    import importlib.util

    lint_path = REPO / "scripts/lint_savings_sign.py"
    spec = importlib.util.spec_from_file_location("_lint_savings_sign", lint_path)
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    scanned = set(lint.MONEY_MODULES)

    missing = sorted(
        f"src/llm_router/{s.where.split(':', 1)[0]}"
        for s in SURFACES
        if f"src/llm_router/{s.where.split(':', 1)[0]}" not in scanned
    )
    assert not missing, (
        f"registered savings surface(s) the clamp lint does not scan: {missing}"
    )


def test_the_lint_scope_is_derived_not_hand_written():
    """AST: `MONEY_MODULES` must be built from the registry, not a literal.

    A literal tuple is what let five surfaces sit outside the lint for as long
    as they did. Asserting on the source text would pass with the call in a
    comment (A-10), so this asserts the module actually calls the deriver.
    """
    tree = ast.parse((REPO / "scripts/lint_savings_sign.py").read_text(encoding="utf-8"))
    assign = next(
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "MONEY_MODULES" for t in n.targets)
    )
    calls = {ast.unparse(c.func) for c in ast.walk(assign) if isinstance(c, ast.Call)}
    assert "_surface_modules" in calls, (
        "MONEY_MODULES is no longer derived from savings.SURFACES. A "
        "hand-written list goes stale, which is the defect this assertion "
        "exists to prevent — it is how share.py, both statuslines, "
        "session-end-clawcode.py and the web dashboard went unscanned."
    )


def test_the_lint_scans_meaningfully_more_than_it_used_to():
    """Anti-vacuity. A deriver returning () passes every assertion above."""
    proc = subprocess.run(
        [sys.executable, "scripts/lint_savings_sign.py"],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert proc.returncode == 0, (
        f"CHZ-SS-01 is failing:\n{proc.stdout}\n{proc.stderr}"
    )
    # "no clamped savings subtraction in N money modules."
    import re
    m = re.search(r"in (\d+) money modules", proc.stdout)
    assert m, f"could not read the module count from: {proc.stdout!r}"
    n = int(m.group(1))
    assert n >= 20, (
        f"the lint scans only {n} modules. It scanned 10 before R6 and 23 "
        "after; a drop back means the deriver has stopped finding the "
        "surfaces and the lint is quietly protecting less than it claims."
    )


def _net_helpers() -> list[pathlib.Path]:
    return [
        REPO / "src/llm_router/commands/share.py",
        REPO / "src/llm_router/hooks/session-end-clawcode.py",
        REPO / "src/llm_router/hooks/status-bar.py",
        REPO / "src/llm_router/hooks/status-bar-clawcode.py",
        REPO / "src/llm_router/dashboard/server.py",
    ]


def test_the_import_fallback_is_signed_not_clamped():
    """The defect must not return through the error path.

    Each surface wraps `net_saved` in a try/except so a broken import cannot
    take down a statusline. If that fallback clamped, the clamp would come back
    on exactly the machines where something else is already wrong — and it came
    back four times before.
    """
    for path in _net_helpers():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_net"),
            None,
        )
        assert fn is not None, f"{path.name} has no _net helper"
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "max":
                raise AssertionError(
                    f"{path.name}:_net calls max() — the fallback clamps, so "
                    "the defect returns whenever the import fails"
                )
        # And it must really delegate when it can.
        calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert "net_saved" in calls, f"{path.name}:_net does not call net_saved"


def test_a_loss_survives_every_net_helper():
    """Behaviour, not shape: feed each helper a loss and require a loss out."""
    import importlib.util

    for path in _net_helpers():
        spec = importlib.util.spec_from_file_location(f"probe_{path.stem}", path)
        # Importing a hook script executes it; read the function out of a
        # namespace built from its source instead.
        ns: dict = {}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_net"
        )
        mod = ast.Module(body=[fn], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), str(path), "exec"), ns)  # noqa: S102
        got = ns["_net"](1.0, 4.0)
        assert got == -3.0, (
            f"{path.name}:_net({1.0}, {4.0}) returned {got}; routing that cost "
            "more than it saved must render as a loss"
        )
        assert spec is not None
