# Phase 50 — Reproduce the criticals from clean

> Not from the repository. From what a user actually installs.

Run against **`llm-routing==15.0.0` from PyPI**, in a fresh `uv venv` with an
isolated `HOME`, on 2026-09-22 — minutes after the publish workflow completed.
No repository on the path. This is the artifact 1,000 new installs get.

The audit's own completion criterion 4: *"A fresh-install clean-room run passes
on the published artifact."*

---

## Setup

    uv venv .venv
    uv pip install llm-routing==15.0.0
    HOME=<isolated>            # no real ~/.llm-router, no real settings
    llm_router.__version__  -> 15.0.0

---

## The 14.1.0 regression that motivated the release

**Phase 42-45 finding:** `llm-router status` — the README's own first "verify
it worked" step — crashed on every clean install of 14.1.0 with
`ModuleNotFoundError: No module named 'rich'`, because `rich` was imported
unconditionally and absent from `Requires-Dist`.

**15.0.0: FIXED.** The command runs and renders.

    ╭───────────────────────────────────────────────────────────╮
    │ ⚡ LLM_ROUTER Status  ·  Health: Optimal                  │
    ╰───────────────────────────────────────────────────────────╯

**But it ships a NEW cosmetic defect** — see P50-N3.

---

## The three CRITICALs

### A-01 — The agent command allowlist cannot constrain execution

**Status on 15.0.0: REPRODUCES, AS DOCUMENTED.**

    guard_command(['python3','-c','...open(~/.ssh/id_rsa).read()'])  ->  ALLOWED

This is the intended outcome. R3 remediated A-01 by **honesty, not
containment**: SECURITY.md now states the allowlist is a typo-and-footgun
guardrail, that 10 of its 28 permitted programs are general-purpose
interpreters, that an agent with `run_command` executes arbitrary code as the
operator, and that untrusted repositories need OS-level containment.

**Reproducing it is the pass condition.** The documented behaviour and the
actual behaviour agree. Had it been refused, the shipped documentation would
now be wrong in the flattering direction.

### A-03 — "Preserving task success" is structurally unmeasurable by default

**Status on 15.0.0: MECHANISM SHIPPED, MEASUREMENT STILL ABSENT.**

    ground_truth_consent.TERMS_VERSION          -> 1        (module present)
    'cwd' in signature(prompt_capture.capture)  -> True      (R16 signature)
    ground_truth_consent.read_consent()         -> None      (never asked)

The consent flow reached users. `read_consent() -> None` on a fresh install is
**correct**: nobody has been asked yet, and silence is refusal.

The finding is NOT closed. Neither downgrade-regret nor upgrade-waste is
computed anywhere in the shipped package, and the claims ledger carries
`preserves-task-success` as UNPROVEN. A-03 is honestly scoped, not fixed.

### A-07 — A killed hook is indistinguishable from one that chose not to route

**Status on 15.0.0: FIXED, VERIFIED IN THE ARTIFACT.**

    llm_router.hook_liveness                    -> present
    'hook_kills' in counter_registry.REGISTRY   -> True
    len(REGISTRY)                               -> 7 counters

The marker mechanism and its reader both shipped. `doctor --audit` renders it.

---

## New defects found by running the release

### P50-N1 — `doctor --audit` inflates the counter it reports

**Severity: HIGH. This is the most important result of Phase 50.**

Three consecutive runs of a READ-ONLY diagnostic, same state, no work between:

    run 1   fail_open_events: 4
    run 2   fail_open_events: 8
    run 3   fail_open_events: 12

Root cause, isolated by instrumenting `failopen.record`:

    2 migration statements fail on EVERY database open:
       duplicate column name: is_simulated
       duplicate column name: is_simulated

`_migrate_alter` guards the `ADD COLUMN` form with `_column_exists`, and two
paths escape that guard. Each failure is recorded as a fail-open event.

Three consequences:

1. **An operator investigating a high count makes it higher.**
2. **A normal condition is counted as a degradation.** The counter's docstring
   says a spike means migration is silently failing; the baseline is 4-per-open,
   so a real spike is indistinguishable from someone looking more often.
3. **Numbers I published were measuring my own tooling.** The R12 commit and
   `REMEDIATION_RUN.md` state `fail_open_events: 713, of which
   CHZ-FO-COST-MIGRATE-ALTER = 531` as a product finding. A share of it was
   `doctor` runs during development. The exact share is unrecoverable, which
   is itself the point: a contaminated counter cannot be decontaminated after
   the fact.

### P50-N2 — the `is_simulated` migration is attempted six times

**Severity: MEDIUM.** Five `ALTER TABLE … ADD COLUMN is_simulated` statements
in `cost.py` (`usage`, `claude_usage`, `codex_usage`, `gemini_usage`,
`savings_stats`) plus a sixth in `hooks/session-end.py`, each with its own
try/except. The provenance column added by T-05/R6 is migrated by whoever gets
there first, and the rest fail benignly and loudly.

### P50-N3 — rich markup renders as literal text

**Severity: LOW.** `llm-router status` emits one line containing unrendered
markup tokens (`[bold #7aa2f7]…[/]`) — a string printed with `print()` rather
than through the rich console. Cosmetic, on the command the README sends every
new user to first.

---

## Verdict

| | |
|---|---|
| Published artifact installs and runs | **PASS** |
| 14.1.0's clean-install crash | **FIXED** |
| A-01 remediation (honesty) reached users | **PASS** — reproduces as documented |
| A-03 mechanism reached users | **PASS**; measurement still absent, scoped |
| A-07 remediation reached users | **PASS** |
| New defects found by running the release | **3** |

**The remediation reached users.** All three CRITICALs behave in the shipped
package as the shipped documentation says they do — which is the only form of
"fixed" that matters, and the form a repository-only audit cannot establish.

**And running the release found three defects that reading the repository did
not**, one of which undermines a number the remediation itself published. That
is the lesson to carry: Phase 42-45 and Phase 50 are the two phases that
installed the product, and between them they produced the finding that caused
this release and the finding that most damages its claims.
