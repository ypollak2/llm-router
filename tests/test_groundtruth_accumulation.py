"""Ground Truth Accumulation Mode.

The failure this guards against is a task marked eligible when the state it
needs was never captured. So most tests below assert a REJECTION, and each has
a positive counterpart — a gate that rejects everything would pass a
rejection-only suite while accumulating nothing.

The regression cases at the end are real rows from the frozen seed-v2 corpus.
They are the categories that produced 121 rows and zero labels, and the gate's
job is to have caught them at capture time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from groundtruth import dataset as ds  # noqa: E402
from groundtruth import envelope as envmod  # noqa: E402
from groundtruth import pool as poolmod  # noqa: E402
from groundtruth.eligibility import (  # noqa: E402
    R_ENVELOPE_INCOMPLETE,
    R_EXTERNAL_STATE,
    R_MACHINE_STATE,
    R_PRIVACY,
    R_SESSION_STATE,
    R_SUBJECTIVE,
    R_TEMPLATE,
    V_NONE,
    assess,
    summarise,
)

CODE_TASK = ("Make the name_contains filter in src/query.py case-insensitive. "
             "The order of results must stay the order of the input rows.")


# ── Eligibility: state requirements ─────────────────────────────────────────


@pytest.fixture
def with_replayer(monkeypatch):
    """Pretend a replay runner exists, so repo-bound tasks are admissible.

    T-01/H-08. `eligibility.replay_available()` gates repo-bound tasks on whether
    anything can actually check out the captured commit; today nothing can, so
    they are correctly refused. Most tests in this file are not about that gate
    at all -- they exercise pool persistence, dedup and funnel counting, and only
    need SOME admissible task to work with.

    Patching the capability (rather than asserting the pre-gate contract) keeps
    those tests about their own subject, and means they start exercising the real
    path automatically on the day a replayer lands.

    Uses `monkeypatch` so the patch cannot leak: the hand-rolled version of this
    in tests/qa/ restored only one of the two channels it touched and silently
    masked eight failures here.
    """
    from groundtruth import eligibility as _el

    monkeypatch.setattr(_el, "replay_available", lambda: True)
    return _el

def test_session_state_task_is_rejected() -> None:
    e = assess("commit this and keep going with the rest of the plan")
    assert not e.ground_truth_candidate
    assert e.requires_session_state
    assert R_SESSION_STATE in e.ineligibility_reasons


def test_machine_state_task_is_rejected() -> None:
    e = assess("what models operate now in Ollama on this machine?")
    assert not e.ground_truth_candidate
    assert R_MACHINE_STATE in e.ineligibility_reasons


def test_external_state_without_evidence_is_rejected() -> None:
    e = assess("Summarise the changelog at https://example.com/releases and list breaking changes",
               has_external_evidence=False)
    assert not e.ground_truth_candidate
    assert R_EXTERNAL_STATE in e.ineligibility_reasons


def test_external_state_WITH_frozen_evidence_is_not_rejected_for_that_reason() -> None:
    """The positive counterpart: freezing evidence removes the blocker."""
    e = assess("Summarise the changelog at https://example.com/releases and list breaking changes",
               has_external_evidence=True)
    assert R_EXTERNAL_STATE not in e.ineligibility_reasons


def test_repo_task_without_captured_repo_state_is_rejected() -> None:
    e = assess(CODE_TASK, has_repo_state=False)
    assert e.requires_repo_state
    assert not e.ground_truth_candidate
    assert R_ENVELOPE_INCOMPLETE in e.ineligibility_reasons


def test_repo_task_with_captured_state_waits_for_a_replayer() -> None:
    """H-08. Captured state is necessary, not sufficient.

    Before H-08 this asserted that capturing repo state made a task a candidate.
    It does not: nothing in the tree can check out the captured commit, so
    admitting the task would fill the pool with work the harness can never grade
    while the funnel reported a healthy pipeline.

    The refusal reason is deliberately distinct from `replay-envelope-incomplete`
    -- that one sends an operator to fix capture, which would not help here.
    """
    e = assess(CODE_TASK, has_repo_state=True)
    assert e.verification_candidate, "the task is still mechanically checkable"
    assert e.ground_truth_candidate is False
    assert "no-replayer-for-required-state" in e.ineligibility_reasons

    # Worth recording: H-08's reason lands in `ineligibility_reasons`, which also
    # flips `replayable` to False. Arguably "replayable" should mean "the state
    # was captured" and a separate axis should mean "and something can run it" --
    # conflating them loses the distinction between a capture failure and a
    # missing runner, which is exactly the distinction R_NO_REPLAYER exists to
    # preserve. Asserted as-is rather than silently; see FIX_RUN.md F02.
    assert e.replayable is False


def test_repo_task_becomes_a_candidate_once_a_replayer_exists(with_replayer) -> None:
    """The other side of the same gate, so the refusal above cannot be permanent.

    If this ever fails, the gate has stopped being tied to the capability and has
    become a hard-coded no.
    """
    e = assess(CODE_TASK, has_repo_state=True)
    assert e.ground_truth_candidate, e.ineligibility_reasons
    assert e.verifier_class == ds.V_SANDBOX


def test_subjective_task_is_replayable_but_not_a_candidate() -> None:
    """Replay and verification are separate axes; this proves they are."""
    e = assess("Write me a vision document describing where this product should go",
               has_repo_state=True)
    assert e.subjective
    assert e.verifier_class == V_NONE
    assert not e.verification_candidate
    assert R_SUBJECTIVE in e.ineligibility_reasons


def test_template_automation_is_rejected() -> None:
    e = assess("Analyze this codebase for performance optimizations and list findings",
               duplicate_count=47)
    assert R_TEMPLATE in e.ineligibility_reasons


def test_unsafe_to_capture_is_rejected_rather_than_weakening_privacy() -> None:
    e = assess(CODE_TASK, has_repo_state=True, scrub_safe=False)
    assert not e.ground_truth_candidate
    assert R_PRIVACY in e.ineligibility_reasons


def test_incomplete_envelope_overrides_a_clean_assessment(with_replayer) -> None:
    """Content can look perfect and the state still be missing."""
    ok = assess(CODE_TASK, has_repo_state=True, envelope_complete=True)
    bad = assess(CODE_TASK, has_repo_state=True, envelope_complete=False)
    assert ok.ground_truth_candidate
    assert not bad.ground_truth_candidate
    assert R_ENVELOPE_INCOMPLETE in bad.ineligibility_reasons


def test_degenerate_prompt_is_rejected() -> None:
    assert not assess("ok go").ground_truth_candidate


# ── Verifier-candidate classification ───────────────────────────────────────

def test_factual_question_needs_a_reference_answer_and_is_not_high_confidence() -> None:
    e = assess("What is the capital of Portugal? Answer with just the city.")
    assert e.verifier_class == ds.V_MECHANICAL
    assert e.needs_reference_answer, (
        "nobody has established the right answer yet; admitting it as settled "
        "would be manufacturing certainty")


def test_speculative_question_gets_no_verifier() -> None:
    e = assess("Maybe gemini-3-flash-preview can act like the router with local models?")
    assert e.verifier_class == V_NONE


def test_first_person_question_gets_no_verifier() -> None:
    e = assess("Why can't you do a full scale evaluation locally on my machine?")
    assert e.verifier_class == V_NONE


# ── Funnel accounting ───────────────────────────────────────────────────────

def test_summarise_partitions_every_task() -> None:
    items = [assess(CODE_TASK, has_repo_state=True),
             assess("commit this and keep going"),
             assess("what models operate now in Ollama?"),
             assess("Write me a vision document about the future")]
    s = summarise(items)
    assert s["captured"] == 4
    assert s["candidates"] + sum(s["rejected"].values()) == 4


def test_every_rejection_carries_a_reason() -> None:
    for prompt in ("commit this and keep going",
                   "what models operate now in Ollama?",
                   "ok go", "Write me a vision document about the future"):
        e = assess(prompt)
        if not e.ground_truth_candidate:
            assert e.ineligibility_reasons, f"silent drop for {prompt!r}"


# ── Replay envelope ─────────────────────────────────────────────────────────

def test_envelope_reports_missing_repo_state() -> None:
    env = envmod.ReplayEnvelope(prompt="x", required_state=["repo"])
    complete, missing = env.completeness()
    assert not complete and "repo-commit" in missing


def test_envelope_reports_missing_external_evidence() -> None:
    env = envmod.ReplayEnvelope(prompt="x", required_state=["external-evidence"])
    complete, missing = env.completeness()
    assert not complete and "external-evidence" in missing


def test_envelope_with_frozen_evidence_is_complete() -> None:
    ev = envmod.freeze_external("https://example.com/x", "the frozen body")
    env = envmod.ReplayEnvelope(prompt="x", required_state=["external-evidence"],
                                external=[ev])
    assert env.completeness()[0]


def test_session_state_can_never_be_marked_complete() -> None:
    """Capturing a transcript does not make 'continue' well-defined."""
    env = envmod.ReplayEnvelope(prompt="x", required_state=["session"],
                                context_slice="lots of prior conversation")
    complete, missing = env.completeness()
    assert not complete
    assert "session-state-not-reconstructable" in missing


def test_envelope_without_prompt_is_incomplete() -> None:
    env = envmod.ReplayEnvelope(required_state=[])
    assert not env.completeness()[0]


def test_frozen_evidence_carries_provenance() -> None:
    ev = envmod.freeze_external("https://example.com/a", "hello world",
                                version="v1.2.3", media_type="text/plain")
    assert ev.source == "https://example.com/a"
    assert ev.version == "v1.2.3"
    assert ev.retrieved_at > 0
    assert len(ev.content_sha256) == 64


def test_frozen_evidence_is_scrubbed_and_hashed_after_scrubbing() -> None:
    secret = "token ghp_AbCdEf0123456789AbCdEf0123456789abcd here"
    ev = envmod.freeze_external("https://example.com/s", secret)
    assert ev.content_sha256 != envmod.sha256_text(secret), (
        "the hash must cover what was STORED, not the original")


def test_frozen_evidence_store_writes_scrubbed_bytes(tmp_path: Path) -> None:
    ev = envmod.freeze_external("https://example.com/s",
                                "key sk-proj-AbCdEf0123456789AbCdEf0123456789",
                                store_dir=tmp_path)
    body = (tmp_path / f"{ev.content_sha256[:32]}.txt").read_text()
    assert "sk-proj-AbCdEf" not in body


def test_repo_state_capture_on_this_repo() -> None:
    st = envmod.capture_repo_state(Path(__file__).resolve().parents[1])
    assert st is not None and st.commit
    assert st.lockfile_hashes, "expected at least one lockfile hash"


def test_dirty_tree_is_recorded_not_hidden() -> None:
    st = envmod.RepoState(commit="a" * 40, dirty=True, diff_sha256=None)
    assert not st.reconstructable
    clean = envmod.RepoState(commit="a" * 40, dirty=False)
    assert clean.reconstructable


def test_envelope_prompt_is_scrubbed() -> None:
    env = envmod.build(prompt="deploy with sk-proj-AbCdEf0123456789AbCdEf0123456789",
                       required_state=[])
    assert "sk-proj-AbCdEf" not in (env.prompt or "")


# ── Lifecycle ───────────────────────────────────────────────────────────────

def _cand(**over) -> poolmod.Candidate:
    d = dict(task_id="t1", prompt_sha256="s" * 16)
    d.update(over)
    return poolmod.Candidate(**d)


def test_lifecycle_forward_path() -> None:
    c = _cand()
    assert c.advance(poolmod.ELIGIBLE, "gate passed")
    assert c.advance(poolmod.READY_FOR_REPLAY, "envelope complete")
    assert c.advance(poolmod.VERIFIED, "replayed and graded")
    assert c.advance(poolmod.FROZEN, "sampled into GT v1")
    assert c.state == poolmod.FROZEN


def test_lifecycle_refuses_to_skip_states() -> None:
    c = _cand()
    assert not c.advance(poolmod.VERIFIED, "wishful thinking"), (
        "a candidate must not reach VERIFIED without being replay-ready")
    assert c.state == poolmod.CAPTURED


def test_lifecycle_records_a_reason_for_every_transition() -> None:
    c = _cand()
    c.advance(poolmod.ELIGIBLE, "gate passed")
    c.advance(poolmod.INELIGIBLE, "envelope went stale")
    assert [t.reason for t in c.history] == ["gate passed", "envelope went stale"]
    assert all(t.at > 0 for t in c.history)


def test_frozen_is_terminal() -> None:
    c = _cand(state=poolmod.FROZEN)
    assert not c.advance(poolmod.AMBIGUOUS, "no")


def test_failed_replay_can_retry() -> None:
    c = _cand(state=poolmod.REPLAY_FAILED)
    assert c.advance(poolmod.READY_FOR_REPLAY, "envelope repaired")


# ── Pool: persistence, dedup, stats ─────────────────────────────────────────

def _pool(tmp_path: Path) -> poolmod.Pool:
    return poolmod.Pool(tmp_path / "pool.jsonl", tmp_path / "funnel.jsonl")


def _make(prompt: str, **over):
    e = assess(prompt, has_repo_state=True, envelope_complete=True)
    return poolmod.make_candidate(
        task_id=over.pop("task_id", "gtc-" + str(abs(hash(prompt)))[:8]),
        prompt=prompt, prompt_sha256="h" * 16, eligibility=e,
        envelope={"prompt": prompt, "complete": True}, **over)


def test_candidate_persists_across_reload(tmp_path: Path, with_replayer) -> None:
    p = _pool(tmp_path)
    ok, _ = p.admit(_make(CODE_TASK), CODE_TASK)
    assert ok
    reloaded = _pool(tmp_path)
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0].state == poolmod.READY_FOR_REPLAY


def test_history_survives_a_reload(tmp_path: Path, with_replayer) -> None:
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK), CODE_TASK)
    c = _pool(tmp_path).all()[0]
    assert [t.to for t in c.history] == [poolmod.ELIGIBLE, poolmod.READY_FOR_REPLAY]


def test_exact_duplicate_is_rejected_with_attribution(tmp_path: Path) -> None:
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK, task_id="a"), CODE_TASK)
    ok, reason = p.admit(_make(CODE_TASK, task_id="b"), CODE_TASK)
    assert not ok and reason == poolmod.DUP_EXACT
    funnel = (tmp_path / "funnel.jsonl").read_text()
    assert poolmod.DUP_EXACT in funnel and "duplicate_of" in funnel


def test_near_duplicate_is_rejected(tmp_path: Path, with_replayer) -> None:
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK, task_id="a"), CODE_TASK)
    # Near, not exact: stemming already collapses inflection and punctuation,
    # so a genuine near-duplicate needs an actual wording difference that still
    # clears the 0.9 Jaccard threshold.
    variant = CODE_TASK.replace("The order of", "The final order of")
    ok, reason = p.admit(_make(variant, task_id="b"), variant)
    assert not ok and reason == poolmod.DUP_NEAR


def test_distinct_task_is_not_a_duplicate(tmp_path: Path, with_replayer) -> None:
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK, task_id="a"), CODE_TASK)
    other = ("Add a retry with exponential backoff to src/client.py for 5xx "
             "responses, leaving 4xx handling unchanged.")
    ok, _ = p.admit(_make(other, task_id="b"), other)
    assert ok


def test_duplicate_increments_the_original(tmp_path: Path) -> None:
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK, task_id="a"), CODE_TASK)
    p.admit(_make(CODE_TASK, task_id="b"), CODE_TASK)
    assert _pool(tmp_path)._index["a"].duplicate_count == 2


def test_ineligible_task_is_recorded_not_dropped(tmp_path: Path) -> None:
    p = _pool(tmp_path)
    prompt = "commit this and keep going"
    e = assess(prompt)
    c = poolmod.make_candidate(task_id="x", prompt=prompt, prompt_sha256="h" * 16,
                               eligibility=e, envelope={"prompt": prompt})
    ok, reason = p.admit(c, prompt)
    assert not ok
    assert reason == R_SESSION_STATE
    assert _pool(tmp_path)._index["x"].state == poolmod.INELIGIBLE, (
        "a rejected task stays in the pool as evidence")


def test_stats_counts_and_rejection_funnel(tmp_path: Path, with_replayer) -> None:
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK, task_id="a"), CODE_TASK)
    bad = "commit this and keep going"
    p.admit(poolmod.make_candidate(task_id="b", prompt=bad, prompt_sha256="h" * 16,
                                   eligibility=assess(bad), envelope={"prompt": bad}), bad)
    s = p.stats()
    assert s["captured"] == 2
    assert s["eligible"] == 1
    assert s["replay_ready"] == 1
    assert s["rejected"].get(R_SESSION_STATE) == 1


def test_reference_answer_candidate_is_not_high_confidence(tmp_path: Path) -> None:
    p = _pool(tmp_path)
    q = "What is the capital of Portugal? Answer with just the city."
    e = assess(q)
    c = poolmod.make_candidate(task_id="q", prompt=q, prompt_sha256="h" * 16,
                               eligibility=e, envelope={"prompt": q, "complete": True})
    p.admit(c, q)
    s = p.stats()
    assert s["eligible"] == 1
    assert s["high_confidence"] == 0, "a reference answer nobody established is not evidence"
    assert s["needs_human_review"] == 1


def test_coverage_report_renders(tmp_path: Path) -> None:
    from groundtruth.accumulate_report import render
    p = _pool(tmp_path)
    p.admit(_make(CODE_TASK), CODE_TASK)
    text = render(p)
    # The runtime funnel, in the order a task moves through it.
    for line in ("Captured:", "Eligible:", "Persisted:", "Rejected:",
                 "Deduplicated:", "Errors:", "Verifier-ready:"):
        assert line in text, f"report is missing the {line!r} row"
    assert "Readiness for Ground Truth v1" in text
    assert "VERDICT" in text


def test_report_flags_an_empty_pool_as_not_ready(tmp_path: Path) -> None:
    from groundtruth.accumulate_report import render
    assert "far too few to freeze" in render(_pool(tmp_path))


# ── No timestamp joins ──────────────────────────────────────────────────────

def test_nothing_in_accumulation_joins_on_time() -> None:
    """The pool keys on prompt hash and task id, never on proximity in time."""
    for mod in ("pool.py", "eligibility.py", "accumulate.py"):
        src = (Path(__file__).resolve().parents[1] / "scripts" / "groundtruth" / mod).read_text()
        for smell in ("nearest_ts", "closest_time", "within_seconds", "abs(ts", "ts_delta"):
            assert smell not in src, f"{mod} appears to join on time via {smell!r}"


# ── Part 14: regression against the historical failure categories ───────────

SEED_V2 = Path(__file__).resolve().parents[1] / "data/groundtruth/seed-v2/corpus.jsonl"


@pytest.mark.skipif(not SEED_V2.exists(), reason="seed-v2 not present")
def test_gate_rejects_the_historical_corpus() -> None:
    """121 rows produced zero trustworthy labels. The gate must agree.

    Two rows are admitted — both factual questions whose reference answer
    nobody has established — and both are flagged `needs_reference_answer`, so
    neither counts as high-confidence. That reproduces the hand-review verdict
    of zero, without hard-coding it.
    """
    rows = [json.loads(ln) for ln in SEED_V2.read_text().splitlines() if ln.strip()]
    results = [assess(r["text"], duplicate_count=r.get("duplicate_count", 1))
               for r in rows]
    high_confidence = [e for e in results
                       if e.ground_truth_candidate and not e.needs_reference_answer]
    assert high_confidence == [], (
        f"gate admitted {len(high_confidence)} historical rows as high-confidence; "
        "hand review found zero trustworthy")
    s = summarise(results)
    assert s["candidates"] + sum(s["rejected"].values()) == len(rows)


@pytest.mark.skipif(not SEED_V2.exists(), reason="seed-v2 not present")
def test_historical_rejection_reasons_match_the_known_categories() -> None:
    rows = [json.loads(ln) for ln in SEED_V2.read_text().splitlines() if ln.strip()]
    reasons = {r for e in (assess(x["text"], duplicate_count=x.get("duplicate_count", 1))
                           for x in rows)
               for r in e.ineligibility_reasons}
    for expected in (R_SESSION_STATE, R_EXTERNAL_STATE, R_SUBJECTIVE, R_MACHINE_STATE):
        assert expected in reasons, f"gate never produced {expected}"


def test_accumulate_never_raises_on_bad_input(tmp_path: Path) -> None:
    from groundtruth.accumulate import accumulate
    p = _pool(tmp_path)
    for bad in ("", None, "x", "ok"):
        ok, reason, _ = accumulate(bad or "", pool=p)
        assert ok is False and reason


# ── Dirty trees must stay replayable ────────────────────────────────────────
# Regression: storing only `diff_sha256` made every dirty-tree task
# unreconstructable, so a normal developer working tree rejected every coding
# task — the traffic this pool most needs.

def test_dirty_tree_with_stored_patch_is_reconstructable() -> None:
    st = envmod.RepoState(commit="a" * 40, dirty=True,
                          diff_sha256="h" * 64, diff="--- a/x\n+++ b/x\n")
    assert st.reconstructable


def test_dirty_tree_with_only_a_hash_is_not_reconstructable() -> None:
    st = envmod.RepoState(commit="a" * 40, dirty=True, diff_sha256="h" * 64)
    assert not st.reconstructable, "a hash identifies a patch, it does not produce one"


def test_oversized_diff_is_marked_not_reconstructable() -> None:
    st = envmod.RepoState(commit="a" * 40, dirty=True, diff_sha256="h" * 64,
                          diff=None, diff_truncated=True)
    assert not st.reconstructable


def test_captured_diff_is_scrubbed(tmp_path: Path) -> None:
    """A secret in an uncommitted file must not reach the envelope."""
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    (repo / "a.txt").write_text("clean\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, env=env, check=True)
    (repo / "a.txt").write_text("key sk-proj-AbCdEf0123456789AbCdEf0123456789\n")
    st = envmod.capture_repo_state(repo)
    assert st is not None and st.dirty and st.diff
    assert "sk-proj-AbCdEf" not in st.diff
    assert st.reconstructable


def test_task_ids_are_content_derived_and_distinct() -> None:
    from groundtruth.accumulate import _task_id
    a = _task_id("same", "same", "Make the filter case-insensitive in src/a.py")
    b = _task_id("same", "same", "Add a retry to src/b.py for 5xx responses")
    assert a != b, "identical caller-supplied hashes must not collapse two tasks"
    assert a == _task_id("other", "other", "Make the filter case-insensitive in src/a.py")


# ── Tool-state detection must not fire on ordinary English ──────────────────
# Regression: a substring check matched "make " in "Make the filter
# case-insensitive", so that task demanded tool state and was rejected, while
# "Add a retry to src/client.py" — the same shape — was admitted.

@pytest.mark.parametrize("prompt", [
    "Make the name_contains filter in src/query.py case-insensitive.",
    "Build a summary of the changes in src/app.py",
    "Run the numbers again for src/report.py",
])
def test_english_verbs_do_not_imply_tool_state(prompt: str) -> None:
    assert not assess(prompt, has_repo_state=True).requires_tool_state, (
        "an imperative verb is not a command invocation")


@pytest.mark.parametrize("prompt", [
    "Fix the parser in src/p.py and run the tests",
    "Update src/a.py then run `pytest tests/`",
    "Change src/b.py so that make test passes",
    "Fix src/c.py; npm run lint must stay clean",
])
def test_real_command_invocation_implies_tool_state(prompt: str) -> None:
    assert assess(prompt, has_repo_state=True).requires_tool_state


def test_same_shaped_code_tasks_get_the_same_verdict(with_replayer) -> None:
    """The bug this guards: two equivalent tasks split by their opening verb."""
    a = assess("Make the name_contains filter in src/query.py case-insensitive.",
               has_repo_state=True, envelope_complete=True)
    b = assess("Add a retry with exponential backoff to src/client.py for 5xx.",
               has_repo_state=True, envelope_complete=True)
    assert a.ground_truth_candidate == b.ground_truth_candidate is True
    assert a.verifier_class == b.verifier_class


# ── Runtime wiring: one flag, four observable outcomes ──────────────────────

def test_one_flag_enables_the_whole_path(monkeypatch) -> None:
    from llm_router import prompt_capture as pc
    for name in (pc.ENV_FLAG, pc.ENV_FLAG_LEGACY, pc.ENV_NO_ACCUMULATE):
        monkeypatch.delenv(name, raising=False)
    assert not pc.enabled() and not pc.accumulation_enabled()
    monkeypatch.setenv(pc.ENV_FLAG, "1")
    assert pc.enabled() and pc.accumulation_enabled(), (
        "one flag must turn on capture AND accumulation")


def test_legacy_flag_still_works(monkeypatch) -> None:
    from llm_router import prompt_capture as pc
    monkeypatch.delenv(pc.ENV_FLAG, raising=False)
    monkeypatch.setenv(pc.ENV_FLAG_LEGACY, "1")
    assert pc.enabled(), "the retired name must not silently stop working"


def test_accumulation_can_be_suppressed_without_a_second_switch(monkeypatch) -> None:
    from llm_router import prompt_capture as pc
    monkeypatch.setenv(pc.ENV_FLAG, "1")
    monkeypatch.setenv(pc.ENV_NO_ACCUMULATE, "1")
    assert pc.enabled() and not pc.accumulation_enabled()


def test_capture_records_an_accumulation_outcome(tmp_path: Path, monkeypatch) -> None:
    """The four outcomes must be distinguishable — no silent failure."""
    from llm_router import prompt_capture as pc
    monkeypatch.setenv(pc.ENV_FLAG, "1")
    monkeypatch.delenv(pc.ENV_NO_ACCUMULATE, raising=False)
    monkeypatch.setenv(pc.ENV_PATH, str(tmp_path / "cap.jsonl"))
    monkeypatch.setenv(pc.ENV_OUTCOME_LOG, str(tmp_path / "out.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    # Pretend to be production: under pytest every run is synthetic, and a
    # synthetic run skips accumulation entirely, so the rejection path below
    # would never be reached.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SYNTHETIC", raising=False)

    assert pc.capture("commit this and keep going", route_id="r1", task_type="code")
    rows = [json.loads(x) for x in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert rows, "an accumulation attempt must leave a trace"
    assert rows[-1]["outcome"] == pc.OUTCOME_REJECTED
    assert rows[-1]["reason"], "a rejection must carry its reason"
    assert rows[-1]["route_id"] == "r1"


def test_outcome_states_are_distinct() -> None:
    from llm_router import prompt_capture as pc
    states = {pc.OUTCOME_PERSISTED, pc.OUTCOME_REJECTED,
              pc.OUTCOME_DEDUPED, pc.OUTCOME_ERROR, pc.OUTCOME_SKIPPED}
    assert len(states) == 5, "each outcome needs its own value"


def test_status_exposes_the_outcome_log(monkeypatch, tmp_path: Path) -> None:
    from llm_router import prompt_capture as pc
    monkeypatch.setenv(pc.ENV_OUTCOME_LOG, str(tmp_path / "o.jsonl"))
    st = pc.status()
    assert "outcome_log" in st and "outcome_counters" in st


def test_synthetic_run_never_reaches_the_pool(tmp_path: Path, monkeypatch) -> None:
    """A benchmark must not contribute candidates.

    The pool is what a future Ground Truth v1 is sampled from, so a fixture is
    kept out at the door rather than filtered later.
    """
    from llm_router import prompt_capture as pc
    monkeypatch.setenv(pc.ENV_FLAG, "1")
    monkeypatch.setenv(pc.ENV_PATH, str(tmp_path / "cap.jsonl"))
    monkeypatch.setenv(pc.ENV_OUTCOME_LOG, str(tmp_path / "out.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_SYNTHETIC", "1")

    assert pc.capture("Make the filter in src/query.py case-insensitive.",
                      route_id="r-synth", task_type="code")
    rows = [json.loads(x) for x in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert rows[-1]["outcome"] == pc.OUTCOME_SKIPPED
    assert rows[-1]["reason"] == "synthetic-run"
    assert not (tmp_path / "ground_truth_candidates.jsonl").exists(), (
        "a synthetic run must create no candidates at all")


def test_provenance_marker_is_explicit_not_inferred() -> None:
    """No model name, session id or token count decides provenance."""
    import inspect

    from llm_router import routing_quality as rq
    src = inspect.getsource(rq.detect_synthetic)
    for smell in ("final_model", "session_id", "mock", "test/", "prompt_tokens"):
        assert smell not in src, (
            f"detect_synthetic() inspects {smell!r} — that is an inference, "
            "and every such inference has needed revising")
