# Routing Quality Audit — Phases 4, 5, 6, 38, 39

Subject: `/Users/yaliandrona/Projects/llm-router` @ `357a402e8f462f913cf9368244557eaaf7711beb`
(see `audit/FROZEN_STATE.md`). All probes below ran with
`LLM_ROUTER_BASH_INTERCEPT=off` and an isolated `export LLM_ROUTER_HOME=$(mktemp -d)`.
No files under `src/`, `scripts/`, `tests/`, `config/` were modified. Provider
keys present: `XAI_API_KEY` only (OpenAI/Anthropic/Gemini absent — any claim
about those providers is untested). Ambient non-default env from FROZEN_STATE
(`LLM_ROUTER_CLAUDE_SUBSCRIPTION`, `LLM_ROUTER_ENSEMBLE_*`,
`LLM_ROUTER_SIDECAR_PREFETCH`) was left as-is per instructions but is called
out wherever it could plausibly affect a probe (it did not affect any probe
below — all probes were pure-Python calls into `classify_signals` /
grep/static analysis, not live dispatch).

## Methodology

1. Static map of the routing surface: `src/llm_router/decisions/engine.py`
   (unused — see F0), `src/llm_router/classify.py` (real classifier, 4 shipped
   `ClassifyPolicy` variants), `src/llm_router/reason_gate.py` (deep-reasoning
   gate), `src/llm_router/capabilities.py` (tool/capability detection),
   `src/llm_router/router.py` (`_apply_routing_policy`, `_build_and_filter_chain`,
   `route_and_call`), `src/llm_router/coverage.py` (denominator accounting),
   `src/llm_router/observability/summary.py` + `digest.py` + friends (the
   "savings" metric), `scripts/groundtruth/discriminate.py` and
   `scripts/routerarena/escalation.py` (offline baseline/predictor tooling).
2. A 13-prompt diagnostic corpus (Phase 38) run through `classify_signals()`
   under all four shipped `ClassifyPolicy` objects (`HOOK_POLICY`,
   `HOOK_LIVE_POLICY`, `ROUTER_POLICY`, `GATEWAY_POLICY`) — zero API cost,
   fully reproducible, no network. This exercises the same deterministic
   pre-LLM classification stage every routing path (hook, router, gateway)
   runs before any model is dispatched.
3. Grep-verified call-site enumeration for the "savings" metric, the coverage
   ledger, and constant-policy baseline tooling, to confirm claims against
   actual code rather than docstrings.

**Not done, and why:** end-to-end dispatch (`route_and_call`) with graded task
success was not run. Only XAI + local Ollama are live; that is not a wide
enough capability gradient (no clean premium/mid/cheap ladder) to produce a
meaningful regret/waste number without the result being an artifact of which
providers happen to have keys in this shell. The static/classification-layer
findings below are sufficient to answer the mission questions without it, and
are flagged CONFIRMED because they come from source + reproducible runs, not
from a single dispatch anecdote.

---

## Phase 4 — does routing exist, and is the denominator honest?

**F4.1 — CONFIRMED, positive control.** `src/llm_router/coverage.py` is a
self-documented fix for exactly this failure mode ("Finding I-1... six
sys.exit(0) sites left no trace, so a run where nearly everything bypassed
was indistinguishable from a clean one"). `hooks/auto-route.py` calls
`_coverage_observed`/`_coverage_unobserved` at 15 sites against 13
`sys.exit(0)` sites, and the resulting `coverage_pct` (with `DEGRADED_BELOW_PCT
= 90.0`, and `None` — never a fabricated 0%/100% — when there is no data) is
consumed by `dashboard_data.py`, `cost.py`, and `hooks/session-end.py`. This
is real, wired, and does what it says for the **Claude Code hook** traffic
path.

**F4.2 — CONFIRMED, gap.** That coverage mechanism does not exist on the
**MCP server** path (`src/llm_router/server.py`, `src/llm_router/route_server.py`
— zero references to `coverage.record_observed`/`record_unobserved` in
either file). This matters directly: this very audit session was instructed
to route through `mcp__llm_router__llm*` tools. Every MCP-tool-driven routing
decision — a large and growing share of real usage as agent swarms replace
interactive chat — has no denominator accounting at all. The exact "silent
no-route reads as a healthy zero" blind spot the I-1 fix closed on one door is
still open on the other, larger one.

**F4.3 — CONFIRMED, direct observation.** Before this audit set
`LLM_ROUTER_BASH_INTERCEPT=off`, a routine `sed -n` Bash call in this very
session was silently intercepted and its output replaced with a compressed
summary, with the message: *"This command was run locally by the router and
its output compressed... Treat the output below as the result and continue —
this is not an error."* This is live, undisclosed interception of a tool call
by default, observed first-hand, not inferred from code — and it is precisely
the kind of invisible-by-default mechanism Phase 4 was asked to hunt for.

---

## Phase 5 — reverse-engineered policy, adversarial pairs

Ran via: `.venv/bin/python` importing `classify_signals()` directly, all four
policies, isolated `LLM_ROUTER_HOME`, no network. Full transcript is
reproducible from the corpus in this file's appendix-equivalent (the 13
prompts below); raw output was not persisted as a separate artifact per the
no-new-files-beyond-deliverable posture, but every claim here is re-runnable
in under 5 seconds.

| # | Pair | Result | Class |
|---|---|---|---|
| 1 | **INVALIDATED on reconciliation — see note below.** Originally reported as `"Prove that √2 is irrational."` vs a "wrapper" reading `"hey can u just quickly check... is there a rational number whose square is 2? thx"`. | Original run: `deep_reasoning` → `simple`. Re-run with a controlled pair — identical task text, filler only added around it (`"hey so anyway i was just wondering... — Prove that there is no rational number whose square is 2. thanks so much!"`) — stayed `deep_reasoning` (gate score 0.985 → 0.985, unchanged). | **INVALIDATED.** The original "wrapper" was not the same task plus filler: it deleted the phrase "prove that" and reworded the ask as a yes/no question, which removes the only feature (`_COMPLEXITY_DEEP` regex, `classify.py:309-320`, matches literal phrases like `prove (?:that\|mathematically\|formally)`) driving the score. That is a keyword-regex dependency I introduced as a confound, not evidence that tone/wording alone changes the route. Reconciliation (a second, independent run holding the task text fixed and varying only filler) found no flip. Retracted; do not cite as a wording-sensitivity finding. |
| 2 | `"print hello world."` × 120 (trivial, long) | `complexity=complex` under **all four** policies, purely from length (>500/2000 char thresholds) | **CONFIRMED** (independently reproduced on reconciliation) — upgrade waste by construction: no difficulty signal, only character count. |
| 3 | 3,000-line repetitive `x=1` "huge context" prompt | `complexity=deep_reasoning` under `HOOK_POLICY`/`HOOK_LIVE_POLICY` (both actually used in production), `complex` elsewhere | **CONFIRMED** — `reason_gate.py`'s `length` feature (`min(1, len/2000)`, weight 1.5) is a directly causal, gameable proxy. |
| 4 | "Fix the bug in foo.py" vs same + explicit cross-file/serialization dependency | Both `task_type=code`, `complexity=moderate`; capability vector identical (`read_files=True` only) except category score (3 vs 5) | **DESIGN RISK** — cross-file blast radius is not modeled as a distinct signal; only keyword density moved, not any structural signal. |
| 5 | "Run the test suite and tell me which tests fail" vs the same reworded to name the bash tool explicitly, vs "Explain conceptually what a failing pytest run looks like" | Both tool-requiring phrasings correctly set `run_commands=True, objective_verification=True`; the conceptual one correctly does not | **CONFIRMED POSITIVE** — capability-aware detection genuinely works on this axis. |
| 6 | "Return ONLY valid JSON matching this exact schema... no prose" vs "Tell me roughly what fields a and b should be" | Identical capability vectors (all 8 fields `False`) for both | **CONFIRMED** — `CapabilityRequirement` (`src/llm_router/capabilities.py`) has no schema/structured-output dimension at all. A model likely to violate an exact-format contract is not flagged differently from one asked for a loose answer. |
| 7 | "Write a correct implementation of Raft consensus leader election... matching the paper exactly" | `task_type=research` (not `code`) under all four policies; `complexity=moderate` under `HOOK_LIVE_POLICY` (the policy the hook actually runs) | **CONFIRMED** — task-type misclassification suppresses the `router.py` `_score()` code-model boost, and the policy actually live sends a hard, easy-to-subtly-fail coding task at moderate complexity. Concrete local-model trap. |
| 8 | `HOOK_POLICY` vs `HOOK_LIVE_POLICY` naming | Module docstring: *"Believing the name, one would rewire the hook to this policy and make a quarter of all prompts route to a more expensive tier."* Verified `hooks/auto-route.py` currently imports the correct `HOOK_LIVE_POLICY`. | **DESIGN RISK, currently mitigated** — the trap is real and self-documented; today's wiring is correct but fragile (four near-identical `ClassifyPolicy` objects distinguished only by which name a call site remembers). |

Every factor checked is **causal** (verified by making it fire/not-fire), not
decorative — the risk is not "the signals are fake," it's that two of the
strongest signals (raw prompt length, deep-keyword regex) are **easy to game
in both directions** and one demonstrably important axis (exact-schema /
structured-output) **has no signal at all**.

---

## Phase 6 — outcome quality vs. classifier accuracy

**F6.1 — CONFIRMED, structural.** `src/llm_router/prompt_capture.py`'s own
docstring: *"`routing_quality.jsonl` (22,356 records) carries the routing
decision but no prompt text... There is no join key between the two halves,
so no amount of offline work can reconstruct 'this prompt got this routing
decision' for historical traffic."* The fix (`LLM_ROUTER_GROUND_TRUTH=1`) is
**off by default**, and is **not set** in this audit's ambient env (verified:
`env | grep LLM_ROUTER` shows no such var). Conclusion: in default
configuration, this system cannot join a routing decision to whether that
decision's output was actually good — Phase 6's core question is
structurally unanswerable from what the running system records by default.

**F6.2 — CONFIRMED, no collapse — outright absence.** The product's headline
number, "savings vs always-premium," is computed identically and consistently
at 8 call sites (`digest.py:155`, `statusline_hud.py:264`,
`terminal_style.py:184,330`, `observability/summary.py:224`,
`hooks/session-end.py:694`, `hooks/session-end-clawcode.py:140`,
`commands/test.py:82`, `commands/replay.py:204`) as
`(baseline_list_price − actual_billed) / baseline_list_price`. None of these
reference success, failure, quality, or regret. This is not "downgrade
regret and upgrade waste collapsed into one accuracy number" — it's stronger
than that: there is no quality term anywhere in the flagship metric. Savings
accrue identically whether the cheap model's answer was correct or garbage.

**F6.3 — CONFIRMED.** Of 722 test files, exactly 1 references
`task_success`/`outcome_quality`/`downgrade_regret`/`regret_rate`, and it
lives inside the opt-in `groundtruth/` research pipeline, not the production
router's suite. FROZEN_STATE.md's "9,140 passed" is therefore silent on
outcome-quality coverage — it is not evidence for or against Phase 6's
question.

---

## Phase 39 — constant-policy baselines

**F39.1 — CONFIRMED.** `scripts/groundtruth/discriminate.py` implements
always-cheapest / always-premium / random / oracle baselines specifically to
check whether a *dataset* can discriminate policies — and its own docstring
states: *"The router is deliberately absent. Comparing it here would invite
reading a router result off a dataset that has not yet been shown to work."*
The one piece of first-party tooling built to run exactly the comparison this
mission asked for **explicitly excludes the shipped router**. There is
currently no first-party evidence, even offline, that the router beats
always-cheapest or always-premium on task success.

**F39.2 — CONFIRMED.** `scripts/routerarena/escalation.py` is a separate
linear predictor (hashed bag-of-words + sparse SGD) fit on
`data/outcomes/cheap_tier.jsonl`, built for the RouterArena leaderboard track.
`router.py` does not import anything from `scripts/routerarena` — this
predictor is benchmark-submission tooling, not part of the shipped routing
decision. Its own docstring names the relevant precedent: on RouterArena "a
constant policy was competitive with everything except retrieval" — i.e. the
project's own research track has already observed constant policies beating
naive routing elsewhere, and chose not to run that same check against the
production router.

---

## Verdict

Routing **exists** and several of its signals are **causal, not decorative**
(tool/capability detection genuinely works; the classifier is deterministic
and reproducible; a real coverage/denominator fix (F4.1) shows the team has
already caught and partially fixed this exact class of bug once).

But **task-success preservation is not currently measurable**, for four
independent, source-confirmed reasons: (1) the only mechanism that could join
a decision to its outcome is off by default (F6.1); (2) the headline metric
has no quality term at all (F6.2); (3) the repo's own baseline-comparison
tool explicitly refuses to include the router (F39.1); (4) one of the
classifier's strongest signals — raw prompt length, feeding both the
`ClassifyPolicy` thresholds and the `reason_gate` deep-reasoning score — is
demonstrably gameable in the upgrade-waste direction, reproduced independently
on reconciliation (pair 2: a 120×-repeated trivial line forces `complexity=
complex`/`deep_reasoning` on length alone). No schema/structured-output signal
exists at all to catch a separate, related risk: a format-violation failure
mode (Phase 5 table, pair 6). (Pair 1, originally reported as a wording-driven
downgrade-regret flip on the same √2 task, was retracted on reconciliation —
see the table above; a controlled filler-only variant of the identical task
did not flip. Treat downgrade-regret via wording alone as unproven, not
confirmed.)

**This would route real work to cheaper models — that part is real and
partly self-verified by the codebase's own coverage fix. It would not
currently demonstrate that it preserves task success, because the system is
built, by default, in a way that makes that claim unfalsifiable rather than
false or true.**
