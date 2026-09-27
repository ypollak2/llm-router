# Local execution of instructions — design and pre-registered pilot

Status: DRAFT for review (2026-09-24). Nothing here is built.

## 1. The problem, measured

Replaying 186 real prompts (2026-09-14..24, benchmark sandboxes, notifications
and noise excluded) through the current hook:

| Prompt kind | Share | Reaches a local draft |
|---|---|---|
| Instructions ("push both commits", "keep going", "fix X") | 58% (108) | 32% — and a draft cannot carry one out |
| Questions | 23% (43) | 67% |
| Sub-agent reports | 18% (34) | ~0% (correct) |

Drafts were never the lever for this workload. 0 of 1,191 drafts were ever used
(before 2026-09-24), and even a perfect draft only answers questions — a quarter
of the traffic. The user's expectation (70–80% of work done locally) can only be
approached by **executing** instructions locally.

What does not work, measured:

- **Claude choosing to delegate** (`llm_local_task` offered as a tool): Claude
  delegated in 2 of 47 A/B runs; delegated-token ratio 0.98. A delegating turn
  still pays Claude's fixed session overhead (~110–150k tokens in `claude -p`).
- **Substituting a tool result** (PreToolUse deny + result text): the block is
  reliable, but the model reads the substituted text as prompt injection,
  refuses it and retries with another tool — more turns, not fewer (3/3 runs).

What does work mechanically: **UserPromptSubmit `decision: block`** (hooks
reference, verified 2026-09-24): the prompt never reaches the model and is
erased from context; the `reason` is shown to the user. That is a whole turn
of Claude avoided — the only thing that saves quota.

## 2. Local agent capability, measured

| Suite | Local (qwen3-coder:30b) | Claude |
|---|---|---|
| Easy — contained single-file edits (held-out) | 72/78 | — |
| Hard — cause in another file | 16/30 | 27/30 |
| Plan — implement a multi-step plan | 3/9 | 9/9 |
| Hard, split: edits vs questions (2026-09-12) | 6/7 edits, 0/3 questions | 10/10 |

Local agents are good at **named, contained edits and mechanical runs**, and
bad at investigation. The design must only hand them the first kind.

## 3. Design

### 3.1 Who decides a turn is local — the user, explicitly (pilot)

A prompt starting with **`@local`** (e.g. `@local run the full suite and tell me
what failed`, `@local rename parse_cfg to parse_config in config.py`) is executed
locally. Nothing else changes.

Why explicit, not classified: the classifier that decides whether to draft
disagrees with its sibling on 33% of real prompts, and a wrong local *execution*
costs far more than a wrong draft (it changes the tree). An explicit prefix
makes the pilot's error rate the local agent's, not the classifier's. Automatic
eligibility is a later step, gated on pilot data (§5).

### 3.2 What runs

The hook calls the existing `llm_local_task` machinery (tools/local_task.py):
objective = the prompt minus the prefix; workdir = the session's cwd;
`apply_writes=True`; an acceptance check chosen as follows:

1. If the prompt names a check (`… and run pytest tests/test_x.py`), use it.
2. Else, if files changed, the repo's own gate for them (configurable per repo,
   default: the test files matching the changed modules; `ruff` on changed files).
3. Else (a pure run: "run the suite"), the command's own exit code is the check.

Only `verified_complete` is reported as done. Any other status is reported as
not done, with the diff and check output, and nothing is claimed.

### 3.3 Guard rails

- **Clean start:** refuse unless `git status --porcelain` is clean for the
  files the agent may touch, or auto-stash with a named stash the report names.
- **Never outward:** `git push`, tags, publish, deploy, anything that sends data
  off the machine is refused in the pilot (the user runs it, or asks Claude).
- **Writes stay in the repo root;** the existing `agent_writes` allowlist and
  command guard apply unchanged.
- **Time:** these turns need more than the 55s draft budget. The `@local` path
  gets its own budget (default 300s) and the hook's registered timeout is raised
  to cover it; ordinary prompts keep the 55s deadline. (Hooks reference: the
  UserPromptSubmit default is 30s, the field has no documented cap; the hook
  holds the prompt until it returns, so the internal deadline is what keeps
  ordinary prompts fast.)

### 3.4 What the user and Claude see

- The user sees the block `reason`: status, files changed (diff stat), the
  check that ran and its last lines, elapsed time, model.
- **Continuity:** the same hook output carries `additionalContext` with the
  summary ("ran locally: <objective>; verified_complete; changed a.py, b.py;
  check `pytest …` passed"). Per the hooks reference, `block` erases the prompt
  but `additionalContext` is still inserted and saved in the transcript, so
  Claude sees it on its next turn. Without it Claude would be blind to a turn
  it never saw.
- **On failure:** block with the failure report (the user decides: retry,
  `@local` with a sharper instruction, or ask Claude). No silent fallback to
  Claude — a fallback would make the turn cost Claude *plus* the local time.

### 3.5 Accounting

A `verified_complete` local turn writes a VERIFIED saving: the Claude tokens of
the turn it replaced, estimated from the median of the same user's comparable
Claude turns (same repo, same kind) — labelled as an estimate with its n.
Everything else writes nothing.

## 4. Sizing — how much of the traffic this could take

The 108 instructions from §1, each classified by whether a local agent could
carry it out AND prove completion with a machine check (per-row file:
scratchpad/instruction_classes.jsonl; classifier: a separate agent, 5 rows on
the B/D line flagged uncertain):

| Category | n | % of instructions | Self-contained |
|---|---|---|---|
| D judgment (investigate, design, plan, audit) | 35 | 32% | — no objective check |
| C continue ("keep going", "yes do it") | 28 | 26% | — needs the live plan |
| A mechanical (push, commit, run) | 17 | 16% | 3 of 17 |
| B named edit | 15 | 14% | 3 of 15 |
| F other (continuation summaries, meta) | 12 | 11% | — |
| E config / accounts | 1 | 1% | — |

**Ceiling, as a share of all 186 real prompts:**
- today, instruction alone: **6/186 ≈ 3%**
- with the conversation's referents resolved ("both commits", "fix D-1"):
  **32/186 ≈ 17%** — and several of those are `git push`, which §3.3 excludes.

**Conclusion: local execution cannot deliver the 70–80% target for this
workload either.** It is a directed-agent workload: two thirds of instructions
are judgment or continuation, which only the model holding the conversation
can do. The pilot below is worth running only as a small opt-in convenience,
not as the answer to "why is local doing so little".

## 5. Pilot and pre-registered decision rule

Run for 2 weeks of normal use, `@local` only when the user chooses it.

Measured, each with n:
- verified_complete rate of `@local` turns;
- revert/redo rate: a later turn (local or Claude) that undoes or redoes the
  same files within the session;
- user time: median wall time of `@local` turns vs Claude turns of the same kind;
- Claude tokens avoided (estimate, §3.5).

Ship-to-default rule (decided now, before data):
- verified_complete ≥ 70% **and** revert/redo ≤ 10% over ≥ 40 `@local` turns →
  next step: propose automatic eligibility for the categories that met it.
- Otherwise: keep `@local` as an opt-in power tool, report the numbers, no
  automatic routing.

## 6. Not in scope

- Replacing Claude on investigation, planning or "keep going" turns.
- Any change to the draft path (it stays; routing-health measures it).

## 7. Open decisions for the user

1. Prefix name (`@local`) and whether it also accepts a slash command.
2. Default acceptance check per repo (§3.2 step 2).
3. Whether `git commit` (local, reversible) is allowed in the pilot; push stays out.

## 8. Pre-registered test: can a local model continue a real conversation? (2026-09-24)

Requested by the user: for "keep going", "continue" and mechanical steps that
belong to a longer conversation, pass the conversation and its context to the
local model and let it do the work. Measured before building.

**Sample.** From the 108 real instructions (§4), the continuation (C) and
conversation-dependent mechanical/edit (A, B) moments whose session worked in
this repo and whose next Claude window changed at least one tracked file.
Up to 20, taken in time order; the selection is recorded before any run.

**What the local model gets** (per moment):
- the conversation up to that prompt, compressed: every user prompt and every
  assistant text block (tool output omitted), trimmed from the oldest end to
  fit 60K tokens, first user prompt always kept;
- a sandbox worktree of this repo at the last commit before the prompt;
- the agent loop with read/list/search/edit/write and the command allowlist,
  writes applied inside the sandbox only; model qwen3.8 (131072 window),
  budget 600 s.

**Ground truth.** What Claude did in the window between that prompt and the
next user prompt: the set of repo files it changed (Edit/Write targets and
files in commits made in the window), and the test commands it ran that passed.

**Scoring (mechanical, no model judge, no Claude quota).** A moment PASSES if
- file-set F1 between the local agent's changed files and Claude's is >= 0.5, and
- every pytest command Claude ran successfully in the window also passes in
  the local sandbox after the local agent's changes (moments with none: the
  F1 condition alone).
File overlap is a proxy — it can credit a wrong edit to the right file and
miss a right edit to an equivalent file. Reported with that caveat.

**Decision rule.** PASS rate >= 70% over >= 15 scored moments -> build local
continuation into the `@local` path (still opt-in). 40-70% -> report where it
breaks, no build. < 40% -> not viable with this model. Every number is
reported with n.

### 8.1 Amendment before any scored run (2026-09-24)

The first moment exposed a harness limit, not a result: moment 1's
conversation was 64,749 tokens, and qwen3.8 took **572 s to read it once**
(113 tok/s at that length; ~200 tok/s had been measured at 24K). With a 600 s
budget every moment would fail on reading time alone, measuring the hardware
rather than the model. (Its reply to "yes, do it" did name the right next
steps from the conversation — "N1, N4, N6, then the six gated ones … merge
PR #138" — which is why the test is worth running properly.)

Changed, before scoring anything:
- conversation cap **20K tokens** (80,000 chars): the first user prompt plus
  the most recent prose, oldest dropped first (~3 min to read);
- budget **900 s**;
- moment 1's run is void (harness-limited) and is re-run under these settings.

Unchanged: sample, ground truth, scoring, decision rule. A real local
continuation would face the same limit — a turn that spends ten minutes
reading before acting is not usable — so the cap is also the realistic setting.

### 8.2 Run 1 result, and run 2 after a tool fix (2026-09-25)

**Run 1 (§8.1 settings): PASS 0/20; 0 of 20 moments changed any file.**
13 ended at the 15-step cap, 4 stopped on a repeated identical command, 3
returned nothing. By the §8 rule that is "< 40% — not viable", *for the loop as
it stood*. Harness checked before believing the zero: a known-positive
("create hello.txt") was written and detected.

A trace of one moment ("Great, go on") showed the model orienting as Claude
would — reading the backlog, then `git log --oneline -12 && git status --short
| head -20` — and the loop's shell-free `run_command` passing `&&` and `|` to
git as literal arguments, and refusing read-only `git branch --show-current`.
It retried variants until the 15 steps were gone. That is a tool defect, fixed
in S (PR #149): sequences and pipes are tokenized and chained without a shell.

**Run 2**: identical sample, context cap, budget, 15-step cap, scoring and
decision rule; the only change is S. Reported separately from run 1; neither
replaces the other.

### 8.3 Run 2 result, and run 3 — the last harness-only change (2026-09-25)

**Run 2 (after S): PASS 0/20; 1 of 20 moments changed a file (the wrong one).**
16 ended at the 15-step cap, 2 on a repeated command. The command tool now
worked; a trace showed the next defect was the harness's: each sandbox was a
`git worktree` of the live repo, so `git log main` showed commits made AFTER the
moment (e.g. today's PR #141 merge). The model paged back through history
(-8, -20 … -200) reconciling the conversation with a repo it could not match.
It also hit `2>&1`, now supported (S, 2dc87ae).

**Run 3**: each moment runs in a clean-room clone whose only ref is `main` at
the base commit, reflog expired and unreachable objects pruned (verified on
moment 6: 868 reachable commits = the base's history; today's merge absent).
Everything else as §8.1.

**Run 3's score is the answer.** No further harness changes after it: whatever
it scores is reported against the §8 rule, with the traces that explain it.
Changing the setup until something passes would make the number meaningless.

### 8.4 Result (final, run 3): PASS 0/20 — not viable as the loop stands

**Run 3: 0/20; 1 of 20 moments changed a file (a scratch script, not Claude's
files).** 16 ended at the 15-step cap, 2 on a repeated command, 2 returned
nothing. By the §8 rule (< 40%): **not viable with qwen3.8 and the current loop.**

What the traces show, now that the harness is clean (moment 6, "Great, go on"):
the model oriented itself in ~5 steps, located the right code (`HOOK_POLICY` in
`classify.py` — the file Claude changed), and was still reading it when the
15-step cap ended the turn. It was not lost; it ran out of steps.

Why 15 steps cannot hold these turns: Claude used a **median of 20 tool calls**
in the same 20 windows (range 1–90); 13 of 20 needed more than 15. Claude also
starts each turn holding the tool output of earlier turns; the local model gets
prose only and spends ~5 steps re-orienting. Of the 7 windows Claude finished
within 15 calls, 2 were `git push`, which the local agent may not run.

Not tested (a new pre-registration would be needed): a Claude-comparable step
budget (~60) — at ~20 s per local step that is a 20-minute "keep going" turn.
