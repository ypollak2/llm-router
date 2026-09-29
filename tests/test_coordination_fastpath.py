"""Coordination fast-path tests.

Pins that multi-agent orchestration prompts ("coordinate three agents
to build, test, and deploy") classify as ``task_type=coordinate`` and
that the bucket is ADVISORY-ONLY: direct execution must never fire for
them, in any mode. A stateless direct model has no subagents — a
pre-generated answer would fabricate parallel work that never happened.

False-positive guardrails: an orchestration verb alone ("spawn a
background process") or an agent noun alone ("what is a subagent?")
MUST still route normally — only the verb+target pairing triggers.

All example prompts below are hand-written paraphrases, not verbatim
copies of any golden-set or captured-traffic record (routing-golden-v1's
constructed_cases.jsonl and this project's own scrubbed real-prompt
corpus both exist for MEASUREMENT, not for pinning literal strings in
tests — a test built from the exact eval text would pass by memorizing
the eval, not by generalizing to it. See the 2026-09-28 remediation:
patterns first tuned against 10 golden-set examples scored 0/63 recall
on a held-out sample of real short prompts).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def auto_route():
    """Dynamic-import the hook script (not an importable module path)."""
    spec = importlib.util.spec_from_file_location(
        "_auto_route_coord_under_test",
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── True positives — orchestration prompts classify as coordinate ────────


@pytest.mark.parametrize("prompt", [
    "Coordinate three agents to build, test, and deploy the service",
    "orchestrate a swarm of workers to crawl these endpoints",
    "delegate the refactor to two subagents and merge their diffs",
    "spawn four agents and fan out the migration work",
    "parallelize this across worker agents",
    "dispatch specialists for research, coding, and review",
    "split the work between three sub-agents",
    "divide the tasks among your assistants",
])
def test_coordination_prompts_classify(auto_route, prompt):
    """Every prompt must hit the coordination fast-path."""
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    assert result is not None, f"coordination prompt missed classifier: {prompt!r}"
    assert result["task_type"] == "coordinate", (
        f"expected coordinate, got {result['task_type']!r} for {prompt!r}"
    )
    assert result["method"] == "coordination-fast-path"


# ── False positives — single-signal prompts must route normally ──────────


@pytest.mark.parametrize("prompt", [
    # Verb without agent target — process/code semantics
    "spawn a background process to tail the log",
    "parallelize this loop with multiprocessing",
    "dispatch the event to the handler",
    "split the work into two functions",
    # Agent target without orchestration verb — knowledge questions
    "what is a subagent?",
    "explain how AI agents use tools",
    "write a bio for a real estate agent",
    "how many workers does gunicorn need?",
])
def test_non_coordination_prompts_route_normally(auto_route, prompt):
    """Single-signal prompts must NOT trigger the fast-path."""
    assert not auto_route._is_coordination_task(prompt), (
        f"false positive — should NOT be coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


# ── Session-state / recent-history / continuation / cross-system / ambient ──
#
# routing-golden-v1 (2026-09-28): the verb+target pair above only catches
# literal "spawn agents"-style prompts. 11 of 12 constructed `coordinate`
# cases in that probe were routed to a local model and answered anyway,
# because none of them mention "agents" — they were cross-system,
# session-referential, or continuation-anaphora shaped instead.


@pytest.mark.parametrize("prompt", [
    # Cross-system orchestration (no "agents" mentioned at all)
    "Coordinate a rollout across the payments service, the notifications "
    "service, and the mobile repo to retire the old auth token format, "
    "tracking progress in a shared doc.",
    # Continuation anaphora — "the same X" + "the rest/other"
    "Pick up from where we left off and apply the same fix to the "
    "remaining two files.",
    "Go ahead and make the same change across the rest of the modules.",
    # Recent-history reference — "the last N PRs/commits/edits"
    "Combine the changes from my last four pull requests and resolve any "
    "merge conflicts.",
    "Roll back to the state before my last edit and reapply only the "
    "caching fix.",
    "Revert my last two commits but keep the config file changes.",
    # Multi-agent, broadened verbs ("spin up")
    "Spin up three sub-agents, have them split the test-writing work, and "
    "combine their results.",
    # Session/conversation-state reference
    "Gather everything we discussed earlier in this conversation and turn "
    "it into one design doc.",
    "Ship the config change we agreed on to every region, one region at "
    "a time, and report back after each.",
    "Compare this session's approach with what last week's session did, "
    "and pick the stronger one.",
    # Ambient status / bare continuation checks
    "what's the status on this?",
    "carry on",
    "check on the running agent",
])
def test_broadened_coordination_shapes_classify(auto_route, prompt):
    """These previously fell through to the ordinary category scorers and
    were routed to a local model (the golden-probe defect). They must now
    hit the coordination fast-path exactly like the multi-agent prompts."""
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    assert result is not None, f"coordination prompt missed classifier: {prompt!r}"
    assert result["task_type"] == "coordinate", (
        f"expected coordinate, got {result['task_type']!r} for {prompt!r}"
    )
    assert result["method"] == "coordination-fast-path"


# ── Precision guardrails for the broadened shapes — ordinary prompts must ──
# ── NOT become coordinate just because they share a surface word.        ──


@pytest.mark.parametrize("prompt", [
    # Contains "session"/"conversation" as an ordinary code/data noun, not a
    # reference to THIS conversation's state.
    "Add a session timeout of 30 minutes to the auth middleware.",
    "Write a function that serializes the conversation history to JSON.",
    # Contains "same" + "other" but about a fresh, self-contained task.
    "Write a function with the same signature as `parse_config` for the "
    "other file format, YAML.",
    # Contains "last" + a count, but as a slicing/indexing spec, not history.
    "Return the last three elements of the input list.",
    # A single orchestration verb or agent noun alone — the original
    # false-positive guard, still true for the broadened matcher.
    "Deploy the last commit to staging.",
    "Merge the feature branch into main.",
    # Bare short prompt, but NOT one of the ambient status/continuation
    # phrases — ordinary short questions must still route normally.
    "explain recursion",
    "what's 2+2",
    "list the files here",
])
def test_broadened_shapes_do_not_over_fire(auto_route, prompt):
    """New patterns must not swallow ordinary code/query/edit prompts that
    happen to share a keyword ('session', 'same', 'last', 'merge')."""
    assert not auto_route._is_coordination_task(prompt), (
        f"false positive — should NOT be coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


# ── Real-traffic shapes (2026-09-28 remediation) ──────────────────────────
#
# A held-out, hand-labelled sample of this machine's own real short prompts
# (extracted and scrubbed via scripts/groundtruth/extract_corpus.py, never
# committed) showed the golden-set-tuned patterns above scored 0/63 recall
# on real traffic — real status/continuation prompts don't use the golden
# set's vocabulary. These patterns were added and tuned against one half of
# that sample (the "tune" half); the other half ("holdout") was scored
# exactly once, after the patterns were locked: 80.2% recall (65/81),
# 84.4% precision (65/77), n=126. The examples below are paraphrases in the
# same shapes, not the real captured text.


@pytest.mark.parametrize("prompt", [
    # Bare status/progress query ("what's left", "did you X", "next steps")
    "what's left before we can ship?",
    "did you already push the branch?",
    "what are the next steps here?",
    "what's the status of the migration run?",
    # Continuation opener with a thin, unresolved remainder
    "ok, keep going with the rest of it",
    "yes, do the first two and skip the rest",
    "so what did we find?",
    # Bare deictic subject with no named anchor
    "is it still broken?",
    "can you check on it?",
    "why is it failing again?",
    # Ambient orchestration: an operational verb applied to a bare pronoun
    "merge it once the checks are green",
    "deploy it and let me know when it's live",
])
def test_real_traffic_shapes_classify(auto_route, prompt):
    """Status queries, thin continuations, and bare-pronoun operational
    prompts a stateless routed model cannot resolve without conversation
    history."""
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", [
    # "commit"/"push" with a bare pronoun are ordinary git shorthand for
    # "whatever is currently staged" on real traffic, not a reference that
    # needs conversation history — must NOT be swept in by the generic
    # deictic check.
    "commit it and push when you're done",
    "push it once the tests pass",
    # A continuation opener whose remainder names a concrete anchor (a
    # file, a PR number, a version) is a real, self-sufficient task.
    "ok, update the version in pyproject.toml to 2.4.0",
    "now fix the bug described in issue #482",
    # A deictic pronoun with an antecedent in the SAME sentence, or a
    # descriptive sentence that merely contains "this"/"that" as an
    # ordinary determiner or relative pronoun, is not session-dependent.
    "I found a bug in parser.py, can you fix it",
    "write a function that serializes this list to JSON",
])
def test_real_traffic_shapes_do_not_over_fire(auto_route, prompt):
    """The real-traffic patterns must not swallow ordinary operational
    instructions that happen to contain a pronoun or a git verb."""
    assert not auto_route._is_coordination_task(prompt), (
        f"false positive — should NOT be coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


# ── Coordinate-status generalization (2026-09-28 follow-up) ──────────────
#
# Live defect: "status of the loop guard PR?" was sometimes caught and
# sometimes not — the shape only worked with the literal word "of" and no
# possessive form. A held-out sample of this machine's own real short
# prompts (tune half, 50/50 fixed-seed split, scored once on the disjoint
# holdout half — see the PR description for n and the before/after numbers)
# showed several closely-related status/progress/continuation shapes were
# also missed. Every example below is a hand-written paraphrase in the
# same shape as a real miss, not the captured text itself.


@pytest.mark.parametrize("prompt", [
    # "status of/on X" — "on" was missing entirely.
    "status on the loop guard PR?",
    "status of the deploy PR?",
    # A possessive status ("X's status"), not "the status"/"status of".
    "what's the migration PR's status?",
    # "how's/how is X going/doing/coming along" with a NAMED subject in
    # between — only the bare "how's it going?" shape was covered before.
    "how's the loop guard PR going?",
    "how is the release train coming along?",
    # "is/was X done/finished/ready/merged/landed/shipped" with a named
    # subject — only the bare-pronoun form ("is it done?") was covered.
    "is the loop guard PR done?",
    "was the hotfix deploy successful",
    # "did X land/ship/merge/finish/complete" with a named subject.
    "did the loop guard PR land?",
    "did the release ship yet?",
    # An explicit "still waiting/pending/open/..." — a real-traffic status
    # phrase distinct from the generic "what's left" shape.
    "the fixes are still pending review",
    # A bare "the X failed/broke" status report.
    "the build failed again",
    # References Claude's own prior turn, not external knowledge.
    "what are you proposing for the rollout order?",
    # An explicit continuation phrase ANYWHERE in the prompt, not just as
    # the opening word — "keep going" mid-sentence was previously missed.
    "don't wait between tasks, just keep going",
    "merge it when green and keep going",
    # A bare re-check or background-process ping with no named target.
    "please check again",
    "are you running anything?",
])
def test_status_generalization_classifies(auto_route, prompt):
    """Status/progress/continuation shapes closely related to the ones the
    fast-path already covered, in the phrasing real traffic actually uses."""
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", [
    # "current state of <topic>" stays excluded (pre-existing regression
    # guard, in BOTH places the phrase can match — see the fix's comment):
    # the new "status of/on" broadening must not widen this hole.
    "what's the current state of quantum computing research",
    "give me the current state of the art in fusion energy",
])
def test_status_generalization_does_not_over_fire(auto_route, prompt):
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


@pytest.mark.parametrize("prompt", [
    # An "[Image #N]" attachment tag must not falsely anchor a genuinely
    # bare, unresolved "it" elsewhere in the same prompt.
    "are you sure you fixed it? it happens again [Image #6]",
    # "this/that/these/those" as a determiner on the same nouns already
    # covered for "the/our/your" — was previously only recognized with
    # those three determiners.
    "use this plan and get started",
    "why weren't those drafts used?",
    # "your recommendation(s)" joins the existing plan/audit/report/... list.
    "just go with your recommendation",
    # "the last things we('ve) worked on" — a history reference without a
    # numeric quantifier, unlike the existing "my last three commits" shape.
    "can you pick up the last things we've worked on?",
])
def test_deictic_and_history_generalization_classifies(auto_route, prompt):
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", [
    # A noun reading of a word that is ALSO in the imperative-verb list
    # ("score", "list", "design", ...) must not be read as a verb just
    # because it follows a continuation opener — the determiner-guard fix.
    "so, what's the score?",
    "ok, what's on the list?",
])
def test_imperative_verb_noun_collision_still_classifies(auto_route, prompt):
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )


def test_multistep_operational_chain_still_classifies(auto_route):
    """Two recognized op verbs joined by "and" (an existing, pre-existing
    positive shape) stays on the multi-step path unaffected by the
    broadened imperative-verb list."""
    assert auto_route._is_coordination_task("push and create a release")


@pytest.mark.parametrize("prompt", [
    # A continuation opener whose remainder now correctly resolves as a
    # real, self-sufficient task with the two verbs added to close a
    # precision gap ("release", "start") — these must NOT be swept into
    # coordinate just because they follow a discourse-filler opener.
    "so use the token to release the package",
    "then start the falsifying experiment",
])
def test_imperative_verb_precision_fixes_do_not_over_fire(auto_route, prompt):
    """The determiner-guarded, narrowly-expanded imperative-verb list
    ("release", "start") fixes real precision misses without re-opening the
    ones a broader, unproven verb list was found (on the held-out half) to
    break — see the verb list's own comment for the specific regression and
    why it was reverted."""
    assert not auto_route._is_coordination_task(prompt), (
        f"false positive — should NOT be coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


# ── The OLD "coordination" (no trailing "e") bucket is a different thing ──
#
# Decision (2026-09-28, with evidence — see enforce-route.py's comment next
# to the "coordinate" exemption): the older, heuristic-scored "coordination"
# task type is NOT exempted from PreToolUse enforcement, because it is
# dominated by executable git/deploy operational commands
# (test_enf_coordination_bash_names_llm_act.py pins these as correctly
# BLOCKED and redirected to llm_act). What could have made that decision
# wrong is if bare ambient-status prompts also landed there — they don't:
# the fast-path above runs first and claims them as "coordinate" instead,
# so old-bucket "coordination" only ever sees the prompts the fast-path
# declined to claim.


def test_ambient_status_does_not_reach_old_coordination_bucket(auto_route):
    """The live symptom this whole fix targets ('status of the post-deploy
    run?' landing in 'coordination'/moderate, held, then routed to
    llm(task='query')) must now resolve via the 'coordinate' fast-path
    BEFORE the heuristic scorer — where the old bucket lives — ever runs."""
    result = auto_route.classify_prompt("what's the status of the deploy run?")
    assert result is not None
    assert result["task_type"] == "coordinate"
    assert result["method"] == "coordination-fast-path"


def test_operational_git_command_still_reaches_old_coordination_bucket(auto_route):
    """A git/deploy operational command with an ambiguous target — the
    shape actually left over in the old bucket (measured on the tune/
    holdout real-prompt sample: 8/8 residual old-bucket cases were exactly
    this shape) — must keep landing in 'coordination', not the new
    advisory-only 'coordinate', so it still gets redirected to the
    tool-capable llm_act door instead of being silently exempted."""
    result = auto_route.classify_prompt("Run the test suite and commit the passing changes.")
    assert result is not None
    assert result["task_type"] == "coordination"


# ── Enum wiring ──────────────────────────────────────────────────────────


def test_tasktype_enum_has_coordinate():
    """The bucket exists in the shared TaskType enum."""
    from llm_router.types import TaskType

    assert TaskType.COORDINATE.value == "coordinate"
    # Text/media/introspect members are untouched.
    assert TaskType.INTROSPECT.value == "introspect"
    assert TaskType.CODE.value == "code"


# ── End-to-end: enforce-route lets native tools through for coordinate ──


def test_enforce_route_skips_coordinate(tmp_path):
    """A pending route with task_type=coordinate must not block Bash, even
    under the strictest enforcement modes — mirrors introspect's exemption
    in enforce-route.py. Before this fix, a coordinate-classified prompt
    still had its tools held until a throwaway llm() routing call was made
    (the live symptom in the defect report: 'status of the post-deploy
    run?' held Bash under enforcement)."""
    import json
    import os
    import subprocess
    import sys
    import time

    enforce_hook = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "enforce-route.py"
    )
    session_id = "sess-coordinate"
    router_dir = tmp_path / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    (router_dir / f"pending_route_{session_id}.json").write_text(
        json.dumps({
            "expected_tool": "llm_query",
            "task_type": "coordinate",
            "complexity": "moderate",
            "method": "coordination-fast-path",
            "issued_at": time.time(),
            "session_id": session_id,
        })
    )

    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(tmp_path)
    env["LLM_ROUTER_HOME"] = str(tmp_path) + "/.llm-router"
    env["LLM_ROUTER_ENFORCE"] = "strict"

    result = subprocess.run(
        [sys.executable, str(enforce_hook)],
        input=json.dumps({
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo status"},
        }),
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"coordinate must skip enforcement; got block payload: {result.stdout!r}"
    )
