# `llm-router local-check`

Measures **your own local model** on a bundled public benchmark of two text jobs (`commit_message`, `pr_description`) and records a verdict
for it. The result describes the model, runtime, settings and benchmark version on this machine; it is not a claim about any other setup.

```
llm-router local-check [run] [--dry-run] [--no-judge] [--yes] [--model M] [--endpoint URL [--allow-remote]]
                       [--profile quick|standard] [--fresh] [--force] [--judge-route auto|api|subscription]
llm-router local-check status [--model M]
llm-router local-check explain [--model M]
```

| Flag | Effect |
|---|---|
| `--dry-run` | Prints the plan screen and the consent wording. **Zero model calls**, nothing written. Reports the same refusals a real run would. |
| `--no-judge` | Programmatic grading only. Nothing is sent anywhere. Can record a FAIL (Wilson upper bound < 0.80), never a PASS. |
| `--yes` | Consent without a prompt. Required when stdin/stdout is not a terminal; without it the command stops before any call. |
| `--model`, `--endpoint` | Pick the model or runtime. Default: auto-discover Ollama, LM Studio, vLLM and llama.cpp on loopback. |
| `--allow-remote` | Only together with an explicit `--endpoint`: allows a non-loopback runtime (the benchmark prompts are then sent to it). |
| `--profile quick` | Only diffs up to 12 000 characters (narrower scope; the scope is stored with the result). |
| `--fresh` | Discards the open (resumable) run of this configuration and starts a new one. Counts as a strike (see below). |
| `--force` | Overrides the 24 h cooldown and the subscription-quota refusal. Never lifts "bank exhausted". |
| `--judge-route` | `auto` (default): your Anthropic API key if set, else the `claude` CLI. `api` / `subscription` force one. |

## What is sent, to whom, at what cost

* **To Anthropic (the judge):** the public benchmark task text (commit and pull-request diffs from open-source repositories, licences MIT, BSD,
  Apache-2.0, ISC, Unlicense) and **your local model's answers to them**. About 30 validity checks plus up to 70 answers, around 100 requests.
* **Not sent, ever:** your prompts, files, repositories, shell history or anything else from this machine. The benchmark does not read them.
* **To your local runtime:** the same public benchmark prompts. Nothing leaves your machine for that arm (unless you pass a remote `--endpoint --allow-remote`).
* **Judge route (D-260):** `ANTHROPIC_API_KEY` (or the configured Anthropic provider key) -> metered API route, pinned model, temperature 0, exact cost,
  default cap **$3 or 120 requests**. Otherwise the `claude` CLI on `PATH` -> your subscription (`claude -p`, no tools, hooks off); the plan screen
  warns above 80 % of your plan limit and the run refuses above 95 % unless `--force`. Otherwise only `--no-judge`.
* **`ANTHROPIC_BASE_URL` (D-264):** if it points at llm-router's own local proxy (the sentinel port, `LLM_ROUTER_PROXY_PORT`, or the default
  8787/8797), the judge goes **direct to api.anthropic.com** and does not pass through the proxy. Any other custom base URL refuses the API route
  (your key and prompts would go to another host); use `--judge-route subscription` or `--no-judge`.
* **Stored locally** (`~/.llm-router/local_check/runs/`, mode 0600): answers of your model to public items, judge verdicts, the attempt log. No user prompt text.

## Plan screen

Model and digest, hardware, items in scope (how many of the draw's 70 fit the model's context window), time, judge route, cost estimate and cap,
then the consent wording (`Proceed? [y/N]`). The time is measured by a 3-item rate probe at the start of the run; it is shown on the plan screen
only when a run is being resumed. **Below 50 items in scope the run is refused** (a verdict needs n >= 50 and >= 20 per lane): raise `num_ctx`
(Ollama: a Modelfile `PARAMETER num_ctx`, or `OLLAMA_CONTEXT_LENGTH`) or use a longer-context model.

## Verdict

PASS needs the point estimate >= 0.80 and the Wilson 95 % lower bound >= 0.60 (n = 70: k >= 56; n = 140: k >= 112). FAIL is a powered
failure (upper bound < 0.80). Anything between is INCONCLUSIVE and writes nothing. A lane whose own rate is below 0.70 is excluded from a pooled pass.
Answers the judge could not rate (after retries) are excluded from n, up to 5 % of judged calls (at least 1); beyond that the run is INVALID.
The screen shows `k/n` and **n first**, the lane table, the validity gates (G0 integrity, G1 serves, G3a grader self-test, G3b judge plants, G5 completeness),
a rule-concentration warning (one grader rule behind >= 40 % of failures), what was recorded, and the routing line. Routing activation itself
is a later update; until it ships the screen says so.

`explain` prints, for the last finished run, a histogram of failed grader rules with the **public item ids** behind each. It shows no item text and no answer.

### Retry rules

Per (model digest, benchmark version, settings): draw 1, then draw 2 only after 24 h (the verdict is then cumulative over 140 items); a third run is
refused until the model, settings or benchmark change. A run that stops after the local arm without a verdict (budget stop, Ctrl-C, an unexpected
error, an INVALID) leaves an **open run**: the next run of the same configuration resumes it (no re-running of local answers or judgements).

* `--fresh`, an INVALID, or deleting/emptying the run directory of an open run each count as a **strike**. Strikes 1 and 2 are free; the third and later burn the draw.
* An open run older than **7 days lapses without a strike**; the next run starts a new run id.
* Changing the model (new digest) or settings (`num_ctx`, thinking) is a different configuration: an open run of the old one is orphaned and is not resumed.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | PASS, a dry run, `status`, `explain` |
| 1 | Finished without a pass (FAIL, NOT_PASSED, INCONCLUSIVE, probe) or consent declined |
| 2 | Usage error |
| 3 | Bundled benchmark failed its integrity check |
| 4 | No runtime or model found, or a remote endpoint without `--allow-remote` |
| 5 | Subscription quota above 95 % |
| 6 | Refused: cooldown, bank exhausted, fewer than 50 items in scope, run directory mismatch |
| 7 | No usable judge route (or the judge cannot be used safely) |
| 8 | The runtime stopped answering a probe |
| 10 | Judge budget cap reached (resumable) |
| 11 | Another run holds the runtime lock |
| 12 | The eligibility store refused the record (the run stays reserved) |
| 13 | A validity gate failed: INVALID, nothing recorded |
| 130 | Ctrl-C. Progress is saved; run the same command again to resume |
