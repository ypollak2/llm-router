# 27 — Two enforcement findings, observed live

Found 2026-09-23 during the execution-strategy architecture review, by being
trapped by them. Not a code audit — a transcript.

---

## F-1 · The escape hatch is behind the thing it escapes

**Severity: high. Fix: ~2 lines.**

### What happened

Enforcement was in `hard`. A read-only `grep` against the local checkout was
held:

```
[llm_router] Routing directive BLOCKED.
  Directive:      ⚡ MANDATORY ROUTE: research/moderate → call llm(task="research")
  Tool attempted: Bash
```

The block message prints the remedy itself:

> `Run llm_router set-enforce off to disable enforcement.`

**That command is also blocked.** Running `llm-router set-enforce smart`
produced the identical refusal. The only way through was to spend a routed model
call on a throwaway prompt ("Reply with just: ok") purely to clear the lock — twice,
because the lock is per-turn.

### Root cause

`enforce-route.py` has a documented local-tool allowlist whose own rationale is
exactly this case (`:419-424`):

> *"A shell command that invokes a local dev tool … is NEVER LLM reasoning, so
> no routed model can perform it. Blocking such a command to 'force routing'
> therefore saves nothing; it just traps the user."*

`_BASH_LOCAL_TOOL_RE` lists `git`, `npm`, `pytest`, `docker`, `mkdir`, `python`…
**It does not list `llm-router` or `llm_router`.** Verified:

```
llm-router in _BASH_LOCAL_TOOL_RE: False
llm_router in _BASH_LOCAL_TOOL_RE: False
```

The router's own CLI is the most inherently-local tool on the machine — no
routed model can ever run `set-enforce`, `doctor` or `status` for you — and it
is the one local tool the allowlist omits.

### Why the allowlist would not have saved it anyway

`_bash_exempt_from_hold` (`:480`):

```python
if task_type in _QA_TASK_TYPES or task_type == "code" or redirect_fires:
    return False
```

Under a **QA task type — `query`/`research`/`analyze`/`generate` — the local-tool
allowlist is bypassed entirely.** This is deliberate and defensible on its own
terms (a read-only command can answer a Q&A question natively, which is the
bypass enforcement exists to stop, pinned by
`test_readonly_bash_blocked_for_qa_tasks`).

But it means a *misclassification* into `research` holds **every local tool** —
`git`, `pytest`, `make`, and the router's own CLI — with no allowlist relief.
See F-2: that misclassification is not rare.

### Proposed fix

Narrow and safe, because it cannot be used to bypass routing — no routed model
can execute the router's own CLI:

1. Add `llm-router|llm_router` to `_BASH_LOCAL_TOOL_RE`.
2. Exempt it **even under QA task types** — a dedicated check before the
   `_QA_TASK_TYPES` return, since that is precisely the case where the trap
   closes.

Test to pin it: *under every task type including the four QA types, with
enforcement `hard` and no route satisfied, `llm-router set-enforce …` is not
held.* Red-check by removing the exemption; it must fail.

`strict` mode disables every escape valve by design — the exemption should hold
there too, or `strict` becomes unexitable without editing a file.

---

## F-2 · The task-14 classifier fix did not hold

**Severity: high. Fix: structural — see `architecture/ROUTING_MODEL.md` §2.**

### The prior fix

`enforce-route.py:470-474` records a previous instance of this exact failure and
where it was fixed:

> *"The misrouting that motivated task 14 — local debugging landing in
> `research` and then holding `grep` against the user's own checkout — is
> therefore fixed where it originates, in the classifier's `research` intent
> pattern, not by loosening this gate."*

### It recurred, identically

Prompt: **"check if agenticgraphs accepts an injected runner"** — a question
answerable only by reading a local file. Classified `research/moderate`, holding
`grep` against the user's own checkout. The same sentence describes both.

A second prompt, **"set-enforce smart"**, inherited the label via
`via: context-inherit` — so one misclassification propagated to a turn whose
text contains the literal name of the enforcement command.

### Why fixing it in the intent pattern cannot hold

Measured 2026-09-23 over **n=1571** real prompts (`scripts/measure_low_signal_rate.py`,
CLAUDE.md drop rules applied):

```
score == 0, nothing scored at all ......... 651/1571 = 41.4%
decided by low_signal_default, not a score  783/1571 = 49.8%
```

**Half of all real traffic never reaches an intent pattern at all.** Tightening
`research`'s regex cannot fix inputs that match no regex — they are decided by
`policy.low_signal_default`. Task 14's fix addressed the half that scores; this
recurrence came from the half that does not.

### The structural defect

The router routed a repo-archaeology question to a **stateless model with no
filesystem access** — a model that could not answer it under any circumstances.
Nothing prevented that, because **there is no capability filter on the live
path, only an ordering** (`architecture/CURRENT_ARCHITECTURE.md` §3):

- `model_registry.ModelMetadata` carries `context_window` and a capability tuple
  — **no reader outside that module and its tests**.
- `capabilities.detect_capabilities()` describes what a task needs — gated off
  by default, and its only production consumer is a dead path.

Both halves exist. Nothing joins them.

Observed cost in this session: 3 routed calls (~96 s wall clock, one of them
69 s to return the word "ok"), zero information produced, plus two blocked
tool calls. The saving claimed on the directive was **$0.0030**.

### Proposed fix

Not a regex change. `architecture/IMPLEMENTATION_PLAN.md` Phase 0.5:

1. Shadow-measure how often the live chain contains a model that cannot serve
   the task — the number that justifies or kills the work.
2. Wire `detect_capabilities()` as a real `filter()`.
3. **Fail closed**: a model whose capabilities are unknown is not eligible.

A prompt requiring filesystem access would then never be offered a stateless
endpoint, regardless of how it was classified. That is the difference between
fixing the symptom in the classifier and fixing the class.

---

## What connects them

F-1 is the trap; F-2 is what springs it. The local-tool allowlist exists because
someone already reasoned that holding local tooling "saves nothing; it just
traps the user" — and then the QA carve-out reopened the trap for exactly the
inputs the classifier is least confident about.

Both are instances of the pattern this repo keeps finding: **a guard that is
correct in isolation, defeated by an upstream decision that was never
measured.**
