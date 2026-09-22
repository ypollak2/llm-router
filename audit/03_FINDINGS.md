# FINDINGS — Ultimate Adversarial Audit

Subject frozen at `audit/FROZEN_STATE.md` (HEAD `357a402`).
Reconciled findings only. Specialist reports are the source; the orchestrator
re-derived every CRITICAL independently before admitting it here.

---

## A-01 — The agent command allowlist cannot constrain execution

**Severity:** CRITICAL
**Confidence:** CONFIRMED (orchestrator-reproduced against the real
`guard_command`, default mode)
**Subsystem:** `src/llm_router/hooks/agent_writes.py`
**Found by:** Security Red Teamer (as a `git -c` bypass); ENLARGED by the
orchestrator into the general case.

### Expected
`command_mode()` defaults to `allowlist`. `_ALLOWED_PROGRAMS` (28 entries),
`_BLOCKED_SUBCOMMANDS` and `_BLOCKED_MODULES` are presented — in `SECURITY.md`
and in the module's own docstring — as the control that stops a model-authored
command from doing arbitrary damage.

### Observed
Ten of the 28 allowlisted programs are general-purpose execution vectors.
Reproduced against the real `guard_command`, default mode, no flags:

```
ALLOWED  python -c "import os; os.system(...)"
ALLOWED  python3 -c "..."
ALLOWED  node -e "..."
ALLOWED  find . -name x -exec touch /tmp/pwned {} ;
ALLOWED  awk "BEGIN{system(...)}"
ALLOWED  sed -e 1e/bin/sh x
ALLOWED  cargo run
ALLOWED  go run x.go
ALLOWED  git -c protocol.ext.allow=always clone ext::<cmd>
ALLOWED  git -c core.sshCommand=<cmd> fetch origin
ALLOWED  git -c alias.z=!<cmd> z
REFUSED  python -m http.server          <-- the only control that fires
```

Allowlist partition:

* **execution vectors (10):** `awk cargo find git go node pytest python python3 sed`
* **genuinely inspection-only (18):** `ag cat cut diff du echo file grep head ls rg sort stat tail tree uniq wc which`

`git config` is in `_BLOCKED_SUBCOMMANDS`; `git -c`, the inline equivalent, is
never inspected. That is three separate arbitrary-execution doors through one
allowlisted program, and `python -c` is a fourth that needs no trick at all.

### Why this is not a patchable bug
Blocking `git -c` closes one door of eleven. Closing all of them means removing
`python`, `node`, `awk`, `sed`, `find`, `cargo`, `go` from the allowlist — at
which point the coding agent cannot run tests, scripts or builds, i.e. cannot
do its job. **The design goal (let a model run development tools) and the
security goal (constrain what a model can execute) are in direct conflict, and
the allowlist resolves that conflict in favour of capability while presenting
itself as a control.**

### User impact
An agent driven by indirect prompt injection — untrusted repo content, a
poisoned dependency README, a malicious issue body — can execute arbitrary
code with the operator's privileges. Compounded by A-02.

### Why tests missed it
`tests/` asserts the twelve sample commands in `SECURITY.md`'s table. Those
twelve are all *shell-shaped destructive* commands (`rm -rf`, `git push
--force`, `curl | sh`). None is an interpreter invocation, so the suite never
asked the question this finding asks.

### Orchestrator self-indictment
Commit `31459b5` (mine, today) "corrected" `SECURITY.md` to say **ten of twelve
commands are refused**, shipping a corpus and a repro command so the number
would be re-derivable. That number is accurate and *more misleading than the
wrong one it replaced*: it invites the reader to conclude the allowlist is
83% effective, when the allowlist permits arbitrary execution through ten
programs it deliberately allows. I made a security document more confidently
wrong. The corpus I added tests the wrong population.

---

## A-02 — `agent_loop.run_command` inherits the full parent environment

**Severity:** HIGH
**Confidence:** CONFIRMED (Security Red Teamer, canary against the real
`execute_tool`)
**Subsystem:** `src/llm_router/agentic/agent_loop.py`

`safe_subprocess.get_delegated_env` — the allowlist-based env that exists for
exactly this — is never called on this path. Sibling module
`scripts/groundtruth/verifiers.py` does call it (fixed 2026-09-22, commit
`5462cda`); this one does not. Every provider key, OAuth token and cloud
credential in the parent process is readable by any command A-01 permits.

**Chain:** A-01 gives execution, A-02 gives it the secrets.

---

## A-03 — "Preserving task success" is structurally unmeasurable by default

**Severity:** CRITICAL
**Confidence:** CONFIRMED (Routing Scientist; orchestrator verified the flag)
**Subsystem:** `src/llm_router/prompt_capture.py`

`prompt_capture.py:20` — *"Off by default. `LLM_ROUTER_GROUND_TRUTH=1` turns
the whole path on."* That capture is the only join between a prompt and its
routing decision. Unset here, and unset for every user who does not know to
set it.

There is therefore **no mechanism by which a shipped install could establish
whether routing preserved task success.** This is not "not yet measured" — the
wire does not exist in the default configuration.

Compounding: the headline savings metric (8 call sites, incl. `digest.py`,
`statusline_hud.py`, `observability/summary.py`, both session-end hooks) has no
quality or success term. Savings accrue identically whether the cheap model's
answer was correct or garbage.

---

## A-04 — The router has never been compared to a constant policy

**Severity:** HIGH
**Confidence:** CONFIRMED
**Subsystem:** `scripts/groundtruth/discriminate.py`

The tool builds `always-cheapest`, `always-premium`, `random` and `oracle`
baselines. Its docstring:

> *"The router is deliberately absent. Comparing it here would invite reading
> a router result off a dataset that has not yet been shown to work."*

**The reasoning is methodologically correct** — validate the ruler before
measuring with it, and the docstring cites RouterArena, where a constant policy
was competitive with everything except retrieval. This is not concealment.

The finding is that **the gate was set honestly and has never been passed.**
No dataset has been shown to discriminate, so the router has never been
compared against "always cheapest" — the one comparison that would establish
whether routing adds value over a one-line policy.

---

## A-05 — Tier escalates on prompt LENGTH alone

**Severity:** HIGH
**Confidence:** CONFIRMED (orchestrator-reproduced independently)
**Subsystem:** `src/llm_router/classify.py`, `reason_gate.py`

```
'print hello world'          len=17    -> query/SIMPLE
'print hello world' x120     len=2280  -> query/COMPLEX
```

Identical trivial task, repeated. Upgrade waste by construction, and directly
gameable: padding a prompt buys a better model.

Visible in the same trace: `CapabilityRequirement` has 8 boolean fields and
none expresses structured-output strictness, so a strict-JSON prompt and loose
prose produce an identical capability vector.

---

## A-06 — Bash output is intercepted and compressed by default, undisclosed

**Severity:** MEDIUM
**Confidence:** CONFIRMED (direct observation, this session)
**Subsystem:** bash-compress hook

This audit's own shell output was silently intercepted and rewritten before
reaching the caller, until `LLM_ROUTER_BASH_INTERCEPT=off` was set. The
interception announces itself in the replaced payload, but it is on by default
and the operator did not opt in. Any tool reading command output through this
hook receives a summary, not the output.

---

# INVALIDATED

## I-01 — "Wording, not difficulty, drives the route"

Filed HIGH by the Routing Scientist; **retracted by that agent under
reconciliation, with the confound identified.**

Its pair was not the same task with filler added: it rewrote *"Prove that there
is no rational number whose square is 2"* into *"is there a rational number
whose square is 2?"*, deleting the phrase `prove that` — the literal keyword
that `_COMPLEXITY_DEEP` (`classify.py:309-320`) matches — and simultaneously
changing the speech act. Two confounds.

Orchestrator control: same task text with conversational filler wrapped around
it, unchanged -> `deep_reasoning`, score 0.985 (unchanged). **Filler alone does
not flip the route.**

What survives is narrower and weaker: the deep-reasoning feature is a literal
keyword-phrase match, so a hard task phrased without one of those phrases gets
no boost *from that feature*. Length and math-density features are separate
and unaffected.

## I-02 — `git core.pager` RCE
Does not fire: `subprocess.run(capture_output=True)` means no tty, so git never
invokes a pager. Tested and closed by the Security Red Teamer.

## I-03 — Symlink escape via `_resolve_path`
`Path.resolve()` collapses symlinks before the boundary check. Tested and closed.

## I-04 — `run_verifier` environment leakage
Correctly uses `get_delegated_env` with a fail-closed fallback. Env is clean;
filesystem/network access from generated verifier code remains open
(HYPOTHESIS, separate finding).

---

## A-07 — A killed hook is indistinguishable from a hook that chose not to route

**Severity:** CRITICAL
**Confidence:** CONFIRMED (Reliability Engineer, measured; orchestrator
verified the timeout and re-derived the field rate from the live log)
**Subsystem:** `hooks/auto-route.py`, installed `~/.claude/settings.json`

### Observed
* Installed `UserPromptSubmit` timeout: **60s** (two entries, both 60s).
* Measured hook latency on a 50KB prompt: **p50 35.1s, max 55.265s**, with no
  injected load. Margin to the kill: **4.7s**.
* Ollama classification measured **p50 11.75s / max 14.1s cold** against the
  hook's own docstring claim of "1-3s". Cold is every session start.
* A single retried 429 burns 14.13s inside litellm before surfacing, and
  `_extract_retry_after()` discards a real `Retry-After: 7` header.
* These costs STACK. Each of cold-Ollama, large-prompt and one retried 429
  independently consumes 10-25% of the 60s budget.

### Field evidence (observational, operator's real log)
`~/.llm-router/auto-route-debug.log`, 41,276 lines:

```
invocations started (prompt_len=)  : 8,362
reached OUTPUT COMPLETE            : 4,733
reached any terminal marker        : 7,903
NO end marker at all               :   459  (5.5%)
```

**~1 in 18 real invocations ends with no terminal marker** — consistent with
the kill hypothesis, though log rotation and hard crashes are not excluded.

### The actual defect
The repo's own `CLAUDE.md` states the invariant: *every invocation logging
`prompt_len=` must log exactly one terminal outcome.* The markers exist. The
invariant is real. **Nothing computes it.** No `doctor` check, no counter, no
operator surface reports the unterminated rate.

So the detection mechanism is present and unread — structurally the same defect
as T-07 (58 `failopen.record()` writers, 0 readers) and as
`execution_ledger.dropped_event_count()`, which increments correctly and is
read by nothing. **Third instance of "the counter exists, nobody looks".**

### Why this outranks routing accuracy
If the hook dies, there is no route. At 100x traffic the first symptom is a
silent decline in routing rate — which this repo's own `CLAUDE.md` explicitly
warns readers not to mistake for a regression. The one warning that would
resolve the ambiguity is the number nobody computes.

### ORCHESTRATOR SELF-CORRECTION
My first pass at this measured **44% unterminated** and I nearly reported it.
That was a defective regex on my side (it missed `DIRECT SUCCESS:`,
`DIRECT FAILED:` and `OUTPUT COMPLETE`, which are the real terminal markers).
The corrected figure is 5.5%. Recorded because an audit that hides its own
near-miss is doing the thing it exists to detect.

---

## A-08 — Unlocked rotation loses records under concurrency

**Severity:** HIGH
**Confidence:** CONFIRMED (8-process barrier, 3/3 trials, unmodified code)
**Subsystem:** `attempt_log.py::_rotate`

`read_text()` / `write_text()` with no lock. Measured loss: **2-17 of 320 new
records (0.6-5.3%)**, plus occasional corrupt JSON lines.

Contrast, and this is the useful part: `execution_ledger`/`usage.db` held
**0 lost events at 32 processes x 150 writes (4,800)** — twice the load of the
previously documented 2,400-write incident — and the GT pool's lock fix held
under fresh SIGKILL. The durability work that was done, worked. `attempt_log`
is simply a file that never got it.

---

## A-09 — `finish_reason` is never inspected

**Severity:** MEDIUM
**Confidence:** CONFIRMED
**Subsystem:** `router.py` dispatch loop

A response flagged `content_filter` with partial content present is accepted as
a normal success, indistinguishable from `stop`. Truncated and filtered
responses enter the ledger, the context buffers and the bandit's success signal
as clean completions.

---

## A-10 — Source-text assertions can be evaded by a comment

**Severity:** HIGH
**Confidence:** CONFIRMED (orchestrator performed the evasion and ran the tests)
**Subsystem:** `tests/test_t08_every_terminal_path_writes_a_quality_row.py` and
10 further recently-added test files
**Found by:** Test Skeptic (as a DESIGN RISK, code-inspection only); ESCALATED
to CONFIRMED by the orchestrator, who executed it.

### The defect
Several tests added in the last 15 commits pin `router.py`'s SOURCE TEXT via
`inspect.getsource(module)` and a substring check. A substring check over a
whole module is not a call-site assertion: the string can live in a comment
while the executing code does the opposite.

### Reproduction (performed, in a scratch copy at /tmp, now deleted)
Break the real call site, keep the pinned string alive as a comment:

```python
# historical form, kept for reference:
# False if getattr(response, "quality_degraded", False)
success=_response_is_usable(getattr(response, "content", "") or ""),
```

That restores T-10 exactly: the bandit is once again rewarded for answers the
router itself gate-rejected.

Verified the mutation was live (not shadowed by the editable install):

```
module loaded from: /tmp/evade/src/llm_router/router.py
  mutated call site present (bug restored): True
  pinned string still findable in source  : True
```

Result:

```
tests/test_t08_every_terminal_path_writes_a_quality_row.py
tests/test_t09_bandit_reward_is_bounded.py
....................... [100%]     23 passed
```

### Why this is the most self-indicting finding in the audit
Those tests were written in the previous session, by the model orchestrating
this audit, explicitly under a rule stated in their own docstrings:

> *"THIS FILE ASSERTS THE CALL SITE, NOT THE DEFINITION (Rule B)."*

They do not. They assert a string's presence anywhere in a 5,300-line module.
The rule was stated, believed, tested against a whole-file revert — which does
remove the string, so the red-check passed — and the narrower evasion was never
tried. **A red-check that only reverts the whole file cannot distinguish a
call-site assertion from a substring scan.**

11 of 33 recently-touched test files use `inspect.getsource`. Every one is
suspect by the same argument.

### Related
Same class as A-07 (an invariant that exists and is unread) and the "delegate,
don't copy" gap: the protection is stated rather than mechanised.

### Recommended remediation direction (NOT IMPLEMENTED)
Assert behaviour, or assert the AST. For this specific case: call the real
`log_routing_decision` path with a `quality_degraded=True` response and assert
the recorded `success` field is False. That is immune to comments, formatting
and rewrites.
