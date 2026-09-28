# Spike: which levers make a local model fast enough for per-step routing (2026-09-28)

Question: docs/spikes/per-call-proxy-2026-09-28.md found that routing Claude Code
"continuation" steps to `ollama/qwen3-coder:30b` worked (6/6 tasks, 0/19
validation failures) but was 17.7x slower (938s vs 53s baseline, n=6 tasks).
This spike measures which levers close that gap. Measurement only: no PR,
nothing deployed, no `llm-router` runtime code touched.

Code: `scripts/spikes/local_speed_replay.py` (lever harness),
`scripts/spikes/local_speed_wallclock.py` (wall-clock reconstruction).
Both import `scripts/spikes/per_call_proxy.py` (unchanged, from the parent
spike) for the Anthropic<->Ollama translation.

## Why replay, not live isolated Claude Code sessions

Confirmed by hand: `HOME=<empty scratch> claude -p "..." --setting-sources
project --strict-mcp-config --no-session-persistence` -> `Not logged in ·
Please run /login`. Isolated Claude Code cannot authenticate without
symlinking `~/Library/Keychains`, which is out of bounds here (the parent
spike did that and it was reverted; this task's own rules repeat the ban and
name the fallback: run against the golden-fixture harness instead). So this
spike replays the 24 REAL "continuation" request bodies the per-call-proxy
spike captured on 2026-09-28 (`routing-golden-v1` cases c001-c006,
`rsi-engine-probe`) against a live local Ollama, one lever at a time. Nothing
here calls Anthropic.

What this buys and what it does not:

- Per-call latency, schema validation, and **tool-choice agreement** with
  whatever tool the original successful routed run actually called at that
  exact decision point (ground truth for the 19 of 24 calls the parent
  spike's policy served locally; the other 5 were kept on Claude by the
  classifier and have no local ground truth).
- A wall-clock **estimate**, reconstructed by replaying the 2026-09-28
  session timeline (`cases.jsonl` + `routed.jsonl`) and substituting each
  lever's measured latency for the 19 originally-served calls, keeping every
  Anthropic-forwarded call's *original recorded* latency untouched.
- It does **not** re-run "tasks passing" live for any lever, including the
  best combination — that needs a live agent loop, which needs the auth this
  spike is not allowed to set up. The proxy for correctness used instead:
  0/24 schema-validation failures at every lever tested, plus tool-choice
  agreement against the known-good baseline trace.

**Baseline noise floor.** Replaying the *exact* baseline config (same model,
same server, no trim) against the 8 calls with ground truth agrees with the
original successful run's tool choice only 5/8 of the time (temperature > 0,
no fixed seed — same model, same prompt, different sample). Read every
agreement number in this doc against that ~60% floor, not against 8/8.

## Conditions (apply to every number below)

- Apple M5 Pro, 48 GB unified memory, Ollama 0.32.13, `qwen3-coder:30b`
  (Q4_K_M). Metal reports 37.4 GiB available to Ollama.
- **The machine was not idle.** `top` showed ~33 GB wired by other running
  apps (Chrome, Cursor, a VM) throughout testing, with the swap file active
  (up to ~27 GB used). Loading a second 20 GB model instance alongside the
  machine's existing default Ollama instance overflowed this and produced
  corrupted/empty responses and one hung runner (`llama-server` in `stuck`
  state) before this was diagnosed and fixed by never loading two model
  instances at once. Absolute latencies below are pessimistic versus a
  quieter machine; the lever-to-lever comparison (same machine, same
  contention, sequential loads only) is the number to trust.
- Sample sizes: exploratory lever runs used a stratified **n=10** subset of
  the 24 golden continuation calls (2 Read, 2 Edit, 2 Bash, 1
  ReportFindings, 2 classifier-kept, picked to span fast and slow cases from
  the parent spike). The baseline and the winning combination were also run
  on the **full n=24** set for the headline numbers. n=10/24 is small;
  treat medians as order-of-magnitude, not precise percentiles.

## The levers, in the order tested

### 1-2. Keep the model warm + keep num_ctx constant

Two servers, never loaded at the same time:

- **default**: the machine's existing `Ollama.app`-managed server (port
  11434), `OLLAMA_KEEP_ALIVE=15m`, `OLLAMA_NUM_PARALLEL=4`,
  `OLLAMA_MAX_LOADED_MODELS=2`, no flash attention, f16 KV cache (none of
  these set at the OS/launchd level — this is what `Ollama.app` already had
  running; matches the "Ollama.app discards launchctl env" condition from
  earlier work, so no attempt was made to change it in place).
- **tuned**: a hand-run `ollama serve` (not persistent — dies with the
  shell/session, per that same earlier finding) on port 11500:
  `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_KEEP_ALIVE=-1
  OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1
  OLLAMA_CONTEXT_LENGTH=32768`, plus a throwaway warm-up call before timing
  starts.

`num_ctx=32768` was already held constant on every call in the parent
spike's `to_ollama()` (verified by reading the code, not assumed) and stayed
constant here too. Evidence it never forced a reload: `load_duration` in the
winning combination's 24-call run stayed 0.08-0.16s on **every** call
(n=24) — a real model load on this hardware costs 8-110s, so this is a
flat line, not reload noise.

### 3. Trim the routed request

Condensed system prompt (366 chars, replacing the real 27,739-char one) +
a fixed 4-tool subset (`Read`, `Edit`, `Write`, `Bash` — the only tools this
fixture's one-function bug fixes ever need, of 24 sent originally; kept
**byte-identical name and `input_schema`** to what Claude Code sent, only
the other 20 tools dropped) + history capped to the first user turn plus the
last exchange. Measured, not estimated: `prompt_eval_count` (Ollama's own
token count) on the winning combination's 24-call run was **2,829-3,146
tokens** per call, comfortably under the 5k target (parent-spike untrimmed
calls carried ~21k prompt tokens). Tool defs alone: 87,549 -> 17,382 chars.

Caveat found while building this: the history trim only fires when a
conversation has more than 3 non-system turns; these toy fixture tasks
mostly don't, so on this fixture most of the token savings come from the
system-prompt and tool-def cuts, not history trimming. Flagged, not hidden
— the parent spike's own doc already cautions that toy-sized fixtures
under-count real multi-turn savings.

### 4. Prefix stability

By construction, the condensed system prompt and the 4-tool list (same
order) are byte-identical across every call in a trimmed run. Ollama's
0.32.13 `llama-server` log confirms a prompt cache is active
(`prompt cache is enabled, size limit: 8192 MiB`, `context checkpoints
enabled`). This spike did not instrument token-level cache-hit accounting,
but the observed `prompt_eval_duration` (0.29-0.91s for ~3k tokens, n=24,
after warm-up) is consistent with the shared prefix not being fully
recomputed on every call. Read as suggestive, not proven.

### 5. `think:false` + cap `num_predict`

`think:false` was already the default in the parent spike's translator
(unchanged, verified in code) — not a new lever here. What *is* new: capping
`num_predict` to 200 tokens when the previous tool was `Read`/`Bash`/none
(the model is picking the next obvious action) and 700 otherwise, versus the
parent spike's flat 4096-token ceiling.

### 6. Smaller model

Tried `qwen3.5:latest` (6.6 GB, already pulled — no new download) in place
of `qwen3-coder:30b`, same tuned server, same trim+cap. **It was not
faster**: median 7.12s vs 2.19s for the 30B coder model, and lower
tool-choice agreement (3/8 vs 4/8). No second model was pulled, per the
"only if small, and report it" instruction — `qwen3.5:latest` was the only
already-installed smaller candidate. Rejected for this workload: parameter
count was not the bottleneck once the server was warm and the request was
trimmed, and the smaller model was worse on both axes measured.

### 7. Hedge

Implemented as a real first-token deadline over Ollama's streaming
`/api/chat` (`asyncio.wait_for` on the first NDJSON line; abort and report
`hedge_timeout` if nothing arrives in time), not a whole-response timeout.
At an aggressive 3s deadline on the winning combination, **0/10** subset
calls hedged — once warm, every call's first token arrived under 3s. Hedging
matters only for the cold first call after a restart (8-110s observed here)
or a tail spike; not needed in steady state for this combination.

## Lever comparison table

All rows: `qwen3-coder:30b` unless noted; `n` = number of continuation
calls replayed; validation failures = schema-invalid or unparseable
responses (0 in every row); tool-choice agreement compares against the
known-good baseline trace where one exists (n/a where the classifier kept
that step on Claude in the original run).

| Lever | Server | Trim | `num_predict` cap | n | median | p90 | min-max | Validation failures | Tool-choice agreement |
|---|---|---|---|---|---|---|---|---|---|
| L0 baseline | default, `keep_alive 5m` | no | 4096 | 10 | 71.2s | 93.2s | 4.1-93.9s | 0 | 5/8 |
| L1a warm-only (**partial, aborted for time**) | tuned | no | 4096 | 2 of 10 | 4.2s, 8.5s (raw) | - | - | 0 | n/a |
| L2 smaller model | tuned | yes | 200/700 | 10 | 7.1s | 17.9s | 3.3-18.8s | 0 | 3/8 |
| L3 hedge sanity (3s deadline) | tuned | yes | 200/700 | 10 | 2.1s | 6.7s | 1.5-7.2s | 0 | 4/8 |
| **L1 winning combo** | tuned | yes | 200/700 | 10 | **2.2s** | **6.2s** | 1.8-6.5s | 0 | 4/8 |
| **Winning combo, full set** | tuned | yes | 200/700 | 24 | **2.2s** | **3.6s** | 1.6-4.0s | 0 | 11/19 |

L1a (keep-warm alone, no trim/cap) was killed after 2 of 10 calls because
uncapped generation made per-call cost unbounded and this spike's time
budget did not allow finishing a clean isolation of lever 1 alone; the 2
points collected (4.2s, 8.5s, both far below L0's 71s median for the same
calls) are consistent with "keeping warm" doing most of the work, with
trim+cap sharpening the tail, but this is not a controlled n for that claim
— read it as directional, not a measured row.

## Best combination

**Tuned dedicated `ollama serve`** (flash attention on, q8 KV cache,
`keep_alive -1`, single slot, single loaded model) **+ a warm-up call at
start + trimmed request (366-char system, 4-tool subset, ≤3-turn history) +
`num_predict` capped 200/700 + `qwen3-coder:30b`** (unchanged model) **+ an
8s hedge as a safety net** (0 calls actually needed it).

| Metric | Target | Result |
|---|---|---|
| Routed-call median | <=5s | **2.19s** |
| Routed-call p90 | <=10s | **3.59s** |
| Validation failures | - | **0/24** |
| Wall clock vs 53s baseline | <=2x (106s) | **69.4s estimated (1.31x)** |
| Tasks passing | 6/6 | **not measured live** (auth blocked); proxy: 0/24 validation failures + 11/19 tool-choice agreement (baseline noise floor ~5/8=63%) |

Wall-clock reconstruction (`local_speed_wallclock.py`, replaying the real
2026-09-28 session timeline, substituting this combo's latency for the 19
originally-served calls and keeping every Anthropic call's original recorded
latency):

```
c001: 6.1s over 3 calls (2 local)
c002: 8.3s over 4 calls (2 local)
c003: 18.1s over 7 calls (5 local)
c004: 14.8s over 6 calls (4 local)
c005: 13.6s over 6 calls (4 local)
c006: 8.5s over 4 calls (2 local)
TOTAL: 69.4s   (parent-spike baseline: 53s; parent-spike routed run: 938s)
```

This is a reconstruction, not a fresh live run (see "why replay" above): a
genuinely different local reply could in principle change what Claude Code
does next in a live session.

## Verdict

**Local is viable for per-step routing of easy continuation steps, on this
hardware, once the model stays warm and the request is trimmed — it is not
viable in the parent spike's un-tuned configuration.** The gap between 938s
(17.7x slower) and 69.4s (1.31x) is almost entirely the warm/dedicated
server plus the trimmed request; model size was not the lever that mattered
(the smaller model tested was slower, not faster). Cold start still costs
8-110s on this machine under real memory pressure — hedge to Claude, or
pre-warm at session start, rather than let a cold call block a step.

**Which step classes:** the "easy, obvious-next-tool" classes this spike
covers (`Read` a named file, run the test with `Bash`, summarise a tool
result as final text) hit target latency cleanly (median 2.2s) with 0
validation failures. The substantive `Read -> Edit` decision (the actual
code fix) was mostly kept on Claude by the classifier in the parent spike
and this spike did not re-measure code-fix *quality* at this speed — only
that schema-valid, fast replies are achievable. Tool-choice agreement
(11/19, against a baseline noise floor of ~60%) says the trimmed/capped
setup is not obviously worse at picking the right next action than the same
model asked twice under the original settings, but n=19 is too small to
call that proven, and it says nothing about `Edit` content quality.

**What would need to be true for a real build:** (1) a warm-up call at
session start plus a genuinely dedicated local server process (this spike's
"tuned" server is not what `Ollama.app`'s default install gives you, and
Ollama.app discards launchctl env — someone has to hand-run and keep-alive
this server, or package it); (2) the step-class trim list (here: 4
hardcoded tool names) generalized beyond this one fixture's tool set; (3) a
hedge to Claude for the cold-start and tail cases, since even the winning
combo saw an 8-110s cold call; (4) a real quality benchmark on the
`Edit`-class steps this spike did not route.
