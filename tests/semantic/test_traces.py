"""A result you cannot attribute to a treatment is not a result.

The arms exist to answer "did this help". That question needs three things at
once, and the layer currently keeps none of them past the end of a call:

  * WHICH TREATMENT ran — the arm, and the switch positions it implied
  * WHAT IT SAW — the exact evidence selected, and what was dropped and why
  * WHAT STATE it ran against — the code snapshot AND the memory snapshot,
    which move independently

An independent review of this branch put it plainly: retrieval and intervention
traces are computed in memory and discarded, which is fine for an MVP and fatal
for the evaluation track, because offline replay of an outcome is only valid for
rows generated under the evidence conditions being compared. Without a trace you
can re-run an arm, but you cannot say the arm is why the number moved.

Two traces, because two different things are being claimed:

  RetrievalTrace     what the system offered, and what it could not find
  InterventionTrace  what was then actually DONE, and what nobody observed

The second matters more than it looks. "The agent was shown the lesson" is not
"the lesson prevented the bug"; an intervention log that cannot distinguish
"check ran and passed" from "check was never run" will quietly report prevention
coverage it never had. `unobserved_steps` is the field that stops that.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.semantic import experience as exp
from llm_router.semantic import indexer as ix
from llm_router.semantic import pack as spack
from llm_router.semantic import traces as tr


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t.t",
                    "-c", "user.name=t", "commit", "-qm", "seed"], check=True,
                   capture_output=True, timeout=30)
    return path


@pytest.fixture
def project(tmp_path: Path):
    repo = _repo(tmp_path / "repo", {
        "ledger.py": "def post_entry(amount):\n    return amount\n",
    })
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    store = exp.ExperienceStore(tmp_path / "experience")
    store.put(exp.Lesson(
        lesson_id="ledger-001",
        statement="post_entry must not run twice for one line.",
        affected_paths=["ledger.py"], affected_symbols=["post_entry"],
        known_from="2026-09-18",
        check_refs=["tests/test_ledger.py"],
        enforcement=exp.Enforcement.ADOPTED_RULE,
    ))
    return repo, base, store


# ── retrieval traces ─────────────────────────────────────────────────────────

def test_building_a_pack_records_what_it_offered(project, tmp_path):
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")

    pack = spack.build("fix post_entry in ledger.py", root=repo, base=base,
                       experience=store)
    trace_id = sink.record_retrieval(pack, query="fix post_entry in ledger.py",
                                     arm="M1")

    got = sink.get_retrieval(trace_id)
    assert got is not None
    assert got.arm == "M1"
    assert got.snapshot_id == pack.snapshot_id
    assert got.memory_snapshot_id == pack.memory_snapshot_id
    assert got.scope_id == pack.scope_id
    assert got.retrieval_status == pack.retrieval_status
    assert got.budget_tokens == pack.budget_tokens


def test_the_trace_names_the_exact_evidence_not_just_a_count(project, tmp_path):
    """A count cannot be replayed. Ids and hashes can."""
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")

    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    got = sink.get_retrieval(sink.record_retrieval(pack, query="post_entry"))

    assert got.selected_evidence, "no evidence identified in the trace"
    for item in got.selected_evidence:
        assert item["path"] and item["source_hash"], (
            "evidence recorded without the hash it was read at, so a later "
            "reader cannot tell whether it still describes the same bytes"
        )
    assert {i["path"] for i in got.selected_evidence} == \
        {e["path"] for e in pack.evidence}


def test_the_trace_records_lessons_and_the_conflicts_among_them(project, tmp_path):
    repo, base, store = project
    store.put(exp.Lesson(
        lesson_id="ledger-002", statement="Batch the posts.",
        affected_paths=["ledger.py"], affected_symbols=["post_entry"],
        known_from="2026-09-18", contradicts=["ledger-001"],
    ))
    sink = tr.TraceStore(tmp_path / "traces")

    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    got = sink.get_retrieval(sink.record_retrieval(pack, query="post_entry"))

    assert set(got.applicable_lessons) == {"ledger-001", "ledger-002"}
    assert got.unresolved_conflicts, (
        "a live disagreement was offered to the model and the trace does not "
        "record that it was unresolved at the time"
    )


def test_omissions_are_recorded_so_a_thin_pack_is_explicable(project, tmp_path):
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")

    pack = spack.build("post_entry", root=repo, base=base, experience=store,
                       budget_tokens=1)
    got = sink.get_retrieval(sink.record_retrieval(pack, query="post_entry"))

    assert got.omissions, (
        "everything was cut for budget and the trace shows an empty pack with "
        "no reason, which reads as 'the repo had nothing to say'"
    )


def test_two_identical_runs_produce_identical_traces(project, tmp_path):
    """Retrieval must not drift, or a moved number is unattributable.

    If the same query at the same code and memory snapshot can select different
    evidence, then a difference between two arms might be retrieval noise rather
    than the treatment. This is the property that makes the comparison mean
    anything.
    """
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")

    a = sink.get_retrieval(sink.record_retrieval(
        spack.build("post_entry", root=repo, base=base, experience=store),
        query="post_entry", arm="C"))
    b = sink.get_retrieval(sink.record_retrieval(
        spack.build("post_entry", root=repo, base=base, experience=store),
        query="post_entry", arm="C"))

    assert a.replay_key() == b.replay_key(), (
        "the same query at the same snapshots selected different evidence"
    )


def test_a_changed_memory_snapshot_changes_the_replay_key(project, tmp_path):
    """And it must not be mistaken for a code change."""
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")

    before = sink.get_retrieval(sink.record_retrieval(
        spack.build("post_entry", root=repo, base=base, experience=store),
        query="post_entry", arm="M1"))

    store.put(exp.Lesson(
        lesson_id="ledger-003", statement="Another thing entirely.",
        affected_paths=["ledger.py"], affected_symbols=["post_entry"],
        known_from="2026-09-18",
    ))
    after = sink.get_retrieval(sink.record_retrieval(
        spack.build("post_entry", root=repo, base=base, experience=store),
        query="post_entry", arm="M1"))

    assert before.snapshot_id == after.snapshot_id, "the code did not change"
    assert before.memory_snapshot_id != after.memory_snapshot_id
    assert before.replay_key() != after.replay_key()


# ── intervention traces ──────────────────────────────────────────────────────

def test_an_intervention_records_what_was_done_not_what_was_suggested(project,
                                                                     tmp_path):
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")
    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    rid = sink.record_retrieval(pack, query="post_entry", arm="M2")

    iid = sink.record_intervention(
        retrieval_id=rid,
        task_id="task-1",
        action_taken="edited ledger.py to guard the double post",
        checks_run=["tests/test_ledger.py"],
        outcomes={"tests/test_ledger.py": "passed"},
        unobserved_steps=["the host may have edited files we never saw"],
    )

    got = sink.get_intervention(iid)
    assert got.retrieval_id == rid
    assert got.checks_run == ["tests/test_ledger.py"]
    assert got.outcomes == {"tests/test_ledger.py": "passed"}


def test_a_check_that_was_never_run_is_not_coverage(project, tmp_path):
    """The distinction that decides whether 'prevention' means anything.

    A lesson suggested a check. If nothing ran it, the task has NO prevention
    coverage from that lesson — and an intervention log that cannot say so will
    report coverage it never had.
    """
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")
    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    rid = sink.record_retrieval(pack, query="post_entry", arm="M2")

    assert pack.suggested_checks, "the fixture lesson should suggest a check"

    iid = sink.record_intervention(
        retrieval_id=rid, task_id="task-2",
        action_taken="edited the file", checks_run=[], outcomes={},
        unobserved_steps=[],
    )

    got = sink.get_intervention(iid)
    assert got.prevention_coverage(pack.suggested_checks) == "unavailable", (
        "a task where the suggested check never ran reported prevention "
        "coverage anyway"
    )


def test_partial_coverage_is_partial_not_observed(project, tmp_path):
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")
    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    rid = sink.record_retrieval(pack, query="post_entry", arm="M2")

    iid = sink.record_intervention(
        retrieval_id=rid, task_id="task-3", action_taken="edited",
        checks_run=["tests/test_ledger.py"],
        outcomes={"tests/test_ledger.py": "passed"}, unobserved_steps=[],
    )
    got = sink.get_intervention(iid)

    assert got.prevention_coverage(["tests/test_ledger.py"]) == "observed"
    assert got.prevention_coverage(
        ["tests/test_ledger.py", "tests/test_other.py"]) == "partial"


def test_an_unknown_check_outcome_gives_no_prevention_evidence(project, tmp_path):
    """Same rule the library learned: unobserved is not success.

    This test first asserted `partial`, on the reasoning that running a check
    is worth something even if nobody watched it. That is wrong, and the
    implementation was right. The question `prevention_coverage` answers is
    "can this task's outcome be used as evidence that a known failure was
    prevented" — and a check whose result nobody observed answers it exactly as
    well as a check that never ran, which is not at all.

    The two cases stay distinguishable where it matters: `checks_run` records
    that an attempt was made, so you can still tell "we never tried" from "we
    tried and lost the result" when diagnosing why coverage is missing.
    """
    repo, base, store = project
    sink = tr.TraceStore(tmp_path / "traces")
    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    rid = sink.record_retrieval(pack, query="post_entry", arm="M2")

    iid = sink.record_intervention(
        retrieval_id=rid, task_id="task-4", action_taken="edited",
        checks_run=["tests/test_ledger.py"],
        outcomes={"tests/test_ledger.py": "unknown"}, unobserved_steps=[],
    )
    got = sink.get_intervention(iid)
    assert got.prevention_coverage(["tests/test_ledger.py"]) == "unavailable", (
        "a check whose result nobody observed was counted as prevention"
    )
    assert got.checks_run == ["tests/test_ledger.py"], (
        "the attempt itself must stay on the record, or 'we never tried' and "
        "'we tried and lost the result' become the same entry"
    )


# ── scoping and durability ───────────────────────────────────────────────────

def test_traces_from_two_projects_do_not_mix(tmp_path):
    a = _repo(tmp_path / "a", {"m.py": "def only_a():\n    pass\n"})
    b = _repo(tmp_path / "b", {"m.py": "def only_b():\n    pass\n"})
    base = tmp_path / "store"
    ix.index(root=a, base=base)
    ix.index(root=b, base=base)
    sink = tr.TraceStore(tmp_path / "traces")

    sink.record_retrieval(spack.build("only_a", root=a, base=base), query="only_a")
    sink.record_retrieval(spack.build("only_b", root=b, base=base), query="only_b")

    from llm_router.semantic.scope import scope_key
    a_traces = sink.for_scope(scope_key(a))
    assert len(a_traces) == 1
    assert a_traces[0].query == "only_a"


def test_traces_survive_a_reopen(project, tmp_path):
    repo, base, store = project
    root = tmp_path / "traces"
    pack = spack.build("post_entry", root=repo, base=base, experience=store)
    rid = tr.TraceStore(root).record_retrieval(pack, query="post_entry", arm="C")

    assert tr.TraceStore(root).get_retrieval(rid).arm == "C"


# ── the baseline arm needs a row too ─────────────────────────────────────────

def test_the_baseline_arm_is_recorded_even_though_it_retrieves_nothing(
    project, tmp_path, monkeypatch
):
    """Arm B is the thing every other arm is compared AGAINST.

    B has all three switches off, so `apply` returns early without building a
    pack — correct, that is what B means. But the early return also skipped the
    trace, which leaves the baseline as the one arm with no rows. You cannot
    compare D against an absent denominator, and the absence looks exactly like
    a run nobody performed.
    """
    from llm_router.semantic import modes

    repo, base, store = project
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "B")

    result = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)

    assert result.arm == "B"
    assert result.pack is None, "arm B is the no-layer baseline"
    assert result.trace_id is not None, (
        "the baseline arm produced no trace, so nothing can be compared to it"
    )

    trace = modes._trace_store(repo).get_retrieval(result.trace_id)
    assert trace.arm == "B"
    assert trace.selected_evidence == []
    assert trace.retrieval_status in ("off", "empty")
    assert trace.snapshot_id, "even a no-op arm has to say which code it ran on"


def test_ordinary_use_with_no_arm_writes_no_traces(project, tmp_path, monkeypatch):
    """Normal work is not an experiment and must not fill the evaluation store."""
    from llm_router.semantic import modes

    repo, base, store = project
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home2"))
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "on")

    result = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)

    assert result.pack is not None, "source=on should still retrieve"
    assert result.trace_id is None, (
        "a normal call recorded an evaluation trace; the store fills with rows "
        "nobody will compare and the real ones get harder to find"
    )
