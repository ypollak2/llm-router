# `llm-router local-check`

Measures **your own local model** on a bundled public benchmark of two text jobs (`commit_message`, `pr_description`) and records a verdict
for it. The result describes the model, runtime, settings and benchmark version on this machine; it is not a claim about any other setup.

Bank v1 audit: all 140 items have grader and judge results for two models (a local model and a Sonnet reference), and the known defect rate is
1/140 (one grader defect, `q37-511a487c23de`, fixed in the grader; 0 item defects). The audit used no human labels: a Sonnet judge and a hand
audit by Claude. The bank manifest's `release_audit` block records it.

```
llm-router local-check [run] [--dry-run] [--no-judge] [--yes] [--model M] [--endpoint URL [--allow-remote]]
                       [--profile quick|standard] [--fresh] [--force] [--judge-route auto|api|subscription]
llm-router local-check --help  # run options and all subcommands
llm-router local-check status [--model M]
llm-router local-check explain [--model M]
llm-router local-check enable <lane> [--model M]
llm-router local-check disable <lane> [--model M]
```

`<lane>` is `commit_message` or `pr_description`. Model names carry their tag as the runtime lists them (`qwen3.5:latest`); without `--model`,
`enable` / `disable` use the only local model in the inventory and ask for `--model` when there are several.

| Flag | Effect |
|---|---|
| `--dry-run` | Prints the plan screen and the consent wording. **Zero model calls**, nothing written. Reports the same refusals a real run would. |
| `--no-judge` | Programmatic grading only. Nothing is sent anywhere. A probe: never a PASS, and it never writes the eligibility store (it cannot replace or downgrade a judged verdict); it appears in the attempt log and in `status` only. |
| `--yes` | Consent without a prompt. Required when stdin/stdout is not a terminal; without it the command stops before any call. |
| `--model`, `--endpoint` | Pick the model or runtime. Default: auto-discover Ollama, LM Studio, vLLM and llama.cpp on loopback. Ollama cloud models (tag `cloud` or `*-cloud`) are refused: they run off-machine, so local-check measures local models only. |
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
  warns above 80 % of your plan limit and the run refuses above 95 % unless `--force`; the quota is **re-read every 10 judge requests** and a run
  that crosses 95 % stops there (resumable, nothing recorded) unless `--force`. Otherwise only `--no-judge`.
* **Billing:** on the **subscription route the judge requests count against your Claude plan limit, not a dollar bill, but only when the
  `claude` CLI is logged in with your plan.** If that CLI is configured with an **API key** (for example `ANTHROPIC_API_KEY` in its environment or
  `claude` set up for the Console), those requests are billed to that key like any API call; the run then shows no dollar figure because the
  `claude` CLI does not report one. The metered API route (above) is the one with an exact cost and a hard cap.
* **`ANTHROPIC_BASE_URL` (D-264):** if it points at llm-router's own local proxy (the sentinel port, `LLM_ROUTER_PROXY_PORT`, or the default
  8787/8797), the judge goes **direct to api.anthropic.com** and does not pass through the proxy. Any other custom base URL refuses the API route
  (your key and prompts would go to another host); use `--judge-route subscription` or `--no-judge`.
* **Stored locally** (`~/.llm-router/local_check/runs/`, mode 0600): answers of your model to public items, judge verdicts, the attempt log. No user prompt text.

## Plan screen

Model and digest, hardware (RAM and the memory a model can use: total minus a reserve of max(4 GB, 20 %), not the momentary free memory),
items in scope (how many of the draw's 70 fit the model's context window), time, judge route, cost estimate and cap,
then the consent wording (`Proceed? [y/N]`). With `--yes` the question is replaced by `consent: given by --yes`.
When the name includes a parameter count, Time shows an estimate from its size class, labelled as not measured.
The real run measures its rate on items 2-3 (excluding the cold load) and prints the total; a resumed run shows its measured remaining time. **Below 50 items in scope the run is refused** (a verdict needs n >= 50 and >= 20 per lane): raise `num_ctx`
(Ollama: a Modelfile `PARAMETER num_ctx`, or `OLLAMA_CONTEXT_LENGTH`) or use a longer-context model.

## Verdict

PASS needs the point estimate >= 0.80 and the Wilson 95 % lower bound >= 0.60 (n = 70: k >= 56; n = 140: k >= 112). FAIL is a powered
failure (upper bound < 0.80). Anything between is INCONCLUSIVE and writes nothing. A lane whose own rate is below 0.70 is excluded from a pooled pass.
Answers the judge could not rate (after retries) are excluded from n, up to 5 % of judged calls (at least 1); beyond that the run is INVALID.
The screen shows `k/n` and **n first**, the lane table, the validity gates (G0 integrity, G1 serves, G3a grader self-test, G3b judge plants, G5 completeness),
a rule-concentration warning (one grader rule behind >= 40 % of failures), what was recorded, and one routing line per passed lane.

An early stop instead starts with `not passed (NOT_PASSED) -- stopped early after N of M items (K passed)` and the pass bar.

### After the verdict

* **Routing is live (D-260).** A recorded PASS switches the passed lanes on, per lane: `routing: on for <lane> -- disable with ...`,
  `routing: already on for <lane>`, or `routing: kept off for <lane> -- it was disabled; run ...` (a lane you or the demotion monitor disabled stays
  off after a new pass). Routing is Ollama-only in this release (D-274): a judged PASS on LM Studio, vLLM or llama.cpp is recorded, and
  `run` and `status` print `measured; routing for this runtime is not supported yet (Ollama only in this release)` instead of `routing: on`.
  A pre-registered lane is never switched on by a run; only an explicit `enable` does that. See
  [local-check-routing.md](local-check-routing.md) for what routing does and the demotion monitor.
* **No env flag needed (D-272).** With `LLM_ROUTER_LOCAL_TEXT_JOBS` unset, `llm_text_job` runs locally for a job exactly when a lane passes every
  routing guard (see local-check-routing.md). `LLM_ROUTER_LOCAL_TEXT_JOBS=1` also runs jobs with no pass; `=0` (or `false`/`off`) turns local text jobs off, passed lanes included.
* **Retention.** Any completed run (whatever the verdict) is followed by a prune: the last 3 runs are kept, answers older than 30 days are stripped.
  It prints only when it hit an error.
* **Recommendations.** After a FAIL, NOT_PASSED or an INCONCLUSIVE on fewer than 70 items, the screen lists models that fit this machine from a
  data file, comparable measurements first (`measured k/n on vN`). Measurements from another bank are shown as
  `untested on bank vN (no number shown)`, without their old counts; other untested entries keep their sizes-and-licence label, each with the pull command as text for you to run. Nothing is
  pulled or routed for you. With no entry for your runtime it says `no verified recommendation for <runtime>`.

`status` shows the recorded lanes (the same lines `doctor` shows, including `[routing: on]`), the routing switches in `local_routing.json`
with the command that flips each, any lane the demotion monitor switched off, and the attempt log.
The log shows `N of 2 judged draws used`; scored programmatic-only runs add
`; M programmatic-only run(s) (--no-judge) not counted; latest probe result: V k/n (probe, not a judged verdict)`.

A probe (`--no-judge`) writes no verdict, so `doctor` shows nothing for it; only `local-check status` shows it, labelled as a probe. The `last V k/n <date>` part of that line always comes from the latest judged run (`no judged run yet` when there is none).

`enable <lane>` switches routing on for a lane by hand (source `manual`); it still applies only while the lane is passed and fresh.
`disable <lane>` switches it off; a later PASS keeps it off.

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
