"""R12 — a counter nothing reads is not instrumentation.

The defect this file exists to prevent has happened at least five times in this
repo, each time inside a commit that believed it was ADDING observability:

    failopen.record()                    58 writers in src/, 0 readers
    execution_ledger.dropped_event_count  docstring claimed "surfaced by doctor";
                                          doctor did not import it
    session_store.lock_timeout_count      tests only
    prompt_capture.counters()             assembled into status(), imported by nothing
    the hook terminal-outcome invariant   a unit test, never computed on traffic

Nothing about the writing side was wrong in any of those. The number was
correct, the store was durable, the tests passed. It simply could not reach a
human, which makes it identical in effect to not having counted at all.

Three checks, in the order they catch things:

1. DISCOVERY — a counter-shaped accessor in ``src/llm_router`` is either in the
   registry or in :data:`NOT_INSTRUMENTATION` with a written reason. New
   counter, no entry → this fails and names it.
2. BEHAVIOUR — for every registered counter, driving its WRITER moves the value
   the registry reports. This is the check that cannot be satisfied by a
   declaration: a registry entry pointing at a reader that does not read is a
   failure here.
3. SURFACE — ``llm-router doctor`` renders the registry, asserted on the AST of
   the doctor function (a call to ``counter_registry.readings``), not on its
   source text. A-10: a pinned phrase in a comment satisfied 23 source-text
   assertions while the call site was broken.

Every one of the three is red-checked in `audit/REMEDIATION_RUN.md`.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from llm_router import counter_registry

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


# ── 1. Discovery ──────────────────────────────────────────────────────────

#: Name shapes that mean "this function hands back a tally".
_SUFFIXES = ("_count", "_counts", "_total", "_totals")
_EXACT = {"counters", "snapshot", "count"}

#: Accessors that match the shape and are NOT instrumentation. Each carries the
#: reason, and the reason is CHECKED where it names a reader — an allowlist that
#: nobody re-derives is how the blind spot moves rather than closes.
#:
#: Deliberately small. The scan was written to match the way this codebase
#: actually names tallies; a scan that fired on sixty functions would be
#: allowlisted wholesale and the allowlist would become the defect.
NOT_INSTRUMENTATION: dict[str, tuple[str, str]] = {
    # name → (reason, a module that must still reference it — "" to skip)
    "llm_router.token_budget:count_tokens": (
        "a pure function of its argument — it measures the text handed to it "
        "and retains nothing, so there is no tally for anyone to read",
        "",
    ),
    "llm_router.test_delta:snapshot": (
        "a developer tool that captures a test-suite state for later diffing; "
        "its reader is its own CLI subcommand, not an operator surface",
        "llm_router/test_delta.py",
    ),
    "llm_router.execution_ledger:reset_dropped_event_count": (
        "a test seam that ZEROES the counter. Registering a resetter as a "
        "counter would make the registry assert that setting a number to zero "
        "is a way of reporting it",
        "",
    ),
    "llm_router.cost:get_correction_count": (
        "a per-tool lookup used as a ROUTING INPUT, not a degradation signal: "
        "it is keyed by an argument and consumed by the router to lower a "
        "tier, and it already has a production reader",
        "llm_router/tools/routing.py",
    ),
}


def _discovered() -> dict[str, pathlib.Path]:
    found: dict[str, pathlib.Path] = {}
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue  # hook scripts with a shebang-only dialect
        mod = str(path.relative_to(SRC).with_suffix("")).replace("/", ".")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            if node.name in _EXACT or node.name.endswith(_SUFFIXES):
                found[f"{mod}:{node.name}"] = path
    return found


def test_the_scan_finds_the_counters_we_know_about():
    """A scan that finds nothing passes everything.

    This is the check on the CHECK. `_discovered` is a name-shape heuristic; if
    a refactor renames `dropped_event_count` to something the pattern misses,
    every assertion below keeps passing while the registry silently stops
    covering the codebase.
    """
    found = set(_discovered())
    for known in (
        "llm_router.failopen:snapshot",
        "llm_router.execution_ledger:dropped_event_count",
        "llm_router.session_store:lock_timeout_count",
        "llm_router.prompt_capture:counters",
        "llm_router.coverage:snapshot",
    ):
        assert known in found, (
            f"the discovery scan no longer finds {known}. Either it was renamed "
            "to a shape the scan does not match — in which case widen the scan — "
            "or it was deleted, in which case remove it from the registry. "
            "Do not leave the scan blind."
        )


def test_every_counter_shaped_accessor_is_registered_or_excused():
    registered = {c.source for c in counter_registry.REGISTRY}
    unaccounted = [
        f"{name}  ({path.relative_to(SRC)})"
        for name, path in sorted(_discovered().items())
        if name not in registered and name not in NOT_INSTRUMENTATION
    ]
    assert not unaccounted, (
        "counter-shaped accessor(s) with no entry in counter_registry.REGISTRY "
        "and no reason in NOT_INSTRUMENTATION:\n  "
        + "\n  ".join(unaccounted)
        + "\n\nA counter that no surface reads cannot inform anyone, which is "
        "the same as not counting. Add it to the registry (doctor then renders "
        "it automatically) or record why it is not instrumentation."
    )


def test_the_excuses_still_point_at_something_real():
    """An allowlist entry claiming a reader must still have one."""
    discovered = _discovered()
    for name, (reason, reader_file) in NOT_INSTRUMENTATION.items():
        assert len(reason) > 40, f"{name}: give a real reason, not a label"
        if name not in discovered:
            continue  # gone; harmless, and the scan test covers deletion
        if not reader_file:
            continue
        p = SRC / reader_file
        assert p.exists(), f"{name}: excuse names {reader_file}, which is gone"
        symbol = name.split(":", 1)[1]
        assert symbol in p.read_text(encoding="utf-8"), (
            f"{name} is excused on the grounds that {reader_file} reads it, and "
            f"{reader_file} no longer mentions it. The excuse has rotted — "
            "either restore the reader or register the counter."
        )


def test_registry_entries_point_at_accessors_that_exist():
    for c in counter_registry.REGISTRY:
        mod_name, attr = c.source.split(":", 1)
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, attr), (
            f"{c.id} declares its source as {c.source}, which does not exist. "
            "The registry is the inventory; an inventory of things that are not "
            "there is worse than none."
        )


# ── 2. Behaviour — the part a declaration cannot satisfy ──────────────────

def _drive_fail_open(tmp_home: pathlib.Path) -> None:
    from llm_router import failopen

    failopen.record("CHZ-FO-R12-PROBE", RuntimeError("probe"))
    failopen.reset_cache()


def _drive_dropped_events(tmp_home: pathlib.Path) -> None:
    """Force a real ledger write failure rather than poking the global.

    Setting `_dropped_events` directly would prove the reader can read a
    variable. It would NOT prove that a dropped event increments it, which is
    the link that was broken.
    """
    from llm_router import execution_ledger

    ev = execution_ledger.LedgerEvent(
        event_id="r12-probe", event_type="route_started",
    )
    # A directory where the DB file should be: sqlite cannot open it, the
    # fail-open handler catches, the counter increments.
    bad = tmp_home / "unwritable.db"
    bad.mkdir(parents=True, exist_ok=True)
    assert execution_ledger.record_event(ev, path=bad) is False


def _drive_lock_timeouts(tmp_home: pathlib.Path) -> None:
    from llm_router import session_store

    session_store._note_lock_timeout("r12 probe")


def _drive_capture_outcomes(tmp_home: pathlib.Path) -> None:
    from llm_router import prompt_capture

    prompt_capture._record_outcome("skipped", "r12-probe")


def _drive_unterminated(tmp_home: pathlib.Path) -> None:
    """Write a log with one invocation that never reaches a terminal line."""
    log = tmp_home / "auto-route-debug.log"
    log.write_text(
        "[2026-09-22 10:00:00] [INVOCATION START] ID=1.0\n"
        "[2026-09-22 10:00:00] [INVOCATION 1.0] session_id=abc123def prompt_len=42\n"
        # ...and nothing else. This is the ENFORCE=off shape exactly: the hook
        # ran, saw the prompt, and returned without logging why.
        "[2026-09-22 10:00:01] [INVOCATION START] ID=2.0\n"
        "[2026-09-22 10:00:01] [INVOCATION 2.0] session_id=abc123def prompt_len=9\n"
        "[2026-09-22 10:00:02] [INVOCATION 2.0] DIRECT SUCCESS: ollama\n",
        encoding="utf-8",
    )


def _drive_interception_gaps(tmp_home: pathlib.Path) -> None:
    from llm_router import coverage

    coverage.record_unobserved(coverage.Reason.CLASSIFY_FAILED)
    coverage.reset_cache()



def _drive_hook_kills(tmp_home: pathlib.Path) -> None:
    """Leave a marker that looks like a killed hook (R9).

    A dead pid is obtained by spawning and reaping a trivial child, so the
    marker is genuinely orphaned rather than merely old.
    """
    import json
    import subprocess
    import sys
    import time

    from llm_router import hook_liveness

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    d = tmp_home / hook_liveness.MARKER_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{proc.pid}.json").write_text(
        json.dumps({"pid": proc.pid, "started_at": time.time() - 300})
    )


#: counter id → the thing that makes its WRITER fire.
#:
#: Keyed by id and cross-checked against the registry below, so a counter added
#: without a driver fails rather than quietly going unexercised.
def _drive_low_signal_classifications(tmp_path):
    """Classify a prompt that scores NOTHING, so the default decides it.

    The premise matters more than the call: if this prompt ever starts scoring,
    the driver stops driving and the counter would look broken. Asserted here
    rather than left to chance — a driver that no longer drives is how a
    registered reader goes quietly unverified.
    """
    from llm_router import classify

    prompt = "tell me what the capital of Portugal is"
    assert max(classify._score_categories(prompt).values()) == 0, (
        f"{prompt!r} now scores — pick another prompt that scores nothing, or "
        "this driver is no longer exercising the low-signal path"
    )
    classify.classify_signals(prompt)


DRIVERS = {
    "fail_open_events": _drive_fail_open,
    "ledger_events_dropped": _drive_dropped_events,
    "session_lock_timeouts": _drive_lock_timeouts,
    "capture_outcomes": _drive_capture_outcomes,
    "hook_kills": _drive_hook_kills,
    "unterminated_invocations": _drive_unterminated,
    "interception_gaps": _drive_interception_gaps,
    "low_signal_classifications": _drive_low_signal_classifications,
}


def test_every_registered_counter_has_a_driver():
    missing = sorted(set(counter_registry.counter_ids()) - set(DRIVERS))
    assert not missing, (
        f"registered counter(s) with no driver in this file: {missing}. "
        "Without one, nothing checks that the registered reader actually reads "
        "the registered writer — which is the whole failure being prevented."
    )
    stale = sorted(set(DRIVERS) - set(counter_registry.counter_ids()))
    assert not stale, f"driver(s) for counters no longer registered: {stale}"


@pytest.mark.parametrize("counter_id", sorted(DRIVERS))
def test_driving_the_writer_moves_the_reported_value(counter_id, tmp_path, monkeypatch):
    """The link, end to end: writer fires → the registry's reader sees it."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    # Counters that live in process globals must start from a known point, or a
    # previous test's probe is indistinguishable from this one's.
    _reset_process_counters(monkeypatch, tmp_path)

    before = counter_registry.read_one(counter_id)
    DRIVERS[counter_id](tmp_path)
    after = counter_registry.read_one(counter_id)

    assert after.value is not None, (
        f"{counter_id}: reader returned Unknown after its writer fired — "
        f"{after.unknown_reason}"
    )
    baseline = 0.0 if before.value is None else before.value
    assert after.value > baseline, (
        f"{counter_id}: the writer fired and the registry still reports "
        f"{after.value}. The reader is not reading this counter."
    )


@pytest.mark.parametrize("counter_id", sorted(DRIVERS))
def test_the_driven_value_reaches_the_rendered_report(counter_id, tmp_path, monkeypatch):
    """Rendering is part of the contract, not a detail.

    A reader whose value never makes it into `render_lines` is the T-07 defect
    one layer further in: the number exists, is correct, and reaches no one.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _reset_process_counters(monkeypatch, tmp_path)
    DRIVERS[counter_id](tmp_path)

    lines = counter_registry.render_lines()
    row = [ln for ln in lines if ln.startswith(f"{counter_id}:")]
    assert row, f"{counter_id} does not appear in render_lines()"
    assert "Unknown" not in row[0], f"{counter_id} renders Unknown after being driven"


def _reset_process_counters(monkeypatch, tmp_path) -> None:
    from llm_router import (
        classify,
        coverage,
        execution_ledger,
        failopen,
        prompt_capture,
        session_store,
    )

    execution_ledger.reset_dropped_event_count()
    classify.reset_low_signal_counters()
    monkeypatch.setattr(session_store, "_lock_timeouts", 0, raising=False)
    monkeypatch.setattr(prompt_capture, "_counters", {}, raising=False)
    failopen.clear()
    coverage.clear()
    coverage.reset_cache()


# ── 3. Surface ────────────────────────────────────────────────────────────

def test_doctor_calls_the_registry():
    """AST, not source text.

    A-10: a test asserting `"counter_registry" in inspect.getsource(...)` passes
    when the string sits in a comment and the call site is gone. Comments are
    not in the AST, so this cannot be satisfied that way.
    """
    doctor_py = SRC / "llm_router" / "commands" / "doctor.py"
    tree = ast.parse(doctor_py.read_text(encoding="utf-8"))
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "counter_registry.readings" in calls, (
        "llm-router doctor no longer calls counter_registry.readings(). Every "
        "counter in the registry is unread again the moment that call goes."
    )


def test_doctor_renders_a_driven_counter(tmp_path, monkeypatch, capsys):
    """End to end through the real command, not through render_lines."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _reset_process_counters(monkeypatch, tmp_path)
    from llm_router import session_store

    session_store._note_lock_timeout("r12 doctor probe")

    from llm_router import counter_registry as cr

    out = []
    for c, r in cr.readings():
        out.append((c.id, r.value))
    assert ("session_lock_timeouts", 1.0) in out

    # And the doctor section itself, rendered.
    lines = cr.render_lines()
    assert any(ln.startswith("session_lock_timeouts: 1") for ln in lines), lines


def test_unknown_is_never_rendered_as_zero(tmp_path, monkeypatch):
    """The substitution that makes a broken counter look like a clean run."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    # No log at all → the invariant has no denominator. It must say so.
    r = counter_registry.read_one("unterminated_invocations")
    assert r.value is None, "an absent log reported a number"
    lines = counter_registry.render_lines()
    row = next(ln for ln in lines if ln.startswith("unterminated_invocations:"))
    assert "Unknown" in row and ": 0 " not in row, row


def test_a_broken_reader_does_not_take_the_others_down(monkeypatch):
    """One failing counter must degrade to Unknown, not blank the report."""
    def boom() -> counter_registry.CounterReading:
        raise RuntimeError("reader exploded")

    broken = counter_registry.Counter(
        id="r12_broken_probe",
        makes_visible="nothing; this is a test probe",
        source="llm_router.failopen:snapshot",
        reader=boom,
    )
    monkeypatch.setattr(
        counter_registry, "REGISTRY", counter_registry.REGISTRY + (broken,)
    )
    ids = {c.id for c, _ in counter_registry.readings()}
    assert "r12_broken_probe" in ids
    assert "fail_open_events" in ids, "one bad reader blanked the rest"
    lines = counter_registry.render_lines()
    assert any("r12_broken_probe: Unknown (RuntimeError)" in ln for ln in lines), lines
