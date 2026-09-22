# Ground Truth audit — Phases 17-18, 21-24 (2026-09-22, re-run)

Scope: `scripts/groundtruth/*.py` (6,678 lines), the router integration
(`src/llm_router/prompt_capture.py`, `router.py` `_finalize_successful_route`),
`docs/GROUND_TRUTH.md`, and the prior two audits (2026-09-21, 2026-09-22),
which this document does **not** trust and re-derives independently. All
probes ran under `export LLM_ROUTER_HOME=$(mktemp -d)`; nothing was written to
`~/.llm-router`. No code was changed.

The 2026-09-22 audit's headline claim — "the rigorous half of the pipeline is
disconnected from the half that produces labels" (ID namespace mismatch,
`T-06`) — and commit `5462cda`'s claim to have fixed it were both
**independently reproduced** below. Verdict: the ID mismatch is genuinely
fixed; the pipeline is still disconnected, for a different and previously
undocumented reason (Phase 17).

---

## Phase 17 — every arrow, run for real

```
real traffic -> capture -> scrub -> traceability -> eligibility -> replay envelope
-> candidate pool -> dedup -> verifier generation -> verifier validation
-> model matrix -> label -> frozen GT
```

| Arrow | Status | Evidence |
|---|---|---|
| traffic → capture → scrub → eligibility → envelope → pool | **CONFIRMED, live** | Ran `llm_router.prompt_capture.capture()` under an isolated `LLM_ROUTER_HOME` with `LLM_ROUTER_GROUND_TRUTH=1`. Produced a `READY_FOR_REPLAY` row in `ground_truth_candidates.jsonl` (`gtc-a03d11fac17097d1`) and a `persisted` line in `gt_accumulation.jsonl`. `router.py:2144` really calls `prompt_capture.capture`, which really calls `groundtruth.accumulate.accumulate` (`prompt_capture.py:286`). The docs' wiring diagram is accurate. |
| pool → propose → mutants → registry (ACTIVE) | **CONFIRMED, live** | Drove a candidate through `verifier_cli.py suggest` (blocked here on `frozen_reference` needing a human reference answer — itself correct behaviour, see Phase 18) and through the repo's own test `tests/test_t06_registry_verifier_reaches_a_frozen_task.py`, which is a real, passing (10/10) integration test, not a comment. |
| registry (ACTIVE) → frozen task grading (`run_matrix --use-registry`) | **FIXED, mechanically** | `run_matrix._content_task_id()` recomputes `gtc-{exact_key(prompt)}` from a frozen task's own prompt text (`groundtruth.extract_corpus.exact_key`) and looks the registry up under both identities. Verified this actually grades: a frozen `gt-0001` task with `verifier=None` picked up an `ACTIVE` `words("lisbon")` verifier from the registry, accepted "Lisbon", rejected "Madrid". The old bug (`reg.active_for(t.task_id)` alone, silently `None` forever) is real and is really gone. |
| pool candidate → **frozen dataset task** | **STILL BROKEN — no producer, not an ID bug** | This is the finding the previous two audits missed. `freeze.py` and `author_tasks.py` never import, read, or reference `scripts/groundtruth/pool.py` in any form (`grep -rln pool scripts/groundtruth/freeze.py scripts/groundtruth/author_tasks.py scripts/groundtruth/extract_corpus.py` → no hits). `author_tasks.py`'s only input is `data/groundtruth/corpus.jsonl`, built by `extract_corpus.py` from **historical** transcripts (`~/.claude/projects`, `routing_quality.jsonl`) — a population that is structurally disjoint from live-captured pool prompts. The T-06 bridge is real and correctly implemented, but it is a lookup mechanism with **no producer that ever creates a matching pair**. `reg.active_for()`/`_registry_record_for()` will only ever find a match if a human manually retypes a pool candidate's exact prompt text into the tasks file that `freeze.py` consumes — which is exactly what the fix's own test and commit message did to demonstrate it ("Verified end to end"), and which nothing in the product automates or documents as a step. In today's shipped pipeline, `--use-registry` will report `0 adopted` against any real frozen dataset, forever, unless someone builds the missing arrow (`pool → tasks file`, e.g. a `promote.py`) that does not exist. |
| model matrix → label → frozen GT | **CONFIRMED, mechanically sound** (see prior audit + `tests/test_groundtruth_pipeline.py`, 72 tests, reran here, all pass) | `label.py` never emits `cheapest_acceptable_model` across mixed `verification_type`s; three-state PASS/FAIL/AMBIGUOUS is enforced; `discriminate.py` fails closed on missing `verification_type`. |

**Net effect on the headline claim:** the 2026-09-22 audit's finding was real, and the fix is real and independently reproducible — but it fixes only the *lookup*. The subsystem is still disconnected end to end, because nothing produces overlapping prompts for the lookup to find. Every label that exists today still comes from the older, fully manual `author_tasks.py` path, exactly as the unfixed system did.

### A second, more consequential wiring gap (new, not in either prior audit)

`src/llm_router/prompt_capture.capture()` — the one function `router.py` calls
on the live success path — has this signature:

```python
def capture(prompt, *, route_id=None, session_id=None, task_type=None,
            complexity=None, chosen_tier=None, chosen_model=None,
            classification_method=None, extra=None) -> bool:
```

There is **no parameter for `cwd`, `tool_names`, `test_command`, or
`external`.** `router.py:2144` calls it with none of those. It cannot,
structurally — the plumbing to pass them from the routing call site does not
exist. `groundtruth.accumulate.accumulate()` — which *does* accept all four —
is invoked from inside `capture()` with only `prompt_sha256`, `task_type`,
`complexity`, and `model_config={"chosen_model": ...}` (`prompt_capture.py:286-292`).

Consequence, traced through `eligibility.assess()`:

* `has_external_evidence=bool(external)` is **always `False`** on the live
  path → any prompt whose task needs external evidence is permanently
  ineligible, not because the evidence wasn't available, but because nothing
  offers it.
* `env.repo` is built from `envmod.build(cwd=None, ...)` → repo state is
  captured from the **router process's own cwd**, not the task's, when it
  captures anything at all — moot in practice, because repo-bound tasks are
  separately, honestly gated ineligible today via `R_NO_REPLAYER =
  "no-replayer-for-required-state"` (`eligibility.py:58`), a state the repo's
  own tests document (`test_repo_task_with_captured_state_waits_for_a_replayer`).
* `tool_names`/`test_command` are always empty → tool-required tasks are
  permanently ineligible on the live path for the same structural reason.

**The only category of prompt that can ever become a live-captured Ground
Truth candidate today is one that needs *no* repo state, *no* tool state, and
*no* external evidence** — a standalone, closed-form question. This is not a
policy choice recorded anywhere (unlike the honestly-disclosed
`no-replayer-for-required-state` gate); it is a byproduct of one function
signature not being extended when three other subsystems were built to expect
it. See Phase 24 for what this does to representativeness.

---

## Phase 18 — zero-human feasibility, HARD vs SOFT

**HARD GT (mechanically provable) exists and is correctly separated from
SOFT.** `label.py` refuses to emit a label when `verification_type` mixes
`SUBJECTIVE_METHODS` (`llm_judge`, `human`) with `DETERMINISTIC_METHODS`
(mechanical/sandbox/programmatic/existing). `discriminate.policy_score` fails
**closed** on a cell with no recorded `verification_type` (reran the relevant
tests — pass). `contract.py` blocks on `unclear` acceptance criteria rather
than guessing. None of this is new; it was already correct as of the prior
audit and remains correct at HEAD.

**Can this become zero-human?** No, and the gap is not incidental:

1. Every mechanical verifier in this repo is a **hand-authored assertion
   paired with a hand-authored prompt**. `propose.py`'s "assistant" is a
   template engine over five fixed shapes (`num`, `words`, `yesno`, `run`,
   `pytest`) plus a `frozen_reference` strategy that is *structurally* human:
   it blocks with `"reference answer not yet captured"` until a person
   supplies one (reproduced live — a "capital of Portugal" candidate stayed
   `PROPOSED`/`BLOCKER` with zero path to auto-resolve). Verified against real
   traffic shape: a code-fix prompt ("reverse a linked list") produced
   `strategy=no_reliable_verifier`, not a generated test — the templates do
   not cover open-ended coding tasks at all.
2. Human sign-off (`approve`/`activate`) is enforced by `require_human_actor`,
   a **name-plausibility filter**, not authentication — the code says so
   itself (`verifier_registry.py:44-48`). A determined script can still pass
   `--by janedoe37`. This is honestly documented as a guardrail against
   *accidental* automation, not a security boundary, so it is a DESIGN RISK,
   not a bug.
3. **SOFT GT does not inherit HARD GT's confidence anywhere measured.**
   `SUBJECTIVE_METHODS` are excluded from `label.py`'s deterministic label,
   from `discriminate.policy_score`'s accept-rate, and the `llm_judge`
   strategy (`S_JUDGE`/`V_JUDGE`) is a taxonomy entry with **no implementation
   anywhere** — `grep` for `S_JUDGE`/`V_JUDGE` across every script shows it
   used only in enum definitions and one unreachable `if strategy == S_JUDGE`
   branch that only appends a risk string; `generate_snippet()` has no case
   for it. So the one channel through which SOFT could contaminate HARD
   (a judge verdict silently pooled as if mechanical) is not merely guarded —
   it doesn't exist yet to be exploited. This is the strongest structural
   property in the subsystem (see the companion Verifier Trust audit,
   Phase 20).

**Residual bias even at zero-human:** the taxonomy itself (`TaskType`,
`Complexity`, `route_kind`, `tool_execution_attempted`) is the router's own,
reused deliberately ("no invented categories" — `docs/GROUND_TRUTH.md` §6).
That is good practice for construct validity but means the dataset can never
surface a failure mode the router's own taxonomy doesn't already have a slot
for.

---

## Phase 21 — replay under hostile conditions

**There is currently no replayer to attack.** `eligibility.py`'s own constant
`R_NO_REPLAYER = "no-replayer-for-required-state"` means every repo-bound task
is rejected today, in production, regardless of how well its state was
captured; the repo's tests can only exercise the "replayer exists" path via a
`monkeypatch` fixture (`with_replayer`, `tests/test_groundtruth_accumulation.py:48`).
`run_matrix.resolve_sandbox()` — the only code that actually points a
verifier at a directory — resolves a **hand-built fixture tree** under
`data/groundtruth/<version>/sandboxes/<name>/`, entirely disconnected from
`envelope.RepoState` (no code anywhere calls `git checkout <commit>` or
`git apply` against a stored `RepoState.diff` — confirmed by grep across
`run_matrix.py`, `freeze.py`, `dataset.py`, `sources.py`: zero hits for
`git checkout`/`git apply`/`.commit`). So "hostile replay" (commit missing,
branch moved, dependency unavailable, lockfile changed, different user home)
cannot be tested against a mechanism that has not been built — that is an
honest, disclosed gap, not a hidden one, and the correct audit statement is
**"unimplemented," not "broken."**

**A latent design risk for when a replayer is built:** `RepoState.reconstructable`
correctly distinguishes hash-from-reconstruction for the **dirty** case (`M-01`
fix — requires the actual stored `diff`, not `diff_sha256`) but does **not**
apply the same standard to the **clean** case: `if not self.dirty: return True`
as soon as `self.commit` is a non-empty string (`envelope.py:94`). A commit SHA
is itself only a hash; nothing here verifies the commit is still reachable
(force-push, rebase, `git gc --prune`, a deleted fork, a renamed/moved repo).
The dirty-tree fix's own docstring states the exact principle this violates
("a hash proves you have the right tree; it cannot produce it") one case away
from applying it consistently. Not exploitable today only because the
consuming replayer does not exist yet; will be exploitable the day one is
built unless this is fixed alongside it.

**External evidence is the one part of "replay" that is actually solved
correctly today**: `envelope.freeze_external()` stores the **scrubbed bytes**,
not just a hash, when given a `store_dir` (`envelope.py:259-266`), so a
changed or vanished source page does not break replay for evidence that was
frozen. The catch (Phase 17): the live wiring never calls this with
`external=` populated, so this correct mechanism is currently unreachable
from production traffic.

---

## Phase 22 — contamination

**Explicit-signal harness detection exists and is deliberately narrow.**
`routing_quality.detect_synthetic()` — the gate `prompt_capture.capture()`
checks before accumulating (`prompt_capture.py:279-282`) — fires only on
`LLM_ROUTER_SYNTHETIC=1`, `PYTEST_CURRENT_TEST`, or a `BENCH_SANDBOX` working
directory. Its own docstring explains why it does **not** use session-id or
model-name heuristics: those were tried and produced false positives/negatives
on real data (`M-02`). That is a defensible, evidence-based design choice, and
it closes the specific contamination incident on record (twelve
`bench_backend_quality.py` runs, six verbatim fixture prompts, fixed
2026-09-20).

**What it does not, and by its own design cannot, catch: autonomous
agent-loop traffic that is not a pytest run and not in a bench sandbox.**
This audit itself is an instance of exactly that shape — a long-running
agentic session issuing many structured, repetitive, instrumented prompts
through tooling, with none of the three signals present. Nothing in
`detect_synthetic()` would exclude such traffic from the live capture path if
it were routed through this router with `LLM_ROUTER_GROUND_TRUTH=1` set. This
is a **DESIGN RISK**, not a reproduced incident — flagging it because the
user's own memory record (`concurrent Claude sessions`, `S64 workbench`,
`agentic loop reliability`) shows this exact traffic shape is common on this
machine, and the corpus-extraction side (`sources.py`) already had to build
separate fixture-session/hex-stem heuristics to filter it out of the
*historical* corpus — heuristics the live gate deliberately does not reuse.

**Historical-corpus rejection is real and independently confirmed**:
`test_gate_rejects_the_historical_corpus` (reran, passes) exercises the same
claim `docs/GROUND_TRUTH.md` §0 makes in prose.

---

## Phase 23 — leakage

* **The router never sees a label.** `grep -rln groundtruth src/llm_router/*.py`
  → exactly one file, `prompt_capture.py`, and its only groundtruth import is
  the accumulation call — one-directional, write-only, on the *success* path,
  after the routing decision has already been made. No file under
  `src/llm_router/` reads anything under `scripts/groundtruth/` or
  `data/groundtruth/`. Routing decisions cannot be informed by GT labels
  because nothing wires that read path.
* **Metadata does not reveal difficulty beyond what the router itself
  assigned.** The only stratification axes (`TaskType`, `Complexity`,
  `route_kind`, `tool_execution_attempted`) are read directly from the
  router's own classification, not derived from outcome data — so there is no
  separate "difficulty" signal to leak.
* **Verifier artifacts do not leak target behaviour into the prompt the model
  sees.** `run_matrix.call_model()` sends only `task.prompt` to the tier under
  test; the verifier snippet/acceptance contract is never included in the
  message (confirmed by reading the call — `messages=[{"role": "user",
  "content": prompt}]`, no verifier text concatenated).
* **Tune/test semantic overlap: not measured, and the mechanism that would
  prevent it operates at the wrong stage to fully guarantee it.**
  `extract_corpus.py` deduplicates (exact + near, Jaccard ≥ 0.9) before
  `author_tasks.py` and `freeze.py` ever see the corpus, and `split_tasks()`
  (`dataset.py:219`) partitions that already-deduplicated population
  deterministically by stratum. So a single `freeze.py` run cannot leak a
  near-duplicate across its own tune/test split. But nothing re-runs
  near-duplicate detection **across dataset versions or across manually
  appended tasks** — a task added to `tasks/draft.jsonl` by hand after
  `extract_corpus.py` ran would bypass the Jaccard filter entirely and could
  land in either split with a near-duplicate already sitting in the other.
  This is a HYPOTHESIS (no reproduction attempted; the seed set is too empty
  today — 0 authored verifiers — to test against), not a confirmed leak.

---

## Phase 24 — representativeness (the critical question)

**Is the router being evaluated mainly on tasks that are easy to verify
rather than tasks users actually care about? Yes, and Phase 17's wiring gap
makes this quantifiable rather than just plausible:**

The live accumulation path — the only path that is actually wired into
production traffic — can **only ever admit tasks needing zero repo state,
zero tool state, and zero external evidence** (Phase 17). Cross-reference
`docs/GROUND_TRUTH.md` §0's own measurement of what real traffic looks like:
median 46 characters, ~8 words, "roughly four in five" are continuations,
unresolved-pronoun follow-ups, or session-meta questions ("what's left?").
Put the two together: the traffic segment structurally capable of reaching
the pool (self-contained, no state dependency) is close to the complement of
the traffic segment the docs themselves say dominates real usage
(context-dependent continuations). The eligible-for-GT population is not
merely *skewed toward* easy-to-verify tasks — for two of three state
categories, it is **mechanically incapable** of containing anything else,
independent of how much traffic accumulates. No volume of capture fixes this;
it requires wiring `cwd`/`tool_names`/`external`/`test_command` through
`capture()` into `accumulate()`, and (separately) building the repo-state
replayer `eligibility.py` already names and defers.

**Quantifying the gap precisely was attempted and is blocked by the same
finding as Phase 17's headline**: the honest way to measure "eligible
population vs. real traffic" is `accumulate_report.py --sampling` against a
populated pool, but the seed set has **0 authored verifiers** and the pool in
this isolated environment is synthetic (2 candidates, both manually seeded by
this audit). A trustworthy quantitative gap measurement needs either (a) the
capture wiring fixed and run against real traffic for a representative
window, or (b) `join.coverage()` run against the live `~/.llm-router` store —
which this audit's rules correctly forbid treating as more than observational
evidence, since it is known-contaminated (see `FROZEN_STATE.md`, the
`savings_stats` id-8827 incident). Recommend this be the first thing measured
once the wiring gap above is fixed — the report answers exactly this
question and already exists, unused for lack of the input population to
audit.

**One positive control**: `discriminate.py` genuinely refuses to publish a
router number if `always-cheapest` and `always-premium` score the same
(`--min-gap`, default 0.10). This is the correct check against exactly the
RouterArena failure mode the docs cite — it just cannot run yet, because
`run_matrix` has never had a populated `tune`/`test` split with authored
verifiers to run against (0 today).

---

## Summary table

| Phase | Verdict |
|---|---|
| 17 — arrows | ID-bridge fix (5462cda) is real; pipeline is still disconnected because no producer ever creates a matching prompt pair; **new** finding: the live wiring can only ever admit state-free prompts |
| 18 — zero-human | No. Authoring is irreducibly human by design (template engine, not an assistant); SOFT never contaminates HARD (llm_judge is unimplemented, not merely unused) |
| 21 — hostile replay | No replayer exists to attack (honestly disclosed); one latent hash-vs-reconstruct gap in the clean-commit case, inert until a replayer is built; external-evidence freezing is correctly implemented but unreachable from live traffic |
| 22 — contamination | Explicit-signal detector correctly closes the one recorded incident; does not and by design cannot catch non-pytest, non-benchmark agent-loop traffic |
| 23 — leakage | Router→GT is one-directional and write-only; no cross-split semantic dedup across manually-added tasks (unconfirmed) |
| 24 — representativeness | Confirmed structural bias toward easy-to-verify, state-free tasks; magnitude not quantifiable yet because the pool that would let `accumulate_report.py` measure it does not exist in real traffic |
