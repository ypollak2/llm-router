# REMEDIATION PLAN

Subject: `357a402`. Findings: `audit/03_FINDINGS.md`. **Nothing here is implemented.**

## Organising principle

This plan is NOT organised by file, and deliberately not one-task-per-finding.

The audit's central result is that the individual bugs are symptoms of two
classes:

* **CLASS-A — the mechanism is built and nothing reads it.**
  58 `failopen.record()` writers / 0 readers · the hook terminal-outcome
  invariant stated in CLAUDE.md and computed nowhere ·
  `execution_ledger.dropped_event_count()` incrementing into nothing ·
  "Rule B" call-site discipline asserted as prose in a docstring.
* **CLASS-B — fixes are applied call-site-by-call-site instead of
  consolidated.** 9 import-time path instances *after* a sweep declared the
  class closed · 3 provider-identity instances, the third created by the commit
  that fixed the second · 6 scrubber pattern tables.

**A plan that fixes 12 findings gets you finding 13.** P2 is therefore the most
important section, not the least.

The one counter-example is the template: the secret-scrubber consolidation
(*delegate to a canonical function, never copy its state*) was applied once and
**held** under this audit. Generalise that, or repeat the cycle.

## Acceptance criteria rules

Every criterion below must be a gate that **can fail**, and every task carries
a **RED-CHECK**: the specific mutation that must make the gate fail. A criterion
with no stated red-check is not accepted.

This rule exists because of A-10: yesterday's "Rule B" tests were red-checked by
reverting a whole file, which removes the pinned string, so the check passed —
and the narrower evasion (keep the string in a comment, break the call site)
was never tried. **The red-check must be the narrowest mutation that
reintroduces the bug, not the broadest.**

---

# P0 — Ship-blockers

## R1 · Scrub before persistence, at the boundary
**Findings:** privacy CRITICAL (`execution_events`, `attempts.jsonl` 0644)
**Root cause:** scrubbing is a call-site convention; `execution_ledger.py`
contains no scrub call at all, and exception text reaches it via `str(exc)`.

**Solution direction:** a single write-boundary chokepoint. Every durable
writer takes its text through one `persist_text()` that scrubs; direct
`str(exc)` into a persisted field becomes a lint error.

**Acceptance criteria**
1. Canary set (OpenAI/Anthropic/GitHub/AWS/JWT/Slack/Google/PEM/bearer/DSN)
   injected through a *provider exception*, not a prompt; recursive scan of an
   isolated `LLM_ROUTER_HOME` finds **zero** canaries in any file.
2. `attempts.jsonl` and every JSONL store created at **0600**, asserted at
   creation, not after chmod.
3. A test enumerates every durable writer and fails when one persists a field
   that did not pass the chokepoint.

**RED-CHECK:** remove the scrub call from exactly one writer → criterion 1
fails naming that writer and that canary. (Not: disable the whole scrubber.)

**Migration risk:** existing stores already contain secrets. Ship a
`llm-router gc --redact` pass; do not silently rewrite history.

## R2 · Nothing leaves the machine unscrubbed
**Finding:** `alerts.emit_alert` POSTs `detail` to a webhook; captured carrying
a Postgres DSN with a plaintext password.

**Acceptance criteria**
1. Every outbound path (alert webhook, `team-sync`, `share`, any telemetry
   upload) passes through R1's chokepoint.
2. DB-connection-URL-with-credentials is added to the canonical pattern table
   — it is currently absent, which is why the DSN survived.
3. A test asserts the wire body, not the log line.

**RED-CHECK:** remove the scrub from the webhook path → the wire-body test
fails with the canary visible.

## R3 · Stop calling the allowlist a security control
**Finding:** 10 of 28 allowlisted programs are general-purpose interpreters.

**This is not a bug to patch.** The design goal (let a model run dev tools) and
the security goal (constrain execution) are in direct conflict; the allowlist
resolves it toward capability while presenting as a control. Attempting to
"fix" it by blocking `git -c` closes 1 door of 11.

**Solution direction — pick one, explicitly:**
* **(a) Honesty.** Reclassify it in `SECURITY.md` as a *typo-and-footgun
  guardrail*, not a containment boundary. State plainly: an agent with
  `run_command` can execute arbitrary code as the operator. Recommend OS-level
  containment (container/VM/seatbelt) for untrusted repos.
* **(b) Containment.** Run agent commands in a sandbox (macOS `sandbox-exec`,
  Linux namespaces, or a container) and keep the allowlist as defence in depth.
* **(c) Both.**

**Acceptance criteria**
1. `SECURITY.md` contains no claim that survives this test: *"does the
   allowlist prevent arbitrary code execution?"* — the honest answer is no
   under (a), yes-with-caveats under (b).
2. The command-matrix corpus (`docs/security_command_matrix.txt`) is extended
   with the ten interpreter invocations. **The current corpus tests the wrong
   population and produced a number (10/12 refused) that is accurate and
   misleading.**
3. Under (b): a probe proving `python -c` cannot read `$HOME/.ssh` or reach
   the network.

**RED-CHECK:** add a new interpreter to `_ALLOWED_PROGRAMS` → the corpus test
fails and names it.

## R4 · `agent_loop.run_command` must not inherit the environment
**Finding:** `get_delegated_env` exists, is used by `verifiers.py`, and is never
called here.

**Acceptance criteria**
1. Canary env vars (incl. a name no denylist knows) are absent from the child.
2. A test enumerates every `subprocess` call site in `src/` and fails on any
   that passes `env=os.environ` or omits `env=`.

**RED-CHECK:** revert one call site to inherited env → the enumerating test
fails naming it. This is a CLASS-B task: the enumeration, not the single fix,
is the deliverable.

## R5 · Release, or the audit fixed nothing for users
**Finding:** PyPI 14.1.0 is 26 commits behind. Users get: `status` crashes
(`rich` undeclared), `demo` prints `-100% cheaper`, `gain` documented but absent.
All three already fixed on HEAD.

**Acceptance criteria**
1. Clean-room install from the published artifact in a fresh venv + fresh HOME:
   `install`, `status`, `doctor`, `demo` all exit 0 with no traceback.
2. CHANGELOG entry for the provenance migration warning users their historical
   savings will drop, and why.
3. A CI job runs (1) against the built wheel on every tag.

**RED-CHECK:** remove `rich` from dependencies → the clean-room job fails.

---

# P1 — Make the claims true, or remove them

## R6 · One savings number
**Finding:** `cost.py` filters provenance; `dashboard_data.py` (~26 surfaces)
does not. `savings-report`, `status`, `doctor` report three different totals
from one DB at one moment.

**Acceptance criteria**
1. A test seeds one DB and asserts **every** savings surface returns the
   identical figure. Surfaces enumerated by inspection, so a new one must join
   or fail.
2. `savings-report`'s "SINGLE source of truth" docstring is true or deleted.

**RED-CHECK:** make one surface bypass the shared accessor → the test fails
naming that surface.

## R7 · Say what the number means
**Findings:** flat Opus baseline (1.7–2.5× inflation); subscription gate applied
on 1 of 4 surfaces; `routing_overhead_usd` never populated (37,872 rows, $0.00);
retries tracked correctly but only in a ledger users never see.

**Acceptance criteria**
1. Every displayed dollar figure carries its baseline model and whether it is
   *baseline-equivalent avoided* or *real dollars avoided*. Under subscription
   the latter renders **$0** and says why.
2. `net_realized_savings_usd` (already correct) becomes the user-facing number;
   gross is available but labelled.
3. Either `routing_overhead_usd` gets populated, or it is deleted. A column
   that is always zero is worse than absent.

**RED-CHECK:** strip the label from one surface → a test fails asserting every
money string carries its qualifier.

## R8 · Decide about task-success measurement
**Finding:** the only prompt↔decision join is off by default; the savings metric
has no quality term.

**This is a product decision, not an engineering one.** Options:
* **(a)** Default `LLM_ROUTER_GROUND_TRUTH=1` with explicit consent at install,
  and measure.
* **(b)** Keep it off and **remove every claim about preserving task success**
  from README, CLI and docs.

Either is defensible. The current state — capture off, claim on — is not.

**Acceptance criteria**
1. Under (a): a report showing downgrade-regret and upgrade-waste **separately**
   on ≥N real tasks, with N stated.
2. Under (b): a claims-ledger test (R12) fails on any doc asserting preserved
   quality.

## R9 · Make hook death visible
**Finding:** a killed hook is observably identical to "chose not to route".
60s timeout, measured max 55.3s, 5.5% of real invocations unterminated.

**Acceptance criteria**
1. `doctor` reports unterminated-invocation rate over the last N invocations,
   with N. Above a threshold it is an ISSUE, not a line.
2. The hook writes a start marker before expensive work and clears it on exit;
   an orphan marker is a detected kill.
3. Hook p50/p95/p99 measured in CI against the installed timeout; the margin is
   asserted.

**RED-CHECK:** inject a 70s sleep into the hook → `doctor` reports kills.

## R10 · Refuse what cannot be served
**Findings:** capability registry has zero callers; tools silently dropped;
vision silently discarded; `/v1/responses` missing the tool refusal its
siblings got; no context-window pre-flight.

**Acceptance criteria**
1. A request needing a capability the selected model lacks is **refused with a
   stated reason**, never silently degraded. Covers tools, vision, structured
   output, context length.
2. A parametrised test over **all** gateway endpoints — a new endpoint must be
   added or the test fails.
3. `CapabilityRequirement` gains a structured-output dimension, or JSON claims
   are removed from the registry.

**RED-CHECK:** drop the refusal from one endpoint → the parametrised test fails
naming it. (This is precisely how `/v1/responses` was missed.)

---

# P2 — Kill the classes *(the real deliverable)*

## R11 · One canonical source per concept — enforced
**Findings:** 9 path-resolution instances · 3 provider-identity instances (incl.
`cost.py:1641` rejecting `'google'`, so every Gemini decision is swallowed) ·
6 scrubber tables.

**Solution direction:** for each duplicated concept, one canonical module
function. Then a **structural test** that fails when a second implementation
appears — an AST scan for the literal patterns (`Path.home() / ".llm-router"`,
provider-name set literals, secret regex tables) outside the canonical module.

**Acceptance criteria**
1. Concept inventory with one named owner each.
2. A test that fails on a second implementation, naming file and line.
3. `cost.py` imports `GOOGLE_PROVIDERS` rather than hand-listing.
4. A probe proving a real Gemini row reaches `routing_decisions`.

**RED-CHECK:** add `Path.home() / ".llm-router"` to any file → the scan fails.
**This single task subsumes findings 10, several path findings, and prevents
instance 13.**

## R12 · Every counter has a reader — enforced
**Findings:** 58 fail-open writers/0 readers · hook invariant uncomputed ·
`dropped_event_count()` read by nothing.

**Acceptance criteria**
1. A registry of every counter/invariant with its reader surface.
2. A test that fails when a counter has no reader.
3. `doctor` renders the registry.

**RED-CHECK:** add a new `record()` with no reader → the test fails naming it.

## R13 · Ban source-text assertions
**Finding A-10:** I reintroduced T-10 behind a comment and **23 tests passed.**
11 of 33 recent test files use `inspect.getsource`.

**Acceptance criteria**
1. Every `inspect.getsource` substring assertion is converted to a behavioural
   assertion, or to an **AST** assertion on the specific call site.
2. A lint rule fails on new `getsource` + `in` assertions in `tests/`.
3. Each converted test is re-red-checked with the **narrowest** mutation
   (string preserved in a comment, call site broken).

**RED-CHECK:** the A-10 evasion applied to any converted test → it fails.

## R14 · Fix `attempt_log` rotation
**Finding:** unlocked `read_text`/`write_text`, 0.6–5.3% record loss under 8
processes. Note `execution_ledger` held 0 losses at 32 procs — the durability
pattern exists, this file never got it.

**Acceptance criteria:** 8-process barrier test, 3 consecutive runs, zero loss
and zero malformed lines.
**RED-CHECK:** remove the lock → the test fails.

## R15 · Inspect `finish_reason`
**Finding:** `content_filter` with partial content is accepted as success.
**Acceptance criteria:** a non-`stop` finish reason is recorded and excluded
from the bandit's success signal.
**RED-CHECK:** force `content_filter` → the row records it and success is False.

---

# P3 — Ground Truth honesty

## R16 · Let capture see real work, or scope the claim
**Finding:** `prompt_capture.capture()` has no `cwd`/`tools`/`external`
parameters, so repo- and tool-bound tasks are **permanently ineligible** — a
missing signature, not a policy. The T-06 id-bridge works and is inert because
nothing produces a frozen task matching a pool candidate.

**Acceptance criteria:** either a real captured repo task reaches a frozen
dataset end to end, or the docs state that GT covers state-free prompts only,
and the representativeness gap is quantified.

## R17 · The verifier validator must discriminate
**Finding:** `len(answer) > 5` reached **HIGH** confidence and then accepted a
confidently wrong long answer. 0% real kill rate.

**Acceptance criteria:** a corpus of known-worthless verifiers (length checks,
non-empty checks, always-true) cannot exceed LOW confidence; a corpus of
known-good verifiers does reach HIGH.
**RED-CHECK:** feed `len(answer) > 5` → capped at LOW.

---

# Audit-readiness kit

Goal: the next audit finds **new classes, not new instances**, and takes hours.

| # | Item | Why |
|---|---|---|
| K1 | `scripts/audit/freeze_state.py` — emits `FROZEN_STATE.md` | Phase 0 took manual work; it is mechanical |
| K2 | Machine-checked claims ledger: every README/CLI claim tagged to the test proving it; untagged claims fail CI | Kills the whole "docs describe a different product" class |
| K3 | Denominator identity tests: `total == success + reject + dedup + error + excluded` for every population | The audit's single most productive question |
| K4 | Standing adversarial corpus (routing pairs, near-neighbour cache pairs, canary secrets, interpreter invocations) — diagnostic only, **never tuned against** | Re-runnable in minutes |
| K5 | `llm-router doctor --audit` printing every counter, denominator, unterminated rate and provenance split | Makes CLASS-A structurally impossible |
| K6 | Self-audit CI job running K3+K4 nightly | Finds drift between audits |
| K7 | Written rule: a remediation is not done until its red-check uses the *narrowest* mutation | The A-10 lesson, mechanised |

## What must be true before calling the next audit "clean"

1. Every P0 and P1 acceptance criterion has a red-check that has been **observed
   failing**, not merely asserted.
2. K2, K3, K5 are green.
3. The claims ledger has no `UNPROVEN` rows — or the claims are deleted.
4. A fresh-install clean-room run passes on the **published artifact**.
5. `audit/21_INVALIDATED_FINDINGS.md` is non-empty. An audit that invalidated
   nothing did not attack itself.

## Sequencing

```
R1 R2 R4  ──► R3 ──► R5 (release)
   │                  ▲
   └──► R11 R12 R13 ──┘   (P2 first: it prevents the next instances)
              │
              └──► R6 R7 R9 R10 ──► R8 (product decision)
                          │
                          └──► R16 R17 ──► K1..K7
```

**Do P2 before P1.** R11 and R13 change how the P1 fixes get written; doing
them second means writing the P1 fixes twice.

## Rollback

Every task is independently revertable. R1 and R11 touch many call sites —
land each behind a test that fails without it, and split by subsystem so a
revert is one commit.
