# Local-check routing (LC-8)

The command that measures a model and switches lanes on or off is described in [local-check.md](local-check.md).

Owner decisions: D-260 (a passed lane routes local-first by default, on the verified text-job path only), D-262 (effective lane: the user's own fresh local-check result, else pre-registered, else unknown), D-258 (the proxy authority stays off; pre-registered lanes are record-only).

## What routes, and when

**The env flag (D-272).** `LLM_ROUTER_LOCAL_TEXT_JOBS` is no longer needed for a passed lane:

| `LLM_ROUTER_LOCAL_TEXT_JOBS` | Passed, routable lane for the job | `llm_text_job` |
|---|---|---|
| unset or empty | yes, and the passed model leads the router's chain | runs on the passed local model ALONE (D-277); a local error returns `fallback`, no cloud call |
| unset or empty | no (no pass, stale digest, settings mismatch, disabled, demoted, input too long) | returns `fallback`, no model call |
| unset or empty | yes, but the passed model is not in the chain (see below) | returns `fallback`, no model call |
| `1` / `true` / `on` / `yes` | any | runs (opt-in; also without a pass) on the full chain with fall-through; a passed lane goes first |
| `0` / `false` / `off` / `no` | any | returns `fallback`: global off |
| any other value (`disable`, `nope`, ...) | any | returns `fallback`: fails CLOSED, same as off |

Values are case-insensitive and whitespace-stripped (` 0 ` is off, `TRUE` is on). Only unset or empty means unset (LC-12b).

The unset-flag check is `eligibility_routing.passed_lane_available(job, input_len, now)`, which applies the same guards as `prefer_passed` below. The `llm_text_job` tool is
listed when the flag is on, or unset and some lane of an **Ollama** model is switched on in `local_routing.json` (a lane enabled by hand for a non-Ollama model does not list it).
The tool is registered when the MCP server starts: a pass or `enable` made afterwards needs an MCP restart or reconnect before `llm_text_job` is listed.

**Flag unset means local-only (LC-12b, D-277).** `prefer_passed` reorders; it never adds a model, and the router puts `ollama/*` models into a GENERATE chain only on a cheap tier
(not PREMIUM or REASONING; `_build_and_filter_chain`) and only models that `config.all_ollama_models()` reports and the P0.15 gate admits, so a passed model can be absent.
With the flag unset, `llm_text_job` calls `eligibility_routing.set_require_passed_first()` and the router then:

1. refuses at the eligibility step (`_drop_failed_eligibility` raises `PassedModelNotInChain`) unless a routable passed Ollama model leads the chain;
2. runs the TQ-007 daily-cap step, the P2.4 capability filter and the CLI-backend strip as usual;
3. as the LAST chain transform, cuts the final chain to the passed model alone. If any step removed it, the call refuses before any model call;
4. skips the BUDGET emergency fallback (a pass-only call never walks the BUDGET chain);
5. releases the budget reservation on every refusal (a refused call leaves `_pending_spend` as it was).

On a refusal, a local error, a timeout or exhausted retries, `llm_text_job` returns `fallback` ("Do this job yourself") and the host Claude session does the job. **No paid cloud API is called.**
Acceptance-rejection retries (`MAX_JOB_ATTEMPTS`, reason fed back) also use the local model only, and after the last rejection the result is "do this job yourself" (`rejected`).

With `LLM_ROUTER_LOCAL_TEXT_JOBS=1` none of this applies: the full router chain runs with fall-through (the passed model first, then the router's next entries, then the emergency chain), as before D-277.
The stream door (`route_and_stream`) never carries the requirement: `llm_text_job` calls `route_and_call` only.

**Ollama only (D-274).** `prefer_passed` considers `ollama/` models only. A PASS measured on LM Studio, vLLM or llama.cpp is recorded but switches nothing on; `local-check run` and `status`
print `measured; routing for this runtime is not supported yet (Ollama only in this release)`.

**Ollama cloud models are excluded (LC-12e).** A model whose tag is `cloud` or ends in `-cloud` (e.g. `gpt-oss:120b-cloud`, `<name>:cloud`) runs off-machine even though the local Ollama daemon lists it, so it is never routable, `local-check` refuses to measure it, `local-check enable` refuses it, and the local-only cut refuses it even if an older `local_routing.json` says passed. An untagged name means `:latest` (`x-cloud` is `x-cloud:latest`, local by name). **Limits.** Detection is by name at route time (no network call), plus Ollama's `remote_host` / `remote_model` metadata at `local-check` time: a cloud model renamed with `ollama cp` or a Modelfile `FROM` is refused when local-check runs, so it never gets a PASS. A model whose Ollama metadata hides that it is remote cannot be detected. A pre-registered (`text_job`) lane is matched by name, not digest, so a cloud model copied onto the name of a model that has one, then enabled by hand, is not caught either.

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

The acceptance check is the same under the unset flag and `=1`. They differ only in the chain: `=1` keeps the router's whole chain, so if the local model errors the router moves to the next entry; unset has the passed model alone (see above), so an error hands the job back.

**Age.** A pass older than 30 days whose Ollama digest still equals the stored one routes: design section 5 expires only runtimes that report no digest.

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
A call where the passed model errors (flag unset) records an `error` row for the model the router tried and returns `fallback`; a call refused before any model call records no attempt row. With `=1`, a call where every chain entry fails raises out of `llm_text_job` (its attempts are still recorded).
