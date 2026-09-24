# Checkpoint — 2026-09-24

Written at session close. What is open, what needs YOU, what is fragile, and
what will bite later. Everything here is measured or cites a file; where a claim
is unverified it says so.

`main` @ `9a370e6`, pushed. **One piece of work is uncommitted — see §0.**

---

## 0. UNCOMMITTED AND AT RISK — do this first

```
 M src/llm_router/codex_agent.py
?? tests/test_a29_codex_stdout_is_not_the_answer.py
```

The **Codex adapter fix** (§3.1). Written, 6 tests pass, 4 red-checks all RED,
ruff clean — but the full-suite gate had **not finished** when the session
ended, so it was never committed.

    # verify, then commit
    CLEAN=$(mktemp -d)
    HOME="$CLEAN" .venv/bin/python -m pytest tests/ -q -p no:randomly

If it goes green, commit it. If it does not, the change is self-contained and
`git checkout -- src/llm_router/codex_agent.py` reverts it cleanly.

---

## 0b. DECISIONS TAKEN AT SESSION CLOSE (2026-09-24)

| Question | Decision | State |
|---|---|---|
| The uncommitted Codex fix | **Wait for the gate, then commit** | gate re-running after an identity-gate failure — see below |
| Savings credited to discarded drafts | **Condition on acceptance — a discarded draft credits nothing** | NOT IMPLEMENTED, see §1.2 |
| `.gitignore` `/docs/*` | **Un-ignore** — but see the contradiction below | NOT DONE |
| What to start next | **The falsifying experiment (§5)** | — |

### The identity gate caught a leak I introduced

`test_identity_gate` failed on `9a370e6`, which is **already on main**:

    tests/test_a28_d1_d2_context_gate.py:67:
        "continue, remember to use <a private project name> all along"

A real corpus prompt carrying a private project name. **The name is redacted
even here**: the first version of this checkpoint quoted the offending string
verbatim while documenting it, and the gate failed a second time on the
documentation of the leak. Writing about a leak can recreate it. Fixed by replacing the
name (the test asserts the SHAPE, "continue, remember to ...", not the name),
**not** by extending the allowlist — `check_identity.py` says runtime hits must
be fixed. No other file in `audit/` or `architecture/` carries it.

Worth noting for future corpus work: **pulling real prompts into tests can
carry identifying material.** The gate caught this one; it is the only thing
standing between a corpus-derived fixture and the public repo.

### The `/docs/*` decision rests on a premise that turned out false

The rule is **deliberate**, with a stated rationale and carve-outs:

    # Docs are local working notes — never commit (except the CI-generated
    # benchmark page and versioned release-evidence packs, ...)
    /docs/*
    !/docs/BENCHMARKS.md
    !/docs/releases/

"Un-ignore" was chosen against the option "intentional — leave it", and that
comment is evidence it WAS intentional. **Not changed unilaterally**, because
the new fact contradicts the basis of the choice. 24 markdown files, 1.3 MB.

Two coherent positions, both defensible:
* the comment is stale and the planning corpus should be backed up (the risk in
  §1.3 is real: one disk, no copy);
* the comment stands and `Docs/` is a scratchpad by design — in which case §1.3
  should be closed as accepted rather than left open.

One `.gitignore` line either way. It needs your call with this fact in hand.

---

## 1. NEEDS A DECISION FROM YOU

### 1.1 PyPI trusted publisher — **the next release fails without this**

`e9d7710` removed the long-lived API token from `publish.yml` and switched to
OIDC. **The publisher is not registered**, so the next `git tag vX.Y.Z` will
build and then fail to upload.

Nothing is broken today: 15.1.0 is already on PyPI and installable.

    https://pypi.org/manage/project/llm-routing/settings/publishing/
    owner:       ypollak2
    repository:  llm-router
    workflow:    publish.yml
    environment: pypi          ← easy to miss; publish.yml declares it, and
                                 omitting it fails with a confusing
                                 permissions error rather than a clear mismatch

A Chrome tab was left open at that page. I could not complete it — it needs
your login, and entering credentials is not something I do.

### 1.2 Should savings be conditioned on acceptance?

Today's claimed savings: **$0.0928** over 55 rows. Breakdown:

| model | n | saved | what it was |
|---|---|---|---|
| `ollama/qwen3.5` | 50 | **$0.0773** | drafts that were **discarded** |
| `ollama/qwen3-coder:30b` | 2 | $0.0146 | escape-valve calls returning "ok" |
| `codex/gpt-4o-mini` | 2 | $0.00085 | calls that returned the **stdin banner** |

**83% of today's savings is credited to drafts nobody used.** The estimator
credits a saving whenever a local model emitted tokens, regardless of whether
the output was used — which contradicts CLAUDE.md's own rule, *"'A local model
ran' is not a saving."*

`routing_report.draft_acceptance()` now makes this conditionable.

**DECIDED 2026-09-24: condition on acceptance — a discarded draft credits
nothing. NOT YET IMPLEMENTED.**

Scope when picked up: `savings_stats.estimated_claude_cost_saved` is written on
every routed call. The acceptance verdict arrives ONE INVOCATION LATER (the
marker is only visible in the next assistant turn — see `hooks/draft_usage.py`),
so the saving cannot be decided at write time. Either write it as provisional
and settle it on the next prompt, or compute the reported figure as a JOIN at
read time and leave the row alone. **The second is far less invasive** and does
not require a second writer, which this repo has twice paid for.

Expect the headline to fall by roughly an order of magnitude — today $0.0928 →
about $0.015 — and lifetime $110.91 to fall similarly. That drop is the point:
it is the first defensible version of the number.

### 1.3 `.gitignore:111` — `/docs/*` leaves the whole planning corpus untracked

`git ls-files Docs/` returns **0 files**; `audit/` has 50+. So
`BACKLOG_NEXT.md`, `TECH_DEBT.md`, `MEASUREMENT.md`, `PLAN_*.md` and
`decisions/` exist **on one machine only, unbacked**. On a case-insensitive
filesystem `/docs/*` swallows `Docs/`.

Architecture docs were filed under `architecture/` and `audit/` to avoid it.
Decide whether that ignore is intentional.

### 1.4 The two remaining architecture decisions

`architecture/README.md` records four decisions taken. Two were never settled:

- **Does agenticgraphs accept an injected runner?** — **VERIFIED YES.**
  `run_graph(doc, runner, ...)`, duck-typed (`run`/`bind`, optional
  `approve`/`contract_for`), `MockRunner` proves no env vars are needed. Phase 1
  in-process integration is a go; blockers B1/B2 do not apply.
- **Phase 0.5b** — the fail-closed capability filter on the model chain. Shadow
  measurement done (`scripts/measure_capability_mismatch.py`); the filter is
  not built. **See §2.1 for why the first attempt was reverted.**

---

## 2. REVERTED / KNOWN-WRONG — do not re-attempt blind

### 2.1 F-2's enforcement exemption — reverted (`e348772`)

I wired `detect_capabilities()` into `enforce-route.py` so a prompt needing an
action would not be held. It broke two deliberate invariants and was reverted:

- `test_explanatory_prompt_same_task_type_stays_text_only` — *"the signal, not
  the task_type, gates the redirect."* My predicate flagged
  *"Explain how to run the migration and commit the result."*
- `test_delegate_off_disables_the_execution_redirect` — I keyed on
  `not _delegate_redirect_fires(...)`, which is true both when a prompt does not
  warrant a redirect **and** when the operator switched delegation off.
  Conflating an operator's explicit choice with a capability fact.

Root cause: `detect_capabilities` is a keyword matcher. **Verified:
`explain what git commit does` → `needs_action = True`.** It cannot tell "do X"
from "explain X", and the hook already has a signal that can (`_detect_execution`).

**What I failed to measure:** 21.1% is *recall* — how many prompts need an
action. **Precision was never measured.** Measure it before any retry.

### 2.2 The "three macOS home leaks" — claim WITHDRAWN

I asserted that three tests leak into `$HOME`. **I could not reproduce it** and
withdrew the claim; it was never written into any file. Per-test detection over
the whole suite attributed the artifacts to **no test**, and each half in
isolation produced nothing. `.claude.json.lock` and `Library` are live-process
artifacts. It reproduces only in one command form, which suggests something
outside the suite.

**Do not "fix" this without reproducing it first.**

---

## 3. LANDED TODAY (context for whoever picks this up)

| | |
|---|---|
| `dddce4e` | **F-1** — the escape hatch is no longer behind the thing it escapes |
| `48e039c` | plugin bundle rebuild |
| `ef2bad8` → `e348772` | F-2 attempt and its revert (kept separate deliberately) |
| `70bfe27` | `draft_acceptance` counter; banner no longer prints its own query limit |
| `bb02c08` | R12 driver for it |
| `a40085e`, `49cfcbe`, `4cd15a6` | `audit/28` — the measurements |
| `9a370e6` | **D-1 + D-2** |
| uncommitted | **Codex adapter** (§0) |

### 3.1 The Codex adapter defect (uncommitted fix)

Reproduced by hand with the adapter's own argv:

```
$ codex exec --json -m gpt-4o-mini -c model_provider=openai ... </dev/null
Reading additional input from stdin...                    ← non-JSON, on STDOUT
{"type":"item.completed","item":{"type":"error",
  "message":"Model metadata for `gpt-4o-mini` not found."}}
{"type":"turn.completed","usage":{"input_tokens":0,"output_tokens":0}}
```

1. `except json.JSONDecodeError: text_chunks.append(line)` — any non-JSON stdout
   line became the model's answer.
2. An error item carries `message`, not `text`; the old code read `""` and
   dropped it silently.
3. **NOT fixed:** `codex/gpt-4o-mini` should not be routed to at all — the CLI
   has no metadata for it and the turn used 0 tokens. That is §1.4's capability
   filter, not an adapter bug.

---

## 4. WILL CAUSE PROBLEMS LATER

### 4.1 The duplicated classifier — a standing drift, now demonstrated

`classify.py:_SIGNALS` and `hooks/auto-route.py:SIGNALS` are hand-kept in sync
("Option B"). The hook does **not** call `classify_signals()`.

**It has already drifted.** S3b fixed over-detection of `that`/`which` in
`context_signal` this week; the hook keeps its own copy and never received it.
Verified: `write a regex that validates an email address` is still flagged by
the hook's detector. Left deliberately, recorded in
`tests/test_a28_d1_d2_context_gate.py`.

Consequence: **every new pre-routing signal must be built twice**, or the hook —
the surface you touch most — does not get it. Existing backlog item N8.

### 4.2 The drafting path has never produced an accepted draft

**0 relayed out of 1,132 audited**; 3,122 produced lifetime. Rule of three: with
95% confidence the true acceptance rate is **below 0.27%**.

D-1 and D-2 (`9a370e6`) will reduce the volume. Neither can *improve* acceptance
in a way that is measurable from zero. Watch `drafts used: N/M` in the banner —
if it stays 0/M after a week, the mechanism's value is disproved on this
workload and the honest options are in `audit/28`.

### 4.3 `unterminated_invocations = 337`

`doctor` flags it: *"a routing branch that skips without logging a reason — the
defect that hid ENFORCE=off for a day."* Pre-existing, untouched, and the exact
shape of defect that previously cost a day of investigation.

### 4.4 Enforcement `hard` traps local tooling on a misclassification

`_bash_exempt_from_hold` returns False for every QA task type, so a prompt
misclassified as `research` holds **all** local tooling — `git`, `pytest`,
`make`. F-1 exempted llm-router's own CLI; everything else is still held. With
**49.8% of prompts decided by a default rather than a score**, misclassification
is not rare. `audit/27` has the detail.

### 4.5 `~/.llm-router/knowledge/` is 298 MB with 12,448 project subdirs

Many named after test fixtures — test writes leaked into the real store.
Unfixed. A new knowledge store that repeats this is worse than no store.

### 4.6 Measurement hygiene that must not be lost

- **Gate under `HOME=$(mktemp -d)`.** Three tests passed only on a machine with
  prior state and left CI red across 15.0.1 and 15.1.0 while the local suite was
  green. `pre-release-verify.sh` runs the suite locally and never consults CI.
- **`ollama stop` before a long run.** A 17 GB model resident with ~111 MB free
  is the OOM that killed the suite three times.
- **Run halves sequentially**, not in parallel, for the same reason.

---

## 5. THE ONE EXPERIMENT THAT MATTERS

From `architecture/ARCHITECTURE_CHALLENGE.md` Q15, unrun:

> Run the §28 scenario through the graph, and again as **one strong model given
> the same prompt and the same acceptance checks**. Compare verified completion
> and total tokens.

If the single call matches verified completion for fewer tokens, the
orchestration layer is overhead and the answer is a better prompt plus objective
checks. It is cheap, and it should run **before** Phases 3 and 4 are built on
the assumption that it passes.

---

## 6. HONEST NOTE ON THIS SESSION

Five times I proposed something that already existed (the context-dependence
gate, the draft-acceptance signal, routing-with-context, the OKF context
injection, and the `_is_context_dependent` fix S3b had already made elsewhere).
Once I shipped a fix that had to be reverted. Once I asserted a defect I could
not reproduce and withdrew it.

The pattern: **asserting a cause before isolating it.** The measurements in
`audit/28` are sound because they were taken before the fixes; the proposals
that failed were made before the measurements. If you pick this up later, the
measurement scripts are the trustworthy part:

    scripts/measure_low_signal_rate.py         49.8% decided by a default
    scripts/measure_capability_mismatch.py     21.1% need an action
    scripts/routing_rate.py                    the canonical rate parser
    routing_report.draft_acceptance()          0 of 1132
