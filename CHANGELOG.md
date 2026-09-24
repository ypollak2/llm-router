# Changelog

All notable changes to `llm-router` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

| Where | What |
|---|---|
| This file | The current major line — v11.0.0 onward |
| [CHANGELOG-ARCHIVE.md](CHANGELOG-ARCHIVE.md) | v10.1.5 back to v6.3.0 |
| [GitHub Releases](https://github.com/ypollak2/llm-router/releases) | v6.2 and earlier |

## [15.2.0] - 2026-09-24

Security fixes and honest numbers, from a forensic audit whose every CRITICAL
and HIGH finding was re-checked by an independent verifier
(`audit/forensic_2026-09-24/`). Each fix below landed with a test that failed
before it and a red-check reverting only that fix; the full suite ran under a
clean HOME for every commit (final: 9,910 tests, 0 failures, 0 errors).

### Security

- **A cloned repository can no longer receive your provider API key.** A
  project `.env` setting `OPENAI_COMPAT_BASE_URL` made `call_llm` send the real
  `OPENAI_API_KEY` to that host (reproduced end to end against a local
  listener with a canary key). The openai-compatible provider now sends its own
  `OPENAI_COMPAT_API_KEY` or a placeholder; a project `.env` can no longer set
  endpoint-shaped keys (`…_URL/_BASE/_HOST/_ENDPOINT/_WEBHOOK`, proxies), and
  the hook injects only API keys and non-endpoint `LLM_ROUTER_*` settings from
  it — not `PYTHONPATH`, `NODE_OPTIONS` or `DYLD_*`. The pxpipe URL must be
  loopback. Your own `$LLM_ROUTER_HOME/.env` is unaffected.
- **The direct-execution chain is budget-gated and scrubbed.** It reached paid
  providers with no budget check and sent prompts unscrubbed. Paid providers
  now pass the router's own budget check (fails closed when unreadable), and
  prompt, history and system prompt are scrubbed after assembly.
- **`LLM_ROUTER_AGENT_WRITES=off/propose` now also covers `run_command`.**
  `python3 -c "open(p,'w')…"` wrote outside the project with writes off. Under
  `propose` (the default) and `off`, inline interpreter code, `sed -i` and
  `find` actions are refused. **Behaviour change:** `python -c` / `node -e` no
  longer run in the local agent loop unless writes are `apply`. This is not a
  sandbox — see SECURITY.md.

### Changed — what the numbers mean

- **Savings nobody observed being used are "unverified", never a headline.**
  Only the hook's realized-gated rows count as verified; MCP/gateway/agentic
  credits and rows of unknown provenance are shown beside the headline,
  labelled `+ $X unverified, n=N`. On the maintainer's ledger (2026-09-24),
  `llm-router status` all-time went from **$372.58 "saved"** to **$0.47 saved +
  $483.14 unverified (n=46,099)**; the canonical accessor reports $0.11 (n=7).
- Docs state the tool counts the server registers: **70** (`LLM_ROUTER_SLIM=off`)
  and **12** in the default surface, including `llm_local_task`, which can edit
  and run commands. A test ties the docs to the registration code.
- Host docs: Codex has the prompt-routing hook but **no** tool-call
  enforcement; Pi is **not installable** today (`install --host pi` fails).

### Fixed

- The local agent loop's wall-clock cap never fired (an absolute deadline read
  as a duration), so the hook could be killed at 60s holding nothing.
- The Codex adapter no longer returns the CLI's stdin banner as the answer.
- `last`, `retrospect`, `replay` and `snapshot` exit non-zero on failure.
- The service `/health` reports the package version (was a hardcoded 5.3.0).
- Three scripts that could not compile on Python 3.11; CI now compiles `scripts/`.

### Removed

- `llm_router.cache.SemanticCache`: a stub whose `get()` always returned None,
  with no callers. The working cache is `llm_router.semantic_cache`.

### Measured, not shipped

- A local agent (qwen3-coder:30b) passes spec-shaped edits reliably, and a
  pre-registered rule selecting them held out at precision 10/10 tasks — but it
  admits 1 of 708 real prompts on the maintainer's machine, so prompt-level
  local routing was **not** wired. `architecture/Q15_HARNESS_PLAN.md`.

## [15.1.0] - 2026-09-23

Remediation plan II, the half that needed measuring before it could be fixed.
A minor release: routing behaviour changes in three places, each measured
before and after on the population it affects, plus one root-cause analysis
that was inventing findings out of a NULL column.

Every number below carries its n and the file that produces it.
`scripts/measure_low_signal_rate.py` re-derives the headline 49.8%.

### Changed — routing behaviour

- **S3b · A relative `that`/`which` is no longer read as a deixis.**
  "write a regex **that** validates emails" was classified context-dependent
  because `that` matched the anaphora pattern. A relative pronoun points inside
  its own sentence, not at the user's state, so it is masked before the deixis
  check when followed by something verbal. Measured on a 13-prompt labelled
  set: false positives **5/12 → 0/12**, recall **11/13 → 12/13**. The
  `xfail(strict=True)` carrying this in the S3 parity test is removed.
- **S4a · The bandit trains only on rows whose origin was recorded.**
  `aggregate_stats` now filters `provenance = 'runtime'`. On the development
  ledger, **1387 of 1601** rows were pre-provenance placeholders carrying a
  single latency value (500ms) and a flat $0.01 — 87% of what the bandit
  learned from was not measurement. Excluding them does not remove a model
  from routing (`reorder()` explores from `candidates`, not `eligible`).
  Measured delta: the top pick was unchanged; the two models dropped were
  already ranked last on the placeholder data.
- **S4b · A tie in the reward breaks toward the faster model.**
  Three free local models tied at identical expected value and `max()` resolved
  it by iteration order, landing on `qwen3.8:latest` at **54.3s** over
  `lfm2.5:8b` at **10.1s**. Latency is a **tie-break, not a cost term** — a
  cost term needs a $/second rate and no trusted row is paid, so any rate would
  be a guess embedded in routing policy. A tie-break cannot reorder any pair
  whose expected values differ. An unrecorded latency sorts **last**, never
  first.

### Fixed

- **A classifier confidence that was never recorded was reported as 0%.**
  `routing_decisions.classifier_confidence` is NULL for **213 of the 214** rows
  with trusted provenance. `retrospective.py` coerced that NULL to `0.0`, which
  is below every threshold, so each of those decisions was flagged
  `LOW_CONFIDENCE` and promoted by `classify_root_causes` to a
  `CLASSIFIER_ERROR` at confidence "High" with the evidence string
  *"Classifier confidence 0%"* — 213 certain findings manufactured from a
  missing column. The same coercion averaged NULLs into `avg_confidence`,
  making it roughly *the share of rows that recorded one*. An unrecorded
  confidence now gets its own `CONFIDENCE_UNMEASURED` flag and is counted
  separately; `avg_confidence` is computed over the decisions that have one and
  ships with its denominator. A genuinely measured `0.0` is still
  `LOW_CONFIDENCE`.

### Added

- **The low-signal fall-through is counted and read.**
  `classify.low_signal_classifications()` returns `(decided_by_default, total)`
  — the numerator with its denominator, because 12 fall-throughs is a healthy
  hook and a catastrophe in a gateway that served 12,000 requests. Registered
  in `counter_registry` and rendered by `llm-router doctor`, alarming on the
  *share* (≥25%); `0 of 0` reads as Unknown, never as a clean 0%.
- **`scripts/measure_low_signal_rate.py`** — the script every published form of
  the 49.8% came from, committed so the figure can be re-derived rather than
  believed. It reuses `scripts/groundtruth/sources.py` for the drop rules;
  two ad-hoc parsers of this repo's traffic have already disagreed.

### Measured, and deliberately not fixed

- **Half of all traffic is routed by a default, not by a classification.**
  Entered the plan as "word order changes the route" (`what is 17 * 3?` →
  `query`, `tell me what 17 * 3 is` → `analyze`). Measuring it first showed the
  framing was wrong. Only the **gateway** is order-sensitive (10/14 on a
  14-pair paraphrase corpus); the router and hook are 0/14. Both prompts score
  **zero in every category**, so `policy.low_signal_default` decides — `query`
  for the hook and router, `analyze` for the gateway, which is right by luck
  rather than by measurement.

  Over **n=1571 real prompts** (the CLAUDE.md drop rules applied first, which
  removed 1389 records): **41.4%** score zero, **49.8%** are decided by a
  default, and the gateway and hook return a **different** task type for
  **49.8%** of them.

  `ClassifySignal.confident` had recorded this since it was introduced and had
  **zero readers in `src/`**.

  **The route is unchanged.** Both candidate fixes were measured and refused:
  adding `(?:tell|show) me (?:what|how|…)` to the query intent takes gateway
  order-sensitivity 10/14 → 2/14 but pulls genuine analyze/code work into
  `query` 0/8 → 2/8 (gateway) and 2/8 → 4/8 (router and hook); flipping the
  gateway default re-routes half its traffic on no evidence that `query` is
  right for it. A labelled target-distribution set does not exist, and a
  proxy split has already misled this project by 4.25 points. The K4
  `xfail(strict=True)` therefore stands.

- **The `or 0` coercion class is sized but not swept.** 217 matches in
  `src/llm_router`. Most are benign sums; the dangerous ones are those compared
  to a threshold or averaged, which is what S9 was. A 217-site sweep is a
  refactor, not a remediation — the right shape is a lint, and that is its own
  task with its own red-check.

- **The low-signal counter is in-process.** `llm-router doctor` classifies
  nothing, so it renders `Unknown` there — honest, but it means the counter is
  load-bearing only where writer and reader share a process (the hook, and the
  gateway). Closing it properly means logging the outcome on the hook's
  existing `prompt_len=` line, which belongs with consolidating the hook's
  duplicated classifier rather than bolted on before it.

## [15.0.1] - 2026-09-23

Remediation plan II — what attacking the 15.0.0 release found. Patch release:
no routing behaviour changes, no money-figure changes.

### Fixed

- **`llm-router doctor --audit` no longer inflates the counter it reports.**
  Measured on a clean install of 15.0.0, three consecutive runs of a read-only
  diagnostic against unchanged state:

      fail_open_events: 4  ->  8  ->  12

  An operator investigating a high count was making it higher. Root cause:
  `_column_exists` consulted a hand-maintained allowlist of nine table names
  that had drifted — `codex_usage`, `gemini_usage` and `migrations` were all
  migrated by the module and none was on the list. An unmatched table returned
  `False`, meaning "the column does not exist", when the truth was "I cannot
  tell", so the `ALTER` ran, hit `duplicate column name`, and the failure was
  recorded as a swallowed exception.

  Two fixes, independently sufficient: the allowlist is replaced by a strict
  identifier pattern plus a parameterised `sqlite_master` lookup (neither can
  drift), and `_safe_migrate` treats `duplicate column name` as SUCCESS,
  because an idempotent migration that no-ops is the migration working.

  **If you have been reading `fail_open_events`, the figure included your own
  `doctor` runs.** Reset and re-measure.

- **`CHZ-FO-COST-MIGRATE-ALTER` is no longer the loudest code in the
  fail-open counter.** It was counting normal operation. A counter whose
  baseline is normal operation cannot signal abnormal operation.

- **`llm-router status` renders its markup instead of printing it.**
  `rich.text.Text(...)` does not parse markup; `Text.from_markup(...)` does.
  Four call sites in `ui/status_premium.py` built a markup string and passed
  it to the constructor that renders it literally — on the first command the
  README sends a new user to. The header was the visible one; fixing it
  exposed 30 more tokens from three other sites.

### Privacy

- **83 occurrences of a developer's home directory removed from committed
  documents** across `audit/` and `Docs/`, 63 of them in one archived file.
  An audit artifact should record the shape of a machine, never whose machine
  it was. Enforced by a test that scans every committed markdown file.

### Added — tests that pin what was found

- Every registered counter is asserted to read the same value twice in a row,
  and to read zero on a fresh database. Both properties existed for exactly
  one counter (`hook_liveness.orphan_count`) and were never generalised.
- The routing layers' agreement contract is pinned: a context-dependent prompt
  must write no pending enforcement state, so a tool no routed model can use
  is never held.

### Known, recorded, not fixed

Three findings are carried as `xfail(strict=True)` rather than fixed, because
each changes which prompts route or which model answers — and that is measured
on the target distribution or not at all:

- **The context-dependence detector over-fires** on incidental deictics
  ("write a regex **that** validates emails"). `enforce-route.py` removed its
  own re-check for exactly this reason; the detector itself was never fixed,
  so every remaining consumer still over-fires.
  **FIXED in 15.1.0** — see S3b below.
- **The routing reward is blind to everything but dollars.**
  `expected_value = success_rate * ANSWER_VALUE_USD - avg_cost`, and every
  local model costs zero dollars — so between two free models the reward picks
  the higher success rate. **CORRECTED 2026-09-23:** the 15.0.1 text said
  "which is the larger model". That is wrong — measured on the table the
  bandit actually reads, the 8B model outranks the 30B (EV 0.05000 vs
  0.04632), because the 30B's success rate is lower. The mechanism was right;
  the example was backwards. A local model's real cost is memory and latency;
  `avg_latency_ms` is already collected on every stats row and the reward does
  not read it. **PARTIALLY ADDRESSED in 15.1.0** — latency is now a tie-break,
  not a cost term; see S4b below.
- **Word order changes the route.** `what is 17 * 3?` classifies as `query`;
  `tell me what 17 * 3 is` falls to `analyze` — the more expensive tier.
  **REFRAMED in 15.1.0:** measured, this is not about word order and it is not
  a small finding; see "Half of all traffic is routed by a default" below.

## [15.0.0] - 2026-09-22

Remediation of the 2026-09-22 adversarial audit. Security fixes that need no
attacker and no unusual input, money figures that now mean what they say, and
the instrumentation that makes the next audit cheaper.

### Why this is a MAJOR version

Three user-visible behaviours change, and one of them will look like a
regression until you read the reason.

**Your reported savings will DROP, possibly to near zero.** Money surfaces now
exclude rows whose provenance was never measured, fail-closed
(`COALESCE(is_simulated, 1) = 0`). A row written before provenance existed was
never certified as production, and counting it asserts that it was on no
evidence. On the development machine this took the counted set from 285 rows
to 23. **Nothing was lost and no routing got worse** — the old number included
rows nobody could vouch for. `llm-router doctor --audit` prints the split so
you can see exactly how many rows are excluded and why. New rows carry
provenance from the moment they are written.

**Savings can now be NEGATIVE.** Ten clamped subtractions were removed,
including one in the shareable savings card and one in the web dashboard's
headline tile. Routing that cost more than the baseline now renders as a loss.
The clamp is precisely what stopped anyone finding out.

**The gateway now refuses requests it used to answer.** `/v1/responses`,
`/api/chat` and `/api/generate` return 400 for a request carrying `tools` or
`tool_choice`, matching `/v1/chat/completions` and `/v1/messages`. Previously
they discarded the tool definitions and returned fluent prose with
`finish_reason: "stop"` — a well-formed answer to a question the client had not
asked. If you were relying on that 200, you were getting the wrong answer.

### Security

- **Credentials no longer reach disk in the clear.** `attempt_log` is written
  through the canonical scrubber and created at `0600` rather than
  `0644`-then-chmod. Measured against the previous code, a GitHub PAT, an AWS
  key id, an AWS secret, a Slack token and a bearer token all landed intact in
  a world-readable file. Existing files are repaired on next write.
  **Truncation is not redaction** — `reason[:80]` previously shortened a
  100-character key enough to defeat an exact-match search while leaving 80
  characters on disk.
- **Nothing leaves the machine unscrubbed.** `alerts.emit_alert` POSTs its
  detail dict to a webhook; a live capture carried a Postgres DSN with a
  plaintext password off the host. Scrubbed at one chokepoint, with a new
  pattern covering `postgres`/`mysql`/`mongodb`/`redis` URLs that preserves
  host and port so the alert stays actionable.
- **A model-chosen command no longer inherits your environment.**
  `agent_loop.run_command` and `tools/local_task` now use an env ALLOWLIST. A
  denylist strips only names it knows; an allowlist carries nothing that was
  not named.
- **SECURITY.md stops calling the allowlist a security control.** It is a
  typo-and-footgun guardrail. 10 of its 28 permitted programs are
  general-purpose interpreters and each is a complete bypass: `cat
  ../../.ssh/id_rsa` is refused, `python3 -c` reading the same file is not.
  Use OS-level containment for untrusted repositories.

### Added

- `llm-router doctor --audit` — every instrumentation counter, the canonical
  savings figure with its baseline and denominator, the provenance split, and
  the Ground Truth scope.
- **Ground Truth consent.** Capture is on after an explicit prompt at install
  and stays OFF in a non-interactive install — CI cannot agree to anything.
  Revocation is `llm-router gt-consent --revoke` and is recorded as a refusal
  rather than a deletion.
- `hook_liveness` — a killed hook is now distinguishable from one that chose
  not to route, via a marker that outlives the process.

### Fixed

- **`attempt_log` rotation erased concurrent records** — 0.6–5.3% loss under 8
  processes, silently, in valid JSONL.
- **A censored or truncated answer counted as a routing win.** `LLMResponse`
  had no `finish_reason` field at all, so a `content_filter` stop returned
  fluent partial text that passed the bandit's success check.
- **Verifier validation accepted worthless verifiers.** `len(answer) > 5`
  reached HIGH confidence with a 0% real kill rate.

### Known and stated, rather than fixed

- **"Routing preserves task success" is UNPROVEN.** The only mechanism that
  could measure it was off by default, so there is no dataset, and neither
  downgrade-regret nor upgrade-waste is computed anywhere. Recorded in the
  machine-checked claims ledger; it fails CI if anyone marks it proven without
  evidence.
- **17 of 20 savings surfaces still compute their own figure.** All are
  individually named with their exact divergence, and a 21st cannot be added
  without joining the registry.
- **Word order changes the route.** `what is 17 * 3?` classifies as `query`;
  `tell me what 17 * 3 is` falls through to `analyze` — the more expensive
  tier. Found by the new adversarial corpus and carried as a strict xfail.
  **SCOPED 2026-09-23:** true at the *gateway*, which is what the corpus row
  exercises; the hook and router fall through to `query`, the cheaper tier.
  And the cause is not word order — neither prompt scores anything, so the
  door's default decides. See 49.8% measurement above.
- **Ground Truth covers state-free prompts only**, because no replayer exists
  for repo-bound tasks.

## [14.1.0] - 2026-09-21

Ground Truth accumulation: normal routing now produces replayable, verifiable
evaluation candidates. Adds one environment variable and one ledger field.
No MCP tool is added or removed, and model-selection policy is unchanged.

### Why this is a minor version

Nothing public was removed or renamed. `RouteLedgerRecord` gains fields and
`schema_version` moves 2 -> 3; readers already normalise by version, and
quality denominators test `>= 2` rather than `== 2` so v3 rows are counted
exactly where v2 rows were.

### Added

- **Ground Truth accumulation**, off by default behind `LLM_ROUTER_GROUND_TRUTH=1`.
  A routed task is assessed for replayability and verifiability at capture
  time, its replay envelope is captured, and it is admitted to a candidate pool
  or rejected with a stated reason. Wired into `_finalize_successful_route`,
  the single choke point every success path already passes through.
- **Ledger traceability (schema v3).** `session_id`, `prompt_sha256`,
  `response_sha256`, `latency_ms`, `complexity`, `classification_method`,
  `verification_type`, `verifier_name`, `capture_ref`. Content is hashed, never
  stored: the ledger still persists no prompt or response text. The contract is
  `route_id -> one record` and `prompt_sha256 -> the captured task`, both exact
  key lookups, with no timestamp used as a join anywhere.
- **Explicit provenance.** Every row records whether it came from a test or
  benchmark run, set from `LLM_ROUTER_SYNTHETIC` or pytest's own marker, never
  inferred from a model name or session id. Rows predating the field are
  treated as unknown provenance and excluded from evaluation rather than
  assumed to be production.
- **Verifier authoring assistant.** Proposes an acceptance contract and a
  verifier for a candidate, validates it by mutation, and requires human
  approval before it can grade anything.
- `scripts/groundtruth/` — corpus extraction, eligibility, replay envelope,
  candidate pool, sampling, verifier authoring, and CLI reports.

### Fixed

- `summarize()` filtered `schema_version == 2`. The bump to v3 would have
  emptied every quality denominator while still reporting a clean-looking 0%
  instead of a missing measurement. Now `>= 2`, with a test whose only job is
  to prove an equality filter would have dropped the row.

### Privacy

Prompt text is written only under `LLM_ROUTER_GROUND_TRUTH=1`, and only after
`secret_scrubber.scrub_text()` — the canonical scrubber — has run. Scrubbing
fails closed: if that module cannot be imported, nothing is written. Home
paths, emails and public IPs are additionally scrubbed; person and customer
names need `LLM_ROUTER_CAPTURE_DENYLIST`, because no regex can detect them.

### Known limitations

- The candidate pool is empty until the flag is enabled; none of this can be
  backfilled from existing telemetry.
- The verifier assistant's existing-test search matches on file stem and is
  noisy on common words. Tracked, not yet fixed.

## [14.0.0] - 2026-09-18

Five prerequisite defects in project scoping and grounding measurement, and a
new `semantic` package. Adds one CLI command (`llm-router semantic`) and four
environment variables. No MCP tool is added or removed, and model-selection
policy is unchanged.

### Why this is a major version

No public name was removed or renamed, so the repository's own semver gate
reports `PATCH` as the minimum. The gate compares the surface, and the surface
is not what changed here — **the default behaviour is**:

- **Source retrieval is on by default.** Every routed prompt now carries a
  `<repository_evidence>` block it did not carry before. Nobody asked for that,
  and it changes what every model sees on every call. That alone is what a
  major version is for.
- **An unobserved command outcome is recorded as `unknown` rather than `ok`.**
  Chapter sealing requires `ok`, and most tool responses carry no exit code, so
  expect markedly fewer sealed chapters. Correct, and a visible drop in a
  number somebody may be watching.
- **OKF writes less.** Definitions are verified against the file before being
  stored, so a store that previously accumulated unverified symbol names stops
  growing as fast — and some of what it already holds is wrong.

Anyone who wants the previous behaviour sets `LLM_ROUTER_SEMANTIC_SOURCE=off`.

Two notes for whoever reads the release tooling's output. The semver gate
reported "public surface unchanged" despite this release adding a CLI
subcommand, because it extracts top-level `def`/`class` names from eleven fixed
files and a subcommand is a string in a `choices` list — the same blind spot
its own comment records from 13.3.1. And `sync-versions.py` did not update
`npm/package.json` while `verify-version-sync.py` checked it; fixed here.

### Fixed

- Indexing a project while the process sits in a different one no longer writes
  the first project's documents into the second. `index_project(root=B)` called
  from inside A reported B's store in its return value and wrote A's directory;
  six OKF write paths recomputed their destination from the process cwd, and
  two of them had not been named in any prior audit of this bug.
- OKF no longer records what a file defines without reading the file. Two
  independent regexes produced a list of paths and a list of symbols, and the
  writers paired the first path with every symbol, so a reply mentioning a
  module that does not exist alongside a function defined elsewhere became a
  stored, retrievable claim that one defines the other.
- The grounding benchmark's scorer no longer accepts a wrong directory.
  `wrong_directory/okf.py` scored correct against `src/llm_router/okf.py`
  because the lenient rule was a substring test — it was meant to forgive an
  omitted directory and equally forgave a wrong one. Strict and lenient are now
  reported separately, each with its `n`.
- `router.py` now passes the project root into context preparation, so the
  code-context branch — gated on that argument and therefore unreachable for
  every routed call — can run. It also attaches context through the shared
  choke point instead of its own copy; it was the last path exempted by name.
- One project now resolves to one scope. Five modules answered "which project
  is this" privately, with two environment variable names and two fallbacks, so
  running from a subdirectory put OKF at the repository root and the caches on
  the subdirectory. `result_cache` was the one that did not heal: it hashes into
  a file path rather than a TTL'd column, so a divergent spelling orphaned a
  database nothing reopens.
- A command whose outcome could not be determined is recorded as `unknown`
  rather than `ok`. The fallback branch meant unobserved commands became
  evidence that a fix succeeded, and — because chapter sealing requires `ok` —
  quietly decided that unverified commits were milestones. Expect noticeably
  fewer sealed chapters; most tool responses carry no exit code.
- The biography no longer freezes at forty facts. Once the document was full,
  every durable fact learned afterwards was discarded in silence. The cap is now
  on the readable view, which says when it is showing a subset; the records
  themselves are uncapped and individually retrievable.
- Quality-escalation's short-prompt guard measures the user's prompt rather than
  the assembled context, so attached material cannot make a short prompt long.
- Tests no longer read the developer's live session store. `LLM_ROUTER_HOME` was
  documented as the sandbox for this and nothing set it, so a test asserting on
  context contents could fail carrying text from whatever you were doing.

### Added

- `llm_router.semantic`: a per-project derived index (SQLite, `ast`-extracted,
  rebuildable) and typed engineering-experience records carrying two timelines
  and four independent state axes. Retrieval produces an evidence pack that
  names what it omitted and why, renders filed prose inside an explicit
  untrusted region, and surfaces contradictory records rather than letting the
  newest win.
- `llm-router semantic index|status|explain|lessons|seed`.
- `LLM_ROUTER_SEMANTIC_SOURCE`, `_HISTORY`, `_INTERVENTION` (`off`/`shadow`/`on`,
  all `off` by default; `shadow` is byte-identical to `off`) and
  `LLM_ROUTER_SEMANTIC_ARM` (`B`/`C`/`D`/`M0`/`M1`/`M2`).

### Measured

Grounding, after the fixes above and before any semantic-layer work, so that a
scope fix cannot later be credited to a graph: **0/60 → 40/60 (+66.7%)** with
OKF context, on questions derived from symbols the repository defines exactly
once. Strict and lenient scoring agreed exactly (40 = 40). Material was
retrieved for 41 of 60; on those alone, 0% → 97.6%. Model `qwen3-coder:30b`,
seed 7. Full provenance and per-question detail in
`docs/measurements/2026-09-18-grounding-corrected-baseline.md`.

Arms A/B/C/D, same n=60 and same scorer, paired
(`docs/measurements/2026-09-18-semantic-arms.md`):

| arm | | correct |
|---|---|---|
| A | no context | 0/60 |
| B | OKF context — the corrected baseline | 40/60 |
| C | semantic pack, no traversal | **58/60** |
| BC | OKF + semantic — what default-on ships | 58/60 |

C beats the baseline by +30.0 points, 18 discordant pairs all one way, McNemar
exact p=7.6e-06 — and uses a median 122 of its 2000-token budget.

**Traversal was deleted, not switched off:** arm D answered all 60 questions
identically to C and cost 0.2s more. The blueprint's adoption gate for the
graph was +3 points over the corrected baseline; it scored zero, so the
expansion code, hop caps and high-degree penalty are gone — 97 lines net.

Three harder strata were then derived and run at n=60 each
(`docs/measurements/2026-09-18-harder-strata.md`), and they bound the claim:

| stratum | B | C | BC |
|---|---|---|---|
| symbol — query names the identifier | 41/60 | 60/60 | 60/60 |
| decoy — basename in two or more directories | 42/60 | 59/60 | 59/60 |
| concept — docstring, identifier hidden | 2/60 | 1/60 | 3/60 |
| absent — symbol does not exist | 60/60 | 60/60 | 60/60 |

The win survives same-basename decoys under a scorer that rejects a wrong
directory. It does **not** generalise to questions phrased in prose: on
`concept` everything collapses to a floor and the layer does not rescue it. On
`absent` nothing hallucinates, and the semantic arm retrieves nothing at all —
the correct behaviour for a symbol nobody wrote.

**It does not show improved task completion**, and the M0/M1/M2 history track
is still scaffolded and unrun, which is why history and intervention remain off
while source retrieval does not.
See `docs/decisions/0002-semantic-layer.md` for what was deliberately narrowed
and what is known to be broken and unfixed.

## [13.3.2] - 2026-09-16

Fixes to repository and session context, draft validation, routing telemetry,
and local-task execution defaults. No MCP tool or CLI command is added or removed.

### Fixed

- Codex installations now register and trust a Stop hook that shows estimated
  savings today and over the router's lifetime after every turn. The Codex
  plugin uses the same reporter, which preserves ongoing session context and
  reads the existing shared ledger across hosts.
- Routed execution paths share repository knowledge injection. The MCP path now
  loads session events from the caller's project even when the server starts in
  another directory. Fresh Git facts supply branch, commit and working-tree state.
- Session context retains more Bash, Write and Edit output. Knowledge retrieval
  recognizes document labels already mentioned in the same session, and session
  startup refreshes the repository index.
- Draft grounding checks bare filenames and checks symbols against the working
  tree, reducing both invented references and rejection of newly written code.
- Hook classification stays aligned with the shared classifier. Ollama model
  discovery and checked-in startup settings replace guessed model defaults.
- Routing telemetry records failed attempts and lost context evidence. Provider
  success signals require usable output, and unreliable providers are demoted.
- `llm_local_task` now defaults to proposing writes (`apply_writes=False`) and
  no longer implicitly enables unrestricted commands when writes are applied.
  Acceptance checks remain separate from worker execution.
- Removed unused policy-version code and restored regression coverage while
  triaging quarantined tests. Added an import regression check that records two
  existing control-plane modules with missing enterprise dependencies.

### Release checks

- Added a public-surface version check to the pre-release gate and a local gate
  covering lint, generated plugin files, mutation configuration and tests.
- Updated measurement guidance and benchmarks. This release makes no new token
  savings or routing-rate claim.

## [13.3.1] - 2026-09-13

Measurement, and what measuring exposed. Nothing here raises the routing rate;
several entries lower a number this project previously reported.

**A patch release that adds a tool.** `llm_local_task` is new API surface, and
the semver this file declares makes that a MINOR bump. Shipped as a patch at the
maintainer's explicit instruction — recorded here so the version number does not
have to carry the explanation by itself.

### `llm_local_task` — one Claude turn for a whole task

A new MCP tool (consolidated tier). Claude submits an objective and reads one
result; every read, edit and command in between runs on a local model and never
enters Claude's context. The prompt-time loop could not do this: it fires inside
UserPromptSubmit, is bounded at 90s, and answers a prompt rather than owning a
task.

Three properties, each from a measured failure:

- **A typed terminal status.** The loop returns "Agent reached maximum
  iterations" on exhaustion and `quality_ok` scored that string as a pass in the
  2026-09-12 benchmark. Exhaustion is now `incomplete` and can never be
  `verified_complete`.
- **Acceptance checked by a supervisor subprocess**, never by the worker. The
  same benchmark caught the local model diagnosing a bug correctly in prose and
  never changing the code.
- **No cloud fallback.** Ollama down or budget gone returns a typed failure with
  whatever was staged.

Verified end to end against Ollama: a failing test fixed, edited on disk, check
passed, in one Claude turn.

**What it does not do: make the model better.** Measured on the 11-task brutal
suite across four local configurations — raw loop, raw loop with two previously
broken tools fixed, the service with no acceptance check, and the service with
one — every run scored **8/11 with the same three failures**. Codex and Claude
score 10/11. Budget, checking and task-ownership move none of it. Ship it for
the turn saving; scope the work to failures you would catch.

### Savings are credited only when a Claude turn was actually replaced

`log_direct_savings()` ran unconditionally ~39 lines before the `_turn_blocked`
check that decides whether the routed answer substituted for anything. In echo
mode — the default — nothing is substituted, so **$0.426410 was credited on
2026-09-12 against drafts the debug log recorded as `DRAFT UNUSED`**. The call
now sits below the check and carries `realized`; a non-realized route still
writes a row, at zero, tagged `mode='echo'`, because a missing row is
indistinguishable from the hook never running. New nullable `mode` column;
existing rows stay NULL rather than being backfilled as verified.

### Execution and routing traces

`LLM_ROUTER_TRACE=1` (or `LLM_ROUTER_TRACE_FILE`) writes a JSONL fact stream.
The agent loop emits every model round-trip, every tool call with arguments and
result, and a terminal reason: `max_iterations`, `budget_exhausted`,
`repeated_identical_call`, `llm_unreachable`,
`final_text_without_any_tool_call`. The routing hook emits the prompt, the
decision (task type, zone, pressure, `needs_tools`, the chain actually built)
and the outcome — including `substituted`, which says whether Claude's turn was
replaced or merely decorated.

`scripts/trace_view.py` renders it: `--verdict` for "did the model do the work",
`--routes` for "did routing replace a Claude turn, or just route". Both
distinctions have historically been miscounted here.

### Fixed

- **`list_files` and `search_files` were broken under any symlinked project
  root.** They reported paths relative to the unresolved root while
  `_resolve_path` validated against the resolved one, so on macOS — where `/tmp`
  is `/private/tmp` — both returned "is not in the subpath of" for directories
  the model could legitimately read, and it burned its iteration budget
  retrying. Every local benchmark score before 2026-09-13 was measured this way.
  Re-running with the fix gives the same 8/11.
- **Interception admits `&&` conjunctions of allowlisted reads.** Every segment
  must independently pass the allowlist, so `git status && rm -rf build` stays
  refused without this module judging which half is dangerous. Honest effect:
  eligibility moves from 8/492 to 12/492 of real commands; 436 of the rest are
  pipes and redirects. It did expose a real bug — a leading `cd` was being
  stripped by `effective_command`, which would have returned a different
  directory's file as the answer.
- **The benchmark harness measures `time.monotonic()`.** A macOS Maintenance
  Sleep advances the wall clock and not the monotonic one; one task was recorded
  as 918.6s when 902s of that was the laptop asleep.

## [13.3.0] - 2026-09-12

Local models can now do the work, and the tool calls that cost the most can be
answered before they reach Claude. No savings figure is claimed here on purpose —
see "What is not claimed" below.

### The agent loop actually works now

`qwen3-coder:30b` emits tool calls in Qwen's XML dialect while leaving Ollama's
structured `tool_calls` field empty. The loop read that as "the model only
chatted" and discarded correct calls. Fixing the parser moved a 5-task harness
from 2/5 to 5/5, and the loop now scores 40/40 across shell, code navigation,
file edits and vision.

- Qwen XML tool-call dialect is parsed (`_repair_xml_toolcalls`)
- Grammar-constrained decoding via Ollama `format=`, with `finish` as a real
  tool so a constrained model can stop — without it the grammar could only
  express "call something" and the model repeated one read to exhaustion
- An identical repeated call is interrupted rather than run again
- A wall-clock budget bounds the loop (default 90s); 15 iterations at a 60s
  per-call timeout was a 15-minute worst case inside UserPromptSubmit
- Context discipline: tool results capped, `read_file` takes offset/limit, the
  task is re-stated every turn, and the harness evicts oldest TOOL RESULTS —
  llama.cpp evicts the system prompt instead, silently
- The loop runs by default (`LLM_ROUTER_LOCAL_AGENT_LOOP`)

### Local interception (opt-in)

A PostToolUse hook cannot replace a tool result — verified live. A PreToolUse
`deny` carrying the answer can, so that is the mechanism.

- `image_intercept`: a Read of a raster image is answered by a probed local
  vision model; the image is never loaded
- `bash_intercept`: an allowlisted read-only command is run by the hook and its
  output compressed; the uncompressed output never enters context
- Both OFF by default, configured in `~/.llm-router/routing.yaml` (a hook never
  sees a shell export)
- Every interception is logged to `~/.llm-router/intercepts.jsonl`

### Vision capability is measured, not assumed

`vision_registry` probes each model that advertises vision by asking it to read
a randomly generated code from an image, three times, exactly. Verdicts are
cached and re-probed when the installed model set changes. Nothing is hardcoded
per model, and anything unproven routes to Claude.

### Safety

- `agent_writes`: local edits default to `propose` — a diff, not a write — and
  `apply` journals the previous contents first, refusing any edit it cannot undo
- `run_command` defaults to an inspection allowlist; compound commands are
  refused whole rather than parsed
- A cross-session leak in `mcp_roots` is fixed: the cache was keyed on
  `id(session)`, a memory address, so a recycled address served one session's
  project root to another

### Measurement

- `effective_rate` in the routing report counts drafts USED, not produced — the
  old rate counted production, which is how a fully-routed-looking session drove
  quota from 49% to 79%
- `POST /ground` exposes the grounding check (docs/GROUNDING_API.md)
- Compression no longer truncates blindly: an unrecognised output shape declines
  rather than returning `output[:200]`, which had been silently dropping data

### What is not claimed

No token-savings figure. Two figures reported during development were
projections from offline replays rather than observations, and both were wrong.
`scripts/intercept_report.py` reads the interception log so the next figure is
measured from real use. Until then this release ships the capability and no
number.



## [13.2.2] — The injected block repeated itself (2026-09-10)

### Fixed

- **Every retrieved document reached the model twice.**
  `_write_source_concept` stores one string in two fields — `description` is
  `summary[:120]`, the body is the full `summary` — and `as_context_block` emitted
  both. So a SourceFile arrived as its symbol list truncated mid-name, followed
  immediately by the same list in full:

      ## [SourceFile] src/llm_router/router.py
      Defines: route_and_call, build_chain, ..., _format_subprocess_chain_error,     <- 120 chars
      Defines: route_and_call, build_chain, ..., execute_chain, _call_text, ...      <- 1003 chars

  With up to three documents injected inside a 3000-token draft budget, that is
  real space spent on a duplicate — and the truncated copy is worse than useless: a
  name cut in half is a name the model can complete wrongly, which is the failure
  the grounding checks exist to catch.

  Deduplicated at render, not at write, so documents already on disk benefit
  without a re-index. The body wins when one string contains the other, since the
  description is the truncated one; a description that says something the body does
  not — curated notes, the model catalog — is kept. Measured on a live document:
  765 -> 644 characters.


## [13.2.1] — The index eroded itself (2026-09-10)

### Fixed

- **`okf index` decayed as you worked.** `_write_source_concept` overwrote the
  document rather than merging into it. That was harmless while only routed answers
  enriched, which was rare — and 13.2.0 put enrichment on `context-capture.py`,
  which fires on EVERY tool call with a cap of 10 symbols and sees only what the
  tool printed. So one tool result mentioning one function replaced that file's
  entire indexed document with that single symbol.

  Measured after a few hours of ordinary work, against 1069 indexed documents:

      docs with FEWER symbols than the file defines: 19

      src/llm_router/hooks/auto-route.py    stored   1 / real  91
      src/llm_router/cost.py                stored   1 / real  63
      src/llm_router/router.py              stored   1 / real  52
      src/llm_router/okf.py                 stored   1 / real  36

  Silent, and it points the wrong way: the eroded files are the large central ones,
  because those are what tool calls keep touching, so the documents most likely to
  be asked about were hollowed out first. It also disguises itself — a query that
  worked right after indexing stops working an hour later, and the cause looks like
  a scoping problem.

  Writes now merge. Only `index_project` may shrink a document, because only it
  read the whole file; a writer that saw a fragment can no longer assert that the
  file contains less than it does.

### Upgrading

Anyone who ran `llm-router okf index` on 13.2.0 has a partly eroded store. Re-run
it once after upgrading; the merge keeps it correct from then on.


## [13.2.0] — Routing recovery: the timeout, the contamination, the fragmentation (2026-09-10)

Sustained routing had fallen from 31-39% to ~2% of real prompts (5 successes in 246,
2026-09-04..09-10). Three separate defects, each found by measuring rather than
reasoning, and one non-fix that the measurement talked us out of.

### Fixed

- **`OLLAMA_TIMEOUT` defaulted to 4s, which no local model could meet.** Measured
  p50s are 11.4s (lfm2.5:8b), 15.8s (qwen3-coder:30b) and 28.5s (qwen3.8); even
  "Say OK." took 6.6s warm. Every DIRECT attempt aborted at exactly 4s and fell
  through — 86 of 246 real prompts in one week. Now 45s.

- **OKF retrieval contaminated prompts it had nothing to do with.** A `capital of
  Portugal` question came back carrying another project's source doc and a model
  capability sheet: the MCP server's cwd is `$HOME`, which has no `.git`, so every
  project collapsed into one scope; the shared model catalog was a retrieval root
  and matched everything; and the relevance floor was `> 0`. Scope now honours
  `LLM_ROUTER_PROJECT_ROOT`, the catalog is out of task retrieval, and matching is
  weighted and token-based.

- **Session events were scattered across project buckets and mostly unreadable.**
  `_project_id()` hashed the raw cwd, so a session that moved between directories
  split its log while `build_session_context` read one bucket — 451 recorded, 284
  readable. Now resolved to the repo root.

- **Claude's own answers were never persisted**, only routed ones, so the stored
  conversation held every question and no conclusion.

- **Four import-time path bugs of one class**, where a module-level constant froze
  `$HOME` before any isolation override could apply: the auto-route debug log (227
  test rows in the production log), `cost.savings_log_path()`, `receipt_store`,
  and session scoping. A path computed at import ignores every isolation mechanism
  added afterwards.

- **The gateway rejected every normally-configured SDK client.** Honouring the
  caller's `model` forwarded a bare name to `model_override`, which requires
  `provider/model` and 400s otherwise. Each wire endpoint now qualifies with its
  own provider; `auto` still means "you pick".

- **`LLM_ROUTER_SESSION_CONTEXT=local` silently disabled context entirely** — the
  hook passes `target_provider="local"`, which was missing from the allowlist.

- **The session store re-ingested its own injected context.** `record_event`
  stripped llm-router's sentinel but not OKF's `<knowledge_context>`, so retrieved
  documents were recorded as though the user had typed them.

### Added

- **`llm-router okf index`** — index a repo's tracked source into the knowledge
  store. The store could previously only be filled by a successful routed call,
  which is a deadlock; it held 2 documents after weeks of use, and 1063 after.
- **`llm-router sessions status` / `merge`** — recover events stranded by the old
  scoping bug. Dry-run by default, timestamp-ordered, originating shards kept.
- **Grounding checks on routed drafts.** A draft citing a file or calling a
  function that exists neither in its context nor in the index is discarded and the
  turn falls through, rather than being shown.
- **Host `env` propagation** — `LLM_ROUTER_*` settings now travel in each host's
  MCP config, so Cursor/OpenCode/Codex get the tuned models instead of falling back
  to one that is not installed. Credential-shaped names are never propagated.

### Not changed, deliberately

- **The context-dependent gate was left alone.** It skips ~48% of prompts and
  looked like the main culprit; its noun list even contains `agent`, matching every
  prompt about the user's own "Transfer Agent". Measured against 376 real prompts,
  loosening it freed 36 — and almost every one genuinely needed local state
  ("commit this and show me the demo again"). Routing those produces fabrication,
  not savings. Seven of those prompts are pinned in a test so the next attempt has
  to argue with the measurement.

- **Consolidating session shards at runtime**, which would have recovered the rest
  of the fragmentation but turned `load_events` into a cross-project read path.

### Notes

Two mechanisms in this release were measured, found to be producing confident
fabrication, and fixed before shipping — an eligibility count is not a quality
measurement, and both times only real model output caught it.

---

The following also ships in 13.2.0; it was already on `main` unreleased when the
routing work landed.

### Fixed (host installation)

- **Codex → llm-router works, for the first time.** The installer wrote the
  MCP server to `~/.codex/config.yaml` (and an older path to `config.json`
  and `rules/llm_router.md`). Codex reads only `config.toml`, so no Codex
  session has ever seen llm-router. One writer now targets
  `[mcp_servers.llm_router]` in `config.toml` (via `codex mcp add`, TOML
  fallback), `hooks.json`, and a marked block in `AGENTS.md`, and removes the
  legacy entries when they are ours. The duplicate installer in `cli.py` is
  gone.
- **Codex hooks now actually run.** Codex 0.153 silently skips any hook in
  `hooks.json` without a `[hooks.state."…"] trusted_hash` record in
  `config.toml`. `llm_router.codex_host` computes the hash (verified against a
  real run) and install writes it for every hook it installs; doctor fails on
  a missing or stale record.
- `llm-router doctor --host codex` validated `config.yaml` and reported a
  broken install as healthy. It now checks `config.toml`, that the command
  can start, `codex mcp list`, hook trust, and `AGENTS.md`.

### Added

- **Routing defaults come from the seat table.** With
  `LLM_ROUTER_SUBSCRIPTION_PROVIDER` unset, the strongest logged-in seat is
  the subscription provider (Claude over ChatGPT over Google) and the other
  seats join the free bucket, so a Claude Max + ChatGPT machine sends cheap
  work to Codex and hard work to Claude from either host, with nothing
  configured. The env var still wins; `LLM_ROUTER_SEATS_AUTO=off` restores
  env-only behaviour.
- **`llm-router install --project`** writes a marked llm-router block into the
  repository's `AGENTS.md` and links `CLAUDE.md` to it (a copy on Windows; an
  existing `CLAUDE.md` file is kept and gets the same block), so Claude Code and
  Codex read one set of project rules.
- **`llm-router install` auto-detects Codex.** With no `--host`, a machine
  that has Codex gets it wired in the same run, and the seat table prints at
  the end. `--no-hosts` opts out; `--host codex` is unchanged. Gateway mode is
  opt-in (`--mode gateway`), no longer the Codex default.
- **Codex gets push routing.** A `UserPromptSubmit` hook injects the same
  `⚡ ROUTE:` hint Claude Code gets. `hosts/events.py` now carries the verified
  Codex prompt payload keys (fixture from a real run).

- **Seat detection.** `llm-router doctor` and the session banner now show which
  subscriptions this machine is logged in to (Claude via `claude auth status`,
  ChatGPT via `codex login status` plus the plan claim in the Codex login
  token, Gemini CLI, Ollama) and the free bucket derived from them. Cached in
  `~/.llm-router/seats.json`; kinds and plan names only, never tokens. A plan
  claim past its window is reported as stale but still counts, because login
  status is the fact and the claim a hint. First step of
  `guide/PLAN_DUAL_HOST_INSTALL.md`.

## [13.0.8] — Close the second co-owned-file guard (2026-08-31)

Closes #92.

### Fixed

- **The sandbox guard no longer whole-file-diffs `.claude/settings.json`.**
  The same structural problem #88 fixed for `~/.claude.json`, in the other
  `_REPORT_ONLY` entry. That file is co-owned — a live Claude Code
  session, another tool, or the user editing their own settings all write
  to it — so diffing it whole makes the guard fire on somebody else's
  change and blame whichever test sampled it at that instant. It had not
  flaked yet purely because of timing; the race was identical, and #88
  cost hours precisely because a false escape report looks exactly like a
  real one.

  The snapshot now compares only what `install()` writes:
  `mcpServers["llm_router"]`, `statusLine`, and hook registrations —
  with hooks filtered to commands naming this package, since the user's
  own registrations share those lists. Mutation-checked both ways:
  unrelated key churn, a third-party MCP entry and a user-edited hook are
  all ignored, while our own mcp entry, statusLine or hook being changed
  is still caught. Not an allowlist entry, which would have blinded the
  guard to the escapes it exists to detect.

### Known issues

An `-m slow` run still calls `uv build` three times (#91): three test
files each define their own module-scoped `built` fixture. Excluded from
the default addopts and from CI, so no standard run pays it.

## [13.0.7] — Test-suite honesty (2026-08-31)

Closes #82, #84, #87, #88. Every one of these is a test that reported
something other than what was actually wrong, and three of the four
issue titles turned out to be misdiagnoses corrected by reproduction.

### Fixed

- **~100 more `llm_router <cmd>` strings across `src/` (#82).** Follow-up
  to #72. A fresh scan found 225 raw matches across 86 files, not the ~89
  estimated; after the `ast`/`tokenize` pass that drops docstrings and
  comments, 100 were genuine user-facing violations across 44 files. The
  lint's file list grows from 2 to 46 — the ~42 files whose matches were
  entirely comment noise are deliberately excluded, since there is
  nothing there to guard. Two matches were prose that merely reads like a
  command; rather than start a suppression list they were reworded, so
  both files are genuinely clean instead of exempted. Nine more
  assertions across eight test files had the broken string encoded as
  expected output.

- **The concurrency test now has a budget it can meet (#84).** Filed as
  order-dependence; it is not. The project-wide `timeout = 30` is sized
  for unit tests, and this one forks six processes doing 200
  lock-serialized writes each. Under CPU load, pytest's own setup was
  measured at 22-27s, so setup plus call crossed 30s; the watchdog then
  unwound the test while workers were still writing and the enclosing
  `TemporaryDirectory` deleted the directory underneath them —
  indistinguishable from lost writes. Random ordering never leaked state
  into it; it only changed how much of the budget was left.

- **The build-artifact tests likewise (#87).** Same error string, a
  different cause — worth stating, because #84 had just taught the
  opposite lesson. Here pytest-timeout fires inside
  `subprocess.communicate`, blocked on a real `uv build` child: 16.8s
  idle, 76.4s under load. The suggestion that each test rebuilds the
  package was checked and disproved — the fixture is already
  module-scoped and builds once — so no session fixture was written. The
  global default stays at 30: three files needing more is a property of
  those tests, not of a ~6,900-test suite.

- **The sandbox guard no longer blames tests for another process's
  writes (#88).** Filed as tests escaping their sandbox to write
  `~/.claude.json`. Instrumenting every write path across ~10 full-suite
  runs never once caught this package touching that file. What changed
  between a failing run's before/after pair was `promptQueueUseCount`
  — a concurrently running Claude Code session's own bookkeeping. The
  tell was that an identical seed and test order passed and failed on
  back-to-back runs, which an in-process defect cannot do. The guard now
  diffs only `mcpServers["llm_router"]`, the one slice the installer
  owns; real escapes are still caught, and it is not an allowlist entry.

### Known issues

An `-m slow` run calls `uv build` three times, because
`test_sdist_excludes_quarantined_tests.py` and
`test_gh43_direct_execution_disclosure.py` each define their own
module-scoped `built` fixture. Excluded from the default addopts and from
CI, so it affects no standard run. `.claude/settings.json` has the same
whole-file-diff weakness #88 fixed for `.claude.json`; it has not flaked
yet.

## [13.0.6] — Remove the dead enterprise surface, and a credential leak (2026-08-31)

Closes #68, #69, #70, #71, #72, #74, #75, #79 — everything a repo-wide
audit turned up after 13.0.5, plus three bugs found while fixing them.

### Security

- **Importing `llm_router.providers` loaded an unrelated `.env` into the
  process (#74).** `litellm/__init__.py` calls `dotenv.load_dotenv()`
  unconditionally at import time when `LITELLM_MODE` is unset — and it
  defaults to `"DEV"`. With no argument, `find_dotenv()` walks upward from
  *the caller's own source file*, i.e. from inside
  `.venv/lib/.../litellm/`, not from the working directory. That walk
  climbs out of the project entirely and can reach a personal `~/.env`,
  whose contents are merged into `os.environ` for the life of the
  interpreter. Verified directly: `XAI_API_KEY` absent before the import,
  present after. Any credential in any `.env` above the virtualenv was
  being loaded without the operator asking. Fixed by setting
  `LITELLM_MODE=PROD` before importing litellm, which still honors an
  explicit setting.

### Removed

- **The enterprise surface (#68, #70, #71).** `llm_router.enterprise` is
  not shipped, and five modules depended on it — each converting the
  missing module into a failure at a different layer, all invisible by
  default. `LLM_ROUTER_RBAC_MODE=strict` made every route raise
  `AttributeError` before contacting a provider, so enabling the
  officially safer mode was a total outage rather than enforcement.
  `audit_routing.py` raised on every routed turn behind a blanket
  `except`, so the routing audit trail has never worked on any install.
  `llm-router audit verify`, the documented tamper-evidence gate, died
  with a raw `TypeError`. And `verify_enterprise.py` — the tool built to
  catch exactly this drift — was never wired into the CLI and crashed if
  invoked, which is why the other three went unnoticed.

  `commands/audit.py` is kept: its `misroute` subcommand is the only entry
  point for a live, unrelated feature. Default-mode routing is unchanged,
  proven by an identical 239-test router battery before and after.

### Fixed

- **An unrecognized `LLM_ROUTER_PROFILE` stopped the server booting
  (#69).** `RouterConfig.llm_router_profile` is bound by naming
  convention to that variable and validated against the routing tiers, so
  `LLM_ROUTER_PROFILE=enterprise` raised `ValidationError` at import time
  — before the startup checks that would have refused it with an
  actionable message. This was the third reader of that one variable, and
  the one that actually drives routing; #65 fixed only the display-only
  one. It now accepts `LLM_ROUTER_COST_PROFILE` as an alias and falls back
  to `balanced` with a warning naming both axes, without special-casing
  any particular value.

- **`install` and `doctor` printed a command that does not exist (#72).**
  37 user-facing strings said `llm_router <cmd>`; the binary is
  `llm-router`. Every failing doctor check named an unrunnable command in
  its `fix=` hint, and the headless Dockerfile snippet failed at its
  second line when copy-pasted. The docs-lint now covers these two source
  files, distinguishing a printed command from `import llm_router` by what
  follows the name, then dropping docstrings via `ast` and comments via
  `tokenize`.

- **Two unrelated bugs behind the "flaky tests" (#75).**
  `judge.evaluate_response_async` created an `asyncio` task and never
  awaited it or held a reference. On the session-scoped event loop, that
  orphan ran during a *later* test's awaits and hit whatever
  `litellm.acompletion` mock was installed — so a Claude model appeared in
  assertions expecting another provider. Not ordering at all: the sample
  rate is 0.1, so it fired on `random.random()`, which is why identical
  commits gave opposite CI results. Holding a reference is also a
  production fix — asyncio may collect a pending task nothing refers to.
  Separately, a test's own SQL stored `datetime('now','localtime')` where
  production stores UTC, so the reader's single `'localtime'` conversion
  shifted it twice.

- **A fixture that failed for ~100 seconds after local midnight (#79).**
  `test_routing_logic_uses_today_cutoff` seeded rows at `now - 100` and
  asserted they counted as today. A CI run starting at 23:58 UTC failed
  both Python jobs. Rows are now anchored to the day boundary.

### Internal

- The release tooling could not read this changelog: `extract_changelog_entry`
  matched only `## v1.2.3` headings, and the plugin lookup used the module
  name `llm_router` where the manifests say `llm-router` — the same naming
  confusion, in the tooling that ships releases. Both fixed in 13.0.5's
  release commit.

### Known issues

Underscore-CLI strings remain across the rest of `src/` (#82). One
order-dependent test remains in `test_session_store_concurrency.py` (#84).

## [13.0.5] — The nine issues opened this week (2026-08-30)

Fixes #59-#67. Three of them (#59, #60, #63) were retests of issues closed as
fixed in 13.0.4, where each fix had missed the real code path because it was
verified against existing DB rows or synthetic env rather than a fresh run.
Every fix here was built the other way round: the repro test written first,
observed failing, then patched, then mutation-checked by reverting the fix to
confirm the right test dies.

### Fixed

- **Session-scoped `set-enforce` never activated (#59).** `_session_enforce()`
  and `_run_set_enforce()` read a bare `CLAUDE_SESSION_ID`; Claude Code exports
  `CLAUDE_CODE_SESSION_ID`. `set-enforce` wrote the global `routing.yaml` every
  time while printing "(this session only)". Both now resolve through
  `session_store.resolve_session_id()` — writer and reader share one resolver,
  which is the property that was actually missing.

- **`routing_decisions` was always empty (#60).** `log_routing_decision` was
  gated behind `if classification_data:`, which `route_and_call` defaults to
  `None` and `tools/text.py` never passes — so the entire consolidated tool
  surface could not write a row. Fixed at the sink, covering `llm`, `llm_query`,
  `llm_code`, `llm_analyze`, `llm_generate`, `llm_research` and any future
  caller. Synthesized rows are marked `classifier_type="unhinted"` with NULL
  confidence, latency, budget and quality_mode, so an unclassified call cannot
  masquerade as a measured one. Ten `classification_accuracy is None` crash
  sites are fixed — four of them in `retrospective.py` itself, which neither the
  issue nor the plan had listed.

- **An unknown subcommand started the MCP server (#61).** `main()`'s final
  `else` was reached by any unrecognized token, so a typo launched the full
  stdio server and hung the terminal. The server now starts only with no
  arguments; anything else exits 2 with a `did you mean` suggestion.

- **The ensemble classifier failed silently on every call (#62).** `ensemble.py`
  hardcoded `ollama/qwen2.5:7b` with no check that it was pulled, so on a machine
  without it the classifier failed every time, degraded to the heuristic, and
  `doctor` still reported Ollama green. `doctor` and `verify` now compare both
  classifier models against `/api/tags`, normalizing the implicit `:latest` tag.
  `LLM_ROUTER_ENSEMBLE_SECONDARY` is new — the secondary had no override path at
  all, so suggesting one would have been false advice.

- **The statusline called a working setup an outage (#63).** The health check
  tested five cloud API keys and not `LLM_ROUTER_CLAUDE_SUBSCRIPTION`, and
  conflated "a provider is configured" with "something happened in the last 30
  minutes". Subscription mode is recognized (via `install_hooks.check_api_keys()`,
  not a second parser), and a new `idle` state separates a quiet Ollama from an
  unreachable one.

- **The quality breaker overrode explicit pins, invisibly (#64).**
  `should_skip_model()` blacklisted a model after three answers below 0.4, and a
  terse-but-correct QUERY answer scores ~0.3 because it can never earn the
  length or structure bonuses — so three *correct* short answers permanently
  disabled a pinned model. Pins are now exempt alongside `model_override`, and
  every skip appends a visible marker to `chain_attempts`: a candidate exclusion
  must leave a trace.

### Added

- `LLM_ROUTER_COST_PROFILE` for the routing cost tier (#65). `LLM_ROUTER_PROFILE`
  meant both that and the enterprise identity mode, so following the documented
  rename for one silently broke the other. The legacy name is still read, but
  only when its value is a valid routing tier, which makes the two readers
  mutually exclusive immediately rather than in 14.0. `VALID_PROFILES` now
  derives from the `RoutingProfile` enum instead of a hand-written list that had
  3 of the real 6 values.
- `LLM_ROUTER_QUALITY_MIN_CALLS`, `LLM_ROUTER_QUALITY_SKIP_THRESHOLD` and
  `LLM_ROUTER_QUALITY_SKIP=off` — the quality-skip thresholds were hardcoded and
  undiscoverable.
- A docs-lint test that derives its subcommand and hook-filename lists from
  `cli.py` and `install_hooks.py` rather than freezing a copy, so it cannot rot
  the way the docs it polices did.

### Changed

- **Documentation described a tool surface that no longer exists (#66).**
  `llm_health`, `llm_quality_report`, `llm_classify`, `llm_cache_stats`,
  `llm_cache_clear`, `llm_setup` and `llm_policy` are absent from the
  consolidated 11-tool surface. 16 files were affected, not the 4 reported.
  `LLM_ROUTER_SQL_DEBUG` and `LLM_ROUTER_HOOK_DEBUG` did not exist anywhere and
  are gone. `plugin.json`'s `mcpServers` key pointed at a `.mcp.json` that
  `.gitignore` excludes repo-wide, so it is removed rather than dangling.
- **`--help` told users to run a binary that is not on `$PATH` (#67).**
  `cli.py`'s docstring printed `llm_router <cmd>` 47 times; the installed binary
  is `llm-router`. `rules/llm_router.md`, installed as the agent-facing rules
  doc, was handing a live agent two commands that do not exist. The v5
  `rules/llm-router.md` fork is deleted — `install_hooks.py` already treated its
  installed counterpart as a pre-rebrand artifact, which is how it drifted.

### Known issues

`LLM_ROUTER_PROFILE=enterprise` still raises `ValidationError` at import time
through `config.py`'s `RouterConfig`, a third reader on a different mechanism
(#69). The enterprise surface is non-functional end-to-end: #68, #70, #71.
Remaining underscore-CLI strings in `install` and `doctor`: #72.

## [13.0.4] — Security: remove the unauthenticated SSE entry point (2026-08-28)

### Security

- **`llm-router-sse` is removed.** `server.main_sse`'s own docstring forbids
  exposing it as a console script — the prior entry point "bound 0.0.0.0 with no
  auth and exposed the full 60-tool MCP surface — including filesystem tools and
  wallet — to anyone reachable on the network" — and lists three conditions that
  must all hold before re-adding it. It was nonetheless present in
  `[project.scripts]` and shipped in 13.0.2 and 13.0.3 with none of them met: no
  auth middleware (that is `main_sse_secured`, which the script did not point
  at), and a bind of `os.environ.get("HOST", "0.0.0.0")` that never consulted
  `_allow_public_bind()` — a gate defined a few lines below it.

  Anyone who ran `llm-router-sse` published the full MCP tool surface,
  unauthenticated, on every interface. **If you have run it, stop the process
  and upgrade.** Nothing else invokes it, so an install that never ran the
  command was not exposed.

  Defence in depth: `main_sse` now defaults to `127.0.0.1` and refuses a
  non-local bind unless the shared gate allows it.

### Fixed

- **Statusline reported "✗ no provider" for every Ollama-only setup** (#50).
  `$SAVINGS_LOG` was read four times and never assigned, so `open("")` threw
  into a swallowed except and the health check could never see local activity —
  breaking the documented "route free to local Ollama with no cloud keys" path.
- **The dashboard's access URL was redacted by its own scrubber** (#48). The
  token pattern matched both the `token=` and `url=` log fields, so the only
  documented way to obtain a working URL printed `[REDACTED-TOKEN]`. The URL is
  now printed outside the logging pipeline; redaction itself is unchanged.
- **`doctor` reported three different states as "no routing decisions"** (#55).
  An unreadable table, an empty table on a machine that recorded activity
  elsewhere, and a genuinely idle machine printed the same sentence — so a user
  who had just made four calls was told to make some calls. Root cause: four
  different functions named `log_routing_decision` write to four destinations.
- **Session snapshots asserted zeros they had not measured** (#56). Downstream
  of the above; `accuracy: 1.0` derived from zero samples is what made the files
  look real. Facts now carry a `measured` flag.
- **`--help` crashed or was ignored on three console scripts** (#51, #52).
  `onboard` and `quickstart` ran their interactive flows and died on `EOFError`
  under a non-TTY stdin. `--help` is now handled first, before any other work,
  and is verified to open no socket.
- **The Textual install hint named a package that does not exist** (#47).
  `pip install llm_router[tui]` was wrong three ways; now
  `pipx inject llm-routing textual`.
- **`set-enforce` changed every running session on the machine** (#49). It is
  now scoped to the session that ran it (`--global` restores the old
  behaviour), and the messages describe what actually happens.
- **"routed" could mean a hint rather than an execution** (#53). Reserved for
  real executions; hint counts say "classified" and name their source.
- **The enforcement block demanded an attribution the agent could not honestly
  give** (#54). The route-indicator line is now offered only when the routed
  answer is what the user actually receives, and the "violations are logged and
  escalated" language is gone — nothing was escalated.

### Added

- **DIRECT-execution timeouts are surfaced** (#57). They previously failed
  silently to a debug log while routing fell through to Claude, so a local path
  that never once succeeded looked like nothing was wrong. `doctor` now
  distinguishes "raise `LLM_ROUTER_OLLAMA_TIMEOUT` to N" from "this machine is
  too slow for local routing".

## [13.0.3] — The MCP command that could never resolve (2026-08-26)

Fixes GH#41 and GH#43, and four defects found while reproducing GH#42.

### The entry point (#41)

- **Every MCP registration named a command that does not exist.** `[project.scripts]`
  declares only the hyphenated `llm-router`, so `shutil.which("llm_router")` returned
  `None` on *every* install type — pipx, pip and uv alike. Thirty call sites depended on
  it. The Claude Desktop and claw-code paths fell back to the literal string
  `"llm_router"`; the main registration fell back to
  `uv run --directory <site-packages>`, which is what produced `CONNECTION_CLOSED` on a
  clean pipx install of 13.0.2. Every pull integration — VS Code, Cursor, Windsurf, Kimi,
  Gemini CLI, Copilot CLI, OpenCode, OpenClaw, Trae, Pi, Codex — was registered dead the
  same way, along with three configs committed in this repo.

- **`doctor` reported 0 issues while the server was dead.** Every MCP check asked only
  whether the key `llm_router` was present in `mcpServers`; none read the command back.
  It now verifies the registered command exists, is executable, and — for
  `uv run --directory DIR` — that `DIR` is a real project root.

- **Both IDE config templates were invalid JSON.** `localize()` rewrites tool names to the
  1.0 surface (`llm_code` → `llm(task="code")`) and was running that substitution over a
  raw JSON document, injecting unescaped quotes into the `"description"` string. Since the
  templates are written verbatim, `install --ide` produced `.vscode/mcp.json` and
  `.windsurf/mcp.json` that no IDE could parse. Both are now built with `json.dumps`.

### The security disclosure (#43)

- **`LLM_ROUTER_DIRECT_EXECUTION` is now documented in README.md.** #36 was closed by
  writing it up in `SECURITY.md`, which the sdist excludes — so for anyone installing from
  PyPI the entire disclosure was invisible: a default-on feature handing a local model
  `write_file`/`edit_file`/`run_command` unsupervised, with no shipped text naming it or
  its off switch. A test now asserts the disclosure survives into the built artifacts.

### Install/uninstall symmetry (#42)

The two behaviours reported already had fixes in the 13.0.2 tag, and the published sdist
is byte-identical to that tag, so the report could not be reproduced as written. Asserting
the real contract — install then uninstall is a no-op on every config file — surfaced four
genuine defects instead:

- **An unguarded `unlink()` aborted uninstall partway through.** One `OSError` in the
  hook-removal loop (or on the rules file) raised straight out of `uninstall()`, so the
  statusLine restore and Claude Desktop deregistration — both later in the function —
  silently never ran. This is the most likely root cause of #42.
- **An empty `hooks` scaffold was left behind** for users who had no `hooks` section.
- **`~/.claude.json` was left as a `{"mcpServers": {}}` husk.** It is now recorded as
  `created_file` and removed only when install is the sole reason the file exists.
- **`settings.json.bak` held POST-install state**, so restoring it reinstated the very
  hooks a user was trying to remove. The snapshot is now taken before the first mutation.

### Packaging

- **`_quarantined_tests/` no longer ships to PyPI.** The exclude list anchored `/tests/`;
  the quarantine lives at the repo root, so 13.0.2 shipped 16 files of dead test code.

### Fixed

- **`KIMI.md` gained a duplicate routing block on every install.** The idempotence guard
  tested for `llm_router`, a token the block it writes never contains.

## [13.0.2] — Document what `LLM_ROUTER_DIRECT_EXECUTION` actually grants (2026-08-19)

Documentation and a test. No behaviour change.

### Security documentation

- **SECURITY.md now covers `LLM_ROUTER_DIRECT_EXECUTION`** (#36). It is **default-on**,
  and with it enabled the routing hook runs a tool-calling agent loop that hands a local,
  uncurated model `write_file`, `edit_file` and `run_command` — the last as an arbitrary
  shell string via `subprocess.run(..., shell=True)` — unsupervised, no confirmation, up
  to 15 iterations, before Claude ever sees the prompt.

  The section states what is enforced (file operations are confined to the project root)
  and what is not, with the blocklist coverage **measured against 13.0.1**: of twelve
  representative commands, **three** are blocked. `rm -rf ./src`,
  `git push --force`, `cat ../../.ssh/id_rsa`, `curl -d @.env` and
  `echo $OPENAI_API_KEY` all pass. The filter stops catastrophic *system* damage; it does
  not stop project damage, credential disclosure, or exfiltration.

  It also records that `agent_loop.py`'s docstring — *"All file operations are sandboxed
  to the project directory"* — is true of the file tools and false in effect, because
  `run_command` runs a shell string and `cat ../../.ssh/id_rsa` is not a "file operation"
  the sandbox sees.

  The entry documents the current state rather than changing it. Whether this should
  default to on is a real question and the section says so, but a default change needs
  its own decision and its own release note.

### Added

- A test re-derives the whole coverage table from the live regex. A table of
  measurements in a document rots silently — nothing fails, the file still reads
  plausibly, and the figure becomes a claim nobody re-checked. It now fails if the
  blocklist widens (so the doc stops understating its protection) or narrows.

## [13.0.1] — 13.0.0 shipped without `llm_router.agents` (2026-08-19)

**13.0.0 cannot start.** `import llm_router.server` fails with
`No module named 'llm_router.agents'`, so the MCP server exits before registering a
single tool and the client reports `CONNECTION_CLOSED` — indistinguishable from a network
fault, which is the same diagnostic dead end as #37.

### Fixed

- **Two unanchored exclusion patterns, in two different files.** Without a leading slash,
  both `.gitignore` and hatch's sdist `exclude` match a directory of that name at ANY
  depth:

  - `.gitignore`'s `agents/` and `Library/` also matched `src/llm_router/agents/` and
    `src/llm_router/library/`, so 11 source files existed locally and were never
    committed. Caught by CI, fixed before 13.0.0 was tagged.
  - `pyproject.toml`'s sdist `exclude = ["agents/", …]` also matched
    `src/llm_router/agents/`. **This one shipped.** `uv build` builds the wheel FROM the
    sdist, so the published wheel was missing the package as well.

  Both are now anchored, and a comment in each says nothing in that block may exclude
  anything under `src/`.

### Why every check passed

A local `uv build --wheel` builds straight from source and included the files, so the
wheel on this machine was correct while the published one was not. The pre-release suite,
the linters, the identity gate and CI all ran against the source tree, where nothing was
missing.

The only step that distinguishes "the release workflow succeeded" from "the artifact
works" is installing the published artifact and importing it. That is now the last step
of the release, not an optional afterthought.

## [13.0.0] — Upstream core sync: the routing engine, its guards, and four security fixes (2026-08-19)

The package is now built from the upstream routing core, rebranded, rather than from a
port of selected capabilities. 358 source files and 477 test files, ~4,956 identifiers.
`scripts/sync_downstream.py` upstream is the reviewable artifact — the sync is
reproducible, not a one-off copy.

**6,695 tests passing, 0 failing.**

### ⚠️ Breaking

- **`requires-python` is now `>=3.11`** (was `>=3.10`). The synced source uses 3.11-only
  stdlib. Leaving the floor at 3.10 would let a 3.10 user install code that cannot run —
  a silent failure rather than a resolver error.
- **`llm_router.audit_routing` is a different module.** It is now the live per-turn
  compliance log. The post-hoc misroute **scorer** that used to live there —
  `run_audit`, `score_decision`, `sample_unaudited_decisions`, `AuditedDecision` — moved
  to **`llm_router.misroute_audit`**, unchanged. Both features exist; they simply stop
  sharing a name. Update imports.

  The two shared a path across the repositories, with disjoint APIs, and a file-level
  copy in either direction would have deleted one of them in silence: no merge conflict,
  no import error, no failing test. Renaming is what makes that impossible.
- **`llm_router.observability` is a package, not a module.** Its OpenTelemetry layer is
  at `llm_router.observability.core` and re-exported from the package, so
  `observability.is_enabled()` and friends keep working. Direct imports of
  `llm_router.summary` and `llm_router.surface_status` are now
  `llm_router.observability.summary` / `.surface_status`.

### Security

- **The persistence redactor never shipped.** `persist_redact` lived under `enterprise/`,
  which is excluded from public distributions, and five write paths — result cache,
  semantic cache, idempotency, context, session store — imported it inside a `try/except`
  that fell back to a scrubber carrying none of its patterns. Measured against the
  published upstream package: JWTs, Slack tokens, emails, SSNs, phone numbers,
  credit-card numbers and prose secrets all reached disk verbatim, 7 of 7.

  The upstream suite was green throughout, including tests asserting exactly that those
  secrets never reach disk — they passed because the development tree *has* `enterprise/`.
  The control was only ever exercised in its strongest configuration. Fixed upstream and
  carried here.
- **SEC-002 layer 2 — path confinement — has landed.** `llm_fs_*` tools now reject paths
  resolving outside `project_root`. 12.0.1 shipped only the opt-in gate.
- **An optional tool group was a load-bearing import of the MCP server.** A build
  excluding `agoragentic` could not import at all — `ModuleNotFoundError` before a single
  tool registered, surfacing as `CONNECTION_CLOSED`, indistinguishable from a network
  fault. Same shape as the mcp 2.0 breakage in #37.
- **Reordering profiles resolved to no chain.** `SUBSCRIPTION_LOCAL` produced a
  one-model chain containing only the paid seat — no fallback, and the exact inverse of a
  profile whose purpose is preferring the free local bucket. `QUOTA_BALANCED` produced an
  empty chain on the two paths that exist to guarantee a non-empty one.

### Added

Whole subsystems from the upstream core, including the agentic engine, control plane,
policy runtime, semantic classification, quota and budget envelopes, the execution
ledger's realized-savings accounting, and the `misroute_audit` scorer with
`llm-router audit misroute`.

`config/` (model registry, agents, signals) and `scripts/` (the CI guards) are now
synced, so the checks that keep these fixes from regressing ship with the code.

### Not included, by design

`enterprise/`, `admin_api`, `invoice_reconciliation`, `tenant_policy_sidecar` and the
agoragentic marketplace/wallet tools. Tests covering them are skipped with that reason
rather than failing, so a red suite still means something is wrong.

## [12.0.1] — Fix the install breakage introduced by `mcp` 2.0.0 (2026-08-19)

Patch release, shipped alone and ahead of the next feature work, because 12.0.0 cannot be
installed fresh.

### Fixed

- **`mcp` was pinned `>=1.0.0` with no upper bound.** `mcp` 2.0.0 removed
  `mcp.server.fastmcp`, which seven modules here imported, so every fresh
  `pip install` / `pipx install` resolved 2.0.0 and died during import — before a single tool
  was registered. The client surfaced this as `CONNECTION_CLOSED`, which is what a network or
  provider fault also looks like, so there was no way to tell the two apart from the outside.
  The imports are ported to the 2.x API (`MCPServer`, `mcp.server.mcpserver`) and the pin is
  now `mcp>=2.0.0,<3.0.0`. (#37)
- **SECURITY.md claimed "Hooks cannot block core tools."** They can, and have since v13
  enforcement landed; the document contradicted the shipped behaviour. Rewritten to describe
  what the hook actually does. (#35)
- **`agoragentic_*` tools registered unconditionally at server startup.** They are now behind
  an explicit opt-in, so a routing install no longer exposes them by default. (#34)
- **Filesystem tools were ungated.** `llm_fs_*` now requires the same explicit opt-in.

### Added

- Two tests that keep this from recurring, deliberately not one:
  `test_pin_has_an_upper_bound` reads `pyproject.toml`, so a loosened pin fails in CI on the
  commit that loosens it; `test_every_mcp_import_in_src_resolves` parses `src/` for real
  `from mcp…` imports and checks each resolves against the *installed* package, so a partial
  port to a future major fails too. Neither subsumes the other — the first passes against a
  broken environment, the second passes against a dangerously loose pin that happens to
  resolve today.

## [12.0.0] — Chuzom capability wave: execution ledger, quality/fallback split, capability-aware shadow routing, budget envelope, misroute audit + CLI (2026-08-02)

> **Version note:** released as a major bump (v11.0.0 → v12.0.0) to mark the scale of the Chuzom capability wave, even though every routing-affecting behavior ships default-off or shadow-only.

Ports eight capabilities adapted from an internal reference implementation ("Chuzom") into llm-router. Everything here is additive; every new routing-affecting behavior ships default-off or shadow-only, so existing installs behave identically until explicitly opted in.

### Added

- **Execution ledger + session store** (`execution_ledger.py`, `session_store.py`). Append-only SQLite ledger recording every route attempt (`execution_events`) via additive `ALTER TABLE` migrations onto the existing `usage.db`, with realized-savings gating (see below) and route/cost invariants. `session_store.py` adds a durable JSONL session-context log with cross-process advisory locking, TTL/size-triggered compaction, and privacy modes.
- **Quality/fallback split** (`routing_quality.py`, `bounded_operational.py`, `quality_feedback.py`). A schema-v2 route-quality ledger (`routing_quality.py`) with fail-open recording and a `summarize()` that never conflates verified/unverified or legacy-v1 rows into v2 metrics. `bounded_operational.py` adds a bounded-operational route predicate and pricing-derived budget, gated by `LLM_ROUTER_BOUNDED_OPERATIONAL` (default off). `quality_feedback.py` gains LoopHole ground-truth verdict ingestion (`record_loophole_verdict`/`ingest_loophole_jsonl`) feeding the existing heuristic quality store.
- **Realized-savings measurement + dashboard split** (`dashboard_data.py::query_realized_savings`, `dashboard/server.py`). Realization-gated savings accounting alongside the existing potential-savings columns: only attempts with `realization_status == "verified_used"` and an adoption method that counts as realized are counted, and the figure is never reconciled against or allowed to overwrite `usage.saved_usd`. Exposed additively as `/api/stats`'s `realized_savings` key, isolated in its own fail-open block.
- **Capability-aware routing (shadow mode)** (`capabilities.py`, wired into `router.py`/`cost.py`). An 8-bit capability detector records what capability-aware routing *would* choose into `routing_decisions.capabilities_json`, without changing any live routing decision. Gated by `LLM_ROUTER_CAPABILITY_ROUTING` (default off); live routing (`needs_claude_tools()`) stays byte-identical regardless of the flag.
- **Budget envelope** (`budget_envelope.py`). Standalone `BudgetEnvelopeManager` (register/reserve/release/commit/settle/tier-state, hierarchical ancestor accounting) gated by `LLM_ROUTER_BUDGET_ENVELOPE` (default off). Ships as an accounting primitive only — no router/cost wiring — so routing and spend behavior are unchanged with the flag off; `execution_ledger.py` remains the sole source of truth for realized spend.
- **Misroute audit** (`audit_routing.py`). A fully offline, post-hoc scorer over existing `routing_decisions` rows (heuristic over judge score / complexity downgrades / downshifts), writing back new `audit_verdict`/`audit_checked_at` columns idempotently. Gated by `LLM_ROUTER_AUDIT_DISABLED`; inert until explicitly invoked via `run_audit()` or the new `llm-router audit` CLI command (below).
- **Retrospective loop + team report enrichment**. Verified the existing retrospective debrief (`retrospective.py`, native since v6.x) reads the new `audit_verdict` directly rather than re-deriving misroutes, and fails open on the context fields it reads from the items above. `commands/team.py`'s report/push surfaces gain fleet-wide realized-savings and inferred misroute-rate columns, sourced from the realized-savings query and quality-ledger summary via a fail-open helper.

- **`llm-router audit` CLI command** (`commands/audit.py`). Wires `audit_routing.py::run_audit()` to a CLI entry point (mirrors the `team` command's structure): renders sampled/audited counts, verdict breakdown, and the inferred misroute-rate baseline, with a `--json` mode and a `--limit N` flag (default 100). Respects `LLM_ROUTER_AUDIT_DISABLED`. Strictly read-only/reporting — never mutates routing state.
- **Bounded-operational routing wired into the live path** (`router.py`), strictly behind `LLM_ROUTER_BOUNDED_OPERATIONAL` (default off). When the flag is unset/false, the routing decision path is byte-identical to before, proved by an invariance test comparing route decisions with the module absent vs. present-but-disabled.

### Config

New env vars (all optional, all default off / non-disabling): `LLM_ROUTER_BOUNDED_OPERATIONAL`, `LLM_ROUTER_LOOPHOLE_JSONL`, `LLM_ROUTER_CAPABILITY_ROUTING`, `LLM_ROUTER_BUDGET_ENVELOPE`, `LLM_ROUTER_AUDIT_DISABLED` (opt-*out* — unset means audits run when explicitly invoked).

### Notes

- There is no `LLM_ROUTER_QUALITY_FEEDBACK` flag, and none is planned. The heuristic quality scorer's `should_skip_model()` check in the router's fallback-chain path is unconditional — it pre-dates this release, is unrelated to the LoopHole-verdict additions above, and is already always-on in production. Gating it now would change existing behavior, so it intentionally stays ungated; this note exists only to correct an earlier reference to a flag that was never implemented.

## [11.0.0] — Adaptive routing wave: observability, importable classifier, subscription-local profile, PII→local (2026-07-09)

A wave of routing and observability capabilities, plus a docs restructure. Everything new is additive and off-by-default where it touches routing, so existing setups behave identically until opted in.

### Added

- **Cross-surface status indicator** (`llm_router.observability.surface_status`). A stdlib-only, fail-soft "router is working" signal for hosts without a native statusline: a compact status line (`⚡ llm-router · 🎯 hermes3:8b code/moderate · $0.03 · ✓`), an OSC terminal title, and a rate-limited OS notification, all derived from the shared savings log. Answers *is it active / what did it last route / is it healthy*.
- **Session-end summary** (`llm_router.observability.summary`). A content model over the existing `usage.db` with `render_markdown()` (CI / Claude Desktop / logs) and a rich `render()` (rich is optional; falls back to markdown): headline savings vs baseline, tier mix, per-provider cost, latency p50/p95/p99, outcomes, and top routes.
- **Importable deterministic classifier** (`llm_router.classify`). The hook's weighted intent×3 + topic×2 + format×1 scorer (`score_categories`, `classify_complexity`) is now an importable module with a `classify_signals() -> ClassifySignal` wrapper, so the router core, gateway, and MCP tools can classify at 0 cost/latency — previously only the UserPromptSubmit hook could. A drift-guard test keeps it byte-identical to the hook.
- **`SUBSCRIPTION_LOCAL` routing profile** (`llm_router.subscription_local_routing` + `RoutingProfile.SUBSCRIPTION_LOCAL`). Cost-inverted routing for the "one paid seat + free bucket" shape: free-first for simple/moderate, seat-first for complex, and the seat demoted to last when its quota is strained. Wired into `chain_builder.build_chain`; a complete no-op unless `LLM_ROUTER_SUBSCRIPTION_PROVIDER` is set. Quota-pressure source is a pluggable hook.
- **PII / secret signal with force-local routing** (`llm_router.signals`). `PiiSignal` detects API keys, tokens, JWTs, private keys, and `.env`-style secrets; `force_local_for_pii(chain, prompt)` filters a chain to local providers when a secret is present and is **fail-closed** (empty chain when no local model exists) so a secret is never dispatched to an external API. Evidence names the matched pattern, never the value.
- **`run_port_tests.sh`** — one-command runner for the new modules' tests.

### Changed

- **Docs restructure.** `docs/` is now gitignored (local working notes) except the CI-generated `docs/BENCHMARKS.md`. README media moved to `assets/readme/`, and public guide pages moved to `guide/` (Getting Started, Providers, Policies, Tools, Architecture, Troubleshooting, …). README and CHANGELOG links updated accordingly. **If you linked to `docs/*.md` externally, update to `guide/*.md`.**
- Version bumped to **11.0.0** to signal the new capability surface and the docs path change.

### Config

New env vars (all optional): `LLM_ROUTER_SUBSCRIPTION_PROVIDER`, `LLM_ROUTER_INTERNAL_PROVIDERS`, `LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD`, `LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES`, `LLM_ROUTER_STATE_DIR`, `LLM_ROUTER_INDICATOR`.

### Follow-ups (not yet wired)

Call `force_local_for_pii` in the dispatch path; wire `get_subscription_pressure` to a live quota source; repoint `hooks/auto-route.py` to import `classify.py` (removing its duplicate definitions).
