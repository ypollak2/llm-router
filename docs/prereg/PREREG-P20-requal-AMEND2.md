# PREREG-P20-requal AMEND2: qwen3.8 as the only local arm, thinking off, a foreign-arm guard

This file is frozen by the hash in `PREREG-P20-requal-AMEND2.sha256`.
It amends `PREREG-P20-requal.md` (sha256 `59e27eb889601b4eff80cac645b21442333ff850109219ef10b6de68bd2029b2`,
frozen_utc 2026-10-16T15:43:02Z) as already amended by `PREREG-P20-requal-AMEND1.md` (sha256
`6c5b867ac97198b70109d1b5961de6ec64e2f1aba3b0b38068c090f70cfdeabd`, frozen_utc 2026-10-16T17:22:05Z).
It changes only the items below. Everything else in the prereg and in AMEND1 stands word for word.
The harness (`scripts/p20_requal.py`) refuses every gated subcommand unless this amendment is
frozen, frozen after AMEND1, and its constants block is exactly the six keys below.

## Why (owner instruction, D-166)

The owner instructed: "The local model should be qwen3.8 and not the other early versions of
qwen". Coordinator decision D-166 carries this out. It **reverses prereg §0 Q9**, which kept the
plan's three models and excluded `qwen3.8-uncensored:latest` ("not the plan's qwen3.8, which needs
an approved Ollama upgrade").

## Changes

1. **One local arm.** For both the `qa` set and the `code` set, the only local arm is
   `qwen3.8-uncensored:latest`. `qwen3.6:35b-a3b-coding`, `qwen3-coder:30b` and `qwen3.5:latest`
   are removed from every arm list, and their `ID_*` pins are void. The `tech_op` set has the same
   arm list, but it stays terminal (n = 1, `terminal tech_op n_min` in `use.log`). Sonnet
   (reference), Haiku (report only) and the Opus judge are unchanged.
   * In code, the prereg's `CONSTANTS` stay as frozen. `frozen()` applies AMEND1's overrides, then
     drops every prereg `ID_*` key and applies the block below.
   * The code runner's `--arm` choices come from the same amended list:
     `[qwen3.8-uncensored:latest, sonnet]`.
2. **Thinking off on every local call, as in production.** Production sends `think: false`
   (`src/llm_router/providers.py` `_ollama_think_enabled`, off by default;
   `hooks/direct_executor.py` and `hooks/agent_loop.py` hard-code it).
   * **Q&A and tech-op answers and the warm-up:** every `/api/chat` body carries `"think": false`.
     The existing `<think>` stripping (§7) is kept.
   * **Code runner, Pi agent (pi-coding-agent 0.99.1):**
     * Pi talks to Ollama's OpenAI-compatible `/v1` endpoint. That endpoint thinks by default,
       and it sends no `think` field.
     * The runner now writes its own Pi config, `<run-dir>/pi_config/models.json`, and points
       `lib_pi.PI_CONFIG_DIR` at it. The research `pi_config` is not changed, and it has no
       qwen3.8 entry.
     * In that config the model has `reasoning: true`. `off` is its only thinking level, mapped to
       `reasoning_effort: "none"`. Every other level is `null` (unsupported). So Pi's default
       level (`medium`) is clamped to `off`, and every request carries `reasoning_effort: "none"`.
   * **Verified 2026-10-16, synthetic prompt "Reply with the word OK." only:**

     | Path | Thinking output | Output tokens |
     |---|---|---|
     | `/api/chat` with `think: false` | none | 2 |
     | `/v1/chat/completions`, no reasoning parameter | present (80 chars) | 25 |
     | `/v1/chat/completions`, `reasoning_effort: "none"` | none | 2 |
     | Pi with `reasoning: false` (the style of the research config) | `thinking` block | 15 |
     | Pi with the runner-generated config | text block only | 2 |
3. **Context window.** `num_ctx` stays `local_models.num_ctx(arm)`. For
   `qwen3.8-uncensored:latest` that is `DEFAULT_NUM_CTX` = **32,768**, because the model is not in
   the table. This matches the server's `OLLAMA_CONTEXT_LENGTH=32768`, read from the live
   `ollama serve` environment on 2026-10-16.
   * The Pi path does not send `num_ctx`. There the server value applies, and Pi's
     `contextWindow` is the same 32,768.
4. **Foreign-arm guard.**
   * `run` and `judge` refuse (Refuse, exit 2, before any model call or write) if
     `answers/<set>.jsonl` holds a row whose arm is not in the current arm list.
   * `ingest-code` refuses on such a row in its `--results` file.
   * `score` refuses on such a row in `answers/qa.jsonl`, `answers/tech_op.jsonl` or
     `code_results.jsonl`. It checks before it writes its `started` line, so a refusal does not
     use up the single use.
   * `status` reports `foreign_arm_rows` per file (`qa`, `tech_op`, `code_raw`, `code`). It gives
     counts only.
5. **The amendment is enforced.** `frozen()` checks this file the way it checks AMEND1:
   * the sidecar must match;
   * its `frozen_utc` must fall strictly after AMEND1's `frozen_utc` and before now;
   * the constants block must have exactly the six keys below;
   * the four fixed values must equal the harness values;
   * `REPO_SHA` must be 40 hex characters and `CODE_RUNNER_SHA256` 64;
   * if `<run-dir>/PREREG-AMEND2.md` exists, it must be byte-identical.

   `score`'s `started` line also records `amend2_sha256`. AMEND1's checks run first and are
   unchanged.

## No data consumed

* **What exists.** A qa run of the old arm `qwen3.6:35b-a3b-coding` started under the prereg and
  AMEND1 and stopped on 2026-10-16 at about 23:36Z (the `answers/qa.jsonl` mtime is 23:36:17Z).
  Harness `status` at 2026-10-16T23:49:25Z read **23 rows in `answers/qa.jsonl`, all for that arm.
  21 of them are final.** Retry attempts account for the difference.
  * **Contradiction with D-166:** the coordinator's brief said 16 rows. The move-aside must
    record the count it actually moves.
* **What does not exist.** Nothing has been judged, ingested or scored (`judged {qa 0, tech_op 0}`,
  `code_rows 0`, `spend_usd 0.0`). `use.log` holds one `selected` line and one `terminal` line
  (`tech_op n_min`) and nothing else.
* **Selection.** The selection is unchanged and nothing new is selected: `qa` n = 118, `code`
  n = 75, `tech_op` n = 1 (terminal).
* **The old rows.** Before any AMEND2 run, the coordinator moves `answers/qa.jsonl` aside and
  records its sha256 and row count. The old rows are never judged or scored. The foreign-arm guard
  refuses `run`, `judge` and `score` if the move is forgotten.

## Models (replaces the three local rows of §3)

| Arm | Sets | Identity pin (checked before every `run` and code-runner arm; mismatch = refuse, not a result) |
|---|---|---|
| `qwen3.8-uncensored:latest` | qa, tech_op (terminal), code | `ollama list` ID `b3f4c17c6811`; `/api/show` gguf / Q6_K / llamacpp / 27.3B, family `qwen35`, parent_model `Qwen3.8-27B-Uncensored-Q6_K.gguf`, capabilities completion, tools, thinking |
| `sonnet`, `haiku`, `opus` | as in §3 | unchanged |

* Ollama 0.40.0 (`ollama --version`, 2026-10-16). No upgrade and no pull was made.
* Warm speed on the synthetic smoke: 2 eval tokens at about 23 tokens/s. A cold load takes
  7.3 s, and the 22 GB model is a dense 27.3B.

## REPO_SHA and where runs execute

* **New REPO_SHA `f82166d5ef984959b16b6443ac0881d6517c2fdf`.** It is the AMEND2 code commit,
  holding the changes above, and the last commit that touches `scripts/`, `src/` or `tests/`. This
  docs commit comes after it and touches only `docs/`.
* As in AMEND1, runs execute from the freeze commit of this amendment. The code gate requires
  `scripts/`, `src/` and `tests/` there to equal REPO_SHA.

## Pins

* **Changed by the block below:**
  * `LOCAL_MODELS`, `CODE_LOCAL_MODELS`, `SIZE_ORDER`;
  * `ID_qwen3.8-uncensored:latest` (new);
  * `ID_qwen3.6:35b-a3b-coding`, `ID_qwen3-coder:30b`, `ID_qwen3.5:latest` (void);
  * `REPO_SHA`;
  * `CODE_RUNNER_SHA256` becomes `7fb952a10d024e3281d3a6615ea38275671fb540e023ac89c20f6e12f8017a4a`,
    because the runner now writes the Pi config and takes its arm choices from AMEND2.
* **Unchanged:**
  * every other prereg constant;
  * the ten source pins and the fixtures tree;
  * the three pool sha256s, the build manifest and the proxy copy;
  * the Claude model ids;
  * the AMEND1 checklist (S4-S7).

## Known biases added to §16 (stated before any AMEND2 data)

15. **A community build, not the official qwen3.8.** The arm is a community "uncensored" GGUF at
    Q6_K (`parent_model` `Qwen3.8-27B-Uncensored-Q6_K.gguf`; `/api/show` reports family `qwen35`).
    Results speak for this build, `b3f4c17c6811`, only. They do not speak for the vendor's qwen3.8
    weights or for another quantisation. The "vendor weights" sentence of §4 does not hold for
    this arm, though nothing here is tuned on any item.
16. **Thinking disabled.**
    * All local calls run with thinking off: native `think: false`, and Pi
      `reasoning_effort: "none"`. This matches production routing.
    * A thinking-on run of the same weights could score differently: higher, or lower through the
      180 s / 900 s timeouts.
    * The earlier [NC] and M3.5 Pi runs used `reasoning: false` configs. For a thinking-capable
      model that leaves Ollama's `/v1` default, which is thinking on. So the code harness is not
      identical to those priors.
17. **A single local arm.**
    * "No local model eligible" now means this one model. The same holds for `cancel_p21`,
      `cancel_p210` (`decisive` over one lane), `kill_all`, the D-21 table and `e_tech`.
      `SIZE_ORDER` has nothing to break a tie between.
    * A failure of this build kills the local Q&A path on one model's evidence.
18. **Prior [RX] outcome from a different build.** The [RX] 4/37 of §4 ("qwen3.8 at the time")
    came, per D-166, from a different qwen3.8 build than `b3f4c17c6811`. The leakage statement of
    §4 is unchanged: those outcomes are known in aggregate, and they are not this stratum's result.
19. **Timeout pressure.** A dense 27.3B at about 23 tokens/s gives roughly 4,000 output tokens in
    the 180 s local timeout. Long answers time out more often than they would on the MoE arms
    the prereg planned. Timeouts are charged to the arm (§6: one retry, the last attempt is final;
    §16 item 11 for the code runner).

## Run rule

Unchanged:

* the night window (00:00-07:00 local);
* the quiet gate (three readings; no foreign model resident, so `qwen3.8-uncensored:latest` must
  be the only resident model, or none);
* the $60.00 spend cap;
* the order of §13.

Before the first AMEND2 `run`, `answers/qa.jsonl` is moved aside as described above.

Pre-run checks added at the freeze (independent review NITs 2-3, procedure, no code change):

* **Pi version.** The code arm's "thinking off" relies on Pi 0.99.1 internals (its default level
  falls back to off and every request carries `reasoning_effort: "none"`). Before the first code
  run, `pi --version` is recorded in PROGRESS and must print `0.99.1` (it did at the freeze); any
  other version stops the code arm until a new amendment.
* **No foreign code rows.** Before each code-runner invocation, harness `status` must show zero
  foreign-arm rows in the raw code results; the runner itself does not check this, and
  `ingest-code` would refuse only after the night's runs.

## Constants (override the prereg's and AMEND1's values of these keys; every other prereg ID_ pin is void)

```text P20-AMEND2-CONSTANTS
LOCAL_MODELS=qwen3.8-uncensored:latest
CODE_LOCAL_MODELS=qwen3.8-uncensored:latest
SIZE_ORDER=qwen3.8-uncensored:latest
ID_qwen3.8-uncensored:latest=b3f4c17c6811
REPO_SHA=f82166d5ef984959b16b6443ac0881d6517c2fdf
CODE_RUNNER_SHA256=7fb952a10d024e3281d3a6615ea38275671fb540e023ac89c20f6e12f8017a4a
```

## Freeze record

* Written by the build agent (Claude) under D-166. The code commit was also written by Claude, not
  by Codex; under D-137 (Codex unavailable, builds go to Claude agents) that is the standing rule,
  not a deviation.
* Independent review of d1f2c043..ad3ea4c0: PASS, no MUST or SHOULD, 4 NITs (18/18 mutants red).
  At the freeze the coordinator changed only this document: the §6/§16 citation (NIT 4), the two
  pre-run checks above (NITs 2-3) and this record. NIT 1 (a changelog fragment line) needs no
  change.
* Frozen by the coordinator. The sidecar holds the sha256 of these bytes and `frozen_utc` from
  `date -u`. The independent freeze-guard review of the frozen bytes is commissioned by the
  coordinator. A byte-identical 0444 copy goes to
  `requal/b2/PREREG-AMEND2.md`, and the harness checks that copy if it is present.
