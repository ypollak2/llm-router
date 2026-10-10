"""Guards for the serial `timing` lane (TIMING-1, docs/bugs/TIMING-1.md).

Tests tagged `timing` assert on wall-clock speed and run in their own serial CI
job (`pytest -m timing -p no:xdist`); the parallel job runs `-m "not timing ..."`.
Three ways that arrangement can silently stop doing its job, one test each:

* the marker is dropped from pyproject (strict markers would then error, but
  only if someone runs with --strict-markers),
* ci.yml's `-m` expressions drift from pyproject's addopts (a command-line `-m`
  REPLACES the addopts `-m`, so a dropped `not slow` re-enables slow tests),
* the tagged set shrinks (an un-tag or a file deletion removes coverage with no
  failure anywhere: an empty lane passes everything).

A fourth test flags NEW timing assertions that are not tagged.
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
CI = REPO / ".github" / "workflows" / "ci.yml"

# Floor for the number of test FUNCTIONS tagged `timing` (module-level or
# class-level pytestmark counts every test in scope). Raise it when the lane
# grows; lowering it needs a reason in the PR.
MIN_TAGGED = 125

TIME_FUNCS = {"perf_counter", "monotonic", "time_ns", "perf_counter_ns", "monotonic_ns"}


def _addopts_exclusions() -> str:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    addopts = cfg["tool"]["pytest"]["ini_options"]["addopts"]
    m = re.search(r"-m\s+'([^']+)'", addopts)
    assert m, f"no -m expression in addopts: {addopts!r}"
    return m.group(1)


def _is_timing_mark(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "timing" or (
        isinstance(node, ast.Call) and _is_timing_mark(node.func))


def _marks_timing(decorators: list[ast.expr]) -> bool:
    return any(_is_timing_mark(d) for d in decorators)


def _module_marked(tree: ast.Module) -> bool:
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets):
            v = n.value
            items = v.elts if isinstance(v, (ast.List, ast.Tuple)) else [v]
            if any(_is_timing_mark(i) for i in items):
                return True
    return False


def _iter_tests(tree: ast.Module):
    """Yield (qualname, node, marked) for every test function."""
    mod = _module_marked(tree)

    def walk(body, cls_marked, prefix):
        for n in body:
            if isinstance(n, ast.ClassDef):
                yield from walk(n.body, cls_marked or _marks_timing(n.decorator_list),
                                f"{prefix}{n.name}.")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                yield f"{prefix}{n.name}", n, mod or cls_marked or _marks_timing(n.decorator_list)

    yield from walk(tree.body, False, "")


def _all_tests():
    for p in sorted(TESTS.rglob("*.py")):
        if p.name == Path(__file__).name:
            continue
        tree = ast.parse(p.read_text())
        for qual, node, marked in _iter_tests(tree):
            yield p.relative_to(REPO).as_posix(), qual, node, marked


def test_the_marker_is_registered():
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("timing:") for m in markers)


def test_ci_expressions_match_pyproject_addopts():
    text = CI.read_text()
    excl = _addopts_exclusions()
    assert f'-m "not timing and {excl}"' in text, "parallel job lost an addopts exclusion"
    assert f'-m "timing and {excl}"' in text, "timing job lost an addopts exclusion"
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    jobs = re.split(r"\n  (?=[a-z-]+:\n)", text)
    timing = next(j for j in jobs if j.startswith("timing:"))
    assert "-p no:xdist" in timing and "-n auto" not in timing, "timing lane must be serial"
    assert "-m \"timing and" in timing and "not timing" not in timing


def test_the_tagged_set_has_not_shrunk():
    tagged = [(f, q) for f, q, _n, marked in _all_tests() if marked]
    assert len(tagged) >= MIN_TAGGED, (
        f"only {len(tagged)} tests are tagged `timing`, floor is {MIN_TAGGED}: "
        "a test lost its mark (it now runs in the parallel job and will flake) or was deleted")


def _asserts_on_a_clock_delta(fn: ast.AST) -> list[int]:
    """Lines of `assert` statements that compare a clock reading or a delta of one.

    Deliberately narrow: a direct perf_counter/monotonic call inside the assert, or
    a name assigned from `<clock call> - x` / `x - <clock call>` (a duration) used in it.
    `time.time()` is excluded: it is overwhelmingly a timestamp, not a measurement.
    """
    def is_clock(c: ast.AST) -> bool:
        return (isinstance(c, ast.Call) and (
            (isinstance(c.func, ast.Attribute) and c.func.attr in TIME_FUNCS)
            or (isinstance(c.func, ast.Name) and c.func.id in TIME_FUNCS)))

    def has_clock(n: ast.AST) -> bool:
        return any(is_clock(x) for x in ast.walk(n))

    def names(n: ast.AST) -> set[str]:
        return {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}

    stamps: set[str] = set()
    durs: set[str] = set()
    for _ in range(3):
        for a in ast.walk(fn):
            if isinstance(a, ast.Assign):
                tg = {x.id for t in a.targets for x in ast.walk(t) if isinstance(x, ast.Name)}
                subs = [x for x in ast.walk(a.value) if isinstance(x, ast.BinOp) and isinstance(x.op, ast.Sub)]
                if any(has_clock(s) or names(s) & stamps for s in subs):
                    durs |= tg
                elif has_clock(a.value):
                    stamps |= tg
    hits = []
    for a in ast.walk(fn):
        if isinstance(a, ast.Assert):
            t = a.test
            if has_clock(t) or names(t) & durs:
                hits.append(a.lineno)
    return hits


# Untagged tests that read a clock inside an assert and are deliberately NOT timing
# (generous polling deadline, or the clock is data). Each needs a reason.
_DATA = "monotonic() used as data: an injected age/deadline, no real wait or measurement"
_LOWER = "only a lower bound on a real sleep: load can lengthen it, never break it"
_GENEROUS = "deadline handed to a function is generous (>= 5 s) or already expired; no measurement"
NOT_TIMING = {
    "tests/test_readonly_draft_deadline.py::test_readonly_draft_deadline_never_goes_negative_on_an_already_late_hook": _DATA,
    "tests/qa/test_network_failures.py::test_rate_limit_recovers_after_custom_cooldown": _DATA,
    "tests/qa/test_network_failures.py::test_breaker_recovers_after_cooldown": _DATA,
    "tests/qa/test_network_failures.py::test_state_machine_failure_success_failure_cycle": _DATA,
    "tests/scenarios/test_cross_cutting.py::test_scenario_stale_failure_reset_recovers_provider": _DATA,
    "tests/test_a30_agent_loop_deadline_units.py::test_an_exhausted_deadline_does_not_start_the_loop": _GENEROUS,
    "tests/test_agt_a0_review_followups.py::test_same_root_runs_are_serialised": _LOWER,
    "tests/test_cache.py::TestCacheTTL.test_expired_entry_returns_none": _DATA,
    "tests/test_direct_executor_deadline.py::test_no_call_is_started_with_no_budget_left": _GENEROUS,
    "tests/test_edge_cases.py::TestCacheEdgeCases.test_ttl_boundary": _DATA,
    "tests/test_edge_cases.py::TestHealthEdgeCases.test_rapid_failure_and_recovery": _DATA,
    "tests/test_health.py::TestProviderHealth.test_recovers_after_cooldown": _DATA,
    "tests/test_local_timeout_demotion.py::test_model_leads_again_once_the_cooldown_has_passed": _DATA,
    "tests/test_local_agent_verify.py::test_no_changed_files_is_a_trivial_pass": _GENEROUS,
    "tests/test_rate_limit.py::TestProviderHealthRateLimit.test_rate_limit_clears_after_cooldown": _DATA,
    "tests/test_routing_value.py::TestCircuitBreaker.test_rate_limit_recovers_after_cooldown": _DATA,
    "tests/test_warm_plan_3_7.py::test_should_skip_cold_does_not_skip_when_resident": _GENEROUS,
    "tests/test_warm_plan_3_7.py::test_should_skip_cold_matches_untagged_name": _GENEROUS,
    "tests/test_warm_plan_3_7.py::test_should_skip_cold_generous_deadline_gets_a_real_attempt": _GENEROUS,
    "tests/test_warm_plan_3_7.py::test_should_skip_cold_unknown_state_proceeds": _GENEROUS,
}


def test_new_clock_assertions_are_tagged_timing():
    offenders = []
    for f, qual, node, marked in _all_tests():
        if marked or f"{f}::{qual}" in NOT_TIMING:
            continue
        lines = _asserts_on_a_clock_delta(node)
        if lines:
            offenders.append(f"{f}::{qual} (assert at line {lines[0]})")
    assert not offenders, (
        "These tests assert on a perf_counter/monotonic reading but are not tagged "
        "`@pytest.mark.timing`, so they run in the parallel job and will flake under load. "
        "Tag them, or (if the clock is data, not a measurement) add them to NOT_TIMING "
        "with the reason:\n  " + "\n  ".join(offenders))
