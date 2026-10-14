# G1 evidence, synthetic (D-99), 2026-10-14

Every number on this page is labelled **synthetic (D-99)**. None is organic, and none is mixed into
organic counts. The raw files sit beside this page in `docs/measurements/`. The commands that
produced them are in `scripts/g1_synthetic_evidence.py`.

## Owner decisions recorded in this session (2026-10-14)

- **D-99:** "The G1 evidence can be done synthetically by the machine." For G1 only, this replaces
  D-42's organic-only clauses (P1.1-b fidelity, P1.9 offload, P1.7-d adoption and every "organic"
  clause). Every synthetic figure is labelled and kept apart from organic counts.
- **D-100:** "P1.6-g is approved to pass." P1.6-g is PASS by owner decision. It stays FAIL as a
  measurement under D-72, and the classifier stays in shadow.

## Verdict

**G1 is NOT passed.** Clause (3), context fidelity (P1.1-b), FAILs on the Sonnet stratum, and the
local stratum and P1.7-d cannot run in this container. P2 merges stay blocked (D-76).

| G1 clause | Item | Synthetic result | File |
|---|---|---|---|
| (1) every P1 MUST | mechanical MUST tests on main 9c244981 (16 sets) | all 0 fail (PROGRESS 2026-10-14) | — |
| (1) | P1.1-a seven doors | PASS: spy test merged in #507 | — |
| (1) | P1.2-a summary coverage and spawn p95 | **PASS**: 24/24 sessions with ≥ 12 messages covered; spawn p95 22.1 ms (bar 50); 0 worker failures | `p12a-synthetic.json` |
| (1) | P1.7-d nimble shadow adoption | **NOT RUN**: needs nimble:9b in Ollama; ollama.com and huggingface.co are blocked by this container's network policy | — |
| (1) | P1.9-a O3 integrity | **PASS**: 3 measurable sessions at ratio 1.000 (bar 0.90-1.10); the coverage-50% session is UNMEASURABLE and the 30-prompt session is not a gate unit, both as designed | `p19a-synthetic.json` |
| (1) | P1.10-a identity per path | **PASS**: proxy 1,320/1,320, usage 240/240, execution_events 314/314 (each ≥ 200, 100%); replay probe envelopes reported apart | `p110a-p09-synthetic.json` |
| (2) | P1.6-g | **PASS by owner decision D-100** | — |
| (2) | P1.7-a | N/A (D-82) | — |
| (3) | P1.1-b Sonnet stratum, PACK vs FULL | **FAIL**: n=60, worse 20, better 0, (w-b)/n = 0.333 > 0.02, exact McNemar p = 1.9e-6 | `p11b-synthetic-sonnet.json` |
| (3) | P1.1-b local stratum, PACK vs HOOK6 | **NOT RUN**: needs a local model (blocked as above) | — |
| (4) | G0 guards P0.9-a..c | **PASS (synthetic)**: session-start p95 240 ms (n=60, bar 2,000); status-bar p95 94 ms (n=300, bar 300); statusline p95 44-47 ms (n=200 x 3 conditions, bar 100). The "last 7 days live" reading needs the live install | `p110a-p09-synthetic.json`, `p09c-statusline-synthetic.txt` |

## P1.1-b: what failed and why

Prereg `docs/prereg/PREREG-P11B-synthetic.md` was frozen at 2026-10-14T19:09:54Z (sha256
`231fe6a4…`, `check_prereg.py` match) before any scored model call. The harness ran at 543c8bda.
Model: claude-sonnet-5-5 through the `claude` CLI. 0 errors; all 60 items were in pack mode.

| Category | n | PACK pass | FULL pass | worse |
|---|---|---|---|---|
| recent (last tool result) | 30 | 30 | 30 | 0 |
| early_user (first request) | 10 | 10 | 10 | 0 |
| early_tool (early tool output) | 10 | 0 | 10 | 10 |
| early_assistant (early decision) | 10 | 0 | 10 | 10 |

In pack mode the pack holds the last 7 messages, the request and the rolling summary. With no
local model the summary is **extractive**: user requests, files and tool counts. It holds no tool
output and no assistant decisions, so every fact that lived only in early tool output or an early
assistant message is gone. The dry check before the run predicted exactly this.

Two readings matter for the owner:

1. **The window was forced small.** The prereg fixed `target_window = 8192` so that every item is
   in pack mode. For a cloud Sonnet door the real window is ~200k tokens, and these 16k-token sessions
   would be in **full** mode, where PACK = FULL by construction. The measured loss applies to sessions
   longer than 0.8 x the target window, and to small local windows.
2. **The obvious repair needs a local model.** The P1.2 summary is meant to be written by a local
   model (five sections, including decisions). Extractive is the fallback. A model-written summary
   could carry early decisions and key tool facts. Tuning the extractive heuristics to this
   generator would overfit it, so no such repair was tried.

The kill rule (park after 2 failed repairs) is not reached: 0 repairs were run. P1.1-b is parked
here pending an owner call (see "Owner decisions needed").

Spend: $8.73 for the 120 scored calls (CLI-reported `total_cost_usd`). A 1-item smoke run after the
freeze, to check CLI auth, added $0.15; it was not scored. The total is above the prereg's $5
estimate and under the plan's ~$18 P1.1-b cap.

## P1.9-a: O3 integrity (P1.9 variant of the M0.3 check, D-58 population rules)

`o3_integrity()` in the harness counts, per session:

- O3 human turns: `offload_share.build_units` + `turn_units`, with the transcript join, the same units
  `kpi` counts;
- typed prompts: `o3_transcripts.is_typed_prompt`;
- proxy coverage: main-thread assistant message ids found in the ledger.

The synthetic sessions carry the shapes that broke the M0.3 count (PR #295), plus the other host
shapes. Host shapes:

- one message written as several entries;
- tool loops;
- sub-agents under `subagents/`;
- task notifications (`promptSource: system`);
- slash commands;
- a prompt typed beside tool results;
- interrupts;
- one compaction.

Ledger shapes:

- side calls;
- calls that no transcript holds (orphans);
- failed calls that are retried;
- sub-agent first calls mislabelled `turn_first`;
- lost rows.

**Bug found and fixed in this PR:** `is_typed_prompt` counted the host's `[Request interrupted …]`
marker and its compaction summary (`isCompactSummary`) as typed prompts. Before the fix the ratios
were 0.965 / 0.976 / 0.965. After it they are 1.000 / 1.000 / 1.000. The proxy's step labeller
already read the interrupt marker as not human text.

The check can fail: without the transcript join, the proxy-only rule keeps the orphan calls and
the ratio rises above 1.10. `tests/test_g1_synthetic_evidence.py` checks this.

Limit: the generator encodes our understanding of the host's transcript shapes. A shape it does not
know is not tested. The kind `synthetic` stands in for `organic`.

## P1.10-a and P0.9-a/b: $0 replay

`scripts/synthetic_replay.py` (the D-42 world) drives the corpus through every door and hook:

- a stub upstream;
- a network guard;
- a scratch HOME.

The corpus is `g1_synthetic_evidence.py replay-corpus`: 60 sessions x 5 turns, with sub-agents in
turns 2-5 (1,320 records). Identity uses kpi's P1.10-a rule (`_identity_share`): session_id not
`unknown:*`, a real task_id (`gen-` is missing), bar 99%, 200 rows per path. Latency comes from the
replay's per-hook wall times. The replay rows are tagged `harness`, so `llm-router kpi --identity`
itself excludes them, and the readout applies the same rule directly.

The replay (1,020 s wall, 0 errors, 0 network violations) gave:

| Path | n | with session_id + real task_id | Verdict |
|---|---|---|---|
| proxy (host-shaped calls) | 1,320 | 1,320 (100%) | PASS |
| usage (sub-agent route rows) | 240 | 240 (100%) | PASS |
| execution_events | 314 | 314 (100%) | PASS |
| proxy probe envelopes (not scored) | 900 | 0 | — |

`routing_decisions` and `direct` had no traffic in this world: the stub serves no DIRECT local
answers. Sub-agent proxy calls (`subagent_first`) carry their parent turn's task id.

**Probe envelopes.** `synthetic_replay.py` also sends each turn-first record as three probe
envelopes (`nosys`, `bare`, `reminder`). Each is sent under its own session id `<sid>-<variant>`,
which no host prompt ever started, so no hook began a turn and the proxy stamps a `gen-` task id.
The first readout counted them and gave proxy 59.5%. They are not host traffic, so they are
reported apart. The same thing happens live when a session's UserPromptSubmit hook never runs
(hooks disabled), and kpi then correctly counts those rows as missing.

| Hook (G0 guard) | n | p50 | p95 | Bar | Verdict |
|---|---|---|---|---|---|
| P0.9-a session-start | 60 | 188 ms | 240 ms | 2,000 ms | PASS |
| P0.9-b status-bar (UserPromptSubmit) | 300 | 70 ms | 94 ms | 300 ms | PASS |
| P0.9-c statusline, cold | 200 | 35 ms | 46 ms | 100 ms | PASS |
| P0.9-c statusline, warm | 200 | 33 ms | 44 ms | 100 ms | PASS |
| P0.9-c statusline, refresher hangs | 200 | 36 ms | 47 ms | 100 ms | PASS |

These are wall times in a quiet container (load1 0.8 at the bench start). The live clauses gate on
all organic rows, contention included (§1.4), so they measure something different.

## Owner decisions needed

1. **P1.1-b:** choose one.
   - (a) Accept the Sonnet-stratum FAIL as a property of the extractive fallback, and amend G1 (3)
     to measure at the door's real window.
   - (b) Run repair 1: a local-model summary (`--summary-model`), on the owner's machine or here once
     ollama.com and huggingface.co are allowed.
   - (c) Keep G1 blocked.
2. **Local stratum and P1.7-d:** both need a local model. Allow ollama.com and huggingface.co in this
   environment's network policy, or run
   `scripts/g1_synthetic_evidence.py fidelity --stratum local --local-model <m>` on the owner's machine.
