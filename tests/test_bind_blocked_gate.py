"""S3 (BIND W1): the blocked-answer gate, ``agent-route.py::_blocked_marker``, on invented answers and minimal pairs.

The independent review of PR #818 measured the first push's gate: on 16 invented reports in the wording of a WRITABLE,
no-network run it missed 10, and of the 28 real answers it flagged only through its weakest markers 5 were complete
deliverables (docs/bugs/BIND-2.md has the before and after on the review's labelled real sample, which is private and not
in this repo). These tests hold what can be public: the 26 invented answers the review wrote, four more rounds written
after it (``tests/_bind_gate_samples.py``), and one minimal pair per rule, so a rule cannot be dropped or widened without a
named test going red.

Rules, each with its pair below:

* the "not done" word must come from OUTSIDE the environment phrase it is paired with ("permission denied",
  "connection refused", "approvals are disabled" are mentions in prose about code, not reports);
* weak labels ("this session", "api.github.com", "is read-only") need the reply's own voice, a first person or a command it
  ran, so a finding about the code under review passes;
* a reply that could not SAVE its report file but carries the report is complete, and only when it has a body;
* an environment fact alone, or "I did not run the tests" with no cause named, is not enough.
"""

from __future__ import annotations

import importlib.util
import os
import re
import time

import pytest

from tests import _bind_gate_samples as G
from tests._bind_support import AGENT_ROUTE

_SETS = ("review", "r1", "r2", "r3", "r4")
BLOCKED = {f"{n}/{k}": t for n in _SETS for k, t in getattr(G, f"{n.upper()}_BLOCKED").items()}
COMPLETE = {f"{n}/{k}": t for n in _SETS for k, t in getattr(G, f"{n.upper()}_COMPLETE").items()}


@pytest.fixture(scope="module")
def mark():
    spec = importlib.util.spec_from_file_location("agent_route_gate_unit", AGENT_ROUTE)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod._blocked_marker


def test_the_fixture_sets_are_not_empty_and_the_gaps_name_real_samples():
    assert len(BLOCKED) == 96 and len(COMPLETE) == 70  # an empty set passes everything
    assert set(G.KNOWN_GAPS) <= set(BLOCKED), set(G.KNOWN_GAPS) - set(BLOCKED)


@pytest.mark.parametrize("sid", sorted(COMPLETE))
def test_a_complete_answer_that_uses_the_trigger_words_about_code_is_bound(mark, sid):
    assert mark(COMPLETE[sid]) is None, COMPLETE[sid]


@pytest.mark.parametrize("sid", sorted(set(BLOCKED) - set(G.KNOWN_GAPS)))
def test_a_report_of_work_the_environment_stopped_is_flagged_with_a_label(mark, sid):
    label = mark(BLOCKED[sid])
    assert label is not None, BLOCKED[sid]
    assert re.fullmatch(r"[a-z_]+", label)  # a label, never a piece of the answer


@pytest.mark.parametrize("sid", sorted(G.KNOWN_GAPS))
def test_a_documented_gap_is_still_a_gap(mark, sid):
    """A ratchet in the improving direction: when the gate learns one of these, this fails and the id has to come out of
    ``KNOWN_GAPS`` (and the numbers in docs/bugs/BIND-2.md be re-measured)."""
    assert mark(BLOCKED[sid]) is None, f"{sid} is flagged now: remove it from KNOWN_GAPS ({G.KNOWN_GAPS[sid]})"


# ----------------------------------------------------------------------------------------- one minimal pair per rule

_PAIRS = [
    # (rule, text that must be flagged, text that must pass)
    ("not-done word outside the phrase: permission denied",
     "I could not create the file: permission denied.",
     "The loader raises a clear error on 'permission denied' and retries once, then fails closed (loader.py:77)."),
    ("not-done word outside the phrase: connection refused",
     "I could not push: connection refused by the remote, so the branch is local only.",
     "The CLI reports 'connection refused' with exit code 7 when the daemon is down (cli.py:88)."),
    ("not-done word outside the phrase: approvals",
     "Approvals are disabled in this run, so I skipped the integration tests.",
     "Approval prompts are suppressed only when CI=1 is set; otherwise approvals are disabled for non-interactive jobs."),
    ("agent voice: this session",
     "I cannot push from this sandbox.",
     "This session id cannot be reused after logout: SessionStore.close() deletes the row (store.py:140)."),
    ("agent voice: opens on the inability",
     "Unable to push from this sandbox.",
     "The classifier in this session cannot distinguish 'fix' from 'fixes' (classifier.py:44)."),
    ("agent voice: a command the reply ran",
     "`gh pr view 812` failed: lookup api.github.com: no such host.",
     "The CLI resolves api.github.com once and caches it, so it cannot fail on a second lookup (net.py:90)."),
    ("agent voice: cannot reach",
     "I could not reach the registry, so nothing was installed.",
     "Finding: the client cannot connect to the gateway when TLS is disabled (client.py:88)."),
    ("agent voice: read-only state",
     "I could not commit: the config file is read-only, so the commit step was skipped.",
     "The cache is read-only by design and cannot be mutated from the pager."),
    ("a refusal by the sandbox is a report on its own",
     "The sandbox denied worktree creation and fetch.",
     "The sandbox profile is documented in sandbox.md; it covers the child process."),
    ("a tool's own error line",
     "mkdir: /tmp/work: Operation not permitted",
     "mkdir fails with Operation not permitted when the parent is read-only, which the test asserts (fs.py:40)."),
    ("verification left incomplete",
     "**FAIL: static findings; required runtime verification blocked.**",
     "Runtime verification passed on the staging host; no checks were skipped."),
    ("a command that failed because of the environment",
     "`git fetch origin` failed because the sandbox cannot write .git/FETCH_HEAD.",
     "The job failed because the session expired after 15 minutes (ci.yml:30)."),
    ("the run itself cannot",
     "This run cannot modify files outside the sandbox, and the target path is outside it.",
     "This environment variable cannot be empty: the loader raises ValueError (config.py:20)."),
    ("not permitted in the reply's own voice",
     "I was not permitted to run the code, so every runtime claim below is unverified.",
     "Users are not permitted to delete their own account; the handler returns 403 (accounts.py:60)."),
]


@pytest.mark.parametrize("rule, flagged, passed", _PAIRS, ids=[p[0] for p in _PAIRS])
def test_each_rule_has_a_pair_the_gate_tells_apart(mark, rule, flagged, passed):
    assert mark(flagged) is not None, f"{rule}: this must be flagged: {flagged}"
    assert mark(passed) is None, f"{rule}: this must pass: {passed}"


def test_a_reply_that_could_not_save_its_report_file_but_carries_the_report_is_complete(mark):
    sentence = "I could not save `docs/reports/run-sheet.md`: this session permits filesystem reads only."
    body = "Findings.\n" + "\n".join(f"- finding {i}: the cache key ignores the tenant (cache.py:{i})." for i in range(1, 30))
    assert len(body) > 800
    assert mark(f"{sentence}\n{body}") is None  # the report is in the reply: only its file was not written
    assert mark(sentence) is not None  # no body: a bare "could not write" is a blocked run
    two_things = sentence.replace(".md`:", ".md` and could not run the tests:")
    assert mark(f"{two_things}\n{body}") is not None  # a second thing was not done
    not_a_report = "I could not edit `src/router.py`: this session permits filesystem reads only."
    assert mark(f"{not_a_report}\n{body}") is not None  # the file is the work, not a report of it


def test_an_environment_fact_alone_and_an_unexplained_not_run_are_not_flagged(mark):
    assert mark("Network access is off by default in the sandbox profile (sandbox.py:50).") is None
    assert mark("I implemented the change in `pager.py` and added a test, but I did not run the tests.") is None


@pytest.mark.timing  # asserts on a perf_counter reading: runs in the serial timing lane (tests/test_timing_lane.py)
def test_the_gate_is_fast_on_hostile_input(mark):
    """Windows are the head 1500 and tail 1000 characters, but a sentence can be all of one: no pattern may backtrack."""
    hostile = ["I " * 1500, "`" * 1500, "no " * 600, "a" * 5000, "I could not " * 300, "sandbox " * 400,
               "Operation not permitted: " * 100, "\n".join(["- item"] * 600), "." * 4000,
               "I " + "x " * 1000 + "cannot reach"]
    for text in hostile:
        t0 = time.perf_counter()
        mark(text)
        assert time.perf_counter() - t0 < 0.5, text[:40]
