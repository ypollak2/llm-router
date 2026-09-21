# Ground Truth: from current state to a measurable router

How llm-router gets from "no evaluable history" to a production-derived dataset
that can judge a routing decision. Written 2026-09-20.

---

## 0. Why the historical data cannot support a Ground Truth set

Not a volume problem. There are 22,356 routing records. They are unusable for
this purpose for three independent reasons, each measured:

**There is no join key.** Prompt text lived only in conversation transcripts,
which carry no `route_id`, no `task_type` and no timestamp.
`routing_quality.jsonl` carries the decision and no prompt. The two stores were
written by different processes and share no field, so no offline work can
establish which prompt produced which routing decision. Pairing them by
timestamp proximity would be a guess, and a third of the rows are benchmark
traffic that would happily match something.

**The prompts are not tasks.** The surviving corpus is agentic-session traffic:
median 46 characters, ~8 words. Roughly four in five are continuations
("go with the plan"), carry an unresolved pronoun ("measure it against the
brutal suite first"), or ask about the session itself ("what's left?", "did you
push the npm?"). A model answering those cold cannot be right or wrong.

**The outcome fields are dominated by test writes.** Of 2,774 fallbacks, 416
share a single completion-token value and 437 share two — fixtures, not
inferences. After excluding synthetic models and sessions, quality escalations
with both a real tier change and a nonzero cost number **one**. Any historical
figure computed from `mis_route`, `weak_pass` or `fallback_reason` is measuring
the router's own test suite.

Full evidence: `data/groundtruth/REPORT.md`.

---

## 1. Clean telemetry (done — schema v3)

`RouteLedgerRecord` gained eight fields. The contract they exist to satisfy:

```
given route_id       ->  exactly one ledger record
given prompt_sha256  ->  the captured task text, if capture was enabled
```

Both are exact-key lookups. **No timestamp is used as a join mechanism
anywhere**, and `scripts/groundtruth/join.py` reports an unjoinable route as
`incomplete` rather than guessing.

| Field | Purpose |
|---|---|
| `session_id` | groups routes into one conversation |
| `prompt_sha256` | **the join key**, via `trace_id.hash_prompt()` |
| `response_sha256` | identifies the output without storing it |
| `latency_ms` | wall-clock, `time.monotonic()` at the call site |
| `complexity` | stratification dimension |
| `classification_method` | routing decision metadata |
| `verification_type`, `verifier_name` | *how* acceptability was decided |
| `capture_ref` | pointer into the capture store, null when capture is off |

Everything else the evaluation unit needs was already recorded: `route_id`,
`parent_route_id`, `ts`, `task_type`, tiers, models, cost, tokens,
`fallback_*`, `chain_attempts` (retries), `tool_execution_*`.

Two things worth knowing about the change:

- `summarize()` now tests `schema_version >= 2`, not `== 2`. The equality test
  would have silently emptied every quality denominator at the bump and
  reported a clean 0% instead of a missing measurement. There is a test whose
  only job is to prove an `==` filter would have dropped the row.
- Identity resolution moved above the ledger write so `session_id` can go into
  the row. Its fail-open guard is unchanged.

## 2. Privacy

**The routing ledger still contains no prompt or response text.** v3 added
hashes, not content, so reading `routing_quality.jsonl` reveals nothing it did
not already reveal. Text exists only in the opt-in capture store.

| Data | Treatment |
|---|---|
| Prompt text | **Scrubbed before durable storage.** Only in `prompt_capture.jsonl`, only when `LLM_ROUTER_GROUND_TRUTH=1` |
| Prompt identity | **Hashed** — `sha256`, in the ledger, not reversible |
| Response text | **Never stored** by this system. Hash only |
| Credentials, keys, tokens | **Never stored.** `secret_scrubber.scrub_text()` runs first, unconditionally |
| Home directory paths | **Scrubbed**, shape preserved: `/Users/x/proj/a.py` → `<HOME:…>/proj/a.py` |
| Emails, public IPs, phone numbers | **Scrubbed** |
| Customer / person names | **Denylist only.** Regexes cannot detect a name; `LLM_ROUTER_CAPTURE_DENYLIST` is the only mechanism, and it is as complete as whoever maintains it |
| Repository / file references | **Referenced, not duplicated** — path shape survives, identity does not |
| Anything still suspicious | **Flagged** by `residual_risk()` for human review, not silently admitted |

**Are raw prompts required?** No, for the ledger — it needs only the hash. Yes,
for authoring evaluation tasks, since a task has to be readable. Those are
scrubbed before they reach disk; there is no raw mode, and adding one would be
a policy change rather than a flag.

`scrub()` **fails closed**: if `secret_scrubber` cannot be imported it raises
rather than scrubbing with a partial ruleset. The canonical scrubber is the
single source of truth per CHZ-SEC-01; this layer only adds what a credential
scrubber should not be doing — identity and location.

**Retention.** The capture store is append-only and has no automatic expiry
today; it is the operator's to prune. It is deliberately *not* the 7-day
`result_cache`, whose TTL would age out prompts mid-dataset. Frozen datasets
are immutable by design and outlive the capture file they came from.

## 3. The pipeline

```
production task
  → captured + scrubbed          prompt_capture.py
  → joined to its route          join.py            (route_id / prompt_sha256)
  → frozen as an eval task       author_tasks.py → freeze.py
  → run against every tier       run_matrix.py
  → verified per (task, model)   verifiers.py
  → PASS / FAIL / AMBIGUOUS      dataset.Outcome
  → cheapest_acceptable_model    label.py
  → ruler checked                discriminate.py
```

**The router never labels its own ground truth.** `label.py` reads the outcome
matrix and nothing else; a label derived from the system under test measures
nothing.

**Three outcomes, not two.** A binary pass/fail has nowhere to put "the
verifier could not decide", so an inconclusive run silently becomes a FAIL and
the model is blamed for the harness. AMBIGUOUS is excluded from labelling and
reported separately. When an ambiguous cell sits *below* the cheapest PASS, the
task gets **no label at all** — the cheaper tier might have been acceptable,
and assuming FAIL would bias every such label upward.

**Verification preference**, strongest first — never reach for a weaker method
when a stronger one can decide the same task:

1. `mechanical` — deterministic assertion, exit 0/non-0
2. `sandbox` — executed in an isolated tree, behaviour asserted
3. `programmatic` — structural check on the output
4. `task_assertion` — hand-authored, task-specific
5. `existing_verifier` — the repo's `bench_*` suites
6. `human`
7. `llm_judge` — only where unavoidable

Every outcome carries `verification_type`, `verifier` and `confidence`.
Deterministic and judge verdicts are **never pooled into one number**; a task
mixing them is flagged.

A constraint worth stating plainly: **every verifier in this repo pairs a
hand-authored prompt with a hand-authored assertion.** None derives a check
from a prompt. `bench_grounding.py` looks like an exception but only handles
"which file defines X", where `git grep` supplies the answer. So authoring is
irreducibly human, and `author_tasks.py` exists to make that backlog countable
rather than to hide it.

## 4. Seed Evaluation Set

`data/groundtruth/seed-v1/` — **not** production Ground Truth v1, and it must
not be cited as one. Its only job is to prove the pipeline runs end to end.

Built from 137 prompts that survive every exclusion filter, from a funnel that
balances (2,702 = 2,565 + 137). Currently **0 mechanical verifiers authored**,
so tune/test are empty and no label can be derived yet. That is the honest
state, not a bug: the corpus can support authoring, and nobody has authored yet.

No joins were manufactured to inflate it. No timestamp proximity, no weak
heuristics.

## 5. Accumulating production traffic

```bash
export LLM_ROUTER_GROUND_TRUTH=1
export LLM_ROUTER_CAPTURE_DENYLIST=~/.llm-router/denylist.txt   # customer names
```

Nothing here can be backfilled. Every day capture stays off is a day of
telemetry that cannot become ground truth.

Check progress with `join.coverage()`, which accounts for **every** route and
names the reason each unjoinable one failed. Read it before quoting a dataset
size: a dataset built from the complete rows alone will look representative
while describing only the fraction that had capture switched on.

## 6. Ground Truth v1

Cut when enough clean traffic exists for 300–500 tasks. The sampling code is
already written (`sampling.py`) so the stratification decisions are settled
before anyone is looking at a number they want to move.

- **Proportional stratification** over the repo's own taxonomy — `TaskType`
  and `Complexity` from `llm_router.types`, plus an agentic axis
  (`route_kind=delegate`) and a tool-heavy axis (`tool_execution_attempted`).
  No invented categories: strata the router does not use are strata nothing can
  act on.
- **A floor for rare strata.** `analyze` was 0.4% of non-test traffic;
  proportional sampling alone would drop it, and the rare routes are often the
  expensive ones to get wrong. `min_per_stratum` guarantees representation, and
  the manifest records the resulting over-representation — guaranteeing the
  floor and hiding it would be worse than not guaranteeing it.
- **Deduplication**: exact on punctuation-stripped, casefolded, lightly stemmed
  tokens; near on token-set Jaccard ≥ 0.9, first occurrence kept with a
  `duplicate_count`. So retries, repeated commands and regenerated answers
  contribute one task, not fifteen — the set represents tasks, not traffic
  volume.

Every frozen dataset carries a manifest with version, creation date, traffic
window, sample count, source provenance, task distribution, exclusion filters,
deduplication methodology, anonymisation methodology, verifier coverage,
ambiguous count, model set and content hash.

**The test partition is immutable and must not be tuned against.** Reading it
requires an explicit reason and is logged to `test_access.log`. A file cannot
be made unreadable to its owner, so the guard is visibility, not prevention: if
a tuning run touches the test set, the log says so and the number can be
discarded.

## 7. Future router evaluation

Out of scope here, and deliberately blocked until the above is trustworthy:
router scoring, routing accuracy, regret curves, cost-quality frontier, shadow
mode, cache evaluation, RouterArena comparison, dashboards, published claims.

One check does belong to this phase, because it validates the dataset rather
than the router: `discriminate.py` replays `always-cheapest`, `always-premium`,
`random` and `oracle` over the outcome matrix. **If the constant policies score
the same, the dataset cannot measure routing** and any router number from it
would be noise with a decimal point. That is not hypothetical — on RouterArena
a constant policy was competitive with everything except retrieval, which said
more about the benchmark than about the routers.

---

## Commands

```bash
# Corpus
python3 scripts/groundtruth/extract_corpus.py --out data/groundtruth/corpus.jsonl
python3 scripts/groundtruth/author_tasks.py --suggest

# Freeze  (immutable; a new version rather than an edit)
python3 scripts/groundtruth/freeze.py --version seed-v1 --note "..."

# Evaluate
python3 scripts/groundtruth/run_matrix.py --version seed-v1 --split tune --dry-run
python3 scripts/groundtruth/label.py       --version seed-v1 --split tune
python3 scripts/groundtruth/discriminate.py --version seed-v1 --split tune

# Self-checks
python3 scripts/groundtruth/verifiers.py          # 13 helper self-tests
pytest tests/test_groundtruth_*.py -q
```

---

# Ground Truth Accumulation Mode

How a live request becomes a future Ground Truth candidate. Added 2026-09-20,
after the historical corpus produced 121 rows and zero labels.

## The principle

**Do not collect prompts first and ask later whether they are evaluable.** The
state a task needs to be replayed exists only while the task is running. Ask
then, or never. Eligibility is decided at capture time and the decision is
recorded with its reasons.

## The path

```
live request
  → capture (scrubbed)                 prompt_capture.py
  → assess eligibility                 eligibility.assess()     cheap, pure
  → capture ONLY the state it needs    envelope.build()
  → re-assess against what was kept    eligibility.assess()     conservative
  → admit or reject, with a reason     pool.Pool.admit()
  → accumulate                         ground_truth_candidates.jsonl
```

The third step is what keeps it honest: a task can pass every content check and
still be rejected because the repo was not a git tree, or the external evidence
was never frozen. **Eligibility is a property of the prompt plus what was
actually preserved**, never of the prompt alone.

## Two independent questions

| | |
|---|---|
| `replayable` | can this be RUN again from captured state? |
| `verification_candidate` | if it ran, could anyone decide whether it succeeded? |

A task can be replayable and unverifiable (*"write me a vision document"*
against a frozen repo), or verifiable and unreplayable (a crisp bug fix whose
repo state was never captured). Only both makes a candidate.

## Eligibility rules

Rejected outright: harness artefacts, canned templates (same prompt across many
sessions), prompts under five words, and anything that cannot be scrubbed
safely — privacy wins over evaluation, always.

| Required state | Satisfiable? | How |
|---|---|---|
| session | **Never** | Capturing a transcript does not make *"continue what we were doing"* well-defined |
| machine | Not today | Nothing captures which models were loaded |
| external | Yes | Only if evidence was frozen at task time with provenance |
| repo | Yes | Commit SHA; plus the stored patch when the tree is dirty |
| tool | Yes | Tool names or a test command |

Verifier class comes from the repo's existing `VERIFICATION_PREFERENCE`, never
a parallel vocabulary. A factual question gets `mechanical` **and**
`needs_reference_answer`: it is admitted, but never counts as high-confidence
until a person establishes the right answer from evidence rather than belief.

## What is captured

Prompt (scrubbed) · route_id / session_id / prompt_sha256 · task type and
complexity · repo identity, commit, branch, dirty flag, **the patch itself**
when dirty and under 200k chars (scrubbed) · lockfile hashes · hashes of files
the task names · frozen external evidence with source, timestamp, hash and
version · tool names, tool-input hash, test command · toolchain versions.

## What is deliberately NOT captured

Whole repository snapshots (a commit already references the tree) · response
text (hash only) · full environment dumps · files the task never mentions ·
raw unscrubbed anything · machine state · conversation history as a way to
rescue session-bound tasks — it does not, so it is not stored for that purpose.

## Lifecycle

```
CAPTURED → ELIGIBLE → READY_FOR_REPLAY → VERIFIED → FROZEN_IN_GROUND_TRUTH
```

with failure states `INELIGIBLE`, `REPLAY_FAILED`, `VERIFICATION_UNAVAILABLE`,
`AMBIGUOUS`. Transitions are **checked**: `advance()` refuses a jump that skips
a state, so nothing reaches VERIFIED without having been replay-ready. Every
transition carries a reason, and rejected tasks stay in the pool as evidence
rather than being deleted.

## Inspecting the pool

```bash
python3 scripts/groundtruth/accumulate_report.py
python3 scripts/groundtruth/accumulate_report.py --traffic traffic.json --sampling
python3 scripts/groundtruth/accumulate.py --from-capture     # dry-run the gate
```

The report answers: how many captured, how many eligible, replay-ready, with a
credible verifier, high-confidence; why tasks are being rejected; which task
types are thin; and how the pool's mix compares to real traffic.

## Turning it on

```bash
export LLM_ROUTER_GROUND_TRUTH=1                              # the only switch
export LLM_ROUTER_CAPTURE_DENYLIST=~/.llm-router/denylist.txt # customer names
```

**One flag** turns on the whole path: capture, eligibility gate, replay
envelope, candidate pool. To disable, unset it. `LLM_ROUTER_CAPTURE_PROMPTS`
is the retired name and still works — it enables exactly the same thing, not a
second half.

Off by default, because this is the one part of the system that writes user
prompt text to disk. Scrubbing runs first and fails closed, but "we scrub it"
is a reason to allow the choice, not to make it for people.

`LLM_ROUTER_GT_NO_ACCUMULATE=1` keeps the capture file without the pool. It is
an escape hatch, not a second switch.

Nothing here can break routing: capture, accumulation and the outcome log each
fail open.

## Before freezing Ground Truth v1

Count is necessary and not sufficient. The report's verdict line checks the
count; **the type mix is the judgement call**. Reaching 400 candidates that are
all the same easy coding task would look like progress and measure nothing.


## Runtime wiring

Accumulation runs inside normal routing, not only from the CLI.

```
route_and_call()
  └─ _finalize_successful_route()          src/llm_router/router.py:1958
       ├─ record_route(stamp_trace(...))   the v3 ledger row
       └─ prompt_capture.capture()         src/llm_router/router.py:1989
            └─ accumulate()                eligibility → envelope → pool
```

One integration point, at the single choke point every success path already
goes through. Capture and accumulation are both fail-open: a failure loses a
candidate, never a turn.

### Observability

Every accumulation attempt writes one line to
`~/.llm-router/gt_accumulation.jsonl` with one of four outcomes, so "nothing
appeared in the pool" is never ambiguous:

| outcome | meaning |
|---|---|
| `persisted` | a candidate entered the pool |
| `rejected` | the eligibility gate said no — with its reason |
| `deduplicated` | already in the pool; `duplicate_count` incremented |
| `error` | accumulation itself failed, with the exception type |

`prompt_capture.status()` reports the log path and in-process counters.
There is no `except: pass` on this path — the only swallowed failure is the
observability write itself, which is the last thing that can fail.

### Known gotcha

`LLM_ROUTER_HOME` redirects the capture store, the pool and the outcome log,
but **not** the routing ledger, which reads `LLM_ROUTER_ROUTING_LEDGER`.
Isolating a test run needs both.
