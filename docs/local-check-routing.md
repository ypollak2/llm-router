# Local-check routing (LC-8)

The command that measures a model and switches lanes on or off is described in [local-check.md](local-check.md).

Owner decisions: D-260 (a passed lane routes local-first by default, on the verified text-job path only), D-262 (effective lane: the user's own fresh local-check result, else pre-registered, else unknown), D-258 (the proxy authority stays off; pre-registered lanes are record-only).

## What routes, and when

**The env flag (D-272).** `LLM_ROUTER_LOCAL_TEXT_JOBS` is no longer needed for a passed lane:

| `LLM_ROUTER_LOCAL_TEXT_JOBS` | Passed, routable lane for the job | `llm_text_job` |
|---|---|---|
| unset | yes | runs, local lane first |
| unset | no (no pass, stale digest, settings mismatch, disabled, demoted, input too long) | returns `fallback`, no model call |
| `1` / `true` / `yes` / `on` | any | runs (opt-in; also without a pass); a passed lane goes first |
| `0` / `false` / `no` / `off` | any | returns `fallback`: global off |

The unset-flag check is `eligibility_routing.passed_lane_available(job, input_len, now)`, which applies the same guards as `prefer_passed` below. The `llm_text_job` tool is
listed when the flag is on, or unset and some lane is switched on in `local_routing.json`.

**Ollama only (D-274).** `prefer_passed` considers `ollama/` models only. A PASS measured on LM Studio, vLLM or llama.cpp is recorded but switches nothing on; `local-check run` and `status`
print `measured; routing for this runtime is not supported yet (Ollama only in this release)`.

Only `llm_text_job` (`commit_message`, `pr_description`, `summarize_output`). Plain `llm_*` calls, `llm_edit` and the proxy never use this.
`eligibility_routing.prefer_passed(models, job, input_len, now)` runs at the router's eligibility step, after D-116 and the F106/F112 step, and moves a local `ollama/` model that is **already in the chain** to the front. It never adds a model. All of these must hold, otherwise the chain is returned unchanged:

| Condition | Source |
|---|---|
| routing enabled for (model, job) | `local_routing.json` |
| effective lane is `passed` | `local_eligibility.json` (`text_job_local` first, then `text_job`) |
| lane verified | a local-check lane needs its stored `model_digest` to equal the digest in the inventory cache (`discovery.json`, no HTTP). A missing or stale (older than 7 days) cache, or no digest for the model, means no preference. The 30-day no-digest rule of the display (`local-check list`) is display only |
| no prereg fallback for a local-check entry | an entry enabled with `source: local_check` whose own lane went stale (model changed) is not routed on a pre-registered lane. Only `prereg` / `manual` entries may use one |
| input fits | `len(input) + len(hint) <= min(lane.scope.max_prompt_chars, window of the router's ctx)` |
| settings parity | route-time `think` is off, and the router's `num_ctx` is **>=** the `ctx` the check measured with (stored on the lane with `think`). Unknown router ctx means no preference |

Settings parity replaced the ctx hash equality of LC-8: a model with a smaller trained window, a Modelfile `num_ctx`, an `llmr-*` alias or an env override was silently never preferred. A lane recorded before `ctx` was stored keeps the old rule (the route-time settings must hash to its `settings_hash`).
Temperature is not part of the claim: local-check measures at 0 and the router calls with 0.2. The acceptance check gates every answer either way.
When a passed lane is withheld for a settings reason (`think_on`, `ctx_unknown`, `lane_think`, `ctx_below_measured`, `settings_mismatch`, `no_scope`) or an evidence reason (`digest_unverified`, `local_lane_stale`), the call records a `not_preferred` skip cause (`llm-router why` shows it) and, for settings, `llm-router doctor` prints a line. It is never silent.

A pre-registered lane has no scope, digest or settings. It is preferred only after an explicit `enable`, only with thinking off, and only when the input fits the window the router will request. It is not digest-checked: there is no digest to check.
The router's Ollama window comes from `LLM_ROUTER_OLLAMA_NUM_CTX` or the shared local window table.

The acceptance check and the Claude fallback in `llm_text_job` are unchanged: a rejected local answer still ends in "do this job yourself".

## The switch: `local_routing.json`

```
{"<model>": {"<job>": {"enabled": true, "since": "2026-10-20T10:00:00Z", "source": "local_check"}}}
```

A disabled entry also holds `reason` and `disabled_at`. Python API (`llm_router.local_check.routing`):

- `enable(model, job, source="manual")` writes an enabled entry (source `local_check`, `prereg` or `manual`). Pass the inventory name with its tag; a name without one is stored as `<name>:latest`.
- `disable(model, job, reason="manual")` writes `enabled: false`.
- `is_enabled(model, job)`.
- `on_pass(model, jobs)` is called after a recorded local-check PASS: enables each lane that has no entry, returns `{job: "enabled" | "already_on" | "kept_off"}`. A lane disabled earlier (by the user or by the monitor) stays off.

`enable`, `disable` and `on_pass` each do one read-modify-write under the file lock (`local_routing.json.lock`), with a unique temp file per write.
`llm-router local-check enable|disable <lane> [--model M]` call `enable` (source `manual`) / `disable`; a passing run calls `on_pass` and prints one line per lane. The CLI lives in `commands/local_check.py`.
Model names are compared with `:latest` normalised.

## Demotion monitor

Each `llm_text_job` attempt appends one row per LOCAL attempt to `text_job_local_attempts.jsonl` (`ts`, `job`, `model`, `outcome`: `ok`, `rejected` or `error`; no prompt text). An `ollama/*` model that the router tried and moved past (error, timeout, contract rejection) counts as `error` even though a later chain entry served the call, and a rejected first attempt counts even if the retry is accepted. After the job, `demotion_check(model, job)` reads the tail of that file. If more than 30% of the last 20 local attempts for that (model, job) since it was enabled were not `ok` (7 or more of 20; the window must be full), routing is disabled with `reason: "demoted: fallbacks"` and `llm-router doctor` prints a line. It never re-enables; only an explicit enable does.
Not covered: a call where every chain entry fails raises out of `llm_text_job` and records no attempt row.
