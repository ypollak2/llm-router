# Local-check routing (LC-8)

The command that measures a model and switches lanes on or off is described in [local-check.md](local-check.md).

Owner decisions: D-260 (a passed lane routes local-first by default, on the verified text-job path only), D-262 (effective lane: the user's own fresh local-check result, else pre-registered, else unknown), D-258 (the proxy authority stays off; pre-registered lanes are record-only).

## What routes, and when

Only `llm_text_job` (`commit_message`, `pr_description`, `summarize_output`). Plain `llm_*` calls, `llm_edit` and the proxy never use this.
`eligibility_routing.prefer_passed(models, job, input_len, now)` runs at the router's eligibility step, after D-116 and the F106/F112 step, and moves a local `ollama/` model that is **already in the chain** to the front. It never adds a model. All of these must hold, otherwise the chain is returned unchanged:

| Condition | Source |
|---|---|
| routing enabled for (model, job) | `local_routing.json` |
| effective lane is `passed` | `local_eligibility.json` (`text_job_local` first, then `text_job`) |
| lane fresh | stored `model_digest` equals the digest in the inventory cache (`discovery.json`, no HTTP); with no digest to compare, 30 days from the lane date |
| input fits | `len(input) + len(hint) <= lane.scope.max_prompt_chars` |
| settings parity | route-time `think` (off) and `num_ctx` (the router's Ollama window) hash, with the local-check temperature and max tokens, to the lane's `settings_hash`. Unknown ctx means no preference |

A pre-registered lane has no scope or settings hash. It is preferred only after an explicit `enable`, only with thinking off, and only when the input fits the window the router will request.
Note: the router's Ollama window comes from `LLM_ROUTER_OLLAMA_NUM_CTX` or the shared local window table. If it differs from the window local-check measured with, the settings hash differs and the model is not preferred (conservative by design).

The acceptance check and the Claude fallback in `llm_text_job` are unchanged: a rejected local answer still ends in "do this job yourself".

## The switch: `local_routing.json`

```
{"<model>": {"<job>": {"enabled": true, "since": "2026-10-20T10:00:00Z", "source": "local_check"}}}
```

A disabled entry also holds `reason` and `disabled_at`. Python API (`llm_router.local_check.routing`):

- `enable(model, job, source="manual")` writes an enabled entry (source `local_check`, `prereg` or `manual`).
- `disable(model, job, reason="manual")` writes `enabled: false`.
- `is_enabled(model, job)`.
- `on_pass(model, jobs)` is called after a recorded local-check PASS: enables each lane that has no entry, returns `{job: "enabled" | "already_on" | "kept_off"}`. A lane disabled earlier (by the user or by the monitor) stays off.

`llm-router local-check enable|disable <lane> [--model M]` call `enable` (source `manual`) / `disable`; a passing run calls `on_pass` and prints one line per lane. The CLI lives in `commands/local_check.py`.
Model names are compared with `:latest` normalised.

## Demotion monitor

After each `llm_text_job`, `demotion_check(model, job)` reads the tail of `text_job_outcomes.jsonl`. If more than 30% of the last 20 calls for that (model, job) since it was enabled were `rejected` (7 or more of 20; the window must be full), routing is disabled with `reason: "demoted: fallbacks"` and `llm-router doctor` prints a line. It never re-enables; only `local-check enable <lane>` does.
