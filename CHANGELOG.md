# Changelog

All notable changes to `llm-router` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

| Where | What |
|---|---|
| This file | The current major line — v11.0.0 onward |
| [CHANGELOG-ARCHIVE.md](CHANGELOG-ARCHIVE.md) | v10.1.5 back to v6.3.0 |
| [GitHub Releases](https://github.com/ypollak2/llm-router/releases) | v6.2 and earlier |

## [Unreleased]

## [16.0.0] - 2026-11-15

### Added
- S1 daily row: a separate `coordinated` population (sessions the owner reclassified as non-organic coordinator sessions in `session_kind_overrides.json`, D-407) with its own n, floors and CI, labelled "COORDINATED sessions, not organic" in `kpi --daily`; organic, release_clock and the streak are unchanged.
- P3.6-c PR3d (review of PR #708): a tune scoring is canonical only after the owner labelling pass is recorded
  complete (`redo-label --owner` writes counts-only `owner_done.json`: `n_labelled` and the sha256 of
  `labels_owner.jsonl`); earlier scorings (e.g. judge-only) print F1, log `owner_done: false` and lock nothing, so
  adding owner labels no longer forces a void. `redo-sample --freeze` recomputes the digest of the canonical label
  files and refuses (exit 2, "labels on disk changed since canonical scoring; re-score or void") if they differ.
  No judge-only escape flag: design section 3 gives the owner the final say. v3c "same error" is skipped for
  prompts longer than 2200 chars (it could not end within six words) instead of rescanning them (was
  near-quadratic: 84k chars took 1.4 s); v3d benign list adds important/needed/necessary/urgent. Procedure-only:
  the label-file set is chosen at the first canonical scoring and `--labels` subsets are possible; incomplete
  scorings print F1 so results can be peeked at (narrow); a candidate first scored after a transcript expires
  has n_lost > 0 and freeze refuses (fails closed): score all candidates promptly after labelling.
- P3.6-c PR3c (recall-raising redo variants): `redo_signal` variants `v3c` (extra phrases that reject the
  previous answer: "that's not what I asked", "still wrong", "still not fixed", "still getting the same error" or
  a bare "same error", "no, I meant" (not "I meant to"), "undo that", "revert your change", "go back to the
  previous version" ...; reported as `contradiction`) and `v3d` (`v3c` plus a short rejection of at most 6 words:
  wrong/not it/not that/not right/not quite/still wrong/broken/failing/the same, minus "not now", ...; bare
  "still", "not", "no" and "nope" are excluded: a bare "no" after "Want me to push it?" declines an offer, and v2
  already flags "no," / "no." / "nope"). The v3c/v3d-only patterns ignore text in ``` / ~~~ fences and on `>`
  quote lines. "same error" needs "still getting / I get the same error" ending the prompt within 6 words without
  a "when/if/while/on/in/is that/known" continuation; "that broke" needs "it" / "the|your fix|change|code" or a
  prompt of at most 8 words, as does "that made it worse"; "undo that" / "revert it" must end the prompt or a
  clause, or name the change ("undo your edit"); "go back to the previous version" must end there and not be
  followed by "and summarize|explain|list|write|add ..."; "i meant: also ..." is a refinement. Product reports
  ("same error appears when the server restarts", "I get the same error when the server restarts on prod; is
  that a known issue?"), new questions ("I meant to ask ..."), git instructions ("undo that last commit on the
  release branch"), "revert your branch", "go back to the plan", "still not merged" and "still, can you also add
  X" are must-not-flag fixtures. Both keep every v2 signal, so their flags are
  a superset of v2's (tests/test_redo_variants_recall.py, including reask/repeat combined with a new phrase).
  Written from the label definition only, no data. Under the at-most-2-widenings rule v3a, v3b, v3c and v3d are
  four candidates but only two non-baseline (candidate, detector sha256) pairs may ever be tune-scored, so the
  owner picks which two. Once both are used, any edit to `redo_signal.py` is a permanent dead end: no variant can
  be tune-scored or frozen again, only `v2`. `PATTERN_VERSION` and the default stay `v2`.
- P3.6-c PR3b (anti-gaming): `redo-sample --freeze --detector X` refuses a non-baseline X unless X is the strict F1
  argmax of the study's canonical tune set. One study has one live tune run: a second `redo-sample --tune` refuses
  until the first is voided with `redo-sample --void-tune --run ID --reason TEXT` (recorded in every later
  `frozen.json`; a voided run cannot be scored and its widenings stay spent). The run's first complete
  `redo-score --tune` row (n_lost 0) fixes the canonical set: tune manifest sha256, labels digest (sha256 over the
  sorted sha256 of each labels file read), scored-pair digest (sha256 over the sorted pair ids scored),
  projects-dir digest and unclear count. A later tune score on that run with other label files refuses. Only
  complete rows with that identity are compared; the set must hold a `v2` row and a row for every other
  candidate with tune rows at the current detector sha256; competitors are ranked by their maximum F1 there, X by
  its minimum (X's own older-sha rows are not competitors); a tie fails closed. What this prevents with ordinary
  CLI use: crowning a loser by re-scoring on another labels file (refused at score time), by scoring the
  candidate from a projects dir missing transcripts (incomplete, refused at freeze), or by drawing a fresh tune
  run and scoring only v2 and the candidate there (refused while the first run is live; after a recorded void the
  other candidates must be re-scored on the new run). `redo-sample --tune` also refuses a non-default
  `--projects-dir` unless `--allow-nondefault-projects-dir` is passed (recorded with the dir's real-path digest
  and transcript count): the tune pool must come from the census population, and a curated directory is a
  procedure violation. What stays procedure-only (the tool cannot detect it): a rigged labels file scored as the
  run's FIRST complete scoring, edited transcripts, a curated pool drawn with the override flag, and a void
  combined with a `redo_signal.py` edit that leaves a competitor scored only at an older sha on the voided run.
  Each refusal cause has its own counts-only message. `frozen.json` records `tune_row_sha256`,
  `tune_log_sha256`, `tune_log_bytes`, `tune_run`, `tune_manifest_sha256`, `tune_labels_sha256`,
  `tune_scored_pairs_sha256` and `tune_projects_dir_sha256`; `redo-score` re-runs the gate on the bound log prefix
  against the live tune run, refuses a tune-set mismatch or a voided bound run, and refuses a baseline freeze that
  carries tune provenance; `result.json` records `detector_variant`. `redo-sample` prints the run id, never the
  run directory path.
  Tune-log rows store `detector_sha256`, `tune_manifest_sha256`, `labels_sha256` and `variant_params`; the widening
  cap counts distinct (candidate, sha256). `tune_log.jsonl` is append-only by procedure: deleting or rewriting it
  AFTER a freeze is refused at score time, but an edit BEFORE freeze cannot be detected by the tool and is a
  procedure violation only.
- proxy Haiku tier on Claude Haiku 5.5 (HAIKU55-TIER-1, docs/bugs/HAIKU55-TIER-1.md): the Haiku rewrite rules now key
  off the tier's `model`. With `claude-haiku-5-5` the body keeps adaptive thinking, effort and mid-conversation system
  messages (no fold), `max_tokens` clamps to 128K, a body with sampling parameters or an assistant prefill (400 on 5.5)
  or too large for the window (estimate x 1.3 tokenizer factor + max_tokens > 1M, 670,769 estimated tokens at 128K
  max_tokens; owner decision 2026-10-10; calls over 100K prompt tokens bill at $0.50/$2.50 per MTok, up to 100K
  $0.10/$0.50) goes up a tier
  (`haiku_body_blocked`). Any other id keeps the 4.5 rules; the shipped default is still `claude-haiku-4-5`
  (4.5 retires not sooner than 2026-10-15). Capabilities are from platform.claude.com, fetched 2026-10-10.
- AGT A.2-s: the gateway serves `stream: true` on `/v1/chat/completions`, `/v1/responses` and `/v1/messages` as server-sent events in each endpoint's own protocol, and the "streaming not supported yet (v16 A.2)" 400 is removed. Chat sends `chat.completion.chunk` objects (role, content and tool-call deltas, a `finish_reason` chunk, a usage chunk when `stream_options.include_usage` is set, then `data: [DONE]`); Responses sends `response.created`, `response.output_item.added`, `response.output_text.delta`, `response.function_call_arguments.delta` and the matching done events through `response.completed`; Messages sends `message_start`, `content_block_start`, `content_block_delta` (`text_delta` or `input_json_delta`), `content_block_stop`, `message_delta` with `stop_reason`, and `message_stop`. The stream is the routed call's complete answer re-framed, for every provider, not provider token streaming: it goes through the same `route_and_call` as a non-streamed request, so the routing decision, the gates, the cache bypass for tools and the single usage row (same fields as a non-streamed call) are identical by construction, and `route_and_stream` (which ignores history and tools) is not used. Headers go out at once and SSE comment lines keep the connection open while the model works; a failure after that is an `error` event in the endpoint's own shape (chat sends no `[DONE]` after it). When the client disconnects the routed call is cancelled and the router leaves its one `error_cancelled` row with `success=0`. Requests the gateway refuses (hosted tools, images) are still an HTTP 400 before any event. Evidence: `tests/test_agt_a2_gateway_clients.py` (A.2-s section; official openai and anthropic clients against a real uvicorn, text and tool-call streams, ledger rows, disconnect, mid-stream errors).
- AGT A.6 (examples): `examples/` holds one runnable example per framework and per door: `python_sdk` (SDK), `mcp` (MCP over stdio), `proxy` (the per-call proxy), `langgraph`, `openai_agents`, `crewai`, `pydantic_ai`, `claude_agent_sdk` and `typescript_openai` (gateway). Each makes one call through llm-router, asserts the answer and, where that door writes one, the ledger row; LangGraph, the OpenAI Agents SDK, Pydantic AI and the TypeScript client stream and make one tool call. They run on `examples/fake_backend.py`, a deterministic offline fake of the model edge (no key is set, no provider is called, any other outbound connection is refused and fails the run). Framework packages are pinned in each example's own `requirements.txt` and installed only by the new `.github/workflows/examples.yml`, one matrix job per example; they are not llm-router dependencies. `docs/integration.md` is the one-page guide: the `base_url` one-liner, a section per framework and door with its example, and the SSE behaviour (the complete answer re-framed; a routing failure on a stream is HTTP 200 plus an `error` event). Evidence: `tests/test_a6_examples_wired.py` (every example has a `main.py`, a workflow row and a docs link; requirements are exact pins; no framework is a llm-router dependency).
- AGT A.6 (adapters): the five framework adapters in `llm_router.frameworks` are real, thin `base_url` wrappers and none raises `NotImplementedError` any more. `LangGraphAdapter.wrap_model` returns `langchain_openai.ChatOpenAI`, `CrewAIAdapter` a `crewai.LLM`, `OpenAIAgentsAdapter` an `OpenAIChatCompletionsModel` over an `AsyncOpenAI`, `PydanticAiAdapter` an `OpenAIChatModel` over an `OpenAIProvider`, and `ClaudeAgentSdkAdapter` a `ClaudeAgentOptions` whose Claude Code process has `ANTHROPIC_BASE_URL` set: each points at the llm-router gateway (`LLM_ROUTER_GATEWAY_URL` or the active preset; the gateway token when one is configured), so the framework's calls are routed, gated and ledgered by the gateway, streamed or not, with or without tools. The framework packages are the user's to install and are not llm-router dependencies; a missing one is an `ImportError` that names the package to install. The Hermes stub (and `get_hermes_adapter`) is removed: "Hermes" names several tool-call formats rather than one framework, so there is nothing to wrap, and a Hermes-format model is served by any OpenAI-compatible client against the gateway. Evidence: `tests/test_a6_framework_adapters.py` (what each adapter builds against stand-in framework modules, plus a scan that no adapter raises `NotImplementedError`).
- SDK `Router(session=None, budget_usd=None, project=None, policy=None)` with `.run()` and `async .arun()` (worker thread, never blocks the loop); `route()` is now a shim over it. `RouteResult` gains `decision`, `session_id`, `task_id` and `cost_usd`. A `budget_usd` cap raises `BudgetExceeded` (exported from `llm_router`) before a call once the consumed list-price cost reaches it. New engine door `sdk`: under `LLM_ROUTER_ENGINE_MODE=decide` the SDK routes on the same `engine.decide` Decision the MCP `llm_route` uses; in shadow mode the gateway rules route and the engine shadows with door `sdk`. MCP `llm_route` now classifies the caller's raw prompt (the model still receives the boundary-wrapped prompt). `scripts/door_agreement.py` gains an `sdk` site and `--sample N --seed S`.
- AGT A.2-t: the gateway serves tools on `/v1/chat/completions`, `/v1/responses` and `/v1/messages` (non-streaming) instead of refusing them with HTTP 400. Each endpoint normalises its tools array and `tool_choice` to the OpenAI chat form, routes to API models only (the codex and gemini_cli CLI backends return text and are excluded; models known to lack tools are excluded by the capability filter, which now gets the request's real tools array as `Needs(tools=True)`, the signal D-165 asked for), and renders the model's tool calls in the caller's own format: chat `tool_calls` with `finish_reason: "tool_calls"`, Responses `function_call` items, Anthropic `tool_use` blocks (`input` parsed to an object) with `stop_reason: "tool_use"`. `parallel_tool_calls`, `stream_options`, `store` and `include` are accepted and not acted on. A tools request bypasses the semantic cache (neither read nor written). A reply that is only tool calls is a complete answer: it is exempt from the empty-content check, the contract gates and the quality escalation. Tool-call history in a follow-up turn is still rendered to text by the context pack, not sent as native tool messages. Unchanged: hosted tools (web_search, file_search, Anthropic server tools) and `tool_choice` without `tools` are refused with 400; `/api/chat` and `/api/generate` still refuse `tools`; `stream: true` with tools is still the streaming 400 until A.2-s. New: `LLMResponse.tool_calls`, `route_and_call(tools=, tool_choice=)`, `finish_reason` and `tool_calls` in the `route_payload` result. `openai` and `anthropic` are added to the dev extra (test clients). Evidence: `tests/test_agt_a2_gateway_clients.py` (official openai and anthropic clients against a real uvicorn, the fake at litellm.acompletion).
- CLS-v16 B0 (draft, DEV-gated, runs nothing until PREREG-CLS-DEV is frozen): `scripts/cls_dev_run.py` and `scripts/cls_dev_lib.py`, the DEV candidate runner. Subcommands: manifest (T0/cutoff drops, exact and 5-shingle dedup, secret exclusion by detector, group recovery, length-band counts for the generator, fold ids, registered as a PINS-RUN entry), scrub-check, folds, fit-heads (C1 bge-small, C2 nomic-embed, ordinal heads with the frozen fitting choices, 5-fold OOF plus refit), c4-precheck/c4-run (qwen3.5 first-token logprobs of the v6 prompt, no argmax fallback), trigger-mb, fit-modernbert and download-modernbert (gated, never runs by default), reference-arms, latency, stability and assemble (the B1 arms table, `cls_eval` fit/score as a subprocess, kill criteria 0-8, at most two finalists with the diversity rule and the arm A/B choice). Quiet-machine gate, 00:00-07:00 local exclusion with a measured-duration stop rule, own-model unload on every stop. Counts, ids and hashes only; no model call, network or download in the tests.
  Repair round (D-160): criterion 1 labels a candidate with 1500 < p95 <= 15000 ms `async_only` (killed above 15000 ms or above 12 GiB) and records which condition fired; criterion 3 decides on the AUROC with error items excluded (cls_eval scores a filtered table; both AUROCs reported); criterion 8 passes "not evaluable" only for a voided judge; timed blocks sample load, foreign models, `claude -p`, AC power and thermal every 60 s, and every candidate checks foreign-resident Ollama models; a C4 precheck whose tier token is not located parks for the coordinator instead of killing; the arm-B threshold is labelled an in-sample screen.
- CLS-v16 B1: `scripts/cls_eval.py`, the counts-only scoring harness for learned tier classifiers (score, fit, selftest). It implements PREREG-CLS-DEV sections 3, 4, 7 and 8: truth rules (majority3, per-source, sensitivity, other_two_higher fallback), paired group-bootstrap bars, non-inferiority bounds and the NOT-TRIVIAL guard over the shadow_eval cost functions, session-grouped 5-fold threshold fitting, refusal handling, single-use log. TEST-gated: scores nothing until PREREG-CLS-TEST is frozen.
  Non-inferiority bound is the 97.5th percentile of the paired bootstrap, the guard the one-sided 95th (D-148); `--mode test` requires `--single-use --use-log`; criterion 8 helper covers sonnet_only and the DEV-SYN leg.
- CLS-v16 B-L (draft, DEV-gated, runs nothing until PREREG-CLS-DEV is frozen): `scripts/cls_send_lib.py` (secret detector a-e with per-shape self-test, PII replacement, independent scan, `outgoing_check` on the exact bytes with the denylist and `.env` detector configured, the single child-spawning `send` that records a timeout as a failed call, claude/codex child hygiene with every codex call pinned by `-m`, a plain-mode Codex canary that checks the `model:` header and scans the whole output, OpenRouter guard behind explicit no-retention preferences, a spend cap with pending rows written before each priced call, TEST-SYN custody guards), `scripts/cls_label_run.py` (blinded three-judge relabel runner on the P1.7-d judge contract: pinned `gt500.py` and `rubric.py`, `prompt_view` form, `{"labels": [...]}` answers checked by `valid_label`; first-call and interleaved plants; a Codex failure or a missing model id parks the run instead of voiding it), `scripts/cls_gen_run.py` (synthetic generator runner, spec versions, TEST-SYN custody, resume of an interrupted set, a regeneration that verifies its pins before discarding), `scripts/cls_resemble.py` (numbers-only resemblance gate) and `scripts/cls_pins.py` (PINS-STATIC with the `gt500.py` pin, help flags matched on their definition lines, the PINS file checked against the freeze record, and the append-only PINS-RUN chain). The tests make no model call and use no network or download (fake claude/codex binaries, fake Ollama, injected scorers).
- CLS-DEV: `docs/prereg/PREREG-CLS-DEV-AMEND1.md` (D-280) lets E1 items with no recoverable record timestamp take the E1 source file's mtime as an upper-bound timestamp, and discloses the m1 attach deviations (id label join, `truth` tier field, `--allow-missing-timestamps`); `scripts/cls_attach.py attach --timestamp-upper-bound-from-mtime e1_tune|e1_ho` implements it (refused with `--allow-missing-timestamps` for the same set, mtime rounded up to the second, counts and source sha256 recorded). Frozen scripts and the frozen prereg are unchanged. Evidence: `test_mtime_*` in `tests/test_cls_attach.py`.
- CLS-DEV AMEND2 (D-283, D-284, docs only): the Codex read-scope canary parked the DEV-SYN v1 generation run (exit 11, 0 calls sent, $0) because `-s read-only` allows full-disk read and the model reads through several default-on tools (shell, `view_image`, image generation, `spawn_agent` `local_image`). The amendment runs every CLS `codex exec` with `CODEX_HOME=~/.codex-cls`, whose `config.toml` (321 bytes, sha256 `203b7e93…feb87`) turns those tools and web search off, links `auth.json` to the main login, and moves the session-log listing and purge to `$CODEX_HOME/sessions`. No script, argv or pin changes. Evidence: `docs/prereg/PREREG-CLS-DEV-AMEND2.md` and its `.sha256`, verified by `scripts/check_prereg.py`.
- Discovery now rescans local models, provider-key names and subscription seats on first use, on a bounded timer (default hourly, `LLM_ROUTER_RESCAN_INTERVAL_S`, floor 5 min) and on change (Ollama `/api/tags` name+digest fingerprint, key-name presence, seat state), from the session-start hook (detached child) and as a loop in the MCP server and proxy. `ollama/@local` is now resolved at call time (`ROUTING_TABLE` and the active policy re-resolve when the local model changes), so a running server needs no restart after `ollama pull`/`rm`. New `llm-router discover [--rescan] [--json]` prints what changed (names and counts only, never key values). (D-205)
- F124: `llm-router eligibility record --prereg DIR [--apply]` records a pre-registered text-job qualification (`commit_message`, `pr_description`, `summarize_output`) in `local_eligibility.json` under `{model}.text_job.{job}` with provenance (prereg id, MANIFEST sha256, pooled k/n, the D-241 bar, grade `passed`). Dry run by default; refuses when the manifest hash differs from `FROZEN.txt`/`run/USED.json`, the verdict is not PASS, or the actions name no valid job. `eligibility_store.record_competence` accepts task type `text_job` (job sub-keys) and `eligibility.grade_text_job` is the D-241 grade. `llm-router status` and `doctor` list qualified text jobs. No routing change: no router reader looks up `text_job`, and its grade is not `eligible`. Evidence: `tests/test_f124_text_job_eligibility.py`.
- synthetic G1 evidence harness (PLAN v16 G1, owner decision D-99): `scripts/g1_synthetic_evidence.py` generates 60 deterministic synthetic coding sessions (seed 20261014) with one planted answer token each and runs the P1.1-b fidelity pairing (`fidelity --stratum sonnet`: PACK vs FULL through the `claude` CLI; `--stratum local --local-model <m>`: PACK vs HOOK6 through Ollama) and P1.2-a summary coverage plus spawn p95 (`summary`), in a scratch HOME / LLM_ROUTER_HOME. Pre-registered and hash-frozen in `docs/prereg/PREREG-P11B-synthetic.md`; every output is labelled `synthetic (D-99)` and written to `docs/measurements/`.
- Lane A PR1 (D-285, D-292, D-293): the proxy can serve the Haiku tier for classifier-confident continuation steps (a step that answers a tool result) despite `pinned_models`, for the `technical_op` allowlist only. Default OFF: serving needs `lane_a.enabled: true` in the tier policy YAML, `LLM_ROUTER_PROXY_LANE_A=on` and `haiku_rewrite: true`; turn-first, sub-agent and harness steps stay pinned. Every proxy continuation ledger row now carries `lane_a_reason` (a closed set of reason codes, null if unknown) and `lane_a_conf` (the engine confidence, or null) even with the lane off, so real traffic can measure the eligible share without serving anything. No offload or saving figure is claimed. Evidence: `tests/test_proxy_lane_a.py` (byte-identical upstream requests with the lane off, a reason code per branch, an AST ratchet that the one ledger writer stamps both fields). Not yet: breaker, shadow sampler, quality writer. Built directly by Claude (Codex at its usage limit, D-271). Repair round 1: the lane gates are side-effect free (rules classifier plus `engine.compute(cache=False)` in a worker thread: no shadow row, decision-ledger row, decide cache, classifier_health write or embed with the lane off), the shipped tau is tested against known score-4 and score-5 prompts, and lane-off byte identity is tested for non-streaming, 4xx, 5xx and tool-use continuations. Repair written directly by Claude (Codex at its limit).
- Lane A PR2 (plan W1-4; D-285, D-292, D-293, D-297): a breaker and failure feed for the lane A Haiku serve, a fallback, two rules deferred from PR1 and the `complexity_knn` veto. Still default OFF: nothing serves unless the YAML `lane_a.enabled`, `LLM_ROUTER_PROXY_LANE_A=on` and `haiku_rewrite` are all on. Breaker (`proxy/lane_a_breaker.py`): opens on 5 hard Haiku failures in 10 minutes or a redo rate above 15% over at least 30 served steps in 7 days, half-opens after a cooldown and admits one probe, state persisted 0600 through `state_files` and flushed off the event loop; open steps stay pinned (`breaker_open_reserved`). Failure feed: 4xx/5xx/429, timeouts, empty or truncated replies and unparseable tool calls, plus the redo feed from `local_gate.FailureFeed`. A lane A 5xx or timeout now retries once on the pinned model (`retried_original`); 4xx/429 already did. `haiku_units` includes lane A continuations. New rules, both configurable under `lane_a:` with conservative defaults chosen here, not measured: the cache-switch rule (`switch_cost_high`, `max_switch_usd` 0.05) and the consecutive cap (`consecutive_cap`, `max_consecutive` 6). D-297: with `complexity_knn: true` a "needs frontier" vote keeps the step pinned (`knn_veto`), asked only on the serve branch so a lane that is off makes no embed call. Evidence: `tests/test_proxy_lane_a_breaker.py` (state machine, restart, fallback per status, side-effect comparison, byte identity against a golden frozen from main 224584a3). No offload or saving figure is claimed. Written directly by Claude (Codex at its usage limit).
- Lane A PR3 (plan W1-4; D-285, D-292, D-293; default OFF, lane-off output byte-identical to main): the Frontier shadow for lane A serves (deterministic key, 100% until 60 decided pairs per task group then 2%, a lane A bucket of 12 calls / 400k input tokens a day INSIDE the unchanged global shadow cap, background after the reply), a `lane: "A"` ledger tag, the first writer of `lane_quality.jsonl` `shadow_pairs` rows (`python -m llm_router.lane_quality_writer`, also run after `shadow_frontier judge`; n >= 60 decided pairs, gap and unpaired Newcombe bound from `eligibility.evidence_from_counts`), and a `lane_a` / `not_lane_a` split of the offload KPI that adds up to `action` (the S1 rule is unchanged). Carry-ins from the #731 review: `lane_a.header_timeout_s` (default 60 s, a placeholder), one failure direction for the breaker (an unreadable state file or an internal error does not serve), 401/413 from Haiku are not breaker failures, a stale probe id is ignored, no second body serialization on the loop, the `s_A` and `retried_original` docs fixed, dead code removed, and a lane A trip shows in `quality_breaker.open_classes()` and the Stop-line (read-only). Evidence: `tests/test_lane_a_pr3.py`. Built directly by Claude (Codex at its usage limit). Repair round 1 (review of #732): judge failures of lane A pairs are `cannot_judge` verdict rows (counted in the 20% share); one pair per `(session_id, msg_count)` in the writer, the sampler (`step_seen`) and the phase count; lane A verdicts no longer feed the arm's D-20 shadow trigger, the statusline or the session report, and lane A has its own trigger (`haiku_guard.lane_a_shadow_trigger`, lever `proxy_haiku` task `lane_a`) read by the lane A breaker; a body-sha pairing check (`lane_a_body_sha` on served rows, `Replay.sent_sha` on the replay); D-301: S1 counts only the FINAL `shadow_pairs` row (`window_complete: true`), written at the first batch of 60 decided pairs or 28 days after the owner flip (`lane_a_flip.json`), NOT INFORMATIVE final rows earn no credit, and a closed window never reopens; `excluded` counts are in the written row; unused `lane_a_breaker.read_state` removed. Repair written directly by Claude (Codex not attempted). Repair round 2 (D-302 + review): a batch with n >= 60 and over 20% cannot_judge is a non-final diagnostic row and the window stays open (at 28 days the window closes NOT INFORMATIVE when n < 60 or the last batch is over 20%); the owner flip is the first proxy request that sees `lane_a.enabled`, `LLM_ROUTER_PROXY_LANE_A=on`, `haiku_rewrite` and the shadow flag all on (recorded with those four values, in a worker thread; never written with any one off); the sampler stops for a task group once its window is final; final rows are exempt from the S1 28 day cap, stamped with the lane A model, judge brief sha, prompt set and breaker thresholds, and invalidated (reported as `invalidated`, never auto-reopened) only when one of those changes; S1 prints each final row's age; the pair window is clamped to flip + 28 days; a replay that reports no sha voids the pair; pairs from before the flip are counted in `excluded`.
- LC-0: user-run local-check results get provenance and live in their own sub-record of `local_eligibility.json`, `{model}.text_job_local.{job}`, beside the pre-registered `{model}.text_job.{job}` lanes (F124), which `record_local_check` never modifies or downgrades; every existing reader of `text_job` is unchanged. A `text_job_local` lane (`source: "local_check"`) must carry `bank_version`, `bank_sha256`, `settings_hash` and counts `k`/`n` whose D-241 grade matches, and may carry `model_digest`, `runtime`, `judge`, `run_id`, `date`, `scope`, `attempts`, `rule_concentration`; unknown extra fields are ignored. New `resolver.eligibility_store.record_local_check(model, jobs_result, provenance)` writes them atomically (0600, existing lock) and returns a per-job result `{job: {"status": "written"|"skipped_inconclusive"|"skipped_invalid"|"refused", "reason"}}`; INCONCLUSIVE and INVALID jobs are never written, a verdict whose counts grade differently is refused for that job, and a new result replaces only the previous local-check lane for that model and job (latest wins, `attempts` = previous + 1). `lane_is_fresh(lane, current_digest, now)` treats a digest mismatch as stale and a lane without a checkable digest as valid for 30 days. `effective_text_job_lane(model, job, current_digest, now)` returns `(lane, source)`: the user's fresh local-check lane (D-259), else the prereg lane, else `(None, "unknown")`. `text_job_lines` keeps the prereg line unchanged and adds a local-check line (`<model> — jobs passed k/n (bank vN, date)`, or `stale — model changed` / `stale — expired` when the caller passes the current digest). No CLI and no routing use yet. Evidence: `tests/test_lc0_eligibility_provenance.py`.
- `llm-router local-check` item bank (LC-1, data and loader only; no command yet): `llm_router.local_check.bank` loads `data/bank-v1.jsonl.gz` and `data/manifest.json`, re-hashes the file against the manifest sha256 on every load and refuses on any mismatch, and exposes 140 items (70 commit_message, 70 pr_description; strata size x language), two disjoint 70-item draws seeded by the bank sha and stratified by job x size x language (per-stratum counts differ by at most 1 between the draws; draw 1 holds audited items only, draw 2 the rest), and 30 judge-validity plants (12 good, 12 swapped, 6 mutated) from a pool disjoint from every item. Each item carries `audited`: 118 of the 140 items (62 commit_message, 56 pr_description) were answered by both arms (qwen3.6 and Sonnet) in the Q37 run or calibration slices with grader and judge results behind them; the other 22 (8 commit_message, 14 pr_description) come from the unanswered poolc pool and have never been run, so a verdict that includes them is not fully audited ("audited" means verdicts exist behind the item, not that it is free of grader defects) (the manifest carries `items_audited` / `items_unaudited` per job). Plants judged contrary to their label in Q37 calibration are excluded (one good plant, `q37-98983aade6f1`), the 2 plants rebuilt here (4 more are byte-identical Q37 calibration plants) get new content-derived ids (`lc1p-...`) and a `provenance` field. The text is public diffs and commit/PR messages from MIT, BSD, Apache-2.0, ISC or Unlicense projects (17 repos incl. mypy, MIT); each record names repo, commit sha, SPDX licence and URL, and `data/NOTICE` carries the attribution. Pylint (GPL-2.0) is excluded, and diffs that touch vendored third-party paths (`_vendor/`, `vendor/`, `third_party/`, `node_modules/`) are dropped, as are diffs touching a path a repo's LICENSE puts under another licence (celery `docs/`, CC BY-SA 4.0, read from the LICENSE text; 4 items replaced from poolc). `scripts/build_local_check_bank.py` rebuilds the bank reproducibly (needs the Q37 clones incl. express, mypy, pylint) and reads each licence from the LICENSE file at the record's sha. Evidence: `tests/test_local_check_bank.py` (tamper refused, schema, licence filter, mypy-like licence text, vendored filter, calibration guard, stratified draws, plant ids, privacy grep, 2 MB cap, wheel contents).
- LC-2 (local-check grader): new package `llm_router.local_check` with `rubric` (single source of the commit_message / pr_description limits, and the prompts rendered from them), `grader` (programmatic grading, ported from the frozen Q37 grader with identical behaviour), `stats` (Wilson interval, bar thresholds kmin(70)=56, kfail(70)=49, kmin(140)=112, Newcombe/power helpers) and `grader_cases` (the 103 synthetic self-test cases). No routing or CLI change. Evidence: `tests/test_lc2_grader.py` (103 cases; port-equivalence on 64 public Q37 items x 2 arms = 128 stored answers with identical programmatic outcomes, fixture in `tests/fixtures/lc2/` with repo and licence per item, GPL pylint items excluded; no limit literal in grader code).
- LC-3 (local-check runtimes): new `llm_router.local_check.runtimes` and `.runner`. Endpoint discovery (Ollama via `model_discovery`; LM Studio 1234, vLLM 8000, llama.cpp 8080 probed on loopback with short timeouts; `endpoint` override, loopback only), Ollama native and OpenAI-compatible chat adapters (temperature 0, 1024 output tokens, `<think>` stripped), per-runtime context-fit to `max_prompt_chars`, model digest (`None` = digest_unverified) and `settings_hash`; a serial resumable runner (240 s per item scaled by a 3-item rate probe, no retries, optional lock file, early-stop helper with the bar as a parameter, append-only 0600 results, Ctrl-C safe). No model name is hard-coded. Evidence: `tests/test_lc3_local_check_runtimes.py` (fake loopback Ollama-like and OpenAI-like servers on ephemeral ports).
- local-check judge (LC-4, library only; no CLI command yet): `llm_router.local_check.judge` ports the Q37 accuracy-only rubric judge with two backends, the user's Anthropic key (pinned model id passed in, temperature 0, served model id and exact cost from usage and the repo price table) or `claude -p` on the user's subscription (`--safe-mode`, `--tools ""`, hooks disabled by a settings file, prompt on stdin, fresh temp cwd guarded against any CLAUDE.md/AGENTS.md at or above it, env allow-list that never passes an API key, 240 s timeout, one retry on a CLI error or unparseable reply, the user's real HOME because the CLI authenticates from it). It adds plants gate G3b (good >= 11/12, swapped >= 11/12, mutated >= 5/6, judge errors <= 5%; a budget stop is INCONCLUSIVE, never PASS), `local_check.budget` (call/USD cap, default $3 and 120 calls, checked before every call; subscription quota check that warns above 80% and refuses above 95% unless forced; per-call estimate computed from `llm_router.pricing`) and `local_check.consent` (the plan/consent text and y/N prompt). No model name is a default. Evidence: `tests/test_lc4_judge.py` (fakes only: fake Anthropic client and fake `claude` script).
- LC-5 (local-check scoring, library only; no CLI command yet): new `llm_router.local_check.score` (per-item success = programmatic grader ok AND judge pass, pooled k/n over commit_message + pr_description with per-lane counts and Wilson intervals; verdict PASS at k >= kmin(n) (56 of 70, 112 of 140), powered FAIL when the Wilson upper bound is < 0.80, otherwise INCONCLUSIVE; a lane below 0.70 is excluded from the PASS; early stop when failures make kmin unreachable gives `not_passed` with partial counts, not a powered fail; `--no-judge` can only give FAIL or a probe, never PASS; a grader rule behind >= 40% of failures is reported; gates G0 integrity, G1 serves (timeouts, errors and empties counted once per row), G3a grader self-test, G3b plants and G5 completeness make a run INVALID and record nothing), `local_check.attempts` (per model/digest/bank/settings anti-retry log: draw 1 first, draw 2 only on a rerun, verdict on the cumulative union of 140, a third same-configuration run is refused, 24 h cooldown unless forced) and `local_check.session` (one check end to end, all collaborators injected, writes through `record_local_check` with bank, digest, runtime, judge, run id, scope, settings hash and attempts). `record_local_check` now accepts verdict `NOT_PASSED` and a lane `status` (`not_passed`, `lane_guard`), stored on a `failed` lane. Evidence: `tests/test_lc5_scoring.py` (fakes only, real bank; the stored Q37 qwen run re-scored with the ported scorer gives PASS 60/70, 32/35 commit_message and 28/35 pr_description: `test_port_equivalence_q37_rescored_is_pass_60_of_70` over `tests/fixtures/lc5/q37_cases.json`, from the PREREG-Q37 run started 2026-10-20 per `local_check/data/recommendations.json`). Anti-retry and scoring rules (D-263 revised, review of #669): an INCONCLUSIVE rerun writes nothing (an earlier recorded PASS is kept, the rerun is logged); a PASS or FAIL needs n >= 50 cumulative assigned items and >= 20 scored items per lane, otherwise INCONCLUSIVE ("scope too narrow"), and a draw whose context-fit count is below that is refused before the plants gate and the local arm; recorded counts are k over `n_assigned` (items an earlier early-stopped attempt never ran count as not passed), a store refusal is surfaced as `record_refused` and consumes no draw; G0 compares every served judge id (comma lists included) with `judge.served_model_matches`; a judge_error is not cached, errored answers are re-judged at the end of the run (2 more passes) and at most max(1, floor(5% of judged calls)) may stay unjudged, excluded from n, more is INVALID; an INVALID, a budget stop or an interrupt after a local item ran leaves the run dir as a reservation that the next run of that configuration and draw must resume (cached answers, judgements and plants; `fresh` or 7 days discards it), the first 2 INVALID/discards per configuration are free and the 3rd and later burn the draw, and an INVALID outcome shows the gates only (no k, no rates); the configuration key uses the model digest when known (`ollama cp` aliases do not get fresh draws); the plants gate result is cached per run id; `text_job_lines` shows `not passed` / `inconclusive` lanes apart from `failed`.
- LC-6 (`llm-router local-check`): new CLI that measures the user's own local model on the bundled public benchmark. `local-check [run]` discovers a runtime (`--endpoint`, loopback only unless `--allow-remote`), shows a plan screen (model + digest, hardware, items in scope, time, judge route, cost and cap, consent), then runs through `local_check.session`; `--dry-run` makes zero model calls, a `--no-judge` run is a probe: it records nothing, shows only in `local-check status`, and never records a verdict or enables routing, `--yes` is required without a terminal, `--profile quick` limits diffs to 12k characters, `--fresh` discards the open run (a strike). Refuses below 50 items in scope with `num_ctx` advice. Judge route (D-260): the user's Anthropic API key, else the `claude` CLI (subscription; warn above 80 %, refuse above 95 % of the plan unless `--force`), else `--no-judge` only; `ANTHROPIC_BASE_URL` pointing at llm-router's own proxy (sentinel port, `LLM_ROUTER_PROXY_PORT`, defaults) makes the judge go direct to api.anthropic.com, any other custom base URL refuses the API route (D-264). `status` and `explain` (per-rule failure histogram with public item ids, no item text). Probe, run-start, mismatch, budget and judge-setup errors and Ctrl-C map to distinct exit codes with clear messages (docs/local-check.md). LC-5b fixes in `local_check.session`: an unexpected exception or Ctrl-C after the local arm leaves a reservation without a strike; a missing or empty run directory of an open reservation is a discard strike; the 7-day expiry lapses without a strike (documented); removed the dead `inconclusive` branch in `text_job_lines`. Routing activation is not part of this change; the verdict screen says so. Evidence: `tests/test_lc6_cli.py` (fake runtime, fake judge, no network).
- LC-7 (local-check display surfaces): `llm-router status` and `doctor` now show the user's own local-check results next to the pre-registered text-job lines, from `local_eligibility.json` and the model-discovery cache (no HTTP call on these paths; digests come from `discovery.json` and are ignored when that cache is older than 7 days): `local-check: <model> — commit_message, pr_description passed k/n (bank vN, date) [routing: on|off|n/a]`, `not passed (k/n, bank vN, date); try: llm-router local-check recommend`, `stale — model changed` / `stale — expired`, and `not measured (llm-router local-check)` for an installed local model with no result (neutral colour, never a failure). Counts are always printed as k/n and an n below 70 adds `small n` (a small-n verdict is weaker evidence). `[routing: ...]` reads `local_routing.json` if present and shows `n/a` until the routing PR writes it. The fast status line (`statusline_tick`) shows `local ok` or `local n/p` only when a fresh result exists (the background refresher puts it in `statusline_cache.json`; the tick adds one dictionary lookup and no I/O). `llm-router install` and the first `doctor` print at most one dim tip line per bank version when a local model is installed and nothing is measured (state in `local_check/nudge.json`, 0600; it never prompts or runs anything). New `local_check.retention.prune` (called from `doctor`): keeps the newest 3 run directories under `local_check/runs/` (plus any unexpired reservation), and blanks the stored answers of a kept run once its results file is 30 days old. `docs/RETENTION.md` lists local-check runs, `attempts.jsonl` and `text_job_local` records. Evidence: `tests/test_lc7_display.py`. No hook file changed (`hooks/status-bar.py` untouched).
- LC-8 (local-check routing, D-260): a text-job lane that PASSED the user's own `local-check` now routes local-first by default, on the verified `llm_text_job` path only. `local_routing.json` (llm-router home) records `{"<model>": {"<job>": {"enabled", "since", "source": "local_check"|"prereg"|"manual"}}}`; new `local_check.routing` has `enable(model, job, source)`, `disable(model, job, reason)`, `is_enabled(model, job)` and `on_pass(model, jobs)` (the CLI/session calls it after a recorded PASS; a lane that was disabled stays off, and a pre-registered lane is never enabled automatically, only by an explicit `enable`). `eligibility_routing.prefer_passed(models, job, input_len, now)` moves a local model already in the chain to the front only when routing is enabled for it, its effective lane (own fresh local-check result, else pre-registered, else unknown) is passed, the lane is fresh against the model digest in the inventory cache (no HTTP), the input fits the lane's `scope.max_prompt_chars`, and the route-time Ollama `think` and `num_ctx` hash to the lane's `settings_hash`; otherwise the chain is left as built (D-116 order, F106 filter and F112 exemption unchanged; the acceptance check and the Claude fallback are unchanged). Demotion monitor: when more than 30% (7 of the last 20) of the calls for a (model, job) since it was enabled failed the acceptance check, routing for that lane is disabled (`reason: "demoted: fallbacks"`), `doctor` prints a line, and nothing re-enables it but an explicit `enable`. The proxy and authority are not touched. Evidence: `tests/test_lc8_routing.py`; see `docs/local-check-routing.md`.
- local-check LC-9: `llm_router.local_check.recommend.recommend(hardware, runtime, current_model, current_result)` lists local models that fit the user's probed RAM/VRAM and runtime, from the data file `local_check/data/recommendations.json` (no model name is in the code). Measured entries show their recorded result with source (today only PREREG-Q37, qwen3.6:35b-a3b-coding, 60/70 on the Q37 set, run 2026-10-20, recorded in `recommendations.json`; PREREG-Q37 overlaps bank v1 draw 1, with 37 of its 70 items being Q37 scored items, so it is in-sample); the rest are listed as untested with sizes and licences from cited pages. The pull command is shown, never run. A list older than 90 days carries a "newer models may exist" note.
- LC-10i (`llm-router local-check`, final integration): routing is live. A recorded PASS now calls `local_check.routing.on_pass` (D-260 default on) and prints per lane `routing: on` / `already on` / `kept off` (a lane the user or the demotion monitor disabled stays off; prereg-only lanes are never auto-enabled). New `local-check enable <lane> [--model M]` and `disable <lane> [--model M]` (inventory model names with tag; lanes `commit_message`, `pr_description`). `status` renders the LC-7 display lines plus the `local_routing.json` switches and demotion lines. Every completed run (any verdict) calls `retention.prune()` and reports errors only. After a FAIL, NOT_PASSED or an INCONCLUSIVE on fewer than 70 items the screen prints `recommend()` entries (measured first, untested labelled, pull command as text only) or `no verified recommendation for <runtime>`. On the subscription route the quota is re-read every 10 judge requests (`budget.recheck_quota`): a run above 95 % stops resumably unless `--force`, one above 80 % warns once. Fixes from the live dry-run: the "several models found" message and menu list each (name, runtime, endpoint) once; the plan hardware line shows capacity (total minus reserve) instead of momentary free memory (`0.0 GB usable`). Docs: `docs/local-check.md` (routing live, enable/disable, API-key billing line for the subscription route, retention, recommendations), linked from `docs/local-check-routing.md`. Evidence: `tests/test_lc10i_wiring.py` (fake runtime, fake judge, no network).
- context-pack builder core (PLAN v16 P1.1 tasks 1, 2, 4-core; R-CTX-1/2/4): new library module `llm_router/context_pack.py` with `build_pack(request, messages=None, *, session_id, transcript_path, project_root, target_window, door) -> ContextPack` and `messages_for(door, ...)`. The request is kept verbatim; `recent` is the last 7 messages with tool_use/tool_result rendered as text (a tool block over 4,000 chars keeps head 1,500 + `[…truncated N chars…]` + tail 1,500); `mode="full"` carries the whole conversation when it, the request and the instructions fit 0.8 x `target_window` and the whole conversation was read; only the last 2 MB of a transcript is read. `project` comes from `context_injection.inject` (OKF, semantic pack, repo state), `instructions` is a CLAUDE.md/AGENTS.md digest (headings and rule lines, <= 1,500 tokens, cached by content hash), `summary` stays `None` until P1.2. Errors in any section yield an empty section. Formats: Claude Code transcript JSONL, Codex rollout JSONL, Anthropic Messages, OpenAI chat, Responses-API items. Instructions come from the project's CLAUDE.md, .claude/CLAUDE.md and AGENTS.md only; nothing under `~/.claude` is ever read, including via `project_root == $HOME` or a symlink (owner decision D-43), and a `project_root` that resolves to `$HOME` is no project at all: no instruction files and no project section are read from the home root. `target_provider=` applies the session privacy rule to `recent` (new `session_store.allows_session_content`, now shared with `build_session_context`; behaviour unchanged there); `context=` carries an MCP caller-context string that never qualifies for `mode="full"`; an unknown `door` raises `ValueError`. No door calls it yet (the 7 door PRs follow); no hook changed.
- The context pack now carries the P1.2 rolling summary (PLAN v16 P1.1 x P1.2, R-CTX-3). When `LLM_ROUTER_ROLLING_SUMMARY=1`, `context_pack.build_pack` reads the session's `rolling_summaries` text into `ContextPack.summary`, which reaches the model in the `<conversation-summary>` layer. This happens only in `mode="pack"` and only at the hook, agent and mcp doors (`SUMMARY_DOORS`). The session is the explicit `session_id`, else the transcript's file stem, else, at the mcp door, the calling session. The same privacy gate as `recent` applies. The summary gets only the room left in 0.8 x `target_window` after the request, `recent`, instructions and project, and never more than `SUMMARY_MAX_TOKENS` (2,000). When it is over budget it keeps the Goal and Current state sections first, then whole lines, and the first line that does not fit is cut rather than dropped. Layer tags inside the summary text are neutralised. The read goes through the new `context_summary.read_summary_text`: read-only, with a 0.25 s busy timeout, and it never creates the database or table. With the feature off, the only cost is one environment-variable read, with no import. Any error means no summary.
- Rolling session summary (PLAN v16 P1.2, R-CTX-3), opt-in and off by default. `llm_router.context_summary` keeps one summary per session in `sessions.db` (`rolling_summaries`), refreshed by a detached worker the Stop hook starts only when `LLM_ROUTER_ROLLING_SUMMARY=1`; five fixed sections, local-model or extractive mode, host compaction summaries adopted. `load_summary(session_id)` is the read API for the context pack.
- Overflow policy for local calls (PLAN v16 P1.3, R-CTX-4, R-LOC-9), opt-in via `LLM_ROUTER_PACK_OVERFLOW=1` (default off): before `check_overflow` raises, the six local call sites first trim project knowledge, then summary detail (Goal and Current state kept), then the instructions digest, never the request or recent messages, then try the next local model with a larger `local_models.num_ctx` window, and only then escalate. Adds `llm_router.pack_overflow`.
- Provider-layer Ollama overflow handling now honors P1.3's larger-window step: after tagged context trimming, the LiteLLM provider path can switch to a validated installed local model with a larger known `local_models.num_ctx` window before raising `ContextOverflow`. Unvalidated, unknown-window, absent, and non-generator local models still escalate instead of leading.
- Prompt-cache layout (P1.4, R-CTX-8), opt-in and off by default: `cache_layout.build_layout` orders a request instructions, knowledge, recent turns, request with breakpoints after the stable system block and after `recent` (never on the request); `prompt_cache.inject_cache_control` gains an optional `recent_end`; `context_injection.inject_system_block` returns the knowledge block for system placement. Nothing calls them on a default path. The P1.4-a live measurement (`scripts/bench/cache_layout_live.py`) is pending a live run.
- OKF ranking and semantic refresh (PLAN v16 P1.5, R-CTX-5 / R-CTX-6; do not merge before G0): `okf.find_relevant` and `context_injection.inject` default to `limit=None`: every doc scoring at least max(floor, 0.5 x top score), best first, until 1,500 tokens (`LLM_ROUTER_OKF_BUDGET`) instead of a fixed top 3. The top doc is always kept and the identifier-anchor rule is unchanged; an explicit `limit=N` keeps the old top-N. PostToolUse Edit/Write/MultiEdit queue the path in `semantic_dirty.txt`; every 10 distinct queued files a detached child re-indexes them, and SessionStart drains the rest (`LLM_ROUTER_SEMANTIC_REFRESH=0` turns it off; only projects that already have an index are queued). New `semantic/extractors/typescript.py` (regex over a comment- and string-blanked skeleton) and `.ts .tsx .js .jsx` in the indexer. Not covered: class methods, call sites, other languages (16.1). An index built before this change re-extracts once.
- One classifier engine, core only (PLAN v16 P1.6; do not merge before G0): `llm_router.engine.decide(text, door=...)` returns one frozen `Decision` with every R-CLS-2 field, from L1 heuristics (`ENGINE_POLICY`), an optional pure-Python lexical L3 model (`ml_lexical.py`, artifact `engine_lexical.json`, built by `scripts/build_engine_lexical.py`), abstain-to-safer-tier, secret `local_only`, and an LRU + `decide_cache.sqlite` cache (0600, 30-day TTL). No LLM and no network on the sync path; nothing calls it yet (the call sites are wired in #433). Tests (`tests/test_engine_core.py`) use synthetic prompts and prove mechanics only, not accuracy. New env vars: `LLM_ROUTER_DECIDE_CACHE`, `LLM_ROUTER_ENGINE_LEXICAL`, `LLM_ROUTER_ENGINE_CALIBRATION`.
- one classifier engine, wired in SHADOW (PLAN v16 P1.6, D-72; Owner D-75: merge to main right after #471; no deploy; supersedes #454, #447 and #433): at the seven doors (proxy `backends.tier_classify` / `policy_chain`, MCP `ensemble.classify_for_routing` and `tools/agentic`, gateway `_classify`, the `auto-route`, `agent-route` and `gemini-cli-auto-route` hooks, `router._resolve_profile`) the rules still route, unchanged, and `engine` computes a Decision AFTER the rules decision and logs it beside the rules class and tier to `engine_shadow.jsonl` (capped by `capped_log`; Decision fields plus the rules tier, no prompt text). The engine never raises into the request path (`failopen` code `LR-FO-ENGINE-SHADOW`); the server doors reach the engine only through `llm_router.engine_door`, which falls back to the rules and records `LR-FO-ENGINE-DOOR` if the engine import or `decides()` fails, and the proxy binds the session only in decide mode. The server doors queue the shadow to one `ThreadPoolExecutor` worker (not a daemon thread: joined at interpreter exit, bounded by a 256-item queue; the request path pays the queue put); the hooks run it inline, L1 only. Shadow calls do not go through `engine.decide`, so they write no `decisions.jsonl` row (P1.7); only a decide-mode door records. The shadow call skips the capability detector, the lexical load in the hooks, and the decide cache. New env var `LLM_ROUTER_ENGINE_MODE`: `shadow` (default), `decide` (the #433 wiring: the engine decides, the hook leaves its Decision for the proxy by `text_sha`, `LLM_ROUTER_ENSEMBLE=legacy` keeps the old MCP path) or `off`. `decide` is not the v16.0 default: the P1.6-g held-out FAILed on the outcome basis (D-71/D-72). Complexity (D-69): `ENGINE_POLICY` is GATEWAY_POLICY plus keyword complexity and a simple band up to 599 chars, tuned on the GT-500 tune split only (`scripts/engine_policy_tune.py`). `scripts/door_agreement.py` gains the `mcp`, `agent_route`, `gemini_cli` and `router` sites; `scripts/hook_reuse_rate.py` reads the P1.6-h instrument. The hook version is 51. Tests: `tests/test_engine_shadow.py`, `tests/test_engine_doors.py` (decide mode).
- Decision ledger, shadow and drift (P1.7, draft; stacks on the P1.6 engine core): `engine.decide` appends a text-free row to `decisions.jsonl` (layer, confidence-or-abstain, reason, policy fields; `none` is written, never NULL), `routing_decisions` and the proxy ledger gain the matching columns, `outcome_link` links decisions to success / redone / escalated / unknown within 24 h (run from the SessionEnd background child), `decision_report` writes a daily accuracy and PSI drift record and the `kpi` classifier line. The tier-stratified 5-10% shadow is OFF: it needs `LLM_ROUTER_DECISION_SHADOW=on` and a passing P1.7-c gate (`scripts/bench_shadow_continuation.py --gate`). `docs/prereg/PREREG-shadowlab.md` is the P1.7-d prereg. Every live criterion (P1.7-a, b, d, e rates) is pending organic data.
- one resource map, core (PLAN v16 P1.8, branch `feat/map-core`): `llm-router map [--json]`
  and `llm_router_status(view="map")` return the same JSON, built by `resource_map.build()` and written atomically to
  `resource_map.json` (rebuilt when older than 300 s). One row per seat (claude, codex, gemini_cli), local model and API
  key, with status and reason, quota values as `{value, provenance, as_of}` (measured / estimated / stale / default),
  marginal cost, capabilities, per-model limits (`num_ctx`, max context) and `effective_priority` from the routing policy.
  Key values are never read. Not yet read by any chain builder (that is `feat/map-readers`), so routing is unchanged.
  `local_models.ROLES` declares that `nimble:9b` serves `decision` only, so the map does not list it as a generate model (N19).
- one resource map, discovery (PLAN v16 P1.8, DRAFT, do not merge before G0): every `Seat` now has `status` (connected /
  installed_not_connected / absent) and `reason`; Codex's reason is the first line `codex login status` printed (fixture:
  `Error loading configuration: ...config.toml:443:14: invalid transport`); new Copilot seat (`gh copilot --version`,
  `hosts.json`, VS Code extension dir, existence checks only); Gemini plan from `~/.gemini/settings.json` (`unknown` +
  reason when unrecognised). Old `seats.json` loads unchanged. New `discovery/keys.py` `discover()` returns
  `KeyInfo{var, provider, source env|dotenv:<path>|keychain, usable_by, billing}` without ever holding a value;
  `secrets_vault` gets a `keychain` backend. New `claude_creds.py` holds the one keychain query. Routing is unchanged.
- P1.8 task 6, readers: the router chain (`_build_and_filter_chain`), the hook chain (`hooks/chain_builder.build_chain`), the proxy chain (`policy_chain`) and the dynamic routing table now read `~/.llm-router/resource_map.json` when a usable map exists. Semantics: the map always FILTERS (resources whose status is not connected are dropped); it REORDERS by `effective_priority` only when the map's routing policy expresses an order (any policy other than `balanced`, e.g. `local-first`, `cost`). Under the default `balanced` policy the ranks are only row order, so today's cheap-first order is kept. `chain_builder._apply_subscription_local` follows the same rule: an ordering map replaces the subscription-local reorder; a balanced map filters and the reorder still runs as without a map. The reorder decision uses the policy recorded in the map file (not the live config), so a policy change takes effect when the map is rebuilt (`llm-router map`), up to 6 h. The MCP server's dynamic table (previously build-once) and the proxy chain cache are rebuilt when the map is rewritten, and also when it is deleted or older than 6 h (back to the no-map result); the proxy chain cache is cleared on each change. With no map file, or a stale, unreadable or other-schema one, every chain is exactly what it was. With a fresh map, a chain whose every entry is a map-known disconnected provider becomes empty (never routed to a disconnected resource) and each door takes its existing nothing-routable path: the MCP router raises its "No providers available" error so the host model answers, the hook falls through to Claude, the proxy forwards upstream, the SDK raises RoutingError. Providers with no map row keep their slots. Readers never build the map. Doors (`llm-router map [--json]`, `llm_router_status(view="map")`) were already on main with the map core (#402).
- `llm-router kpi --json` gains `offload` (PLAN v16 P1.9): the offload share per action, per proxy call, per human turn (O3) and the `spread` line for work another Frontier subscription served, each with k, n, Wilson 95%, n_sessions, largest-session share and window, plus `by_lane`, `by_task_type` and caveats. The numerator counts only actions in a quality-sampled lane (`lane_quality.jsonl`; none exists yet, so k = 0 by rule). Adds `offload_share.action_units`, `pricing.is_very_cheap` and `lane_quality`. O3 no longer counts as human turns the calls in no transcript message or the first answers to task notifications and other injected input (M0.3 window W0, PR #295). Measured 2026-09-29..2026-10-11 on a copy of `proxy_calls.jsonl` (43,468 rows): O3 integrity on the two organic sessions with 50 or more typed prompts went from 3.824 and 0.304 to 0.041 and 0.029 (3/74 and 2/69), outside the 0.90-1.10 bar, so the O3 warning stays; source `~/.rsi/research/primary-plan/scripts/o3_integrity.py`.
- P1.10: every ledger writer stamps `task_id` and `trace_id` (usage, routing_decisions, execution_events, proxy rows, edit_outcomes, verify records); the SDK no longer stamps the placeholder `sdk`. The host task id is `sha256(session_id + turn_index + nonce)[:16]`: the per-state nonce means a session resumed after its turn state was pruned never reuses an id (deviation from the PLAN formula). `begin_turn` waits at most 50 ms for its lock and fails open with a one-off nonce. `llm-router kpi --identity` prints, per writer, n, the share of rows with a session_id and a real (non-NULL, non-`gen-`) task_id, and a Wilson 95% interval. Evidence: `tests/test_p110_task_identity.py`, `tests/test_p110_verify_ids.py`.
- Add `llm_router.eligibility.grade` (P2.0 task 6): one pure eligibility rule (n >= 60, gap <= 3 pts, Newcombe LB >= -10) that returns unknown, probe, eligible or failed, plus a Newcombe method-10 CI helper. From raw counts the gap is computed exactly (Fraction). Both bounds allow 1e-9 pt of float noise. Malformed evidence (non-integer or impossible counts, a non-finite gap or LB) never raises and never passes. `newcombe_lb`, the key used in lane_quality rows, is read as the lower bound. `lane_quality.batch_passed` now grades shadow-pair batches through `grade`. Not wired into routing.
- P2.8 tasks 4-5: sha-pinned model catalog (`catalog/models.json`, 7-day TTL, bundled fallback) with `recommend(hardware, answers)`; approval-gated Ollama pull with progress; task-type fitness tasks recorded in `local_eligibility.json` (pass marks empty until the P2.0 prereg, so results are `unknown`).
- Single local-runtime install offer and onboarding needs questions (PLAN v16 P2.8 tasks 2-3). `llm_router.runtime_offer` detects Ollama, LM Studio, MLX and llama.cpp (`runtime_status()`: each `running` when its server answers on the `local_platforms` port, `LLM_ROUTER_LOCAL_*_PORT` overrides included, or `installed`; a bare `/health` on :8080 no longer counts) and supplies the one `runtime_offer()` text used by the six former "install Ollama" hint sites. The OS install command runs only after an explicit `y` in `llm-router onboard` at a terminal (piped input never runs it). The MCP `llm_setup(action="install_runtime")` only returns the command: a flag the model sets is not a human's approval, and running a 15-minute installer inside the MCP server would block it and hand the installer its JSON-RPC stdin. An install is reported as done only when it exits 0 and `ollama` is then on PATH; the Linux `curl ... | sh` runs under `bash -o pipefail`. Onboarding probes once, tells an installed-but-stopped Ollama to `ollama serve` instead of offering an install, and counts another runtime as a backend only while its server answers. The "No providers available" error shows the offer as its own block, and not at all when Ollama is configured. `llm_router.onboard_needs` asks at most three questions (main use, languages with Hebrew explicit, speed vs quality) and yields the `{main_use, languages, priority}` dict `model_catalog.recommend` accepts.
- `install.run_full(interactive)` factored out of the CLI install; `host_detect` now detects every snippet host (opencode, copilot-cli, openclaw, trae, factory, vscode, cursor, windsurf). CLI output gains ONE summary line, `also detected, not auto-wired: <hosts> (wire one with `llm-router install --host <name>`)`, when a detected host has a snippet but no auto-installer; nothing else in the output changes. Adds the 15.4.0 upgrade test and the P2.12-c preservation floor test. Review fixes before merge: a host's config dir that holds only files llm-router's own installer writes there no longer counts as the host being installed, so wiring a host with `--host` no longer makes later installs report it as detected. Copilot CLI is detected by `~/.copilot` and Factory by its `droid` binary. On Windows, config dirs follow `%APPDATA%`. Windsurf (per-project config) and Factory (plugin install) get a reason line instead of the `--host` hint. The preservation floor now also covers an upgrade with a Claude seat, where the only change is the documented one-time `.env` flag, and the upgrade tests no longer depend on the developer's PATH.
- P2.1 offline core (nothing wired, no live behaviour change): `technical_ops.classify_tool_call` (GE1 read-only op list; redirection, unlisted chains, writes -> None), the `technical_ops.jsonl` row writer (no text, basenames only, `capped_log`, mode 0600), and the bundled rule text no longer says "not your file tools" (rules-version 11; the rule text names no env var). Review fixes before merge: a quoted operator (`'|'`, `';'`, `'&&'`) is an argument, not a separator, so the command fails closed (before, `find . '|' cat -delete` read as a search). Formatters must be the bare name (`./black` is refused), and the `--write=`/`--fix=` forms count as writes. The log keeps ids, the model and the session kind only when they are identifiers, and the skip reason only when it is a lower-case code; any other text is stored as null. It writes `model`, `re_read` and `session_kind`, the fields `offload_share` reads, and `offload_share` now reads both generations of the capped log. The rules text no longer points back at a sentence that was removed.
- P2.2-A: `llm-router mode route|advise` and `LLM_ROUTER_AUTHORITY` (default `route`; `advise` is shadow: the proxy forwards the original bytes, prefixes included, and logs its proposal as `tier_proposed` / `tier_reason_proposed`, read per request so no restart), `llm-router install --authority route|advise`, a `skips.jsonl` log that refuses a row without a reason (the proxy writes none; only session-level skips and fail-open rows explain a call with no ledger row), `scripts/coverage_check.py` (decision-point coverage of host model calls, per-session cap 40, Wilson interval, routed share beside it, `--switch-file` required) and a `coverage` key in `llm-router kpi`. Nothing changes until an owner command is run: `PROXY_DEFAULT_ON_INSTALL` ships False, and a plain `install` prints one line saying routing authority is off. Evidence: `tests/test_authority_mode.py`, `tests/test_proxy_authority.py`, `tests/test_install_authority.py`, `tests/test_skips_log.py`, `tests/test_coverage_check.py`, `tests/test_kpi_coverage.py`.
- P2.6-a: automatic demotion levers with recovery. `proxy_haiku` (new `quality_breaker` class fed by the `proxy/haiku_guard.py` triggers): a guard trip now opens the breaker class instead of writing `tier_overrides.json`, and after the breaker's 24 h cooldown the rewrite is switched on again as a half-open probe over Haiku-served turns (5 turns, redo rate below 50%, the breaker's own thresholds); a failed probe reopens it, a passed one closes it and the triggers then only see newer evidence. Behaviour change: the guard no longer writes `tier_overrides.json`; that file is now owner-written only, still beats the breaker, and is never touched by it. `techop_local` lever key with trigger and recovery tests: the lever exists, it is not live, because `technical_ops.py` has no call site (P2.1 is blocked on P2.0). The local code levers (`proxy`/task via `local_agent/capability.BreakerCache`, `zero_claude_edit`/`code`) get trigger and recovery tests only; `local_agent/*` is unchanged. Evidence: `tests/test_p26a_demotion_levers.py`.
- P2.7: user prefixes `local:`, `claude:`, `opus:`, `sonnet:`, `haiku:`, `codex:` (and `native:` as `claude:`) are honoured at three doors. The UserPromptSubmit hook (v60) now honours a Claude-family prefix in every mode, not only zero-Claude, and forces `local:`/`codex:` through the routing directive with the prefix stripped (a prefix beats the classifier, the Q&A-quiet policy and the soft pressure override; enforce off/shadow, kill switches and hard budget caps still win). The proxy pins the named tier (`sonnet:`/`haiku:` generalise the `opus:` pin), strips the prefix from the forwarded body, forces `--serve local-agent` steps local with a one-line refusal instead of falling back to Claude, and records `prefix`/`prefix_outcome` on prefixed ledger rows only. MCP `llm()` runs the named model, or returns a one-line refusal/host note; the budget check and quality breaker still apply. Unknown prefixes are ignored. Each forced choice appends `{text_sha, forced, decided, door, ts}` (no text, bounded to 2 MiB) to `~/.llm-router/examples.jsonl`. P2.7-a is conditional-partial (D-136): 6/6 prefixes at the hook (the tier prefixes act as `claude:` there, since a hook cannot switch the host model), 4/6 at the proxy (`codex:` has no backend, and `local:` needs `--serve local-agent`). Not in this change: parsing in the engine L1 (`engine.py` does not exist yet), and a Codex or serve-off local backend in the proxy (those prefixes are forwarded unchanged with `prefix_outcome=unserved:*`). Evidence: `tests/test_p27_prefixes.py`, `tests/test_p27_proxy_prefixes.py`, `tests/test_p27_hook_prefixes.py` (hook run as a subprocess).
- P2.8-HW-1: new `llm_router.hardware` module with `probe()` and `probe_cached(ttl_s=30)` reporting total/free/usable memory, GPU class (apple_unified, nvidia, none), VRAM, arch and free disk on macOS, Linux and Windows. It never raises (failures become `None` fields plus `errors`), and its subprocess calls use no shell and a 2 s timeout. Nothing calls it yet, so routing is unchanged. Evidence: `tests/test_hardware_probe.py`. Review fixes before merge: the memory and GPU stages fail independently; Apple Silicon is detected under Rosetta 2 (`hw.optional.arm64`); `nvidia-smi` rows of `[N/A]` are skipped, and exit with 'No devices were found' is no GPU rather than an error; on Linux a cgroup memory limit caps total and free; `probe_cached` is keyed by its arguments and does not cache a result with errors.
- P2.9-b: static shell-write tripwire (`llm_router/write_policy.py`, `check_command`). Before a local agent's `bash`/`run_command` runs, redirects (`>`, `>>`, `>|`, `&>`, `2>`, heredoc), `tee`, `dd of=`, `cp`/`mv`/`install`/`ln` destinations, `sed -i`/`perl -i` targets and `cd <outside> &&` chains are resolved (symlinks followed) and a target outside the project root is refused; interpreter one-liners (`python -c`, `node -e`, `sh -c`, `eval`, `curl | sh`) and anything unparseable need approval and do not run. This is a tripwire, not a sandbox; the OS sandbox is 16.1. Behaviour change: `python -c` through the local agent's command tool is no longer run without approval. The P0.13 `xfail(strict=True)` shell-redirect test now passes.

  Follow-up: bare `-` interpreter args, `timeout`/`stdbuf` wrappers, `touch`/`mkdir`/`truncate`/`rm`/`chmod`/`chown`/`rename` operands, subshell-scoped `cd`, `~+`/`~-`, and `$((..))` / `$(cat <<E ..)` no longer counted as command substitution. `needs_approval` is treated as refused (approval is not wired).

  Follow-up 2: the `$(cat <<E ... E)` exemption is removed (a heredoc body can nest `$()` and its end cannot be found reliably), so `git commit -m "$(cat <<'E' ...)"` needs approval; use `git commit -F file`. `$((...))` counts as arithmetic only when it closes with an adjacent `))` and holds only identifiers, digits, spaces and `+ - * / % ( ) ! ~ ^ = , ? : $`.

  Follow-up 3: compound-command grammar words (`do then else elif if while until ! { } done fi esac`, `function NAME`) are stripped before each simple command is checked, so `for i in 1; do tee /tmp/x; done` and `for ((..)); do touch /out/q; done` are judged on `tee`/`touch`; `for`/`select`/`case` heads are data, `((..))` heads are skipped only when plain arithmetic, function bodies are checked at definition. A line using `let`, `declare`, `typeset`, `local -i` or `((` that also contains `$(` (not `$((`) or a backtick, quoted or not, needs approval (bash re-evaluates `a[$(cmd)]` in arithmetic). Review round 5 (35 escapes, each one confirmed to write outside the root under sh or bash, now gated):
  - an unquoted heredoc body with `$(...)` or backticks counts as substitution;
  - `-c`/`-e` is found after options that take a value (`python -W x -c`, `bash -o errexit -c`);
  - `env -S`/`-C`, `exec -a NAME` and `setsid` are handled;
  - hard links (`ln SRC DEST`, `cp -l`) are checked;
  - a `cd` in a command that also has a loop or a function needs approval, and so does `CDPATH`;
  - the `sed -i` forms `--expression=` and `-eSCRIPT` are parsed, and sed's own `w` command needs approval;
  - a case pattern's `)` no longer pops a subshell's directory;
  - `for x do BODY` bodies are checked;
  - re-evaluated subscripts are caught (`a[x]`, `read 'a[...]'`, `${!x}`, `$((cmd) )`);
  - a command name built from an expansion (`$T`, `$'..'`, brace expansion) needs approval, and a backslash-newline joins the word;
  - `cp -vt DIR`, `chmod -w`, `awk` `system()`/redirects, `python3 /dev/stdin` and the search paths of `find -delete`/`-fprint` are covered.
  Still a tripwire: `git -C`, programs that write on their own, and a script file written first and then run are not seen.

  Review round 6 (D-124, after the #452 delta review), now gated:
  - `case` after `then`/`do`/`{`/`!`/`time` keeps a subshell's directory (its pattern `)` closes nothing);
  - `find`: global options (`-H -L -P -D -O`) are skipped before the start paths, a bare path inside the expression counts, `-L`/`-H`/`-follow` with `-delete` needs approval;
  - `sed` scripts are parsed instead of matched by a regex: `w`/`W` and `s///w` files are write targets, `e` and `s///e` need approval, `-f`/`--file` scripts and unknown or abbreviated options are resolved or need approval;
  - `awk` gates only output redirection after `print`/`printf`, `system()`, `| getline`, `|&` and gawk `@` directives; `-f`/`-l`/`-i`/`-E`/`-W` need approval and `-o`/`-d`/`-p` files are write targets;
  - wrappers `ionice taskset flock strace unbuffer caffeinate fakeroot busybox` and `uv run`/`poetry run`/`pipx run` are unwrapped (flock lock files, `strace -o` and `fakeroot -s` files are write targets); `chroot`, `watch`, `trap 'cmd' SIG` and options before `run` need approval;
  - a heredoc without its terminator line (CRLF, `$[1<<2]`) and path errors (null byte, symlink loop) need approval instead of being skipped or raising;
  - `cp --target`/`--target-dir` abbreviations, `cp -s`, `/dev/fd/N` scripts, `csh`/`tcsh`/`mksh`/`yash` like `sh`, and `deno eval`, `bun -e`, `tsx -e`, `ts-node -e`, `Rscript -e`, `julia -e`, `osascript -e`, `expect -c`, `pwsh`/`powershell` command strings.
  No longer gated (false positives): `sed -n '/warning/p'`, `sed 's/world/x/'`, awk comparisons such as `'$1 > 5'` and `/a|b/`, `for`/`select`/`until` inside quotes or as plain arguments after `cd`, and a backtick inside single quotes. Newly gated benign forms: `trap 'rm -f tmp' EXIT`, `watch`, unknown sed/awk options. Still a tripwire, not a sandbox.

  Review round 8 (re-review of the round-6/7 changes): a sed `w`/`s///w` file name that still holds `$` or a backtick (the shell expands it first, e.g. `sed -n "w $D/x"`) needs approval instead of being judged as an in-root path; `gsed` is checked like `sed`; an awk backslash-newline continuation no longer hides a redirect or `| getline`; awk `print > "/dev/stderr"` (and `/dev/stdout`, `/dev/null`) is allowed; `uv run` with an option it does not know needs approval; launchers that run another command (`uvx npx bunx`, `pdm/hatch/conda/rye/pipenv run`, `pnpm/bundle exec`, `xvfb-run dbus-run-session su runuser pkexec systemd-run nsenter unshare`) need approval. Still a tripwire, not a sandbox.
- `llm-router prefer "<text>"` (and `llm_setup(action="prefer", text=..., confirm=...)`) parse English and Hebrew priorities (`X before Y`, `never X`, `no paid API`, `cap X N/day|N%`, `only local for <task>`), echo what was understood, and store them in a managed block of `routing.yaml` (every byte outside the block is preserved; the block is JSON flow style, readable without PyYAML). Unknown phrases are refused with examples. Routing is not changed until the doors read the stored preferences (next change); `llm_setup`'s new `confirm` parameter is inert for every action except `prefer`.
- Release eval gate, CI side (PLAN v16 P3.2, D-192): `scripts/release/eval_gate.py` is a paired comparator (blocks only when a set's pass drop is strictly above 3.0 points; prints exact McNemar p, Newcombe paired CI, discordant rate and n; fails as NOT INFORMATIVE on too few, missing, duplicate or mismatched items), with PACK-vs-FULL fidelity (<= 0.02) and decision stability (>= 0.98) rules. `release_evals.py` defines the set manifest and `release_evals/<sha>.json` formats. `pre-release-verify.sh` gains a mandatory gate step and refuses `--skip-quality` on a tag unless `LLM_ROUTER_RELEASE_OWNER_OVERRIDE=1`; `release.yml` gates `publish.yml` on the eval file attached to the GitHub Release; a PR job runs the comparator tests, mutants and pack goldens. The duplicate `scripts/pre-release-verify.sh` is removed. The four sets are frozen by PREREG-P32 (`docs/prereg/PREREG-P32.md`, frozen 2026-10-17T23:13:17Z) and amended twice: AMEND1 (`docs/prereg/PREREG-P32-AMEND1.md`, 2026-10-27) re-pinned the hash of one harness file after PR #582, and AMEND2 (`docs/prereg/PREREG-P32-AMEND2.md`, 2026-11-02) moved the fidelity window from 8192 to 65536 after the first scored run, post-hoc (see Known issues). The v15.4.0 baseline for the two performance sets is pinned in `release_evals/baseline.json`, and this release's own eval result is attached to its GitHub Release as `release_evals/<sha>.json`.
- P3.3 daily KPI job, installer wiring (D-220: installed only with explicit consent): `llm-router install` asks "Install the daily KPI job? [Y/n]" on a terminal after the Seats block, naming the files it writes, the command and time, and that the job reads only local ledgers; `--schedule-daily` consents without a prompt (the only way a headless install writes it), `--no-schedule` skips, and a plain headless install writes nothing. `llm-router uninstall` now unloads/disables/deletes the job through `daily_schedule.uninstall` before the generic manifest replay, so the job is never left loaded after its file is unlinked; proxy-default teardown is unchanged. `llm-router doctor` gains one row: scheduler entry, last run age, `late_days` ("not installed", "last run n/a", "late_days n/a" when unknown, never 0). Linux and Windows entries are not verified on a real machine. Evidence: `tests/test_p33_installer.py` (tmp HOME, fake runner).
- Daily offload rows: `llm-router kpi --daily [--date D --backfill N --force --partial --synthetic-manifest PATH]` writes one UTC-day row (organic, synthetic (D-167), other) with action and turn offload share, Wilson and session-clustered CIs, by task group / served model / lane breakdowns, gap to 0.60 and to a preregistered T_P2 (when its file exists), and `k_if_all_lanes_sampled`; a day below 30 actions or 10 turns is "unmeasured" (no value, no CI; minimums await owner ratification, D-193). `kpi --daily-streak [--end D]` counts consecutive closed days with measured organic action and turn lines and exits 1 below 14 (PLAN v16 section 7.1 c3). `kpi --daily-render-plist` prints a launchd plist and installs nothing. `routing_report.executed_routes` gains optional `since`/`until`.
- P3.8-a (zero-config route test): `tests/test_p38_zero_config_route.py` pins two zero-config cases on an empty HOME with no env, no config file and the network refused. (A) With a keyless backend installed (Codex CLI faked as present; the Gemini CLI and Ollama are detected the same way) one request routes exactly once to it. (B) With nothing installed `route_and_call` raises `NoRoutableModelsError` carrying a `llm-router doctor` hint, before any backend call. Not zero-config: the Claude subscription route needs `llm_router_claude_subscription`, which defaults to False and is set by `llm-router install` (an NFR-CFG gap, so "works with no config" holds only when a keyless backend is installed). A subprocess `llm-router config` smoke test (exit 0, no traceback) is included and is not routing evidence. The env strip list is derived from `env_registry.ENV_REGISTRY` and `RouterConfig`. Evidence: `tests/test_p38_zero_config_route.py`.
- PLAN v16 P1.1-a: `tests/test_context_pack_doors.py::test_a_spy_sees_each_door_call_build_pack` is the seven-door spy. It runs one behavioural test per door (hook, MCP, proxy, Codex, agent-route, SDK, gateway; each drives that door's real call site) in a child pytest with `context_pack.build_pack` wrapped before any test module is imported, and requires that the doors seen are exactly the seven, one per test, and that every call came from production code (never a test calling `build_pack` itself). It runs under `timeout(180)` in the `nested_pytest` xdist group (P12-FLAKE-1 precedent). The static call-site and test-file checks stay; the gateway's explicit `context_door="gateway"` is caught only by the static check, since that is also `route_and_call`'s default.
- P1.1-b local stratum measurement, synthetic (D-99): `docs/measurements/p11b-synthetic-local.json` (PACK vs HOOK6, qwen3-coder:30b, extractive summary, window 8192, n=60 paired, 0 errors) scores PASS under PREREG-P11B-synthetic + AMEND1 (worse 0, better 40, exact McNemar p 1.8e-12). HOOK6 passes 0/60 and both arms pass 0/20 early_tool/early_assistant items; the write-up in `docs/measurements/G1-synthetic-2026-10-14.md` records this caveat.
- P1.6-k: repo-wide outbound-host allow-list test (`tests/test_p16k_outbound_hosts.py`) over src/ and hooks/; 0 telemetry endpoints; Frontier shadow and judge off without the OD-4 switch.
- P1.7-d synthetic harness (PLAN v16 P1.7-d, D-106/D-107/D-109): `scripts/p17d_synthetic.py` adds `select`, `shadow`, `label`, `score` and `status` after the frozen `docs/prereg/PREREG-P17D-synthetic.md`. `select` runs the seeded 0.50 session-capped walk, the section 2a admission, the feasibility gate and the early `assemble_mismatch` gate. `shadow` replays each selected item through the production `ShadowScheduler` against nimble:9b on localhost Ollama, in a scratch HOME, after a digest check, a quiet gate and one warm-up. `label` runs the Opus rubric through `claude -p`, calibration first, with a hard $10 spend cap and the weekly-quota pause. `score` runs `shadow_eval` on the last record per key and applies the section 7 verdict order. Every command except `status` refuses unless the prereg matches its freeze record, and once `use.log` is terminal. Every output is labelled `synthetic (D-99)`. No routing change. Every nimble attempt first waits out the local classifier's timeout cooldown; a failed judge call is charged its reported cost or the spend-guard estimate, with a provisional ledger row written before the call; `score` requires a finished `label`, and `shadow`, `label` and `score` require a clean `scripts/`/`src/` tree at the logged harness commit. The one `~/.llm-router` read (`usage.json`, quota pause) is disclosed per D-110. `score` reads the P1.7-c status from `~/.rsi/research/primary-plan/gates/v16/P1.json` (`test_gates_2026_10_13["P1.7-c"].verdict == "PASS"`), refuses if that file or key is missing or unreadable, and records the file's path, sha256, verdict and gap disclosure in `result.json` and the measurement (D-111a); the `--p17c` flag is gone.
- Map re-discovery (PLAN v16 P1.8 tasks 8 and 10). The resource map rebuilds when an env file, the Codex auth file, `usage.json` or `codex_quota.json` changes mtime, or when the signature of Ollama's `/api/tags` changes. The signature hashes the sorted `(name, digest, modified_at)` triples, so a re-pull under the same name is detected; the map's Ollama rows gain an additive `digest` key (schema stays 1). Diffs are appended to `map_changes.jsonl`, and the next UserPromptSubmit hook output carries one line such as `map: + qwen3.6 local`. The MCP server and the proxy poll every 30 s; with neither running, the hook starts one detached refresh (stat-only check, 60 s debounce). `auto-route.py` hook version 54. New state files: `map_watch.json`, `map_notice_cursor.json`, `map_changes.jsonl`, `map_refresh.lock`, `map_refresh.spawn`. Opt out with `LLM_ROUTER_MAP_REDISCOVERY=0`.
  Review fixes: the cwd `.env` is no longer a key of the global watch state (it caused a rebuild on every poll that alternated project directories); `refresh_if_stale` takes the poll lock (non-blocking) so a diff is logged once, and writes its change log beside a custom map path; `map_changes.jsonl` is capped at 256 KB (last 1000 lines kept) and the notice cursor is a timestamp, not a byte offset; the poller and the detached child record failures through `failopen`.
  Known limits: because the cwd `.env` is no longer a watch key, a change to a project's `.env` is caught only by the 300 s TTL; and rebuilds from different cwds can still show key add/remove diffs, because `build()` reads the cwd `.env`. Second review fixes: the notice cursor never advances past a batch whose trailing line is still half-written (the batch is re-read, not skipped); `refresh_if_stale` skips only when the lock is held by another process, and when locking is unavailable (e.g. unwritable `$LLM_ROUTER_HOME`) it rebuilds unlocked as before and records `CHZ-FO-MAP-LOCK-UNAVAILABLE`; `file_lock.exclusive_lock` takes an optional `status` dict that reports `acquired`, `held` or `unavailable`.
- Added the P2.0 requalification harness (preregistration draft, not frozen; the harness refuses until frozen) and the record_competence writer, which preserves fitness alongside validated competence lanes.
  Added an offline pinned pool builder for QA, technical-operation, and code requalification with private outputs, source hashes, transcript reconstruction, and exclusion accounting.
  Added the frozen-gated P2.0 local code runner with hidden-test grading, resumable per-arm results, and quota-gated Sonnet reference calls on the shared spend ledger.
  Added frozen AMEND1 identity overrides, per-command code identity gates, and Claude session persistence suppression.
  Added AMEND2 enforcement: qwen3.8-uncensored:latest as the only local arm, think:false on every local Ollama call (Pi agent thinking off), and a foreign-arm guard on run, judge, ingest-code and score.
- QUOTA_BALANCED now orders subscriptions with `resource_map.score` in one
  `route_and_call` pass using primary/weekly pressure, a reset bonus, marginal
  cost and policy priority. Unknown inputs are never scored; unknown pressure
  follows measured pressure below 0.95 and precedes measured pressure at or above
  0.95. The old `quota_balance` reorder is removed from the router's chain build.
  The compatibility `chain_builder.build_chain` and agent-route Codex delegation
  also use the score under the quota_balanced profile. Weights and checks are
  registered in `docs/prereg/PREREG-P23-quota-score.md`.
- The judge queue now samples with a deterministic per-door sampler (`judge_sampler.py`) instead of a global `random.random()` gate: for each door (proxy, sdk, gateway, mcp, hook, agent, technical_ops, plus `unknown` reported separately) exactly one call in every `ceil(1/rate)` consecutive eligible calls is picked, with counters `{seen, picked, lock_skipped}` persisted in `judge_sampler.json` under the shared file lock (hook callers use a non-blocking lock and fail open, recorded via failopen). `enqueue_for_grading` and `log_routing_decision` accept a `door` keyword stored in the queue entry, `LLM_ROUTER_JUDGE_SAMPLE_RATE` is validated to (0,1] (0 still means off, anything else invalid falls back to 0.1), and the drain appends a text-free outcome row to `judge_results.jsonl`. No call site passes a door yet.
- P3.1-2: judge door tags. Routed answers now enter the judge sampler under their real door: `sdk` (sdk.py), `gateway` (route_server.py, which the HTTP gateway uses), `mcp` (the router finalizer, inside an MCP tool call), `hook` (main-session DIRECT) and `agent` (agent-route DIRECT), all through `hooks/savings_logger.log_direct_to_db` or `cost.log_routing_decision(door=)`; untagged calls stay `unknown`. The agent-route PreToolUse path never samples or writes (pinned). `judge_sampled` reader (F80): a door that only lock-skipped now alarms instead of reading unknown, the lock-skip share needs at least 20 calls, and `bool` no longer counts as an integer. Evidence: `tests/test_p31_door_tags.py`.
- Judge selection (`judge_select.py`, P3.1-3) is decided at drain time from canonical model identity instead of raw strings, and no judge model is named in code (D-205). `canon(model)` strips the provider prefix and the default `:latest` tag; `same_model(a, b)` also compares `/api/tags` digests and `model_aliases.model_family` plus tag, and treats any doubt (empty name) as "same". A cloud answer is graded by the strongest installed generation-capable local model, else the row is `ungraded_no_judge` (never a cloud judge for a cloud answer); a local answer is graded by the strongest OTHER local model. "Strongest" ranks `details.parameter_size`, then on-disk `size` (both now kept in `discovery.json` by the Ollama probe), then the user's `LLM_ROUTER_OLLAMA_MODEL`, then discovery order; unknown metadata ranks below known, never as 0. The Haiku fallback for a local answer with no second local model takes its id only from `LLM_ROUTER_JUDGE_MODEL`, `judge.haiku_model` in `routing.yaml`, or the haiku tier of the user's `claude_tiers.yaml`, and stays OFF behind `LLM_ROUTER_JUDGE_HAIKU_FALLBACK` (default off) until P3.1-4 lands the privacy and spend gates it must not run without. `judge_results.jsonl` rows now carry `judge_model`, `judge_kind` (`local`|`subscription`) and `status` (`ungraded_no_judge` replaces `ungraded`); `_grade_one` refuses on canonical-identity doubt; the `claude-haiku-4-5-20251001` default parameter of `_evaluate_background` is removed. `get_judge_scores_for_model` returns `avg_score`/`min_score`/`max_score` of `None` (was 0.0) when there are no judged rows. Fixes `ollama/foo:latest` being judged by `foo`. Evidence: `tests/test_p31_judge_select.py`.
- Judge queue hygiene and spend caps (P3.1-4). `judge_queue.jsonl` is the only judge file with text: an entry whose prompt or answer matches a `secret_scrubber` pattern is skipped whole (failopen `CHZ-FO-JUDGE-QUEUE-SECRET-SKIP`, no text in the record), the rest passes `shadow_text.scrub` and `persist_redact`, is capped at 8000 characters each, and the file is created and rewritten mode 0600. Entries are deleted at claim; unclaimed entries older than `min(LLM_ROUTER_PERSIST_TTL_DAYS, LLM_ROUTER_JUDGE_QUEUE_MAX_AGE_DAYS (3))` are purged at drain and at the enqueue size check. `_enforce_queue_cap` now does one `stat` per enqueue and re-reads the queue only below 64 KB or when an append crosses a 256 KB stride, instead of reading the whole queue on every hook-path enqueue. New `judge_ledger.jsonl` (no text; capped at 5000 rows): `ts, door, answering_model, judge_model, judge_kind, tokens, cost_usd (None = unknown, 0 only for a measured local call), outcome`. The Haiku judge, still off unless `LLM_ROUTER_JUDGE_HAIKU_FALLBACK` is set, now also stops with a recorded outcome at `LLM_ROUTER_JUDGE_HAIKU_MAX_CALLS` (20/day, `cap_calls`), `LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME` (400000/day, `cap_tokens`), and whenever the quota reading is stale or unknown, session >= 80% or weekly >= 85% (`quota_skip`); the flag is re-checked at call time (`privacy_off`). Judge calls run with `ANTHROPIC_BASE_URL` removed so they never loop through the proxy. `llm-router judge status` and `llm-router kpi --judge` show today's calls and tokens against the caps. Fixes F93: a cloud answerer whose last path segment equals an installed local model's name (`openai/foo` with local `foo`) is no longer classed local, so it can never reach the Haiku path. Evidence: `tests/test_p31_judge_hygiene.py`.
- Judge quality view (P3.1-6): `judge_view.judge_summary(since, until, session_id)` reads `judge_results.jsonl` and `judge_sampler.json` (no database) and reports, per door and overall, n judged, mean score (n/a when n is 0), the self-judge count (must be 0), ungraded rows by status and sampler seen/picked; a door under 50 judged answers is flagged "not informative". `llm-router kpi` prints it as a "judge quality" section; the session-end hook (v41) prints `judge: mean X (n=N, window)` or `judge: n/a (no judged calls, window)` from a `judge_summary.json` cache that the detached child writes (the Stop only reads the cache). The child also starts a rate-limited, low-priority judge drain when the queue is non-empty.
- P3.1-7: judge figures ride on `ledger_view` windows. `LedgerWindow` gains `judge_n`, `judge_mean` (None at n=0), `judge_self_judge` and `judge_not_informative` (n<50), read from `judge_view.judge_summary` for the same bounds as the window (additive; `to_json`/`from_json` stay at v2 and an older cache without the fields loads as unknown, printing `n/a`, never 0), plus `LedgerWindow.judge_line()` and a `judge` entry in `figures()`. `window_summary(..., judge=True)` is opt-in (default False): the judge read parses all of `judge_results.jsonl` (O(file)), so the synchronous Stop path (`dashboard_data.summary`), the refreshers, gain and the savings report never pay it; only the kpi judge section and the session-end child's ledger cache opt in. `dashboard_data.summary("today")` does no judge read (the synthetic timing of the judge read has no recorded date or script, so no figure is given here). The kpi judge section parses the file once (was twice). The kpi judge section's headline figures now read `ledger_view.judge_figures`; the statusline snapshot and the Stop line keep reading the `judge_summary.json` cache (the same `judge_view` function; their equality with the ledger window is tested). Evidence: `tests/test_p34a_six_surfaces.py` (hand-counted judge n/mean/self-judge over seeded `judge_results.jsonl` for today, 24h, 7d and 14d, equal across the ledger window, kpi, the statusline snapshot's cache and the Stop's cached line).
- PREREG-P32 release eval harness `scripts/release/run_p32.py`: runs the four synthetic regression pins (`rt_hand_60`, `rt_agt_50`, `fid_syn_60`, `stab_syn_500`) model-free and offline, writes the `eval_gate.py` input file with provenance, refuses a worktree/sha/package mismatch, and writes `release_evals.py freeze`-compatible manifests (`--write-manifests`). `PIN.json` gains `prereg_sha256`; `eval_gate.py compare` and `release_evals.py pin` take `--prereg release_evals/PREREG.sha256` and return NOT INFORMATIVE when the sidecar is absent or differs.
- `kpi --daily` rows: every breakdown group now carries `wilson95`, `gap_to_0.60`, `gap_to_T_P2` (when the prereg file exists) and `informative` (false below 30 actions, value and interval null, k/n always shown); top-level `breakdown_basis`; provenance `run_by` / `lateness_h` (excluded from `--force` equality); `daily/.last_run.json` after every run; new `kpi --daily-status [--json]`; `--daily-streak --json` gains `scheduled_days` and `late_days`. Schema stays `daily-offload/2` (additive).
- P33-2 (daily scheduler entry): `llm-router kpi --daily-install [--yes]` installs an opt-in OS job that runs `python -m llm_router.cli kpi --daily --backfill 7` once a day at the local-clock equivalent of 01:15 UTC (computed at install), with `LLM_ROUTER_SCHEDULED=1` (and `LLM_ROUTER_HOME` only when it was set and is not the default); `kpi --daily-uninstall` deactivates and removes it. macOS: LaunchAgent `com.llm_router.kpi-daily` (Nice 10, low-priority IO, logs under the state dir's `logs/`), activated like the proxy default. Linux: systemd user `llm_router-kpi-daily.service` + `.timer` (`OnCalendar=*-*-* 01:15:00 UTC`, `Persistent=true`); with no user systemd nothing is installed and the manual command is printed (no crontab). Windows: a Task Scheduler XML registered with `schtasks /Create /XML`. The Linux and Windows entries are NOT verified live on this machine: all three are rendered and unit-tested against fake launchctl/systemctl/schtasks runners, and the macOS entry has not been installed for real either until the owner runs `--daily-install` (P33-4). Every written file is recorded in the install manifest (path contains `kpi-daily`), `install_manifest.records()` / `forget()` are the public reader, and `daily_offload.scheduler_entry` now uses `records()`. `kpi --daily` takes `daily/.lock` (a second run prints `busy` and exits 0) and a scheduled run exits 0 `deferred_busy` while `<state>/bench.active` exists (opt-in marker; no benchmark script in the repo writes one, so otherwise the job relies on Nice/idle IO). `--daily-render-plist` now prints the plist the installer writes. A `.last_run.json` write failure of any kind is recorded as `CHZ-FO-DAILY-LAST-RUN` and never changes the exit code. `scheduled_days` / `late_days` over a window that mixes rows from before and after P33-1 count only the rows that carry the key. Evidence: `tests/test_p33_daily_schedule.py` (CI only, not run on the author's machine).
- P35-1 (D-220): units and the quota rate. User-visible number changes:

  - Measured and estimated dollars are no longer added. `Summary.compact()` (the Stop line's lifetime figure, the Codex Stop line) prints `~$5.00 est · $2.00 measured` instead of one `~$7.00 est`; the Stop's today figure does the same. The split is required by the P3.5 plan acceptance ("measured apart from estimated"). A window with only unconfirmed savings still prints `~$X est`; one with only confirmed savings now prints `$X measured` (it used to print `~$X est`).
  - `show(fig, unit=, quantity=)` renders through a units table: `37.5% of window`, `~2.5 work-hours`, `$1.50`, `~$1.50`; `with_n=True` appends the n(s) and window (`~2.5 work-hours (n=12, rate n=10, 7d)`). Estimates are always marked `~`.
  - New `llm_router.quota_rate`: median weekly quota points per active hour from `quota_history.jsonl` (14 days); fewer than 10 active hours is `n/a (k of 10 active hours)`. `~work-hours` = quota-% saved / that median.
  - The quota-saved figures (`llm_quota_saved`, the routing notice's `saved Xpp wk / Ypp 5h`) no longer assume a built-in plan price: with no `LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV`, `LLM_ROUTER_WEEKLY_QUOTA_USD` or `LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH` set they print `n/a`. Anyone who relied on the old `$200/month` default must set one of those.

  The session-end hook pair is now v43 (installed copies update on the next `llm-router install`).
- P35-2 (D-220): the statusline shows the five P3.5 fields, each with its n and window, and no longer adds measured to estimated money. User-visible changes:

  - New second row under the default statusline (Claude Code prints each line as its own row): `router: <model> (<reason>) | offload <x>% (n=, session) | Claude 5h <p>% (n=1 poll, age <m>m, 5h) / Codex <k>/<budget> delegations (5h) | quality <q>% (n=, 7d) | saved ~<h> work-hours (n=, rate n=, today)`. A `judge <mean> (n=, window)` field is appended when the judge cache has judged answers. The router is the newest proxy turn or routing decision of the session; offload is the per-action share of the session; Claude 5h is ONE `usage.json` poll, so it says so (`n=1 poll` and the poll's age); with no fresh poll it is `Claude 5h n/a (n=0, 5h)`, never a % with n=0. Codex is a count, not a %: the delegations the router's Codex window counted in the last 5 h against its delegation budget (`Codex 0/15 delegations (5h)` when the window is empty: a measured count of zero, D-237); with no `codex_window.json` (never delegated on this machine) it is `Codex n/a (n/a, 5h)`. quality is the blind-judge verdict rate against the Frontier answer (Wilson interval stored in the snapshot). saved = (human turns served off Claude today x weekly quota-% per Claude turn) / median weekly quota-% per active hour. Its n is turns, the unit of the burn: a turn's first call (or a zero-Claude hook turn) served by a local, very-cheap or other-Frontier model and not redone; continuation calls, MCP `llm()` assists inside a Claude turn, technical ops, Haiku and redone turns are not counted. A local `turn_first` row counts as a whole replaced Claude turn even when Claude served that turn's continuation calls (the same definition as the O3 turns count), which is one reason the figure is `~`. The burn per turn is `quota_burn`'s measured `weekly_per_turn` over 14 days: a POOLED RATIO (sum of per-session weekly deltas / sum of their Stop turns), not the median per action the plan named; quota samples are taken per Stop turn, never per model call, so no per-action burn can be measured. The per-active-hour rate is a median (P35-1). No turn served off Claude today prints `saved n/a (n=0 turns off Claude, today)`, also when the rate is unknown; any other unknown prints its reason once (`saved n/a (3 of 10 active hours)`). Unknown prints `n/a` with its n and window (an offload whose session has no quality-sampled lane prints `offload n/a (n=15, session, no quality-sampled lane)`), never 0. A snapshot older than 15 minutes prints `n/a` for the whole row.
  - The money segment is two segments now: `💰 ~$1.20 est·today n=3` (estimated) and `💵 $0.50 measured·today n=1` (confirmed), each with its own n. It used to print one summed `~$1.70 est·today n=4`. This continues the P35-1 reversal of the 2026-09-27 merge rule (needs D-27 ratification).
  - New `snapshots/<host>-<session>.json` in the router home (0600, newest 20 kept), written by one function (`statusline_snapshot.refresh`). The default layout's own detached refresher calls it (the row costs the shell one file read, no process); `llm-router statusline --refresh --host H --session S` calls it for Codex, Pi and the fast tick, and recomputes the global `statusline_cache.json` only when that is older than a minute. `--host` defaults to `claude_code`; both flags take plain ids only.
  - The default layout's detached refresher now also appends the 5-minute `quota_history.jsonl` row (same function, slot lock and 5-minute rate as the fast tick, router home only). Before, only the fast tick wrote that file, so `saved` could never print a value in the default layout. `saved` still prints `n/a (k of 10 active hours)` until 10 active hours of history exist (P35-1). The file has no size cap or retention rule yet (about 290 rows a day while a statusline is drawn).
  - The refresher's 30 s hard stop now survives the 10 s snapshot budget: the budget raises an exception that a field's own error handling cannot swallow, the budget is the smaller of 10 s and what is left of the 30 s, and the remaining hard stop is armed again after the snapshot. SIGALRM is blocked while the handlers and timers are swapped, a budget signal still pending after the snapshot finished is dropped rather than reaching the hard stop, and if the budget fires in the last instant before the restore the refresher re-arms the hard stop itself.
  - The fast tick (`LLM_ROUTER_STATUSLINE=fast`) starts a refresh sooner when the database (`usage.db-wal`, else `usage.db`) or `proxy_calls.jsonl` was written after the session's snapshot (after its cache when there is no snapshot yet), at most once per 10 seconds; it passes the session id from the session JSON so the same run writes the snapshot. Its line is unchanged.
  - `shadow_frontier.verdict_summary(days=7)`: n, k, rate and Wilson interval of the blind-judge verdicts where the served answer was acceptable; `cannot_judge` rows are counted apart, not in n.
  - `ledger_view.LedgerWindow` gains `verified_n` and `unverified_n` and `figures()` gains `saved_measured` and `saved_estimated` (additive; `saved` is unchanged).
  - C1 control characters (U+0080-U+009F) are stripped from the snapshot line and every cache value, in Python and in the shell, as C0 controls already were.
  - The statusline cache file has new keys `money_m`, `money_m_n`, `snap_line`, `snap_at`. The statusline script is not versioned and has no installed pair; re-run `llm-router install` to copy the new script and segments module.
- P35-3 (D-228): the end-of-turn line on Codex and Pi is the statusline snapshot line. User-visible changes:

  - The Codex Stop hook (`codex-stop.py`, hook version 1 -> 2) prints the session's `snapshots/codex-<session>.json` line as its systemMessage: the same text as the second statusline row. It no longer sums the ledger itself, so the old `⚡ llm-router · today ~$X est · lifetime ~$Y est` message and its `savings unavailable` fallback are gone. A missing, unreadable, unversioned or older-than-15-minutes snapshot, or a payload without a plain session id, prints `n/a`, never a number. The hook reads the file (a stat and one JSON read), imports nothing from `llm_router` (no asyncio, no sqlite) and does not load `.env` files any more, so `LLM_ROUTER_HOME` must be in the real environment. It starts a detached `llm-router statusline --refresh --host codex --session <id>` (at most one per 10 s; `LLM_ROUTER_STATUSLINE_REFRESH_CMD` replaces the command prefix) and never waits for it. `llm-router` not being on PATH is reported on stderr, not hidden. The refresher is what imports pending `savings_log.jsonl` and `routing_quality.jsonl` entries now (same atomic importers; it did this on every turn inside the hook before). Install the new copy with the usual hook redeploy: a stale copy keeps printing the old message.
  - `codex-stop` is registered in `HOOK_BUDGETS_MS` at 300 ms (the house Stop-hook budget, D-228). The hook does not time itself: importing the recorder would be the `llm_router` import this change removes; the budget is checked by a timing test.
  - `llm-router pi` loads a new `summary.ts` extension (main agent only, not sub-agents). After each agent run it prints the session's `snapshots/pi-<session>.json` line (a notice in the TUI, stderr otherwise, never stdout) and starts the same detached refresher with `--host pi`. Same `n/a` rule as above.
- P35-4 (D-220): a session summary block, every figure with its n and window; unknown prints `n/a`, never 0.

  - New `llm_router.session_report`: `build(session_id)` (or `build(scope="today" | "24h" | "7d" ...)`) and `render()`. Lines: targets (actions served `local` / `cheap` / `frontier` / `spread`; the offload numerator is local + cheap, spread work is in n only), offload (the S1 per-action share; a session with no quality-sampled lane is `n/a`), escalations (proxy rows whose `tier_reason` is an escalation, with the session's proxy-call n), semantic-cache hit rate (`n/a` below 20 lookups; the counts are still shown), quota saved (`~% of window` and `~work-hours`: turns served off Claude x the measured weekly points per Claude turn over 14 days, divided by the median points per active hour; both n's), quality vs Frontier (blind-judge verdicts over 7 days, acceptable rate beside the Frontier answer's rate, with n; the verdicts carry no session id), the judge mean, and one recommendation computed from the session's dominant task group and the eligibility rule applied to the verdicts, or `no recommendation (n/a: <missing input>)`.
  - `ledger_view.cache_hit_stats` narrows a session-id scope to that session's lookups.
  - `llm-router summary --session [ID]` prints the block (default: the current `session_id.txt`).
  - The session-end background child appends the block to `last_summary.ansi` (default and disabled modes; in the full mode the Stop writes that file and the child leaves it alone). The Stop's own line is unchanged and does no new work.

  The session-end hook pair is now v47 (installed copies update on the next `llm-router install`).
- P35-5: weekly offload report built from the daily rows only (never re-scanning a ledger). `llm-router kpi --maintenance` runs the due daily backfill (7 closed days) and writes the last two closed ISO weeks to `<state>/weekly/<YYYY-Www>.json` and `.md` (schema `weekly-offload/1`, atomic, rewritten only when the measurement changes): offload trend per UTC day, pooled per-action and per-turn rate with a Wilson 95% interval, breakdowns by task group / served model / lane, and a week-over-week delta only when both weeks have at least 5 daily rows and 30 actions (otherwise "not informative (n=...)"; unknown prints `n/a`, never 0). It defers while the benchmark marker `bench.active` exists. Session start (hook v42) stats `weekly/.last_run.json` (no `llm_router` import) and, when it is missing or older than `LLM_ROUTER_MAINTENANCE_STALE_H` hours (default 24), spawns one detached `kpi --maintenance` (at most once an hour, off with `LLM_ROUTER_MAINTENANCE_AUTO=0`). The pooled interval is Wilson because daily rows carry no per-session counts for the clustered bootstrap. Evidence: `tests/test_p35_weekly.py`, `tests/test_p35_fixtures.py`.
- New `llm-router why [--last N] [--json]` and `llm_router_status(view="why")` (P3.5-f): for the last N decisions, the door, model served and tier_reason, then class, confidence, layer, quota state and alternatives considered with exclusion reasons; anything not recorded for that door prints `n/a (not recorded for <door>)`. Read-only; ids only, no prompt text. Join probe: `decisions.jsonl` has a null `trace_id` today, so hook/MCP rows use their own `routing_decisions` columns and no decision-id write was added.
- P3.6-a and P3.6-b (classifier learning and self-protection, R-CLS-5, R-CLS-7, R-RTE-7). **Learning store and update gate:** `examples.jsonl` now also takes human-validated redos (the `r` key on the receipt band, hash only) and "always local" rules (hash only: a rule sentence is not a prompt and is never embedded), next to the forced-prefix rows it already held. A forced-prefix row written by the proxy or MCP carries, only when the kNN index is on and has an artifact, a **user-derived embedding** of the person's own prompt (computed in a worker thread, never on the event loop; the hook writes hash-only and makes no network call); prompt text is never stored, and rows older than 180 days are deleted from the file on append and in `learn`. New `llm-router learn [--dry-run] [--replay PATH] [--json]` folds the rows that carry an embedding into the L3 kNN index, but only after re-scoring the candidate index on a held-out replay set (`LLM_ROUTER_LEARNING_REPLAY`, at least `LLM_ROUTER_LEARNING_MIN_REPLAY` rows, default 100, every row with a `text_sha` or the file is rejected): an update that worsens exact-match or under-route by more than 2 points, or that has no usable replay set, is rejected and the live index is left untouched. `learn` runs under a lock file, writes through a unique temp file, and keeps the previous index as `complexity_knn.json.prev` only for a base it actually read. `llm-router learn --purge-user-examples` rebuilds the index without the user-derived embeddings `learn` added. **Classifier health (D-255):** `llm-router status` (and `kpi` when there is data) shows the 7-day **rules-vs-kNN agreement** with n against the prior 28 days, and a PSI line for the rules' tier mix. The two deciders live under D-121 are the rules and the L3 kNN; each turn on which both answer (proxy, kNN on) appends one text-free row `{ts, rules_tier, knn_tier, source}` to `classifier_agreement.jsonl`. This is agreement, not ground-truth accuracy; with the kNN off it prints "not measured (kNN off, n=0)". The killed LLM classifier's shadow log is read only as a labelled fallback if its file exists. A drop of 5 points or more at n >= 100 in both windows switches on conservative mode: the proxy tier policy looks the tier up one complexity level higher, and one notice line is printed. That bias changes what is served only with `authority=route` (under `advise` it moves the ledger's proposed tier only), and the hook and MCP paths get no bias yet (follow-up). It never fires below n = 100, and `LLM_ROUTER_CLASSIFIER_CONSERVATIVE=off` removes the bias. Retention: `docs/RETENTION.md`. Evidence: `tests/test_p36_classifier_learning.py`, `tests/test_p36_classifier_health.py` (synthetic fixtures; the owner's GT-500 is not used). Not included: P3.6-c (redo detector re-validation).
- P3.6-c PR1: `llm-router redo-sample --census|--freeze` counts organic redo-detector pairs and draws seeded F/S/R validation strata into private state files, with hashed session identities, exclusion support, supply gating, concentration caps and estimation weights; no prompt or answer text is printed or stored. The shipped M0.9 hash list is empty because its ids are not derivable from the repository; a follow-up must supply the owner's local hashes. Census evidence is limited to synthetic fixtures, not an owner's transcript census. Evidence: `tests/test_redo_sample.py`.
- Redo validation can opt into untagged sessions with `redo-sample --include-untagged` (default off), reporting per-kind census counts, frozen populations and draw sizes, and precision/recall alongside the combined gate. M0.9 exclusions can be supplied through the local state directory's `redo_m09_excluded.txt` or `--exclusion-file PATH`; census/freeze report the effective list size, SHA256 and source while retaining hashes only.
- P3.6-c PR2: `llm-router redo-score --run ID` scores frozen text-free validation labels with owner precedence, source agreement, Wilson precision, stratified recall and seeded session-cluster bootstrap 95% intervals for precision and recall. Bootstrap intervals are reporting only. It checks detector/manifest hashes, refuses invalid labels and reused runs, and records a private single-use marker before results; a marker without results remains spent. Write-free `--dry-score` refuses spent runs and prints only coverage counts, SHA check outcomes and agreement n. The precision gate requires precision >=0.80 and TP>=15 and becomes inconclusive when unclear or lost-as-not-redo sensitivity changes the outcome. Any lost flagged-stratum pair or overall lost above 5% voids the run, taking precedence over inconclusive; the completeness rule is recorded in results. Unclear rates use labelled denominators excluding lost, with warnings above 10% in F or overall. Concentrated capped draws report precision but no recall estimate. Evidence is limited to synthetic fixtures in `tests/test_redo_score.py`, not an owner validation run.
- P3.6-c PR 3 (redo detector tune mode, D-276): `llm-router redo-sample --tune --run ID` draws a TUNE set into `redo_validation/tune-ID/` with its own seed and `purpose: tune`, excluding the M0.9 list, every pair in any existing test or tune manifest, and (with `--freeze`) a later test freeze now excludes every tune pair and records `--detector v2|v3a|v3b`. `llm-router redo-score --tune [--candidate v2|v3a|v3b]` scores a candidate detector variant against the tune labels (made by the unchanged `redo-label`): per-signal and overall tp/fp/fn, precision, recall and F1, counts only, repeatable, never writing `test.used`, refusing a `purpose: test` run, and refusing a third distinct candidate via the counts-only `tune_log.jsonl`. `redo_signal` gains named variants `v3a` (tighten `repeat`) and `v3b` (demote `repeat`) written from the design, not fitted to data; `v2` stays the default. Evidence: `tests/test_redo_tune.py`.
- P3.6-c PR4: `llm-router redo-label --run ID --judge|--owner` labels a frozen redo validation draw. `--judge` is a sealed, resumable `claude -p` runner (shadow_frontier isolation flags, allow-listed env, batches of 20 plus 6 synthetic calibration pairs, served-model check, stops on a calibration miss, per-invocation call and USD caps, counts-only output); `--owner` is a blind terminal UI (y/n/u/q, shuffled, needs a TTY and unredirected stdout, no scratch files). Text is re-derived from the transcript at label time and never written; label files hold only pair id, label and source (`labels_judge.jsonl`, `labels_owner.jsonl`, accepted by `redo-score --labels`). Evidence: `tests/test_redo_label.py` (synthetic transcripts and a fake `claude` caller only; the real model was not called).

  P3.6-c PR4 repair: `redo-label` now refuses (exit 2, nothing written) when `--projects-dir` is missing or empty, when no pair resolves, or when more than the 10% unresolved threshold of the run's pairs cannot be resolved (`--force` waives only the 10% floor); `lost` is written only for single absent pairs in a resolving corpus, and session read errors are counted in the output. The judge env is an exact-name allow-list (no `ANTHROPIC_*`); `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` or a custom `ANTHROPIC_BASE_URL` refuse the run unless `--allow-api-key` (key case only). `--model-id` is required for `--judge`, recorded in `judge_meta.json` and must match on resume. A persisted `judge_spend.json` ledger makes call/USD caps cumulative and checked before each call, stops after 2 consecutive rejected batches and honours the subscription-quota gate (`--force` overrides). `allow_abbrev=False`; the owner TUI clears the screen on exit. Evidence: `tests/test_redo_label.py` (synthetic only; the real model was not called).
- S3 vs the GE6 baseline week and S7 retention (D-26 option A). `llm-router kpi --quota-burn` now also reports the baseline week (7 days from the first GE6 row, or `--baseline-start`), the per-turn comparison (n/a when the baseline is zero or unknown, never a reduction claim), the 5-minute series burn and S3 per-session coverage with n and a Wilson interval. New `llm-router kpi --retention` shows this install's 14-day status from a local `active_days.jsonl` (one row per local day: date and a locally generated install id; nothing leaves the machine; at most 400 rows, first row kept), written from the existing session-start and Stop quota-sample path with no hook change. Retention rule in `docs/RETENTION.md`, definitions in `docs/repo_goals/KPIS.md`. No measured value is claimed: the 95% coverage bar and S7 need live data (7 days and 14 days after the marker ships).
- P2.12 (P212-1): `llm-router install` now leaves an install receipt. Every full install writes `install_receipt.json` in the state dir (file mode 0600) with one row per host: claude-code, claw-code when wired, and every host `host_detect` finds. Each row says `wired`, `not_wired` (with the reason) or `error` (with the message), and for wired hosts the hook version read back from each installed hook file. `wired` means every hook file is installed and registered: its installed path appears in a hook command in the host's registry (Claude Code or claw-code `settings.json`, `~/.codex/hooks.json`, the Gemini CLI extension `hooks.json`). A sha256 `digest` covers the package version and each host's status and hook versions, but not the timestamp, paths or how a host was wired, so two install paths that leave the same state give the same digest. The install prints one line, `receipt: <digest8> hosts=<wired>/<rows> version=<pkg>`. A host installer that raises, or a hook file missing or unregistered after install, is now an `error` row and the install goes on to the other hosts, instead of aborting part way; a raised error is also printed to stderr and counted as a fail-open (`P212-INSTALL-HOST-FAILED`). A plain `llm-router install` then exits 1, and `llm-router quickstart` prints its manual-install fallback instead of "configured". New `llm-router install --auto`: the full install with no prompt, no daily-job question and no routing-authority step (it still records a detected Claude subscription in the state `.env`, as every install does), for npm postinstall, the plugin and the MCP door to call; it exits 0 when every row is `wired` or `not_wired`, 1 when any row is `error`, and 2 when combined with `--host`, `--project`, `--check`, `--headless`, `--proxy-default` or `--authority`. Evidence: `tests/test_p212_receipt.py` (receipt contents, `--auto` against `run_full` on a twin HOME, a host-matrix cell for every host in `host_detect._HOSTS`, negative controls for a raising installer, a missing hook file, an unregistered hook, a wrong or missing receipt and the quickstart fallback), plus a receipt assertion in `tests/test_p212_upgrade_from_15_4_0.py`.
- P2.12 (P212-2): the npm postinstall now wires your hosts. After it downloads the binary and checks that it runs, it runs `llm-router install --auto` and echoes that run's `receipt:` line, replacing the old "next step -> llm-router install" hint; the Claude Code plugin pointer is now printed only when no host was wired. If the installer fails or cannot start, the postinstall prints a warning with the installer's reason and the `llm-router install` command to retry, and still exits 0, so `npm install` keeps the working binary; the failure stays in the install receipt. The installer gets 120 seconds and is then killed, with the same warning. Under `npx` or `npm exec` (which the Claude Code plugin uses to start its server) the postinstall wires nothing and points at `llm-router install` and the plugin instead, so plugin users do not get every hook twice. npm hides a successful postinstall's output, so the receipt line and warnings show only with `--foreground-scripts`; `llm-router doctor` shows what was wired. A failure to download or run the binary is still fatal. Set `LLM_ROUTER_NO_AUTO_INSTALL=1` to install the binary without wiring any host (an install-time switch, not a documented setting). Evidence: `tests/test_npm_wrapper.py` (P212-2 section; real node against a stub binary in a fixture HOME, including the full `node install.js` run with the release download served from a local tarball; fails rather than skips in CI when node is missing).
- P2.12 (P212-3): the Claude Code plugin keeps the wired hosts on its own version, and `install` no longer wires Claude Code hooks twice for a plugin user. The plugin now ships a SessionStart hook (`hooks/plugin-install.py`, Claude plugin only; the CLI installer does not register it) that compares the plugin version with the `package_version` in `install_receipt.json`. On a mismatch without consent it prints one line pointing at the new `/llm-router:setup` command and does not repeat it for that version; with consent it starts `npx -y llm-routing install --auto --via-plugin` detached, once per plugin version. Running the installer through the plugin records the consent (`plugin_install_consent.json`, mode 0600). New `install --auto --via-plugin`: Claude Code's hooks and MCP entry come from the plugin, so nothing is written for them to `settings.json` or `~/.claude.json`; the claude-code receipt row reads `via=plugin` with the plugin's hook versions, every other detected host (Codex, Gemini CLI) is wired as usual, and the receipt digest equals the one a CLI install gives. Fix: the same detection now runs in every `install --auto` and `run_full` (so the npm postinstall too): when `CLAUDE_PLUGIN_ROOT` is set, or `installed_plugins.json` lists an enabled `llm-router@...` plugin whose install path exists, the Claude Code hook and MCP writes are skipped, where before a plugin user who also ran `npm i -g llm-routing` got every hook registered in both places and firing twice. If `settings.json` already holds llm-router hooks from an earlier CLI install, the install says so and points at `llm-router uninstall`. Evidence: `tests/test_p212_plugin_install.py`. Review fixes: the detached install is pinned to the plugin version (`llm-routing@<version>`) and runs in the state dir, not the project dir; a failed attempt is recorded with a timestamp, retried after 24 hours at most 3 times per version, reported with a line pointing at `/llm-router:setup`, and cleared when the receipt matches. Plugin detection counts only user-scope entries or project entries for the current directory, treats a missing `enabledPlugins` key as not enabled, prints the plugin path it skipped for, and `install --auto --no-plugin` forces CLI wiring. The hook only passes a plain `x.y.z[-pre]` plugin version to npx, treats planted or corrupt state as attempts exhausted, and a user-scope plugin entry is judged only on the user's own `enabledPlugins`, never on a repository's settings.
- P2.12 (P212-4): the MCP install door. `llm_router_admin(action="install")` installs from inside an MCP host in two steps, without a new tool or a change to the door count. The first call (empty `value`) lists the hosts it would wire and the hook files and registry for each, returns a confirm token, and writes nothing. The second call with `value='{"confirm": "<token>"}'` runs the same full install as `llm-router install --auto`, with stdout and stderr captured so nothing reaches the MCP stdio stream, and returns the host rows and the receipt digest, which equals the CLI's for the same state. A wrong token, a token older than 10 minutes, or one issued before the detected hosts changed is refused and nothing is written. It never changes the proxy default or the routing authority. Evidence: `tests/test_p212_nl_install.py` (no writes on step 1, empty process stdout, digest equal to `install --auto` on a twin HOME, refused tokens).
- Pi (pi.dev) is wired by `llm-router install`, MCP config only (D-315): `--host pi`, or a plain/`--auto` install on a machine with Pi, adds `mcpServers.llm_router` to `~/.pi/agent/mcp.json`, keeping every other server and key and the file mode; an existing `llm_router` entry is left as it is, and a file that is not valid JSON (or has no `mcpServers` object) is refused and left unchanged (the pi receipt row is then `not_wired` with the reason). A symlinked or read-only `mcp.json` is refused the same way. When a `pi` binary is on PATH, `--host pi` and a plain/`--auto` install run `pi mcp list --json` (it starts every enabled server in the file); `--host pi` prints the result and exits 1 on a refusal or a failed check, and a plain/`--auto` install records it in the pi row's `smoke` field (`passed`, `failed` = an `error` row, or `skipped` without the binary; outside the receipt digest). Unexpected `pi mcp list` output is a failed check, never an aborted install. The install summary no longer says Pi sessions auto-route: Pi gets the MCP tools only. `llm-router uninstall` removes only the `llm_router` entry. The quickstart Pi writer now uses the same writer (no more `INSTRUCTIONS.md` Pi never read, no `lifecycle` key). No hooks and no end-of-turn line on Pi: planned in 16.1. Kimi's `--host kimi` message now says "planned in 16.1".
- **Proxy local gate (default off, P2.1/P2.10 groundwork).** `llm-router proxy --local-gate on` (or `LLM_ROUTER_PROXY_LOCAL_GATE=on`) serves a continuation on a local model only when its lane (tech_op, qa, code) is eligible in `local_eligibility.json` and the quality breaker, P0.15 promote gate, F14 capability filter and backend health pass. Every step's reason is in the ledger row's `local_gate`; a missing or malformed file always forwards. Off is byte-identical to before. See `docs/proxy.md`.
- T21f (v16 acceptance, dyn adaptation floor): a timing-drill test for same-name re-pull detection. Twenty seeded drills re-pull an Ollama model under the same name with a new digest at a random phase of the 30 s map poll, on a fake clock with no network, and require, per drill, a `digest` event, the map row's digest updated, exactly one `map_changes.jsonl` line and a notice naming the model, then an identical-digest re-pull that writes nothing through a TTL rebuild; across the 20 drills the detect latency must be <= 60 s at p95 and <= 300 s at worst. A negative control swaps in a names-only `tags_signature` (the pre-P1.8-e behaviour) and requires it to miss the 60 s bound. Fixture result (n = 20 drills, seed 20261028, `tests/test_t21f_timing_drills.py`): p95 = 25.7 s, max = 29.0 s; the names-only mutant detects only at the 300 s TTL rebuild (p95 = 325.7 s). Evidence: `tests/test_t21f_timing_drills.py`.
- `llm-router prefer` rules now steer routing: a stored order rule ("codex before claude") puts that provider ahead in the chain and a ban ("never claude opus", "no gemini") removes it, at the MCP/router and hook doors and at the proxy's non-Claude model choice. The proxy chain cache rebuilds when the rules change. Caps and "only local for <task>" are stored but not applied yet, and a Claude-model ban at the proxy tier decision is not covered.
- `llm-router prefer` caps, "only local for <task>" and "no paid API" now steer routing at the MCP/router door, the hook and SDK chain builder, and the proxy's non-Claude model choice. A per-day cap ("cap codex N/day") drops that provider or model once the router has executed that many calls for it in the rolling 24 hours. Calls the router itself sends are counted and limited (MCP llm tools, hook- and SDK-routed calls); calls the proxy serves from a non-Claude model are limited by a hit cap but not counted toward it; Claude Code turns forwarded to Claude are neither counted nor limited. A percent cap ("cap claude N%") drops the target while its quota pressure on the resource map is at or above the percent. "only local for code" keeps only Ollama models on this machine (loopback daemon, no `-cloud` tag) for that task type; LM Studio, vLLM and llama.cpp do not count as local for this rule, other task types are unchanged, and no local model means the usual no-route result. "no paid API" keeps local runtimes, Codex and Gemini CLI (treated as unpaid because they run on your seat), an `openai_compat` endpoint only when its base URL is loopback, and Claude only in Claude subscription mode. Bans (from the previous change) and these three rules also apply to a pinned model, including the proxy's configured `--model` pin. A cap that cannot be read never blocks routing. A ban or cap on a Claude model at the proxy tier decision is not implemented.
- W1-1 privacy door matrix (UN-04, R-RTE-8, NFR-PRIV): `tests/test_w11_secret_door_matrix.py` classifies every prompt-carrying call site under `src/llm_router` (AST scan of `route_and_call`, `route_and_stream`, `route_payload*`, `call_llm*` and `litellm.*completion` callers at main 8d607849: 44 sites in 43 rows: 32 router-door, 5 provider-guard, 3 own-check, 1 local by construction, 2 fixed prompt, 1 unreachable). A new call site that is not in the table fails the test, and a stale row fails it too. An in-process socket trace (`socket.connect`, `connect_ex` and `getaddrinfo` recorded, no connection ever made) sends a generated GitHub-token prompt through 16 doors (route_and_call, route_and_stream, MCP llm_query, llm_route, llm_stream, llm_edit, llm_text_job, SDK `route` and `Router.run`, gateway chat, responses, messages, `/api/chat`, `/api/generate`, `/route`, and the route server) with a loopback model and a cloud model in the chain: 0 non-loopback peers at every door, and the loopback model was dialled at every door (so the trace saw something). With secret detection off the same 15 doors that have a cloud entry dial the cloud peer, and with `private_dispatch_active` patched to False the provider layer dials it. A second scan keyed by sink (CLI runners, direct_executor call_gemini / call_openai, urlopen / open_url, httpx / requests / aiohttp, subprocess to a model CLI, anthropic / openai SDK clients, bare litellm `completion`) finds 150 calls in 143 keys at main 2ec7707d; each has a row or its file has a pinned-count row (50 probe, 29 local, 23 router door, 13 opt-in, 13 proxy, 5 fixed prompt, 4 primitive, 7 guarded, 6 LEAK), and a new urlopen call site fails the test. 6 sink calls in 5 functions still send a secret-bearing prompt to a cloud sink, traced by 4 strict xfails (each also has a plain positive control, so a guard before the sink flips the xfail to XPASS): direct_executor `call_gemini` / `call_openai` (the secret value is redacted by `scrub_text`, the rest of the prompt leaves) and the auto-route API classifiers (opt-in). The llm_codex and llm_gemini tools, the agent-route doors (UN-04b, #772) and the playwright-compress Gemini call (UN-04c, #775) were leaks and are now class `guarded`, with plain passing tests. Router doors are traced at the `litellm.acompletion` stub boundary, not through litellm's own transport. Other findings recorded as strict xfail, no behaviour change: `judge_cascade.judge_inline` sends prompt and response to a cloud judge with no private check (no entry point reaches it), and `ensemble.local_llm_classify` trusts `LLM_ROUTER_ENSEMBLE_PRIMARY` to name a local model. Evidence: the test file, run once with the local suite lock.
- UN-06 (SEC-S6): every resource-map row now carries a `privacy` class, `local` or `cloud`, taken from `local_privacy.stays_local` (loopback Ollama daemon and not a `-cloud` tag); when that class changes for a row present in two consecutive maps, `map_changes` records a `privacy` event and the next hook prints `map: ! <model> privacy local->cloud` (`~` for the reverse). A map written before the field existed announces nothing on its first rebuild. Evidence: `tests/test_sec_s6_privacy_notice.py`.
- W1-2 (UN-08): `llm-router kpi` and `llm-router doctor` now print an ineligible-route line: how many routed decisions of the window went to a model that was not eligible at decision time (provider has no connected row, or a local model absent or not connected in the inventory), read post-hoc and read-only from `usage.db` `routing_decisions`, `proxy_calls.jsonl` and the decision-time `inventory_snapshots` that W0-1 stamps. Rows are split into organic, synthetic, coordinator (research) and untagged populations, and "informative" is judged on organic rows only (300 decisions, 3 sessions, largest session at most 60%). A row with no `inventory_sig`, or a signature with no stored snapshot, is reported as "not evaluable" with its count and is never counted as eligible, so an empty or unstamped window never reads "0 ineligible". Capability and privacy ineligibility are not stored per decision and are not counted. `doctor` flags organic ineligible routes. Evidence: `tests/test_w12_ineligible_counter.py`.
- UN-10 (W2-7, release rule 7.1.7): `scripts/check_numbers_provenance.py` lints README.md, `docs/*.md` and `changelog.d/*.md` so a result-shaped number (percentage, "N of M", multiplier, latency, dollar figure, token volume) needs a source (a file path, `PR #NNN` or a `~/.rsi` path) and an N or window on the same paragraph, list item or table. Config thresholds and list prices are recognised by keyword; `<!-- provenance: exempt (reason) -->` is the escape. The lines already flagged when the lint landed are recorded in `scripts/numbers_provenance_baseline.json`, and the lint fails on a new line or on a baseline entry that no longer matches, so the baseline only shrinks. Evidence: `tests/test_numbers_provenance.py` (planted unsourced figure goes red; sourced, config, version and date controls stay green; the repo at head is green).
- Added `scripts/bench/l1_latency.py` (W2-8 / UN-11, R-CLS-8 heuristic half, P1.6-c): times the L1 engine path (`llm_router.engine.decide`, no LLM, no network) on a real prompt corpus passed by path, and decides the first 500 prompts in two separate processes with different `PYTHONHASHSEED` to report the agreement share and any disagreeing ids. Output is JSON with n, date, commit sha, p50/p95/p99/max, cold-first call, the corpus sha256 (never its text) and an `uncontended` flag from load1. The corpus is JSON Lines `{"i": id, "text": prompt}` and stays outside the repo. Evidence: `tests/test_bench_l1_latency.py` (invented fixture; a sleeping stub engine is reported over 50 ms, a hash-seed-dependent stub is reported unstable). The recorded run on the release-candidate SHA is separate and is not part of this change.

  The default sample is a seeded random draw (`--seed`, recorded; `--first-n` for file order), `scope` in the JSON reports whether a lexical artifact was actually loaded (`rules-only` or `rules+lexical`), and the script exits 1 when `ok` is false (fallback layer, short corpus, p95 over the bar, unstable, network attempt, or a given artifact that did not load).
- W2-8b / R-CLS-6: each daily offload row (`kpi --daily`) now carries `classifier_agreement` with `n`, `agree_pct`, `window` and `source`, computed from the day-window rows of `classifier_agreement.jsonl` (rules vs kNN agreement, not accuracy). A day with no compared row has `agree_pct: null`, `n: 0` and `status: "not_measured"`, never 0; the schema stays `daily-offload/2` (additive key). The outcome-link half of R-CLS-6 stays 16.1. Unlike the `kpi` status line, the daily row does not fall back to `classifier_shadow.jsonl` (a different population), so with only the shadow log present the status line can show a percentage while the daily row says not measured. The agreement log is rotated, so a backfilled or `--force`d older day can report `measured` with an `n` that covers only the rows still kept. Evidence: `tests/test_daily_classifier_agreement.py`.
- W0-1: every routing decision row carries `layer`, `confidence`, `reason` and `inventory_sig`, so "routes to an ineligible model" and the `llm-router why` layer become countable. Evidence: 2,717 `routing_decisions` rows, `layer` and `inventory_sig` empty on all of them (independent audit, 2026-10-25). New `decision_stamp` module; writers stamped: `router.route_and_call` (MCP tools and the gateway), the failed-call row in `cost` (layer NULL), `hooks.savings_logger.log_direct_to_db` (hook, agent-route, SDK) and the proxy ledger (`cls_layer`, `cls_confidence`, `cls_reason`, `inventory_sig`, stamped at write time). `layer` is a closed set (`decision_stamp.LAYERS`: the ledger layers `prefix`/`L1`/`cache`/`L3`/`L2`/`fallback` plus `heuristic`, `llm`, `cached`, `override`, `unhinted`, `rules`, `knn`); a door or method name (`agent-route`, a fast-path name) is stored as NULL. `confidence` is the classifier's own number and NULL when it has none (override, fallback, unhinted, prefix, 0.0): never fabricated. `inventory_sig` is sha256[:12] of the resource map's per-resource chain id, kind (local/subscription/api), status, effective priority, capabilities (tools, vision) and context window, plus the policy (not quota or timestamps). The first time a signature is computed its inventory is stored in the new `inventory_snapshots` table of `usage.db` (never deleted, like `routing_decisions`, which has no TTL; see `docs/RETENTION.md`) and `decision_stamp.resolve_inventory(sig)` returns it, so a row stays checkable after the map changes. The signature is cached per process keyed on `map_reader.token()`: a map rewrite changes it on the next call (cached per map token, not for a fixed time); a snapshot write waits at most 0.2 s on a locked `usage.db` and a failed write is not cached, so the next call retries it; a warm call is one `stat`. NULL when there is no usable map. Rows written before this change stay NULL. Not stamped: the lineage store's separate `routing_decisions` table and `model_tracking.jsonl`.

### Fixed
- Pre-release verify: the numbers-provenance check skips files git ignores, so the suite's generated docs/SCENARIO_REPORT.md no longer fails the real-repo scan (PRV at RC6, 2026-11-07: 35 false findings, all in that file). The detached-orphan runner tests no longer depend on timing: under load, the process tracker could link and kill the orphan before the refusal path ran.
- `redo-score` on a test run frozen before the detector source changed (e.g. before #706) now says so and that the
  test half stays unspent, instead of the generic invalid-inputs refusal; the unreadable-prior-run refusal names the
  run id(s) and the recovery step (move it aside or remove it), never a path.
- ledger on every exit (LEDGER-EVERY-EXIT-1, docs/bugs/LEDGER-EVERY-EXIT-1.md): one guard around the whole `llm()` and `llm_act()` body leaves one `usage` and one `routing_decisions` row (caller's session, $0, `error_*` / `breaker_open`, excluded from metrics by the shared predicates) on every exit, including failures before dispatch, `llm_act` refusals and silent returns; paths that already wrote are not doubled. A hard kill stays unledgered. Not re-measured on a live M0-3 run.
- quality breaker (N21, docs/bugs/N21.md): units of `research` and `harness` sessions no longer feed the breaker. In the M0-3 rerun2 (2026-10-10, n=20 calls, 2 research-tagged sessions that discarded every answer on purpose) they opened `mcp_llm:code` for 24 h for every session. A breaker refusal in `llm()` now writes one `usage` and one `routing_decisions` row (`breaker_open`, caller's session, $0); the shared predicates (`SQL_NOT_ERROR_ROW`, `SQL_REAL_DECISION`) exclude them from routing metrics, bandit, judge and cost readers. session-end hook v30 (both copies), statusline literal updated. M0-3 gate count not re-measured.
- local model hygiene (N19, N20; docs/bugs/N19.md, docs/bugs/N20.md): `all_ollama_models()` no longer lists an Ollama
  model whose `/api/show` capabilities lack `completion` (nimble:9b returned 400 "does not support generate" on every
  code route; fails open when `/api/show` is unavailable, the classifier still names its model directly). `keep_alive`
  no longer falls back to -1 (pin forever): `warm.edit_keep_alive()` and the proxy default are 600 s; an explicit `-1`
  is still honoured. `llmr-classifier` is also kept out of chains. The default `num_ctx` for qwen3.5/qwen3.8 is 32768 (was
  131072), so it no longer overrides the server's `OLLAMA_CONTEXT_LENGTH`; env overrides unchanged. The proxy off-mode
  golden now records `keep_alive: 600`.
- suffixed Anthropic model ids (HAIKU55-1 follow-up, docs/bugs/HAIKU55-1.md): `pricing.resolve` returned None for
  `claude-haiku-5-5-20260901` and `claude-haiku-5-5[1m]`, so such a proxy row would be labelled but unpriced and fail
  G3. An Anthropic id with an 8-digit date suffix now resolves to its base entry. Exact matches still win. `[1m]` on
  Haiku 5.5 uses its own tiered card. `claude-mythos-5-1` is priced from the pricing page ($10/$50, $0.25 cache read).
  Behaviour change: the proxy tier policy resolves ids through the same function, so a dated id of a configured tier
  model (`claude-opus-5-5-20260901`) now belongs to that tier and is tier-routed, as its `[1m]` spelling already was,
  instead of being forwarded unchanged as `unknown_model`. `pinned_models` matching widens the same way.
- emergency-chain local timeouts (LOCAL-TIMEOUT-1 follow-up, docs/bugs/LOCAL-TIMEOUT-1.md): a local timeout in the
  BUDGET emergency chain now starts or refreshes the cooldown, as it already did in the primary chain.
- ledger (LEDGER-ERR-1, docs/bugs/LEDGER-ERR-1.md): an `llm` call that reached dispatch and failed (all providers timed out, no healthy candidate, wall-clock timeout, cancel) wrote no row with the caller's session id; in the M0-3 rerun (2026-10-10, n=20 calls, 2 sessions) 3 of 20 left none. It now writes one `usage` row (success=0, $0, attempted model, latency, `error_*` reason code), never a second for the same call. Call counts, the statusline mix, routing_report/routing_health and the dashboard call totals leave these rows out. Owner decision 2026-10-10: the same failed calls, and cache-served calls, also write one `routing_decisions` row (`reason_code` `error_*` / `cache_hit`, caller's session id) so the M0-3 gate query, which reads that table, sees every call; every routing-metric reader excludes them (`provider_classes.SQL_REAL_DECISION`). session-end hook v29. The gate count with this change is not yet re-measured.
- local timeout demotion (LOCAL-TIMEOUT-1, docs/bugs/LOCAL-TIMEOUT-1.md): an `ollama/*` model that had just timed out
  led the next route again. In the live ledger (`routing_quality.jsonl`, 2026-10-10 08:34:22Z to 09:30:14Z),
  `ollama/qwen3-coder:30b` timed out at 120 s on 21 of the 21 code routes that tried it. Ollama's scheduler was stuck
  behind a nimble:9b load that could not fit, so only models already loaded answered. Now a local model that times out
  goes to the end of the chain for `LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S` seconds (default 600, `0` = off). It is moved,
  never dropped, and the cooldown is per process.
- session context store across resume (CONTEXT-RESUME-1, docs/bugs/CONTEXT-RESUME-1.md): every `claude --resume` is its
  own SessionStart..SessionEnd under the same session id and SessionEnd deleted the store, so resumed sessions never held
  more than one process's events (live 2026-10-10: 4 and 7 lines at archive after 10 resumed turns each, vs 22 for a
  single process). SessionEnd now moves the store to `projects/<id>/archive/`; SessionStart `source=resume` restores it.
  Archives are swept after 7 days. Hook versions: session-end 28, session-start 33.
- proxy main-thread test (TURNFIRST-2, docs/bugs/TURNFIRST-2.md): a general-purpose sub-agent is sent the `Agent`
  tool, so the launcher-only test labelled its first call a main-thread `turn_first`. In the live ledger after the
  TURNFIRST-1 deploy (2026-10-10 00:15:07Z to 08:17:31Z, n=100 rows) 3 of the 4 `turn_first` rows were such first
  calls. `is_main_thread` now also needs no sub-agent marker: one of the lines Claude Code 2.1.296 puts in every
  sub-agent's system prompt, or the `SubagentHandback` tool. The Haiku arm uses the same rule. P0.9-e and the Haiku
  arm counts must be recomputed on rows written after the deploy.
- proxy turn population (TURNFIRST-1, docs/bugs/TURNFIRST-1.md): `step_class == turn_first` labelled every later
  sub-agent call and notification turn as a human turn; in the live ledger (2026-10-08 17:50 to 10-09 21:05) all 113
  classified turn_first rows were Sonnet requests and 108 (95.6%) classed `code`, while the Opus main loop was never
  classified. New kinds `subagent_turn` and `harness_turn`; `turn_first` is now a main-thread human turn. Every proxy
  row gets text-free `is_main_thread`, `is_first_call`, `turn_origin` and `tier_text_len`. The tier classifier's human
  text strips the shared harness-tag list (`groundtruth_sources.HARNESS_TAGS`) and matches a reminder's close tag at a
  line start, so a quoted close tag no longer leaks the reminder; a notification-only turn keeps the sticky tier. The
  strip is linear (bounded single-line attributes): 1,000 line-start `<command-name foo` lines with no `>` before 3 MB of
  text took 19.3 s in the first draft (one run) and take 0.83 ms (median of n=5, `scripts/bench_harness_strip.py`).
  Labelling is fail-open: an error gives `step_class: unknown`, `step_error: true`, and the call is forwarded unclassified. The
  Haiku arm, `kpi` G1_proxy, O3 and the Haiku guard read the new kinds. The P0.9-e turn-first p95, Haiku arm counts,
  classifier mix and O3 turns must be recomputed on rows written after the deploy.
- statusline first render (P0.9-c repair round 2, docs/bugs/STATUSLINE-COLD-1.md): the first render of a session built the segment
  cache inline (interpreter probe plus build, 80-200 ms; live p95 234 ms, n=47, after #370). It now starts the detached,
  single-flight builder and prints the line with `segments pending`; the cache lands for the next render. `scripts/statusline_wall.py`
  `first` mode now uses a new session per run (it had reused one id and skipped the path), plus a `first_ever` mode.
  Bench, scratch HOME, 6000-line transcript, n=60 per mode: first-render p50/p95 79/82 ms -> 20/25 ms, first_ever 103/108 -> 21/23 ms.
- hook budget judges router time, not delegated Codex time (HOOKMETRIC-1, docs/bugs/HOOKMETRIC-1.md): `codex_delegation` is now one of
  `hook_latency.MODEL_PHASES`, so agent-route's `router_added_ms` excludes the synchronous Codex run and the statusline no longer shows
  `hooks p95 57.8s agent-route` for it; the row-reader fallback and `hook_wall` live clause judge `router_added_ms` too. `elapsed_ms`
  stays reported (`p95_elapsed_ms`). `direct_subagent` / `cli_delegation` are still not subtracted (deferred to 16.1).
- test infra (LINEAGE-PERF-1, docs/bugs/LINEAGE-PERF-1.md): the `LineageStore.record` p95 <= 11 ms budget failed twice on shared
  runners (p95 64.77 and 34.61 ms, n=50, code unchanged) because it measured fsync latency. New deterministic test
  `tests/qa/test_lineage_record_mechanism.py` pins connects, closes, transactions, statements, python fsyncs and the
  synchronous level per `record()` call; the timing test is renamed `test_perf_lineage_record_p50_budget` and asserts p50. No product change.
- hooks fail open on a non-object payload (docs/bugs/HOOKS-FAILOPEN-1.md): `[]`, `"x"` or `123` on stdin made
  agent-depth-release, agent-route, cc-usage-track, enforce-route, subagent-start and usage-refresh exit 1 with a traceback
  (bash-compress and playwright-compress on `123`; usage-refresh also on invalid JSON). They now exit 0 silently, like an
  empty payload. Hook versions: agent-depth-release 6 -> 7, agent-route 18 -> 19, bash-compress 3 -> 4, cc-usage-track 4 -> 5,
  enforce-route 16 -> 17, playwright-compress 3 -> 4, subagent-start 7 -> 8, usage-refresh 4 -> 5.
- one result store (PLAN v16 PG3 / P0.5-c, D-R8-5; docs/bugs/PG3-1.md): the legacy `result_cache`
  (prompt-only key, BM25 "[Relevant prior answers]" injection in `context_prep`) is removed; the
  semantic cache, keyed on (text, context hash, project scope), is the only result store.
  Existing `~/.llm-router/result_cache.db` files are left on disk, unread and unmigrated.
- AGT A.6: `/v1/messages` accepts `system` as a list of text blocks (what Claude Code and the Claude Agent SDK send, with `cache_control`), flattened to the system prompt; it answered HTTP 422 because the field was typed as a string. Found by running the Claude Agent SDK against the gateway. Evidence: `test_anthropic_system_as_text_blocks_is_accepted_and_forwarded` in `tests/test_agt_a2_gateway_clients.py`.
- Auto-route tail latency (hook version 74): a context-dependent prompt re-read and re-parsed every OKF doc of the project in each new hook process (uncached across processes, and outside any recorded phase). Measured in `~/.llm-router/hook_latency.jsonl`(+`.1`), auto-route rows, n=500, 2026-10-17T18:14Z..2026-10-19T12:26Z: the tail came from the real-prompt rows, the bypass rows stayed fast, and it did not depend on load1 (the figures had no result file in the repo and were removed). The OKF bundle now parses front matter with libyaml's `CSafeLoader` (SafeLoader fallback) and `_score` skips docs that contain no keyword stem. `find_relevant` is faster on a cold cache; on a synthetic store built with `okf.index_project` on this repo, parsed concepts and scores were identical before and after. That cold-cache timing has no recorded date or script, so no figure is given here. End-to-end hook timing after the change is not yet measured (the timing lane / CI decides). The lookup is now a named `okf` phase.
- AUTO-PROFILE-OVERWRITE-1: the hourly rescan no longer overwrites a `profile.yaml` the router did not write. `save_profile` now records the file's whole-file sha256 in `profile_hash.json`; the rescan rewrites `profile.yaml` only while it still hashes to that value. An edited file, or any file with no recorded hash (every existing user's), is left byte for byte as it is, the regeneration goes to `profile.generated.yaml`, and one note per change appears in the stop note, `llm-router doctor` and `llm-router map`. `llm-router profile auto` backs the current file up to `profile.yaml.<UTC timestamp>.bak` first. Behaviour change: an existing `profile.yaml` stops auto-refreshing until `llm-router profile auto` is run, so its `quotas` and `routing.model_priority` go stale for their two readers. With an empty discovery cache the profile no longer names five local models nobody observed (EMPTY-ENVIRONMENT-1). Evidence: `tests/test_p015_profile_hash.py` and `tests/test_p015_user_files_untouched.py`.
- BENCH-DRAIN-1: `scripts/bench_shadow_continuation.py` now drains the engine shadow queue before reading the decision ledger and cleaning up the temporary state directory, recording `meta.shadow_drain_ok` and a `meta.shadow_drain_error` on timeout. Before, a late background append to `engine_shadow.jsonl` raced temporary-directory cleanup. Harness errors derived from `BaseException` now exit 2 with an out-file while `KeyboardInterrupt` and `SystemExit` still propagate. Regression tests cover a blocked shadow worker and a custom `BaseException` in the subprocess.
- BENCH-HARNESS-EXIT-1: `scripts/bench_shadow_continuation.py` now exits 2 (and still writes `--out` with `verdict.passed=false` and a `reason`) on a harness error or import failure; exit 1 means only "verdict failed". Before, an uncaught exception also exited 1 with no out-file, so `test_harness_runs_and_refuses_to_pass_a_tiny_run` failed on CI with a bare `FileNotFoundError`. The test now prints stdout/stderr on a bad exit code and gained a loaded-machine case and a harness-error case.
- CI-SUITE-BUDGET-2: the `test` job's pytest budget is raised from 2100s to 3000s (job `timeout-minutes` 45 -> 58). Healthy runs had grown past the old budget, so it failed working suites with "suite exceeded the 2100s budget" on main and on PRs. Nothing in the suite changed.
- Proxy tool classifier: `classify_bash` now splits a Bash command on `|`, `;`, `&&`, `||` and newline, and detects `>`, outside quotes and backslash escapes only. Read-only commands such as `rg "x|y" src` or `grep ">" f` were classed `exec` and are now `technical_op`. Command substitution (`$(`, backtick) is still `exec` inside double quotes; an unbalanced quote or trailing backslash is still `exec`; the read-only allow-lists are unchanged. Comparability note for Lane A: the historical `technical_op` share keyed on `prev_tool_class` is not comparable across this change, because quoted-operator read-only commands move from `exec` to `technical_op`; Lane A Haiku eligibility and the shadow_frontier/offload stats shift accordingly.
- CLS B-A2: `scripts/cls_attach.py` follow-ups to #704, matching the frozen PREREG-CLS-DEV §1.3. `--ge3-label-join`, `--e2-label-join` and `--e1-label-join` now default to `text_sha` (§1.3 (4): labels are keyed by `text_sha`); `key` stays as an explicit opt-in. A label file that joins zero items for its source is refused (exit 2, counts only), and a join share below 50 percent prints a stderr WARNING. Record recovery follows the literal §1.3 (2) rule again: an item missing its session or timestamp takes it from the earliest record with the same `sha256(text)`; the narrowing guards `session_not_taken_from_other_record` and `timestamp_not_taken_from_other_record` are removed and the counters `session_from_record` and `timestamp_from_record` disclose how many items took a value. No frozen script or `docs/prereg` file changed. Evidence: `tests/test_cls_attach.py` (`test_label_join_modes_and_wrong_join`, `test_low_join_share_warns`, `test_recovery_follows_earliest_record_wins`).
- CLS-v16 pre-freeze (D-169): Ollama digest lookups in `cls_pins.py` and B0's C2 embedder treat a bare model name as `<name>:latest`, and record the sorted set of every digest `/api/tags` lists under a name (no last-wins), so `pins --verify` fails when that set changes. C3 inputs are fitted to 512 real ModernBERT tokens with the digest whole and the prompt tail kept (no left truncation); C2's input cap is 384 whitespace words, digest whole, tail kept. C1 unchanged.
- CTX-COMPACT-1: session-context compaction no longer deletes events. Lines dropped when the live `session_context_<sid>.jsonl` is trimmed to its newest 150 (over 300 lines or 256KB), including torn lines, are now appended (fsynced, original order) to `archive/session_context_<sid>.jsonl` before the live file is replaced, so a crash can duplicate a line but never lose one. Records past the persist TTL are still physically deleted (B-02). SessionEnd appends the live file to that archive; resume drops live lines already present in the archive. The live file stays bounded, so hook read cost is unchanged.

  Evidence: `tests/test_session_store_spill.py` (9 tests; 180 writes of 1.3KB each with lowered thresholds, live + archive count equals writes after every call, union equals the written sequence). Owner decision D-57.
- CTX-PROJ-1: one session id now keeps one session-context file. The project hash comes from the session's cwd record (`session_cwd_<sid>.json`), pinned atomically on first use in `session_project_<sid>.json`, instead of the writing process's cwd, so Bash-spawned test/E2E processes and `cd` into a worktree no longer scatter a session's events across several `projects/<hash>/` shards. Precedence: `LLM_ROUTER_PROJECT_ID`, then the session's pin (it outranks a caller-supplied `project_root`, which for MCP/gateway callers is the session's latest cwd), then the caller's `project_root`, then the writer's cwd. When a pin is first created, the session's earlier shards (live and archived) in other project dirs are merged into the pinned project, so a session running across the upgrade keeps its history. `cleanup_old_sessions` now also removes aged `session_project_<sid>.json` / `session_cwd_<sid>.json` files whose session has no log left. The merge locks each source, preserves lines byte-for-byte, and its `*.premerge` leftovers are removed by the same retention sweep; a writer racing the pin retries on the pinned path.
- CTX-PROJ-2: two gaps left by CTX-PROJ-1. (1) A session's first `record_event` could create the pin and run adoption while holding the old shard's own lock, so adoption timed out on that source and the early events stayed stranded until `llm-router sessions merge`; the path is now resolved before the lock is taken, so adoption runs lock-free and the early events land in the pinned shard. (2) `merge_session_shards` without `target=` (the `llm-router sessions merge` path) now holds the target's lock across read and replace, so a concurrent writer's append cannot be lost.
- The engine secret guard now names OpenRouter keys (`sk-or-v1-` + 64 lowercase hex) `openrouter_key` instead of reporting them as `openai_key`; detection and local-only behaviour are unchanged (D-85).
- The opt-in benchmark refresh (`LLM_ROUTER_AUTO_BENCHMARK_FETCH=1`, default off) now downloads only the release-tag catalog whose sha256 matches `src/llm_router/data/catalog_pins.json`; a mismatch is rejected with a one-line log. The default community profile import is pin-checked the same way. The upstream scraper moved to `scripts/bench/benchmark_generator.py` (CI only). Pin refresh: `python scripts/update_catalog_pin.py <tag>`.
- Privacy mode `local` no longer treats Codex or Gemini CLI as on-device: session content is withheld from both (they still receive the request). Only `local` and `ollama` keep session content (D-91, D-95).
- Report executed runtime routes from usage.db over 24h in prompt banners and Stop dashboards; the condensed Stop line shows `N executed (24h)` or `no execution data (24h)`. `llm-router summary` uses its `--since-hours` window (default 24h); `llm_model_usage` labels its counts as classifier hints, and `explain-dashboard` distinguishes both sources.
- Engine doors agree in decide mode (PLAN v16 P1.6-a/-i, D-83): `engine.decide` now computes the class on `engine.canonical_text(text)` (harness blocks and a leading `Caveat:` line removed, last 3,000 chars: the proxy's `steps.tier_text`), so the proxy and the doors that pass the raw prompt get the same Decision; secrets and the cache key still use the raw text. The agent-route and gemini-cli hooks report the engine's complexity verbatim (`deep_reasoning` included) and fold it into `complex` only at their three-row profile/hint tables, so their routing is unchanged. Shadow and off mode route exactly as before (the rules decide).
- MCP tool errors are readable: an unexpected exception from any llm-router MCP tool now reaches the client as `llm-router error: <ExceptionType>: <message>` (secret-scrubbed, plus the models tried when known) instead of the bare `Error executing tool <name>`, while cancellation still propagates; the traceback is logged at DEBUG. Every MCP tool that routes a completion (`llm()`, `llm_route`, the task aliases, `llm_edit`, `llm_text_job`, `llm_router_agent_route`) runs under an overall deadline (`LLM_ROUTER_MCP_DEADLINE_S`, default 90 s, 0 = off). Inside it a local `ollama/` attempt is capped at half the deadline (45 s by default) so a busy Ollama queue falls through to the next candidate without starting the local-timeout cooldown; cloud attempts run until the deadline. An expired deadline returns `llm-router error: deadline <N>s exceeded; tried: <models>; ...`. A cancelled Gemini CLI or Claude Code call now stops its subprocess tree, as Codex calls already did.
- DISC-PROMOTE-1: a newly discovered local model no longer leads cheap chains. A local model (Ollama or OpenAI-compatible) that is not configured (env, `Config`, `routing.yaml` pin) and not grandfathered is now the last entry of a simple query/generate/analyze chain and is absent from every other chain, at the router door (also the proxy `policy_chain`), the hook chain builder (also the SDK and gateway) and the `auto-route` hook's classifier list. Models installed at the first run of an upgrade are grandfathered through `grandfathered_models.json` (written once, empty on a fresh install, never recreated if deleted). Behaviour change: a model you pull after upgrading is not preferred until you configure it. `LLM_ROUTER_PROMOTE_GATE=off` restores the previous chains. Evidence: `tests/test_p015_promote_gate.py` and `tests/test_p015_chain_replay.py`. The router also keeps these models last after the specialist and bandit reorders in `route_and_call`. New state files: `grandfathered_models.json`, `grandfathered_models.init` (marker so a deleted file is not recreated), `profile_hash.json`, `profile.generated.yaml`; `discovery.json` entries gain `task_types_assumed: true`.
- `edit_survival`: git was given a zone-less `--since` (UTC wall-clock digits), which git reads as local time, so on a host east of UTC a survived edit was scored as redo (and west of UTC a redone edit could score as survived). The time now carries an explicit `+0000`. Evidence: `tests/test_edit_survival_utc.py` (UTC, Asia/Jerusalem, America/New_York, Pacific/Kiritimati; 6 of 12 cases fail on the previous code).
- Fix: the background shadow worker writes its row to the log path resolved at enqueue time, and the test suite drains queued shadow work before each test's HOME is restored (main CI 70847288 failed: a queued shadow row wrote the real ~/.llm-router/engine_shadow.jsonl).
- F4 (D-139, D-152): routed answers returned to an MCP caller (`llm()` and its task doors, `llm_route`, `llm_auto`, `llm_codex`, `llm_gemini`) no longer carry tool-call-shaped text the routed model cannot execute: XML function-call / invoke blocks with an arguments shape, tool_use JSON objects and whole-line `Bash(command="...")`-style call lines are removed and replaced by one visible line `[llm-router: removed N tool-call-shaped block(s) the routed model cannot execute]`. Markdown code fences are always kept (a fence holding only a tool call is counted as `fenced_seen`), as are prose, indented code, BPEL-style `name=` elements and single positional calls such as `Write("hello world")`. An unclosed tag never deletes to the end of the answer (an unclosed `invoke` stops at the next blank line). The scan is capped at 256 KB, linear, and fails open on any exception. `llm_edit` pairs, `llm_text_job`, `llm_local_task` and file writes are never touched. Counts go to the explain footer, `LLMResponse.toolcall_blocks_removed` / `toolcall_fenced_seen`, and an append-only record (count, fenced_seen, door, ts; no text) in `toolcall_neutralised.jsonl` under the state directory.

  Evidence: `tests/test_f4_toolcall_text.py` (89 tests; 19 mutants killed).
- The routing hook no longer names an `llm(task=...)` door that no provider on this machine can serve, and so enforcement no longer holds Bash/Edit for it. Before naming a task, auto-route (hook v61) asks the new `llm_router.task_availability` module. The module reads the cached resource map and the API-key names in the process environment (the hook has already loaded the `.env` files into it through `env_loader`; the module opens no `.env` itself), with no network call and no config import. An unservable task falls back to the nearest servable task (research → query → analyze, analyze/generate → query, query → analyze, code → none). If no fallback can be served, the hook names no door and writes no pending state, and records the reason code in the debug log and the routing ledger. If availability is unknown (no map, or a corrupt or stale one), today's directive is kept. User prefixes (P2.7) still win.
- F12 (from the #528 delta review): `scripts/coverage_check.py` routed share counts only real switches (`decision == "served"` or `tier_switch` True). A tier decision that did not switch is a new `decided_kept` bucket (JSON `routed_share.decided_kept`, `decided_kept_share`), so the live config (tiers on, steps off) no longer reads as routed. `not_evidence_for_RRTE2` now fires when off + advise + decided_kept is at least 90% of passed calls. The printed header shows `failopen_explained` (a global explainer within +-120 s). New test pins that advise forwards the client's bytes unchanged (non-default JSON whitespace, key order, non-ASCII). Docs: docs/proxy.md coverage section.
- F16: an unclosed tool-call wrapper tag mentioned mid-line in prose (for example "wraps each call in a tool_call tag; inside is JSON with a "name": key") no longer deletes everything to the end of the answer; only a wrapper that opens at the start of a line runs to the end, otherwise the removal stops at the next blank line. When the 256 KB scan cap or the 64-attempt JSON budget is hit, `CHZ-FO-F4-LIMIT` is recorded and the ledger row carries `limited: true`. The F4 ledger is now written through `capped_log` (1 MB cap).
- CLS-v16 B0 (F20): criterion 8's DEV-SYN leg reads `not_applicable` only when the table has no DEV-SYN rows; with DEV-SYN rows present and the candidate's arm missing from cls_eval's syn leg it is `pending`, which blocks finalist eligibility. An unreachable Ollama during the quiet gate (latency, fit-modernbert, c4-precheck, heavy loops) now parks the run (exit 3, reason `quiet:ollama_unreachable`) instead of crashing it.
- `scripts/check_live_matches_lock.py` exits 2 with a one-line message instead of a traceback when the lock file is missing, malformed or has no packages, or when it runs on Python < 3.11 (no `tomllib`) (F21).
- F22: the Codex chain no longer offers a model the Codex CLI rejects. `policies/standard.yaml` named `codex/gpt-4o-mini` (BUDGET) and `codex/gpt-4o` (BALANCED), so most routed Codex calls burned one HTTP 400 attempt before the next model answered, even with `LLM_ROUTER_CODEX_MODELS` set. Every policy's `codex/*` entry not in `CODEX_MODELS` now becomes `codex/<CODEX_MODELS[0]>` at load time: the `LLM_ROUTER_CODEX_MODELS` choice when set, else `gpt-5.5`.
- F25 (D-170): `llm(task="research")` no longer fails silently when the premium research chain is empty. With no Perplexity key it escalates to PREMIUM only when the premium research chain has a reachable model; otherwise it keeps the default profile (local models still answer, labelled not web-grounded). A research call whose chain is still empty is refused with reason `refused_no_research_provider` and a message naming the empty chain(s), not an `error_exception`; the shared non-decision predicates exclude the new code (session-end hook v34). An empty dynamic routing chain is logged at WARNING once per chain, and a row the MCP ledger guard writes records the profile the router resolved for the call instead of the active profile.
- An empty TEXT routing chain is no longer an error while a model is reachable (F26, D-175): after every filter and injection, the router answers from a last-resort floor (local models first, then Codex/Gemini CLI, then the Claude CLI subscription, never an API key in subscription mode), logs one WARNING per chain and records the floor tier. Non-empty chains are unchanged; media still raises `NoRoutableModelsError`.
- F27: `LLM_ROUTER_CODEX_MODELS` / `CODEX_MODELS` is the documented Codex allow-list, and a rewrite of a non-allowed `codex/<m>` to the first allowed model is logged once per (old, new) pair at INFO. `provider_budget.rank_external_models` (the `external_fallback` at `tools/routing.py`), `auto_profile` and `model_evaluator` no longer offer `codex/o3`, `codex/gpt-4o` or `codex/gpt-5.4` when the allow-list excludes them.
- `scripts/door_agreement.py` sets `LITELLM_LOCAL_MODEL_COST_MAP=True` itself, so the mcp site no longer trips the no-network guard by fetching litellm's cost map (F30).
- F33 (follow-ups from the #558 review): `route_and_stream` now ends after its `route.aborted` event instead of raising a stray `ValueError("Unknown media task type")` (an exhausted chain and an internal error both end cleanly; the abort event is what the caller sees). The zero-dependency `route_server` `/route` refuses an image in `prompt` or the `images` field with HTTP 400 and the gateway's text (a list prompt used to answer 502 and the `images` field was dropped); the image helpers moved to `route_server.py` and `gateway.py` imports them. `docs/empty-chain-behaviour.md` and a comment in `proxy/server.py` now state that a pinned local model with a known small window is forwarded the untrimmed body when the irreducible tokens fit (documentation only). The F14 test comment about the stray error is updated.
- The pinned-model paragraph of docs/empty-chain-behaviour.md now states the capability filter rule exactly: a pin is excluded for missing tools or vision, or when the irreducible tokens exceed 0.9 times the map/registry context window, and a pin with an unknown window always passes the window check. It also names what guards the forward: the native Ollama /api/chat backend checks overflow before each send and falls back to Claude (a `local:`-prefixed step is refused instead, never sent to Claude), a 25,000-token prompt cap and a 32,768 num_ctx minimum apply, and a truncation that slips past the chars/3.04 estimate is recorded only when the heuristic detects it, while the reply is still returned. The image helpers `_has_image` and `_newest_user_turn` are now public (`has_image`, `newest_user_turn`) in route_server, and the gateway imports the public names; nothing else imported the old names, so no aliases are kept.
- F33c: the "What guards the forward instead" part of the pinned-model section of docs/empty-chain-behaviour.md is now a list. It credits the long-log and tool-schema estimator notes to the estimator comment in proxy/local_mode.py, and states that the guard estimate can undercount. It says the per-request guards apply under authority `route`, that under `advise` nothing is served locally, and that the `num_ctx` minimum is checked at startup. It says a `local:` agent turn under `route` is served locally when it can be and is refused, not sent to Claude, when `decide_local` declines it or the local attempt fails, that a Claude pin or a conversation this process did not see start does not make it refused, that the estimate formula is (chars / 3.5) x 1.15, and that check_truncated can also report a truncation on an intact prompt (documentation only).
- F33d: docs/empty-chain-behaviour.md, pinned-model section. The "Per request, under authority `route`" bullet now says that a `local:` agent turn is refused, not sent to Claude, on a context overflow or a prompt over the cap, as the `local:` bullet already said. The sentence that nothing on the `--serve local-agent` pinned path trims or packs the history now names the one opt-in step: with `LLM_ROUTER_PACK_OVERFLOW=1` (off by default) `OllamaBackend.complete` calls `PayloadPack.trim_to`, which trims only the tagged context layers (project, summary, instructions) inside a system-role message or the first message (message 0 can be a user turn), and leaves the other messages and untagged text alone. If the payload still does not fit, it may switch to a local model with a larger `num_ctx`, taken from the static `local_models.NUM_CTX` table with no check that the model is pulled or supports tools (documentation only).
- Flaky test fixed: `test_cached_render_is_byte_identical_to_the_live_computation` failed intermittently because a seeded ledger row could sit on a rolling 24h/7d/14d window edge (`datetime('now', ...)`), so the cached and the live Stop counted it differently ("872 executed" vs "873"). The seeder now keeps rows 30 minutes clear of those edges, and a new test pins that. Test-only; the cache itself is correct.
- Test runs no longer leave detached background processes (`llm_router.cli semantic index`, `llm_router.ollama_watchdog`, judge drain, model discovery) running after the suite: `tests/conftest.py` sets the existing `LLM_ROUTER_NO_DETACHED_SPAWN` opt-out for the whole session, and the session-start hook (stamp 39), the watchdog probe, the semantic auto-index and the semantic refresh child now honour it. `tests/test_f44_no_detached_children.py` guards the hook path.
- F45: `scripts/g1_synthetic_evidence.py` `run_summary` tracks every detached summary worker it starts (`statusline_tick._spawn_detached` now returns the child pid), polls with a deadline instead of a fixed sleep, and before removing its scratch directory waits for the workers (bounded) and stops any survivor (`reap_children`); before, workers that outlived `wait_s` kept running after the script returned. `tests/test_g1_synthetic_evidence.py` fakes the workers, so the evidence test no longer starts ~20 real processes, no longer fails alone on a 30 s timeout, and leaves nothing behind.
- Release eval gate hardening: item values must be real booleans (a string "false" or 0/1 is NOT INFORMATIVE, also checked by `release_evals.py write`), and a committed `release_evals/PIN.json` (written by `release_evals.py pin` at the PREREG-P32 freeze) pins the set list, manifest sha256, n, min_n and the baseline sha; any mismatch, a missing manifest, or manifests without a pin is NOT INFORMATIVE.
- F48: the proxy local gate now fails closed. With capability routing off the request's needs are unknown, so the gate forwards with the new reason `needs_unknown` instead of skipping the capability and window checks and serving. A reply dropped by the loop guard is now logged as `local_repeat` instead of `local_failed`. Tests pin the check order (breaker before promote) and that the proxy never writes `local_eligibility.json`. The gate stays default off.
- No code path or bundled default names a local model any more (D-205): the ensemble classifier (primary/secondary), judge, rolling-summary, ReAct, librarian, `llm_local_task`, direct-executor ranking, `standard.yaml` BUDGET chains and `config/signals.yaml` chains now resolve the user's own model through `model_discovery.local_model()` (`LLM_ROUTER_OLLAMA_MODEL`, else the discovered inventory) or the `ollama/@local` placeholder. With nothing discovered they behave as "no local model" and fail open (`DEFAULT_PRIMARY`/`DEFAULT_SECONDARY`, `llm_local_task.DEFAULT_MODEL` and the family preference lists are removed). A guard test (`tests/test_no_named_local_model_defaults.py`) fails on any new local-model tag literal outside a data allowlist.
- The Stop hook's background ledger-cache refresh (`hooks/session-end.py::_refresh_ledger_cache`) recorded a false `CHZ-FO-LEDGER-CACHE-TMP-REMOVE` fail-open on every successful refresh, because its cleanup `os.remove` of the already-renamed tmp file raised `FileNotFoundError`. A missing tmp file is now the silent normal path; other `OSError`s are still recorded. This also fixes the flaky `tests/test_synthetic_replay.py::test_deterministic_section_is_identical_across_runs`: the detached child finished before or after the ledger read, so `fail_open.jsonl` showed 1 vs 2 rows between two passes (CI runs 39059353893, 39059827131, 39065267213; n=3 failures, source `gh run view --log-failed`).
- F52: four flaky tests made deterministic. The proxy now reads the clock once for `tier_decision_s` and `tier_shadow_schedule_ms`, so the schedule time can never exceed the decision time (two reads let a preemption gap break the invariant). The SDK router overlap test proves overlap with a two-party barrier instead of a wall-clock ratio. The requal judge test (CPU-bound, about 1,500 stubbed calls) and the session-start background-child test (90 s wait inside a 30 s limit) get explicit per-test timeouts.
- Smart enforce mode no longer holds Bash for operational prompts (F59). Short continuation/ambient prompts ("Status?", "keep going", "go on", "continue") no longer context-inherit the prior turn's code/coordination route when `_is_coordination_task` matches them (`hooks/auto-route.py`, hook version 65), and smart mode exempts Bash under the `coordination` task type unless the command is a network fetch or shell-driven LLM call (`hooks/enforce-route.py`, hook version 19). Hard mode is unchanged; genuine Q&A prompts still hold Bash.
- F60: shell-driven LLM calls are no longer exempt from a routing hold. An inline `python -c` / `python -` / heredoc program that imports a provider SDK (openai, anthropic, litellm, google.genai, ollama, ...) or names a provider API host stays route-eligible, and the router's own-CLI exemption (F-1) now covers only config/admin/diagnostic subcommands (`set-enforce`, `doctor`, `status`, ...), not model-calling ones (`run`, `judge`, `benchmark`, `probe`, ...) or unknown words. `python script.py`, `python -m pytest` and `python -c 'print(1)'` stay exempt. Hook version 19 to 20. Evidence: `tests/test_f60_shell_llm_exemptions.py`.
- The local classifier and the decision classifier no longer default to a named model (`llmr-classifier`, `nimble:9b`). The model is `LLM_ROUTER_CLASSIFIER_MODEL` / `LLM_ROUTER_DECISION_MODEL` if set, else the discovered local model, else the classifier is off and the caller's fallback runs (D-205). Set the matching variable in `~/.llm-router/.env` to keep a specific model.
- F63: the PreToolUse Bash exemptions now classify every executed segment of a command, so a model call can no longer ride behind an exempt one while a routing hold is active. The command is split on `;` `&` `|` `&&` `||` and newlines outside quotes, descending into `$(...)`, backticks, `<(...)` and expanding heredoc bodies; `xargs`, `env`, `nohup`, `time`, `nice`, `timeout`, `sudo` and variable assignments (with their flags) are transparent. A command is exempt only if every segment is. Also held now: `python -m openai`, `node -e 'require("openai")'`, and inline python that shells out to `llm-router`. Parsing is bounded (8192 chars, depth 6, 64 segments; over the limit means not exempt). `llm-router set-enforce|doctor|status|install` and the `export LLM_ROUTER_ENFORCE=soft|off` the block message prints stay unheld in every mode. Behaviour change: read-only and local-tool exemptions now need every pipeline stage to qualify (`cat f | python3 x` is no longer read-only). Evidence: `tests/test_f63_exemption_nesting.py`.
- Auto-route hook p95 tail (F64, acc.NFR_auto_route): the transcript tail read now scans each byte once, skips tool_result/tool_use/image lines before the JSON parse and stops at a cap (default 32 MiB, `LLM_ROUTER_TRANSCRIPT_SCAN_CAP_MB`, 1-1024) (`session_io` mean 207 ms to 9.6 ms on a synthetic 100 MB tool-heavy transcript, n=30 per side; cell `bigtool-5000` of `scripts/bench_auto_route_tail.py`, run for PR #589, opened 2026-10-18); the scoped zero-Claude edit pre-flight no longer probes `/api/tags` for model discovery, asks `/api/tags` and `/api/ps` concurrently with the git status, and runs the original and candidate ruff lints side by side (`zce` mean 1338 ms to 539 ms against a synthetic busy Ollama, n=30 per side; cell `edit-busy` of `scripts/bench_auto_route_tail.py`, same run). Hook version 66; `phases_ms` names and `router_added_ms` semantics unchanged.
- `time case ...` / `time -p case ...`: the case arm bodies are now classified like any other case (the `time` prefix no longer hides them from the Bash exemption checks). (F66)
- Stop hook (session-end) follow-ups from the PR #587 review: a stale ledger cache from the same local day now supplies the executed-routes count instead of a live call (live only when the cache is missing, corrupt, or from another day); `last_summary.ansi` is written atomically (tmp file + `os.replace`); the background-child spawn honours `LLM_ROUTER_NO_DETACHED_SPAWN`; disabled mode no longer builds the cheap box or runs the live fallback.
- The F68 statusline timing test no longer races the detached segment builder on a loaded runner (main CI run 39124655224); test-only change.
- The statusline's sampled timing row no longer starts a clock process: start and end are each a shell redirect and `hook_latency record-stamps` reads the two file mtimes, so two `perl` start-ups (most of the live p95 under machine load) are out of both the number and the render. The row now carries `phases_ms` naming the render path (`cache_hit`, `cache_stale`, `cache_miss`, `fast`). The printed line is unchanged.
- Short acknowledgement / owner-action-report prompts ("ok", "done", "yeah I reconnected the mcp", "I ran the command", "copied the hooks", "yes, go ahead", "thanks") no longer context-inherit a routable Q&A class and no longer hold Bash in smart mode (F69). New `_is_acknowledgement` in `hooks/auto-route.py` (hook version 66, max 10 words, no `?`, no interrogative/new-request word, past-tense report) feeds `_is_coordination_task`, reusing the F59 inherit-skip and smart-mode coordination exemption. Follow-up questions ("why?", "what about X") and hard mode are unchanged. Command-shaped prompts (first token a known executable, no prose words, stray backticks tolerated; e.g. `llm-router summary`` ) take the same path via `_is_command_shaped`; questions that merely mention a command still route.
- The auto-route hook no longer treats a real request behind an acknowledgement or a short report as coordination (F71): prompts such as "ok add a docstring to foo", "done, review the diff", "ran pytest, 3 failures, fix them", "looks good, ship it", "Set it to prod" and "Proceed with the plan" were swallowed by the older ambient paths of `_is_coordination_task` (an ack-style opener whose remainder merely lacked a known task verb, and a bare "it"/"that" with no commit/push) and so were never routed and never held Bash. Both paths are now whole-prompt allow-lists over a closed vocabulary of function words, fillers and continuation words, the same shape F69 gave `_is_acknowledgement`, so any content verb or noun falls through to normal classification; pure acknowledgements, owner-action reports and the existing status/continuation prompts still count as coordination. Hook version 68.
- The auto-route hook no longer lets coordination shortcuts swallow work requests (F75): "ok thanks, what's next? add tests" was claimed by an unanchored status regex on "what's next", and "ok thanks. now deploy it", "deploy it now" and "ok merge it" by an unanchored bare-pronoun operational-verb regex. The status shape now applies only when every clause of the prompt is itself a status shape or pure filler, and "merge/deploy it" stays coordination only as a whole-prompt deferred instruction ("merge it once the checks are green", "deploy it and let me know when it's live") built from a closed vocabulary; everything else falls through to normal classification. F74: a prompt that is only a URL into the current checkout's own repo or CI (an Actions run or job, pull, issue, commit, tree or blob URL whose owner/repo matches a remote read from .git/config, with no network call) is now classed as coordination instead of research that held a read-only `gh run view`; third-party URLs and URLs with any other words keep normal routing. F69 residue: failure-status fragments ("Failed in CI", "Crashed on prod", "Blocked on CI") and the base-form imperatives shred and shed are no longer accepted as owner-action acknowledgements. Hook version 69.
- Gateway streaming and errors: a failed route sends the client only the exception class name and a fixed message (`routing failed` / `upstream rate limit` / `stream failed`), never provider error text, which can echo the prompt; the scrubbed detail is logged locally (`llm_router.gateway`). Behaviour change: HTTP 429 / `rate_limit_error` (all three wire formats, stream and non-stream) is now returned only when the exception is itself a provider `RateLimitError` (type check through `__cause__`/`__context__`), with `Retry-After` when the error carries one; the router's aggregate "All models failed" error, budget errors and "quota exceeded" stay 502 as before. The examples' socket guard also covers `connect_ex` and child processes get dead proxy variables; `system` block handling and the offline limit are documented.
- The auto-route hook no longer lets the last coordination shortcuts swallow work requests (F78) and treats a pasted shell escape as operational (F82). The bare status shapes ("what's next", "what's left", "next steps", "so far") must now be the whole clause, so the unpunctuated "what's next add tests" / "what's next fix it" / "what's next and fix bug in parser" route normally; "push the fix and deploy" and similar chains are no longer claimed, since the multi-step shape is now a whole-prompt allow-list of an operation on an unresolved pronoun ("merge them and redeploy", "roll it out and report back"); "keep going and fix the lint errors" no longer counts as a bare continuation because the words after the phrase must be filler too; the own-repo URL host check is case-insensitive. A prompt that is a shell command behind an optional leading space and "!" (" ! kill 65369", "!git status", "  ! ls -la") is command-shaped, with push/merge/deploy/rm-style commands still routed as before. "continue the plan", "continue with the plan" and "carry on with the plan" are now continuation instead of query/simple that held read-only Bash. Hook version 70.
- Last coordination-classification residue in the auto-route hook (F83, hook version 71). "Proceed with the plan" (and work/queue/backlog) is now coordination like "continue the plan" (D-222; the F71 test pin moved from must-route to stays-coordination). The `!` shell-escape list no longer holds bash/sh/source, and a pipe into a shell or interpreter (`| sh`, `| bash`, `| zsh`, `| python`, ...) is never command-shaped, bare or behind `!`, so `curl ... | sh` no longer exempts a whole turn. Subject-bearing status shapes no longer swallow a trailing request ("status of X add tests", "did you push the fix the tests", "is it done now fix the bug"), and the text before a mid-sentence "keep going" is an allow-list too ("fix the bug and keep going" routes; "merge it when green and keep going" is still coordination). On 86 invented/briefed cases in tests/test_f83.py, 34 behave differently from the previous hook (origin/main 00befb23).
- Remaining client-facing exception echoes (same rule as the gateway's wire endpoints): the native `POST /route` and `POST /ground` on the gateway, and the zero-dep `route_server` (`/route`, `/feedback`, bad JSON) now send the client only the exception class name and a fixed message (`<Class>: routing failed`, `<Class>: grounding check failed`, `<Class>: feedback failed`, `<Class>: bad json`), never provider error text, which can echo the prompt. The secret-scrubbed detail goes to the local `llm_router.gateway` log only. Gateway `POST /route` now also returns 429 with `Retry-After` for a provider `RateLimitError`, as the `/v1` endpoints do. 400 messages for the caller's own invalid request (written by the router) are unchanged.
- Last status/pipe residue in the auto-route hook (F89, hook version 72). "status of/on X" is now an allow-list (one short noun phrase, nothing after it but "?" or a closing politeness token) instead of a request-verb deny-list, so "status of X push it", "status of X ship it", "status of the deploy run the tests" and similar route; pure status questions ("status of X", "what's the status of the build", "status of the tests") stay coordination. A pipe, `&&` or `;` into a shell or interpreter is never command-shaped even behind a wrapper (`| sudo -E bash`, `| sudo -u root sh`, `| doas sh`, `| /bin/busybox sh`, `&& bash f`, `; sh f`); plain commands (`!git status`, `ls | grep foo`) are unchanged. No existing test expectation changed.
- Gateway and zero-dep route server: a 400 echoes a `ValueError` only when the router wrote it (new `RouterInputError`); other `ValueError`s answer `<Class>: invalid request`. Error logs now hold the exception class and text length, not the text (it can contain the prompt). Added a test for gateway `POST /route` 429 + `Retry-After`.
- Auto-route hook version 73 (F96): a pipe into a shell behind more wrappers (`ionice`, `systemd-run`, `flock`, `runuser`, `pkexec`, `unshare`, `nsenter`) is never command-shaped, and "status of X rerun/retry/rebuild/redeploy" now routes instead of being skipped as coordination. Ambiguous nouns (cleanup, polish, tune) and pure status questions are unchanged. `test_f89` now checks that the hook stamp is at least 72 instead of exactly 72.
- F97: follow-ups from the P3.1-4 review (#619). Judge queue rows written before the hygiene change are run through `judge_hygiene.prepare` at drain and dropped (counted, no text) when it refuses them; new rows carry a `prepared` marker. Orphaned `<queue>.rewrite.*` and `<queue>.draining.*` temp files are purged at drain and at enqueue, and the enqueue-time purge now also runs when the entry cap is 0. Haiku caps are admitted atomically (lock plus a reservation, released after the call), unknown token use counts the pre-call estimate toward the daily token cap, and the judge day is the local day (D-218). `no_proxy_env` is documented as drain-process only. Evidence: `tests/test_f97_judge_followups.py`.
- Session-end follow-ups from the one-ledger review (hook v42). User-visible number changes:
  - A seat older than 24 h (`seats.STALE_AFTER_SECONDS`, the age `Seats.is_stale` and the ledger `sources` already judge) no longer classes a provider as subscription or paid: those calls are `unclassified` until the seats are re-detected. Affects the `Effective` / `Escalated` figures, the `mix` of every ledger window line (the unclassified share grows, `subscription` / `paid` shrink), and the session free panel (below).
  - Stop box `Effective` and `Escalated` print `(N unclassified)` next to the figure when some decisions could not be classified, and `n/a (N unclassified)` when none could (they used to be left out silently, or `Effective` vanished).
  - Session panels are split by discovered class, not by a provider-name list: `lm_studio`, `vllm`, `llamacpp` rows now land in the free panel (were paid); a provider covered by a fresh seat (e.g. `gemini_cli` with a Google seat, `anthropic` with a Claude seat) is in the free panel (an `anthropic` seat row was paid); with no fresh seat `gemini_cli` is paid-panel, like any unclassified provider. The paid panel keeps unclassified providers and prints their recorded cost. Codex stays in its own panel.
  - `N% cache hit (n=N)` now reads `(n=N, today)`; the figure comes from the shared ledger view.
- F101: judge drains no longer perturb a running benchmark. Session start and session end now share one guard (`judge_view.drain_due`): no drain while the `bench.active` marker exists, when the queue is empty, when `LLM_ROUTER_JUDGE_AUTODRAIN=0`, or within 30 minutes of the last drain started by either trigger (one shared claim file); the session-start drain is now niced like the session-end one (session-start hook v41; session-end unchanged). `judge.drain_queue` itself returns `deferred="benchmark active"` without a model call while the marker exists, and `llm-router judge drain --force` overrides it. The kpi judge line shows status `failed` as "not graded". Evidence: `tests/test_f101_drain_bench_guard.py`.
- Session-end Net line: providers whose class is unclassified (no fresh seat, not local, not known paid) are no longer summed into the paid figure; they print as their own `unclassified n=N this session, $X recorded, not in paid` fragment (`cost n/a` when none is recorded), and are outside the baseline and the n. Hook v44. Evidence: `tests/test_f102_unclassified_net.py`.
- **Verified savings are labelled as savings, not "measured" (F103).** The ledger's `verified_usd` is a verified counterfactual saving against the baseline, not metered spend, so "measured" read as money spent. User-visible label changes:
  - `Summary.compact()` / statusline `money` + `money_m` segments / session-end today and lifetime fragments: `~$X est` becomes `~$X est. saved`; `$Y measured` becomes `$Y verified saved` (statusline `💰 ~$X est. saved·today n=N`, `💵 $Y verified saved·today n=M`).
  - `Summary.display()` (`llm-router status`, `savings-report`, `gain`, MCP `llm_router_status` gain view, `kpi` O1): no longer one merged sum. It prints `est. saved $X (n=N) · $Y verified saved (n=M) vs <baseline>`; a segment with no rows is left out; with neither, `est. saved $0.00 (n=0) vs <baseline>`. The `kpi` O1 `estimated_usd` field is now the estimate only, with `verified_usd` beside it.
  - `units.render`: a `work_hours` figure without its rate n prints `n/a`.
  - A negative, zero or infinite `LLM_ROUTER_WEEKLY_QUOTA_USD` no longer masks a valid `LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH`.
  - `llm_quota_saved` with no plan value names all three variables: `LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV`, `LLM_ROUTER_WEEKLY_QUOTA_USD`, `LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH`.
  - session-end hook v46 (stale "merges both into ONE estimate" comments corrected). The `ledger_view` ledger line `est. saved $X (n=N)` is unchanged.
- The P2.0 benchmark harness (`scripts/p20_requal.py`, `scripts/p20_code_runner.py`) no longer gives coding agents the owner's real HOME. Each Claude `claude -p` and Pi agent attempt (and the Claude Q&A CLI call) runs with HOME set to a fresh per-attempt scratch directory under the run dir and an allow-listed environment (PATH, LANG, LC_ALL, USER, LOGNAME, plus `CLAUDE_CODE_OAUTH_TOKEN` or a `P20_CLAUDE_CREDENTIALS_FILE` copied into the scratch HOME for Claude). `LLM_ROUTER_*` (except `LLM_ROUTER_SESSION_KIND=research` on Claude calls), `ANTHROPIC_*` and every other variable are dropped, and the harness refuses to launch an agent whose HOME is the real HOME or whose environment has an unlisted key. Incidents F86 (2026-10-18) and F86-2 (2026-10-19): agents ran an `llm-router uninstall`-shaped deletion of the live install. Supersedes the "real HOME (auth only)" line of PREREG-P20-requal section 6; a new prereg amendment must reference F104.
- F105: the session-end Routing panel no longer counts unclassified rows in calls / actual / baseline / % saved, and an unclassified row with no recorded cost is no longer summed as $0: the panel adds an "unclassified n=N, $X recorded (k of n); not in the figures above" line (`cost n/a` when none recorded). The Net line shows partial costs as `$X recorded (k of n)` and says `no paid calls` for zero paid rows with or without unclassified rows (it printed `paid n/a` vs `$0.0000 paid`). Stale hook-pair test names renamed. Hook v48. Evidence: `tests/test_f102_unclassified_net.py` (F105 block).
- The router no longer sends a local Ollama model the lanes it measurably failed: `route_and_call` drops an `ollama/*` entry when `local_eligibility.json` records a powered `failed` verdict for that model on the call's (task type, tier) lane (D-235). Q&A task types map to the `qa` lane and `code` to the `code` lane, at simple and moderate tiers only (the call's own complexity, also when `llm_route` passes an explicit profile); unknown, probe, not-informative, inconclusive and eligible lanes, a missing file and a corrupt file (fail-open event `CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE`) leave the chain unchanged. Each drop is recorded as `failed_eligibility` with lane, k, n and source, and `llm-router why` shows it. The file read is cached by mtime. Opt out with `LLM_ROUTER_IGNORE_ELIGIBILITY=1`.
- F107: `quota_history.jsonl` is bounded. It grew without limit (about 288 rows a day since P35-2 writes it from the default layout too) and `quota_rate` read the whole file on every refresher run. Now `statusline_tick.prune_history` keeps rows newer than 30 days (longest reader window is 14 days) and at most 20,000 rows, rewriting the file atomically under the writers' slot lock, at most once a day, only from the background refresher (never the synchronous tick); a failed prune is recorded as `F107-HISTORY-PRUNE`. `quota_rate` scans the file backward and stops at the first row older than its window. Retention is documented in `docs/RETENTION.md`. `quota_rate` read time is now bounded by its window rather than by the file's whole history (the synthetic before/after timing has no recorded date or script, so no figure is given here). The bound keeps the file near 30 days (about 8,640 rows).
   `quota_rate.bad_rows` now counts only the bad lines in the tail it actually read, not in the whole file. A leftover `quota_history.jsonl.<pid>.tmp` older than a day is removed at the next prune.
- The F106 failed-eligibility filter now also covers the hook chain builder (`build_chain`: auto-route DIRECT, agent-route and the Python SDK) and `route_and_stream` (TUI), so a local model with a powered `failed` lane verdict no longer leads CODE (or SDK Q&A) chains at simple and moderate complexity there either (D-235). Same function, lane-tier rule, `failed_eligibility` reason, `LLM_ROUTER_IGNORE_ELIGIBILITY=1` opt-out and fail-open as F106; `model_override` stays exempt. Hook cost is the cached file `stat`. `route_and_stream`'s `route.started` event gains `eligibility_skipped`.
- Eligibility filtering now also (F109): logs the model ids it drops on the hook chain builder at debug level (no prompt text); applies the D-116 Q&A local reorder in `route_and_stream` before the eligibility filter, as `route_and_call` does; and matches local models under any local provider the router already detects (`qa_policy.QA_STRIP_PROVIDERS`: lm_studio, vllm, llamacpp, openai_compat as well as ollama), not only `ollama`. Lanes graded unknown are still never treated as failed, and `LLM_ROUTER_IGNORE_ELIGIBILITY` still turns the filter off. Evidence: `tests/test_f108_eligibility_other_doors.py` (F109 section).
- Benchmark coding agents now run with an allow-listed PATH that contains no llm-router executable, and the agent guard refuses a HOME inside the real HOME outside the run dir (F110, follow-up to F104).
- F111 (follow-ups to P35-3): the Codex Stop hook (`codex-stop.py`, hook version 2 -> 3) and the Pi summary extension now find the state home the way llm-router does: `LLM_ROUTER_HOME` from the real environment, else from the first of `~/.llm-router/.env` and `~/.env` that has the key. The hook and Pi parse those files exactly as `env_loader` does (`export KEY=` is not understood, an empty value counts as set), and the hook reads `LLM_ROUTER_STOP_HOOK` from the same files the package does, so an off switch in `~/.llm-router/.env` still works when it also sets a different home. A user who set it only in the default-home `.env` no longer gets a permanent `n/a` while the refresher wrote to the configured home. Both read that one key by hand (no `llm_router` import: latency). The refresh rate limit is now per session (one small stamp file per session in `.codex-stop-refresh.d/` / `.pi-summary-refresh.d/`, stamps older than 1 h removed, at most 64 kept), so a second session within 10 s is no longer starved. The old single stamp file is left unused. `codex-stop` now writes its own G1 row to `hook_latency.jsonl` (hook, event, elapsed_ms, timed_out, ts, session_id, host, load1; same shape as `hook_latency.record`; skipped when `LLM_ROUTER_HOOK_LATENCY` is off or the log is over its cap; the hook does not rotate, so if it is the only writer in a home its rows stop at the cap). Install the new copy with the usual hook redeploy.
- Jobs whose output is gated by a mechanical acceptance check with a fallback are now exempt from the F106/F108 failed-lane eligibility filter (D-242, owner D-240): `llm_text_job` (`check_output` + fallback, `qa` lane) and `llm_edit` (validated old/new pairs, `code` lane) pass an internal, keyword-only `eligibility_exempt_reason` to `route_and_call` ("verified_text_job" / "validated_edit"; not an MCP parameter). The chain is then unchanged, in the primary and the BUDGET emergency chain, and each model that would have been dropped is recorded as `eligibility_bypassed` (lane, source = the caller id; no prompt text), which counts as a build-time reason, not a loop skip. `ledger_view.local_by_cause` no longer counts a bypass-only `skipped_json` as `other_skip`. Plain callers, `route_and_stream` and the hook chain builder are unchanged. Evidence: `tests/test_f112_verified_jobs_exempt.py`.
- F113: the benchmark agent guard refuses when any `python`/`python3` on the agent PATH can import `llm_router`, agents get `PYTHONNOUSERSITE=1`, and the hidden-test grader runs on the agent PATH with an interpreter that cannot import `llm_router`. `P20_CLAUDE_CREDENTIALS_FILE` now supplies `CLAUDE_CODE_OAUTH_TOKEN` to the Claude child only (file must be 0600, value never logged or written). Adds `scripts/p20_prefreeze_smoke.py`. Evidence: `test_import_guard_refuses_python_that_imports_llm_router` and `test_credentials_file_refused_when_group_or_world_accessible` in `tests/test_p20_requal.py`.
- F114: the session-end compact money line gives the estimated and the verified saving each its own n (`~$5.00 est. saved (n=4, today) · $2.00 verified saved (n=2, today)`) instead of one merged n, and an unsplit ledger prints `est. saved n/a` instead of the merged total as "est. saved" (D-247). `llm-router summary --session ID` sanitises the id to `[A-Za-z0-9._-]` (an ESC was echoed raw), prints `no rows for session ID` for an unknown id, shows the targets line as n/a at n=0, and `--watch` with `--session` is now an error (exit 2) instead of silently ignored. Hook v48. Evidence: `tests/test_p35_session_report.py` (F114 block), `tests/test_p34b1_ledger_contract.py`.
- F115: benchmark agent sandbox follow-ups to F113 (`scripts/p20_requal.py`, `scripts/p20_code_runner.py`, `scripts/p20_prefreeze_smoke.py`). The import guard now fails closed (only a probe exit 0 passes; exit 1/2/126/127, signals, timeouts and OSError refuse) and probes every `python*` / `pypy*` on the agent PATH. A hung grader pytest probe refuses instead of scoring an agent failure, `grader_python` runs as a preflight before any agent and is a smoke check. The Claude coding arm's OAuth token is scanned for (in memory, never printed) in agent output, work files, scratch HOME and the patch before anything is stored; a hit refuses the run. Evidence: `tests/test_p20_requal.py`, `tests/test_p20_code_runner.py`, `tests/test_p20_prefreeze_smoke.py`; details and limits in `docs/prereg/F104-AGENT-SCRATCH-HOME.md`. Repair 1 (review of #651): the token scan no longer hangs or over-reads on an agent-made FIFO, `/dev/zero` symlink or outside symlink (lstat, O_NOFOLLOW|O_NONBLOCK, size/time caps, refuse by kind); the scrub also runs after a timed-out Claude call and when the token is missing; interpreters like `python3.13t` are probed; a hit deletes the work/scratch-HOME copies inside the run dir and tells the owner to purge the run dir and rotate the token; the smoke `--claude-call` scans its scratch HOME.
- F116: session-end follow-ups from the F105/F114 review (D-247: estimated and verified savings are never one number; unknown prints n/a, never 0). The fallback cumulative Savings panel and the Rich dashboard's SAVINGS block no longer print one unlabelled "~$X lifetime / today": each period shows `$X verified (n=N) · ~$Y est. (n=M)`, and a period the ledger cannot split prints `verified n/a · est. n/a` (the 14-day projection extrapolates the verified figure only and says so). `llm-router summary --session ID` now refuses an id outside `[A-Za-z0-9._-]` (exit 2, "invalid session id") instead of rewriting bad characters to `_` (so `abc def` no longer names the session `abc_def`), and `--session ""` is refused instead of falling back to the current session. A paid row with no recorded `cost_usd` is no longer summed as $0 in the Routing panel or the Net line: they show `$X actual recorded (k of n)` (or `cost n/a` / `Net saved n/a`), and % saved is taken over the costed rows only. The Routing panel's unclassified line now shows for a session with no classified calls, and the Stop line no longer prints `(today)` twice for `est. saved n/a`. Hook v49. Evidence: `tests/test_f102_unclassified_net.py` (F116 block), `tests/test_p35_session_report.py`, `tests/test_session_report.py`, `tests/test_p34b4b_session_end_scans.py`.
- F117: benchmark harness git and token-scan hardening (`scripts/p20_requal.py`, `scripts/p20_code_runner.py`). The token scan refuses on an unreadable (chmod 000) directory instead of skipping it, and the purge opens such directories through directory fds (O_NOFOLLOW, chmod by fd) so a symlink swapped in by a live agent cannot redirect the chmod. Harness git on an agent-touched repo reads bytes with a 128 MiB cap and a 120 s timeout and never reads the agent's `.git/config`: it runs against a harness-written git dir with no system/global config, scratch HOME, `GIT_NO_REPLACE_OBJECTS=1`, `--no-textconv --no-ext-diff` and overrides for fsmonitor, hooksPath, external diff, sshCommand, pager, gpg and related keys. **Behaviour change: every git failure (non-UTF-8 aside, which is scanned) such as a broken or deleted `.git`, a timeout or the output cap is a `Refuse` that aborts the whole run, where it used to be a `runner_error` row; this applies to the Pi arms too (through `empty_patch`).** The Claude-arm history scrub now scans the raw content of git objects the agent added (object ids snapshotted before the agent runs), instead of `git log -p --all` over the whole shared mirror, which exceeded the cap on every noncloud attempt; it also covers merges, tags, unreachable commits and replace refs. The Claude agent runs in its own process group; before any scan the harness kills that group while its leader is unreaped and every descendant it recorded (pid plus start time, re-verified before each signal; nothing is matched by environment or argv, an unverifiable pid is never signalled), drains the pipes for at most 30 s, and then **refuses the run (`agent process survived`, tree not scanned) if any other process still holds the work dir or scratch HOME**. The purge is iterative (a deep directory chain no longer raises `RecursionError` and leaves the token). The object scan checks each agent-added object's declared size first (`--batch-check`, 64 MiB per object) and runs git under `RLIMIT_AS` where enforced. Storage git cannot enumerate (stray files in `objects/`, packs without an index) is listed as a limit. `worktree remove` runs from the trusted clone. The interpreter probe no longer matches `pythonw` / `pythonista`. Evidence: `tests/test_p20_requal.py`, `tests/test_p20_code_runner.py`; limits in `docs/prereg/F104-AGENT-SCRATCH-HOME.md`.
- P3.7 follow-ups (F118). `kpi --quota-burn` anchors the baseline week on the first organic row of `quota_samples.jsonl` (synthetic/benchmark rows no longer move it; the 30-day prune of `quota_history.jsonl` no longer moves it), labels it "(assumed: first organic row)" and adds `baseline_meta` to the JSON; a baseline week that is incomplete or has fewer than 10 measured sessions is flagged "partial baseline" (ratio still printed, with n). The S7 marker `mark_active` now costs one `stat` per session and does its read and write at most once a day (docs no longer say "one tail read per session"); `active_on_day14` reads "not yet" through day 14 itself; concurrent first writers share one install id (temp file + atomic `os.link`, never observed empty; where hard links fail, e.g. FAT/SMB, the failure is recorded and an O_EXCL create is used, so the row is still written; orphan `active_days.id.*.tmp` files older than an hour are swept), same-day duplicate rows are collapsed, the trim threshold matches a full file, and existing files are forced to 0600 where `os.fchmod` exists (not on Windows, where the append still succeeds). No hook change.
- F119 / D-247: every user-visible savings surface now keeps the estimated and the verified saving apart, labels each, and prints unknown as `n/a` instead of `0`. Stop hook (v50): the MODELS panel and the complexity breakdown no longer sum an unknown cost as $0 (`n/a`, or "k of n" when only some rows recorded one), the free-model panel prints `n/a` for an unknown token volume, and the net / gross / routing-saved figures are labelled `est.`; the Rich dashboard prints `n/a` for a window the ledger could not read (not `$0.00`), shows est.-only periods, labels the 14-day chart footer and chart `verified`, and puts the sign before the `$` (`-$1.00`). The status bar (v10) prints `~$X est n=N · $Y ver n=M` per window instead of one unlabelled merged sum. The share card, retrospective, snapshot, replay, digest, `gain`, `savings-report`, the admin tools, the web and terminal dashboards, the TUI sparkline, the surface status line, the cyber-grid and claw-code (v5) summaries label their figure `est.` or `verified` and print `n/a` for unknown; claw-code no longer invents token counts for a free provider that reported none. Evidence: `tests/test_f119_d247_savings_surfaces.py` renders each surface from a fixture with known verified, est. and unknown values and asserts no merged sum, the labels, and `n/a`.
- F120 (active_days orphan-tmp sweep): only the exact `active_days.id.<pid>.<hex>.tmp` name form is removed (`active_days.id.backup.tmp` is no longer deleted), age is judged with `lstat`, and a dangling symlink of that name is now removed (it was logged every day and never deleted); the link is never followed. Evidence: `tests/test_p37_burn_retention.py::test_sweep_ignores_lookalike_names_and_removes_dangling_symlinks`.
- F121 / D-247 leftovers. `llm-router share` no longer counts a free (ollama/codex/gemini_cli) row twice (the saving on the card was overstated by the baseline of every free row). `/metrics` omits `llm_router_savings_usd_total` and `llm_router_external_usd_total` (Prometheus convention for unknown, plus a `CHZ-FO-METRICS-LEDGER` fail-open record) when the usage ledger cannot be read, instead of emitting 0; `llm_router_external_usd_total` now reports real paid spend (it read a key that never existed and was always 0). `get_savings_by_period` leaves a paid row with no recorded cost out of calls, baseline and saving (new `unpriced_calls`, `unreadable` keys; it booked the full baseline as saved). Cost totals in the retrospective (`total_cost` is the recorded sum or `None`, new `cost_n`), `digest`, `gain` and `explain-dashboard` print the known sum with "(k of n recorded)" or `n/a`, never `$0` for an unknown. The quota-saved metric (`quota_savings`, `llm_quota_saved`, routing notice) keeps an unreadable ledger as `None` / `n/a` and labels the figure `est.`. Removed the merged `Summary.estimated_usd` / `estimated_n`, `ledger_view.saved_usd()` and `LedgerWindow.figures()["saved"]`; `LedgerWindow.saved_usd` / `saved_n` stay in the v2 JSON, marked deprecated. The `kpi` O1 too-few-rows gate now uses the larger of the estimated and verified segment n (never their sum). `explain-dashboard` prints the estimated and the verified saving apart. `team` broadcasts, `demo`, the canonical subscription headline and agentic delegation savings label their figure `est.`; `demo` shows a paid row with no cost as `n/a`. Evidence: `tests/test_f119_d247_savings_surfaces.py` (F121 section).
- F122 (benchmark harness `scripts/p20_requal.py`, `scripts/p20_code_runner.py`, follow-ups to F117): `stop_agent_processes` now raises `AgentSurvived` when the unreaped agent leader's row is missing from the process table, instead of returning success without a group kill; the message names the surviving pids (pids only). The agent purge has a 120 s budget (the refusal text says when it was cut short) and never descends into a directory on another filesystem (`st_dev`); the captured agent output keeps only its last 64 MiB per stream, but every byte is scanned for the token as it is read (overlap across chunks and the cut), so a token in the dropped part, or any cut, refuses with purge and rotate ("token found in agent output" / "agent output over cap"), never a silent `claude_error`. `agent()` keeps the surviving pids in its `AgentSurvived` message. A purge that runs out of time still attempts the remaining paths and names those not fully deleted. Not detected: a Linux bind mount of the same filesystem (same `st_dev`). New tests: the `_walk_tree` `..` identity check (a subdirectory renamed out of the root during the walk; the check disabled deletes an outside file, so the test fails then), the `st_dev` guard, the missing leader row (emulated), pids in the message, the purge budget and the capture cap. `docs/prereg/F104-AGENT-SCRATCH-HOME.md` documents the lsof false positives (Spotlight, the owner's shell) and that the `/proc` branch does not see mmap-only holders. Evidence: `tests/test_p20_code_runner.py`.
- F123 (P3.6 follow-ups): `learn --purge-user-examples` now replaces `complexity_knn.json.prev` with a copy of the purged index, so no user-derived embedding survives there (documented in `docs/RETENTION.md` and `learn --help`); `learn` waits at most 60 s for the artifact lock and then exits 2 with a message, and uses the cross-platform `file_lock` (`msvcrt` on Windows) instead of a function-level `fcntl` import; `examples.jsonl` prune, trim and append share one lock (the hook and proxy/MCP inline writes wait at most 0.25 s, then drop the row and record `LR-FO-PREFIX-EXAMPLE`) so a rewrite cannot drop a concurrent append; the deferred embed queue holds at most 32 jobs and drops the rest with failopen code `LR-FO-EXAMPLE-QUEUE`. The hook/MCP conservative bias remains a documented follow-up. Evidence: `tests/test_p36_classifier_learning.py` (purge .prev, lock timeout, Windows lock path, deterministic prune+append race, hot-path lock bound, bounded queue).
- Test guard no longer blames tests for a live Codex rewriting ~/.codex/config.toml; it diffs only llm_router's own slice (FLAKE-LAV-1).
- FU-F1/F2 (follow-ups from the #521 review): importing `llm_router.gateway` no longer sets `LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS=codex,gemini_cli` for the host process (the first `llm_router.route()` call used to disable codex/gemini_cli subprocess backends host-wide); the default is now set in the gateway's `main()` only, so the gateway service behaves as before. QUOTA_BALANCED comments in `profiles.py` now name `resource_map.order_chain`/`score` (P2.3); `llm_quota_status` labels its order as the legacy quota_balance order; `_quota_score_chain` logs its explain list at DEBUG instead of INFO. Code for these edits was written by the local model qwen3-coder:30b under D-126 (one whitespace fix by the driver); tests are driver-written.

  Evidence: `tests/test_gateway_import_no_env_side_effect.py` (4 tests, subprocess import checks) and `tests/test_f2_quota_status_labels.py` (2 tests).
- FU-F9 (follow-up from the #524 review): `llm_quota_status` now opens with one line stating that the whole order it shows is the legacy quota_balance order, not the live P2.3 routing (live QUOTA_BALANCED routing ranks with `resource_map.score`). The table column "Route Priority" is now "Legacy order", the first-place marker "1st ← next" is now "1st (legacy order)", and the balance line says "legacy order prefers X" instead of "routing to X". The docstring no longer says QUOTA_BALANCED reorders the routing chain from this order: `resource_map.order_chain`/`score` do not read `quota_balance.get_provider_pressures`. The pressure values shown are still read live by the subscription-local demotion (`subscription_local_routing.get_subscription_pressure`). Operators: since FU-F1/F2 (#524) the per-route `quota_score providers=... explains=...` line from `_quota_score_chain` is logged at DEBUG, not INFO, so grepping for it needs debug logging enabled.

  Evidence: `tests/test_f2_quota_status_labels.py` (3 tests; the two `llm_quota_status` tests fail on the pre-F9 code) and `tests/test_gateway_import_no_env_side_effect.py` (`test_sdk_classify_path_leaves_env_unset` renamed `test_import_sdk_then_gateway_leaves_env_unset`, since it only checks import order and never calls `sdk.route`).
- The Gemini CLI installer now accepts the legacy `hooks.json` shape (`"PostToolUse": {"enabled": true}`): it migrates the toggle dict to the current list shape, keeps user entries and keys, and registers every hook, so `llm-router install` no longer reports the gemini-cli row as `error` on such machines (follow-up to PR #768).
- HOOK-DRAFT-1: the prompt hook's local draft chain, and `first_installed()` fallbacks such as session warm-up, no longer pick a decision-only model (`nimble:9b`, HTTP 400 on generate) or a classifier/unnamed-import alias (`llmr-classifier*`, `llamacpp:<sha>`). The hook now applies the same "can generate" rules as the router; the name rule moved to `local_models` so the hook does not import `discover`.
- JUDGECAP: a judge queue at its entry cap whose size sat just under a 256 KiB check line was fully read and rewritten on every enqueue (each append crossed the line, each trim went back under it). A cap trim on a queue over 64 KiB now also drops enough of the oldest entries to end at least 64 KiB below the next line, so the next full check after a trim needs at least 64 KiB of appends. The extra drop is less than 64 KiB plus one entry beyond the cap (up to ~25% of a queue near the 256 KiB line); the newest entry is always kept. Evidence: `test_queue_at_the_cap_just_below_a_stride_line_is_not_reread_per_enqueue` in `tests/test_p31_judge_hygiene.py`, N=400 enqueues with `queued_at` pinned: 400 full checks before the fix, 2 after. Not fixed, unchanged from before: a queue held just under a check line by about one expiry per enqueue is still fully read on every enqueue (400 in 400 in `test_expiry_purge_never_drops_an_unexpired_entry`, same file, all 3 cap settings). Giving the expiry purge the same headroom removed those reads but evicted unexpired entries, even with the cap off (PR #800 review), so the purge removes only expired entries; that test pins it.
- The auto-route hook (v62) no longer reads and parses the whole Claude Code transcript twice per prompt: it reads a 256 KB tail window (doubling only when the tail holds too few turns) and parses it once, shared by the assistant-turn persist, the draft-usage audit and the draft history, so the session-store content is unchanged. `routing_report.draft_acceptance` (every 10th prompt) counts from a sidecar offset instead of scanning the whole `auto-route-debug.log`, and the log is now rotated at 8 MB keeping 3 backups (`routing_log`, `routing_report`, `routing_health` and `northstar` read every generation, so a rotation does not drop history from them; `scripts/release/outcome_audit.py` still reads the live file only). Policy YAML loads with libyaml's `CSafeLoader` when present, `task_availability.resolve` reads the active policy once, and `session_store` / the hook no longer import tiktoken or `okf` on the per-prompt path. Every auto-route latency row now carries `router_added_ms` (elapsed minus model phases, as `agent-route`) and new phases `engine`, `availability`, `mini_summary`, `trend`. The before/after timing on a synthetic transcript is in the PR #550 description; its harness is not in this repo, so no figure is given here (D-171).
- CI flake fix: the L1 latency bench tests no longer fail when a contended runner pushes the real engine over the 50 ms p95 bar (seen on PR #786). Structural checks (layer histogram, no fallback, id redaction, type-only worker errors) now run with a relaxed `--bar-p95-ms`; the 50 ms bar stays the script default and is asserted in a `timing`-marked test.
- Lane A PR4 (gate before the owner flip; D-302 point 4, D-303): a final `shadow_pairs` row is valid only for the identity its pairs were collected under. Every sampled lane A pair now carries `identity_sha` (the sha of the identities of the proxy that served it), and `lane_quality_writer.tally` counts only pairs equal to the batch stamp: others are `excluded.identity_mismatch`, legacy pairs without one `identity_missing`. Before, the writer stamped the proxy's latest record on all pairs, so 60 pairs from policy A counted under policy B after a restart. The identity record (`lane_a_identity.json`) is now written whenever lane A serving is on (`lane_a.enabled`, `LLM_ROUTER_PROXY_LANE_A=on`, `haiku_rewrite`) even with the shadow off, so S1 invalidates an old final row when the policy changes across a restart; the flip still needs all four switches and lane off still writes nothing. Evidence: `tests/test_lane_a_pr3.py` (mixed-identity 60 pairs give no credit and 60 `identity_mismatch`; shadow-off restart refreshes the record).
- Tests only: `test_failures_through_the_proxy_trip_the_breaker_and_the_next_step_stays_pinned` waited for `lane_a_breaker.json` to exist, but the breaker writes asynchronously and coalesced, so the file already held an earlier "closed" snapshot before the trip's "open" snapshot landed; under CI load it read the stale state (main CI runs 40337978067, 40343225356). It now awaits the breaker's own background flush task before reading. No product code changed. Evidence: the old test fails and the new one passes with a 0.3 s delay injected into the "open" write; `tests/test_proxy_lane_a_breaker.py` passed 20 runs in a row.
- LC-1c (D-268): the `local-check` grader no longer fails an issue reference `#N` when the diff carries the number as a GitHub issue or PR URL (`/issues/N`, `/pull/N`, `/pulls/N`). The old rule false-failed bank v1 item `q37-511a487c23de` (draw 1), whose diff has the number only as `issues/18861`. Other digit runs still do not ground a reference. The bank file and its sha256 are unchanged, so both draws are unchanged. The manifest gains a `release_audit` block: LC-1b's audit of the 22 previously unaudited items (0/22 defects; bank-wide 1/140, the grader defect fixed here; no human labels; source `local_check/data/manifest.json` `release_audit`, dated 2026-10-24). Evidence: the new issue-ref cases in `local_check/grader_cases.py` (G3a 103 -> 110), the recorded deviation in `tests/test_lc2_grader.py` and `tests/test_lc5_scoring.py`, and the `release_audit` loader tests in `tests/test_local_check_bank.py`.
- LC-3b (local-check runtimes/runner hardening, review of #663): a failed context-window or digest probe is no longer cached for the adapter's lifetime (it is retried once, never stored, and raises `ProbeFailed` rather than reading as "unknown"), so `settings_hash` cannot drift on a transient failure; `Runner` refuses to start (`RunStartError`, nothing written) when the digest or context window cannot be confirmed. Only http/https endpoints are accepted, a bad port or scheme no longer raises out of probes or `discover` (`[]`), and `generate()` maps every failure (including the context probe and malformed/non-dict stream lines) to an error result. The run dir is chmod 0700 only when the runner created it, `meta.json` is written atomically (temp, fsync, rename) and a torn one raises a clear `RunMismatch` (`RunDirError`), run files are opened with `O_NOFOLLOW` (symlinks refused), and a resumed run already past the early-stop bar runs no further item. A truncated answer with no text is status `empty` and counts as a failure in `default_is_failure`. `discover(endpoint, allow_remote=False)`: a non-loopback endpoint is refused unless the caller passes `allow_remote=True`; Ollama names from `probe_ollama` now pass the same embedding-model filter as `list_models`. Evidence: `tests/test_lc3b_runtime_hardening.py` (fake loopback servers).
  LC-3b repair: probes (`identify`, `list_models`, `digest`, `context_length`, `discover`) never raise on a 200 JSON body of the wrong shape (an unrelated JSON service on 8000/8080): wrong shape reads as "not this runtime" (`None`/`[]`). HTTP 5xx, 429 and 408 are transient (`ProbeFailed`, retried once, never cached); 404 and other 4xx stay a real, cached `None`. `ProbeFailed` is documented on `ctx`/`settings_hash`/`max_prompt_chars`/`digest`, the runner `Adapter` protocol and `Runner()` (`RunStartError`). Without `O_NOFOLLOW`, `append()`/`load()` check for a symlink, and both raise `RunDirError` for one. Evidence: `tests/test_lc3b_runtime_hardening.py`.
- local-check judge follow-ups (LC-4b, library only): the API judge route now always talks to `https://api.anthropic.com` (an `ANTHROPIC_BASE_URL` pointing at the router proxy or any other host is ignored; an Anthropic provider with a custom base URL is refused) and refuses a model with no price in `llm_router.pricing`, so the dollar cap is always enforceable (a metered call that still cannot be priced stops the budget); the consent text no longer claims a dollar cap on a call-capped route, no longer promises resume, and says the subscription route "uses your Claude subscription; if your claude CLI is logged in with an API key, it is billed to that key". An empty candidate answer is judged fail without a call (as in Q37). The `claude -p` env allow-list gains HTTPS_PROXY, HTTP_PROXY, NO_PROXY, SSL_CERT_FILE, REQUESTS_CA_BUNDLE; the CLI backend is a context manager and cleans its temp dir at exit; `budget.recheck_quota` is a between-batches quota re-check; the CLI's cost is reported as `notional_cost_usd` (not money); `judge.served_model_matches` compares the served model id with the pinned one (exact or same canonical id in the price table). Evidence: `tests/test_lc4_judge.py`.
- local-check judge follow-ups (LC-4c, library only): a failed API judge call (timeout, network error, 4xx/5xx with no usage) is no longer treated as "succeeded but unpriceable": it counts toward the call cap, is charged a conservative per-call estimate against the dollar cap (nothing if the client failed before the request was sent), and the retry-once now happens; only a successful call whose served model cannot be priced stops the budget. `judge.served_model_matches` requires an exact id, except a dated `-YYYYMMDD` suffix, a `[1m]` suffix and an `anthropic/` prefix (local ids and other vendor prefixes are never collapsed). `ApiBackend` resolves the configured Anthropic provider base URL (`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_BASE`) itself and refuses a custom one unless a base URL is passed explicitly. Evidence: `tests/test_lc4_judge.py` (LC-4c section).
- LC-7b: local-check retention no longer aborts on non-UTF-8 files: reads catch `(OSError, ValueError)`, per-run failures are collected in the prune report, an unreadable `attempts.jsonl` keeps every run (fail closed), and a symlinked runs root is refused. `doctor` runs prune and the nudge in separate try blocks, each failure recorded through failopen (`CHZ-FO-LOCALCHECK-PRUNE`, `CHZ-FO-LOCALCHECK-NUDGE`). The nudge state directory is created 0700 and its temp file removed on failure. Evidence: new tests in `tests/test_lc7_display.py`; `test_tick_adds_no_io` now also patches `Path.read_text`, `Path.open`, `Path.stat`, `os.stat`, `io.open`.
- LC-8b (hardening of the local-check routing, rv-675): settings parity replaces ctx hash equality (a lane records the `ctx` and `think` it was measured with; it is preferred when route-time thinking is off, the router's ctx is >= the measured ctx and the input fits `min(scope.max_prompt_chars, router window)`; temperature is not part of the claim; a lane recorded before this keeps the hash rule), and a passed lane that is withheld records a `not_preferred` skip cause and a `doctor` line instead of staying silent. Routing now needs a verified digest match from the inventory cache (the 30-day no-digest rule is display only), and an entry enabled from `local_check` no longer falls back to a pre-registered lane when its own lane goes stale. The demotion monitor counts per-attempt LOCAL outcomes (`text_job_local_attempts.jsonl`), so a dead or slow Ollama rescued by the next chain entry demotes at 7 of 20. `enable`/`disable`/`on_pass` are one locked read-modify-write with unique temp files; an untagged name is stored as `<name>:latest`. Evidence: `tests/test_lc8b_hardening.py`; see `docs/local-check-routing.md`.
- Cap both text-job ledgers at two generations of 4 MiB and read rotated local-attempt tails before the live file so demotion retains its 20-attempt window. Record local errors and run demotion even when every provider fails, preserving the original exception. Explain oversized inputs withheld from local preference with the existing `not_preferred` cause and `input_over_limit` detail. Evidence: review of PR #677; regression tests in `tests/test_lc8c.py`.
- Preserve dispatch attempt traces on cancellation and on both timeout causes and their deadline or wall-clock exceptions. Write text-job outcome rows only after normal loop completion. Retain eligibility cleanup and local-attempt demotion checks when a job raises. Account for swallowed local-attempt, context-reset, outcome-write, and demotion failures with distinct fail-open codes. Evidence: tests/test_lc8d.py.
- local-check LC-9b: model recommendations now fit on machine capacity (total memory minus max(4 GiB, 20%), the probe's own reserve) <!-- provenance: exempt (config constant, not a result) --> instead of currently free memory, so a busy machine is not told its model is too big; unknown total memory stays "unknown" and is never read as zero. New `empty_reason(runtime)` gives the "no verified recommendation for <runtime>" text. `min_vram_gb` stays null (no cited model card gives a VRAM figure), so GPU machines use the RAM rule; the measured entry's `bench.date` is now the run date with a separate `listed_date`.
- Clarify local-check diagnostics for installed models that cannot be measured, judged draw usage, bank-comparable recommendations, size-class time estimates, automatic consent, subcommand help and early-stopped verdicts; exclude cold-load time from rate estimates.
- local-check: a `--no-judge` probe never writes the eligibility store (it could replace an earned judged PASS); `status` shows it as a probe result. Recommendations no longer print "bank vNone" when the bank fails to load. The dry-run time estimate parses MxN-B models as total size, ignores `4bit`/`q4bpw` fragments, and labels an unknown GPU instead of assuming CPU-only. The rate estimate drops only a flagged cold-load record, not the first record by position.
- LC-10l: `--no-judge` help, error hint and consent screen now say what a probe is (programmatic checks only; shown in `status` as a probe; never records a verdict or enables routing) instead of the stale FAIL-recording wording. `local-check status` shows `last V k/n <date>` only from the latest judged run (`no judged run yet` otherwise), so a probe no longer reads like a judged verdict. `docs/local-check.md` states that `doctor` shows nothing for a probe. Evidence: `tests/test_lc10l.py` (full status line for probe-only, judged PASS then probe FAIL, judged only; size glued to letters gives no estimate).
- LC-10m / LC-8e: tighten local-check wording and fail-open handling.

  - `--no-judge` is described as probe-only; a test scans source, docs, changelog and root READMEs for stale wording.
  - `status` adds "(not recorded; recorded verdict X kept)" to an INCONCLUSIVE rerun only when the eligibility record for that configuration really holds a verdict (PASS, FAIL or NOT_PASSED). A burnt INVALID, a `--no-judge` run, or a record that was removed no longer counts.
  - Status-line tests also cover an inconclusive-only run.
  - A local-attempt logging failure keeps the original route error.
  - Fail-open codes for local-attempt, context-reset and demotion are covered by tests; cancellation requires an attempt trace.
  - The wall-clock test budget is relaxed to 0.3 seconds.
- Status: INCONCLUSIVE rerun note now reports the pooled recorded verdict (PASS if any matching lane passed, ignoring lane_guard-excluded lanes) and says 'is for a different model digest' when the record's digest differs.
- LC-12b (follow-ups to #702, D-277): `LLM_ROUTER_LOCAL_TEXT_JOBS` now fails closed. Only unset or empty means unset; `1`/`true`/`on`/`yes` are on, `0`/`false`/`off`/`no` are off (case-insensitive, whitespace-stripped), and any other value (`disable`, `nope`) is off, so a typo can no longer hand `llm_text_job` to a pass-routed local model. With the flag unset, a pass-routed `llm_text_job` is served by the passed local model ALONE: the router cuts the final chain to that model after every other chain step (eligibility, daily cap, capability filter), skips the BUDGET emergency fallback, and on a local error, timeout, exhausted retries or a missing passed model returns `fallback` ("do this job yourself") with no cloud call (`PassedModelNotInChain` is the refusal, raised with the budget reservation released). Acceptance-rejection retries still use the local model only. With `LLM_ROUTER_LOCAL_TEXT_JOBS=1` the full chain with fall-through is unchanged. The tool is listed only for enabled lanes of Ollama models; a manual `enable` for a non-Ollama model prints the D-274 note, and `local-check` says the `llm_text_job` tool appears after the next MCP restart or reconnect (or that Ollama is not reachable now). Evidence: `tests/test_lc12b.py`, `tests/test_lc12c.py`.
- LC-12d / D-277: a local-only `llm_text_job` (flag unset, local-check PASS) now fails closed when the eligibility step raises: the raw chain, whose head could be a paid model, is no longer taken (`router._drop_failed_eligibility` raises `PassedModelNotInChain`), and the final cut also requires the survivor to be the passed local model. `test_unset_emergency_chain_is_never_walked` now reaches the emergency path (non-BUDGET profile, distinct emergency chain). Hand-back cause text and the tool/env docs state the three flag states. Evidence: `tests/test_lc12c.py`.
- LOCAL-OVERRIDE-1: an explicit `model_override` to a local provider (`openai_compat`, `ollama`, `lm_studio`, `vllm`, `llamacpp`) that fails no longer falls back to the BUDGET chain, so the prompt cannot reach a paid cloud model; a failed cloud override still falls back. `openai_compat` calls are now written to `routing_decisions` (they were rejected as an invalid provider), recorded at $0, and classified `free_local` in tier and quota reports.
- LOCAL-PIN-1: at the proxy (`--serve local-agent`), a `local:` prompt now forces only that step (and the tool-loop calls that follow it). Before, a `local:` prompt in the middle of a conversation pinned that conversation to local, even when the step was refused, so later prompts without a prefix kept going to the local model. Behaviour change: after a `local:` prompt, the next prompt without a prefix is routed as if the prefix had never been used. A conversation pinned to Claude stays on Claude. A conversation whose first prompt was `local:` and was served locally is pinned local, as it would be without the prefix. A refused `local:` step stores no pin, so after a refused `local:` first prompt the next prompt without a prefix goes to Claude. Evidence: `test_local_prefix_serves_one_step_and_the_claude_pin_holds`, `test_a_refused_local_step_leaves_no_local_pin`, `test_a_local_first_turn_pins_local_as_the_unforced_path_would`, `test_a_refused_local_first_turn_stores_no_pin` (refused before any local attempt), `test_a_failed_local_first_turn_stores_no_pin` (refused after the local attempt fails; LOCAL-PIN-2) and `test_a_served_local_step_mid_conversation_with_an_unseen_start_stores_no_pin` in `tests/test_p27_proxy_prefixes.py`.
- MAP-FIX-1: the shared resource map no longer flaps between processes with different environments. Seat detection now looks a CLI up on the process PATH, then on the dirs where any process of this user last found claude, codex, gemini, gh, node or ollama (`cli_path.json` in the state dir, written atomically with mode 0600, entries expire after 7 days without a sighting), then on `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`; the probe gets that merged PATH, so a `#!/usr/bin/env node` CLI such as codex starts under a launchd-style PATH. Key discovery (map and rescan) now also sees `$LLM_ROUTER_HOME/<provider>.key` files (existence and non-empty only; values are never stored or logged), so the key rows no longer depend on whether `get_config()` has run. A probe timeout or launch error reports its own reason instead of "CLI not found". A "CLI not found" or failed-probe verdict no longer overwrites a subscription row another writer saw connected within the last hour (the row carries `carried_from`; after an hour the new verdict is written, so a real uninstall still shows). Every `map_changes.jsonl` line and `resource_map.json` now carry `writer` (pid, entry point `mcp` / `proxy` / `hook-once` / script name, and a 12-character hash of PATH).
- MAP-FIX-2: a test pins the carry window of MAP-FIX-1 to the first sighting of a missing CLI: repeated blind polls less than 1 h apart no longer keep a seat connected past 1 h after it was last really seen (test only, no source change).
- MAP-FIX-2b: updates to `cli_path.json` now run under an exclusive lock (`cli_path.json.lock` in the state dir), so two processes learning different CLI dirs at the same moment do not lose an entry. If the lock is busy for more than half a second the update is skipped and the next sighting learns the dir again. A CLI found through a relative PATH entry is still used by the process that found it, but its dir is not written to the shared file, because a relative dir means something different in every process's working directory; learned dirs are stored normalised. When a rescan carries one subscription seat over a "CLI not found" verdict, a change to another seat in the same rescan is now written to `seats.json` at once, with the original `detected_at` kept so the carry still ends one hour after its first sighting.
- MAP-FIX-2c: `refresh_seats`, which `llm-router doctor` and `llm-router install` call on every run, now applies the same carry rule as the discovery rescan. A "CLI not found" or failed-probe verdict from a process that cannot see the CLI no longer overwrites a claude, codex or gemini seat that another writer saw connected within the last hour; the carried save keeps the sighting's `detected_at`, so the carry still ends an hour after that sighting and a real uninstall is then written. The session-start hook refreshes only when `seats.json` is missing or over 24 hours old, so the carry does not apply there. `llm-router doctor` prints a carried seat as a warning that names the carry, this process's own verdict, the last sighting and when the carry ends. Learning CLI dirs into `cli_path.json` now tells a busy lock from a filesystem without locking: a busy lock still skips the write, and when locking is unavailable (for example `ENOLCK` on an NFS home) the entry is written unlocked, as before MAP-FIX-2b, and counted as fail-open `CHZ-FO-CLI-PATH-LOCK`.
- MAP-FIX-2d: `llm-router doctor` and `llm-router install` mark a carried seat (one saved as connected because another process saw it connected within the last hour, though this process cannot find its CLI) wherever they print it. Doctor's carried-seat warning now says "carry anchored" instead of "last seen": the time shown is `detected_at`, which one timestamp covers for every seat, so it can be earlier than that seat's last sighting. The doctor free-bucket line and the install Seats summary and free-bucket lines add "(carried)" after a carried seat; output with no carried seat is unchanged. `file_lock.exclusive_lock` now also sets `status["errno"]`: the errno name (for example `ENOLCK` or `EACCES`) when an OSError made locking unavailable, otherwise `None`; `status["state"]` is unchanged. The `CHZ-FO-CLI-PATH-LOCK` fail-open detail includes that name when there is one, so a read-only state dir no longer reads the same as a filesystem without locking.
- MAP-FIX-2e: `llm-router install` swallowed any seat-detection exception silently, including a TypeError from a wrong call; it now prints one warning line, "seat detection failed: <error>", as `llm-router doctor` does, and install still completes. `file_lock.exclusive_lock` reported state "held" with errno None when flock first said the lock was held and a retry then failed with another error such as ENOLCK; the status now reflects the last error ("unavailable" plus the errno name), so seat learning writes instead of skipping. Windows EACCES from msvcrt.locking is still "held". Evidence: `test_seat_detection_failure_is_reported_and_install_completes` in `tests/test_p212_install_run_full.py`; `test_exclusive_lock_status_reflects_the_last_error` and `test_exclusive_lock_windows_eacces_is_still_held` in `tests/test_map_writer_env.py`.
- MAP-FIX-2f: `llm-router install` labelled any failure in its seats block "seat detection failed", including a failure while printing the seat summary or while setting `LLM_ROUTER_CLAUDE_SUBSCRIPTION`. Each step now has its own label ("seat detection failed", "seat summary failed", "Claude subscription setup failed"), printed with a yellow ⚠ (install's `_warn`, as `llm-router doctor` marks its seat-detection failure) instead of a dim line, and install still completes. Two behaviours changed with the split. When detection succeeds but printing the summary fails, install now passes the detected seats to the subscription step instead of None, so it can write `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` to the state `.env` (Claude seat present, variable never set), which it did not do before. When the subscription step itself fails, install no longer calls it a second time with None; that second call only printed "Claude subscription status unknown (detection failed)", which was wrong because detection had succeeded. `file_lock.exclusive_lock` now reports "unavailable" (errno None) when a retry after a held lock raises a non-OSError, so the last error decides in that case too. Evidence: `test_subscription_failure_is_not_labelled_seat_detection` and `test_seat_summary_failure_is_not_labelled_seat_detection` in `tests/test_p212_install_run_full.py`; `test_exclusive_lock_non_oserror_after_held_is_unavailable` in `tests/test_map_writer_env.py`.
- The map re-discovery poller no longer starts on `import llm_router.server`; it starts in `server.main`, `main_sse`, and `main_sse_secured`. The proxy already starts it in `cmd_proxy`.
- Local-model chain filter no longer lets classifier variants or unnamed imports into code chains: `ollama_can_generate` now excludes any model whose name starts with `llmr-classifier` (was the exact name, so `llmr-classifier-38` escaped), any `llamacpp:<hash>` unnamed import, and the models named by `LLM_ROUTER_CLASSIFIER_MODEL` / `LLM_ROUTER_DECISION_MODEL`. The classifier path names its model explicitly and is unchanged.
  Set `LLM_ROUTER_LOCAL_ALLOW_MODELS` (comma-separated exact names) to keep a deliberately named model that matches those rules; exclusions log at debug.
- N22: a call served from the exhaustion floor (every candidate gate-rejected, best answer returned) now writes its real `usage` row (provider, tokens, caller's session) instead of leaving only the `error_unledgered_exit` safety-net row; once a rejected answer is in hand, a local model that timed out in the same call or is inside its timeout cooldown is skipped instead of costing another 120 s timeout.

  The floor-served usage row is written `success=0`, `reason=degraded_floor` (and its routing row `success=0`), so success-filtered quality/cost/bandit readers do not count a gate-rejected answer as good; attribution, mix and M0-3 still count it.
- Status line fast tick (`LLM_ROUTER_STATUSLINE=fast` / `both`): the guard now checks the interpreter the tick will actually run (the cached absolute one from `.statusline_python`, else `python3`) instead of always requiring `python3` on PATH, so a host with no `python3` on PATH but a usable cached interpreter still gets the fast line. A cached value is used only when it is an absolute path to an existing executable file; anything else (empty, relative, missing) leaves the tick on `python3`. Evidence: tests/test_statusline_tick.py::test_tick_runs_under_the_cached_interpreter_when_path_has_no_python3, tests/test_statusline_tick.py::test_tick_falls_back_to_python3_when_the_cached_interpreter_is_unusable.
- NFR statusline p95: with `LLM_ROUTER_STATUSLINE=both` (or `fast`) the status line's tick ran under a bare `python3`, which on macOS with Command Line Tools is 3.9 with no .pyc for `re`/`enum`/`json`, so every call recompiled them. The tick now runs under the interpreter the segment refresher already cached (first line of `~/.llm-router/.statusline_python`, absolute and executable), else `python3` as before; output is byte-identical (same-state diff, plus `tests/test_statusline_tick.py::test_tick_output_is_identical_with_and_without_the_cached_interpreter`). Profile and before/after timings (CPU and wall clock) are in the PR #750 and PR #752 descriptions; their scripts are not in this repo, so no figure is given here. The live metric `acc.NFR_statusline` (bar p95 <= 100 ms over 7 d organic rows of `~/.llm-router/hook_latency.jsonl`) is not re-measured and needs rows after deploy, so whether the NFR is now met is unknown. Evidence for the interpreter choice: `tests/test_statusline_tick.py::test_tick_uses_the_cached_interpreter_not_bare_python3`.
- O3 / P1.9 typed-prompt count (`o3_transcripts.is_typed_prompt`): the host's `[Request interrupted …]` marker and its compaction summary (`isCompactSummary`) are no longer counted as typed prompts. Each added a "prompt" with no human turn, so the P1.9-a integrity ratio read low on the synthetic D-99 sessions (still inside the 0.90-1.10 bar; that check has no recorded source file, so no figure is given here). The proxy's step labeller already read the interrupt marker as not human text.
- Fix first-run grandfathering to include detected OpenAI-compatible models on upgrades, ignore hook-only database history, and claim fresh installs even when the first chain is empty.
- Model catalog `recommend()` now derives the code score as the minimum of the `edit_with_test` and `code_explanation` fitness scores (the pass mark key stays `code`); with no pass mark, code recommendations are labelled "unknown for code (no pass mark yet)".
  Fitness runs (now `llm-router calibrate --fitness`) stay inside the calibration budget (no task starts with 1 s or less left; each call gets at most the time left), run on local models only, are keyed in `local_eligibility.json` by the bare Ollama name `resource_map` reads, record only conclusive task results merged per task type (a run that hit transport errors no longer overwrites an earlier measurement), and a storage error no longer discards the run's capability profiles or leaves a temp file. `recommend()` judges memory fit at the largest listed context instead of the smallest, so a model that fits only at 4k is no longer recommended.
- P2.7-1: an `opus:` pin was lost on a first prompt longer than 8000 characters, because the proxy read only the last 8000 characters of the newest human turn. Prefix detection now reads the whole turn. Test: `tests/test_p27_proxy_prefixes.py::test_long_pin`.
- `usage.session_id` is never NULL (P0.8). An MCP tool call whose client sent no `tool_use` id (and any call outside MCP) wrote NULL; now a call inside `IdentityMCPServer.call_tool` takes the server's session, and an unresolved row gets `unknown:mcp` / `unknown:non_mcp`, which the G3 completeness check counts as missing.
- `routing_decisions.session_id` is never NULL (P0.8-d), with the same fallback as `usage` (`unknown:mcp` / `unknown:non_mcp`; `unknown:script` for the judge-eval and sidecar-backfill scripts). The Claude Code usage hook writes `unknown:hook` when its payload has no session id. G3 counts every `unknown:*` as missing.
- ledger session ids (PLAN v16 P0.8 hardening): the MCP server's env session is stamped only when a tool_use id is bound or the request's MCP client identified itself as `claude-code` in the initialize handshake (`clientInfo.name`, per server session); Codex, Cursor, Desktop, unknown clients and SSE callers without a tool_use id record `unknown:mcp`. `call_identity.is_unknown_session` is the one predicate for `unknown:*`; offload_share, kpi and northstar treat the sentinel as no session.
- P0.9-c: `statusline_tick.py` (the second row of `LLM_ROUTER_STATUSLINE=both` and the whole line of `=fast`) no longer imports `datetime` at start-up; it is imported only when the once-per-5-minute quota-history row parses an ISO reset time. Rendered output is unchanged. The tick-alone timing is in the PR #418 review; its script is not in this repo, so no figure is given here. The remaining cost is interpreter start-up plus `json`/`re`.
- PLAN v16 P1.1 door 7 follow-up: the gateway keeps tool calls in the history it hands `build_pack(door="gateway")`. Responses API `function_call` / `function_call_output` (and custom/local-shell tool) items and OpenAI `tool` results used to be dropped or rewritten before the pack saw them; they now pass through, and the pack renders them as `[tool_use: name] args` / `[tool_result] output` like every other door. A trailing turn that carries tool output, the normal next step of an agent client, is now the request instead of being flattened away with the history: Anthropic `tool_result` blocks (errors included, text beside them kept) and the trailing run of OpenAI `tool` messages / Responses `*_output` items (every parallel result). The router's T4-M1 redaction and budget estimate for a caller's history now cover every string the pack sends (content, content blocks, OpenAI `tool_calls` arguments, every string inside a `tool_use` input or `arguments` dict, tool output), not only `content` and block `text`; ids, images, files and thinking signatures are neither redacted nor counted. New `context_pack.render_message`.
- P1.7-d synthetic harness (D-117): the `shadow` nimble check now follows `docs/prereg/PREREG-P17D-synthetic-AMEND1.md`, which adopts the `nimble:9b` artifact identity and check of amend2-errata1 (local manifest sha256, `ollama list` id, `/api/show` format/quantization/runner) in place of the frozen prereg's GGUF blob pin, which cannot exist under Ollama 0.40.0. The governing `use.log` harness line is the last one of the leading harness lines, so a re-recorded harness commit can be appended before the first shadow row; a harness line after any other line or after the first shadow row refuses, and `shadow` refuses a future-dated harness line before any row. `shadow` re-derives the completed selection instead of re-running `select`, and `label` (before any Opus call) and `score` check the selection file against the shadow preflight record. An unreadable `ollama list` or `/api/show` refuses (exit 2) instead of raising. Every command except `status` also requires AMEND1 to match its freeze record; `result.json` and the measurement disclose AMEND1.
- Synthetic replay no longer fails on CI after map re-discovery: importing `llm_router.server` started the 30 s poller inside the pytest worker, and its Ollama probe (127.0.0.1:11434) fired during the replay and counted as a network violation. The harness now stops the in-process poller before installing its guard, the suite sets `LLM_ROUTER_MAP_REDISCOVERY=off` by default, the detached `--once` child honours the switch, and the harness prints its violations and errors to stderr on failure.
- CI flake fix: the judge-queue enqueue timing test no longer fails when a contended runner slows every enqueue (it failed with an absolute `p95 < 5 ms` bar on PR #788, which does not touch the judge queue; a re-run passed). The property it protects, that enqueue does no O(n) work or re-read on a large queue, is now checked two ways: a clock-free test counts reads of the queue file and allows one only on an enqueue whose append crosses a `_CHECK_STRIDE_BYTES` boundary, and the `timing`-marked test compares the median enqueue time on a 2,000-entry queue with a 50-entry one, timed in interleaved pairs, against a ratio bound (bound and margins in the test docstring), plus a generous absolute p95 bar for constant costs. Evidence: `tests/test_p31_judge_hygiene.py`.
- P3.4 tidy (F92, F95, F99): the status-bar hook (v9) counts any import-time error inside `llm_router` (class only) and shows `💰 n/a` instead of keeping a stale line; `_fmt_cost` no longer prints `-0.000¢` for a loss below display precision (real losses stay signed); every `savings.SURFACES` ref names its function rather than a line number; a dedicated `ledger_view` test pins that a cache row inside a session window is not an executed call; restores a dropped `@pytest.mark.asyncio`. Evidence: `tests/test_p34b3_status_bar_ledger.py`, `tests/test_p34_fmt_cost_negative_zero.py`, `tests/test_p34_ledger_view_session_cache_row.py`, `tests/test_p34a_ledger_view_lint.py`.
- Redo validation now reports exclusion-list provenance and session-kind supply, warns about tagging gaps, and requires explicit consent to freeze with an empty exclusion list. Missing transcripts refuse a census, and freezes publish private run directories atomically with cleanup after failures. Synthetic CLI regressions cover provenance, privacy, determinism and failure recovery.
- P3.6-c PR1c: The census/freeze verdict said "detector tune to v3" even when most sessions were untagged; it now reports NO-GO (tagging_gap) and directs users to fix session tagging, then re-census. Evidence: test_kind_gate in tests/test_redo_sample_1b.py.
- `redo-sample --include-untagged` now resolves sessions through the proxy ledger's session stamps before counting them untagged. The whole ledger is streamed with no time cutoff (a session with pairs in the window may carry only older stamps); only session-to-kind stamps are held, so memory is bounded by streaming, not by time. A session whose ledger stamps carry two or more kinds (and that no override or tag file resolves) is excluded from the pool, never drawn as untagged or organic; the census and `frozen.json` report `ledger_conflict_excluded`, with a warning when it is above zero. The census reports `proxy_ledger` (`present` / `missing`) and `proxy_ledger_rows`; an unreadable proxy ledger refuses `--include-untagged` with exit 2. A missing ledger prints "WARNING: proxy ledger missing: sessions are resolved from tag files and the backfill sidecar only" in census and freeze output. The backfill sidecar is read once per run; the census reports its state (`present` / `missing` / `unreadable` / `corrupt`, with a warning for the last two) and untagged counts by cause in three facets (tag: `no_tag_file` / `corrupt_tag`; ledger: `no_ledger_stamp`; sidecar: `sidecar_missing` / `sidecar_unreadable` / `sidecar_corrupt` / `sidecar_no_entry` / `sidecar_unknown`). These keys and warnings appear only with `--include-untagged`; without it the census output keys are unchanged. Exclusion-file lines that are not 16 lowercase hex characters refuse with exit 2 and a count only, naming the file as `shipped` or `local` (never the path); a UTF-8 byte-order mark on the first line is ignored; a group- or world-readable local exclusion file gets a warning. `redo-score` reports per-kind precision/recall only for runs frozen with `--include-untagged`, and per-kind recall is not estimable for a concentrated draw. Frozen and draw-row weights are labelled combined (`weights_combined`, `weight_N_h_combined`, `n_h_combined`).
- Persist judge calls before invocation and conservatively estimate unknown costs. Split guard overrides, enforce the 5% unresolved threshold and unwaivable F-pair rule, refuse invalid judge metadata, and retry readable transcript duplicates. Document cumulative per-run spend caps.
- `redo-label --judge`: the USD cap now counts the upcoming call (the fixed $0.50 estimate <!-- provenance: exempt (module constant `redo_label.ESTIMATED_CALL_USD`, not a result) --> is pre-charged to the ledger and replaced by the real cost), so a crash loop is bounded by spend; a corrupt or wrong-typed `judge_spend.json` is refused with exit 2 instead of silently resetting the ledger.
- Redo labelling now holds an exclusive per-run lock, refuses recursively nested spend ledgers, and re-reads the ledger before each judge batch. Redo sampling suppresses tagging-gap warnings when flagged supply meets the threshold and explains that overriding a tagging-gap refusal freezes an under-supplied sample without fixing session tagging.
- S3 quota coverage (P3.7-a) now counts Claude Code sessions only. Codex sessions are tagged by `codex-auto-route.py` but never write a quota row or burn Claude quota, so they sank the denominator: on the owner's `~/.llm-router` for 2026-10-20..2026-10-27, `quota_burn_report` coverage went from 4/18 to 4/4; all 14 dropped sessions were Codex threads (UUIDv7 ids). The reader keeps a tag only when its `host` is `claude_code`, or, for tags without a host, when the session id is not a UUIDv7. `session_kind.tag_session` now records `host` (read from the running hook's path via `hook_latency.detect_host`; omitted when unknown).
- `kpi --quota-burn` coverage (P3.7-a) now counts Claude Code sessions only on the quota rows path too, not just the session tag path. A session seen only through `quota_samples.jsonl` rows is dropped when its tag names a `host` other than `claude_code`, or, with no host (rows carry none), when its session id is a version-7 UUID (Codex thread ids). Before, a Codex plugin install's lone start row with an organic stamp entered the coverage denominator and could never be covered. Dropped sessions are counted in the new `samples.sessions_not_claude_code` field; `sessions_other_kind` keeps its meaning. Evidence: tests/test_quota_samples.py::test_rows_path_drops_a_codex_session_with_only_a_start_row, tests/test_quota_samples.py::test_rows_path_reads_the_host_from_a_tag_outside_the_window
- `kpi --quota-burn` (P3.7-a) applies the Claude Code-only rule in two more places. The assumed baseline-week start (first organic row of `quota_samples.jsonl`) now ignores sessions that are not Claude Code (a `host` other than `claude_code` in the session's tag, or, with no host, a version-7 UUID session id), so a Codex plugin start row older than the first Claude Code row no longer moves the baseline earlier. The default text report now prints the `sessions_not_claude_code` count next to the untagged and other-kind counts, so dropped sessions are visible without `--json`. Evidence: tests/test_p37_burn_retention.py::test_baseline_ignores_an_earlier_codex_organic_row, tests/test_quota_samples.py::test_text_report_shows_the_not_claude_code_count
- Tests: pooled FAIL-then-INCONCLUSIVE status test now uses real runs, the all-lanes-lane_guard fallback verdict is pinned, the redo-label lock-create-failure test asserts no run artefacts, and the d154 grandchild.pid wait no longer races an empty file; 'different model digest' wording aligned in the LC10n fragment.
- `local-check status` says "different model digest" (not "earlier") for a record under another digest; `redo-label` tells a held run lock apart from an uncreatable or unsupported lock.
- Preserve isolated HOME and LLM_ROUTER_HOME in provider-reset subprocess tests so fake benches cannot pollute the operator's KPI G4 ledger (PROVIDER-BENCH-LEAK-1).
- QA-LAST-RESORT-1 (D-114, D-115, D-116): a Q&A route no longer drops local models. They are
  tried after every listed non-Claude provider and before trailing Claude entries, or first
  when only Claude is listed, so an installed Ollama answers instead of the call failing on a
  missing Anthropic API key or on a non-Claude provider that fails at call time. This applies
  to MCP `llm()`/`llm_route`, the gateway and the route server (all via `route_and_call`), the
  SDK `llm_router.route`, and the zero-Claude hook. Normal hook DIRECT mode still strips local
  for Q&A, because its turn falls through to the host Claude session. Earlier D-14 tests now
  assert "after non-Claude, before Claude" instead of "absent". Under a daily cap, a capped Q&A
  chain that contains a local model is now served by it in smart and hard modes. Reconnect MCP
  servers to deploy; the auto-route hook refreshes at hook version 59.
  Details in docs/bugs/QA-LAST-RESORT-1.md.
- The Gemini CLI installer no longer prints a false "reset non-list <event>" line for a hooks.json event key that is simply absent (for example a `{}` file); the note appears only when the key exists and is not a list.
- QHYG3: a named Claude model blocked by subscription mode with nothing else reachable (the main-chain refusal in `router.py`) now leaves a route-error ledger row with the reason `error_named_model_blocked`, written through the existing `cost.log_route_error`; the client-facing refusal text is unchanged. Test: `tests/test_un09b_blocked_named_model.py`.
- QHYG3: the secret scan no longer refuses a "-sk-" blob that only looks like a token start because a scan window begins on its "s". Every window after the first now starts one character early, so the "-" before the "sk-" is visible to the lookbehind, and a match that begins on that context character is left to the previous window. Test: `tests/test_qhyg3_window_left_context.py` (red before the change at the window-start offsets). SECURITY.md now says what the legacy `sk-` token-start rule reads: the character before it is not a Unicode letter or digit, `_` or `-` (Python `\w` is Unicode).
- The Gemini CLI installer no longer prints "Added ... hook" or "reset non-list ... (backed up)" lines when it could not back up `hooks.json`: the file is left unchanged and only "Could not back up ...; left unchanged" is reported (follow-up to PR #776).
- `scripts/synthetic_replay.py` network guard: the guard was installed as `sitecustomize.py` in the scratch venv. On Debian and Ubuntu Pythons, the distribution's own `/usr/lib/python3.X/sitecustomize.py` comes earlier on `sys.path` and shadows it, so no replay child was ever guarded and every run reported 0 network violations. The guard now loads from a `.pth` import line (`00_replay_netguard.pth` → `_replay_netguard_baked`), which a system `sitecustomize` cannot shadow. `tests/test_synthetic_replay.py::test_a_door_pointed_off_the_stub_fails_the_run` and `::test_guard_is_baked_into_the_child_interpreter` pass again in such environments. The baked guard ignores an inherited `LLM_ROUTER_REPLAY_*` environment, so the run's allowlist always wins. A start-up check refuses the run when the guard is not active in an empty-environment child; a guard that silently fails to load is now a hard error, not "0 violations". Also adds a mixed-window family to the D-101 registry-window tests, so a max-instead-of-min mutation of the family rule is caught.
- ROUTE-CONT-1 (D-57): `hooks/auto-route.py` defined `_CONTINUATION_RE` twice and the broad directive pattern (added by 9a370e69) shadowed the strict single-word acknowledgement pattern, so the UserPromptSubmit "strict ack" bypass swallowed real directives such as "keep going, open the PR when done" (no `user_prompt` recording, no routing). The patterns are now `_STRICT_ACK_RE` (bypass, `_is_continuation`) and `_CONTINUATION_DIRECTIVE_RE` (`_is_context_dependent`, `_is_short_code_followup`); a guard test fails if a module-level name is assigned twice.
- `llm-router redo-sample --census` (and the freeze gate) no longer blames session tagging when untagged sessions cannot contribute: the `tagging_gap` verdict, its warning and the "fix session tagging" refusal note now fire only when untagged sessions hold more eligible pairs than organic sessions, not when they are merely more numerous. Headless `claude -p`/SDK runs with no human prompt are untagged but hold no pair, so a shortfall among them now reports `insufficient_flagged_supply`. The census prints a new `untagged_eligible_pairs` count (pairs in the window that untagged sessions would have admitted as organic; counted without keeping text). The warning now reads "untagged sessions hold more eligible pairs than organic ones" (was "most sessions are untagged"). Exit codes and every other census field are unchanged. Evidence: `tests/test_redo_sample_1b.py::test_headless_untagged_sessions_do_not_make_a_tagging_gap`, `tests/test_redo_sample_1b.py::test_untagged_sessions_with_pairs_still_make_a_tagging_gap`.
- S1 release clock: the scheduled `kpi --daily` run now reads the synth-live manifest from `LLM_ROUTER_SYNTH_MANIFEST` when `--synthetic-manifest` is absent (the scheduler install copies the variable into the job when it is set at install time), and a manifest session's `subagent_first`/`subagent_turn` proxy rows with no transcript role count as turns (the driver runs `claude -p --no-session-persistence` without a launcher tool, so they never counted before). Because turn numbering also drives the redo/escalation windows, synthetic action redo verdicts can shift. A readable manifest with 0 session ids is refused (exit 2) instead of writing an organic-only row. The daily row states both in `synthetic_manifest_source` and `synthetic_turn_rule`. Evidence: `tests/test_s1fix_synthetic_streak.py`; diagnosis in `~/.rsi/research/primary-plan/v16/coord/S1-streak-diagnosis-20261101.md`.
- Local agent lanes (`llm_act`, `llm_local_task run_command`) now refuse a non-loopback host in any `node --inspect*`/`--debug*` flag, including `--inspect-port` (value joined by `=` or given as a separate argument), plus `go -mod=mod` and `uv run --keyring-provider`.
- SECDET: the secret door and the scrubber judge the token itself. A nearby `.env.example` / `.sample` / `.template` label or a `.example` host no longer exempts a real-looking key (that text is page- or site-controlled). A placeholder look exempts a token, a JWT signature included, only when no long random body is left once the filler is cut out, so filler glued after or spliced into a real key (`xxxxxxxx`, `abcdefgh`, `-example`, `01234567`) no longer hides it. A key with a distinctive prefix glued to preceding text (`mykeysk-ant-api03-...`, `123AKIA...`, `键ghp_...`, `xsk-or-v1-...`) and every ASCII percent escape (`sk%2Dant-...`, `Bearer%20...`) are now read by the door, and the scrubber decodes the same escapes. Ordinary words that end in a key prefix are not keys: a word-glued unmarked `sk-` is not read, a glued `sk-ant-` / `sk-proj-` needs its marker or a mixed-case alphanumeric run, a glued `AIza` / `gh[pousr]_` needs exactly the issued length (and no 7 lower-case letters in a row for GitHub), `AKIA` is not read after an upper-case letter, and an unglued unmarked `sk-` must end within 256 key characters. The scrubber redacts a percent-encoded secret span only (neighbouring `%26` / `%2F` stay as typed) and finds a glued `ghp_` / `xox` / JWT token. The `exempted_by_label` counter is removed with the exemption. The length rule per shape and the residuals (a word-glued legacy `sk-` key and a glued GitHub token with 7 lower-case letters in a row are left to the scrubber; double percent-encoding, HTML entities, base64 and characters inside a token are missed) are in SECURITY.md. Measured 2026-10-31:
  - Timing: scrub and door on every pattern this change touched, each against a prefix repeated, a prefix plus a long body run plus one non-matching character, and alternating separators, at 200k and 1M characters (n=60 shapes x 2 sizes, one process each; the shape list is in PR #792): at most 0.41 s (scrub) and 0.71 s (door) at 1M. The scrubber's former restart at every `eyJ` took 6.8 s at 200k; its regression test is `tests/test_secdet_token_not_surroundings.py`.
  - Look-alikes, n=192 (rv-792b set: lower-case slugs, PascalCase ids, IPFS / Drive / nanoid ids, `-ant-` kebabs, `highs_` + camelCase, upper-case UUIDs after words ending in `sk`): 0 refused (b29f2bef: 97). Random base64url blobs, n=2000 of 1,024 chars and n=40 of 100,000 chars: 0 refused (main 6d7857c0: 2). Both sets are probes listed in PR #792; tests in `tests/test_secdet_token_not_surroundings.py`.
  - Repo text, `python3 reposcan.py <tree> <tree>` from PR #792 (every `git ls-files` path that reads as UTF-8, each tree with its own code): main 6d7857c0 N=3128 files, this branch N=3130; door-refused files 4 -> 4 (same files); scrubber-changed files 236 -> 236; lines with a high-precision hit 5 -> 6, the new one the synthetic db-URL canary in `tests/test_r1_scrub_before_persistence.py`.
- SECDETb: the secret door refuses a legacy OpenAI key (`sk-` followed by its 48 letters and digits) again when a long run of base64url characters is glued straight after it at the start of a token. Since SECDET, an unmarked `sk-` had to end within 256 key characters, so such a key with a long glued tail reached a cloud model (the log scrubber still redacted it). The door now decides by the character before `sk-`: at a token start the key is read whatever follows it; after `-` (inside a base64url blob) the 256-character end rule still applies, so an `sk-` inside a random blob is still not a key. Still not refused by the door: a legacy key that follows `-` and is glued to a run longer than 256 characters (`x-sk-<48 chars><long run>`); the scrubber still redacts it. Evidence: `tests/test_secdetb_legacy_sk_glued_run.py`.
- statusline timing rows now carry load1 (P0.9-c, D-56): read via os.getloadavg() inside the backgrounded record-raw process, no added spawn.
- STEP-SIDECALL-1 (D-59): the proxy ledger labelled the answer to a human typed prompt `side_call` or `continuation`. Cause 1: Claude Code's Message Threads continue (`thread.type == "continue"`) sends only the tail of the conversation and no `tools`, which `step_kind` read as a side call; the label now comes from the parent reply (the proxy remembers `msg_id -> is_main_thread`) and an unknown parent is `subagent_turn`, never a main-thread turn. Cause 2: a prompt typed while a tool runs rides on the tool-result turn and read as `continuation`; human text beside `tool_result` is now `turn_first` (main thread) or `subagent_turn`. New ledger column `thread` (`create` | `continue` | null). A thread continue is never served (`step_class` returns nothing for it) and tier decisions are unchanged; text beside tool results counts as human only when it has no peer/coordinator/plugin/observer/notification wire prefix. After a proxy restart a thread that began earlier stays `subagent_turn` until a new thread or a continue carrying tools. Measured on a ledger copy (43,642 rows, md5 db7657a1) over 2026-09-29T00:00Z..2026-10-11T12:00Z: session 6cf3b1b8, 26 human turns: 17 `side_call` + 2 `continuation` + 3 `turn_first` + 4 no row before; mapping (synthesized shapes, not a re-run: the ledger keeps no body) gives `turn_first` for the same 22 rows. Six headless sessions, 419 human prompts with a row: 94 `continuation` before, 0 after. 6,942 of 9,306 `side_call` rows in the window (N=9,306) had under 0.5 request bytes per token beside more than 100K tokens (median body 21 KB), i.e. main-thread calls. Sources: `~/.llm-router/proxy_calls.jsonl` copy joined to `~/.claude/projects/*/<session>.jsonl` by `msg_id`; details in docs/bugs/STEP-SIDECALL-1.md.
- STEP-SIDECALL-2: follow-ups to STEP-SIDECALL-1 in the proxy step labeller (`_is_non_user_text`), read from the Claude Code 2.1.296 binary. A verified Slack human turn (led by "A message arrived in the bound thread while you were working:") is now a human prompt instead of being dropped as non-user; an observer-activity digest (`<NAME-activity>` tags and its fixed postamble) and "Activity in the bound conversation" text are now non-user; the user-queued prefix must open the text rather than appear anywhere in it. `async_hook_response` needed no change (it is already wrapped in `<system-reminder>`). Ledger labels only: `step_class` serving is unchanged on 18 non-thread body shapes. Details in docs/bugs/STEP-SIDECALL-2.md.
- Hook tests no longer race late writes into a temporary HOME: a registered opt-out, `LLM_ROUTER_NO_DETACHED_SPAWN`, stops both detached children the auto-route hook can start (the usage refresh and the model-discovery probe), with behavioural tests; the context-pack timing test takes the best of five p95 rounds while keeping the 50 ms limit; CI fetches the OKF baseline commit and the re-derivation test fails instead of skipping under CI.
- The test suite can no longer start the background agentic-registry probe (`python -m llm_router.agentic_registry`), which outlived its test and ran live Ollama inference on developer machines (PR #797 review left 5 such processes running, PR #796 left 8). `agentic_registry.populate_in_background()` now honours `LLM_ROUTER_NO_DETACHED_SPAWN` like the other detached-spawn sites, so an installer started as a child process of a test is covered too. In the suite, an autouse stub replaces the function, and an audit hook refuses any `subprocess.Popen` of that module. Installs outside the test suite are unchanged.
- W1-2 (UN-09): the router's chain injections now answer to the same filters as the main chain build, found by the ineligible-route replay `tests/test_w12_ineligible_replay.py` (seed 20261029, run 2026-10-29). Closed: (1) the precision tier, which fronts `openai/gpt-4o-mini` for a short exact-answer prompt, and a policy subject specialist are only fronted when the provider is reachable, not blocked (`LLM_ROUTER_BLOCK_PROVIDERS`, `block_providers` in `routing.yaml` or `.llm_router.yml`), allowed by the policy lists, present and connected in the resource map and allowed by the user's stored rules; before this, 1 of 118 router and 1 of 118 gateway auto-routed requests went to a disconnected `openai`. (2) In subscription mode a caller-named `anthropic/*` or `gemini/*` model used to fall back to a static chain with no filter at all; the router now treats the blocked name as routing, not as the caller's pin, and routes the call as an un-named call without the blocked provider: the profile and complexity are resolved from the request, the chain is built with the same filters (reachability, block lists, policy, map, user rules), the blocked provider is dropped, and every step after the chain build applies as it does for an un-named call (D-116 Q&A ordering, the F106 failed-lane drop, precision tier, subject specialist, bandit, semantic cache, and the secret door, which no longer reads the blocked name as a pinned cloud request). So providers with no key and blocked or disconnected providers are no longer served. Without this a named Claude model in subscription mode on a Q&A task served a local model first (`tests/test_mcp_qa_no_local.py`, `tests/test_w12_ineligible_replay.py::test_subscription_blocked_named_model_serves_the_unnamed_qa_order`). When nothing is left after the blocked provider is dropped, the call is refused (NoRoutableModelsError naming the subscription block, gateway HTTP 400); in the named slice below that was 33 of 220 requests on each of router and gateway (`tests/test_w12_ineligible_replay.py`, seed 20261029), which on the base were served from the unfiltered static chain. A front entry (precision tier, specialist) whose vetting raises is left out and counted as fail-open event `CHZ-FO-ROUTER-FRONT-VET`. What the tests prove: 1,640 generated auto-routed requests over the router, gateway, hook, SDK and proxy doors gave 1,079 judged decisions (at least 150 per door) with 0 ineligible by both the counter and an independent predicate; a caller-named subscription slice of 220 requests each on router and gateway reads 0 ineligible among the judged decisions; planted ineligible decisions and a map-filter-disabled build are counted. Not closed: an explicit single-model pin (`model_override` outside the subscription fallback) is honoured as asked, so a pin to a disconnected model is routed there and counted by the counter like any other route (07-ACCEPTANCE M2); the proxy pins (`choose_model` pin, `--serve local-agent`) check the user rules but not the map and are not exercised by the replay; a local model the inventory lists but the map marks disconnected while a sibling is connected is not generated; the live 7-day count (tag criterion 9) still waits for organic data.
- UN-09b S1: in Claude subscription mode, a request that names a Claude model is no longer served by Claude through the three paths that still could. The emergency budget fallback chain (walked when the named request's other providers fail) and a routing-policy subject specialist now drop every `anthropic/*` model for that request, as the main chain already did, and the semantic cache is not consulted for it (a stored answer may be Claude's). On the gateway's Anthropic door (`/v1/messages`), Anthropic clients name a Claude model on every request, so in subscription mode that door no longer uses the semantic-cache lookup unless the client sends an auto alias. When nothing in the emergency chain serves, the request ends in the same "All models failed" error as before, with one added sentence saying `anthropic/*` was not tried; that sentence appears only when the block removed a model from the emergency chain. The HTTP status (502), the ledger reason and the HTTP doors' rule that provider error text never reaches the client are unchanged; the MCP door returns the scrubbed error text as before. The same wiring applies to a named `gemini/*` model in Gemini subscription mode, where it is redundant in practice because that mode already removes Gemini from the available providers. Un-named requests, named non-Claude models and non-subscription mode are unchanged. Evidence: `tests/test_un09b_blocked_named_model.py`.
- W0-4 (PRD S7): state files are 0600 and state directories 0700 through one helper, `llm_router.state_files` (`open_private`, `private_dir`, `connect_sqlite`, `harden_tree`, `migrate_once`). Evidence: a 2026-10-25 audit found top-level files in one live state directory group- or world-readable (`proxy_calls.jsonl`, `sessions.db` among them) under a 0700 directory. Routed through the helper: `capped_log` (directory creation), `file_lock` lock files, the proxy ledger, `session_store` merge and restore writes, and the 14 SQLite stores that create their own database (sessions, context, spend, execution ledger, feedback, provider registry, budget backend, admin actions, idempotency, semantic, lineage, agentic telemetry, control plane); the database is created at 0600 first, so `-wal`, `-shm` and `-journal` inherit it. `llm-router doctor` and the MCP server start run a one-time repair of existing files (marker `.modes_migrated_v1` holds counts only; symlinks are skipped and nothing outside the state directory is changed; an owner-execute bit is kept). Remaining raw writers are frozen in `tests/w04_raw_write_baseline.json` (123 files, 302 sites at the time of writing) and may only go down. Evidence: `tests/test_w04_state_file_modes.py` (mode scan under umask 000, rotated logs, WAL sidecars, migration, key-leak scan, ratchet).
- W0-4b (PRD S7, follow-up to W0-4): state-file modes are hardened on every start instead of once. `state_files.harden_state` (the renamed `migrate_once`, marker removed) now runs on every `llm-router doctor`, MCP server start and proxy start, so a file that a legacy writer creates at 0644 after the first run is repaired on the next start and a failed chmod is retried (3,000 synthetic files: 0.035 to 0.044 s for a first pass that fixes every file, 0.008 s when nothing changes; `scripts/time_harden_state.py`, 2026-10-25). Routed through 0600 creation: `prompt_capture.jsonl` and `gt_accumulation.jsonl` (raw prompts), `ground_truth_candidates*.jsonl`, `enforcement.log` and the highest-traffic raw writers of the `enforce-route`, `stop-enforce`, `session-start`, `session-end`, `agent-route` and `auto-route` hooks (stdlib-only `_priv_open`, since hooks run standalone), and the database creators in `budget`, `provider_budget`, `receipt_store`, `cost`, `quota_tracker`, `onboard` and the readers `sidecar`, `retrospective`, `attribution`, `team_sync` (export), `test_delta` and `doctor`, through the new `connect_aiosqlite` and `connect_sqlite(create=False)`. Read-only callers use `create=False`, which creates no file and no directory. The ratchet in `tests/test_w04_state_file_modes.py` is now an AST scan that sees `open(paths.state_path(...), "w")`, `open(str(p), "w")`, `open(p, mode="w")`, `os.open(..., O_CREAT)`, `Path.open("a")`, `write_text` and `sqlite3`/`aiosqlite.connect`, and it covers the top-level `hooks/` directory. Honest count: the same AST scan finds 130 files / 329 sites on the W0-4 tree (the W0-4 regex reported 302 as a lower bound); after this change the baseline is 122 files / 257 sites. S7 is not closed: the remaining writers are listed in `tests/w04_raw_write_baseline.json`, and a shared team database and a foreign LiteLLM database are deliberately left alone. Hook versions: enforce-route 24, stop-enforce 3, session-start 43, session-end 51, agent-route 25, auto-route 76.
- W0-4c (PRD S7, follow-up to W0-4b, from the #725 review): the raw-write ratchet in `tests/test_w04_state_file_modes.py` is stricter and wider. A call counts as routed only with `opener=private_opener` (or `paths.private_opener`, or the hooks' local `_private_opener`) or inside a hook helper named `_priv_*`; `opener=<lambda>` and `opener=None` now count as raw. `os.open` with the flags in a variable is raw unless its mode is the literal `0o600`; `from os import open as o` aliases are resolved; `scripts/` is scanned (`scripts/groundtruth/pool.py` stores prompt text); and files that never mention the state directory are no longer skipped. Baseline regenerated with `python tests/test_w04_state_file_modes.py --write`: 122 files / 257 sites before, 256 files / 497 sites now (the count rose: 18 sites fixed below, 225 sites in 124 `scripts/` files and 33 sites in 20 `src/`/`hooks/` files newly counted; the new total includes writes that are not state). `llm-router doctor` now also hardens after its own checks, so the first run on a fresh HOME leaves `.llm-router` 0700 and `seats.json` 0600 (before: 0755 and 0644 until the second run). `demo`, `gain`, `last`, `replay`, `share`, `verify`, `invoice`, `dashboard_data`, the TUI, `quota_savings` and the premium status panel open `usage.db` with `connect_sqlite(..., create=False)`; on a state directory that exists without a `usage.db`, `demo` and `gain` used to create an empty one (`gain` through `cost.get_compression_stats`), and now create nothing with the same exit code and output. New tests: `tests/test_w04c_readers_create_nothing.py`, a fresh-HOME doctor test, and a per-site check that every `enforcement.log` write in `hooks/enforce-route.py` and `src/llm_router/hooks/stop-enforce.py` goes through a `_priv_*` helper (no hook file changes in this PR). The "0.04 s for 3,000 files" figure in the W0-4b entry now has a source, `scripts/time_harden_state.py`.
- W1-3 (P1.11 task 5, INVENTED-DEFAULTS-1, owner decision D-296: policy path only): `user_routing_policy` no longer invents scoring defaults. Removed the quality 0.5 (free providers) and 0.4 fallbacks and the price 0.001 fallback (and the litellm `model_cost` default); an unknown quality or price is `None` and sorts after every known model, stably, under the `cost` and `quality` policies. `balanced` is unchanged. The policy reads measured benchmark scores only. The resource map reports `limits.context_window` as `unknown` when the model reports none; `local_models.DEFAULT_NUM_CTX` stays the request size. The default chain is unchanged: `benchmarks.py` and `scorer.py` are byte-identical to main; pinned by `tests/test_w13_unknown_defaults.py::test_default_chain_scoring_order_unchanged_vs_main`. Disclosed residuals (R-DIS-13, until measured local quality exists, P1.11, per D-296): `benchmarks._LOCAL_QUALITY_SCORES`, `benchmarks._DEFAULT_QUALITY` (0.50), and `task_scores.get(model, 0.5)` (benchmarks.py ~:647) remain for the composite scorer / chain builder.
- W1-3b: the invented-defaults scan now covers every function in user_routing_policy.py except a commented non-scoring allowlist, and the default-chain test pins score groups and numeric scores including a weak-but-measured model below the unknowns. Test-only; no src/ change.
- Tests only: the invented-defaults scan no longer skips whole functions; it allows exact (function, expression) pairs, flags module-level numeric constants read by a function, and checks allowlist entries still occur.
- W1-3d (tests only): the invented-defaults scan over `user_routing_policy.py` no longer misses five shapes: a second copy of an allowed expression in the same function (each allowed pair now carries an exact count, compared by AST rather than text), an annotated, tuple-unpacked or container-held module constant (`_Q: float = 0.3`, `_DEFAULTS = {"quality": 0.3}`; the range now includes 0, 1 and negatives down to -1), a signed default (`.get(k, -1.0)`), and a `getattr(o, name, 0.3)` default. No source change; the scan finds nothing new on main. Evidence: `tests/test_w13_unknown_defaults.py`.
- `scripts/release/release.sh` no longer uploads to PyPI itself (publishing is only `publish.yml` via OIDC behind eval-gate); release notes are extracted portably (the old GNU-only `head -n -1` failed on macOS), written to a file, and replaced by a CHANGELOG link plus "Known issues" when over 100,000 bytes; `rollback()` never pushes or deletes a public tag. (D-349)

### Added
- door-agreement tool (PLAN v16 P1.6 task 0, P1.6-i): `scripts/door_agreement.py --corpus <jsonl> --sites hook,gateway[,...] --out <json>`
  reports, per pair of classifier call sites, how many prompts get a different task_type and a different tier, with Wilson 95% CIs,
  plus the count where any listed site differs. Sites now: `hook` (auto-route.py `classify_prompt`), `gateway` (`gateway._classify`),
  `proxy` (`proxy/tiers._default_classify` on `steps.tier_text` of the prompt) and the reference `hook_policy` (`classify_signals(HOOK_POLICY)`, what `measure_low_signal_rate.py` called "hook"); the other P1.6
  sites are added one per wiring PR. `--freeze-corpus <path>` writes `measure_low_signal_rate.collect()`'s prompts once (mode 0600,
  never overwritten, temp file hard-linked into place). Each pair also reports a shared-labels rate (only prompts where both
  sites' labels are in the vocabulary both emitted), and `--freeze-dates` / `--dates` / `--date-cutoff` add a reproducible
  split by prompt date from a counts-only per-line date sidecar. Output is counts and hashes only. No model calls: the hook's LLM layers are forced off by the env switches its
  code reads (`LLM_ROUTER_CLASSIFY_LOCAL_ONLY`, `LLM_ROUTER_DISABLE_LLM_CLASSIFIERS`, `LLM_ROUTER_HOOK_LLM_LAYER=off`), the proxy's pre-P0.9-e chain build is replaced by an empty chain,
  `LITELLM_LOCAL_MODEL_COST_MAP=True` stops LiteLLM's import-time download, `LLM_ROUTER_OLLAMA_MODEL` stops older hooks'
  import-time Ollama probe, and DNS plus every outbound socket connect is refused during the run (a `BaseException`, and any
  refusal fails the run). Measure an older commit by putting its tree's `src` first on PYTHONPATH.
- synthetic replay harness (owner decision D-42; test tooling, no product change): `scripts/synthetic_replay.py --corpus <jsonl>
  --out <dir> [--doors hook,proxy,gateway,mcp,sdk,agent-route] [--sessions N]` drives synthetic sessions through every
  classification door (auto-route and agent-route hooks as subprocesses, an `llm-router proxy` on an ephemeral port, the
  gateway/SDK classify path, the MCP `classify_for_routing` path) and fires every hook `hooks/hooks.json` registers for the
  events it emits (SessionStart, UserPromptSubmit, PreToolUse/PostToolUse per tool call, SubagentStart, Stop, SessionEnd) in
  host order, under a scratch HOME and `LLM_ROUTER_HOME`; a registered hook that never ran is listed with the reason.
  Upstream is a local stub. Children run a scratch venv interpreter whose `sitecustomize` is `scripts/_replay_netguard.py`
  with the run's allowlist baked in, so any Python child, even one with an emptied environment, refuses every other
  connection and the run exits 1; non-Python network clients are shadowed on PATH (absolute paths are not covered).
  Leftover and detached processes are counted with a positive-control canary; "cannot tell" fails the run. Rows are
  tagged with the existing mechanisms (`LLM_ROUTER_SYNTHETIC=1`, `LLM_ROUTER_ALLOW_STUBS=1` -> `provenance=test`,
  `LLM_ROUTER_SESSION_KIND=harness`, `synth-replay-<hash>` session ids); no field was added. Writes `summary.json` /
  `summary.md` (counts and hashes only): per-door decisions and field presence, latency p50/p95 with n, ledger completeness
  per writer, P0.1-a archive checks and a pairwise door-agreement matrix with Wilson intervals. Synthetic results are
  mechanics evidence only and never organic. Fixture: `tests/fixtures/synthetic_sessions/`; tests: `tests/test_synthetic_replay.py`.
- ledger completeness (PLAN v16 P0.8-e): a call the semantic cache answers now writes a `usage` row
  (`reason = "cache_hit"`, provider `cache`, model `cache/<cached model>`, 0 tokens, $0) carrying the
  caller's session id, and `semantic_cache_lookups` gains a nullable `session_id` (additive migration;
  old rows stay NULL). Cache hits were previously invisible to per-session accounting. Spend and savings
  are unchanged. No `routing_decisions` row is added for a replay (it would double-count judge, bandit
  and offload inputs). Call counts now include cache hits as `usage` rows, so every surface that splits calls into
  paid / free / subscription excludes provider `cache` from all three (one definition,
  `llm_router/provider_classes.py`; hooks keep a checked `_CACHE_PROVIDER` copy): status-bar session
  calls, session-end paid rows, session-start weekly digest, claw-code session-end, the statusline
  `mix_local`/`mix_paid` segment, the `share` card (`paid_calls`/`total_calls`), `digest`, the
  `routing_health` rates (cache rows get no bucket there: excluded). The
  prompt-cache hit rate (`get_cache_savings`) leaves cache rows out of its denominator. Spend and
  savings are unchanged. Hook versions: status-bar 6 -> 7, session-end 26 -> 27, session-start 31 -> 32,
  session-end-clawcode 3 -> 4. Restart the MCP server to pick it up. See `docs/bugs/P08E-1.md`.
- hook latency rows carry the session (PLAN v16 gap PG9): `enforce-route`, `bash-compress`,
  `playwright-compress`, `cc-usage-track`, `subagent-start`, `usage-refresh` and
  `agent-depth-release` now call `hook_latency.set_session` with the `session_id` from the
  payload they already parse (null when absent, never invented; no prompt text; no extra stdin
  read). The statusline row and `agent-route` / `auto-route` / `status-bar` / `session-*` already
  did. The P0.9-g live clause of `llm-router kpi` now prints `n_sessions` and the largest-session
  share per hook, says "not informative (need >=2 sessions)" below two sessions and cannot PASS
  there (an over-budget p95 still FAILs). Hook versions: enforce-route 15 -> 16, bash-compress
  2 -> 3, playwright-compress 2 -> 3, cc-usage-track 3 -> 4, subagent-start 6 -> 7,
  usage-refresh 3 -> 4, agent-depth-release 5 -> 6. Rows written before the deploy have no session
  id and read as "not informative".
- kpi G1_proxy: classifier-shadow scheduling cost (PLAN G1-proxy "shadow <= 30 ms"; `docs/bugs/P09-13.md`).
  The proxy ledger row gains `tier_shadow_schedule_ms`, present only when a shadow call was scheduled
  (milliseconds, no request content); `llm-router kpi` appends `shadow schedule p50 / p95 (n, sessions,
  within/OVER 30ms target)` to G1_proxy from turn-first rows that carry it, "not informative" below
  n=100 or 2 sessions. `tier_decision_s` is unchanged. Needs a proxy restart to start writing the field.
- ledger completeness (PLAN v16 P0.8-d, D-32 = A; R-EVL-1, NFR-NUM): `usage` gains `reason`
  (additive migration `MIGRATE_USAGE_ADD_REASON`; earlier rows stay NULL and `llm-router kpi` reports
  them as missing, not back-filled). Every `usage` writer passes a short route code (`router_chain`,
  `router_budget_fallback`, `direct`, `explicit_codex_tool`, `explicit_gemini_cli_tool`, `route_tool`,
  `stream_tool`, `claude_code_subscription`); every `routing_decisions` writer sets `session_id` and
  `reason_code` (the router falls back to `router_chain` / `router_unhinted` when classification
  carries none; the sidecar backfill and judge-eval scripts set both). The kpi G3 per-writer check now
  reads `usage.reason` instead of marking it "no column"; "no traffic" still never turns G3 green.
  `cc-usage-track` hook version 2 -> 3. No prompt text is written to any ledger. A test enumerates
  every `INSERT INTO usage|routing_decisions` in src/, hooks/ and scripts/ so a new writer without the
  fields fails. Fixes `llm_stream`, which raised `TypeError` instead of writing its usage row
  (`docs/bugs/P08D-1.md`).
- proxy (D-31 = A, amended 2026-10-09; experiment arm): `haiku_arm_share` in `claude_tiers.yaml` (default
  absent = off) serves that fraction of eligible turns on Haiku even when the requested model is in
  `pinned_models`; every other pinned turn stays pinned. `haiku_arm_eligible` lists `task_type/complexity`
  patterns (`*` allowed for complexity; default exactly `[query/simple]`). Eligible means: a main-thread human turn
  past the first call (the `Agent`/`Task` tool present on every call), an organic or untagged session (headless,
  harness and research sessions never), no `opus:`/`/model` pin, no correction signal, and a body Haiku accepts.
  Assignment is `sha256(session_id|turn|salt)`, not a draw. In-share rows carry `tier_arm`, `tier_arm_assignment`
  (`treatment` / `treatment_retried_original` / `ineligible`), `tier_arm_reason` (`matched:<pattern>` or why not),
  `tier_arm_bucket` and `tier_arm_turn`; counts of Haiku-served rows must also require `served_model` Haiku. Needs
  `haiku_rewrite: true` (which also re-enables the ordinary Haiku rewrite for non-pinned traffic; the Haiku guard
  override turns both off). Read at proxy start: set the share to 0 or delete the key and restart to stop it.
  No hook changed. See `docs/proxy.md`.
- hooks (PLAN v16 P0.9-g, AMEND R8 A.3): `scripts/hook_wall.py` (`llm_router.hook_wall`) times the five
  sync hooks with no latency MUST (`enforce-route`, `bash-compress`, `playwright-compress`,
  `cc-usage-track`, `subagent-start`) from outside, process start to exit, cold and warm, with
  `os.getloadavg()` before and after every run (read outside the timed span). Payload fixtures in
  `tests/fixtures/hook_payloads/`. Each hook's `hook_latency.jsonl` row now carries `load1`, read after
  the clock stops. `llm-router kpi` prints a P0.9-g line per hook: wall cold p95 (n >= 200) AND live
  `elapsed_ms` p95 (n >= 200; 30 for `cc-usage-track` / `subagent-start`) against 300 ms, rows above
  load 4 excluded and counted.
- semantic cache (PLAN v16 P0.5-b, R-CTX-7; the semantic cache is the result cache, D-R8-5):
  `semantic_cache_lookups` gains `project_scope` (additive migration; earlier rows are reported as
  unscoped). `llm-router kpi` and `llm_router_status(view="cache")` print lookups, hits and n per
  project; a project with fewer than 20 lookups prints "not informative" and no percentage.
- doctor (PLAN v16 R8 P0.14-c, owner decision D-R8-4 = explicit only): `llm-router doctor --fix-routing`
  writes the one key `env.ANTHROPIC_BASE_URL` into `~/.claude/settings.json` (backup first, diff
  shown, y/N or `--yes`) only when proxy-default is installed and enabled, `routing_opt_out` is unset,
  the key is absent, no project settings file or environment value overrides it, and both the shim
  and the main proxy answer; otherwise it refuses with the reason. `--decline` sets
  `routing_opt_out`, which also silences SessionStart's "routing is OFF" warning. No hook or plain
  install runs it. SessionStart (hook v31) appends one `observe` row per session to
  `~/.llm-router/settings_writes.jsonl` (key presence, env sha256, mtime) and the repair appends a
  `write` row, so the next unexplained removal is bracketed.
- verifier PR C (SHADOW, opt-in): pending_verify queue, detached `verify_worker`, Codex marker. **Off unless
  `LLM_ROUTER_VERIFY=on`**; no outcome, NS, D1 or D2 changes. See `docs/VERIFIER.md`.
- agents (PLAN v16 AGT A.0): `llm_act`, `llm_delegate` and `llm_local_task` take `wait` (default
  True). `wait=False` returns `{"job_id": ...}` at once; `llm_router_session(action="job", id=...)`
  polls it (`running` / `done` / `failed`, with the tool's result). Jobs live in the MCP server
  process and are forgotten on restart; an unknown id answers `unknown_job`.
- proxy (PLAN v16 GE4, OD-4 = A): Frontier shadow (`shadow_frontier.py`), **off by default**
  (`LLM_ROUTER_SHADOW_FRONTIER=on` to enable). After a `tier_reason == haiku_rewrite` reply is relayed,
  a background task replays the client's original bytes to the requested model; caps in code: 20 calls
  and 400k input tokens per UTC day, paused at session >= 80% / weekly >= 85% / stale or unknown quota,
  100% of Haiku turns for 14 days then 2%, one in flight. `python -m llm_router.shadow_frontier judge`
  runs a blind A/B judge (isolated `claude -p`, `--no-session-persistence`, not via the proxy) into
  `shadow_frontier/verdicts.jsonl`, the file the Haiku guard's `shadow` trigger reads. Ledgers hold no
  prompt or response text; the pair text store is 0600 and kept 14 days.
- proxy (GE4 review fixes on #339): retention is now enforced when the shadow is off or paused
  (pairs and `ledger.jsonl` rows older than 14 days are pruned at proxy start and at the start of every
  judge run, before the flag/quota early returns); the daily 400k input-token cap admits a replay only
  if the Haiku call's input size x 1.35 fits (the replay is tokenized by a different model; a replay
  whose real count exceeds that ratio can still land over the cap by the excess, auditable from
  `est_input_tokens` vs `input_tokens` in the ledger); a truncated or aborted Haiku reply (no
  `stop_reason`, no SSE `message_stop`, cut JSON, client gone) is skipped (`cheap_incomplete`), not
  replayed or paired.
- proxy (PLAN v16 P0.11, owner decision D-20 = A): the Haiku guard (`proxy/haiku_guard.py`) runs in
  the proxy at start and hourly while `haiku_rewrite` is on. A trip (redo > 15% at n >= 30; audit
  batch < 75% at n >= 30; daily audit < 8/10 on 2 consecutive days; `tier_retry` > 1% at n >= 100
  Haiku-decided calls; shadow acceptable < 26/30 at n >= 20) writes `~/.llm-router/tier_overrides.json`
  and turns the live rewrite off. `ClaudeTierPolicy.load` reads that override after the YAML. The
  override can only turn the rewrite off, and the YAML is never edited. New
  `llm-router kpi --haiku-watch --since --until` prints every trigger with its n and exits 1 when a
  D-20 trigger has too little data to judge.
- proxy (v16 P1.7-d, owner decision 2026-10-08 option B): `proxy/shadow_text.py`, an opt-in text sidecar for the
  classifier shadow. With `LLM_ROUTER_SHADOW_TEXT_SAMPLE` set (`on` = 30% of turns, or a rate in (0, 1]; unset,
  `off`, `0` and anything else = off, nothing hashed or written) a sampled turn that gets a `classifier_shadow`
  record also appends `{text_sha, session_id, ts, context, prompt}` to `~/.llm-router/shadow_text.jsonl` (mode
  0600, at most 20 entries per UTC day counted from the file). The sample is a function of `text_sha` and the
  UTC day. Harness and headless sessions are not sampled. A text that matches a `secret_scrubber` pattern skips the turn; router banners (`⚡`, `ROUTE:`,
  `[llm_router]`, ...) are cut out; the text is written nowhere else. An offline labeller reads this file and
  deletes entries (older than 7 days, or labelled/skipped and from before today) under the same `flock`. This is
  the only place the proxy keeps prompt text; it is for labelling the shadow (`LLM_ROUTER_SHADOW_LABELS`).

### Fixed
- session-end / Stop (PLAN v16 PG4 / P2-G-3, bug `P09-SE-1`): two inline steps left the live p95 at 3,136 ms
  (n = 79 Stop rows, 2026-10-08T21:34Z to 2026-10-09T13:44Z, load1 median 2.6). The 0.2 s sleep after a spend
  "flush request" that nothing reads is removed (205 ms p50), and the north-star item (a scan of every transcript
  touched in two days: p50 343, p95 1,819 ms) is computed by the detached child and cached in
  `northstar_line.json`; the Stop shows the last cached share (the last child run: one turn old, more when Stops come
  faster than the 15 s child claim; none on a session's first Stop). The cache expires after 600 s, is keyed by the Stop's stdin session id, and its refresh runs
  before the usage fetch. Hook version 24 -> 26. Deploy: upgrade the `llm-routing` tool, copy `session-end.py` to
  `~/.claude/hooks/llm_router-session-end.py`, then judge on `hook_latency.jsonl` after >= 200 organic Stop rows.
- Haiku guard (HAIKU-SERVED-1): a Haiku rewrite that Anthropic refused and the proxy retried on the original
  model keeps `tier=haiku` / `tier_reason=haiku_rewrite` but is served on that model. `haiku_guard` now
  separates `is_haiku_decided` (router chose Haiku; the `tier_retry` trigger's denominator, retried rows
  stay in it) from the new `is_haiku_served` (`served_model` is Haiku), and `kpi --haiku-watch` reports
  `haiku_served_calls` next to `haiku_decided_calls`. GE4 Frontier shadow was already safe (its reply-time gate
  drops such rows as `not_rewritten`); a proxy test now pins that.
- hooks (PLAN v16 PG4 / P2-G-3, `docs/bugs/AUTOROUTE-LAT-1.md`): the auto-route hook no longer imports
  pydantic-settings, structlog + rich, the SDK, `importlib.metadata` or `urllib.request` on a routed prompt, and no
  longer probes Ollama synchronously at import. Wall p95 (hook_wall, n = 200 cold, load1 <= 3.7) 194.9 -> 124.8 ms;
  in-process p95 125.6 -> 73.1 ms. `llm_router.__init__` exports and `__version__` resolve on first use;
  `config_lite.config_value` skips `get_config()` when nothing can set the field; `get_logger()` is lazy with
  `configure_logging_lazily()` keeping structlog off stdout; `model_discovery.available_ollama_models_nowait()`
  uses a stale cache and refreshes it in a detached child (at most one per 300 s). On the default path the hook no
  longer runs `get_config()`'s key-export side effects (its drafts are free/local only). Hook version 49 -> 50.
- classifier (SYSONE-WARM-1): with `LLM_ROUTER_CLASSIFIER_BACKEND=systemone` the warm-up used `/api/generate`,
  which Ollama refuses for a decision model (HTTP 400), so the model never loaded and every verdict was `cold`.
  The warm-up now loads it through `/v1/systemone`; a refused warm-up is recorded once as
  `CHZ-FO-LOCAL-CLASSIFIER-WARMUP`; a unitless `LLM_ROUTER_CLASSIFIER_KEEP_ALIVE` (`-1`) is sent as an integer.
  See `docs/bugs/SYSONE-WARM-1.md`.
- `llm_local_task` `changed_files` no longer lists the router's own state directory (`LLM_ROUTER_HOME`: ledger/index DBs and
  their -wal/-shm sidecars) when it sits inside the task's workdir; excluded from both the git and walk snapshots
  (`docs/bugs/SLT-2.md`; main CI 37926623089).
- routing config (STALE-OLLAMA-1): `policies/standard.yaml` no longer names `ollama/qwen3:32b` (a default most
  installs never had) in any BALANCED, PREMIUM or REASONING chain, `workhorses` or `fallback_chain_complex`.
  Local models are injected at route time from discovery, so BALANCED/BUDGET routes are unchanged (chain per
  profile x task type compared against the pre-fix chains with a fake 3-model cache,
  `tests/test_stale_ollama_default.py`, n = 40 x 2 paths). Not unchanged: the session-start dynamic table was built
  straight from `ROUTING_TABLE` with no installed-model filter, so it carried the uninstalled entry (and the static
  RESEARCH chains too); on a machine that does have `qwen3:32b`, PREMIUM/REASONING lose it as their last entry.
  `filter_ollama_by_installed` now logs a dropped uninstalled model at WARNING once per model per process, and
  once when an empty discovery cache makes it skip validation; `get_model_chain` records a filter exception as
  `CHZ-FO-PROFILES-OLLAMA-FILTER` instead of a bare `pass`. See `docs/bugs/STALE-OLLAMA-1.md`.

### Changed
- kpi (`docs/bugs/O3-STEP-1.md`): O3 starts a human turn only at a row with `step_class == turn_first`
  (minus transcript sub-agent calls, as KPIS.md already says). Null-step rows (pre-GE1) and
  `subagent_first` rows are per-call units only: they no longer count as turns or end a redo window.
  They are counted and printed (`o3.excluded.no_step_class`, `o3.excluded.step_subagent_first`; keys
  added except that `o3.excluded.subagent_first` now counts only transcript-sidechain turn_first rows;
  `o3.excluded.step_side_call` counts rows labelled side_call without that tier_reason). A
  `subagent_first` row stays a turn when the transcript places it on the main thread. `subagent_first`
  is a live label, so the Haiku guard's `redo` trigger (no transcript join) changes behaviour: those
  rows no longer count as turns or push an escalation out of the redo window. The golden kpi files hold
  no O3 line and do not change.
- ci/test infra (TIMING-1, D-34 = A): a `timing` pytest marker for tests whose verdict is wall-clock speed (elapsed, p95,
  event-loop lag, short real timeouts, sleep ordering). 129 test functions in 54 files carry it; the parallel `test` job runs
  `-m "not timing and ..."` and a new serial `timing (3.11)` / `timing (3.13)` job runs them with `-p no:xdist`. Thresholds and
  assertions are unchanged. `LLM_ROUTER_RUN_PERF=1` now works (`conftest.py` read it after scrubbing it), so the 13 `tests/qa`
  performance budgets run in that job. `tests/test_timing_lane.py` guards the marker, the CI expressions, the tagged-count floor
  and new untagged clock assertions. See `docs/bugs/TIMING-1.md`.
- kpi (PLAN v16 P0.9-e, `docs/bugs/P09-11.md`): G1_proxy's turn-first segment counts only
  `step_class == turn_first` rows and measures the P0.9-e decision (sum of `tier_phases_ms` over
  `proxy.tiers.DECISION_PHASES`), not `tier_decision_s`. Null-step rows (pre-GE1 / side calls),
  `subagent_first` rows and turn-first rows without the decision phases are left out and counted on
  the line; they had filled the segment, so its p95 described rows that do not decide a turn.
  The segment prints n, sessions and the largest session's share, and `not informative` below n=100
  or 2 sessions. The continuation segment is unchanged; no other KPI line changes.
- agents (PLAN v16 AGT A.0): `agents.yaml` ships inside the package (`llm_router/data/agents.yaml`,
  loaded with `importlib.resources`). It lived at the repo root, which no wheel contains, so every
  installed user got `agent_not_found` (`docs/BUGS.md` A.0-1). A project `config/agents.yaml` and
  `LLM_ROUTER_AGENTS_CONFIG` still override it.
- proxy (PLAN v16 P0.9-e, `docs/bugs/P09-12.md`): less work inside the tier decision's timed phases,
  no decision changed. The Haiku body check (a `json.dumps` of the whole 0.5-2.8 MB body) now runs
  at most once per call and only when the decision considers the Haiku tier; the ledger's
  `tier_haiku_block` reuses its verdict instead of a second pass. The tier classifier skips the
  capability detector it never read, and the signal scoring runs exact case-sensitive twins of its
  IGNORECASE patterns. The proxy imports the classifier at start, not inside the first classified
  call. `scripts/bench_proxy_decision.py` (200 fresh processes, 2026-10-09, load1 4.4-5.0, synthetic
  corpus in the live turn-first mix): decision p95 11.0 -> 1.4 ms cold; classify p95 9.2 -> 1.3 ms,
  haiku_checks p95 5.1 -> 1.0 ms (n=90 classified calls each).
- agents (PLAN v16 AGT A.0): `run_delegation` (behind `llm_act` / `llm_delegate`) and
  `llm_local_task`'s agent loop and acceptance check run in a worker thread (`asyncio.to_thread`), so
  a long run no longer freezes every other MCP call (`docs/BUGS.md` A.0-2). `llm_local_task` runs
  still go one at a time, because the loop's write mode is process-wide `os.environ`. A queued run
  takes its file snapshot and starts its budget clock only once its turn comes, so it never reports
  another run's edits as its own and its wait is not charged to its budget; the result carries
  `queued_s` (`docs/BUGS.md` A.0-3). `wait=False` with a missing `workdir` answers `blocked` at once
  instead of returning a job id.
- proxy (v16 P1.7-c): the classifier shadow's `cls_input.assemble` reads the history backwards, stops once it
  has its context and never reads more than 400 messages back. Same input as before whenever the context lies
  in that window (600-case fuzz against the old walk); it no longer holds the GIL long enough to delay
  concurrent continuations (`docs/BUGS.md` P1.7-c-1). Shadow records gain `requested` (the call's real
  requested tier), `backend` and `assemble_capped`; `kpi` gains a `classifier shadow vs rules [model]` line
  (C-lambda2, M1-12 clamp-aware cost, under-route, Haiku precision, n; truth from
  `LLM_ROUTER_SHADOW_LABELS`). Shadow stays off by default. `scripts/bench_shadow_continuation.py`.
- routing (M3.0, owner decision D-14 = A; local models were later re-inserted as a fallback by QA-LAST-RESORT-1, D-114 to D-116): `route_and_call` no longer serves a Q&A task type from a
  local provider. For `northstar.QA_TASK_TYPES` (query, research, generate, analyze and the other Q&A
  types) Ollama and OpenAI-compatible local servers are removed from the chain in `route_and_call`
  (after the specialist and bandit steps, before the daily-cap step) and from the emergency BUDGET
  chain; the next provider in the existing order serves the call. Every `route_and_call` caller with a
  Q&A task type is covered, not only MCP `llm()` (list by file, grep `route_and_call(`):
  `tools/text.py` (`llm_query`, `llm_research`, `llm_generate`, `llm_analyze`, `llm_reason`,
  `llm_text_job`), `tools/routing.py` (402, 589), `tools/agentic.py`, `tools/fs.py` (QUERY calls),
  route_server/gateway, orchestrator, `context.py` (compaction summary), `quickstart.py`,
  `commands/benchmark.py`, `tui/cli.py` and `integrations/agno.py`. Accepted D-14 = A side effect:
  `llm_text_job` (opt-in `LLM_ROUTER_LOCAL_TEXT_JOBS=1`, task type GENERATE) no longer runs locally
  once a cloud provider is configured; at merge base aceb366 its chain dispatched to
  `ollama/qwen3.5:latest`, at this head to `openai/gpt-4o` or `gpt-4o-mini`. The flag is unset on
  the owner's machine, so nothing changes live today. `code` is unchanged and can still go
  local. An explicit `model_override` is honored, and an install with only local providers keeps them
  (no empty chain). The shared `_build_and_filter_chain` is NOT changed, so the proxy
  (`proxy/backends.py` `policy_chain` -> `choose_model`) routes exactly as before
  (`tests/test_mcp_qa_no_local.py` pins it). Reason: real Q&A prompts, local qwen 4/37 acceptable
  vs Sonnet 34/37 (PLAN 0.3 [RX]); 65 local Q&A answers in 7 days [U]. Only processes that run
  `route_and_call` and are started after the deploy pick it up (uv-tool MCP servers).
  `tests/test_mcp_qa_no_local.py` (30 tests; 6 mutations red, including the strip moved ahead of the
  subject specialist). The set of stripped providers is `types.LOCAL_PROVIDERS` (ollama, lm_studio,
  vllm, llamacpp) plus `openai_compat`, derived in one place (the former second literal set in
  `router.py` is gone; `test_the_strip_set_is_derived_from_types_local_providers` pins it).
  Scope, stated plainly: this is the `route_and_call` path (and its emergency BUDGET chain). The
  proxy's `policy_chain` -> `choose_model` -> `_build_and_filter_chain` does not call
  `route_and_call` and is not changed.
  Daily-cap interaction (D-14 intent): the strip runs BEFORE the TQ-007 daily-cap step in
  `route_and_call`. Once a daily spend cap is hit, the cap step confines the chain to the free
  providers (ollama, codex, gemini_cli); for a Q&A task type Ollama has already been removed, so
  Ollama is no longer the free fallback for Q&A task types. Only codex or gemini_cli can serve a
  capped Q&A call. If neither is in the chain: `hard` blocks; `smart`/`soft` fall through to Claude
  only when an anthropic model is in the chain, otherwise the call blocks (a paid non-Claude
  provider is never called once the cap is hit). `code` still falls back to Ollama under the cap.
  `tests/test_mcp_qa_no_local.py` pins the order (capped Q&A, chain [openai, ollama]: blocked, not
  served by Ollama) through the TQ-007 `_run` harness with `TaskType.QUERY`; the mutant that moves
  the strip after the cap step turns 3 of those tests red.
- status line: the default is the full layout again, byte-for-byte as before #273 (owner decision,
  reversing #273's fast-by-default; `tests/test_statusline_default_full.py` compares it with the
  pre-#273 script on 9 fixture states). The fast line is the opt-in debug mode
  `LLM_ROUTER_STATUSLINE=fast` and now shows the Claude 5h / weekly / Sonnet quota.
- statusline (PLAN v16 P0.9-c, bug `P09C-1`): the full status line is a cache read. Its sixteen
  subprocesses per render (twelve `python3 -c`, a whole-transcript scan, `import llm_router` for the
  money figure, `sqlite3`, an interpreter probe loop) moved into one detached refresher,
  `statusline_segments.py` (shipped beside the hooks as `llm_router_statusline_segments.py`), which writes
  `statusline_seg_<session>.kv` (TTL 30 s; also refreshed when the transcript is newer than the cache). The
  script parses the session JSON with bash regexes (any backslash falls back to one python), reads the
  cache with `read`, and prints; output is byte-identical to the previous script on the default-layout
  fixtures (`tests/test_statusline_default_full.py`). A cache older than 90 s is printed with a
  `cached <age> ago` marker; the first render of a session still computes synchronously. The context figure
  now reads the transcript from its tail. Cache values are digit-checked before any arithmetic; the interpreter probe runs only in the
  detached child; the synchronous first-render call is rate-limited (5 s) and a missing cache prints
  `segments pending`. Deploy `llm_router_statusline_segments.py` before the script. Measured with `scripts/statusline_wall.py` on one machine,
  2026-10-09, a copy of the live state (11 MB transcript, 1,637 last-route files): wall p95 22 ms cold
  (n = 219, load1 median 3.6) against 509 ms for the previous script (n = 190, load1 median 3.6); first
  render of a session 224 ms (n = 220). Not a live number: the PRD verdict is judged from
  `hook_latency.jsonl` after deploy.
- CLS B-A3 (`scripts/cls_attach.py`, no frozen file touched): the GE3 Sonnet labels (`--ge3-sonnet-labels`) get the same zero-join refusal (exit 2, counts only, before record recovery and any write) and below-50-percent stderr WARNING as the Opus labels; the `read_ge3`/`read_e2`/`read_e1` defaults are now `text_sha`, matching the CLI; the GE3 label flag help names the `text_sha` default and `key` as opt-in; a new `session_from_record_ts_mismatch` counter discloses items whose session came from a record with a different timestamp (counts only, no behaviour change). Tests: default-join acceptance and zero-join refusal for GE3 and E1, the 10/20 versus 9/20 warning boundary, and the GE3 Sonnet zero-join refusal.
- CLS B-A4 (`scripts/cls_attach.py`, no frozen file touched; #716): a GE3 attach (`--ge3-items`) now requires `--ge3-sonnet-labels` and is refused with exit 2, flag names only, before any source read or output write (PREREG-CLS-DEV reuses the 342 GE3 Sonnet labels for all GE3 items); the help text for the e2/e1 label flags no longer says the label file may be the sample/tasks file itself, it says an inline label file needs `--<set>-label-join key` (and `--<set>-label-tier truth`), and the `_read_keyed` default join is `text_sha`, matching the CLI; the `session_from_record_ts_mismatch` counter compares parsed UTC values and skips records without a timestamp (raw strings are compared only when one does not parse), so a different spelling of the same instant is no longer counted as a mismatch.
- Add the P1.8-c / D-90 `test-windows-claude-creds` CI job on `windows-latest` with a 10-minute timeout, pinned checkout and uv setup actions, uv setup retry, Python 3.11, and development dependencies. It runs only `tests/test_claude_creds.py` with Bash and xdist disabled to keep cost low, and is not a required check.
- The auto-route hook's latency budget is 750 ms (router-added p95), an owner-ratified deviation from the PRD's 300 ms (D-217): zero-Claude edits wait on Ollama metadata. Other hooks keep their bars. (D-217)
- F14 (D-159, D-165): the P2.4 capability filter now also runs on `route_and_stream` (vision marker, window; a filter-emptied chain raises `ValueError` before any event), `execute_agent` (the agent loop needs tools, so a model known to lack tools, vision or window is not attempted; this also covers subagent delegation and the SDK agent path, and an emptied chain returns `None`), and the proxy `--serve local-agent` pinned path (a pin known to lack what the request needs keeps the call on Claude, `policy_kept`, as `choose_model` does for a pin). Native `/route` refuses image parts in `prompt` and the `images` field with HTTP 400 like the other gateway routes; images only in `messages` history are still flattened. Behaviour with no detected need is unchanged. `docs/empty-chain-behaviour.md` gains the filter-emptied table for every door, checked by `tests/test_f14_capability_coverage.py`. Known, pre-existing and not changed: `route_and_stream` raises a stray `ValueError("Unknown media task type")` after its `route.aborted` event when every model fails.
- F41 / F38: the router's local model comes from the user's configuration, never from code. New `model_discovery.local_model()` resolves the user's `LLM_ROUTER_OLLAMA_MODEL` setting (`~/.llm-router/.env`) when that model is installed, else the first model in the discovered inventory (discovery order, no family preference), else None and callers skip local; a setting that is not installed is skipped and the reason recorded (`last_skip_reason`). The hardcoded `qwen3.5:latest` fallbacks are gone from `hooks/auto-route.py` (hook version 62 to 64, both copies), `hooks/session-start.py` warm-up (36 to 38, both copies), `chain_builder._ollama_models` and `zero_claude_edit.edit_model()`. The shipped policies (`standard`, `cost_aggressive`, `routerarena_tuned`) name the new `ollama/@local` token instead of `ollama/qwen3.5:latest`; the policy loader resolves it through `local_model()` and drops it when no local model is discovered. `safe_config` writes an empty `ollama_budget_models` unless discovery found some, and `model_evaluator` benchmarks the installed models rather than a fixed list. The Ollama watchdog `gate()` no longer runs a 1-token `/api/generate` on the prompt path: it reads persisted state only and spawns a detached, rate-limited `python -m llm_router.ollama_watchdog --probe`, which generates only on `local_model()` and only when it is already resident, so a probe never loads a model; no probe is spawned in a storm and a not-resident model reports `ok=None` (unknown, not a failure). Hung detection (`fail_n` failures flip, one success clears) is unchanged; a hang is now noticed one background round later. Cloud chains, tier policy and the promote gate are untouched.
- The Stop hook (`session-end.py`, version 37) no longer renders the Rich summary box, queries the session rows or the cumulative table on the default one-line path: it builds the line from a cached executed-routes count and the quota. The full box is rendered by the detached child into `last_summary.ansi` (new step `_render_last_summary`), and `rich` and `urllib.request` are imported only where used. `LLM_ROUTER_STOP_HOOK=full` is unchanged.
- L2-SE-1 (D-171): the Stop hook renders its ledger-wide figures from a cache the detached child writes, and imports no pydantic. Before, every Stop ran the full-table aggregates itself: five `query_window` windows, `summary("today")` and `summary("lifetime")` (each repeating `query_window`, `query_primary_metric` and `query_model_savings`), the lifetime unverified note and the 14-day series, some of them 2 or 3 times, so its cost grew with `usage.db`. The child (`_refresh_ledger_cache`, a new `_STOP_BACKGROUND_STEPS` entry) now computes each aggregate once and writes `ledger_aggregates.json`; the Stop reads it. Behaviour change: those figures (condensed `today`/`lifetime`, the savings panel, the 14-day panel, the lifetime unverified note) are one turn old, and a first-ever Stop, or one whose cache is older than an hour or from another local day, shows the line without them. Output is byte-identical to the previous hook when the cache is fresh. The Stop no longer collects `efficiency`, `overhead`, `cache_stats` or `savings_by_task` (four full-table scans whose results nothing read). The two host prices come from `pricing.savings_baseline_rates()`, `production_only` moved from `cost` to `savings` (`cost` re-exports it), and the subscription flag and routing policy go through `config_lite`, which now reads a state or cwd `.env` for the one key instead of taking the pydantic path whenever any `.env` exists. Hook version 34 to 35. Evidence: `tests/test_l2_stop_ledger_cache.py`.
- `llm_text_job` now runs locally without `LLM_ROUTER_LOCAL_TEXT_JOBS=1` when a fresh, digest-verified, settings-matched, enabled and not-demoted `local-check` PASS covers the job (D-272; same guards as `prefer_passed`). `=1` keeps the opt-in (jobs run without a pass); `=0`/`false`/`off` turns all local text jobs off. `local-check run` and `status` print "measured; routing for this runtime is not supported yet (Ollama only in this release)" after a judged PASS on a non-Ollama runtime instead of `routing: on` (D-274). The PREREG-Q37 provenance in `recommendations.json` now reads "overlaps bank v1 draw 1; in-sample" (wording only, no number changed).
- door (5) agent-route: DIRECT sub-agent runs (LLM_ROUTER_SUBAGENT_DIRECT=on only) build their context with build_pack(door="agent"): the pack system layers go to the executor as context, the recent turns as history, and the sub-agent prompt is passed verbatim; any pack error keeps the previous behaviour. Hook version 21 to 22.
  door (5) agent-route privacy: the pack is gated on the most external non-free provider in the chain (execute_chain hands history to every fallback model); execute_chain gains okf_inject so the pack path injects once.
- door (4) codex: run_codex builds its prompt with build_pack(door="codex") and accepts messages/transcript_path/target_window; a missing codex rollout file falls back to the caller's messages.

  Behaviour change: callers that pass neither working_dir nor context_root (tools/codex.py, session_broker, model_evaluator, resolver/calibrate) used to get the current repo's state added by inject (~204 chars in a git checkout); they now get nothing added. CODEX_TARGET_WINDOW=200_000 is unsourced. Session content is gated by target_provider="codex"; note session_store.LOCAL_PROVIDERS includes codex, so privacy mode local lets session content reach Codex once a caller passes messages (owner decision pending).
- door (1) hook: DIRECT drafts build context once with build_pack(door="hook"), preserve the request verbatim, relay tool blocks to text and agent executors, and retain the previous context path on pack failure. Hook version 56 to 57.
  door (1) hook privacy: gate transcript content on the first chain provider outside session_store.LOCAL_PROVIDERS (else local; D-91, D-95), honour HISTORY_RELAY=off, include relayed turns in grounding material, and disable executor injection when the pack's project layer already ran inject().
  agent paths (tool loop, read-only draft loop) skip a second project-knowledge injection when the pack already carried it: execute_agent and run_agent_loop take okf_inject (default True).
- MCP text completions reuse the turn’s injected project block, gate session content per attempt, and preserve explicit caller context alongside transcripts.
  Transcript attempts add only bounded earlier-session summaries; missing transcripts retain accumulator context, and pack failures keep the original prompt.
- PLAN v16 P1.1 door 3: the proxy local tier (`proxy/backends.py` `_trim_fast`) now builds its history with `context_pack.build_pack(door="proxy", target_window=<the local model's num_ctx>)`. Whole conversation (native) only when the native Ollama payload estimate fits both the window budget and `LLM_ROUTER_PROXY_FULL_CAP` (default 6000 estimated tokens; cold first-token time grows superlinearly with prompt size). Otherwise the last 7 messages are sent as text, with tool output over 4,000 chars elided to head 1,500 + tail 1,500, plus the newest exchange native and the request verbatim; D-92 (interim, until the P1.2 rolling summary): pack mode also pins the conversation's first user request verbatim when the native payload with it fits the window. Replaces the first-turn + last-exchange cap; with `LLM_ROUTER_SESSION_CONTEXT=off` the local tier gets no history.
- repo state reads are cheaper (PLAN v16 P1.1, context-pack 50 ms budget): `repo_facts.collect` runs two git calls instead of five (`status --porcelain --branch --no-ahead-behind` and one `log` line); it needs git >= 2.17, and on an older git it now returns no repo state instead of answering. New opt-in `repo_facts.render(root, reuse=True)` (via `context_injection.inject(..., reuse_repo_state=True)`) reuses the block for the same root while `.git/HEAD`, `.git/index` and `.git/logs/HEAD` are unchanged, for at most 10 s; only the context pack opts in, so every existing caller still reads git on each call. Under reuse an untracked file or unstaged edit can lag by up to 10 s. Benchmark `scripts/bench/context_pack_bench.py` (synthetic, Apple M5 Pro, 2026-10-09, one run, n=100 each), door shape = 10 MB transcript + temp git repo + CLAUDE.md + semantic default: before (5 git calls, no reuse) p50 45.78 / p95 51.66 ms; after, every call reading git p50 32.05 / p95 34.84 ms; after, reuse hit p50 17.23 / p95 19.62 ms. Transcript only: 10 MB p50 0.71 / p95 0.78 ms; 1.99 MB parsed whole p50 5.61 / p95 6.66 ms.
- P1.8-c: Claude OAuth credentials are read in one module, `claude_creds.read_oauth()`: the macOS keychain (`Claude Code-credentials`) first, then `~/.claude/.credentials.json` (nested `claudeAiOauth` and flat shapes) on every platform. The ten readers (`tools/subscription.py`, the `auto-route`, `session-start`, `session-end` and `usage-refresh` hooks and their `src/llm_router/hooks/` mirrors, and `.claude/hooks/auto-route.py`) now call it, so Claude usage refresh works on Linux and Windows hosts that keep the token in the file. `llm_refresh_claude_usage` reports one "could not read credentials" message instead of separate timeout / not-found / parse messages. The installer copies `claude_creds.py` next to the hooks as `llm_router_claude_creds.py` for interpreters without `llm_router`. `git grep find-generic-password -- src hooks .claude` finds only `claude_creds.py`. Evidence: `tests/test_claude_creds.py`.
- P2.4: the capability filter is on by default and excludes, never reorders. A chain entry known to lack tools, vision (images present) or window (system prompt + newest turn + tool schemas over 90% of the model's window; history is not counted, packs adapt to each window) is removed at the router/MCP/gateway door (the last transform before the empty-chain check, and on the emergency BUDGET chain), the hook and SDK door (`execute_chain`) and the proxy door (`choose_model(needs=)`, which also rechecks a pin against the filter). The tools need is hard only when the request body carries a real tools array (today: the proxy door; the gateway refuses a `tools` field with HTTP 400, as before). The prompt-text tools regex never excludes: it trips on ordinary prompts such as "What is the current version of Python?", so its verdict is only traced as a `capability.shadow` event naming the models it would have excluded. An Ollama-only box therefore no longer raises "No providers available" on such prompts, and unpinned research keeps perplexity/sonar in its chain. Vision and window stay hard. An unknown capability never excludes, and an explicit pin is not exempt from vision or window (the error names the capability). `LLM_ROUTER_CAPABILITY_ROUTING=0` turns the filter off. Not filtered: `route_and_stream`, and the agent-loop branches (`execute_agent`, which the hook and SDK take when `needs_claude_tools` is true, and agent-route's subagent delegation); tracked as F14. Behaviour changes: the default flip also makes `needs_claude_tools` (hook, agent-route, SDK) answer from the 8-bit capability vector instead of the legacy boolean, so a prompt the vector flags and the legacy boolean does not (symbol, repo-search and git prompts; four are pinned in `test_default_on_widens_needs_claude_tools_documented_consequence`) now takes the agent-loop branch instead of `execute_chain`, and `routing_decisions.capabilities_json` is written on every decision. The gateway now refuses image parts in the newest user turn with HTTP 400 on every completion route instead of silently dropping them (same class as the `tools` refusal); image parts only in earlier turns keep the previous handling: the history is flattened (the context pack renders each as `[image]`) and adds no vision need. `docs/empty-chain-behaviour.md` documents what each caller does when every map-known provider is disconnected, and `tests/test_p24_empty_chain_table.py` pins the table to the behaviour. Evidence: `tests/test_p24_capability_hard.py` and `tests/test_p24_empty_chain_table.py`. enforce-route (hook v18) keeps the legacy needs-tools detector for its filesystem exemption whatever the flag says, so the default flip does not weaken enforcement. An Ollama model whose /api/show reports no capabilities list is unknown (kept); only an explicit list lacking `tools` or `vision` excludes it (new additive map field `capabilities_reported`).
- One write policy now covers `llm_act`, `llm_local_task` and the toolkit (P2.9-a). `write_policy` gains `contain` (the single path-containment test, symlinks followed), `check_write`, `check_action` (the shell tripwire plus a destructive table: `rm -r`, git push/reset/checkout/clean and the rest of the existing git table, package installs, `sudo`, `mkfs`, `dd`, `chmod -R`), and `pick_root` (MCP root, then `CLAUDE_PROJECT_DIR`, then the caller's request; never the process cwd). Destructive commands need approval and approval is not wired, so they are refused; this includes commands that were allowed under `LLM_ROUTER_AGENT_COMMANDS=all`. `llm_local_task` takes an optional `ctx` and refuses a `workdir` outside the project root when one is known (no known root: the workdir is the root, as before); its result reports `root_source`. This is a static tripwire, not a sandbox: R-LOC-7 stays partial. The toolkit lane is unchanged apart from the shared policy (shadow-first, D-12). Not covered: the secret deny-lists and the Codex tier-1 sandbox stay lane-specific. Plainly: `llm_act` (tier 0) has no commands switch, so it now refuses mutating git (push, reset, checkout, clean, commit, pull, stash push/pop, tag <name>, branch -d, config <key> <value>) and install commands (pip, npm i/ci/install, yarn, pnpm, uv add/sync/run pip, pipx, brew and others); read-only git (status, log, diff, show, stash list, tag -l, branch -a, remote -v, config --get/--list, rev-parse, ls-files, blame) and `npm run` still work.
- P3.4-A (D-194): one definition per ledger metric. New stdlib-only `llm_router/ledger_view.py` holds the window table (session, today, 6h, 24h, 7d, 14d, month, lifetime; all bounds in local time, closed at both ends), `executed_by_provider` (runtime-provenance real `routing_decisions` plus `proxy_calls.jsonl` rows, counted once, dedupe key `trace_id`), `local_paid_mix`, `saved_usd` and `session_scope`. `dashboard_data.query_window` / `summary`, `routing_report.executed_routes` (F23 `local_by_cause` kept), `cost.get_savings_by_period`, `llm-router summary` and kpi's O1 now delegate to it; `Summary.ledger_line()`, `llm-router summary` and the kpi scorecard print the same `ledger <window>: ...` line. `savings.SURFACES` gains a `ledger_view` flag. Number changes on these surfaces: (1) `week` is the rolling 7 days everywhere (`get_savings_by_period` was since-Monday, `llm-router summary` was a lineage window); (2) `month` starts at local midnight of the 1st (was UTC); (3) window edges are exact on both ends and `T`/space timestamps compare as instants; (4) executed counts include proxy ledger rows (served = `ollama`, forwarded/fallback = `anthropic`); (5) `usage` cache rows are no longer counted as calls in `query_window`; (6) `llm-router summary` verified/unverified dollars now cover `usage` and the platform tables, not only `savings_stats`, and no longer depend on the 5000-row lineage cap; (7) `get_savings_by_period` reads read-only through `ledger_view` and leaves out error and cache rows.
- context pack, cloud doors (owner decision D-101): a cloud model's pack window is now its registry context window, capped at 65,536 tokens <!-- provenance: exempt (config constant, not a result) --> (`context_pack.CLOUD_WINDOW_CAP`).
  - The window comes from `model_registry.context_window`, read from `config/models.yaml`. It is matched by exact id, else by the longest registry id the model extends, else by the model's family. The family rule applies only when every family member's window is at least 65,536, so every member gets the same capped value. That rule is how the routed `anthropic/claude-sonnet-4-6` resolves. A family with a smaller member (such as `groq/llama`, which has an 8k member) leaves an unlisted model on the old defaults.
  - Before, the MCP door used 8,192 for every cloud model, and the gateway and SDK used 32,768.
  - A conversation of up to ~52k tokens (0.8 x the 65,536 cap, `context_pack.FULL_FRACTION`) <!-- provenance: exempt (derived from config constants) --> now goes whole to a known cloud model. Beyond that, the pack (the last 7 messages plus the rolling summary) applies as before.
  - Unchanged: models the registry does not know keep the old defaults, local models keep their `num_ctx`, and the claude-CLI dispatch path keeps 32,768.
  - A wheel install ships no `config/models.yaml`, so most current models keep the old windows there.
  - Known limit: an unlisted legacy model in a large-window family (such as `openai/gpt-4` with an 8k window, or `openai/gpt-3.5-turbo` with 16k) also gets 65,536. Its provider rejects an oversized prompt and the router fails over. None of these models is in the routing table.
  - Expect larger cloud prompts: up to ~52k input tokens per call.
  - New: `scripts/g1_synthetic_evidence.py equivalence` (PREREG-P11B-synthetic-R2), the $0 proof for the P1.1-b Sonnet stratum at the MCP door, with no model calls (D-102).
- PLAN v16 P1.1 door 7: the gateway's completion endpoints (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/api/chat`, `/api/generate`) no longer flatten the message list into one `role: text` prompt. The trailing user turn is sent verbatim as the request and the turns before it go to `route_and_call(messages=..., context_door="gateway")`, which builds the context with `build_pack(door="gateway")` for each provider attempt. As on the SDK door, history is sent only when `session_store.allows_session_content(provider)` allows it (`LLM_ROUTER_SESSION_CONTEXT=local` keeps it off API providers, `off` sends none), a cloud attempt keeps the whole conversation up to 0.8 x a 32,768-token gateway window (`_GATEWAY_NON_LOCAL_WINDOW`, owner decision; the SDK and MCP doors stay at 8,192) and otherwise its last 7 messages, and a pack error records `CHZ-FO-ROUTER-GATEWAY-PACK` and sends the request alone. Gateway calls no longer read this process's own session into the prompt. A request without a trailing user turn (an assistant prefill, a system-only request) is still sent flattened, with no history. `route_payload` accepts an optional `messages` list (a non-list is a 400); without it, the native `/route` is unchanged. New `tests/test_context_pack_doors.py` checks that `context_pack.DOORS` is exactly the seven doors, that each has a `build_pack` call site and its own door test file, and that an unknown door is rejected. History items that are bare strings (Responses `input`) or carry another role (`function`, `tool`, role-less content parts) become user turns labelled with that role, since providers reject unknown roles. The `function`/`tool` roles used to go through as raw provider roles, and bare strings were dropped.
- PLAN v16 P1.1 door 6 rework (D-98): `llm_router.route(messages=...)` no longer goes through `route_and_call`. The history now feeds `build_pack(door="sdk")` inside the SDK's own `build_chain` + `execute_chain` (or agent-loop) call, so model choice, the local agent loop for file tasks, `timeout` and the `host="sdk"` ledger rows are the same with and without `messages`. The pack is sent as the executor's `history` (text chain) or folded into the agent loop's context, and only when `session_store.allows_session_content` allows the most external provider the executor will actually call (it never calls `claude` entries; the agent loop calls ollama only), because a fallback model receives the same context (privacy mode `local` = on-device providers only, D-91/D-95). The turns are reshaped for chat APIs: other roles become labelled user turns, consecutive same-role turns merge, leading assistant turns are dropped. A pack failure records `CHZ-FO-SDK-PACK` and sends the request alone. Behaviour that now matches the plain path for `messages` callers: `timeout` caps each model call (it was a wall-clock cap via `route_and_call`), and a spend cap skips paid providers and ends in `RoutingError` (it raised `BudgetExceededError`). `route_and_call`'s `context_door` default is now `"gateway"`, the only door that still uses it (D-43(c)); the router-level `messages=` tests moved to `tests/test_route_and_call_messages.py`.
- SDK route(messages=) and route_and_call(messages=) build context via build_pack with door="sdk".
  Conversation content is privacy-gated separately for each provider attempt.
  The caller's history also reaches the codex, gemini_cli and Claude-subscription CLI providers, folded into their prompt as a labelled background block (it used to be dropped there). It is T4-M1 redacted like the prompt, and counted in every budget, cap and cost estimate. It is sent even with `context_enabled=false`, which now governs only the router's own project and session context. When a history is given, the semantic-cache key no longer mixes in the process session buffer. `sdk.route(messages=)` honours `timeout` (as `max_wall_clock_seconds`) and drains the turn's background tasks before returning. It lets `BudgetExceededError` through unchanged and wraps `ValueError`, `TimeoutError` and other `RuntimeError`s in `RoutingError`. It writes a host=`sdk` savings row. File and tool tasks with `messages` are text completions; they do not get the local agent loop.
- proxy door follow-up (owner decision D-94(a)): the `fast` trim, which builds the context pack and estimates tokens over the whole prompt, now runs in a worker thread instead of on the proxy's event loop, bounded by the step budget (a stuck thread pool is a `budget_exceeded` fallback, never a hang), and its time is recorded as the `trim` phase in `tier_phases_ms` (outside the P0.9-e decision sum). `context_pack.build_pack` gains `query=`: the retrieval text for the project layer (OKF, semantic pack) when the request is not the person's ask, such as a continuation step whose request is a tool result. The proxy still attaches repository knowledge through `proxy.okf_context.attach` (D-94(a) keeps it), so no door passes `query=` yet.
- Ledger line (P3.4-B1, `ledger_view`): the subscription/paid split now comes from row evidence and discovered seats instead of a hard-coded provider list. User-visible number changes on `llm-router summary`, `dashboard_data`, and the kpi ledger line: (1) proxy calls made with an OAuth login and providers backed by a discovered seat now count as `subscription` (they were `paid`; a Claude Max user saw "subscription 0 / paid N"); (2) a provider with no row evidence, no seat and no API-key evidence is shown as a separate `unclassified` count (printed only when non-zero), never folded into paid or subscription; codex and gemini_cli count as subscription only when `seats.json` shows their login; (3) unknown parts print `n/a` instead of `0` (`est. saved n/a` when there is no usage database); (4) new latency figures p50/p95/p99 over `routing_decisions` rows with a recorded latency (NULL or 0 is not recorded; all-NULL is n/a with n_recorded=0). API: `window_summary(since, until, scope, ...)` (the `window_summary("24h", now=...)` form still works), `Figure`, `show()`, `LedgerWindow` v2 with `to_json`/`from_json` and a `sources` map; `SUBSCRIPTION_PROVIDERS` is removed.
- P3.4-B2 (D-218): the full status line prints ledger figures from one `ledger_view.window_summary` per refresh, each with its n and window. User-visible changes: (1) route mix `⚖` is now this SESSION's executed calls (was usage rows of a wall-clock rolling 6 h) and reads `⚖ 3L/4S/1P·session n=16`; L local, S subscription, P paid, and an unclassified count `·8U` appears only when non-zero (provider classes by evidence, proxy rows included, so codex/gemini seats are no longer counted as paid); when the session id is unknown the window reads `session~` (by timestamp since session start); (2) money `💰` is today's estimated saving and reads `💰 ~$5.70 est·today n=4` (was `~$5.70 est.` with no n or window), shown only above the existing one-cent floor; (3) last route `🔀` keeps its route, staleness mark and token count and appends the ledger `(n=16 session)`, or `(n/a)` when the ledger is unknown; with no last-route file the segment reads `🔀 n/a` (was hidden); (4) a mix or money whose ledger is missing or unreadable is omitted, never 0; (5) the mix no longer depends on the wall clock (the refresher passes its own `now`). New cache keys in `statusline_seg_<session>.kv`: `mix_sub mix_unc mix_n mix_win ledger_as_of money_n money_win`. The deployed `llm_router_statusline_segments.py` and `statusline-command.sh` must be re-copied (`llm-router install`) to pick this up. Not changed: the tick's NS segment stays on the kpi scorecard (same 7 d unit stream; wiring `ledger_view.ns_strict` is left for a later PR).
- P3.4-B3 (D-218): the status-bar hook (hook version 7 -> 8) prints ledger figures from `ledger_view.window_summary`, each with its n and window. User-visible changes: (1) money windows are now LOCAL time: `D:` is today since local midnight (was a UTC day), `W:` is the rolling last 7 days (was Monday 00:00 UTC to now), `M:` is the local calendar month (was a UTC month); each reads `D:$1.42·today n=4`, `W:$9.88·7d n=31`, `M:$41.15·month n=120` (was `D:$1.42 W:$9.88 M:$41.15` with no n or window); (2) the figures are the ledger's estimated saving (all sources, simulated/error/cache rows excluded, proxy rows included) instead of a sum over usage rows only; (3) the baseline label in full mode is the priced baseline model, `(vs claude-opus-5-5)` (from `pricing.savings_baseline_model()`; honours `LLM_ROUTER_SAVINGS_BASELINE`), no longer the month's baseline dollars `(vs Opus:$58)` from a private Opus price that resolved to $0 when `llm_router` was not importable; (4) the efficiency multiplier (`14.2x` / `eff 14.2x`) is removed: it was a ratio over the hook's private baseline dollars, which the ledger does not expose; (5) full mode's session call counts are the session's mix: `sub:4 free:3 paid:1 unc:8 n=16 session` (`free` = local; `unc` = executed calls with no evidence of subscription or paid, shown only when non-zero; window `session~` when the session id is unknown and the count is by timestamp); codex and gemini calls are classified by evidence and seats (no longer counted free), and proxy rows are included; (6) an unknown figure prints `n/a`, never `$0.00`: `D:n/a` when there is no ledger or no usage database, and `💰 n/a` for a copy of the hook without the `llm_router` package (it opens no database). The status line is still read from `status_bar_cache.json` on the prompt path; the detached refresher computes it (three ledger windows in full mode, two in compact). The deployed `~/.claude/hooks/status-bar.py` must be re-copied (`llm-router install`) to pick this up.
- The one-line Stop (session-end hook v39) no longer presents an old ledger count as live. `ROUTING 24h N executed` now prints `(as of HH:MM)` (local time) once the cached count is older than 5 minutes, and `n/a (no ledger yet)` when there is no cache from today; the Stop no longer runs a live `executed_routes` query when the cache is missing (that fallback ran a ledger query on the synchronous Stop path).
  The Stop's money fragment for today carries its count and window: `today ~$1.25 est (n=7)` (the full line previously printed `~$1.25 est` with neither). It and the executed count come from the same `ledger_view.window_summary` call as the dashboard, `llm-router summary` and `kpi`; the cache (`ledger_aggregates.json`, now version 2) embeds the 24h and today `LedgerWindow` JSON, and a version 1 cache is ignored until the next background refresh.
  The full session summary now takes its session rows by `usage.session_id` when the Stop knows its session id, so two sessions open at once no longer show each other's calls; the classifier-log panel is labelled `classified hints ... not executed routes`.
- The session summary box (the full Stop box, and `last_summary.ansi` the detached child renders) now reads every remaining figure through the shared ledger view (session-end hook v40). User-visible number changes:
  - Session rows (`MODELS this session`, free/paid panels, `Net saved` / `NET LOSS`, "Model selection by complexity"): the session id's rows when the Stop knows it, else rows since session start (the free/paid panels already did this in v39; the complexity panel counted every row since start). `Net saved` / `NET LOSS` now ends `n=N session calls`.
  - Routing quality (legacy panel): `0 fallbacks (N routed)` is now `0 fallbacks (N routed, today)`; counts only served `runtime`-provenance decisions, as every other surface. `avg routing` becomes `Xms avg routing (n=N, today)` and averages only decisions that recorded a classifier latency.
  - Session panel (`Effective`, `Fallback`, `Escalated`, burn rate, `vs typical`): each carries `n=N, session <id>` (or `session (by timestamp: no session_id)`); `vs typical` adds `(vs K earlier days)`. `Effective` is now the local plus subscription share by discovered seats, not a fixed provider list; `Escalated` counts simple/moderate work on a paid provider by the same classes (an `anthropic` call on a Claude seat is subscription, no longer an escalation). A cost or burn rate appears only when a row recorded a cost; before, missing costs read as `$0`.
  - `p95 latency` is labelled `(n=N timed, session ...)`; a row with no complexity is no longer counted as `moderate`, and the percentile is nearest-rank like every other latency figure.
  - `cache hits` sparkline in "14-DAY ACTIVITY" carries `n=N rows, 14d`; rolling 14 x 24 h instead of calendar dates, production rows only.
  - `MODELS 14-day mix` note reads `classified routes only (n=N, 14d)`; rolling 14 x 24 h, `runtime` provenance only.
  - Removed (dead code, never printed): the `_lifetime_saved` star prompt and `_query_savings_by_task_type`.
  - "Classified hints" (the routing-method panel) is unchanged: its source is the classifier log, not the ledger, and it stays labelled `classified hints ... classifier log, not executed routes`.
- `llm-router summary` (terminal and `--markdown`) now prints ledger figures only, each with its n and window. User-visible number changes:
  - Latency: `Avg latency: 0 ms` is gone. Percentiles come from rows that recorded a latency (a NULL or 0 is not a measurement) and print as `p50 X ms · p95 Y ms · p99 Z ms (n=K of N rows, 24h)`, or `n/a (no latency recorded (0 of N rows), 24h)` when none did. The old per-lineage average and percentiles are removed.
  - Spent: `Spent $X` is now the ledger's spend with its count and window (`$0.6110 (n=9, 24h)`), or `n/a (no usage database)`; it no longer sums the lineage rows (which read `$0.00` for subscription traffic). The markdown line `Session cost` is renamed `Spent`.
  - Tier and provider panels: a tier the catalog cannot derive prints `unclassified (n)` instead of `unknown`, and a row whose model names no provider prints `unclassified (n)` in the provider panel; both panel titles carry `(n=N, <window>)`.
  - Headline: the verified and unverified savings lines carry the window; the always-premium comparison is kept but labelled `lineage.db estimate, n=N, <window>` (it is a routing-decision counterfactual, not a ledger figure).
  - A negative saving prints signed (`-$5.00`, `≈-$5.00 (-399900%)`) instead of `$0.00`; the one-line panel no longer drops a loss.
  Also: the ledger window gains `spent_usd` / `spent_n` (n/a without a usage database). The status-bar, statusline and session-end Stop figures are unchanged in this release; they are now registered as ledger-view surfaces and the ledger lint covers the Stop's ledger functions.
- P3.6-c PR3f (`redo-label --owner`, `redo-score --tune`): once a run has a canonical tune scoring, `redo-label --owner` refuses (exit 2, "owner sample is fixed once a canonical tune scoring exists; void the run to change it") to change the owner sample, so a larger `--others` can no longer invalidate the owner marker behind the freeze's back (the freeze used to report "labels on disk changed" for it, which was wrong). Raise `--others` before the first canonical scoring; afterwards void the tune run. The "owner pass NOT recorded complete" message now prints how many S/R pairs the manifest holds, says when the run cannot reach the minimum of 20 spot checks, and says why when the sample cannot be verified. Migration: an `owner_done.json` written by #712 (no `others`, no `owner_sample_sha256`) now reads invalid, so a new tune scoring made on it is non-canonical and a freeze that relies on it refuses; one `redo-label --owner` rerun with nothing left to label rewrites a valid marker without changing any label. A new end-to-end test drives tune draw, fake judge, owner pass through a pty, scoring of v2/v3c/v3d, and the freeze of the argmax.
- proxy local path (#508 follow-up): the `fast` trim, the okf attach and the backend now share one step deadline (`t0 + step_budget_s`, as the edit protocol already did). Before, the trim and the backend each got a full `step_budget_s`, so the worst case before falling back to Claude was about 2x the budget plus 2 s (62 s by default, 242 s in local-agent mode). The okf attach keeps its own 2 s cap within the deadline. A step whose budget is spent before the backend call is a `budget_exceeded` fallback, and the backend is not called. The deadline also counts the time before the trim, such as a backend health probe or local-agent compaction. When the budget, not the attach's own 2 s cap, cuts the okf attach short, the row says `okf: {"status": "budget"}` and no attach-timeout failopen is recorded.
- `redo-label --owner`: every labelling screen now shows the judge's own label definition (`LABEL_DEFINITION`, verbatim: the prose and every case row as `case -> y`, wrapped to the terminal) above the pair, and a key map (`y = 1 (redo)   n = 0 (not redo)   u = unclear   q = quit`) between THIS MESSAGE and the key prompt, so owner and judge label against the same text and THIS MESSAGE stays next to the keys on a short terminal. When a complete owner pass of at least 10 labels is all y or all n (lost pairs not counted), the command prints a warning with the count before writing `owner_done.json`. The warning says how to redo the pass before the run is scored (redo-score or redo-score --tune) and gives the re-run command with the `--projects-dir` and `--force-unresolved` the owner used. Labels and the marker are recorded as given. Labels file format, judge mode, sampling and scoring are unchanged. Evidence: tests/test_redo_label.py::test_owner_screen_shows_the_label_definition, tests/test_redo_label.py::test_all_identical_owner_pass_warns_but_records_as_given, tests/test_redo_label.py::test_uniform_pass_warning_repeats_the_owners_projects_dir_and_force, tests/test_redo_label.py::test_mixed_owner_pass_prints_no_warning.
- S1 daily-offload streak per owner decision D-210 (2026-10-17): the streak is 7 consecutive measured days (was 14); the floors of 30 actions / 10 turns are ratified (the "awaits owner ratification" wording is gone). Each `kpi --daily` row gains a `release_clock` population = organic + synthetic (synthetic membership from `--synthetic-manifest`; without it release_clock = organic, stated in the row's `note`) with its own n, floors, value and CI. `kpi --daily-streak` now walks `release_clock` and prints the organic streak beside it; organic and synthetic blocks are unchanged and organic is never merged silently. Unlisted research ("other") still never counts. Schema is now `daily-offload/2`. Rows written before this change (`daily-offload/1`) lack `release_clock`: they break the streak with the distinct reason `old_schema_rerun_force` (a corrupt or unknown schema stays `invalid_schema`); old rows need `kpi --daily --force`, which writes a `.revN`.
- Docs and release notes (T7A, v16 tag rule §7.1.7): numbers in the README, the pending changelog fragments, `docs/ROUTERARENA.md`, `docs/MEASUREMENT.md`, `docs/proxy.md` and `docs/BACKLOG_NEXT.md` that lacked an n, a window or a source file now carry them or are removed, and `docs/BENCHMARKS.md` now names the commit that holds the benchmarks file version its figures come from. A merged GitHub PR description counts as a named source when the figure carries its n and window. The README no longer embeds the hero and why-route images, which carried savings, tool-count and price figures with no source; those images, the unreferenced savings and editors images, and the test that only checked them are deleted.
- Docs (T7B, v16 tag rule §7.1.7): the numbers-provenance baseline (`scripts/numbers_provenance_baseline.json`) shrinks from 197 to 174 entries; nothing was added. Of the 23 docs units that left it, the ones reporting a result now carry a source and an N or lose the number: `docs/BACKEND-QUALITY.md`, `docs/BACKLOG.md`, `docs/MEASUREMENT.md` and `docs/PLAN_CLOSE_THE_GAP.md` cite the merged PR that holds each figure (PR #137, PR #139) with its task or prompt count; `docs/ROUTERARENA.md` cites `submissions/routerarena/PR_BODY.md` and the predictions file the routed-model shares are recounted from; the unsourced "7% of its outcomes" and the G3 "93.0%" reading are removed, and the percentages repeated in the PLAN_CLOSE_THE_GAP section headings are dropped (the sourced table keeps them). Three `docs/proxy.md` units held no result: a list price and the reconcile script's default tolerance are now marked as such, and a model name ("Sonnet-5 tokens") no longer reads as a token count. The `redo-label --judge` fragment now calls the $0.50 pre-charge a fixed estimate (a module constant), not a default.
- `llm-router prefer` accepts more everyday wordings, and 20 of them are now tested to parse to the intended rule and to change the chain identically at the router, hook and proxy non-Claude doors (`tests/test_p213_round_b_doors.py`): `never use gemini`, `I don't want gemini`, `stop using gemini`, `use openai before ollama`, `don't use paid APIs`, `limit gemini to two calls a day`, `cap gemini at eighty percent`, `keep code local`, `only use local models for code`, with counts as digits or English number words. Six natural wordings still refuse and are pinned as strict expected failures in that test (see docs/prefer.md). A per-day cap's description now says proxy-served non-Claude calls are limited by a hit cap but not counted toward it.
- Privacy wording now says which doors enforce the secret door and which do not. SECURITY.md has a per-door table derived from the UN-04 door matrix (#771) and a test that fails when the two disagree; the README no longer says the hooks only log. The opt-in paths that are not secret-guarded (the auto-route cloud classifiers, the direct executor's Gemini/OpenAI calls, the Claude Code proxy and `shadow_frontier` (both log-only), the ensemble with a cloud primary, agoragentic, the latent judge cascade) are listed. `engine.guard()` is documented as an unwired test seam.
- SECURITY.md now states the local action safety contract as refuse-only (owner decision D-321 amends NFR-SAFE): the router is not to run a destructive local action on your behalf, and v16 has no approval flow, so every lane (`llm_act`, `llm_local_task`, the toolkit) refuses a `needs_approval` verdict from `write_policy`. The section also says plainly that in v16 the contract is not yet met. It names the classes the static tripwire misses: `llm_act` is a denylist lane; an allowed program's own output, delete or exec flags are not read (`sort -o`, `pytest --basetemp`, `rg --pre`, `git -c core.pager`); `python -m` modules run code or extract archives; and git plumbing outside the git tables runs. `llm_local_task`'s file tools write nothing by default, but its `run_command` (on by default through `LLM_ROUTER_DIRECT_EXECUTION`) runs those commands. A known-gaps table gives examples of each class (tracked as SAFEFIX). `tests/test_security_docs_match_the_code.py` reads the section's tables and runs every row through the real lane executors. The final execution step is replaced by a recorder, and `Popen`/`os.system` raise. Each known gap, on each lane it names, is a strict expected failure, so a fix fails the test until the row leaves the table. SECURITY.md also corrects one stale sentence: under `LLM_ROUTER_AGENT_WRITES=apply`, `python -c`, `node -e`, GNU `sed e` and `find -exec` pass the allowlist but `write_policy.check_action` still refuses them.

### Added
- kpi (v16 GE6, PRD S3): quota-burn baseline. SessionStart (inside the P0.9 background child, which
  now receives the session id as its second argument) and Stop (not SessionEnd) append
  `{session_id, kind: start|stop, ts, five_hour_pct, weekly_pct, updated_at, five_hour_resets_at, source, session_kind}` to
  `~/.llm-router/quota_samples.jsonl` from the cached `usage.json` (no network); the status-line tick
  appends one `{ts, five_hour_pct, weekly_pct, updated_at, five_hour_resets_at, source}` row at most every 300 s to
  `quota_history.jsonl`. A snapshot older than 30 min, a fallback or a pending one is `source: stale`
  (unknown values null, never 0). New `llm-router kpi --quota-burn --since --until`: burn per session and
  per human turn (one Stop = one turn) from measured samples. A 5h-window reset is a changed
  `session_resets_at` (> 120 s apart), or, with no reset time, a drop of >= 5 pts; after a reset the new
  reading is all burn, even when it is higher than the old one; a smaller drop is jitter and burns nothing.
  Results come with session-clustered bootstrap CIs; stale samples form a separate line labelled estimated; coverage =
  sessions with start and stop samples over every tagged session in the window whose kind, after the
  owner's `session_kind_overrides.json`, is in the population (Wilson CI). Several status-line windows
  ticking together write one history row per slot (a non-blocking `flock` on the stamp). Hook versions:
  session-start 25, session-end 21. Tests: `tests/test_quota_samples.py`.
- toolkit: a router-owned tool layer, phase 1 (`src/llm_router/toolkit/`). Seven tools (read,
  search, list, edit, write, bash, finish) behind one permission function that runs in code
  before every call and logs every decision; a throwaway workspace (a copy; the caller's tree is
  never written and a fingerprint proves it); a macOS `sandbox-exec` profile that denies network
  and writes outside the workspace, proven by a probe at startup, with `bash` OFF when it cannot
  be proven; kill switch (`LLM_ROUTER_TOOLLAYER=off`, `~/.llm-router/KILL`); budgets on steps,
  time, tokens and bytes written; and a verifier that alone decides `used` (V1: the supplied test
  command passes after, nothing that passed before is lost, no test deleted or weakened).
  `llm-router run --model M --verify CMD --workspace DIR "task"` returns a patch and a verdict
  (propose-only). The execution ledger gains `verify` and `used` columns, NULL when unknown.
  `hooks/agent_loop.py` now calls the toolkit executor (one executor); its public functions are
  unchanged, and the secret deny-list now applies to it too. The kill switch does NOT reach it:
  `run_pipelines` honours `LLM_ROUTER_TOOLLAYER=off` / `~/.llm-router/KILL` only for a launched
  (sandboxed) `llm-router run`, so the hook's launcher-less commands behave exactly as on main
  (tests/test_agent_loop_hook_parity.py, `kill` tests). Because the auto-route local agent loop is on by default, this is a live
  path: its file tools and commands now run through toolkit code (parity: 78 commands), and the
  execution ledger migrates `ALTER TABLE execution_events ADD COLUMN verify TEXT` and
  `... used INTEGER` the next time the live ledger is opened (additive, nullable). The local model's context window comes from the one table,
  `local_models.num_ctx(model)` (32768 for qwen3.6).
- classifier (M1.8 round 3, default OFF): a decision-model backend for the local tier classifier,
  `src/llm_router/decision_classifier.py`. `LLM_ROUTER_CLASSIFIER_BACKEND=systemone` makes
  `classify_local` / `classify_async` call Ollama `POST /v1/systemone` (Ollama >= 0.35, default model
  `nimble:9b`, `LLM_ROUTER_DECISION_MODEL`) with ONE `choice` question (haiku / sonnet / opus, the v7 tier
  definitions). The `Verdict` gains `confidence` (p_max of the option probabilities, not the endpoint's own
  concentration score) and `abstain`; `LLM_ROUTER_DECISION_ABSTAIN_BELOW` (default 0.0 = never) turns a low
  p_max into `source="abstain"` with no tier, so the caller keeps the rules. Strict response check (three
  probabilities summing to 1 +/- 0.02, choice = argmax), no prompt text stored. The default backend
  (`chat`, prompt v6) is unchanged. Not routed live; the measurement is PREREG v2 amendment 2.
- hooks: `hook_latency.jsonl` rows can carry `phases_ms` (M4.1, hook tail attribution). `auto-route` (hook
  version 46) names `import`, `session_io`, `zce`, `classify`, `hud`, `db_write`, `draft_chain` (the whole draft
  chain: Ollama, Codex, Gemini CLI) and `cold_wait` (Ollama `load_duration`); `session-start` (version 23) names `import`, `session_io`, `reset_state`,
  `ollama_up`, `pxpipe`, `proxy_health`, `usage`, `hints`, `bg_spawn`, `banner`, `rules_update`. A run that
  names no phase writes the same row as before. Cost: `docs/measurements/2026-10-07-hook-phase-timing-overhead.md`.
- proxy: classifier shadow seam (M1.6, PR 2 of the M1 plan). With `LLM_ROUTER_LOCAL_CLASSIFIER=shadow`, each
  turn-first call also gets a local LLM verdict, logged next to the rules' verdict in `classifier_shadow.jsonl`
  (hashes and tiers only). Shadow only: no tier changes, `cls_applied` is false on every row, continuations
  never wait, at most 4 pending (more are dropped and counted). Default `off` makes zero Ollama calls.
  `proxy/tiers.py` is untouched. `tests/proxy/test_llm_classifier_shadow.py`.
- kpi: `classifier_shadow` (M1.7): n, sessions, agreement with the rules, tier distributions, cheap share,
  fallback rate, p50/p95 ms, drops, calls per turn, from `classifier_shadow.jsonl`. Outside `kpis`, so NS, D1,
  D2 and `--health` do not change. `tests/test_kpi_classifier_shadow.py`.
- kpi: `llm-router kpi --since WHEN --until WHEN` pins an absolute window (replaces `--days`; rows outside it
  never count; JSON gains `window`). Without the flags the output is unchanged (live-home JSON diff against
  c2ed278: identical). `tests/test_kpi_window.py`.
- status line: `LLM_ROUTER_STATUSLINE=both` prints the full line, then the fast (debug) line on a second
  row. Claude Code renders each printed line as its own row (docs: "Multiple lines"). Default, `full`
  and `fast` are unchanged; the `.env` reader is still never sourced (new test: `$(touch marker)` never runs).
- ux: a fast status line (opt-in debug mode) and a receipt band. With `LLM_ROUTER_STATUSLINE=fast` the
  `statusLine` command prints one short line
  (`llm-router · smart · NS 0.0% n=3599 · Claude 5h 12% wk 41% sonnet 3% · Codex 7/15 ↻21:14 · ⚠ hooks p95 3.0s auto-route`,
  at most 200 characters, `n/a` for anything unknown, never 0) from a small cache that
  `llm-router statusline --refresh` rebuilds in the background at most once a minute, plus the Claude quota
  snapshot `usage.json` (the 5h / weekly / Sonnet numbers `llm-router status` shows; past
  `LLM_ROUTER_USAGE_TTL_SEC`, default 300 s, they are shown with `(stale <age>)`). The default is the full
  layout, unchanged. The switch is read from the environment or, failing that, from
  `~/.llm-router/.env` (`LLM_ROUTER_STATUSLINE=fast`), so no `settings.json` edit is needed. New opt-in Claude Code mod `llm-router-receipt` (`llm-router mod install` /
  `uninstall`): after a turn the proxy served off Claude it shows "served by <model> · cost · est. saved" with
  `k` keep and `r` redo on Claude, and a `/router` pane with the last 10 routing decisions. Presses are
  `user_signal` rows in `user_signals.jsonl`; `llm-router kpi` shows `user_kept` and `user_redone` under D3. A
  keep is never counted as used (a passing test is), a redo is a D3 decided event.
- proxy: real Anthropic spend per call and per session. Each ledger row records
  `served_by`, the real Anthropic usage (cache read/write split; zeros when served
  locally), its cost and a counterfactual; `proxy_session_cost()` reports real spend, the
  estimated avoided amount and Claude Code's phantom `total_cost_usd` labelled
  `unreliable`. `llm-router kpi` O1 prints reconciled figures when the ledger supports
  them. `scripts/reconcile_proxy_cost.py` checks the ledger against transcripts.
- `llm-router pi`: runs the Pi coding agent (`@earendil-works/pi-coding-agent`)
  fully locally on one Ollama model, with a profile of Pi extensions in
  `integrations/pi/` that close the gaps the 2026-10-04 harness-parity probes found
  with qwen3.6: SIGINT now kills the running shell command; write/edit paths with a
  dropped leading `/` or a mistyped working directory are repaired; a `question`
  tool with a headless answer channel (UI, `LLM_ROUTER_PI_ANSWER`,
  `LLM_ROUTER_PI_ANSWER_CMD`) plus a one-time nudge when the model asks in prose; a
  `subagent` tool scoped to the working directory; a compaction that keeps the
  user's request verbatim and labels tool output as tool output; and a pre-send
  check that refuses a prompt Ollama would silently truncate. Image input is
  opt-in (`--vision`). Measurements and limits: `integrations/pi/README.md`.
- kpi: a session-kind override file, `~/.llm-router/session_kind_overrides.json` (`{sid: {kind, reason}}`). It beats the
  tag file and a row's own stamp in every KPI reader (NS, D1, D2, D3, O3, D4/G1-proxy) and in the proxy's and
  edit ledger's stamps; G3 is unchanged (writer completeness). `session_kind.overrides()`, `override_of()`,
  `tag_kind_of()`. The `--backfill-tags` run leaves an overridden session alone and `--validate-backfill` still
  compares the tag file. `tests/test_session_kind_override.py`.

### Changed
- SessionStart no longer blocks on the Claude usage refresh. The hook
  (`hooks/session-start.py`, hook version 19 -> 20) used to run the keychain
  read plus OAuth call inline, up to 3 attempts with a 15 s keychain timeout,
  a 10 s HTTP timeout and a 2 s backoff each (about 79 s worst case by those
  timeouts). It now reads the last `usage.json`, labels it
  `(stale, Nm old)` in the banner when older than the fresh threshold, and,
  only when the cache is missing, pending (the placeholder
  `install_hooks.seed_usage_json()` writes on every install), stale or a
  failed-refresh fallback, starts a detached background process (the same
  script with `--background-usage-refresh`, running the unchanged refresh
  logic — or, under a standalone/frozen build, `<binary> run-hook <script>
  --background-usage-refresh`, since `sys.executable` is the binary itself
  there). The spawn claim (`usage_refresh_spawn.txt` in the state dir) is
  taken under `llm_router.file_lock.exclusive_lock` so concurrent session
  starts cannot both spawn, and a failed spawn never breaks the hook. A cache
  missing its percentage fields (the pending seed) is never rendered as 0%.
  `session_start_cc_pct.json` (session-end's delta baseline) is still written
  on every session start — from the cached values when fresh, otherwise the
  same 50% fallback marker the inline refresh used to write. New env vars:
  `LLM_ROUTER_SESSION_START_USAGE_FRESH_S` (default 300) and
  `LLM_ROUTER_SESSION_START_USAGE_COOLDOWN_S` (default 60). With no measured
  cache yet the banner shows a warning-style usage line until the first
  background refresh lands.

### Added
- Local agent: the local ReAct loop (`llm_router.agentic.react.ReActAgent`, the
  tier-0 agent) now shares the proxy's loop guard
  (`llm_router.proxy.loop_guard.LoopGuard`) instead of relying on `max_steps`
  alone. Before running, in each step, a candidate tool call (or set of tool
  calls, for a model that emits several in one turn) is checked against the
  same two conditions the proxy enforces: it is refused if it repeats a tool
  call (name + args, exact match) served within the last
  `LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW` served steps (default 3), or if the
  session already served `LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE` steps in a
  row (default 8) — same env vars, same defaults, imported rather than
  reimplemented. On trip the loop stops immediately (the tool call is never
  executed) and reports an empty output with an `error` naming
  `loop_guard` and the tripped condition — the same "gave up" shape the loop
  already uses for a client-level failure — so the milestone's acceptance
  check fails it exactly as any other incomplete run, and the engine escalates
  normally. A fresh `LoopGuard` is built per `run()` call so state never leaks
  between milestones that reuse one `ReActAgent`. No behavior change when
  neither condition trips.
- Local edits: scoped zero-Claude edits (`LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit`)
  are now lint-verified before they are written
  (`llm_router.local_agent.verify`). Previously the only gate was the syntax
  check, which lets through code that parses but fails the project's lint
  (e.g. an unused import). The original and the candidate text are both
  linted with `ruff check --output-format json` on temp copies that mirror the
  repo-relative path (so `per-file-ignores` apply), using the project's own
  ruff config; only violations NEW in the candidate (multiset of rule code +
  message, not line numbers) block the write, so pre-existing lint debt does
  not. A file whose repo has no ruff config (`.ruff.toml`, `ruff.toml`, or a
  `pyproject.toml` with `[tool.ruff]`, found walking up to the repo root) is
  not linted, nor is anything when neither `ruff` nor `uv`/`uvx` is on PATH:
  syntax check only, as before. A new violation (named in the message), a
  timeout, a ruff error, an unlintable original, or too little of the hook's
  wall-clock budget left blocks the write and escalates to Claude through the
  existing failed-edit path (`claude:` redo hint). The user's file is never
  modified before the check passes. Only Python files are linted; no tests are
  run. Opt out with `LLM_ROUTER_ZERO_CLAUDE_VERIFY=0`. Default ON; no pass-rate
  or savings claim is made.
- Local edits: the edit protocol's direct Ollama calls (the zero-Claude edit
  hook and the proxy's local-agent edit step) now send a JSON Schema as
  Ollama's `format` (`llm_router.local_agent.constrain`), so the reply is a
  parseable `[{file, old_string, new_string}]` array by construction. The
  existing validator (exact-once match, syntax gate, 3 attempts) is unchanged
  and still runs. Motivation: the one failure in the 20 edit fixtures
  (`routing-golden-v1`, run `20260928T103518Z-postdeploy-e893c8d`, case c007)
  never produced a parseable edit array; no new pass rate is claimed. A
  server that refuses a schema `format` with HTTP 400 (Ollama < 0.5) is asked
  again without it. The routed `llm_edit` MCP tool (any provider) is unchanged.
- Proxy: `llm-router install --proxy-default` makes the per-call proxy the
  DEFAULT `ANTHROPIC_BASE_URL` for every Claude Code session on the machine
  (`llm_router.proxy_default`, `llm_router.commands.proxy_default`),
  owner-approved 2026-09-30 after a trial on real prompts (n=6, 72-80% lower
  cost). Supervised (macOS LaunchAgent `KeepAlive` / Linux systemd user unit
  `Restart=on-failure`); the installer verifies the proxy answers before ever
  writing `settings.json` and refuses otherwise, backs up settings.json first,
  and reuses an already-answering proxy instead of fighting it for the port.
  `llm-router doctor`, the statusline, and the SessionStart hook all report a
  dead proxy with the exact recovery command; `llm-router uninstall` (or
  `--proxy-default off`) reverts cleanly via the install manifest. Two ways to
  escalate a conversation to Opus (`proxy/escalation.py`): an explicit `opus:`
  prompt prefix, and automatic escalation on a contradiction, a `claude:`
  re-ask, or repeated tool failures — both logged in the ledger. Never
  downgrades an explicit `/model` pin or a long/multi-part first prompt (a
  paraphrase of the trial's one unacceptable answer is now a regression test).
  See `docs/proxy.md`'s "Proxy-default" section.
- Proxy: backend-health circuit breaker for the local Ollama path
  (`proxy/backend_health.py`). Three consecutive empty or sub-second invalid
  replies, or a crash signature in an Ollama error, pause local serving for 60 s
  (`--backend-fail-n`, `--backend-cooldown-s`, or the matching
  `LLM_ROUTER_PROXY_BACKEND_*` env vars). Skipped steps go to Claude with
  `reason: "backend_unhealthy"`. A one-token probe decides when serving
  resumes. Evidence: after a Metal out-of-memory fault on 2026-09-30, 29/29
  empty replies in one A/B came from the crashed windows, and each one
  fell back to Claude. The Ollama backend now also reports a streamed `error`
  line and the body of an HTTP error, which it used to drop.
- Proxy: opt-in Claude-tier rewrite (`llm-router proxy --tiers on`). Each
  forwarded call can move to a cheaper Claude tier (Haiku 4.5 / Sonnet 5.5 /
  Opus 5.5 / Fable 5.1, set in `proxy/claude_tiers.yaml`). It follows
  no-downgrade rules (user `/model`, first call, thinking/effort support) and
  cache-aware stickiness. Any decision error or 4xx on a rewritten call
  forwards unchanged. `proxy stats` shows the tier mix, the switch rate, and
  an **estimated** cost against the all-requested-model counterfactual. No
  savings claim: on the one live smoke (3 tasks, 23 calls) the
  switch-after-first-call variant cost more than all-Opus, so it ships off.
- **`llm_router.local_agent`: capability gating + tool retrieval/compaction for
  proxy steps** (`llm-router proxy --local-agent`, off by default). No savings
  claim: a 15-pair A/B (2026-09-30) measured −1.1% realized cost change, 95% CI
  −6.4% to +4.3%, with 9/24 healthy local attempts served. See `docs/proxy.md`.
- Proxy: opt-in conversation-level Claude-tier rewrite
  (`llm-router proxy --tiers conversation`, Phase 1.2b). The per-turn rewrite
  above measured a net loss from switching tiers mid-task (a switch re-writes
  the whole prompt cache, and Opus 5.5 / Sonnet 5.5 read cache at the same
  rate); this mode instead classifies the conversation's first human prompt,
  picks the tier once for the whole conversation, and holds it via
  stickiness, escalating only at a cold point or a clear rise in complexity.
  Every proxy ledger row now carries `tier_mode` (`off` / `on` /
  `conversation`). No default-on claim: this ships behind the flag pending a
  pre-registered paired A/B (see
  `~/.rsi/research/llm-router-cursor-parity/p12b-conversation-tiers-ab.md`).
- `complexity_knn`: a learned 0-1 "needs frontier" score (kNN over labelled
  prompts with κ-shrinkage, K=120, κ=3.0, T=0.1), a third vote in the ensemble
  (`LLM_ROUTER_COMPLEXITY_KNN=on`) and a wrapper on the tier policy's `classify=`
  hook (`complexity_knn: true` in `claude_tiers.yaml`). Artifact built locally by
  `scripts/build_complexity_knn.py`, never committed. **Ships off.** On a
  public benchmark proxy (n=12,900; held-out n=2,580) it beats the regex/length
  heuristic (AUC 0.763 vs 0.482) but not a which-dataset-is-this baseline
  (0.776), falls to 0.587 on unseen datasets, and 0 labelled owner prompts
  exist to measure it where it would run.

### Security
- LC-12e: Ollama cloud models are excluded from local routing; the local Ollama daemon serves them, but the prompt leaves the machine. A model counts as cloud when its tag is `cloud` or ends in `-cloud` (e.g. `gpt-oss:120b-cloud`, `<name>:cloud`, case-insensitive; an untagged name is read as `:latest`, as Ollama resolves it, so `x-cloud` and `x-cloud:latest` are both local by name), or, at `local-check` time, when Ollama's `/api/tags` row or `/api/show` answer for it carries `remote_host` / `remote_model`. That metadata check catches a cloud model copied or re-created under a local-looking name (`ollama cp gpt-oss:120b-cloud mine`, a Modelfile `FROM`); it reads the answers local-check already fetches and makes no extra request. `llm-router local-check` refuses to run on a cloud model ("local-check measures local models only"), so it writes no PASS and switches no lane on; `local-check enable` refuses a cloud name, and the interactive model picker lists cloud models as excluded. The eligibility guards (`prefer_passed`, `passed_model_leads`, `passed_lane_available`) reject a cloud name even when a `local_routing.json` written before this fix says passed, and the D-277 local-only cut in `route_and_call` refuses one and releases the budget reservation, so `llm_text_job` returns `fallback` ("do this job yourself") with no model call. Limits: at route time the guards check the name only (no network call); a renamed cloud model is caught when local-check runs, not afterwards, and a model whose Ollama metadata hides that it is remote cannot be detected at all. A pre-registered (`text_job`) lane is matched by name, not digest, so a cloud model copied onto such a name and enabled by hand is not caught either. A name that only contains "cloud" (`cloudy-model:7b`) is not a cloud tag. The local-only path now fails closed when its cut-site check raises (no leaked reservation), and when `prefer_passed` or `passed_model_leads` raises on that path (e.g. the inventory cache read fails) it refuses with "the local-only eligibility step failed" instead of recording a fail-open event. Fail-closed refusals have their own counter, `text_job_fail_closed` (`text_job_fail_closed.jsonl`, capped at two 256 KiB generations), which `llm-router doctor` shows for the last 7 days; the fail-open counter `CHZ-FO-ROUTER-ELIGIBILITY-FILTER` now counts only true fail-open cases. Test fix: `_pending_spend` comparisons in `tests/test_lc12c.py` use `pytest.approx` (a float flake on py3.11 CI). Evidence: `tests/test_lc12e.py`, `tests/test_lc12e2.py`.
- LC-12f: the copy-over window for local-only text jobs narrows from the discovery-cache lag (up to 12 h, or 7 days for a cached digest) to the milliseconds between the live check and the generate call (LC-12g states the remaining limits). Before, a user with a real local-check pass for `mine:latest` could run `ollama cp gpt-oss:120b-cloud mine:latest`. Until `discovery.json` refreshed (12 h by default, and a cached digest is trusted for up to 7 days), the route-time digest check still matched, so a D-277 local-only `llm_text_job` could send the user's diff to ollama.com. Three changes. (1) Both discovery writers now store `remote` per model (true when Ollama's `/api/tags` row carries `remote_host` / `remote_model`, else false), next to the digest that was already stored. The field is new; no field is renamed, so old readers still work. A rescan triggered by a changed digest now writes the digest it saw. Before, it kept the old digest under a fresh `cached_at`. (2) At route time, a model whose cached entry says `remote: true` is never routable (`prefer_passed`, `passed_model_leads`, `passed_lane_available`). This reads the file and makes no network call. (3) Right before a pass-only text job calls its passed model, the cut site in `route_and_call` reads that model's live `/api/tags` row once. The read goes only to the user's own loopback Ollama daemon (`OLLAMA_API_BASE`, else `http://localhost:11434`), with a 2 s timeout. The job is refused before any model call, and the budget reservation is released, when the live digest differs from the one the pass recorded, when remote metadata is present, when the model is not listed, or when the daemon does not answer, times out or is not loopback. `llm_text_job` then returns `fallback` ("no longer matches its pass"). Each such refusal is counted in `text_job_fail_closed` as `CHZ-FC-ROUTER-TEXT-JOB-LIVE-CHECK`. With `LLM_ROUTER_LOCAL_TEXT_JOBS=1`, no live check runs, and a model cached as `remote: true` is no longer preferred (change 2); nothing else in that routing changes. Limits: a model whose Ollama metadata hides that it is remote cannot be detected. A pre-registered (`text_job`) lane is keyed by name, not digest, so for it only the remote check applies. Review nits from #713: an unexpected error in the cut-site check now raises `EligibilityStepFailed` (the hand-back says "the local-only eligibility step failed"). `doctor` reports a fail-closed store it cannot read (e.g. a permission error) as unreadable, not as "present but unparseable". RETENTION.md names the store's `.lock` sidecar. A test pins the `ts >= cutoff` window boundary. Evidence: `tests/test_lc12f.py`.
- LC-12g: on the local-only text-job path (`LLM_ROUTER_LOCAL_TEXT_JOBS` unset, D-277) the text now goes over HTTP only in the generate request, straight to the loopback daemon, and the call never switches to another model on overflow. (1) Proxy. LiteLLM 1.103.0 (`uv.lock`) honours `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` for `localhost` too when `NO_PROXY` is unset, on both its aiohttp (default) and httpx transports. Measured offline with a fake proxy and a fake Ollama on loopback: the `/api/generate` request, prompt included, went to the proxy. On the pass-only path `providers.call_llm` now gives that one Ollama call its own httpx client (`trust_env=False`, no redirects), closed after the call; no environment variable or LiteLLM global changes. That client covers `/api/generate` only. Not covered: LiteLLM's model-info lookup uses its own module-level httpx client on either transport, so in a process whose LiteLLM model-info cache is still cold, LiteLLM's model-info lookup sent `POST /api/show` through the proxy (both transports). Each carries the model name (body `{"name": <model>}`), the destination URL and, when `OLLAMA_API_KEY` is set, an `Authorization: Bearer $OLLAMA_API_KEY` header (LiteLLM `llms/ollama/common_utils.py`; the fake server does not record headers). They carry no text. An earlier reading of 0 on the httpx transport did not reproduce in a fresh process. The router's Ollama probe (`GET {OLLAMA_BASE_URL}/api/tags`, no body) also goes through the proxy. (2) Other requests that carried the text, found by a socket-level trace of a real `llm_text_job` with the semantic cache on and the judge sample rate at 1.0. The semantic cache POSTed the full prompt to `{OLLAMA_BASE_URL}/api/embeddings` with `urllib` (honours proxy env, no loopback check, the URL may name another host) at check and at store, and a hit could return an answer a cloud model wrote. On the pass-only path the cache is now skipped, check and store; `semantic_cache._get_embedding` and `semantic_classify._embed_ollama` also refuse there. The judge queue stored the prompt for out-of-band grading, which sends it to another model through LiteLLM's shared client, or to Haiku when `LLM_ROUTER_JUDGE_HAIKU_FALLBACK` is on. The pass-only path no longer queues. (3) Overflow. With `LLM_ROUTER_PACK_OVERFLOW=1`, an input too large for the passed model could switch the call to a larger local model that has no pass and got no live check. On the pass-only path that switch is now refused before any model call: `llm_text_job` returns `fallback` ("the input does not fit the passed local model"), attempts are not counted, no local-attempt error row is written, and the refusal is counted in `text_job_fail_closed` as `CHZ-FC-PROVIDERS-TEXT-JOB-OVERFLOW-SWITCH`. The router treats it as a policy refusal: no Ollama health failure (two refusals used to mark Ollama unhealthy), no `attempt_failed` ledger row, no "failed ... trying next" notice, and the chain walk stops. (4) One predicate: the router computes `eligibility_routing.pass_only_call` (text-job reason and `llm_text_job`'s ContextVar) and sets it for the dispatch; `providers`, `semantic_cache`, `semantic_classify` and `judge` read that, not the ContextVar alone. (5) Review nits from #714: a non-loopback `OLLAMA_API_BASE` is refused with its own reason, "endpoint is not loopback (text would leave the machine)", instead of "ollama did not answer". `doctor` now flags an unreadable fail-closed store as alarming, as it already does for the fail-open and interception stores. A `/api/tags` row without a digest now clears the cached digest instead of keeping the old one under a fresh `cached_at`. A test runs the real live-check success path against a loopback `/api/tags` server. (6) Disclosed limits (docs/local-check-routing.md): the check-to-generate window is milliseconds and still open; a loopback endpoint that forwards elsewhere (`ssh -L 11434:gpu-box:11434`, a local reverse proxy) passes the check and sends the text off the machine, which is the user's own configuration. The README claim "Secrets never leave your machine" (`README.md:156`) is not changed here; it is a separate follow-up (LC-12h). Evidence: `tests/test_lc12g.py`, `tests/test_lc12g_repair.py`.
- LC-12h: every Ollama call to a loopback daemon bypasses HTTP proxies, and a prompt with a secret goes to a loopback local model or nowhere. (1) Proxy. LC-12g's per-call httpx client (`trust_env=False`, no redirects, closed after the call) now covers every `ollama/` and `ollama_chat/` call whose daemon (`OLLAMA_API_BASE`, else `http://localhost:11434`) is loopback, on every path, `providers.call_llm_stream_events` included (new `local_privacy.py`). LiteLLM's own model-info lookup (`POST /api/show` with the model name, the URL and `Authorization: Bearer $OLLAMA_API_KEY` when set, through the proxy) no longer runs for those models: the router asks the daemon once per process with `http.client` (no proxy, no key) and writes the answer into `litellm.model_cost` in LiteLLM's own shape, so LiteLLM treats the model as known. `config.probe_ollama` and the semantic cache's and semantic classifier's `/api/embeddings` use a urllib opener with no proxy handler and no redirects for a loopback base. Measured offline (fake proxy, fake Ollama and fake embedding host on 127.0.0.1, `HTTP_PROXY`/`ALL_PROXY` set, `NO_PROXY` unset, aiohttp and httpx transports): the fake proxy received no request of any kind on an ordinary local route, the secret door or the stream door, and the prompt reached only the fake Ollama (and, off the private paths, the fake embedding host). A non-loopback Ollama base is unchanged: LiteLLM's shared client, the user's proxy env and an explicit `NO_PROXY` apply. Not changed: model discovery, warm-up, the watchdog, `local_context_guard`'s window probe, the session-end context summary and the local classifier's eval path still use plain `urlopen`. (2) Secret door (D-28 A). `engine.Decision.local_only` existed but nothing in routing read it; the README's "Secrets never leave your machine" was not backed by code. Now `route_and_call` (the `llm_*` tools, the gateway), `route_and_stream` and the `llm_stream` tool scan the request's prompt, system prompt, caller context and messages with `engine.secret_names` (fails closed); on a hit the chain is cut, after every other chain step, to Ollama models on a loopback daemon that are not `-cloud` models, and an empty result raises `local_privacy.SecretLocalOnlyRefused` (a subclass of `engine.SecretLocalOnlyError`) before any model call. That dispatch gets the pass-only text job's privacy: the semantic cache is skipped (check and store), nothing is queued for the judge, there is no BUDGET emergency fallback, a non-local model or non-loopback daemon is refused in `providers` (`PrivateEndpointRefused`), and the PACK_OVERFLOW switch is refused when its target would leave the local set (a switch to another loopback local model is allowed). The LLM complexity classifier returns its fallback instead of sending such a prompt to a classifier model. User-visible: a prompt with a secret shape and no loopback local model is now refused instead of sent to a cloud model. Not covered: the Claude Code proxy (log only, D-28 A), agent delegation, session history the router injects after the scan, and secrets matching none of the shapes. (3) #715 review nits: the policy refusal (overflow switch or private endpoint) adds no chain error, so `fallback_occurred` stays false; the pass-only marker is reset before the ledger-session override (reverse order of the sets). The marker is still set at the dispatch, not where `_pass_only` is computed: the early returns and refusals in between would need one `finally` across ~600 lines, and the only network step in between, the semantic cache check, is skipped on both private paths. (4) README.md "Features" bullet reworded to what is now true, with its limits; SECURITY.md and docs/local-check-routing.md updated. Evidence: `tests/test_lc12h.py`. LC-12h repair 1 (PR #721 review): (a) the door refuses only high-precision secrets (`engine.secret_findings`): real key prefixes with enough entropy, PEM blocks with a body, bearer tokens with entropy, database URLs with a non-common password; placeholders (`AKIA...EXAMPLE`, `ghp_xxxx`, `YOUR_...`, `postgres:postgres@`) are exempt, JWTs and quoted literals are counted by category and never refused; (b) the refusal names the category and the place (current prompt or earlier turn N), says how to proceed, and a `cloud:` prefix overrides one request (counted); the gateway answers 422, not 502; an explicit cloud `model` on a secret request is refused, not silently re-routed; (c) `sdk.route` is behind the same door and its Ollama leg goes direct; (d) the detector is linear (bounded quantifiers, overlapping windows); (e) `/api/show` failures are retried after 10 s and good answers refreshed after 5 min; (f) prompt-carrying urllib Ollama calls in ten modules go through `local_privacy.open_url`, the rest listed in SECURITY.md (hooks `auto-route.py` v75, `playwright-compress.py` v5); (g) `OLLAMA_API_BASE` endpoint suffixes (`/api/generate`) are stripped. Evidence: `tests/test_lc12h_repair.py`. Repair 3: only the new request is scanned (not earlier turns); a value labelled as an example (`.env.example`, sample, placeholder) is not refused; a real JWT is refused while the jwt.io sample and test-claim JWTs are not; the override also exists as `allow_cloud` (route_and_call, SDK, `/route` body) and the `X-LLM-Router-Allow-Cloud` header; the gateway and route server answer a refusal with a typed 422 body.
- The local agent lanes are now allowlist-only (owner decision D-322). `llm_act`, `llm_local_task`'s `run_command` and the toolkit all go through `write_policy.check_action`, which now runs only the programs on one allowlist (`write_policy.allowed_programs()`): read and inspect tools, the in-root writers whose targets the tripwire already reads, read-only `git` plus `git add`/`mv`/non-recursive `rm`, project scripts, and the test and build tools (`pytest`, `go build|test|run`, `cargo build|test|run`, `npm test|run`, `ruff`). Any other program is refused, so commands such as `crontab -r`, `diskutil eraseDisk`, `unlink ~/.zshrc`, `tar -C /` and `git read-tree --reset -u` no longer run on `llm_act`. For allowed programs the policy reads their own flags: output and working paths (`sort -o`, `tree -o`, `git --output`, `go build -o`, `pytest --basetemp`, `git -C`, a script path) must resolve inside the project root, and flags that delete or run a command are refused (`find -delete/-exec`, `rg --pre`, `git -c core.pager/alias/core.fsmonitor/diff.external`, `git grep --open-files-in-pager`, `go -exec`, `cargo --config`, an existing `pytest --basetemp` directory). `python -m` is limited to `pytest`, `unittest`, `py_compile`, `compileall`, `json.tool` and `ruff`; `timeit`, `tarfile`, `zipfile`, `runpy` and the rest are refused. For interpreters and test runners (`python`, `node`, `pytest`, `go`, `cargo`, `uv`, `pip`, `npm`, `yarn`, `ruff`) the rule is general, not a flag table: every argument or `=`/`:` value that looks like a path, including after `--` or `-args`, must resolve inside the root, so plugin and test-binary flags such as `pytest -o log_file=~/.zshrc`, `node --test-reporter-destination=`, `go test -test.coverprofile=` and `cargo test -- --logfile` are refused too. Inline code is refused in any spelling (`python3 '-c…' s.py`, `node --eval=`, `node --import data:…`). `go run|build|test` take only local packages, so a module path such as `example.com/x@latest` is refused. A program given as a path (`./cat`, `/usr/bin/python3`) is refused, and so is a bare name that `PATH` resolves to a file inside the project, except a project virtualenv or `node_modules/.bin`. Writes into `.git` (hooks, config, in any letter case) are refused from the file tools and from commands. `cd`/`pushd` out of the root is refused, `git add/mv/rm` operands must resolve inside the root, a runner argument containing `$` needs approval (pytest expands it itself), `go env -w/-u` is refused in any spelling, and `uv run` index, find-links, upgrade and refresh options need approval. A command prefix may set only harmless variables (`PYTHONPATH`, `NO_COLOR`, ...), and a path in one must be inside the root. `LLM_ROUTER_AGENT_COMMANDS=all` still lifts `llm_local_task`'s narrower inspection list but no longer lifts this allowlist. Some local tasks that used to run are now refused, for example `make`, `bash script.sh`, `git fetch` and `go mod tidy`. Every command of the UN-15 known-gaps table (PR #778) moved to SECURITY.md's refused tables, which the doc-driven test drives through the real lanes, and SECURITY.md now states the method's limit plainly: the check is a static, best-effort tripwire over shell text, unknown bypasses may exist, and the containment that does not depend on parsing is the OS sandbox planned for 16.1. It also names the accepted residual: the lanes still run the project's own code (scripts, test runners, `go run`, `cargo run`, `npm run`), and on `llm_act` that can be a file the model has just written. Repository config files the model can edit (`pyproject.toml` addopts, `conftest.py`, `.cargo/config.toml`, `.npmrc`) can re-enable a refused flag in two steps; SECURITY.md names this as part of the same residual.
- UN-04b: a prompt holding a high-precision secret (API key, token, private key) is no longer sent to a cloud CLI or API through doors that skipped the secret door. The `llm_codex` and `llm_gemini` MCP tools now return a refusal that names the category and never the value, instead of starting the CLI. The subagent hook (`hooks/agent-route.py`) now runs the secret door once, before every path that calls a cloud service (Codex delegation, direct execution, CLI delegation); a secret prompt skips all three, so only the host's own spawn handles it. The `cloud:` prefix overrides in all of them, logged as a count only. Evidence: `tests/test_un04b_secret_door_cli_and_agent_route.py` (drives the real hook `main()` with default settings).
- UN-04c: the Playwright snapshot hook no longer sends a snapshot holding a high-precision secret to Gemini (`GEMINI_API_KEY` set made it default-on, with no secret check). It uses the repo's secret door (`local_privacy.scan_secrets`) and falls back to the local rule-based summary; a door that raises or a missing `llm_router` also blocks the cloud call (event `UN-04C-PW-DOOR`). Non-secret snapshots are unchanged. Evidence: `tests/test_un04c_playwright_secret_door.py`.
  UN-04c (follow-up): the same door guards a non-loopback `OLLAMA_HOST` in the Playwright hook, and the `cloud:` override is deliberately not honoured for page content (a page is not the user asking to send it).

### Docs
- Config surface: `env_registry.PUBLIC` declares the 10 documented user settings (including `LLM_ROUTER_DIRECT_EXECUTION`, the security off-switch the README must disclose; one grouped row for provider API keys, text and media); `docs/configuration.md` (now committed, un-ignored in .gitignore) is generated from it by `scripts/gen_config_docs.py` (`--check` in tests). README and `.env.example` mention only public variables, enforced by test. `.env.example` drops the undocumented `LLM_ROUTER_TIER`, gains `OPENROUTER_API_KEY`, and corrects the budget default (20 USD, not unlimited). No variable was renamed or removed.
- Baseline-start help text and KPIS.md now say that a session identified as not Claude Code (its tag names another host, or with no host its id is a UUIDv7, as Codex ids are) cannot set the quota-burn baseline; any other organic session can (mirrors quota_samples.baseline_start_ts).

### Internal
- CI test budget raised from 1500s to 2100s (job timeout 45 min) and the P1.6-k scanner tests get an explicit 120 s timeout, after repeated budget and 30 s timeout failures on busy runners.
- CLS-v16 F11: tests pin the criterion-8 1-SE floor wiring (real and DEV-SYN legs), the kappa n<5 rule and the `--mode test` gate; `kappa.by_length_band` in `scripts/cls_eval.py` is now split per kind (`real` / `syn`) as PREREG-CLS-DEV section 3.4 reports them, `by_source` unchanged. No decision rule changed.
- Pin `uv.lock` to the exact versions of the live uv-tool install (55 packages moved, 6 transitive additions; e.g. litellm 1.82.6 to 1.103.0, mcp 2.0.0 to 2.2.0) so CI tests what runs (D-164, F10). Add `scripts/check_live_matches_lock.py` to verify a given interpreter against the lock at deploy time.
- Haiku guard: a recovery stamp equal to (or older than) the demotion's trip time no longer counts as a recovery (`rec > ts`), and tests pin that a stale stamp cannot re-enable the rewrite while the breaker state is unwritable.
- CI now fetches full history in the test job and a new test (tests/test_hook_pair_guard.py) fails when a stamped hook's two copies (hooks/ and src/llm_router/hooks/) differ, or when its content changes without a higher llm_router-hook-version than the merge base with origin/main (F53, after PR #568).
- Tests and `scripts/bench_statusline.py` no longer run machine-wide `pkill -f`/`pkill -x`: each terminates only the pid it started (pid file written by the child, bounded wait); `tests/test_f56_no_machine_wide_kill.py` fails if `pkill`, `killall`, `taskkill` or `kill $(pgrep ...)` reappear under `tests/` or `scripts/`.
- Test-only: tests/conftest.py now clears the budget oracle's module-global cache around every test, so test_budget.py no longer leaks a cached high anthropic pressure into later tests (F62).
- Tests: new tests/test_p34a_six_surfaces.py renders the statusline, status-bar, dashboard summary, summary command, kpi and session-end surfaces over one seeded home and checks each figure against a hand-written oracle, with the surfaces that still disagree marked strict xfail for P3.4-B1 to B5.
- P2.12 (P212-5): a test now checks that the four install paths leave the same install receipt: `llm-router install --auto`, the npm postinstall (the real `npm/install.js` `runInstaller` under node, with the binary replaced by a shim that runs the CLI entry point), the Claude Code plugin's SessionStart hook with consent given (`install --auto --via-plugin`), and the MCP door `llm_router_admin(action="install")` (plan, then confirm). Each runs on its own fixture HOME with the same six detected hosts (three wired, three not wired for three different reasons). The test asserts one digest, the same host rows, and every row field equal except `via` for claude-code on the plugin path, which must be `plugin`. It also asserts that no user-owned file in the state dir changes, that nothing undeclared is created there, and that settings.json registers no llm-router hook twice under one event (and none on the plugin path). The npm path's child Python runs under the same conditions as the in-process paths: connections to localhost:11434 are refused, the background agentic-registry probe is stubbed and its subprocess refused, and the test asserts on a record of each refusal, so no run starts live Ollama inference or leaves a process behind. The P2.12-c fixture list of router-written state files now includes the plugin hook's `plugin_install_state.json` and `plugin_install_consent.json`; the plugin path's HOME was the first to create one. Evidence: `tests/test_p212_four_path_receipt.py`.
- CI now runs the real Pi smoke (job `pi-smoke`): it installs the pinned `pi` CLI (0.99.1), runs `llm-router install --auto --no-plugin` into a temp HOME, and asserts that `pi mcp list` exits 0 listing `llm_router` as connected with its tools and that the install receipt's pi smoke is `passed`. The job fails if the smoke is skipped. Source: tests/test_p212_pi_ci_smoke.py.
- Tests: OKF choke-point check now requires a real `build_pack(` call and a behavioural spy that `build_pack` reaches `context_injection.inject`; P1.6-k raw.githubusercontent.com pins are full-string anchored (release-tag benchmarks template and community.json only).
- UN-09b S2: `tests/test_un09b_blocked_named_model.py` pins the UN-09 subscription block (PR #795): in Claude subscription mode a request that names an `anthropic/*` model is never served by Claude through `route_and_call`, the gateway, the MCP `llm_query` tool (`model=`) or the MCP `llm` tool (`haiku:` prefix). Every chain in the file holds a Claude entry the router can serve, with controls showing Claude is served when the request is un-named or subscription mode is off, so removing the block turns the tests red. Not closed (owner decision, UN-09b S1): the emergency BUDGET fallback chain and a policy specialist naming `anthropic/*` can still serve Claude; both are pinned as strict xfails in the same file.
- PREREG-P32 AMEND2 (post-hoc, owner D-351): fid_syn_60 fidelity window 8192 -> 65536, the D-101 cloud-model window cap; the PASS at that window is by construction; run_p32.py re-pinned. The 2f22034d BLOCK stays on record.

### Added
- `scripts/release/bind_report.py --since <fix deploy time>` prints the D-358 success bar over a window. Each line carries its n, UTC window, source and PASS, FAIL, INSUFFICIENT, PENDING or REPORTED, and the exit code is 0 only when every gated line passes; a time without a zone (`2026-11-03T09:50:20Z`) is refused, not read as local time. Line 1, the Codex binding rate: the denominator is every unit where the router emitted a deny carrying a Codex result, and a unit is bound when the deny reached Claude as the sub-agent's result (`hookSpecificOutput.permissionDecision == "deny"` plus the BIND W1 wrapper text, no Claude sub-agent launched for that tool_use_id) and the same prompt was not asked again within an hour (the escape hatch, `codex_reissue`). The runs that delivered nothing (`codex_blocked`, `codex_failed`) and the re-issues are printed beside it as counts and shares, and a deny without the wrapper is named as wording drift. Line 2, the blocked-answer share among bound units, is a hand check of 20 units fixed in advance (the first 20 by sha256 of a seed and the unit id), so nobody picks the sample; entries for other units are ignored. Line 3, the toll, is router holds per 100 organic prompts and fixed-reply probes, from the transcripts and BIND W2's `hold_events.jsonl` when it has rows: the worse source decides, a probe fails the line at any n, and too few prompts reads INSUFFICIENT only when nothing shows the bar broken. Line 4, the outcome rate under the fixed audit, is reported, not gated (D-359), and an estimate (labelled) of the Claude output tokens and dollars a bound unit avoids follows.

  The audit gains `not_applicable` (outside `attempted`, allowlisted signals only), `audit_version`, binding, hold and dedupe counts in its summary, and `--until` to reproduce a window. `scripts/release/audit_agreement.py` re-runs the audit read-only and reports agreement with text-free fixtures of one model's unblinded labels (`tests/fixtures/bind_w3/`: ids are hashes, no prompt or reply text), an in-sample check of the rules against the rows they were written from, not a validation. `tests/bind_mutations.py` changes the audit, gate and report one edit at a time and lists the edits no test notices; `tests/test_bind_thresholds.py` pins every threshold of the bar on both sides.
- **Commit messages and PR bodies are written by the local model and bound into the command, not by Claude (BIND W7, owner decision D-360).** The model writes `git commit -m "${LLM_TEXT:?}"` or `gh pr create --title "<short>" --body "${LLM_TEXT:?}"`; the new PreToolUse[Bash] hook `llm_router-text-job-bind.py` builds the input from the real staged diff (or the branch's commits and diff), runs the router's own `llm_text_job` (local model only, the same checks), writes the checked text to a private file and rewrites the command to `-F <file>` / `--body-file <file>` through `updatedInput`, keeping every other field of the tool input and putting the generated subject into the Bash `description` so that the person approving sees what is committed. Every flag in the command survives the rewrite (`-am`, `-sm`, `-db`, `--draft`), and the hook proves it before emitting. Only the spelling `"${LLM_TEXT:?}"` is the placeholder (proved against `bash` and `zsh`); the variable written where the shell would not replace it (single quotes, a backslash, a here-document, without the `:?` guard) is denied, never passed through, unless every command on the line is a harmless reader (`echo`, `grep`, `cat`, ...); that holds for a line the scanner cannot read too, judged on its text. A commit message is the model's subject plus, by default, a deterministic list of the changed files with their line counts (`LLM_ROUTER_TEXT_JOB_BODY=model|files|none`); mentions, closing keywords and a model-written `Test plan` are neutralised in what is published. If the job is rejected, fails, times out, finds the machine's one job slot busy, or cannot run, or the command has a shape that cannot be bound, the command is denied with a short reason and Claude writes the text itself; a placeholder command is never allowed through unchanged. The placeholder is a shell variable with `:?`, so if the hook is not registered or is killed at its timeout the command does not run and the placeholder is never committed or posted. The hook's own git reads do not write the index and cannot run a program named in the repository's configuration or hooks (file-system monitor, pager, external diff, textconv, filters, `post-index-change`, `core.hooksPath`), as far as the known mechanisms go; a list of filters cut at the byte cap refuses the diff; a diff past the byte cap still reports its true file count, and the hook loads `$LLM_ROUTER_HOME/.env` as the MCP server does. The flag and lane gate runs before the model libraries are imported, so a job the router would hand back anyway is denied before the import (p50 96 ms, p95 109 ms; n=20 runs of the real hook script, stub model, 2026-11-04), and the wait for the job slot is booked as queue time, not router time. Only the user's own `$LLM_ROUTER_HOME/.env` is loaded, never the repository's. The generated text is cleaned before it is published (mentions, closing keywords, any test/testing/verification section, remote images and links, hostile file names in the files list). The adoption figure comes from transcripts of commands that really run `git commit` / `gh pr create`, never from the ledger. Jobs run one at a time per machine (a lock; a job that cannot get it in time is told "busy, write it yourself"), each model attempt is capped at the MCP door's per-attempt share, and the router import is booked as router time, not as the model's wait. The hook's stdout is exactly one JSON object (a private copy of file descriptor 1 carries it; the real script is run as a subprocess and parsed with `json.loads` in `tests/test_w7_hook_script.py`). New: a text-free `text_job_binds.jsonl` ledger and `python -m llm_router.text_job_bind report` (window and units, latency of every outcome, command-shape refusals apart from model failures, adoption from `--transcripts` or n/a with the reason); the hook is instrumented for G1 and its model wait is the new `text_job` model phase. The router's lane safety valve now judges jobs, not attempts: a job fails only if every try is rejected, and timeouts, busy, cooldown and a model that is not available do not count. The rules text teaches the placeholder (rules version 12) only when the hook is registered in `settings.json` AND a passed `commit_message` lane exists AND the off switch (`LLM_ROUTER_LOCAL_TEXT_JOBS` set to anything but 1/true/on/yes, in the environment or in `$LLM_ROUTER_HOME/.env`) is not set, with the pull-request part also needing a passed `pr_description` lane, and `llm-router doctor` / `verify` report a mismatch. Deploy: `llm-router install --force` once (registers the hook with timeout `130` and `"if": "Bash(*LLM_TEXT*)"`, so Claude Code spawns it only for a placeholder command; the Claude plugin bundle carries the same entry, the Codex bundle does not). Because the filter hides hand-written commit and PR commands from the hook, the report needs `--transcripts` for an adoption figure. Every git the hook starts gets a scrubbed environment (none of the owner's provider keys), filters of every name are neutralised, and the hook process runs from the owner's state directory so that the router's configuration is not read from the repository. Every URL that fetches or runs something, every markdown image marker and every embedding HTML tag is removed from the generated text by linear scans (best effort), and a stray placeholder on a line that may run git or gh (including `$G commit` and wrappers) is denied. The text-job-bind hook gets its own G1 budget of 1,500 ms router-added (D-370); other hooks keep their existing budgets. Design, limits and measurement: `docs/text-job-bind.md`.
- Status line, two per-session numbers computed by the detached refresher (owner goal 2026-11-04: more than 15% of LLM calls routed to a non-Claude or local model; Sonnet and Haiku do not count): `routed X% (n/N calls, session)` and `local OK Y% (a/b, session)`; `local OK n/a (0 attempts, session)` when the session made no local attempt, never 0%. Where it shows: default layout, a third row under the snapshot row (also in `LLM_ROUTER_STATUSLINE=both`, where the full part prints it and the tick prints none, so it never doubles); `LLM_ROUTER_STATUSLINE=fast`, a row under the tick line once the session has a snapshot. Units: a call is one model API response, an attempt is one try of one model by the router's chain, a run is one delegated `codex exec` (many calls). N is the unique `message.id` over the session's main, `subagents/agent-*.jsonl` and `subagents/workflows/wf_*/agent-*.jsonl` transcripts plus the non-Claude calls. n is each model response of a DELEGATED Codex run (read from the rollout, Codex's own sub-agent threads included; no rollout counts as one and the row says `1 Codex run est.`), each router attempt a non-Claude provider answered and the router accepted (`execution_events.attempt_completed`, joined to the session through the route's `correlation_id`, which is only 8 hex characters, so the join also needs the trace ids to agree or the attempt to end inside the 15 minutes before the route was logged, and a route that cannot be told from another is left out; an MCP `llm_*` call logged under `unknown:mcp` is joined by the transcript's `tool_use` id), and each Claude Code message the proxy served from a non-Claude model. A `codex_blocked` or `codex_failed` run is discarded work (`northstar._CODEX_ATTEMPT_OUTCOMES`: Claude re-runs it) and is never in n. b is the session's local attempts, every outcome (`attempt_completed`, `attempt_failed`, `attempt_rejected`), so a first-choice model that is not installed and is rescued by the next chain entry is a failed attempt; a is those used: completed and not rejected afterwards by a text job's acceptance check (`text_job_local_attempts.jsonl`; a rejection is matched to the attempt of the same `llm_text_job` call by its `tool_use` id, else of the same session, never by model and time alone, and `text_job_ledger` now writes `session_id` and `tool_use_id` on those rows; rows written before read as used). `routing_decisions.success` is not used (it is "the response is usable" for the whole call). Definitions: `src/llm_router/session_routing.py`; tests: `tests/test_session_routing.py`. Measured on a copy of the real data taken 2026-11-04 about 08:05Z (reproduce with the command in `src/llm_router/session_routing.py`; outputs in `~/.rsi/research/primary-plan/v16/w9-evidence/`): session 1a45a332, n=13,924 calls = 13,872 Claude + 52 non-Claude (48 Codex responses of 1 delegated run, 4 local attempts used); 5 discarded Codex runs (53 responses) are outside the share; local 4 of 9 attempts, the 4 failures being the same not-installed first-choice model (`qwen3.8:latest`, NotFound) on each of 4 routes that `qwen3.5:latest` then answered. Row: `routed 0.37% (52/13924 calls, session) · local OK 44% (4/9, session)`.
- The local routing verdict now runs in the hooks (`llm_router/door_verdict.py`), not only in the proxy shadow. `auto-route.py` logs one text-free row per routed prompt and `agent-route.py` one per routed Agent spawn in `door_verdicts.jsonl` (verdict, door, residency, latency, outcome, `tool_use_id`), written by a detached worker so no prompt waits for it. With `LLM_ROUTER_LOCAL_CLASSIFIER=on` the tier verdict is a difficulty signal that picks the door: opus at p_max >= 0.6 keeps the work on Claude (hint quieted, or no Codex attempt for the spawn); haiku at >= 0.7 goes local first where a local lane can do the job (a passed quality batch, or a commit-message / PR-description prompt with the local text-job switch on; `preferred_door(verdict, kind=..., local_lane=...)` is the seam for a sub-agent fallback); sonnet, or haiku with no lane, goes to Codex first (the prompt hint gains `llm(prompt="codex: ...")` when Codex is usable; spawns keep the existing Codex gates). Thresholds: `LLM_ROUTER_DOOR_OPUS_MIN_CONF`, `..._HAIKU_MIN_CONF`, `..._SONNET_MIN_CONF`. Of the proxy's turn-first verdicts in `classifier_shadow.jsonl` (n=1,755, 2026-10-28 to 2026-11-03; not hook traffic, which was not logged before) 4.1% reach the Claude door at its threshold, 6.8% are local-eligible before the lane test (those fall to Codex while no lane passes), and 60.2% (sonnet >= 0.5 plus haiku >= 0.7 with no lane) reach the Codex door for a prompt, when Codex is usable (binary, not benched, meter reserve, window). A verdict opens no door the existing gates keep shut. A breaker opens only after two failures in a row. `scripts/door_verdict_report.py` reads the ledger; `scripts/door_verdict_check.py` and `scripts/door_verdict_hook_cost.py` are real checks capped at 30 and 20 model calls.
- Seam and log rows for a local model in front of Claude when Codex has no capacity (owner decision D-372); no local runner is built, because the evidence does not support one. Of 1,049 organic sub-agent decisions that Codex declined for lack of capacity (bench 934, window 104, quota 11; `north_star_units.jsonl` copy, lever `agent_route_codex`, 2026-10-04T17:57Z..2026-11-03T17:57Z), 1,048 were classified complex and none simple; for the 843 whose prompt preview (first 500 characters, `agent_calls_ledger.jsonl` copy, joined by time and sub-agent type) could be read, 0 pass the strict gate (short, read-only, one existing file, untyped agent, simple): 843 not simple, 817 not a question, 799 name a write or a run, 481 name no file; relaxing any single rule admits 0. The measured quality (`docs/BACKEND-QUALITY.md`, mechanically scored): local ties Codex and Claude on contained single-file tasks (24/25 each, 25 tasks) and falls to 6/10 on the hard suite (0/3 on its questions, which cross files) and 8/11 on the brutal one (1/2 on questions), against 10/10 and 10/11 for the cloud agents. `agent-route.py` now records, for each task Codex declined for capacity, a `local_candidate` or `local_ineligible` row with every rule it failed (`agent_route_local.jsonl`, `src/llm_router/agent_route_local.py`; kept out of `north_star_units.jsonl`, which is the North Star signal); nothing is started or bound. A runner is worth building when those rows show a share that passes the gate.

### Changed
- Delegated Codex runs (`agent-route.py`, including its Phase-2 CLI path) now choose their sandbox from the session's directory, the hook payload's `cwd` (owner decision D-357, ruled "write in repo"). A session inside a git work tree gets `--sandbox workspace-write` with the network off (`sandbox_workspace_write.network_access=false`), and the work tree's top level is the only writable root and Codex's working directory. A session rooted at the home directory, outside any repository, or in a repository that sits at or above the home directory gets `--sandbox read-only` (network off as well), and a reply that reports blocked work is not delivered, so Claude's sub-agent does the task. A session rooted at `/` is not delegated (`codex_unsafe_cwd`). Before, every delegated run was read-only and most answers reported blocked work. The router chain and the MCP tools still pass no sandbox flag, and `danger-full-access` cannot be requested through `run_codex`. The Phase-2 path now has the blocked-answer gate the NS3 path had. `delegated` and `codex_blocked` ledger rows carry the `sandbox` the run had. `northstar` counts a `codex_blocked` row as a discarded attempt. Delegated runs now also pass `--ignore-rules` (no execpolicy rule, user or project, can run a command outside the sandbox with network: reproduced on the real binary, `docs/bugs/BIND-6.md`) and, in workspace-write, close `/tmp` and `$TMPDIR`; the child's `TMPDIR` is a per-run directory inside the work tree, ignored by git and removed after the run. Every other `codex exec` now passes `--ignore-rules` too (the router chain, the MCP tool, the session broker, the model evaluator, calibration, the agentic adapter, which also passes `--ignore-user-config`, and the benchmark script; the frozen CLS-DEV study script is the one exception), and the router paths do not start a Codex CLI whose `exec --help` does not list the flag: the call fails with `reason_code=no_ignore_rules`, one WARNING and a `CHZ-FO-CODEX-NO-IGNORE-RULES` fail-open row, and routing falls back (`docs/bugs/BIND-8.md`). Delegated runs and the agentic adapter's sandboxed run also turn off Codex's hosted `web_search` tool, which the model provider runs outside the sandbox (`docs/bugs/BIND-9.md`). The ten hooks' LiteLLM guard now forces `LITELLM_LOG=ERROR` even when the user's environment says DEBUG (`docs/bugs/BIND-10.md`). The writable root must also be the nearest directory with a `.git` entry at or above the session's, so a repository's own `core.worktree` cannot widen it to an ancestor (`docs/bugs/BIND-7.md`). The blocked-answer gate now catches the network and approval wording of a writable run, no longer flags a finding that mentions "connection refused" or "this session", and passes a reply that could not save its report file but carries the report (precision and recall before and after: `docs/bugs/BIND-2.md`). The `delegated` ledger row carries `answer_chars`, and the shadow verifier's marker is queued only after a writable run (a read-only run's `git diff HEAD` is the user's own work).
- Status line rows (owner decision D-364, `src/llm_router/statusline_tick.py`, `src/llm_router/hooks/statusline-command.sh`): a new row under the tick row prints two progress lines beside a blind NS, `off-Claude 0.06% (4/7116 calls, 7d, organic)` and `<Opus 86.9% (6181/7112 Claude calls, 7d, organic)` (shape only; n=7,116 calls and n=7,112 Claude calls were the owner's copy on 2026-11-03), computed in the same refresher pass, window and organic population as NS, one call being one unique API message with sub-agents included. Every n carries its unit and window: the mix reads `⚖ 4L/25S/0C/0P·session ledger rows 29 of ~6,900 (0.4%)` (S is the Claude seat, C every other seat such as Codex), money reads `n=43 calls` (the 53 rows of the local day on the 2026-11-03 19:16Z copy of `usage.db`, `docs/bugs/MONEY3X-1.md`), NS reads `0/n (blind) 7d units`. `llm-router kpi` prints the two lines and `kpi --json` carries `call_mix`. `NS_ATTACH_VERIFY_RECORDS` in `src/llm_router/northstar.py` is the seam a verifier connects through. A deploy refreshes `llm_router-statusline.sh`, `llm_router_statusline_segments.py` and `llm_router_statusline_tick.py` in `~/.claude/hooks/` and the installed package.
- `llm-router kpi` rates keep enough places for a small share: D1 printed `0.0%` for 4 of 8,083 calls, one line above `off-Claude 0.05%` for the same 4 calls; a share that one decimal would round to zero now prints two places (`0.05%`), `<0.01%` below that, and only a numerator of 0 prints `0.0%` (`src/llm_router/commands/kpi.py`, `docs/bugs/NSZERO-1.md`). D1 and D2 are also a different series from this change on, not a continuation: they read the new unit stream (sub-agent calls in, one unit per API message), and on one copy of this machine's data `kpi --json --days 7` read D1 `0.1% (n=2294)` over 5,138 window units before and `0.0% (n=8083)` over 15,219 after (n=1 copy, 7 d to 2026-11-03T20:54Z, `~/.rsi/research/primary-plan/v16/coord/rv-PR821-20261103.md`).
- The status-bar hook and the ledger line that `llm-router kpi`, `llm-router summary` and the dashboard summary print (`src/llm_router/hooks/status-bar.py`, `src/llm_router/ledger_view.py`, `docs/bugs/MIXSEAT-1.md`) now keep the Claude seat and every other subscription seat apart and say what their n counts, as the status line does: the hook's full mode prints `claude:25 other:0 free:4 paid:0 n=29 ledger rows, session` (it was `sub:25 free:4 paid:0 n=29 session`) and `est n=44 calls`; the ledger line prints `subscription 10127 (Claude seat A + other seats B)` and `est. saved $4.92 (n=634 calls)` (it was `subscription 10127`, `(n=634)`; the review's 7-day copy of this machine's data, 2026-11-03, PR #821). A package that predates the split keeps the one bucket. The installed hook is re-copied when the MCP server starts.
- `llm-router statusline --refresh` (and `llm-router kpi`, `llm-router northstar`) no longer decode every sub-agent transcript on every run: `northstar.units(subagents=True)` keeps the id, model and time of each message per sub-agent file in `northstar_subagent_index.json` (the state dir, 0600, no message text), keyed by the file's `(size, mtime_ns)`, so an unchanged file is not read again; a file that holds an MCP call, or a message a proxy served, is still read in full (`src/llm_router/northstar.py`, `docs/bugs/REFRESHCOST-1.md`). Measured with PR #821's `statusline_refresh.build()` on a copy of 8 days of this machine's data (278 sub-agent files, 4 organic sessions, 2026-11-03), n=5 runs per build, interleaved: median wall 11.2 s and CPU 9.7 s on main 16fc9296, 13.3 s and 11.6 s before the index, 9.6 s and 8.9 s with it once the index is warm (n=4 runs).
- The 7-day organic row of the fast tick (#821, `off-Claude ... · <Opus ...`) counts a delegated Codex run as its model responses instead of one call, takes back the units of discarded Codex runs, and adds `local OK Y% (a/b, 7d, organic)` with the same attempt definition and population as the session row (a route logged under `unknown:mcp` is placed through the transcript's `tool_use` id; local attempts no session can be found for are counted apart, `unattributed_attempts`). Because `llm-router kpi` keeps one call per run and prints `off-Claude`, the status line names its own figure `routed (incl. Codex turns)` and prints `N Codex runs est.` for delegated runs it had to count as one response. On the same copy (reproduce: `~/.rsi/research/primary-plan/v16/w9-evidence/`) the row moved from `off-Claude 0.07% (10/13882 calls, 7d, organic)` to `routed (incl. Codex turns) 0.37% (52/13924 calls, 7d, organic) · <Opus 93.1% (12908/13872 Claude calls, 7d, organic) · local OK 44% (4/9, 7d, organic)`, the same 52/13924 as the session row. `codex_agent.CodexResult.thread_id` carries the thread the run opened and agent-route records it in `north_star_units.jsonl` (`delegated`, `codex_blocked`, `codex_failed` rows), which names the rollout to count; earlier rows are matched by start time and only when exactly one top-level `codex exec` rollout started then. The tick (`python3 -I -S src/llm_router/statusline_tick.py`) costs +0.1 to +1.6 ms median per render, measured as alternating runs of n=150 each on a copy of the real home (47.5 to 48.2 ms, 55.7 to 55.8 ms and, at load average 7.5, 76.7 to 78.3 ms with equal means; script `bench_tick.py` and output `tick-bench.txt` in `~/.rsi/research/primary-plan/v16/w9-evidence/`); the refresher's cold scan of the 198 transcript files of the measured session took 1.05 s, an idle refresh 0.03 s (`refresher-timing.txt`, same directory). A Codex run counted from token-count events (the fallback for a CLI without `token_usage_record`) counts a replayed parent line once, and an unreadable attempt ledger prints `local OK n/a (7d, organic)` instead of `0 attempts`.
- `LLM_ROUTER_LOCAL_CLASSIFIER` unset now means `shadow` when the decision model is configured (`LLM_ROUTER_CLASSIFIER_BACKEND=systemone` and `LLM_ROUTER_DECISION_MODEL`), else `off`; an explicit `off` still turns it off. `shadow` is log-only and never evicts another lane's model: it classifies only when the decision model is already resident or no model is, and otherwise logs `skipped_slot_busy` without a request; session start loads the decision model only in `on` (opt out there with `LLM_ROUTER_CLASSIFIER_PRELOAD=off`; in `on` the zero-Claude edit model is no longer pinned at session start, and 0 of 571 real prompts were edit-eligible in 30 days). The classifier refuses a non-loopback Ollama (`refused_remote`) unless `LLM_ROUTER_CLASSIFIER_ALLOW_REMOTE=1`, in the proxy path and the hooks alike, and a prompt that holds a secret is classified by no model (`withheld_secret`). Measured on the real hook (2026-11-04, 20 model calls, load average 5.1-6.5): shadow adds p50 17 ms and p95 37 ms per prompt over a classifier-off baseline of 120 ms (n=6 warm, n=4 after an eviction: p95 53 ms), because a detached worker makes the call. A prompt decided in the hook's own process, probe included, is held at most `LLM_ROUTER_DOOR_VERDICT_PROMPT_BUDGET_MS` (2,000) and one that finds the model not resident does not wait for the load; a hook that exits early does not wait for a verdict. In `on`, the prompt hint names Codex only when the Codex meter reserve (D-373, as the router chain) and the 5 h window also allow it, and a prompt kept on Claude prints its own observation line (`local-verdict`) instead of the generic shadow one.
- A Codex bench no longer outlives the evidence against it (routing plan R2, 2026-11-04). A fresh meter reading, taken after the bench was recorded (and checked again against the bench file under its lock) and showing the 5 h window under 90% and the weekly window under 95%, lifts the bench; a reading that predates the bench, is older than 30 minutes or shows one window only does not (the 14 fresh-meter `window_exhausted` refusals of 2026-10-04..11-03 were all correct). While a bench has more than 24 hours left, one real delegation per 3 hours is let through as a canary, and a success lifts the bench. The slot is claimed before a window slot or a daily-budget unit is spent, handed back when the task is then declined or never reaches Codex, and four sub-agents spawned together make one probe and spend one budget unit. Both are logged to `codex_meter.jsonl` and to `provider_bench.jsonl` as an unban with a `source` (`meter`, `canary`), so KPI G4 counts them as wrong benches; `LLM_ROUTER_CODEX_BENCH_PROBE=off` switches both off. The three silent `except` returns in `agent-route.py` (bench read, window reserve, window spend; also window release) now record `failopen` codes `CHZ-FO-AGENT-ROUTE-CODEX-BENCH-READ`, `-WINDOW-RESERVE`, `-WINDOW-SPEND`, `-WINDOW-RELEASE`. Hook stamp 27 to 28.
- The Codex Plus meter is reserved for sub-agent delegation (owner decision D-373). Every Codex caller except agent-route (the `llm_codex` MCP tool, the router chain, Codex-served text jobs, the evaluator, the calibrator, the session broker, tier 1 of `llm_delegate` / `llm_act` (`agentic/adapters.py` `CodexAdapter`, which starts `codex exec` itself and is guarded as `agentic_codex`), and any caller that names nothing) now refuses once the WEEKLY meter reaches 60% (`LLM_ROUTER_CODEX_RESERVE_PCT`, `off` disables) or the 5 h meter reaches 90%. The meter is read from the newest `limit_id: codex` `payload.rate_limits` in Codex's own rollout files (`src/llm_router/codex_meter.py`, read-only, newest by event time, `used_percent` validated 0-100). A reading under 30 minutes old decides; an older one can still refuse, because a window's percentage only rises until its `resets_at` (weekly 80% read at 10:00 still refuses at 11:00); an unreadable rollout directory or an unknown format is counted with `failopen`. With nothing deciding, the router's own counters decide (100/day agent-route budget, 5 h window) and if those cannot be read the call is refused, never opened. The router chain asks before it builds anything for a Codex entry and skips it as a policy skip (no health failure, no `attempt_failed` row, the walk goes on); `run_codex` asks again for every other caller (`reason_code=meter_reserved`, nothing started). Every refusal is written to `codex_meter.jsonl`, and its wording cannot be parsed as a usage-limit report, so it never benches Codex. Hook 28 passes `caller` only to a `run_codex` that takes it, and a `run_codex` call that passes a sandbox and no caller (hook 27) is read as agent-route's, so neither order of deploying the hook and the package breaks delegation. Not covered: the CLS and backend-bench harnesses start `codex` with their own argv (`scripts/cls_send_lib.py`, `scripts/bench_backend_quality.py`).

### Removed
- `llm-router share` is removed (owner decision, 2026-11-03). It printed a savings card written for posting ("llm_router saved me ... Open-source MCP router for Claude Code ... github.com/ypollak2/llm_router"), copied it to the clipboard and opened a browser tab on a tweet-intent URL; `share --svg` wrote the same card as an image. `llm-router share`, with or without `--svg` or `--help`, now answers `unknown command 'share'` and exits with status 2. The module, the dispatch branch, the `--help` line, the `cli_share` entry in the savings surface registry and the tests that existed only for the card are gone. Every other savings and statistics surface (`summary`, `gain`, `savings-report`, `stats`, `status`, `dashboard`, the statusline) is unchanged. `tests/test_w6_share_command_removed.py` pins the removal and fails if the card's wording returns anywhere under `src/` or `hooks/`. One dead word remains: `share` is still listed among the admin subcommands in `hooks/enforce-route.py` (and its copy under `src/llm_router/hooks/`); it is harmless there and goes with the next edit of that file.

### Fixed
- A local model that hits the MCP per-attempt cap now moves behind other candidates during the local timeout cooldown, so later routes can try another model first; the cap keeps its existing reporting and does not count as a provider failure or bench Ollama.
- The release report's Codex binding rate missed delegated results when the Claude Code harness recorded an error with top-level `toolDenialKind: permission-rule` instead of the old `PreToolUse:Agent hook error` prefix. The audit now recognises both rendered hook-denial shapes, still requires an error result, and leaves other denial kinds and unmarked errors in `delivered_without_deny_shape`; transcripts retaining the hook's own stdout still use its deny contract.
- The Codex agent-route handoff is binding. `hooks/agent-route.py` printed a log line to stdout in front of its decision, so Claude Code ignored every handoff and ran its own sub-agent as well (`docs/bugs/BIND-1.md`). The hook now prints exactly one JSON object (library logging goes to stderr, and stdout stays on stderr until the process exits), every block is a deny in both the current and the legacy shape (`docs/bugs/BIND-5.md`), and the Codex result is delivered as the sub-agent's result (untrusted data, not an error, with an escape hatch) instead of an order not to redo the work. The same missing stderr guard is fixed in ten other hooks (`agent-depth-release`, `bash-compress`, `cc-usage-track`, `context-capture`, `playwright-compress`, `session-end`, `session-start`, `status-bar`, `subagent-start`, `usage-refresh`; `enforce-route` has its own guard and tests in the MCP-hold change), and `tests/test_bind_hooks_stdout_canary.py` runs every other shipped hook that imports `llm_router` under a log-line canary. A Codex reply that reports blocked or read-only work is no longer delivered: it is logged `codex_blocked` and the Agent call runs (`docs/bugs/BIND-2.md`). The gpt-5.5 fallback no longer follows a usage limit or a bench, and no Codex run starts with too little hook time left (`docs/bugs/BIND-3.md`). Every `agent_route_codex` ledger row carries the Agent call's `tool_use_id`; a delegation the host discarded is recorded as `delegation_void` by the PostToolUse[Agent] hook, three of them in one session stop Codex delegation in that session, and asking for the same prompt again goes to a Claude sub-agent (`docs/bugs/BIND-4.md`). The ten hooks also keep LiteLLM's own log handler off stdout (`LITELLM_LOG` default and its logger levels), with a canary for it.
- The release outcome audit scores routed work correctly, audit v2 (BIND W3; D-356). On the 16.0.0 window (2026-10-04..2026-11-03, n=564 units in `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl`) the audit had nine defects, each recorded in `docs/bugs/BINDAUDIT-1..9.md` with its test: Codex units paired with the latest Agent call, not their own (31% wrong); sub-agent transcripts never read (nested spawns unknown); `session_kind_overrides.json` ignored (182 of the 564 units were in two sessions the owner marked non-organic); MCP answers scored on the JSON envelope, header and footer (a verbatim relay reached `used` 9 of 113 times); tool inputs and `llm_local_task` file effects not counted as use; fixed-reply probes and status views in the denominator; replayed history counted twice (8); a backgrounded call scored on its stub; and the gate comparing rates across methods.

  What was checked, and what it does not show: one model, unblinded and with no second reader, read 193 of those 564 units, the 35 Codex units of BIND-R1 and the 158 `routed_mcp` rows of BIND-R2 (n=193, `~/.rsi/research/primary-plan/v16/coord/BIND-R2-mcp-unused-20261103.md`); the other 371 Codex units were never read. The rules were written against those same rows, so the agreement is in-sample: it shows the rules reproduce the labels they were fitted to, not that they generalise. On that window (read-only, `scripts/release/audit_agreement.py`, overrides lifted, n=193): the 35 Codex units are paired with their own call (the old audit: 26 of 35) and read `redone / claude_subagent_ran`; 144 of the 158 `routed_mcp` rows carry BIND-R2's label (the old audit: 43 of 158, 27.2%), and the 10 the audit calls used are the 10 its use signals were written for (the old audit: 0). The 35 of 35 `not bound` is no test of `bound`: all 35 are void by construction (the deny was honoured 0 of 35 times), so no bound unit exists in this data and the positive path of `bound` has met only synthetic transcripts in `tests/test_bind_audit.py`; the first real positive control is the BIND W5 canary.

  The fixed audit reads 10 used of 467 attempted (2.141%) on that window where the old one read 0 of 564 (n=556 and n=564 units, `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl`), so it is not comparable with any earlier number: `outcome_gate.py` records `audit_version` and the sha256 of the artifact, compares only within a version, and on a mismatch prints it and FAILS unless `--cross-version rebaseline|compare` is given. Owner decision D-359 is REBASELINE: the audit v2 number of the release that first records it becomes the baseline, compared with nothing, and `scripts/release/pre-release-verify.sh --outcome-cross-version rebaseline` forwards the flag to step 13 once. The baseline is then sticky: a re-run for the same release needs no flag and is compared with the recorded entry, a worse artifact fails, and nothing replaces the entry unless `--force-baseline "<reason>"` is given (the independent review of PR #816 reproduced a 0 of 291 artifact overwriting 3.093% at 4ccd2cfd). 15.4.0's recorded 0.229% (2 of 873 units, `docs/measurements/outcome_audit_history.json`) is an audit v1 number and cannot be re-scored (transcripts purged).

  Not fixed: BIND-R2 class (a) "the host did the same job itself" (8 rows) and class (c) "adopted, reworded" (3) have no lexical signal and stay `unused`/`unknown`; a task asked again in other words after a delivered Codex result is not read as a re-issue (the router's escape hatch matches the identical prompt); 67 router calls made inside sub-agent transcripts (30 days, all sessions) are not scored; the gate's same-version ratchet compares point rates with no tolerance.
- **A routing hold no longer blocks read-only shell inspection on question-and-answer prompts (D-396).** Before, a pending route for a Q&A task type (query, research, analyze, ...) held read-only Bash (`ls`, `cat`, `git log`, `gh pr view`), which code tasks already let through, although `Read`, `Glob`, `Grep` and `LS` already passed for every task, so a hold on a question held the commands that gather its context. Read-only Bash now passes for every task type in `smart` and `hard` (tested with query and research tasks), but not in `strict`; it does not satisfy or clear the pending route, Bash that writes (`>` redirects, `rm`, `git push`, installs) is still held, and `strict` keeps holding every Bash call. Deploy: `llm-router install --force` (enforce-route 27).
- The enforcement hook no longer denies a sub-agent's tool call because of the main thread's pending route. Outside `strict`, the call still counts towards the trap, loop and count releases, so the main thread's hold is released when it was before, and it passes. In `hold_events.jsonl`, 2026-11-03T20:25Z..2026-11-04T21:44Z, session 1a45a332, all 4 `blocked` rows were sub-agent calls.
- A local route no longer spends its first attempt on a model Ollama does not have (`docs/bugs/PHANTOM-LOCAL-1.md`). On the owner's machine 9 of 94 local attempts in the 7 days to 2026-11-04T11:00Z (`routing_quality.jsonl`, 72 local routes; 7 of the 9 were the first attempt of their route) were Ollama's 404 for `qwen3.8:latest`, removed on 2026-10-11 but still named in an MCP server's `OLLAMA_BUDGET_MODELS`. `OLLAMA_BUDGET_MODELS` / `OLLAMA_MODELS` are now believed only for names a fresh installed list carries (`model_discovery.installed_only`), so `local_model()` and the `ollama/@local` chains no longer resolve to a missing name and no longer call the user's installed `LLM_ROUTER_OLLAMA_MODEL` "not installed"; the session-start dynamic routing table gets the installed-model filter the static chains already had; and an Ollama `model not found` is remembered for 600 s per process (the model is dropped from later chains, never to an empty one) and is no longer counted against the whole `ollama` provider, where two of them benched every installed model. The "set but not installed" warning is logged once per process instead of on every route. A before/after on a copy of the live state is in the PR.
- The proxy opens its port before it walks the state directory. `state_files.harden_state` (the 0600/0700 repair over every inode under `~/.llm-router`, run on every proxy start) used to finish first; it now runs on a daemon thread (`state_files.harden_state_in_background`, called from `src/llm_router/proxy/server.py`). The repair and its frequency (every start) are unchanged, and the root is resolved in the calling thread. Measured with `scripts/time_proxy_start.py` on a throwaway HOME holding a tree of this machine's state-directory size (43,648 files in 17,823 directories), 2026-11-04, n=8 starts per arm, load average 5.9 to 5.8: seconds until the port accepts a connection, median 0.857 before and 0.204 after; seconds until `GET /` is answered (that request is forwarded upstream, as a deploy's `curl` is), median 1.022 before and 0.368 after. On a tree three times that size (130,944 files in 53,469 directories), n=5 starts per arm: 2.289 before and 0.209 after, so the time to listen no longer depends on the size of the state directory. The MCP server (`src/llm_router/server.py`) still repairs before it serves.

  What this does not show: the slow start after the #824 deploy on 2026-11-04 (no answer after about 50 s in `~/.rsi/research/primary-plan/v16/coord/deploy-pr824-run-20261104T120527Z.log`; about 85 s at load 8.6 in the comment on line 189 of `coord/deploy-pr.sh`) was not reproduced. The walk costs under 1 s on this machine at that tree size, and four other logged proxy kickstarts that day answered inside a whole deploy run of 22 to 39 s (n=4, `deploy-w7-run-20261104T103735Z.log`, `deploy-pr822-run2-20261104T150959Z.log`, `deploy-pr823-run-20261104T170349Z.log`, `deploy-pr825-run-20261104T181440Z.log` in the same directory). The cause of the one slow start is unmeasured.
- `llm_text_job` threw away local-model work in two ways, and all are closed. (1) The chain of a text job followed its input: with no `complexity_hint`, `route_and_call` derived the tier from the prompt, a prompt over 2000 characters read as `complex`, the PREMIUM chain injects no local model, and `prefer_passed` adds nothing, so with `LLM_ROUTER_LOCAL_TEXT_JOBS` unset the passed lane was refused (`lane_refused`, "not in the router's chain") and with `=1` the local model was not in the chain at all; under `LLM_ROUTER_ENGINE_MODE=decide` the engine's verdict on the wording did the same. `llm_text_job` now passes a fixed `moderate` tier, so the passed model now leads the chain at any size or wording inside the lane's scope. (2) The generic quality score (`score_response`, `LLM_ROUTER_ESCALATE_THRESHOLD` 0.4) rates a plain one-line commit message under that threshold, so with `=1` the local answer the job's own check would accept was rejected as `low_quality` and a pricier model was called; every such score was also recorded for `should_skip_model`, which after three of them skipped the local model (flag unset: the fourth job in one server process was handed back as `local_failed`). A text job's answer (`eligibility_exempt_reason == TEXT_JOB_REASON`, which only `llm_text_job` passes) is no longer scored, escalated or recorded and goes to `check_output`; every other call keeps the escalation unchanged. The lane guards are untouched: enabled, passed, digest-verified, settings-matched, inside `scope.max_prompt_chars`, loopback only, and a pass-only job still never reaches a cloud model. Regression tests: `tests/test_lc12c.py` (real chain builder at 300, 2000 and 40000 characters with the flag unset and `=1`, the engine verdict, scope and no-lane refusals, the escalation, the quality breaker); `docs/bugs/TJCHAIN-1.md`, `docs/bugs/TJCHAIN-2.md`. (3) The router's structural compaction cut a large text-job prompt to its first 20 and last 10 lines, so the model described a change it could not see; a text job is no longer compacted (TJCHAIN-3).
- `llm_text_job` now hands back digit-heavy pass-only jobs whose whole-prompt estimate exceeds the selected local model's context budget, before any model call; ordinary text still runs and an unreadable window leaves the existing guards in charge. Regression tests: `tests/test_lc12c.py::test_dense_text_job_guard`, `test_ordinary_text_job_same_length_reaches_model`.
- **Routing holds no longer invite throwaway `llm_*` calls, and no longer fire on prompts that are not requests (BIND W2).** The `Routing directive BLOCKED` text used to say that calling any `llm_*` tool, even a trivial one, clears the lock, and the model did: `Reply OK` probes sent only to release the hold. A hold is now released only by a routing door whose input carries the owner's request (`llm`, `llm_route`, `llm_act`, `llm_edit`, ...); a prompt too short to compare (`push to main`) is released only by a call that restates it. A trivial or unrelated call, or a status view, still runs but leaves the hold in place and is logged as a probe. The free valves (the second block of one tool in a turn, three in two minutes, a fourth violation) and the `claude:` prefix still release without a model call; `strict` turns the three valves off on purpose and its block text and `SECURITY.md` now say so. In `smart` mode (the default) a prompt of ten words or fewer, or the "This session is being continued" summary Claude Code injects after a compaction, writes no hold; `hard` and `strict` keep every hold, and `soft` and `suggest` keep the release they had. `enforce-route.py` now writes its decision to a stdout reserved for one JSON object, to the end of the process, so a stray library print, or a failed exit-time latency write, can no longer void a hold or a local interception. A tool that crashes in an MCP server older than its package now says to reconnect with `/mcp`, and while Ollama is saturated a call that is still waiting on local models gives up after 20 s instead of the full 90 s deadline. New: a text-free `hold_events.jsonl` ledger (a row per prompt, hold, exemption, BLOCKED call, routing-door call, probe and release) and a `blocked_holds` counter in `llm-router doctor`; `python -m llm_router.hold_ledger --organic` prints BLOCKED, holds and fixed-reply probes per 100 prompts over organic sessions, with every session kind beside it. Deploy: `llm-router install --force` (hooks auto-route 79 and enforce-route 25), then reconnect with `/mcp`. Evidence and limits: `docs/bugs/BIND-TOLL-1.md`, `BIND-HOLD-1.md`, `BIND-STDOUT-1.md`, `BIND-MCP-STALE-1.md`, `BIND-MCP-BUSY-1.md` and the section "When a prompt holds a tool" in `docs/proxy.md`.
- `scripts/release/release.sh` runs on a stock Mac (W4, from the 2026-11-03 release dry run). The sdist leak check no longer blocks the legitimate module `src/llm_router/pricing.py`: the internal-document names (MONETIZATION, ROADMAP, STRATEGY, PRICING, PHASE_COMPLETION) still block in any path that carries one, whatever the file type, except Python source (`.py`, `.pyi`) under `src/`, and `.env`, `CLAUDE.md`, `.internal/`, `/research/`, `/deprecation/`, `/scripts/` and `/skills/` still block wherever they appear. The check now fails closed: an unreadable or empty sdist listing, or a failure of `grep` itself, stops the release instead of printing "sdist is clean". The test step runs through `run_with_timeout` (`timeout`, else `gtimeout`, else a perl fallback that exits 124 on timeout) instead of exiting 127 where GNU `timeout` is missing. `pyproject.toml`, `verify-version-sync.py` and `verify-plugin-sync.py` are read with the project interpreter (`uv run --frozen python`), or the script stops with a clear message when only a `python3` older than 3.11 is available, instead of failing on `import tomllib`. The default `RELEASE_TEST_TIMEOUT` is 7200 s (was 300 s); set `RELEASE_TEST_TIMEOUT=<seconds>` to change it. Tests: `tests/test_release_sh_portable.py`.
- The text-job-bind hook refuses two more kinds of line that mention `${LLM_TEXT:?}`, and relaxes nothing. First, `cp` and `mv` are no longer on the harmless list (they change files), so a stray mention next to one is refused instead of being left to the shell. Second, a line that mentions the placeholder, bound or stray, is refused when it sets a variable that changes how a later command resolves or runs: `PATH`, `CDPATH`, `HOME`, `XDG_CONFIG_HOME`, `IFS`, `BASH_ENV`, `ENV`, `ZDOTDIR`, `SHELLOPTS`, `BASHOPTS`, `PS4`, `EDITOR`, `VISUAL`, every `GIT_*` outside the six author and committer name, email and date variables, and every `DYLD_*` and `LD_*`. Every form counts: bare (`PATH=x;`), in front of a command, `export`, `declare`, `env`, `+=`, `${NAME:=x}`, and on a line the scanner cannot read any `NAME=` in the text. Before this, `PATH=/tmp/x; git commit -m "${LLM_TEXT:?}"` and `PATH=/tmp/x git commit -m "${LLM_TEXT:?}"` bound. The refusal names the variable and says what to write instead (`cd <dir> &&` or `git -C <dir>`). A plain commit or PR, `cd <dir> && git commit`, `git -C <dir> commit`, the six identity variables, and a name that is only read or only appears in a message are unchanged. Details and tests: `docs/text-job-bind.md`, `tests/test_w7_deny_only_env.py`.
- The text-job-bind hook refuses more of what can run something from a line that mentions `${LLM_TEXT:?}` where the shell does not replace it, and relaxes nothing: every line it denied before it still denies. The adversarial reviews found shapes that passed the "every command is a harmless reader" rule and then committed the literal text or ran a command: `$[x]` (arithmetic that evaluates its operand), `printf -v 'a[$(cmd)]'`, `${(e)x}`, `${!y}`, `$~x`, a `${NAME:?message}` whose message expands, an unquoted here-document whose body has `$(...)` or backticks, an option behind a variable (`rg -g -e "$O"`, `sort $O`), `./echo`, `test -v 'a[$(cmd)]'`, and a write into a shell startup file. These are now denied on every path, including a line that also binds a commit, and `test`, `[`, `less` and `more` are no longer on the harmless list. The same refusals apply to a line the scanner cannot read, which is also judged without trusting a quote: a here-document body counts as commands, and `NAME=value` in front of a command is refused. A refusal for a line with no commit in it now names what the hook cannot see through and what to do instead, instead of saying that a commit would carry the text. Details and tests: `docs/text-job-bind.md`, `tests/test_w7_deny_only.py`.
- Status line and `llm-router kpi` stop printing numbers that cannot be right (owner decision D-364). NS no longer reads `0.0%` when its strict numerator cannot be observed: with no verifier verdict among the counted units NS and D2 read `blind: strict 0/n`, and the status line prints `NS strict 0/n (blind) 7d units` (`src/llm_router/commands/kpi.py`, `docs/bugs/NSZERO-1.md`). NS counts what its definition says: sub-agent transcripts under `<session>/subagents/` are read, a call is one unique `message.id` (not one transcript record), and an MCP call takes the model of the router row that answered it (`src/llm_router/northstar.py`, `docs/bugs/NSSUB-1.md`; `kpi --json` `call_mix` on a copy of `~/.llm-router` and the organic transcripts taken 2026-11-03 19:29, 7 days, organic, PR #821 body: n=7,223 Claude calls, 6,450 of them sub-agent calls that were never read). The estimated saving counts a call once: a local call recorded in `usage`, `claude_usage` and `savings_stats` was three rows, so `today n=` summed rows and its dollars were inflated (`src/llm_router/ledger_view.py`, `docs/bugs/MONEY3X-1.md`; copy of `usage.db`, newest row 2026-11-03 19:16:36Z, the local day: n=53 rows became 43 calls). The session mix no longer puts the Claude seat and Codex in one bucket (`docs/bugs/MIXSEAT-1.md`).
- The Stop line's `north star (strict)` item and `llm-router northstar` no longer print a structural zero. With no verifier verdict among a session's units they say `north star (strict): blind (no verifier connected)` (the Stop line, a hot path: no n and no sub-agent scan) and `verified offload blind (no verifier connected)  (n=<n> units, <window>)` (the command, which now counts sub-agent calls like `llm-router kpi`). `northstar.report()` gains `strict_blind` and returns a null `strict_share` for a blind session (`src/llm_router/northstar.py`, `src/llm_router/commands/northstar.py`, `docs/bugs/NSZERO-2.md`; before: `north star (strict) 0% (n=1795)`, 1 session, 7 days, a copy of the owner's data on 2026-11-03).
- Status line `routed` and `local OK` missed the commits the text-job-bind hook had a local model write. The hook runs outside any MCP server, so its router rows (`routing_decisions`, `usage`, `execution_events`) are logged under `unknown:non_mcp` with no `tool_use` id, and the join to the session found nothing; the rows exist, they name no session. Each ACCEPTED bind job of the session (`text_job_binds.jsonl`, `kind: bound`, `outcome: accepted`) is now one call in n on the side of the model that served it; a job handed back (`denied`: rejected, fallback, timeout, error) is no call, and `handwritten` rows ran nothing. The job's own attempts (the `text_job_local_attempts.jsonl` rows of its `jid`: `ok` used, `rejected` thrown away, `error`/`infra` failed) are in local OK, and every attempt of a job handed back is in b with a = 0. Identity only: the bind row now carries the job's `jid` (`llm_text_job` puts it in its structured sink as soon as the job starts, so a job that times out or raises still names it; `GenResult.jid`; the engine writes it on every `bound`/`denied` row that ran the job) and the job is linked to its rows by that id and by nothing else; a row written before this (no `jid`), a `jid` with no rows, or rows naming another session link to nothing, and an accepted job then contributes only what its own row proves (the call and one used attempt on its model). The unfinished-job marker a job writes when it ends with no verdict (a rejected try followed by a refused retry) now says `unfinished: true` in `text_job_local_attempts.jsonl` and is not an attempt, so one local call is one attempt in b. A router attempt is dropped for a job only when its route carries the bind row's `tool_use` id, never by model and time. A ledger that exists and cannot be read (`text_job_binds.jsonl`, `text_job_local_attempts.jsonl`) makes the figures that need it n/a, not zero; one that does not exist holds nothing. The 7-day row counts the same jobs and is labelled `routed (incl. Codex turns and binds)`; when the Codex turns, bind jobs or local attempts cannot be built (an unreadable ledger) its routed share and local OK read `n/a (7d, organic)` instead of a count without them; `llm-router kpi` still prints `off-Claude` without them. Measured on a copy of the live home taken 2026-11-04 about 15:10Z (ledger rows and the figures in `~/.rsi/research/primary-plan/v16/w9-evidence/bind-followup.txt`): session 1a45a332 read `routed 0.52% (83/15904 calls, session) · local OK 44% (4/9, session)` and reads `routed 0.53% (84/15905 calls, session) · local OK 50% (5/10, session)`, the 10:39Z commit (a legacy row, no `jid`) counted once.
- Status line `routed` printed a numeric undercount when the Codex delegation ledger (`north_star_units.jsonl`) existed and could not be read: `northstar._load_north_star_ledger` reads any `OSError` as no rows, so `session_routing` counted zero Codex calls (and a 7-day row without them) instead of saying the number is unknown. `session_routing` now reads the ledger through `northstar._read_north_star_ledger`: a missing file holds no run (counts nothing), a file that exists and cannot be read raises `LedgerUnreadable`, which makes the session's routed share `n/a` (the local OK figure does not depend on it and stays) and the 7-day routed share `n/a (7d, organic)` while preserving its measured local OK. An unreadable bind ledger still makes both 7-day figures unknown. `_load_north_star_ledger` keeps its old behaviour for the unit builders. Rollouts (the per-run response counts) are unchanged: a run whose rollout cannot be found or read is still counted as one response and named `N Codex runs est.`, an estimate and not a zero.
- `decision_classifier.parse_answer` refused an exact tie at the top of the tier probabilities as `parse_error`. The endpoint quantises, and 3 of the 6 replayed `parse_error` prompts were ties (haiku = sonnet, p_max below 0.5). A tie is now a verdict at the highest tied tier. The classifier shadow had 93 `parse_error` rows of 2,417 in 7 days (2026-10-28 to 2026-11-03); how many were ties is measured only for those 6. Four bug entries: CLS-HOOK-1, CLS-COLD-1, CLS-COOLDOWN-1, CLS-TIE-1.

### Security
- The secret door now holds private-key blocks in every format a person pastes, and ten more provider keys (`docs/bugs/SECRETDOOR-KEYS-1.md`). A census of the delegation gates found a PEM block with a short body going to Codex. Held now: a truncated or one-line PEM, a key inside a JSON or YAML string (escaped newlines, as in a Google service-account file), a legacy encrypted PEM with its armor headers, PGP `PRIVATE KEY BLOCK`, the four-dash ssh.com form, PuTTY `.ppk`; and Stripe live, SendGrid, Hugging Face, npm, GitLab, PyPI, DigitalOcean, Slack webhook, age and Azure storage keys. A bare header in a question, an elided body and filler still pass. On 131 real first prompts of workflow agents (675,930 characters) and 1,388 stored sub-agent prompt previews (692,392 characters) the old and the new door hold the same texts: 0 and 0.

### Added
North Star stack and live local-agent serving beside the tier path (NS-L, #855). Proxy turn handling: sub-agent threads are exempt by `is_main_thread`, with a 30 s first-token deadline in combined mode (#856); `LLM_ROUTER_LOCAL_EXEMPT` chooses which turns are exempt from local serving (#858); a conversation is keyed on its whole first message so threads stop sharing one pin (#859); `LLM_ROUTER_CLAUDE_MARKER` keeps marked conversations off local (#914); the classifier can decide local, flagged and default off (#915).
One local model, memory-safe: `LLM_ROUTER_LOCAL_MODEL` and `_URL` run a single big local model on its own Ollama server with a resident guard (#864); a memory-budget guard stops any local model load past machine memory (#866) and counts the context window (KV cache) as well as the weights (#870); the local classifier shares the proxy runner's context and keep-alive (#867); `num_ctx` is computed from the resolved local model so a call no longer reloads the shared runner (#885); `LLM_ROUTER_SHARED_KEEP_ALIVE` bounds the model's idle time (#912); `LLM_ROUTER_WINDOW_STEP` raises the shared local window and the caps derived from it (#876).
Local admission and queueing: a cross-process admission gate for the local model server (#869, default off), stale `.tmp-*` tickets left by a killed creator are reaped (#887), queue priority classes with an interactive yield and an abort primitive (#882), and `LLM_ROUTER_LOCAL_QUEUE` wires the proxy backend into the queue (P13, #888).
Local-path sizing and context controls, each behind its own `LLM_ROUTER_*` switch (a default is stated only where the change itself states one): `LLM_ROUTER_DEADLINE_BY_SIZE` size-aware local deadline (off by default, #871); `LLM_ROUTER_EST_CALIBRATED` one calibrated token estimator (#873); SSE keepalive for long local steps (P17b, #875); batched observation masking (P4, #872) wired into the proxy local path (P5, #878); `LLM_ROUTER_PROXY_CAP_AFTER_REDUCE` cap on the reduced estimate (#881); `LLM_ROUTER_UNPIN` lifts the `prompt_over_cap` pin once when the reduced prompt fits (P7, #884); `LLM_ROUTER_OKF_BY_WINDOW` makes the OKF ceiling follow the local window (P21, #886); `LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN` makes a cap trip fall back one step without pinning the session (#889); `LLM_ROUTER_CAP_GUARD_RECONCILE` makes admission refuse what the overflow guard would refuse (off, #890).
Local summarisation and history layout, all flagged and off by default: `LLM_ROUTER_PROXY_MR` map-reduce summarizer in the proxy (P9, #891); `LLM_ROUTER_COLD_GUARD` cold-start rule (P22, #892); `LLM_ROUTER_SUMMARY_GATE` deterministic summary gate (P11, #893); `LLM_ROUTER_CHUNK_STORE` chunk-summary store (P10, #894, with gate-rejected hits evicted, #895); `LLM_ROUTER_COMPACT_BG` background compaction worker (P18, #896); `LLM_ROUTER_PROXY_LAYOUT` append-only history layout (P14, #900) and its key-stability shadow log `LLM_ROUTER_PROXY_LAYOUT_SHADOW` (#905).
R1 tool-call gate for local-served sub-agent steps, `LLM_ROUTER_PROXY_R1_GATE`, off by default (#930): a local reply is served only if it carries a well-formed tool call (a tool offered in the request, with every required input key); a text answer, an empty reply or an unparseable call is discarded before delivery and the step goes to Claude, and a parser's error text is never forwarded. With the flag off nothing changes. It carries no quality claim: see Known issues.
Release tooling: the outcome gate takes `--tolerance-alpha`, so a drop must exceed sampling noise (default 0 keeps the point comparison, #925); the proxy ledger records `upstream_error_type` on non-200 rows and logs 404s (#913); the Codex sub-agent handoff saves the result to a file and allows a one-line echo instead of a deny, with result files older than `LLM_ROUTER_AGENT_RESULTS_DAYS` (default 7) pruned (#868).
- `LLM_ROUTER_LOCAL_EXEMPT=coordinator_main` (PR-D, default unchanged): keeps only the main thread of a coordinator session on Claude (ledger detail `exempt_coordinator_main`) while its sub-agents can be served locally. Valid alone or with `subagent`; combined with `coordinator` it is invalid and the default applies.
- `LLM_ROUTER_PIN_SCOPE=turn` (default off, `--serve local-agent`): a failed local step pins only the rest of the current human turn (`cls_decide.turn_idx`, ledger `cls_turn_idx`; reason `pinned_turn`), and local is tried again at the next human turn. `pinned_turn` is hard against `LLM_ROUTER_CLASSIFIER_AUTHORITY=positive` within its turn, and lifts only at a strictly later human turn. The fallback count is cumulative per conversation and never reset. After `LLM_ROUTER_PIN_TURN_MAX_FALLBACKS` (default 3) fallbacks in one conversation the pin is sticky under its own reason `pinned_sticky`, which `LLM_ROUTER_CLASSIFIER_AUTHORITY=positive` never overrides. Only this fallback pin changes; kill switch, claude marker, exempt, media and prompt-cap pins are untouched. With the flag on, `local_mode` carries `pin_scope`, `pin_fallbacks` and `pin_cleared`; with it off, no field and no behaviour changes.
- Proxy ledger rows for local (Ollama) calls now carry `prefix_sha_sys`, `n_msgs`, `prefix_common_with_prev`, `appended_only` and `keep_alive`: hashes and counts only, to show whether consecutive turns are append-only so llama.cpp prefix reuse can work. Measurement only; no request is changed.

### Changed
Status line: the context bar uses Claude Code's window and `autoCompactWindow`, not a fixed 200k (#857).
`LLM_ROUTER_LOCAL_EXEMPT`, `LLM_ROUTER_PROXY_*` and the other new local-path switches above are registered in `env_registry` (#877); they are internal settings and do not change the 10 public settings.
- S1 daily row: the `other` population no longer counts sessions reclassified in `session_kind_overrides.json` (they moved to the D-407 `coordinated` line); a session that is both in the synthetic manifest and overridden stays synthetic, so release_clock and the streak are unchanged. Tests now cover that case, the streak, and the `kpi --daily` JSON/text fields (review SHOULDs on #850).

### Fixed
Proxy: an id-less local backend reply no longer returns a 500 (#898); an empty 200 reply without `done_reason` counts as a backend error in the agent loop (#863); a deep payload no longer raises `RecursionError` in the local-context guard, and an unparseable deep payload gets one ledger row (#899, #903); a negated edit request is not treated as an edit request in zero-Claude routing (#908); the OKF session block no longer repeats the verbatim tail beside map-reduce summaries (#911); warm-cache ledger rows no longer underestimate prefill time in the deadline (#880).
Hooks and status line: the usage-refresh hook reads Claude Code's snake_case `tool_name` (it read only `toolName` and exited silently; hook version 7 to 8, #924); the status-line refresher streams ledger and transcript reads instead of loading them (#865).
Security: four state files (judge summary, quality breaker, status-bar cache, usage seed) are created 0600, not 0644 (status-bar hook version 12 to 13; #927).
Packaging and install: the Codex capability probe survives coloured help (`CLICOLOR_FORCE=1`) instead of refusing Codex (#923); the PyPI publish step reads the version without `tomllib` on python3 below 3.11 (#922); `audit/` and `architecture/` are excluded from the sdist (#921).
- Numbers-provenance check: git-ignored paths are matched NUL-separated (`git check-ignore -z`), so an ignored file with a non-ASCII or newline name is skipped exactly; when git is missing or the root is not a git work tree, the scan says on stderr that ignored files are scanned too; the ignore test skips where git is not installed (review SHOULDs on #851).

### Internal
Tests and CI: scrubbed-env and hook-subprocess tests pass the tree under test on `PYTHONPATH` and pytest sets `pythonpath=src` (#916, #917, #918, #926, #929); midnight, leak-fixture and census tests hardened (#902, #906, #928); ZCE hook test pinned to this tree's `src` (#909); P8 and P14 test and script fixes (#897, #901, #904, #907); GitHub Actions bumps (#779, #782, #838, #910).
- Make three scrubbed-env subprocess tests hermetic: pass PYTHONPATH of the code under test (test_gh63_statusline_health_states, test_p09_auto_route_imports, test_kpi_hook_latency). Test-only.
- `tests/test_kpi_definitions.py`: the prompt-hook subprocess now gets `PYTHONPATH` set to the src of the `llm_router` the test process imported, so it no longer imports a stale editable install (5 local failures; CI was unaffected).
- The Pi CI smoke test (`tests/test_p212_pi_ci_smoke.py`) now puts the tree under test on the child process `PYTHONPATH`, so a local run no longer imports the venv's editable install from another checkout. Test-only change.

### Known issues and disclosures
Each figure gives its n, UTC window and source. UNMET: measured and missed. NOT INFORMATIVE: the data miss the check's own minimum, so no pass or fail is called. NOT MEASURED: not run. LIMITATION: shipped behaviour with a known gap. DEFERRED: planned for a later release. S1 is the share of actions served by a local or very cheap model; S3 is quota burn against a baseline. Live readings come from one installation; `coord/` and `cls/` files are private evidence under `~/.rsi/research/primary-plan/v16/`, `gates/v16/` files are private gate records under `~/.rsi/research/primary-plan/`, and `PLAN-v16.md` is the private plan log one level up.
- UNMET: no lane reached the 20% organic local share: 0 of 1,500 organic actions were offloaded over the 7 closed UTC days 2026-10-25..2026-10-31 (3 sessions, largest 96.3%; `coord/D335-reread-20261101.md`), and the MCP door read 0 of 0 (no data) and the proxy 0 of 44 in the 24 h to 2026-11-01T19:04Z (its local serving is off by default, D-207). Two pilots on 2026-10-27 did not establish a route either: the first (n=30 steps) was inconclusive (0 of 30 matched Claude's action, 20 of 30 judged acceptable, 8 of 30 acceptable and read-only), and the second (n=150) stopped on its tripwire with the local model never run (`coord/EXECPILOT*-RESULT-2026-10-27.md`).
- UNMET: measured offload per action (S1) is 0.0%: 0 of 1,500 organic actions in 2026-10-25..2026-10-31 (`coord/D335-reread-20261101.md`), 0 of 52 on 2026-11-01 and 0 of 36 on 2026-11-02 (`~/.llm-router/daily/2026-11-01.rev1.json`, `2026-11-02.json`), which is 20.0 and 60.0 points short of the 20% floor and 60% target. No preregistered offload target (T_P2) exists, and only 11 of 2,437 proxy-ledger rows (0.45%, 2026-10-25T17:00Z..2026-10-26, counted before the release-coordination agent session was reclassified as non-organic; that session supplied 81 of the 98 technical-op rows; `coord/PREREG-TECHOP.draft.md`) were technical-op steps small enough for a 32k local window, so 60% has no supported path here and moves to 16.2.
- NOT INFORMATIVE: the action census (GE1: technical-op share of organic actions) has too little data under the shipped `classify_bash`: 310 actions in 4 sessions, largest 91.0%, in 2026-10-27T21:07Z..2026-11-03T00:00Z, against 1,000 actions and a 60% session cap, so no share estimate is claimed (`coord/GE1-RC-census-20261103.md`; `gates/v16/GE1.json`).
- UNMET: the S1 streak of measured days is 6 (2026-11-01 to 2026-11-06) against the 7 required when organic and synthetic sessions count together (D-210), and 0 when only organic sessions count, because the last closed day, 2026-11-06, has no measured organic action. The combined streak broke on 2026-10-31, a day with 6 turns against the 10-turn floor (`llm_router.daily_offload.streak()` on a copy of `~/.llm-router/daily/`, rows 2026-10-31 to 2026-11-06, read 2026-11-07T09:30Z; `coord/RC6-STEP8-20261107-evidence/streak-rc6-20261107T0930Z.json`). The row for 2026-11-07 is written after that read.
- UNMET: the auto-route hook adds 1,820.1 ms at p95 (p50 259.1 ms) against its 750 ms bar for 16.0 (300 ms from 16.1), on n=43 organic rows in 2026-10-28T16:32Z..2026-11-01T19:04Z (`~/.llm-router/hook_latency.jsonl`; `coord/D335-reread-20261101.md`); n is under the 50-row minimum, so the figure is descriptive, and a control reading of the same window with the release-coordination agent session still counted as organic gave 1,175.9 ms at n=336, also over the bar. A read at the release candidate e1774654 found 0 organic rows in 2026-10-31T16:47Z..2026-11-07T16:47Z (the ledger keeps rows only from 2026-11-04T01:08Z, and all of those come from coordination-agent, synthetic or other non-organic sessions; `coord/RC7-STEP7-20261107.md`), so this is NOT MEASURED at the release candidate and the figure above is the dated read.
- UNMET: the proxy's tier decision takes 55.0 ms at p95 (p50 1.0 ms) against its 50 ms bar, over all n=322 organic decisions in 2026-10-27T08:27Z..2026-11-03T08:27Z (5 sessions, one holding 88.8%, over the 60% single-session cap the plan sets; `~/.llm-router/proxy_calls.jsonl`, every row timed by `tier_decision_s`, the reading `coord/metrics/measure.py` takes for the tag check; `coord/RC3-STEP7-20261103.md`, `coord/rv-RC3-STEP7-20261103.md`). It misses by one row: 17 decisions are over 50 ms where a nearest-rank p95 allows 16, and 12 of the 17 are over 100 ms. Counting only turn-first rows and timing only the four decision phases (classify, quota_read, stickiness, haiku_checks), as `PLAN-v16.md` P0.9-e words it, gives 36.6 ms on n=144 (the same rows timed by `tier_decision_s` give 76.0 ms); leaving out a second, later release-coordination agent session (not the one reclassified above; counted as organic in this read, and reclassified as non-organic on 2026-11-06, D-399) gives 47.0 ms on n=295. The installed `src/` and `hooks/` equalled the release's only for the last 1.6 of the 7 days, and those 1.6 days alone read 81.0 ms on n=48 (under the 50-row minimum, so descriptive). The earlier 7-day read to 2026-11-01T19:04Z was 21.0 ms on n=595, all from one session (`coord/D335-reread-20261101.md`). A read at the release candidate e1774654, in 2026-10-31T16:47Z..2026-11-07T16:47Z with the coordination-agent sessions excluded (D-335, D-399), gives 81.0 ms on n=76 from 4 sessions, one holding 88.2%, so it is not informative under the cap. It is a separate read from the n=48 one above, and all 76 rows predate the candidate's install (newest organic row 2026-11-02T07:43Z), so it times earlier builds (`coord/RC7-STEP7-20261107.md`).
- NOT INFORMATIVE: statusline p95 is 64.3 ms against its 100 ms bar, but on n=18 organic rows (200 needed) in 2026-10-28T16:32Z..2026-11-01T19:04Z (`~/.llm-router/hook_latency.jsonl`; `coord/D335-reread-20261101.md`), so no pass is claimed. A read at the release candidate e1774654 found 0 organic rows in 2026-10-31T16:47Z..2026-11-07T16:47Z (`coord/RC7-STEP7-20261107.md`), so this is NOT MEASURED at the release candidate and the figure above is the dated read.
- NOT MEASURED: session-end p95 (bar 300 ms), session-start p95 (bar 2,000 ms) and status-bar p95 (bar 300 ms) have no organic data at the release candidate e1774654: 0 organic rows for each in 2026-10-31T16:47Z..2026-11-07T16:47Z, because every hook row there is from a coordination-agent or synthetic session (390, 26 and 458 rows; `~/.llm-router/hook_latency.jsonl`; `coord/RC7-STEP7-20261107.md` section 3). The last organic read, 2026-10-27T08:27Z..2026-11-03T08:27Z, is NOT INFORMATIVE: session-end 62.3 ms on n=105 and status-bar 39.0 ms on n=128 each have one session over the 60% cap (69.5% and 67.2%), and session-start 152.2 ms is on n=19 (50 needed) (`coord/RC3-STEP7-20261103.md`), so no pass is claimed.
- NOT INFORMATIVE: judge coverage is 40 judged calls (MCP 23, agent 17; one further MCP call failed to grade; none at the proxy, SDK, gateway, hook or technical-ops doors) in the 14 days to 2026-11-07T09:22Z against a minimum of 50 per door, up from 16 in the owner-ratified snapshot of the 14 days to 2026-10-27 (frozen copy `coord/RC6-STEP8-20261107-evidence/judge_summary-20261107T0922Z.json`, generated 2026-11-07T09:22:38Z from `~/.llm-router/judge_summary.json`, which is rewritten on each read).
- NOT INFORMATIVE: quality parity with a frontier model (S2, bar a gap of at most 3 points, lower bound at least -10, on at least 60 pairs per lane) has no estimate: the Frontier shadow holds 3 verdict rows in all (2 judged acceptable, 1 the judge could not grade), 2 of them in the 14 days 2026-10-24..2026-11-07, against the 30 pairs GE4-a needs (D-40, D-46; `~/.llm-router/shadow_frontier/verdicts.jsonl`, counts read 2026-11-07; `gates/v16/GE4.json`), so 16.0 claims no quality-versus-frontier result.
- NOT INFORMATIVE: quota-burn reporting (S3) had start and end quota samples for 1 of 1 organic Claude Code session (30 turns; 95% interval 0.21 to 1.00) in 2026-10-27T21:07Z..2026-11-01T19:04Z, 4.91 of 7 planned days (`llm-router kpi --quota-burn`), against an assumed 10-session baseline from 2026-10-08T17:13Z; no quota reduction is claimed.
- NOT INFORMATIVE: the classifier screen (CLS-DEV) could not separate its known-better arm (qwen3-coder) from the known-worse one (nimble): cost difference +0.37 (qwen3-coder minus nimble, so positive means qwen3-coder cost more), 95% CI -0.063 to 0.766, on n=605 items (305 real, 300 synthetic), 2026-11-02 (`cls/dev/devrun-m3/cls_dev_result.json`; [protocol](https://github.com/ypollak2/llm-router/blob/v16.0.0/docs/prereg/PREREG-CLS-DEV.md)). The known-worse arm had to be run through a different backend after the frozen chat adapter was refused (HTTP 400) on all 605 items (`PLAN-v16.md` line 3920); no finalist was named, no classifier change ships in 16.0, no accuracy is claimed, and the synthetic items failed their resemblance check in all three generator versions.
- NOT INFORMATIVE: 0 of 324 organic routes evaluated in 2026-10-27T03:34Z..2026-11-03T03:34Z (5 sessions; `src/llm_router/ineligible_routes.py`, `coord/RC2-STEP7-20261103.md`) went to an ineligible model (absent, unhealthy or unlisted), but one session holds 88.3% of them against the 60% cap the check needs, so zero is not claimed as a pass; capability and privacy eligibility cannot be checked from the ledgers. A read at the release candidate e1774654 gives the same: 0 of 76 in 2026-10-31T16:46Z..2026-11-07T16:46Z, 4 sessions, one holding 88.2% (`coord/RC7-STEP7-20261107.md`).
- NOT INFORMATIVE: the Codex binding bar (D-358: binding of at least 90% on at least 30 delegated units over at least 3 days, a hand check of 20 bound answers under 10% blocked, and under 1 hold per 100 prompts) has no organic data: 0 organic prompts and 0 delegated units in 2026-11-03T20:22:29Z..2026-11-06T20:23:05Z (`scripts/release/bind_report.py`; `coord/bind-report-20261106T2023Z-D399.txt`). No organic prompt remains in that window once the release-coordination agent sessions are excluded, as the earlier one was (D-335, D-399). Read with those sessions counted, as description only (`coord/bind-report-20261106T2023Z.txt`): 23 of 23 delegated units bound (100%, Wilson 95% 85.7%..100%, under the 30-unit floor); Codex answered 'blocked' in 7 and failed in 5 of the 35 runs it was given, and those were not delivered; and the hold rate was 8.79 per 100 prompts (8 of 91 in the transcripts; the hold ledger reads 12 of 104 = 11.54; a hold is one prompt the router held, and it can block several tool calls), against the under-1 bar. Those holds were all before the read-only inspection fix in this release, live from 2026-11-06T20:12:29Z; no rate after the fix is measured.
- NOT MEASURED: the redo detector (it flags a human prompt that corrects, complains about, repeats or re-asks the previous turn): its 150-pair test did not run because the census found 0 flagged pairs among 115 eligible (240 organic pairs before exclusions) against a threshold of 19, in 2026-10-02T21:18Z..2026-11-01T21:18Z (`llm-router redo-sample --census --days 30`). The next census is about 2026-12-15, but transcripts are purged after about 30 days, so waiting is not expected to raise the flagged count (`coord/D333-P36C-census-20261101.md`).
- NOT MEASURED: quota-aware spreading (moving work to a provider that still has quota instead of leaving a subscription at its limit) was checked only by a synthetic replay (`tests/test_p23_quota_spread_replay.py`), not on real multi-provider data, because the quota history is Claude-only (D-323); real evidence is planned for a later 16.x release and the cross-provider proxy loop for 16.2.
- NOT MEASURED: traceability (tag criterion 7.1.6: each of the 111 rows is Met or has a LATER the owner ratified) was not recounted in full at the release candidate. The last full row table (`coord/TRACE-v16-rowtable-20261101.md`, 95 section-2 rows, 2026-11-01) labelled 15 rows MET-UNPROVEN and 1 SILENT; a later partial recount from table labels (not re-derived from raw files; `gates/v16/G3.json`, criterion 7.1.6) names R-CLS-8, R-EVL-1, R-EVL-7 and R-RTE-6 as not yet proven, and the 16 rows outside section 2 were not rechecked.
- LIMITATION: the release-eval fidelity window was changed after the first scored run blocked, so the change is post-hoc (PREREG-P32 AMEND2): release candidate 2f22034d returned BLOCK on 2026-11-02 with 20 of 60 sessions worse at 8192 tokens, and the window became 65536 after a sweep on the same 60 items. At 65536 the sessions fit whole (60 of 60 in full mode, `docs/measurements/p11b-synthetic-sonnet-r2.json`), so the check passes by construction and no longer tests trimming or the rolling summary until a longer-session set arrives in 16.1 ([PREREG-P32-AMEND2](https://github.com/ypollak2/llm-router/blob/v16.0.0/docs/prereg/PREREG-P32-AMEND2.md)).
- LIMITATION: once a session exceeds 0.8 x the model's window (about 52k tokens at 65536; the hook and agent doors use the local model's `num_ctx`, default 32768), the context pack's summary keeps the first user goal and recent turns but drops early tool output and early assistant statements. On the 60 synthetic sessions it kept the planted fact in 40 of 60 at windows 8192 and 16384, and in 60 of 60 at 32768 and 65536 only because the sessions then fit whole (model-free sweep, 2026-11-02, `docs/prereg/PREREG-P32-AMEND2.md`); these are counts of a planted token still present in the context, not answer quality, and the 60 of 60 says nothing about trimming.
- LIMITATION: the secret check (a prompt holding an API key, token or private key goes only to a loopback local model, or is refused) does not cover every door: the Claude Code proxy, the opt-in auto-route cloud classifiers, the hook's direct-execution Gemini and OpenAI calls and agent delegation sit outside it, as do other opt-in features (for example an ensemble classifier pointed at a cloud model) and text a host already sent to its own model, so the privacy claim names guarded doors only (README.md Features; `tests/test_w11_secret_door_matrix.py`).
- LIMITATION: the local text-job lane (commit messages, PR descriptions; `llm-router local-check`) rests on one model and a small bank: qwen3.6:35b-a3b-coding passed 118 of 140 items over two draws (95% lower bound 0.77; 2026-10-22 and 2026-10-24; `PLAN-v16.md` lines 3298 and 3328), but 48 of the 70 second-draw items had tuned the grader, and it passed 19 of the 22 never-scored ones (95% interval 0.67 to 0.95). The 22-item audit found 0 grader or item defects with no human labels (`src/llm_router/local_check/data/manifest.json`, 2026-10-24); gemma3:12b was inconclusive at 51 of 70 on 2026-10-24 (`PLAN-v16.md` line 3308).
- LIMITATION: `llm-router local-check` can spend money or plan quota on a cloud judge: it asks y/N consent first (or takes `--yes`) and by default stops at 120 requests (and at $3.00 on the metered API route; `src/llm_router/local_check/consent.py`, `budget.py`).
- LIMITATION: question-and-answer task types (query, research, generate, analyze and others) are not local-first: the router (MCP `llm()`, gateway, route server), the SDK and the zero-Claude hook try a local model after every other listed non-Claude provider and before Claude (first only when no non-Claude provider is listed), and the normal hook path drops local models for these turns and leaves them to the host Claude session (owner decisions D-14 and D-116; `src/llm_router/qa_policy.py`, `src/llm_router/hooks/chain_builder.py`). A local-first default per task type, only where a local model passes the quality bar (D-21), is planned for 16.1 (D-316); `code` tasks are unchanged.
- LIMITATION: lane A (the proxy serving the Haiku tier on classifier-confident continuation steps) is built but off, and no step was eligible on any of the 7 UTC days 2026-10-26..2026-11-01 in any session kind (`lane_a_reason` field of `~/.llm-router/proxy_calls.jsonl`; `coord/PREREG-LANEA.notes.md`), so it has no served steps to compare with a frontier model.
- LIMITATION: the upgrade-together test covers 3 hook hosts (Claude Code, Codex CLI, Gemini CLI) in one run (`tests/test_p212_upgrade_from_15_4_0.py`, 3 tests); Pi is wired through MCP config only, and Pi and every other host get no end-of-turn line until 16.1.
- LIMITATION: the 10-setting limit counts the provider-keys row once: `env_registry.PUBLIC` has 10 rows, one grouping 16 key names, and documents 26 distinct environment names (`src/llm_router/env_registry.py`, owner-accepted counting rule, 2026-10-27).
- LIMITATION: the local agent lanes (`llm_act`, `llm_local_task`'s `run_command`, the toolkit) run only allowlisted programs and refuse every other command, destructive ones included, with no approval step (some local tasks that ran before are now refused); an approval flow is planned for 16.1.
- LIMITATION: all 6 routing prefixes (`local:`, `claude:`, `opus:`, `sonnet:`, `haiku:`, `codex:`) are honoured at the hook but only 4 of 6 at the proxy, because `codex:` has no proxy backend and `local:` needs `--serve local-agent` (`tests/test_p27_proxy_prefixes.py`, `tests/test_p27_hook_prefixes.py`).
- LIMITATION: a ban or cap on a Claude model set with `llm-router prefer` does not change the proxy's tier decision; the fix is planned for 16.1.
- LIMITATION: the confirm token of the MCP install door (`llm_router_admin(action="install")`) and of `llm_setup(action="prefer")` is a consistency check, not human consent: the calling model receives the token and can return it itself (accepted risk, D-319).
- LIMITATION: `llm_apply` (an MCP tool that applies a batch of edits, runs a check, and can try a bounded local repair) is beta and off by default: unless `LLM_ROUTER_APPLY=on` it changes nothing and answers with that hint. Turned on, Claude did not choose it. In a preregistered A/B on 27 public-repo coding tasks (Claude Code 2.1.317 with Sonnet, 2026-11-07T15:05Z..16:16Z, one of three planned runs; it stopped there under a futility rule the owner added during the run, D-414), the two arms that offered it called it 0 times in 54 task-runs and edited with Claude Code's own Edit tool. In that single run they cost (harness-priced at list prices) 1.14x and 1.20x the Sonnet-alone arm ($3.34 and $3.50 against $2.92; the added tool schema and rule text are the likely cause, not measured). Claude Code can also defer MCP tools behind its ToolSearch tool: in an earlier run of the same bank it deferred `llm_apply` in all 39 of its transcripts still on disk (the other 33 of its 72 offering task-runs have no transcript left), Sonnet never loaded it, and that run's 72 offering task-runs made 0 calls; with `ENABLE_TOOL_SEARCH=false` (the only setting tried) the tool is loaded up front. Its local repair quality is therefore not measured, and its sandbox review moves to 16.1 (D-395, D-415; `coord/local-levers/l8/RESULT-L8-FIXED-R1-20261107.md`, `coord/local-levers/PREREG-L8-20261106.md`).
- NOT MEASURED: the North Star (more than 15% of coordinator calls served by a non-Claude or local model, judged on the one-sided 95% lower bound) has not been measured for 16.0.0, and no valid arm-1 read exists. The first NS-COORD arm-1 run was voided on 2026-11-14 (its marker guard found the conversations marked; `coord/RUNBOOK-to-tag.md`, W3 row; archived in `coord/ns-coord/arm1-run-void-20261114`). The restart (t0 2026-11-14T10:46:13Z) was killed at 2026-11-14T16:28:49Z by its preregistered guard G2: 2 of 2 local attempts fell back (reason `validation`, detail `empty response`; n=2, one-sided 95% lower bound 0.223607) and the proxy was rolled back automatically (ROLLBACK_DONE 2026-11-14T16:28:52Z; `coord/ns-coord/arm1-run/watcher.log`, `coord/ns-coord/arm1-run/state.json.status.json`). The last population read is 23 of 1,175 calls, 1.96%, on UTC day 2026-11-12 (2 sessions, largest 99.1%; `routing-window.py --days 1`, `coord/routing-24h.log` row 2026-11-12; `PLAN-v16.md` line stamped 2026-11-14T14:46:27Z). A new arm is planned for 16.0.1. The owner accepted shipping with this open (D-486, 2026-11-14T16:27:43Z).
- LIMITATION: local-model context is accepted as disclosed (owner, 2026-11-14T14:46:27Z, `PLAN-v16.md`; `coord/RUNBOOK-to-tag.md`, HOLD D-428 row). Evidence, all counts only and none of it a pass: (1) the B1 run of 2026-11-09 had a usable window of 8192 tokens and aborted the 16384 rung on swap growth (`local-levers/b1/runs/r1-20261109T111941Z/r1-report-20261109T112012Z.json`); that rung is superseded because the live window is 131072 since 2026-11-10 (`coord/PLAN-COND2-ADMIT90-20261114.md` section 0). (2) Shrinking the prompt did not fix quality: the reduce A/B (REDUCE-AB phase 1, 2026-11-09) had 12 misses in 60 steps against a pass line of 3 or fewer, and was abandoned (`coord/RESEARCH-COND2-INTERNAL-20261114.md`; `coord/HANDOFF-20261109.md`); the map-reduce summariser (MR, 2026-11-09 night) missed 43 of 60 (28.3% correct, one-sided 95% Clopper-Pearson lower bound 0.1893; `local-levers/mr/private/SUMMARY-MR.md`). (3) Local answer quality on synthetic 8-25k sessions (SYNTH-Q, n=132, FAIL) was 93 of 132 correct, 70.5% (lower bound 0.6324; `synthq/SUMMARY.md`); on LONG-Q (25-128k tokens, six cells of n=30) per-cell correct rates were 80.0% to 93.3% (`synth-live/longq/LONGQ-RESULT.json`, written 2026-11-12). The target then in force was 95%; the owner moved the bar to above 90% on 2026-11-14 (`coord/PLAN-COND2-ADMIT90-20261114.md`). `coord/`, `local-levers/`, `synthq/` and `synth-live/` are private evidence under `~/.rsi/research/primary-plan/v16/`.
- LIMITATION: per-call proxy forcing (P4) is parked and no saving is claimed. In bank 2 (n=27 tasks, 162 task-runs, 81 per arm, 2026-11-09T00:38:25.9Z to 05:04:08.8Z) the forced arm served 0.5306 of ledger rows locally (555 of 1,046) and cost 0.9198 of the unforced arm per task-run, but its mean passes were 17.33 against 20.00, below the required 19.0 (`local-levers/RESULT-P4-BANK2-20261109.md`; the transcript-cost reading of that cost ratio is 1.354). The owner accepted it as parked and disclosed (D-464, 2026-11-09).
- LIMITATION: the R1 tool-call gate (#930) ships behind `LLM_ROUTER_PROXY_R1_GATE`, off by default; it has not been run against its preregistered bar (next entry).
- NOT MEASURED: the admission-quality measurement (PREREG-ADMIT90, `coord/PREREG-ADMIT90-20261114.md`) has not run, so there is no quality claim for local-served steps, with or without the R1 gate. The bar it sets is above 90% correct on served steps with a one-sided lower bound of at least 0.85 and n of at least 150 (`coord/PLAN-COND2-ADMIT90-20261114.md` section 2).
- DEFERRED to 16.1, among others (full list: `PLAN-v16.md` section 7.2): the three-OS CI matrix, the 300 ms agent-route latency bar, the 40% quota-reduction and 70% cross-user targets (S3, S7), hosted-runner LLM evals, an OS-level write sandbox (16.0 ships a static tripwire), end-of-turn lines for other hosts, deleting internal env names, the destructive-command approval flow, the proxy fix for model bans and caps, a longer-session fidelity set, and the verifier going live (shadow-only and opt-in in 16.0: `docs/VERIFIER.md`). To 16.2: the 60% offload target and the 70%-exact, 5%-under-routing classifier bar; to 16.3: the rest of dynamic adaptation beyond the 16.0 safety floor.

## [15.4.0] - 2026-09-29

No savings claim is made for this release. The headline item — an opt-in,
off-by-default proxy that routes some Claude Code steps to a local model —
has not shown a net saving in any measurement run so far: an untrimmed
proxied run was 17.7x slower than baseline (938s vs 53s, n=6 golden tasks,
`docs/spikes/per-call-proxy-2026-09-28.md`), and the metric built to estimate
its avoided cost (`net_avoided_usd`) was found to overstate a paired-session's
real saving by ~1.9x at n=6 and is now documented as an upper bound, not a
realized figure (#201). See `docs/proxy.md` and the "Known issues" section
below before enabling it.

### Added

- **Opt-in per-call proxy** (`llm-router proxy`, `llm_router.proxy`, #197-#201).
  **Off by default, experimental.** Point one
  Claude Code session at it with `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`.
  Every call passes through to Anthropic unchanged, except "continuation" steps
  (the newest turn is only tool results). The router's own policy may send those
  to a local tool-capable model, which answers in Anthropic format. A served
  reply that fails validation, errors, or misses the per-step latency budget
  falls back to Anthropic, and the fallback is recorded. It is off unless you
  start it; nothing installs it or changes settings. Per-call rows go to
  `proxy_calls.jsonl` (`llm-router proxy stats`), and `northstar` counts served
  turns as routed `claude_main_call` units. Serving defaults follow the
  local-speed spike: a condensed request with the step's tool subset, a
  `num_predict` cap of 200/700, `keep_alive -1` plus a warm-up call, and an
  8 s first-token hedge. See `docs/proxy.md`.

- **NS1 — the North Star metric** (#180): `llm-router northstar [--session ID] [--days N] [--json]`
  reports, per Claude Code session, the share of (user prompts + every LLM
  call — main, folded sub-agent, and tool-driven) that was routed to a
  non-Claude model AND used as-is. Reports the distribution across sessions
  (median, p25, max, n), never a mean alone; sessions under 50 units say "too
  few to tell". `llm_router.northstar.units()`/`.report()` are the library
  entry points. The Stop line gains one item: `north star NN% (n=NNN)`.
  See `llm_router/northstar.py`'s module docstring for the exact counting
  rules and the outcome signals (draft hook verdict, transcript attribution,
  MCP tool-result reuse, zero-Claude re-ask). The target is documented as
  >=50% (aim 70%) of prompts + LLM calls per session, baseline ~0% (n=300
  sessions, 30 days) (#183).

- **North Star now reads the `llm_edit` (#181) and Codex sub-agent (#184)
  lever ledgers** (#188). Two new unit kinds: `routed_edit` (lever `llm_edit`, one
  per `edit_outcomes.jsonl` row — `used` when applied and, per
  `scripts/northstar/edit_survival.py`'s git-history check, survived; `redo`
  when a later commit touched the file; `discarded` when the edit was never
  applied; `unknown` when survival can't be resolved) and `agent_route_codex`
  (lever `agent_route_codex`, one per `north_star_units.jsonl` row recording
  an actual Codex invocation — `used` on `delegated`, `discarded` on
  `codex_failed`; decision-only rows like `unsuitable` produce no unit). A
  ledger row for an `llm_edit` call is joined to that call's transcript
  `routed_mcp` unit (nearest preceding `mcp__llm_router__llm_edit` tool_use
  within 300s) and replaces it, so the same call is never counted twice.
  `_load_agent_calls()` now also reads the 30-day `agent_calls_ledger.jsonl`
  (unioned with the 50-cap `agent_calls.json`) when folding sub-agent
  sessions into their parent, closing the gap where the 50-cap file had
  already evicted the spawn the join needed.

- **`LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit`** (#190, #194): a narrower zero-Claude
  mode for prompts that name a concrete change to existing files, instead of
  the full fail-closed contract. `zero_claude_edit.py` classifies the prompt
  conservatively (imperative edit verb + 1-3 named, real-looking source
  files; rejects questions and repo-wide requests), resolves targets against
  the repo root (refusing anything outside it), skips dirty targets or an
  open quality-breaker class, then generates and validates the edit with the
  local model and applies it all-or-nothing. #194 extends target resolution
  to prompts that name no literal path: a backticked/`snake_case`/`camelCase`
  identifier maps to the tracked file that defines it, and a bare file name
  maps to the one tracked file with that basename — falling through (reason
  logged) whenever an identifier is defined in more than one file, more than
  3 files are implicated, or the prompt looks like a question. Not enabled by
  default; `LLM_ROUTER_ZERO_CLAUDE_SCOPE` is unset until an operator opts in.

- **NS4 — quality breaker**: `src/llm_router/quality_breaker.py` auto-unroutes
  a `(lever, task_type[, model])` class whose routed answers keep failing.
  Closed → open → half-open per class, computed from `northstar.units()`:
  opens when `(redo + discarded) / (used + redo + discarded) >= 0.5` with at
  least 20 classified units in a rolling window of 50 (unknown units excluded
  from the rate on both sides); a half-open probe of 5 routed units closes it
  again or reopens it. Wired into `auto-route.py`'s direct/draft chain,
  `agent-route.py`'s Codex sub-agent delegation, and the MCP `llm()`/
  `llm_act()` tools; every skip logs its reason. `llm-router status` shows an
  open-classes panel (quiet when nothing is open); `llm-router northstar
  --breaker-dry-run` reports what would be open, read-only. On the
  maintainer's own 30-day history the `drafts` lever would already be open
  (100% failure rate, n=35); every `mcp_llm` split stayed closed only because
  none yet clears the 20-unit minimum (#186).

- Widened the Codex sub-agent lever (#184) to accept `task_type=query`, not
  only `research`/`analyze`/`code` — real spawns on the maintainer's machine
  were classified `query` and always fell through as "unsuitable" (n=5
  observed spawns, 3 `query` / 2 `analyze`) (#192).

- `agent-route.py`'s Codex sub-agent lever can now resolve the MCP-path
  `llm_edit` ledger's session id via the shared session store instead of a
  bare environment read that the long-lived MCP server process does not
  reliably have, fixing a case where `edit_outcomes.jsonl` rows carried
  `session_id: ""` and were invisible to `northstar` (#191).

- Added `ollama/qwen3.5:latest` as a local route for `query`-task requests
  (#167).

### Changed — savings baseline is Opus 5.5

- **Every savings figure is now measured against `claude-opus-5-5` ($4/$20),
  not `claude-opus-5` ($5/$25).** `pricing.SAVINGS_BASELINE_MODEL`, the `opus`
  and `claude-opus` aliases (which the statusline and session-end hook price
  the host with), `cost.LATEST_OPUS_MODEL`, and the fail-open fallback rates in
  `digest.py`, `session-start.py` and `session-end.py` all move together. The
  counterfactual cost of a given token volume drops by exactly 20%. Only
  figures recomputed from tokens at read time move; savings already written to
  `savings_stats` and the per-platform tables keep the rate they were written
  with. `LLM_ROUTER_SAVINGS_BASELINE` still overrides it.
  Measured on the maintainer's own `~/.llm-router/usage.db`, opened read-only,
  lifetime window (earliest row 2026-07-08, latest 2026-09-29T06:16Z), 47,995
  routed rows: the all-time estimate went from $22.12 to $21.47 (n=949
  production rows carrying a saving, identical before and after; $0.00
  verified, n=0). The whole change comes from the 629-row legacy `usage` table,
  the one source that is recomputed from tokens.

### Changed — pricing catalogue and savings display

- **Added pricing for `claude-opus-5-5` ($4/$20, cache read $0.20), `claude-fable-5-1`
  ($10/$50, cache read $0.25) and `claude-sonnet-5-5` ($2/$10, standard
  ratios).** Before this, on the maintainer's real usage, the absence of an
  Opus 5.5 entry sent $308.93 of real Claude Code spend to a fallback rate.
  `"<id>[1m]"` now resolves to its base model's rate for Claude 4.6+ ids only
  (no long-context surcharge, per the pricing page); earlier `[1m]` ids stay
  unknown rather than being under-priced. Rates checked against
  `platform.claude.com/docs/en/about-claude/pricing.md` on 2026-09-28 (#202).

- **Savings displays now show one estimate, never a verified/unverified
  split.** `llm-router status`, `savings-report`, `gain`, the statusline and
  the Stop line all changed from e.g. `verified $0.00 (n=0) · unverified
  estimate $0.43 (n=62)` to `est. saved $0.43 (n=62) vs claude-opus-5-5` (or
  `~$0.43 est.` on space-constrained surfaces) — on a machine where no routed
  answer has ever replaced a Claude turn, `realized_usd` is always $0.00, and
  the old wording read as "nothing was saved" next to a nonzero estimate. The
  underlying verified/unverified split is unchanged and still available to
  `doctor`/`explain-dashboard`; it no longer reaches the default view (#182).

### Fixed — fast-mode pricing (#205)

- **Fast-mode ids were priced at their base model's rate, half the real one.**
  `claude-opus-5-fast` and `claude-opus-4-8-fast` were aliases to
  `claude-opus-5` / `claude-opus-4-8` ($5/$25). They are now priced at the
  published fast rate, $10/$50, and `claude-opus-5-5-fast` is added at $8/$40
  (it previously did not resolve at all). The pricing page says prompt-caching
  multipliers apply on top of fast-mode rates, so each keeps its base model's
  cache ratios: Opus 5.5 fast reads cache at 0.05x ($0.40), the other two at
  0.1x ($1.00); 5-minute writes are 1.25x and 1-hour writes 2x. Rates checked
  against the raw `platform.claude.com/docs/en/about-claude/pricing.md` on
  2026-09-29.

### Fixed — agent-route headless sessions (#203)

- **`hooks/agent-route.py` silently routed a headless benchmark run to `codex
  exec`.** A `claude -p "..." --model sonnet --output-format json` run hit the
  default `_allow_routed_spawn()` path on its first Task-tool call and turned a
  ~40s task into 832s, contaminating the benchmark. Verified with a real `-p`
  run (via a throwaway diagnostic hook) that the PreToolUse[Agent] payload
  itself carries no entrypoint field (`cwd`, `hook_event_name`,
  `permission_mode`, `prompt_id`, `session_id`, `tool_input`, `tool_name`,
  `tool_use_id`, `transcript_path`); `CLAUDE_CODE_ENTRYPOINT` is the real
  signal — `"sdk-cli"`/`"sdk-py"` for a headless (`-p`/SDK) session versus
  `"cli"` for an interactive one. The hook now exits 0 with no decision (and
  no session-state writes) whenever the entrypoint is headless, logging
  `skipped_headless:entrypoint=<value>` to the agent-calls ledger. An unset
  entrypoint and `"claude-desktop"` (the GUI app — a person is driving it) are
  still treated as interactive. `LLM_ROUTER_AGENT_ROUTE_HEADLESS=on` opts back
  into routing for anyone who wants it in a script/SDK session.

### Fixed — proxy cost accounting (#200, #201)

- **The proxy ledger's cost estimate did not match Claude Code's own
  `total_cost_usd` for the same session.** Reconciling six real sessions
  (`--output-format json`) against the ledger surfaced three bugs:
  1. `normalize_usage` was not idempotent, and every row's stored `usage` is
     already its output — `_record_usage` in `server.py` normalizes once
     before `write_row`. Every reader that read a row back (`anthropic_cost`,
     `stats`'s token totals) normalized it a *second* time, and the second
     pass looked for Anthropic's raw nested `cache_creation` dict, which no
     longer existed. It silently zeroed `cache_creation_1h` and folded the
     real 1h-tier tokens into `cache_creation_5m` at the cheaper rate — on
     every row ever read from `proxy_calls.jsonl`, not a corner case: that is
     the only way any caller ever sees a row. `normalize_usage` now detects an
     already-flat dict and returns it unchanged.
  2. `pricing.py` charged Sonnet 5 the standard rate ($3/$15) from
     2026-09-01 on, expecting a scheduled reversion off its $2/$10
     introductory price. Anthropic cancelled that reversion — the pricing
     page states outright that $2/$10 "is now the standard price" and the
     increase "will not occur." On 2026-09-28 this overpriced every Sonnet 5
     call by exactly 1.5x; three sessions matched $2/$10 to the cent and none
     matched $3/$15. `claude-sonnet-5` is now priced flat, like every other
     model, and `PRICES_AS_OF` reflects the re-check.
  3. `est_avoided_usd` priced every served step at the median cost of a whole
     *forwarded* continuation call in its session — repricing the entire
     cached prefix once per served step, as if Anthropic would redo that work
     from scratch each time. Renamed to `net_avoided_usd`: each served step is
     now priced as the cache-read of the prefix established by the last real
     Anthropic call before it, plus its own reply length, minus the extra
     cache-write the next real Anthropic call paid to re-establish its cache
     across the served gap. It **may be negative** — a served step is not a
     saving if that gap cost more to close than the step would have cost.
     See `docs/proxy.md` and the `ledger.py` module docstring ("NET AVOIDED").

  Measured on a routing-off (pure pass-through) session, 8 real Anthropic
  calls, 2026-09-28: ledger `est_cost_usd` matched `total_cost_usd` exactly
  ($0.2176, ratio 1.000) after all three fixes, versus 1.156x before fix 2 and
  0.771x with only fix 2 applied (fix 1 alone masked fix 3's undercount by
  coincidence). On three routing-on sessions (1-4 served steps each) the
  residual gap ranged 5%-27% and grew with served-step count — attributed to
  Claude Code's own cost tracker billing a served step's local (Ollama)
  token counts as Sonnet-5 usage, which this PR does not change (out of
  scope: it is Claude Code's accounting of a reply it received, not the
  ledger's).

### Fixed — proxy

- **The per-call proxy (#197) had no defence against a served step that never
  makes progress (#198).** A live trial (2026-09-28, 3 real `claude -p` tasks) showed
  one session with a run of 44 CONSECUTIVE served replies from
  `ollama/qwen3-coder:30b`, each re-issuing the same `Read` of the same file:
  the task still finished, but the loop took 195s and inflated the routed
  share (63/81 = 77.8%) and "est. avoided $2.93" with calls that only existed
  because of the loop. Two healthy sessions in the same trial never exceeded a
  served run of 6. `llm_router.proxy.loop_guard` now tracks, per Claude Code
  session, in-memory only: a served tool call that exactly repeats one of the
  session's last `LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW` (default 3) served tool
  calls, or a session that has served `LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE`
  (default 8) steps in a row — set above the largest healthy run (6) with
  margin, well below the runaway one (44) — is handed to Anthropic instead,
  with `reason: "loop_guard"` in the ledger row and a `detail` naming which
  check fired. Any non-served step for a session (a real Anthropic call, or a
  policy decision to keep the step on Anthropic) clears that session's streak.
  `llm-router proxy stats` now reports calls per session and a
  `routed_share_excl_repeats` that drops loop-guard-flagged calls from both
  sides of the share; `est_avoided_usd` never counted them (it is computed
  only over rows that were actually served) and now excludes them
  defensively too.

- **A proxied Claude Code session cost 3.5x an identical no-proxy session on
  the Anthropic side (#199).** Measured 2026-09-28, same 3 fixture tasks: $2.44
  proxied vs $0.69 baseline, all tests passing both ways. Root cause is in
  Claude Code, not this proxy: it disables Tool Search (dynamic MCP/skill tool
  loading) whenever `ANTHROPIC_BASE_URL` is not a first-party Anthropic host,
  because it cannot tell that `llm_router.proxy.server.forward()` passes every
  `/v1/messages` body through byte-for-byte. With Tool Search off, every
  request inlines every deferred tool's full schema instead of the few a step
  needs (`claude --debug api` showed `Dynamic tool loading: 0/134 deferred
  tools included` become `134/134`), which is what enlarges the cacheable
  prefix and, on a cache miss, gets billed at the 1-hour cache-write rate (2x
  input). `ENABLE_TOOL_SEARCH=true` restores first-party behaviour and is safe
  with this proxy specifically, because it never inspects or rewrites the
  tools array, so the resulting `tool_reference` blocks reach Anthropic
  unchanged. `llm-router proxy`'s startup banner, `--help` (`USAGE`), and the
  module docstring now all print the enable command via one shared
  `server.enable_hint()`, which always includes `ENABLE_TOOL_SEARCH=true`
  next to `ANTHROPIC_BASE_URL`. See `docs/proxy.md` ("Cost parity").

### Fixed — routing

- **The read-only draft loop could burn the entire hook budget on one stuck
  model, leaving nothing for any fallback.** `~/.llm-router/direct_samples.jsonl`
  showed 20/20 recorded DIRECT failures at ~54-55s (the hook's own budget) —
  `auto-route-debug.log` traced the mechanism: `execute_agent`'s first ollama
  model (reliably the heaviest one) was handed the ENTIRE remaining deadline,
  and `execute_chain`'s text-chain fallback that runs after it was ALSO given
  the full, un-reserved hook deadline. When the first model stalled (cold
  load, drift), both the rest of the agent-loop chain and the text-chain
  fallback were skipped with "out of hook budget before the call" without a
  single request being sent. Both call sites now reserve time for what runs
  after them, mirroring the reserve `execute_chain` already applied to its own
  chain. `execute_agent` also now logs a per-model reason (timeout vs.
  drifted vs. quality-gate-rejected) instead of one blanket "nothing".

### Fixed — enforce hook

- **The PreToolUse enforce hook held `Bash` behind a routing call for
  one-word follow-ups like "merge them."** `detect_execution` requires an
  execution verb AND a repo object, but "merge" appears in both lists, so
  "merge them" matched `verb='merge' obj='merge'` and was held. It also
  matched verbs written inside double quotes, where the user is naming a
  phrase, not giving an instruction. The object must now be a different span
  from the verb, and double-quoted spans are ignored (single quotes and
  backticks unchanged). Evidence: `~/.llm-router/enforcement.log`
  2026-09-25, two real holds this pattern caused (#165).

### Fixed — classifier

- **The direct-executor quality gate rejected every short correct answer.**
  `quality_ok()` required at least 10 stripped characters; on the 2026-09-28
  golden probe (n=150), all 15 "reply with just the number/word" queries were
  rejected on all 6 attempts each (~30s per case) and fell through unrouted,
  even though answers like "42" or "Indices." were correct. The floor is now
  "real content survives": strip `<think>` blocks and markup tags, then
  require at least one alphanumeric character — `"<think></think>"` and
  similar tag-only replies, which the old length floor let through, now fail
  correctly too (#193).

- **`COORDINATE` classification (session-state/history/ambient prompts) only
  fired on literal "spawn agents" phrasing**, so a coordination-shaped prompt
  ("what's the status?", "do the same thing for the rest of the files",
  "merge X and redeploy") was misclassified and had its tools held pending a
  throwaway routing call. Broadened `_is_coordination_task` to also catch
  session/history references, continuation anaphora, cross-system
  orchestration, multi-step operational chains, and short ambient
  status/continuation checks (<=6 words); `enforce-route.py` now exempts
  `coordinate` from PreToolUse holding, matching the existing `introspect`
  exemption. On the 150-case golden set: coordinate recall 0/12 → 10/12
  (83.3%), constructed-only recall 0/10 → 10/10, 0 false positives across all
  138 non-coordinate cases (#196).

- **The `COORDINATE` fast path still missed several status/continuation
  shapes**: "status on X", "X's status", "is/did X done/land", "how's X
  going", a mid-sentence "keep going", and a pre-existing leak where "what's
  the current state of X" matched the wrong alternative. Widened the pattern
  set and added determiner guards so noun uses of verbs don't count as tasks.
  On a real-traffic split (449 prompts of <=12 words, seed 20260929): holdout
  (n=225) recall unchanged at 87/109, precision 87/108 → 87/107; tune (n=224)
  recall 88/110 → 107/110 (the tune gain did not transfer to holdout — one
  holdout item changed). A broader ~30-verb list was tried and reverted: it
  cost 3 holdout recall points (#206).

### Fixed — savings ledger

- **A delegation's saved amount was written twice** — once directly by
  `agentic.telemetry.record_delegation_savings`, and again when
  `import_routing_quality_ledger` (#171) imported the same event's
  `routing_quality.jsonl` parent row. On 2026-09-28, 12 real delegations were
  double-counted this way, inflating the lifetime estimate by $13.60.
  `import_routing_quality_ledger` now excludes `route_kind` in `{"delegate",
  "bounded_operational", "delegate_substep"}`, since those already have a
  savings_stats row from the direct write. Verified against the real
  `~/.llm-router/usage.db`: all-time estimate $22.19 (n=953) before, $15.39
  (n=953) after repair — a $6.80 drop, the routing_quality-side half of the
  $13.60 double count. Adds `scripts/repair_savings_double_count.py`, an
  idempotent, **dry-run-by-default** repair for the 12 rows already written
  before the fix: it zeroes the duplicate side's estimate while preserving
  the original value in a new `corrected_from`/`correction_reason` column
  pair, rather than deleting anything (#207).

- `routing_quality.jsonl` (the ledger `llm(task=...)` and other MCP-routed
  calls write to) is now imported into `savings_stats` via
  `cost.import_routing_quality_ledger()`, so MCP-routed savings show up in
  `status`/`savings-report` at all — previously nothing drained this ledger
  into the tables those commands read. Idempotent (`route_id`-keyed, backed
  by a partial UNIQUE index), read-only against the source ledger, and rows
  are always written `mode=NULL` (the MCP tool has no "was this answer used"
  signal, so they can never read as verified). Dry run against the real
  `~/.llm-router` store: of 24,214 rows, 27 would import, summing $0.4121
  (#171).

- **`llm-router status`, `savings-report`, `gain` and the statusline
  disagreed at the same instant on the same machine** — different row
  counts, different baseline models, and `gain` reported a fabricated $0.00
  Opus baseline for every free/local call (multiplying an actual $0 cost by a
  multiplier instead of pricing the token volume). All four now call one
  `dashboard_data.summary(period)` for their headline figure and baseline
  policy; `gain` now renders `n/a` rather than a fabricated $0.00 when there
  is nothing to price (#173).

- The Stop hook's condensed line and Codex's Stop reporter printed an
  unlabelled, verified-only `"lifetime $X"` figure that could read as "$0.00"
  on a machine `llm-router status` showed real routing activity on for the
  same rows. Both now call the one canonical `dashboard_data.summary()` and
  label each half explicitly (superseded by the one-estimate display, #182,
  above) (#178).

- `is_synthetic_session()`'s "any character outside `[0-9a-f-]` means a
  human-typed fixture id" rule (#176) was also flagging the router's own
  production writers: "gateway" (`route_server.py`, 262 rows measured) and
  "sdk" (`sdk.py`, 38 rows measured) are real, non-hex session ids this
  code itself emits. Added a small, closed allowlist of ids the router's own
  writers use, checked before the fixture-word rule so it can't be caught by
  a future tightening underneath it (#179).

- `quota_snapshots.claude_*_pct` was stuck at 0.0 on every row (3,402 rows
  measured) — the writer read the pressure dict with `usage.json`'s key
  names (`session_pct`/`weekly_pct`) but was handed `_get_pressure()`'s
  dict, which uses different keys and 0.0-1.0 fractions; the lookup always
  missed and its `, 0.0` fallback fabricated a "0% pressure" reading every
  time. Now reads the correct keys with no default, so a genuinely missing
  reading stores NULL (#175).

- The "critical pressure → Opus override" in `auto-route.py`'s `main()` was
  dead for the same reason: it read `session_pct`/`weekly_pct` against a
  dict keyed `session`/`weekly` as fractions, so the lookup always missed
  and the override never fired regardless of real pressure. Extracted into
  `_critical_pressure_reading()`, which reads the correct keys and treats a
  missing reading as unknown rather than a fail-open "safe" (#177).

- `claude_usage` rows (37,894 measured) all carried `input_tokens=0` /
  `output_tokens=0` and no `session_id`, because the writer never threaded
  the real token split or a session id through even though the columns have
  existed since v9.2.2; fixed by threading them from the two call sites that
  already have the values in scope, and added a `session_id` column to
  `claude_usage`/`codex_usage`/`gemini_usage`. Separately, `savings_stats`
  queries in `dashboard_data.py` had no test-traffic filter while every
  other usage table did — 17% of `savings_stats` (1,559 of 9,057 rows
  measured) was test traffic; the same filter now applies there too (#176).

### Fixed — pricing

- Resolved 1,374 `claude_usage` rows priced as "unknown": tag-less
  `ollama/*` names (e.g. `ollama/llama3.2`, no `:tag`) lost their `ollama/`
  prefix during normalization before the ollama fallback check ran, and bare
  `claude-opus` (no version suffix) was missing from the alias table.
  Verified: unresolved rows dropped from 1,374 to 57, all of which are a
  synthetic `openai/some-model` test fixture (#174).

### Fixed — local routing

- **`qwen3.5:latest`, a hybrid-reasoning model, burned entire calls on
  `<think>` output via the LiteLLM `ollama/` provider path** (the one the MCP
  `llm()` tool and `providers.call_llm`/`call_llm_stream_events` use), unlike
  every direct-HTTP Ollama caller in the repo, which already hard-codes
  `"think": false`. Evidence 2026-09-27: a `routing_quality.jsonl` row that
  timed out at 78s, and a separate 120s `litellm.Timeout`. Both LiteLLM call
  paths now send `think: false` by default (`LLM_ROUTER_OLLAMA_THINK=1`
  restores Ollama's own default); the streaming path also gained an
  empty-content check so a reply that streamed only reasoning and no content
  now raises instead of completing as a silent success (#172).

### Fixed — Codex integration

- **A Codex CLI status banner ("Reading additional input from stdin...")
  was reported as a successful answer** when no real `item.completed` text
  ever arrived — `run_codex()` always returned success for any non-JSON
  stdout/stderr fallback. A live regression on 2026-09-26 had a routed
  `llm(task="analyze")` call return the banner as a "successful" 205-token,
  $0 completion. Fixed: success now requires real answer text to have
  arrived; without it, the exit code is forced to a failure so the router's
  existing fallback path takes over (#169).

- Guarded the Codex installer (`_install_codex_files`) against writing to a
  real, non-test `~/.codex` when it is exercised without an isolated
  `Path.home()` — it was resolving the home directory with a bare
  `pathlib.Path.home()`, unlike its already-guarded uninstall sibling (#170).

### Docs

- README redesign: restructured to lead with the quota problem and the
  routing story; a follow-up restored 5 disclosures/pointers the redesign
  had dropped (#166, #168).

### Known issues

`llm-router doctor` on the owner's machine currently reports three red
measurements (read 2026-09-29, against the installed v15.3.0 — this
worktree's 15.4.0 changes are not installed/running, so these figures
predate and are unrelated to this release):

- `unterminated_invocations=1031` — a routing branch that skips without
  logging a reason.
- `interception_gaps=1132` — tool output that passed the allowlist and was
  still not compressed.
- `draft_acceptance=0` — 0 drafts relayed; the routing rate still counts
  drafts as work routed even though none have been used.

None of this release's commits (v15.3.0..15.4.0) touch the draft hook,
`hooks/context-capture.py`'s interception path, or the invocation-logging
call sites `doctor` flags — see the `### Fixed` sections above for what did
change. These three are pre-existing and open; no PR in this release claims
to fix them.

## [15.3.0] - 2026-09-25

Local routing, rebuilt around what was measured. Before this release, on the
maintainer's machine, 0 of 1,191 local drafts were ever used and verified
savings were $0.00. No savings claim is made for this release either; use
`llm-router routing-health` to see your own numbers.

### Changed — routing

- **Smart enforcement now covers prompts that refer to your repo or session.**
  They were exempt ("context-dependent"): 51% of 142 real prompts in a 10-day
  replay, so routing almost never happened in a repo. They now get a directive
  to route WITH the relevant excerpts (`llm(..., context=...)`; OKF, semantic
  and session context are added automatically). **Behaviour change:** in smart
  mode Claude's Q&A tools are held on these prompts until it routes.
  `LLM_ROUTER_ENFORCE_CONTEXT=off` restores the exemption.
- **Hook drafts only for questions** (query, research). Claude used 0 of 1,191
  drafts; for code/instruction turns a draft only added latency.
  `LLM_ROUTER_DRAFT_TASKS=all` restores drafting everything.
- Background-task notifications and sub-agent reports are no longer drafted
  (25.9% and 18% of prompts in real traffic; neither is a user question).
- Repo questions tagged "research" draft locally unless the prompt carries a
  web signal (they used to get an empty local chain).

### Fixed — quality of local answers

- **Discarded drafts no longer become "what Claude said".** A draft offered
  as a hint was written to the session history as an assistant turn and fed to
  later drafts as fact. Only a turn the hook actually answered is recorded.
- **`llm(...)` retrieves from the caller's project.** Claude Code starts the
  MCP server in `$HOME` and sends no MCP roots, so retrieval used the wrong
  project; the server now finds its session's working directory via
  `CLAUDE_CODE_SESSION_ID`.
- Retrieval: private (`_name`) and call-written (`name()`) identifiers are
  search seeds; a function the question names comes with its code and up to 3
  call sites (with the lines above each); module-level constants are indexed
  and shown whole; the search uses the question, not the prepended knowledge.
  Held-out check (14 fresh questions, local qwen3-coder:30b, independent
  judge): 12/14 correct with context vs 0/14 without; both misses are fixed in
  this release but not yet re-measured on a new set.
- The draft notice says what the draft saw (files read, or a session summary)
  instead of "produced WITHOUT access", which told Claude to discard it.
- The local agent's `run_command` accepts `&&`, `;`, `||`, pipes and `2>&1`
  (still without a shell; every segment is allowlist-checked first) and
  read-only `git branch`.

### Added

- `llm-router routing-health [--days N] [--json]`: per day, over prompts you
  typed — reach, context (draft read files), USED by Claude, draft latency,
  and the `llm(...)` calls Claude routed (local / Claude / other), each with n.

### Also in this release

- #143 local-first: the background benchmark fetch is opt-in.
- #145 / #153 answer-quality judge: queue-and-grade-later with an independent
  judge model; correctness-dominant weighted scoring.
- #146 release gates for classifier and answer-quality regressions.
- #147 / #148 `summary` and `status` show verified vs unverified savings.
- #155 security: tag-shaped text in intercepted tool output is neutralised.
- #157 `run_command` drains each pipeline stage before feeding the next.

### Changed — installer

- **`llm-router install` now auto-enables Claude subscription mode when it
  detects a logged-in Claude seat.** Previously a Pro/Max subscriber had to
  hand-set `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` (`src/llm_router/discover.py`
  `get_available_providers()`) even after the installer's own seat probe
  (`seats.py`) had already found the seat — flat-rate subscribers were not
  getting zero-workflow-change routing by default. The installer now writes
  `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` to `~/.llm-router/.env` (the same file
  `llm-router setup`/`onboard` manage) the first time it sees a Claude seat,
  and prints one line saying so and how to turn it off
  (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=false`). **Behaviour change for existing
  subscribers:** re-running `llm-router install` (e.g. `--force`, an upgrade,
  or reinstalling for a second host) now enables subscription mode
  automatically if you never set the variable yourself — an explicit `true` or
  `false` you already set, in the shell environment or in `~/.llm-router/.env`,
  is left untouched, and a failed or inconclusive seat probe never enables it.

### Docs

- **Published the failed Q15 delegation A/B negative result.** Claude
  delegating named sub-edits to a local agent (`llm_local_task`) does not save
  Claude quota: 0/9 plan runs and 0/12 hard runs delegated at all, and the
  2 delegated easy-suite runs cost 0.98× Claude's own tokens against a
  pre-registered ship rule of ≤0.8×. Not wired. See
  `architecture/Q15_HARNESS_PLAN.md` and `docs/BACKEND-QUALITY.md`.
- Added `docs/repo_goals/` (`NORTH_STAR.md`, `AUDIT-2026-09-24.md`,
  `PLAN-fixes.md`) documenting the 2026-09-24 North Star audit and the plan
  to close its gaps.
- Removed the README's unsourced "35–80%" and "87%" savings figures; the
  README now points to `llm-router savings-report` for a user's own measured
  number instead of a general anecdote.
- Replaced the leftover "C H U Z O M" brand text baked into
  `src/llm_router/banner_art.txt`'s painted ASCII art with the llm-router
  brand (the separately-rendered wordmark below the painting already said
  `LLM_ROUTER`; this was a second, stale occurrence inside the art itself).
- `.github/workflows/benchmarks.yml` was re-enabled today after being
  disabled since July (`workflow_dispatch`/schedule only; last scheduled run
  before today: 2026-07-06). `docs/BENCHMARKS.md` is auto-generated and will
  pick up a current "Last updated" date on the next run.


## [15.2.0] - 2026-09-24

Security fixes and honest numbers, from a forensic audit whose every CRITICAL
and HIGH finding was re-checked by an independent verifier
(`audit/forensic_2026-09-24/`). Each fix below landed with a test that failed
before it and a red-check reverting only that fix; the full suite ran under a
clean HOME for every commit (final: 9,956 tests, 0 failures, 0 errors).

### Security

- **A cloned repository can no longer receive your provider API key.** A
  project `.env` setting `OPENAI_COMPAT_BASE_URL` made `call_llm` send the real
  `OPENAI_API_KEY` to that host (reproduced end to end against a local
  listener with a canary key). The openai-compatible provider now sends its own
  `OPENAI_COMPAT_API_KEY` or a placeholder; a project `.env` can no longer set
  endpoint-shaped keys (`…_URL/_BASE/_HOST/_ENDPOINT/_WEBHOOK`, proxies), and
  the hook injects only API keys and non-endpoint `LLM_ROUTER_*` settings from
  it — not `PYTHONPATH`, `NODE_OPTIONS` or `DYLD_*`. The pxpipe URL must be
  loopback. Your own `$LLM_ROUTER_HOME/.env` is unaffected.
- **The direct-execution chain is budget-gated and scrubbed.** It reached paid
  providers with no budget check and sent prompts unscrubbed. Paid providers
  now pass the router's own budget check (fails closed when unreadable), and
  prompt, history and system prompt are scrubbed after assembly.
- **`LLM_ROUTER_AGENT_WRITES=off/propose` now also covers `run_command`.**
  `python3 -c "open(p,'w')…"` wrote outside the project with writes off. Under
  `propose` (the default) and `off`, inline interpreter code, `sed -i` and
  `find` actions are refused. **Behaviour change:** `python -c` / `node -e` no
  longer run in the local agent loop unless writes are `apply`. This is not a
  sandbox — see SECURITY.md.

### Local drafts can read the repo

Measured before this release on the maintainer's machine: 0 of 1,185 audited
local drafts were used and verified savings were $0.00. The causes found and
fixed below are why. **Whether drafts now get used is not yet measured** — no
savings claim is made for this release.

- **Drafts get the conversation and the files the session touched.** The draft
  path now passes the session id and project root to context injection, and
  semantic retrieval is seeded with the session's recent file paths (code was
  retrieved for 0.9% of 456 replayed real prompts from the prompt alone, 98.9%
  with the seeds; a hit is not proof of relevance).
- **Every local draft may open files, read-only.** With the local agent loop on
  (the default), a draft runs the loop with `read_file`, `list_files` and
  `search_files` only; other tools are refused. Questions that name a file also
  draft read-only. If the loop produces nothing in time, the text chain runs.
- **The notice to Claude says what the draft read.** It used to say every draft
  was produced "WITHOUT access to your files" and to discard any answer that
  depends on the repo — true for blind drafts, false for these, and a rule no
  grounded draft could pass.
- **Drafting turns itself off after 50 unused drafts in a row**
  (`LLM_ROUTER_DRAFT_REVERT_AFTER`, `0` disables; delete
  `~/.llm-router/draft_streak.json` to resume).
- **The local context window is sized per model.** `qwen3.5`/`qwen3.8` get
  131072; everything else 32768 (`qwen3-coder:30b` at 131072 loaded at 70 GB,
  45% on CPU, on a 52 GB Mac). `LLM_ROUTER_LOCAL_NUM_CTX` overrides.

### Fixed — local models

- `search_files` scoped to a single file searched nothing and reported
  "(no matches)"; a draft then told the user a name did not exist.
- The capability probe marked every model incapable (it required a write while
  writes default to `propose`); it now writes inside its own temp dir, and no
  longer indexes that dir into the knowledge store.
- The knowledge store path was frozen at import, so tests wrote into the real
  `~/.llm-router/knowledge` (5,457 leftover project dirs on the maintainer's
  machine). New: `llm-router okf prune` lists them; `--apply` moves them aside.

### Changed — what the numbers mean

- **Savings nobody observed being used are "unverified", never a headline.**
  Only the hook's realized-gated rows count as verified; MCP/gateway/agentic
  credits and rows of unknown provenance are shown beside the headline,
  labelled `+ $X unverified, n=N`. On the maintainer's ledger (2026-09-24),
  `llm-router status` all-time went from **$372.58 "saved"** to **$0.47 saved +
  $483.14 unverified (n=46,099)**; the canonical accessor reports $0.11 (n=7).
- Docs state the tool counts the server registers: **70** (`LLM_ROUTER_SLIM=off`)
  and **12** in the default surface, including `llm_local_task`, which can edit
  and run commands. A test ties the docs to the registration code.
- Host docs: Codex has the prompt-routing hook but **no** tool-call
  enforcement; Pi is **not installable** today (`install --host pi` fails).

### Fixed

- The local agent loop's wall-clock cap never fired (an absolute deadline read
  as a duration), so the hook could be killed at 60s holding nothing.
- The Codex adapter no longer returns the CLI's stdin banner as the answer.
- `last`, `retrospect`, `replay` and `snapshot` exit non-zero on failure.
- The service `/health` reports the package version (was a hardcoded 5.3.0).
- Three scripts that could not compile on Python 3.11; CI now compiles `scripts/`.

### Removed

- `llm_router.cache.SemanticCache`: a stub whose `get()` always returned None,
  with no callers. The working cache is `llm_router.semantic_cache`.

### Measured, not shipped

- A local agent (qwen3-coder:30b) passes spec-shaped edits reliably, and a
  pre-registered rule selecting them held out at precision 10/10 tasks — but it
  admits 1 of 708 real prompts on the maintainer's machine, so prompt-level
  local routing was **not** wired. `architecture/Q15_HARNESS_PLAN.md`.

## [15.1.0] - 2026-09-23

Remediation plan II, the half that needed measuring before it could be fixed.
A minor release: routing behaviour changes in three places, each measured
before and after on the population it affects, plus one root-cause analysis
that was inventing findings out of a NULL column.

Every number below carries its n and the file that produces it.
`scripts/measure_low_signal_rate.py` re-derives the headline 49.8%.

### Changed — routing behaviour

- **S3b · A relative `that`/`which` is no longer read as a deixis.**
  "write a regex **that** validates emails" was classified context-dependent
  because `that` matched the anaphora pattern. A relative pronoun points inside
  its own sentence, not at the user's state, so it is masked before the deixis
  check when followed by something verbal. Measured on a 13-prompt labelled
  set: false positives **5/12 → 0/12**, recall **11/13 → 12/13**. The
  `xfail(strict=True)` carrying this in the S3 parity test is removed.
- **S4a · The bandit trains only on rows whose origin was recorded.**
  `aggregate_stats` now filters `provenance = 'runtime'`. On the development
  ledger, **1387 of 1601** rows were pre-provenance placeholders carrying a
  single latency value (500ms) and a flat $0.01 — 87% of what the bandit
  learned from was not measurement. Excluding them does not remove a model
  from routing (`reorder()` explores from `candidates`, not `eligible`).
  Measured delta: the top pick was unchanged; the two models dropped were
  already ranked last on the placeholder data.
- **S4b · A tie in the reward breaks toward the faster model.**
  Three free local models tied at identical expected value and `max()` resolved
  it by iteration order, landing on `qwen3.8:latest` at **54.3s** over
  `lfm2.5:8b` at **10.1s**. Latency is a **tie-break, not a cost term** — a
  cost term needs a $/second rate and no trusted row is paid, so any rate would
  be a guess embedded in routing policy. A tie-break cannot reorder any pair
  whose expected values differ. An unrecorded latency sorts **last**, never
  first.

### Fixed

- **A classifier confidence that was never recorded was reported as 0%.**
  `routing_decisions.classifier_confidence` is NULL for **213 of the 214** rows
  with trusted provenance. `retrospective.py` coerced that NULL to `0.0`, which
  is below every threshold, so each of those decisions was flagged
  `LOW_CONFIDENCE` and promoted by `classify_root_causes` to a
  `CLASSIFIER_ERROR` at confidence "High" with the evidence string
  *"Classifier confidence 0%"* — 213 certain findings manufactured from a
  missing column. The same coercion averaged NULLs into `avg_confidence`,
  making it roughly *the share of rows that recorded one*. An unrecorded
  confidence now gets its own `CONFIDENCE_UNMEASURED` flag and is counted
  separately; `avg_confidence` is computed over the decisions that have one and
  ships with its denominator. A genuinely measured `0.0` is still
  `LOW_CONFIDENCE`.

### Added

- **The low-signal fall-through is counted and read.**
  `classify.low_signal_classifications()` returns `(decided_by_default, total)`
  — the numerator with its denominator, because 12 fall-throughs is a healthy
  hook and a catastrophe in a gateway that served 12,000 requests. Registered
  in `counter_registry` and rendered by `llm-router doctor`, alarming on the
  *share* (≥25%); `0 of 0` reads as Unknown, never as a clean 0%.
- **`scripts/measure_low_signal_rate.py`** — the script every published form of
  the 49.8% came from, committed so the figure can be re-derived rather than
  believed. It reuses `scripts/groundtruth/sources.py` for the drop rules;
  two ad-hoc parsers of this repo's traffic have already disagreed.

### Measured, and deliberately not fixed

- **Half of all traffic is routed by a default, not by a classification.**
  Entered the plan as "word order changes the route" (`what is 17 * 3?` →
  `query`, `tell me what 17 * 3 is` → `analyze`). Measuring it first showed the
  framing was wrong. Only the **gateway** is order-sensitive (10/14 on a
  14-pair paraphrase corpus); the router and hook are 0/14. Both prompts score
  **zero in every category**, so `policy.low_signal_default` decides — `query`
  for the hook and router, `analyze` for the gateway, which is right by luck
  rather than by measurement.

  Over **n=1571 real prompts** (the CLAUDE.md drop rules applied first, which
  removed 1389 records): **41.4%** score zero, **49.8%** are decided by a
  default, and the gateway and hook return a **different** task type for
  **49.8%** of them.

  `ClassifySignal.confident` had recorded this since it was introduced and had
  **zero readers in `src/`**.

  **The route is unchanged.** Both candidate fixes were measured and refused:
  adding `(?:tell|show) me (?:what|how|…)` to the query intent takes gateway
  order-sensitivity 10/14 → 2/14 but pulls genuine analyze/code work into
  `query` 0/8 → 2/8 (gateway) and 2/8 → 4/8 (router and hook); flipping the
  gateway default re-routes half its traffic on no evidence that `query` is
  right for it. A labelled target-distribution set does not exist, and a
  proxy split has already misled this project by 4.25 points. The K4
  `xfail(strict=True)` therefore stands.

- **The `or 0` coercion class is sized but not swept.** 217 matches in
  `src/llm_router`. Most are benign sums; the dangerous ones are those compared
  to a threshold or averaged, which is what S9 was. A 217-site sweep is a
  refactor, not a remediation — the right shape is a lint, and that is its own
  task with its own red-check.

- **The low-signal counter is in-process.** `llm-router doctor` classifies
  nothing, so it renders `Unknown` there — honest, but it means the counter is
  load-bearing only where writer and reader share a process (the hook, and the
  gateway). Closing it properly means logging the outcome on the hook's
  existing `prompt_len=` line, which belongs with consolidating the hook's
  duplicated classifier rather than bolted on before it.

## [15.0.1] - 2026-09-23

Remediation plan II — what attacking the 15.0.0 release found. Patch release:
no routing behaviour changes, no money-figure changes.

### Fixed

- **`llm-router doctor --audit` no longer inflates the counter it reports.**
  Measured on a clean install of 15.0.0, three consecutive runs of a read-only
  diagnostic against unchanged state:

      fail_open_events: 4  ->  8  ->  12

  An operator investigating a high count was making it higher. Root cause:
  `_column_exists` consulted a hand-maintained allowlist of nine table names
  that had drifted — `codex_usage`, `gemini_usage` and `migrations` were all
  migrated by the module and none was on the list. An unmatched table returned
  `False`, meaning "the column does not exist", when the truth was "I cannot
  tell", so the `ALTER` ran, hit `duplicate column name`, and the failure was
  recorded as a swallowed exception.

  Two fixes, independently sufficient: the allowlist is replaced by a strict
  identifier pattern plus a parameterised `sqlite_master` lookup (neither can
  drift), and `_safe_migrate` treats `duplicate column name` as SUCCESS,
  because an idempotent migration that no-ops is the migration working.

  **If you have been reading `fail_open_events`, the figure included your own
  `doctor` runs.** Reset and re-measure.

- **`CHZ-FO-COST-MIGRATE-ALTER` is no longer the loudest code in the
  fail-open counter.** It was counting normal operation. A counter whose
  baseline is normal operation cannot signal abnormal operation.

- **`llm-router status` renders its markup instead of printing it.**
  `rich.text.Text(...)` does not parse markup; `Text.from_markup(...)` does.
  Four call sites in `ui/status_premium.py` built a markup string and passed
  it to the constructor that renders it literally — on the first command the
  README sends a new user to. The header was the visible one; fixing it
  exposed 30 more tokens from three other sites.

### Privacy

- **83 occurrences of a developer's home directory removed from committed
  documents** across `audit/` and `Docs/`, 63 of them in one archived file.
  An audit artifact should record the shape of a machine, never whose machine
  it was. Enforced by a test that scans every committed markdown file.

### Added — tests that pin what was found

- Every registered counter is asserted to read the same value twice in a row,
  and to read zero on a fresh database. Both properties existed for exactly
  one counter (`hook_liveness.orphan_count`) and were never generalised.
- The routing layers' agreement contract is pinned: a context-dependent prompt
  must write no pending enforcement state, so a tool no routed model can use
  is never held.

### Known, recorded, not fixed

Three findings are carried as `xfail(strict=True)` rather than fixed, because
each changes which prompts route or which model answers — and that is measured
on the target distribution or not at all:

- **The context-dependence detector over-fires** on incidental deictics
  ("write a regex **that** validates emails"). `enforce-route.py` removed its
  own re-check for exactly this reason; the detector itself was never fixed,
  so every remaining consumer still over-fires.
  **FIXED in 15.1.0** — see S3b below.
- **The routing reward is blind to everything but dollars.**
  `expected_value = success_rate * ANSWER_VALUE_USD - avg_cost`, and every
  local model costs zero dollars — so between two free models the reward picks
  the higher success rate. **CORRECTED 2026-09-23:** the 15.0.1 text said
  "which is the larger model". That is wrong — measured on the table the
  bandit actually reads, the 8B model outranks the 30B (EV 0.05000 vs
  0.04632), because the 30B's success rate is lower. The mechanism was right;
  the example was backwards. A local model's real cost is memory and latency;
  `avg_latency_ms` is already collected on every stats row and the reward does
  not read it. **PARTIALLY ADDRESSED in 15.1.0** — latency is now a tie-break,
  not a cost term; see S4b below.
- **Word order changes the route.** `what is 17 * 3?` classifies as `query`;
  `tell me what 17 * 3 is` falls to `analyze` — the more expensive tier.
  **REFRAMED in 15.1.0:** measured, this is not about word order and it is not
  a small finding; see "Half of all traffic is routed by a default" below.

## [15.0.0] - 2026-09-22

Remediation of the 2026-09-22 adversarial audit. Security fixes that need no
attacker and no unusual input, money figures that now mean what they say, and
the instrumentation that makes the next audit cheaper.

### Why this is a MAJOR version

Three user-visible behaviours change, and one of them will look like a
regression until you read the reason.

**Your reported savings will DROP, possibly to near zero.** Money surfaces now
exclude rows whose provenance was never measured, fail-closed
(`COALESCE(is_simulated, 1) = 0`). A row written before provenance existed was
never certified as production, and counting it asserts that it was on no
evidence. On the development machine this took the counted set from 285 rows
to 23. **Nothing was lost and no routing got worse** — the old number included
rows nobody could vouch for. `llm-router doctor --audit` prints the split so
you can see exactly how many rows are excluded and why. New rows carry
provenance from the moment they are written.

**Savings can now be NEGATIVE.** Ten clamped subtractions were removed,
including one in the shareable savings card and one in the web dashboard's
headline tile. Routing that cost more than the baseline now renders as a loss.
The clamp is precisely what stopped anyone finding out.

**The gateway now refuses requests it used to answer.** `/v1/responses`,
`/api/chat` and `/api/generate` return 400 for a request carrying `tools` or
`tool_choice`, matching `/v1/chat/completions` and `/v1/messages`. Previously
they discarded the tool definitions and returned fluent prose with
`finish_reason: "stop"` — a well-formed answer to a question the client had not
asked. If you were relying on that 200, you were getting the wrong answer.

### Security

- **Credentials no longer reach disk in the clear.** `attempt_log` is written
  through the canonical scrubber and created at `0600` rather than
  `0644`-then-chmod. Measured against the previous code, a GitHub PAT, an AWS
  key id, an AWS secret, a Slack token and a bearer token all landed intact in
  a world-readable file. Existing files are repaired on next write.
  **Truncation is not redaction** — `reason[:80]` previously shortened a
  100-character key enough to defeat an exact-match search while leaving 80
  characters on disk.
- **Nothing leaves the machine unscrubbed.** `alerts.emit_alert` POSTs its
  detail dict to a webhook; a live capture carried a Postgres DSN with a
  plaintext password off the host. Scrubbed at one chokepoint, with a new
  pattern covering `postgres`/`mysql`/`mongodb`/`redis` URLs that preserves
  host and port so the alert stays actionable.
- **A model-chosen command no longer inherits your environment.**
  `agent_loop.run_command` and `tools/local_task` now use an env ALLOWLIST. A
  denylist strips only names it knows; an allowlist carries nothing that was
  not named.
- **SECURITY.md stops calling the allowlist a security control.** It is a
  typo-and-footgun guardrail. 10 of its 28 permitted programs are
  general-purpose interpreters and each is a complete bypass: `cat
  ../../.ssh/id_rsa` is refused, `python3 -c` reading the same file is not.
  Use OS-level containment for untrusted repositories.

### Added

- `llm-router doctor --audit` — every instrumentation counter, the canonical
  savings figure with its baseline and denominator, the provenance split, and
  the Ground Truth scope.
- **Ground Truth consent.** Capture is on after an explicit prompt at install
  and stays OFF in a non-interactive install — CI cannot agree to anything.
  Revocation is `llm-router gt-consent --revoke` and is recorded as a refusal
  rather than a deletion.
- `hook_liveness` — a killed hook is now distinguishable from one that chose
  not to route, via a marker that outlives the process.

### Fixed

- **`attempt_log` rotation erased concurrent records** — 0.6–5.3% loss under 8
  processes, silently, in valid JSONL.
- **A censored or truncated answer counted as a routing win.** `LLMResponse`
  had no `finish_reason` field at all, so a `content_filter` stop returned
  fluent partial text that passed the bandit's success check.
- **Verifier validation accepted worthless verifiers.** `len(answer) > 5`
  reached HIGH confidence with a 0% real kill rate.

### Known and stated, rather than fixed

- **"Routing preserves task success" is UNPROVEN.** The only mechanism that
  could measure it was off by default, so there is no dataset, and neither
  downgrade-regret nor upgrade-waste is computed anywhere. Recorded in the
  machine-checked claims ledger; it fails CI if anyone marks it proven without
  evidence.
- **17 of 20 savings surfaces still compute their own figure.** All are
  individually named with their exact divergence, and a 21st cannot be added
  without joining the registry.
- **Word order changes the route.** `what is 17 * 3?` classifies as `query`;
  `tell me what 17 * 3 is` falls through to `analyze` — the more expensive
  tier. Found by the new adversarial corpus and carried as a strict xfail.
  **SCOPED 2026-09-23:** true at the *gateway*, which is what the corpus row
  exercises; the hook and router fall through to `query`, the cheaper tier.
  And the cause is not word order — neither prompt scores anything, so the
  door's default decides. See 49.8% measurement above.
- **Ground Truth covers state-free prompts only**, because no replayer exists
  for repo-bound tasks.

## [14.1.0] - 2026-09-21

Ground Truth accumulation: normal routing now produces replayable, verifiable
evaluation candidates. Adds one environment variable and one ledger field.
No MCP tool is added or removed, and model-selection policy is unchanged.

### Why this is a minor version

Nothing public was removed or renamed. `RouteLedgerRecord` gains fields and
`schema_version` moves 2 -> 3; readers already normalise by version, and
quality denominators test `>= 2` rather than `== 2` so v3 rows are counted
exactly where v2 rows were.

### Added

- **Ground Truth accumulation**, off by default behind `LLM_ROUTER_GROUND_TRUTH=1`.
  A routed task is assessed for replayability and verifiability at capture
  time, its replay envelope is captured, and it is admitted to a candidate pool
  or rejected with a stated reason. Wired into `_finalize_successful_route`,
  the single choke point every success path already passes through.
- **Ledger traceability (schema v3).** `session_id`, `prompt_sha256`,
  `response_sha256`, `latency_ms`, `complexity`, `classification_method`,
  `verification_type`, `verifier_name`, `capture_ref`. Content is hashed, never
  stored: the ledger still persists no prompt or response text. The contract is
  `route_id -> one record` and `prompt_sha256 -> the captured task`, both exact
  key lookups, with no timestamp used as a join anywhere.
- **Explicit provenance.** Every row records whether it came from a test or
  benchmark run, set from `LLM_ROUTER_SYNTHETIC` or pytest's own marker, never
  inferred from a model name or session id. Rows predating the field are
  treated as unknown provenance and excluded from evaluation rather than
  assumed to be production.
- **Verifier authoring assistant.** Proposes an acceptance contract and a
  verifier for a candidate, validates it by mutation, and requires human
  approval before it can grade anything.
- `scripts/groundtruth/` — corpus extraction, eligibility, replay envelope,
  candidate pool, sampling, verifier authoring, and CLI reports.

### Fixed

- `summarize()` filtered `schema_version == 2`. The bump to v3 would have
  emptied every quality denominator while still reporting a clean-looking 0%
  instead of a missing measurement. Now `>= 2`, with a test whose only job is
  to prove an equality filter would have dropped the row.

### Privacy

Prompt text is written only under `LLM_ROUTER_GROUND_TRUTH=1`, and only after
`secret_scrubber.scrub_text()` — the canonical scrubber — has run. Scrubbing
fails closed: if that module cannot be imported, nothing is written. Home
paths, emails and public IPs are additionally scrubbed; person and customer
names need `LLM_ROUTER_CAPTURE_DENYLIST`, because no regex can detect them.

### Known limitations

- The candidate pool is empty until the flag is enabled; none of this can be
  backfilled from existing telemetry.
- The verifier assistant's existing-test search matches on file stem and is
  noisy on common words. Tracked, not yet fixed.

## [14.0.0] - 2026-09-18

Five prerequisite defects in project scoping and grounding measurement, and a
new `semantic` package. Adds one CLI command (`llm-router semantic`) and four
environment variables. No MCP tool is added or removed, and model-selection
policy is unchanged.

### Why this is a major version

No public name was removed or renamed, so the repository's own semver gate
reports `PATCH` as the minimum. The gate compares the surface, and the surface
is not what changed here — **the default behaviour is**:

- **Source retrieval is on by default.** Every routed prompt now carries a
  `<repository_evidence>` block it did not carry before. Nobody asked for that,
  and it changes what every model sees on every call. That alone is what a
  major version is for.
- **An unobserved command outcome is recorded as `unknown` rather than `ok`.**
  Chapter sealing requires `ok`, and most tool responses carry no exit code, so
  expect markedly fewer sealed chapters. Correct, and a visible drop in a
  number somebody may be watching.
- **OKF writes less.** Definitions are verified against the file before being
  stored, so a store that previously accumulated unverified symbol names stops
  growing as fast — and some of what it already holds is wrong.

Anyone who wants the previous behaviour sets `LLM_ROUTER_SEMANTIC_SOURCE=off`.

Two notes for whoever reads the release tooling's output. The semver gate
reported "public surface unchanged" despite this release adding a CLI
subcommand, because it extracts top-level `def`/`class` names from eleven fixed
files and a subcommand is a string in a `choices` list — the same blind spot
its own comment records from 13.3.1. And `sync-versions.py` did not update
`npm/package.json` while `verify-version-sync.py` checked it; fixed here.

### Fixed

- Indexing a project while the process sits in a different one no longer writes
  the first project's documents into the second. `index_project(root=B)` called
  from inside A reported B's store in its return value and wrote A's directory;
  six OKF write paths recomputed their destination from the process cwd, and
  two of them had not been named in any prior audit of this bug.
- OKF no longer records what a file defines without reading the file. Two
  independent regexes produced a list of paths and a list of symbols, and the
  writers paired the first path with every symbol, so a reply mentioning a
  module that does not exist alongside a function defined elsewhere became a
  stored, retrievable claim that one defines the other.
- The grounding benchmark's scorer no longer accepts a wrong directory.
  `wrong_directory/okf.py` scored correct against `src/llm_router/okf.py`
  because the lenient rule was a substring test — it was meant to forgive an
  omitted directory and equally forgave a wrong one. Strict and lenient are now
  reported separately, each with its `n`.
- `router.py` now passes the project root into context preparation, so the
  code-context branch — gated on that argument and therefore unreachable for
  every routed call — can run. It also attaches context through the shared
  choke point instead of its own copy; it was the last path exempted by name.
- One project now resolves to one scope. Five modules answered "which project
  is this" privately, with two environment variable names and two fallbacks, so
  running from a subdirectory put OKF at the repository root and the caches on
  the subdirectory. `result_cache` was the one that did not heal: it hashes into
  a file path rather than a TTL'd column, so a divergent spelling orphaned a
  database nothing reopens.
- A command whose outcome could not be determined is recorded as `unknown`
  rather than `ok`. The fallback branch meant unobserved commands became
  evidence that a fix succeeded, and — because chapter sealing requires `ok` —
  quietly decided that unverified commits were milestones. Expect noticeably
  fewer sealed chapters; most tool responses carry no exit code.
- The biography no longer freezes at forty facts. Once the document was full,
  every durable fact learned afterwards was discarded in silence. The cap is now
  on the readable view, which says when it is showing a subset; the records
  themselves are uncapped and individually retrievable.
- Quality-escalation's short-prompt guard measures the user's prompt rather than
  the assembled context, so attached material cannot make a short prompt long.
- Tests no longer read the developer's live session store. `LLM_ROUTER_HOME` was
  documented as the sandbox for this and nothing set it, so a test asserting on
  context contents could fail carrying text from whatever you were doing.

### Added

- `llm_router.semantic`: a per-project derived index (SQLite, `ast`-extracted,
  rebuildable) and typed engineering-experience records carrying two timelines
  and four independent state axes. Retrieval produces an evidence pack that
  names what it omitted and why, renders filed prose inside an explicit
  untrusted region, and surfaces contradictory records rather than letting the
  newest win.
- `llm-router semantic index|status|explain|lessons|seed`.
- `LLM_ROUTER_SEMANTIC_SOURCE`, `_HISTORY`, `_INTERVENTION` (`off`/`shadow`/`on`,
  all `off` by default; `shadow` is byte-identical to `off`) and
  `LLM_ROUTER_SEMANTIC_ARM` (`B`/`C`/`D`/`M0`/`M1`/`M2`).

### Measured

Grounding, after the fixes above and before any semantic-layer work, so that a
scope fix cannot later be credited to a graph: **0/60 → 40/60 (+66.7%)** with
OKF context, on questions derived from symbols the repository defines exactly
once. Strict and lenient scoring agreed exactly (40 = 40). Material was
retrieved for 41 of 60; on those alone, 0% → 97.6%. Model `qwen3-coder:30b`,
seed 7. Full provenance and per-question detail in
`docs/measurements/2026-09-18-grounding-corrected-baseline.md`.

Arms A/B/C/D, same n=60 and same scorer, paired
(`docs/measurements/2026-09-18-semantic-arms.md`):

| arm | | correct |
|---|---|---|
| A | no context | 0/60 |
| B | OKF context — the corrected baseline | 40/60 |
| C | semantic pack, no traversal | **58/60** |
| BC | OKF + semantic — what default-on ships | 58/60 |

C beats the baseline by +30.0 points, 18 discordant pairs all one way, McNemar
exact p=7.6e-06 — and uses a median 122 of its 2000-token budget.

**Traversal was deleted, not switched off:** arm D answered all 60 questions
identically to C and cost 0.2s more. The blueprint's adoption gate for the
graph was +3 points over the corrected baseline; it scored zero, so the
expansion code, hop caps and high-degree penalty are gone — 97 lines net.

Three harder strata were then derived and run at n=60 each
(`docs/measurements/2026-09-18-harder-strata.md`), and they bound the claim:

| stratum | B | C | BC |
|---|---|---|---|
| symbol — query names the identifier | 41/60 | 60/60 | 60/60 |
| decoy — basename in two or more directories | 42/60 | 59/60 | 59/60 |
| concept — docstring, identifier hidden | 2/60 | 1/60 | 3/60 |
| absent — symbol does not exist | 60/60 | 60/60 | 60/60 |

The win survives same-basename decoys under a scorer that rejects a wrong
directory. It does **not** generalise to questions phrased in prose: on
`concept` everything collapses to a floor and the layer does not rescue it. On
`absent` nothing hallucinates, and the semantic arm retrieves nothing at all —
the correct behaviour for a symbol nobody wrote.

**It does not show improved task completion**, and the M0/M1/M2 history track
is still scaffolded and unrun, which is why history and intervention remain off
while source retrieval does not.
See `docs/decisions/0002-semantic-layer.md` for what was deliberately narrowed
and what is known to be broken and unfixed.

## [13.3.2] - 2026-09-16

Fixes to repository and session context, draft validation, routing telemetry,
and local-task execution defaults. No MCP tool or CLI command is added or removed.

### Fixed

- Codex installations now register and trust a Stop hook that shows estimated
  savings today and over the router's lifetime after every turn. The Codex
  plugin uses the same reporter, which preserves ongoing session context and
  reads the existing shared ledger across hosts.
- Routed execution paths share repository knowledge injection. The MCP path now
  loads session events from the caller's project even when the server starts in
  another directory. Fresh Git facts supply branch, commit and working-tree state.
- Session context retains more Bash, Write and Edit output. Knowledge retrieval
  recognizes document labels already mentioned in the same session, and session
  startup refreshes the repository index.
- Draft grounding checks bare filenames and checks symbols against the working
  tree, reducing both invented references and rejection of newly written code.
- Hook classification stays aligned with the shared classifier. Ollama model
  discovery and checked-in startup settings replace guessed model defaults.
- Routing telemetry records failed attempts and lost context evidence. Provider
  success signals require usable output, and unreliable providers are demoted.
- `llm_local_task` now defaults to proposing writes (`apply_writes=False`) and
  no longer implicitly enables unrestricted commands when writes are applied.
  Acceptance checks remain separate from worker execution.
- Removed unused policy-version code and restored regression coverage while
  triaging quarantined tests. Added an import regression check that records two
  existing control-plane modules with missing enterprise dependencies.

### Release checks

- Added a public-surface version check to the pre-release gate and a local gate
  covering lint, generated plugin files, mutation configuration and tests.
- Updated measurement guidance and benchmarks. This release makes no new token
  savings or routing-rate claim.

## [13.3.1] - 2026-09-13

Measurement, and what measuring exposed. Nothing here raises the routing rate;
several entries lower a number this project previously reported.

**A patch release that adds a tool.** `llm_local_task` is new API surface, and
the semver this file declares makes that a MINOR bump. Shipped as a patch at the
maintainer's explicit instruction — recorded here so the version number does not
have to carry the explanation by itself.

### `llm_local_task` — one Claude turn for a whole task

A new MCP tool (consolidated tier). Claude submits an objective and reads one
result; every read, edit and command in between runs on a local model and never
enters Claude's context. The prompt-time loop could not do this: it fires inside
UserPromptSubmit, is bounded at 90s, and answers a prompt rather than owning a
task.

Three properties, each from a measured failure:

- **A typed terminal status.** The loop returns "Agent reached maximum
  iterations" on exhaustion and `quality_ok` scored that string as a pass in the
  2026-09-12 benchmark. Exhaustion is now `incomplete` and can never be
  `verified_complete`.
- **Acceptance checked by a supervisor subprocess**, never by the worker. The
  same benchmark caught the local model diagnosing a bug correctly in prose and
  never changing the code.
- **No cloud fallback.** Ollama down or budget gone returns a typed failure with
  whatever was staged.

Verified end to end against Ollama: a failing test fixed, edited on disk, check
passed, in one Claude turn.

**What it does not do: make the model better.** Measured on the 11-task brutal
suite across four local configurations — raw loop, raw loop with two previously
broken tools fixed, the service with no acceptance check, and the service with
one — every run scored **8/11 with the same three failures**. Codex and Claude
score 10/11. Budget, checking and task-ownership move none of it. Ship it for
the turn saving; scope the work to failures you would catch.

### Savings are credited only when a Claude turn was actually replaced

`log_direct_savings()` ran unconditionally ~39 lines before the `_turn_blocked`
check that decides whether the routed answer substituted for anything. In echo
mode — the default — nothing is substituted, so **$0.426410 was credited on
2026-09-12 against drafts the debug log recorded as `DRAFT UNUSED`**. The call
now sits below the check and carries `realized`; a non-realized route still
writes a row, at zero, tagged `mode='echo'`, because a missing row is
indistinguishable from the hook never running. New nullable `mode` column;
existing rows stay NULL rather than being backfilled as verified.

### Execution and routing traces

`LLM_ROUTER_TRACE=1` (or `LLM_ROUTER_TRACE_FILE`) writes a JSONL fact stream.
The agent loop emits every model round-trip, every tool call with arguments and
result, and a terminal reason: `max_iterations`, `budget_exhausted`,
`repeated_identical_call`, `llm_unreachable`,
`final_text_without_any_tool_call`. The routing hook emits the prompt, the
decision (task type, zone, pressure, `needs_tools`, the chain actually built)
and the outcome — including `substituted`, which says whether Claude's turn was
replaced or merely decorated.

`scripts/trace_view.py` renders it: `--verdict` for "did the model do the work",
`--routes` for "did routing replace a Claude turn, or just route". Both
distinctions have historically been miscounted here.

### Fixed

- **`list_files` and `search_files` were broken under any symlinked project
  root.** They reported paths relative to the unresolved root while
  `_resolve_path` validated against the resolved one, so on macOS — where `/tmp`
  is `/private/tmp` — both returned "is not in the subpath of" for directories
  the model could legitimately read, and it burned its iteration budget
  retrying. Every local benchmark score before 2026-09-13 was measured this way.
  Re-running with the fix gives the same 8/11.
- **Interception admits `&&` conjunctions of allowlisted reads.** Every segment
  must independently pass the allowlist, so `git status && rm -rf build` stays
  refused without this module judging which half is dangerous. Honest effect:
  eligibility moves from 8/492 to 12/492 of real commands; 436 of the rest are
  pipes and redirects. It did expose a real bug — a leading `cd` was being
  stripped by `effective_command`, which would have returned a different
  directory's file as the answer.
- **The benchmark harness measures `time.monotonic()`.** A macOS Maintenance
  Sleep advances the wall clock and not the monotonic one; one task was recorded
  as 918.6s when 902s of that was the laptop asleep.

## [13.3.0] - 2026-09-12

Local models can now do the work, and the tool calls that cost the most can be
answered before they reach Claude. No savings figure is claimed here on purpose —
see "What is not claimed" below.

### The agent loop actually works now

`qwen3-coder:30b` emits tool calls in Qwen's XML dialect while leaving Ollama's
structured `tool_calls` field empty. The loop read that as "the model only
chatted" and discarded correct calls. Fixing the parser moved a 5-task harness
from 2/5 to 5/5, and the loop now scores 40/40 across shell, code navigation,
file edits and vision.

- Qwen XML tool-call dialect is parsed (`_repair_xml_toolcalls`)
- Grammar-constrained decoding via Ollama `format=`, with `finish` as a real
  tool so a constrained model can stop — without it the grammar could only
  express "call something" and the model repeated one read to exhaustion
- An identical repeated call is interrupted rather than run again
- A wall-clock budget bounds the loop (default 90s); 15 iterations at a 60s
  per-call timeout was a 15-minute worst case inside UserPromptSubmit
- Context discipline: tool results capped, `read_file` takes offset/limit, the
  task is re-stated every turn, and the harness evicts oldest TOOL RESULTS —
  llama.cpp evicts the system prompt instead, silently
- The loop runs by default (`LLM_ROUTER_LOCAL_AGENT_LOOP`)

### Local interception (opt-in)

A PostToolUse hook cannot replace a tool result — verified live. A PreToolUse
`deny` carrying the answer can, so that is the mechanism.

- `image_intercept`: a Read of a raster image is answered by a probed local
  vision model; the image is never loaded
- `bash_intercept`: an allowlisted read-only command is run by the hook and its
  output compressed; the uncompressed output never enters context
- Both OFF by default, configured in `~/.llm-router/routing.yaml` (a hook never
  sees a shell export)
- Every interception is logged to `~/.llm-router/intercepts.jsonl`

### Vision capability is measured, not assumed

`vision_registry` probes each model that advertises vision by asking it to read
a randomly generated code from an image, three times, exactly. Verdicts are
cached and re-probed when the installed model set changes. Nothing is hardcoded
per model, and anything unproven routes to Claude.

### Safety

- `agent_writes`: local edits default to `propose` — a diff, not a write — and
  `apply` journals the previous contents first, refusing any edit it cannot undo
- `run_command` defaults to an inspection allowlist; compound commands are
  refused whole rather than parsed
- A cross-session leak in `mcp_roots` is fixed: the cache was keyed on
  `id(session)`, a memory address, so a recycled address served one session's
  project root to another

### Measurement

- `effective_rate` in the routing report counts drafts USED, not produced — the
  old rate counted production, which is how a fully-routed-looking session drove
  quota from 49% to 79%
- `POST /ground` exposes the grounding check (docs/GROUNDING_API.md)
- Compression no longer truncates blindly: an unrecognised output shape declines
  rather than returning `output[:200]`, which had been silently dropping data

### What is not claimed

No token-savings figure. Two figures reported during development were
projections from offline replays rather than observations, and both were wrong.
`scripts/intercept_report.py` reads the interception log so the next figure is
measured from real use. Until then this release ships the capability and no
number.



## [13.2.2] — The injected block repeated itself (2026-09-10)

### Fixed

- **Every retrieved document reached the model twice.**
  `_write_source_concept` stores one string in two fields — `description` is
  `summary[:120]`, the body is the full `summary` — and `as_context_block` emitted
  both. So a SourceFile arrived as its symbol list truncated mid-name, followed
  immediately by the same list in full:

      ## [SourceFile] src/llm_router/router.py
      Defines: route_and_call, build_chain, ..., _format_subprocess_chain_error,     <- 120 chars
      Defines: route_and_call, build_chain, ..., execute_chain, _call_text, ...      <- 1003 chars

  With up to three documents injected inside a 3000-token draft budget, that is
  real space spent on a duplicate — and the truncated copy is worse than useless: a
  name cut in half is a name the model can complete wrongly, which is the failure
  the grounding checks exist to catch.

  Deduplicated at render, not at write, so documents already on disk benefit
  without a re-index. The body wins when one string contains the other, since the
  description is the truncated one; a description that says something the body does
  not — curated notes, the model catalog — is kept. Measured on a live document:
  765 -> 644 characters.


## [13.2.1] — The index eroded itself (2026-09-10)

### Fixed

- **`okf index` decayed as you worked.** `_write_source_concept` overwrote the
  document rather than merging into it. That was harmless while only routed answers
  enriched, which was rare — and 13.2.0 put enrichment on `context-capture.py`,
  which fires on EVERY tool call with a cap of 10 symbols and sees only what the
  tool printed. So one tool result mentioning one function replaced that file's
  entire indexed document with that single symbol.

  Measured after a few hours of ordinary work, against 1069 indexed documents:

      docs with FEWER symbols than the file defines: 19

      src/llm_router/hooks/auto-route.py    stored   1 / real  91
      src/llm_router/cost.py                stored   1 / real  63
      src/llm_router/router.py              stored   1 / real  52
      src/llm_router/okf.py                 stored   1 / real  36

  Silent, and it points the wrong way: the eroded files are the large central ones,
  because those are what tool calls keep touching, so the documents most likely to
  be asked about were hollowed out first. It also disguises itself — a query that
  worked right after indexing stops working an hour later, and the cause looks like
  a scoping problem.

  Writes now merge. Only `index_project` may shrink a document, because only it
  read the whole file; a writer that saw a fragment can no longer assert that the
  file contains less than it does.

### Upgrading

Anyone who ran `llm-router okf index` on 13.2.0 has a partly eroded store. Re-run
it once after upgrading; the merge keeps it correct from then on.


## [13.2.0] — Routing recovery: the timeout, the contamination, the fragmentation (2026-09-10)

Sustained routing had fallen from 31-39% to ~2% of real prompts (5 successes in 246,
2026-09-04..09-10). Three separate defects, each found by measuring rather than
reasoning, and one non-fix that the measurement talked us out of.

### Fixed

- **`OLLAMA_TIMEOUT` defaulted to 4s, which no local model could meet.** Measured
  p50s are 11.4s (lfm2.5:8b), 15.8s (qwen3-coder:30b) and 28.5s (qwen3.8); even
  "Say OK." took 6.6s warm. Every DIRECT attempt aborted at exactly 4s and fell
  through — 86 of 246 real prompts in one week. Now 45s.

- **OKF retrieval contaminated prompts it had nothing to do with.** A `capital of
  Portugal` question came back carrying another project's source doc and a model
  capability sheet: the MCP server's cwd is `$HOME`, which has no `.git`, so every
  project collapsed into one scope; the shared model catalog was a retrieval root
  and matched everything; and the relevance floor was `> 0`. Scope now honours
  `LLM_ROUTER_PROJECT_ROOT`, the catalog is out of task retrieval, and matching is
  weighted and token-based.

- **Session events were scattered across project buckets and mostly unreadable.**
  `_project_id()` hashed the raw cwd, so a session that moved between directories
  split its log while `build_session_context` read one bucket — 451 recorded, 284
  readable. Now resolved to the repo root.

- **Claude's own answers were never persisted**, only routed ones, so the stored
  conversation held every question and no conclusion.

- **Four import-time path bugs of one class**, where a module-level constant froze
  `$HOME` before any isolation override could apply: the auto-route debug log (227
  test rows in the production log), `cost.savings_log_path()`, `receipt_store`,
  and session scoping. A path computed at import ignores every isolation mechanism
  added afterwards.

- **The gateway rejected every normally-configured SDK client.** Honouring the
  caller's `model` forwarded a bare name to `model_override`, which requires
  `provider/model` and 400s otherwise. Each wire endpoint now qualifies with its
  own provider; `auto` still means "you pick".

- **`LLM_ROUTER_SESSION_CONTEXT=local` silently disabled context entirely** — the
  hook passes `target_provider="local"`, which was missing from the allowlist.

- **The session store re-ingested its own injected context.** `record_event`
  stripped llm-router's sentinel but not OKF's `<knowledge_context>`, so retrieved
  documents were recorded as though the user had typed them.

### Added

- **`llm-router okf index`** — index a repo's tracked source into the knowledge
  store. The store could previously only be filled by a successful routed call,
  which is a deadlock; it held 2 documents after weeks of use, and 1063 after.
- **`llm-router sessions status` / `merge`** — recover events stranded by the old
  scoping bug. Dry-run by default, timestamp-ordered, originating shards kept.
- **Grounding checks on routed drafts.** A draft citing a file or calling a
  function that exists neither in its context nor in the index is discarded and the
  turn falls through, rather than being shown.
- **Host `env` propagation** — `LLM_ROUTER_*` settings now travel in each host's
  MCP config, so Cursor/OpenCode/Codex get the tuned models instead of falling back
  to one that is not installed. Credential-shaped names are never propagated.

### Not changed, deliberately

- **The context-dependent gate was left alone.** It skips ~48% of prompts and
  looked like the main culprit; its noun list even contains `agent`, matching every
  prompt about the user's own "Transfer Agent". Measured against 376 real prompts,
  loosening it freed 36 — and almost every one genuinely needed local state
  ("commit this and show me the demo again"). Routing those produces fabrication,
  not savings. Seven of those prompts are pinned in a test so the next attempt has
  to argue with the measurement.

- **Consolidating session shards at runtime**, which would have recovered the rest
  of the fragmentation but turned `load_events` into a cross-project read path.

### Notes

Two mechanisms in this release were measured, found to be producing confident
fabrication, and fixed before shipping — an eligibility count is not a quality
measurement, and both times only real model output caught it.

---

The following also ships in 13.2.0; it was already on `main` unreleased when the
routing work landed.

### Fixed (host installation)

- **Codex → llm-router works, for the first time.** The installer wrote the
  MCP server to `~/.codex/config.yaml` (and an older path to `config.json`
  and `rules/llm_router.md`). Codex reads only `config.toml`, so no Codex
  session has ever seen llm-router. One writer now targets
  `[mcp_servers.llm_router]` in `config.toml` (via `codex mcp add`, TOML
  fallback), `hooks.json`, and a marked block in `AGENTS.md`, and removes the
  legacy entries when they are ours. The duplicate installer in `cli.py` is
  gone.
- **Codex hooks now actually run.** Codex 0.153 silently skips any hook in
  `hooks.json` without a `[hooks.state."…"] trusted_hash` record in
  `config.toml`. `llm_router.codex_host` computes the hash (verified against a
  real run) and install writes it for every hook it installs; doctor fails on
  a missing or stale record.
- `llm-router doctor --host codex` validated `config.yaml` and reported a
  broken install as healthy. It now checks `config.toml`, that the command
  can start, `codex mcp list`, hook trust, and `AGENTS.md`.

### Added

- **Routing defaults come from the seat table.** With
  `LLM_ROUTER_SUBSCRIPTION_PROVIDER` unset, the strongest logged-in seat is
  the subscription provider (Claude over ChatGPT over Google) and the other
  seats join the free bucket, so a Claude Max + ChatGPT machine sends cheap
  work to Codex and hard work to Claude from either host, with nothing
  configured. The env var still wins; `LLM_ROUTER_SEATS_AUTO=off` restores
  env-only behaviour.
- **`llm-router install --project`** writes a marked llm-router block into the
  repository's `AGENTS.md` and links `CLAUDE.md` to it (a copy on Windows; an
  existing `CLAUDE.md` file is kept and gets the same block), so Claude Code and
  Codex read one set of project rules.
- **`llm-router install` auto-detects Codex.** With no `--host`, a machine
  that has Codex gets it wired in the same run, and the seat table prints at
  the end. `--no-hosts` opts out; `--host codex` is unchanged. Gateway mode is
  opt-in (`--mode gateway`), no longer the Codex default.
- **Codex gets push routing.** A `UserPromptSubmit` hook injects the same
  `⚡ ROUTE:` hint Claude Code gets. `hosts/events.py` now carries the verified
  Codex prompt payload keys (fixture from a real run).

- **Seat detection.** `llm-router doctor` and the session banner now show which
  subscriptions this machine is logged in to (Claude via `claude auth status`,
  ChatGPT via `codex login status` plus the plan claim in the Codex login
  token, Gemini CLI, Ollama) and the free bucket derived from them. Cached in
  `~/.llm-router/seats.json`; kinds and plan names only, never tokens. A plan
  claim past its window is reported as stale but still counts, because login
  status is the fact and the claim a hint. First step of
  `guide/PLAN_DUAL_HOST_INSTALL.md`.

## [13.0.8] — Close the second co-owned-file guard (2026-08-31)

Closes #92.

### Fixed

- **The sandbox guard no longer whole-file-diffs `.claude/settings.json`.**
  The same structural problem #88 fixed for `~/.claude.json`, in the other
  `_REPORT_ONLY` entry. That file is co-owned — a live Claude Code
  session, another tool, or the user editing their own settings all write
  to it — so diffing it whole makes the guard fire on somebody else's
  change and blame whichever test sampled it at that instant. It had not
  flaked yet purely because of timing; the race was identical, and #88
  cost hours precisely because a false escape report looks exactly like a
  real one.

  The snapshot now compares only what `install()` writes:
  `mcpServers["llm_router"]`, `statusLine`, and hook registrations —
  with hooks filtered to commands naming this package, since the user's
  own registrations share those lists. Mutation-checked both ways:
  unrelated key churn, a third-party MCP entry and a user-edited hook are
  all ignored, while our own mcp entry, statusLine or hook being changed
  is still caught. Not an allowlist entry, which would have blinded the
  guard to the escapes it exists to detect.

### Known issues

An `-m slow` run still calls `uv build` three times (#91): three test
files each define their own module-scoped `built` fixture. Excluded from
the default addopts and from CI, so no standard run pays it.

## [13.0.7] — Test-suite honesty (2026-08-31)

Closes #82, #84, #87, #88. Every one of these is a test that reported
something other than what was actually wrong, and three of the four
issue titles turned out to be misdiagnoses corrected by reproduction.

### Fixed

- **~100 more `llm_router <cmd>` strings across `src/` (#82).** Follow-up
  to #72. A fresh scan found 225 raw matches across 86 files, not the ~89
  estimated; after the `ast`/`tokenize` pass that drops docstrings and
  comments, 100 were genuine user-facing violations across 44 files. The
  lint's file list grows from 2 to 46 — the ~42 files whose matches were
  entirely comment noise are deliberately excluded, since there is
  nothing there to guard. Two matches were prose that merely reads like a
  command; rather than start a suppression list they were reworded, so
  both files are genuinely clean instead of exempted. Nine more
  assertions across eight test files had the broken string encoded as
  expected output.

- **The concurrency test now has a budget it can meet (#84).** Filed as
  order-dependence; it is not. The project-wide `timeout = 30` is sized
  for unit tests, and this one forks six processes doing 200
  lock-serialized writes each. Under CPU load, pytest's own setup was
  measured at 22-27s, so setup plus call crossed 30s; the watchdog then
  unwound the test while workers were still writing and the enclosing
  `TemporaryDirectory` deleted the directory underneath them —
  indistinguishable from lost writes. Random ordering never leaked state
  into it; it only changed how much of the budget was left.

- **The build-artifact tests likewise (#87).** Same error string, a
  different cause — worth stating, because #84 had just taught the
  opposite lesson. Here pytest-timeout fires inside
  `subprocess.communicate`, blocked on a real `uv build` child: 16.8s
  idle, 76.4s under load. The suggestion that each test rebuilds the
  package was checked and disproved — the fixture is already
  module-scoped and builds once — so no session fixture was written. The
  global default stays at 30: three files needing more is a property of
  those tests, not of a ~6,900-test suite.

- **The sandbox guard no longer blames tests for another process's
  writes (#88).** Filed as tests escaping their sandbox to write
  `~/.claude.json`. Instrumenting every write path across ~10 full-suite
  runs never once caught this package touching that file. What changed
  between a failing run's before/after pair was `promptQueueUseCount`
  — a concurrently running Claude Code session's own bookkeeping. The
  tell was that an identical seed and test order passed and failed on
  back-to-back runs, which an in-process defect cannot do. The guard now
  diffs only `mcpServers["llm_router"]`, the one slice the installer
  owns; real escapes are still caught, and it is not an allowlist entry.

### Known issues

An `-m slow` run calls `uv build` three times, because
`test_sdist_excludes_quarantined_tests.py` and
`test_gh43_direct_execution_disclosure.py` each define their own
module-scoped `built` fixture. Excluded from the default addopts and from
CI, so it affects no standard run. `.claude/settings.json` has the same
whole-file-diff weakness #88 fixed for `.claude.json`; it has not flaked
yet.

## [13.0.6] — Remove the dead enterprise surface, and a credential leak (2026-08-31)

Closes #68, #69, #70, #71, #72, #74, #75, #79 — everything a repo-wide
audit turned up after 13.0.5, plus three bugs found while fixing them.

### Security

- **Importing `llm_router.providers` loaded an unrelated `.env` into the
  process (#74).** `litellm/__init__.py` calls `dotenv.load_dotenv()`
  unconditionally at import time when `LITELLM_MODE` is unset — and it
  defaults to `"DEV"`. With no argument, `find_dotenv()` walks upward from
  *the caller's own source file*, i.e. from inside
  `.venv/lib/.../litellm/`, not from the working directory. That walk
  climbs out of the project entirely and can reach a personal `~/.env`,
  whose contents are merged into `os.environ` for the life of the
  interpreter. Verified directly: `XAI_API_KEY` absent before the import,
  present after. Any credential in any `.env` above the virtualenv was
  being loaded without the operator asking. Fixed by setting
  `LITELLM_MODE=PROD` before importing litellm, which still honors an
  explicit setting.

### Removed

- **The enterprise surface (#68, #70, #71).** `llm_router.enterprise` is
  not shipped, and five modules depended on it — each converting the
  missing module into a failure at a different layer, all invisible by
  default. `LLM_ROUTER_RBAC_MODE=strict` made every route raise
  `AttributeError` before contacting a provider, so enabling the
  officially safer mode was a total outage rather than enforcement.
  `audit_routing.py` raised on every routed turn behind a blanket
  `except`, so the routing audit trail has never worked on any install.
  `llm-router audit verify`, the documented tamper-evidence gate, died
  with a raw `TypeError`. And `verify_enterprise.py` — the tool built to
  catch exactly this drift — was never wired into the CLI and crashed if
  invoked, which is why the other three went unnoticed.

  `commands/audit.py` is kept: its `misroute` subcommand is the only entry
  point for a live, unrelated feature. Default-mode routing is unchanged,
  proven by an identical 239-test router battery before and after.

### Fixed

- **An unrecognized `LLM_ROUTER_PROFILE` stopped the server booting
  (#69).** `RouterConfig.llm_router_profile` is bound by naming
  convention to that variable and validated against the routing tiers, so
  `LLM_ROUTER_PROFILE=enterprise` raised `ValidationError` at import time
  — before the startup checks that would have refused it with an
  actionable message. This was the third reader of that one variable, and
  the one that actually drives routing; #65 fixed only the display-only
  one. It now accepts `LLM_ROUTER_COST_PROFILE` as an alias and falls back
  to `balanced` with a warning naming both axes, without special-casing
  any particular value.

- **`install` and `doctor` printed a command that does not exist (#72).**
  37 user-facing strings said `llm_router <cmd>`; the binary is
  `llm-router`. Every failing doctor check named an unrunnable command in
  its `fix=` hint, and the headless Dockerfile snippet failed at its
  second line when copy-pasted. The docs-lint now covers these two source
  files, distinguishing a printed command from `import llm_router` by what
  follows the name, then dropping docstrings via `ast` and comments via
  `tokenize`.

- **Two unrelated bugs behind the "flaky tests" (#75).**
  `judge.evaluate_response_async` created an `asyncio` task and never
  awaited it or held a reference. On the session-scoped event loop, that
  orphan ran during a *later* test's awaits and hit whatever
  `litellm.acompletion` mock was installed — so a Claude model appeared in
  assertions expecting another provider. Not ordering at all: the sample
  rate is 0.1, so it fired on `random.random()`, which is why identical
  commits gave opposite CI results. Holding a reference is also a
  production fix — asyncio may collect a pending task nothing refers to.
  Separately, a test's own SQL stored `datetime('now','localtime')` where
  production stores UTC, so the reader's single `'localtime'` conversion
  shifted it twice.

- **A fixture that failed for ~100 seconds after local midnight (#79).**
  `test_routing_logic_uses_today_cutoff` seeded rows at `now - 100` and
  asserted they counted as today. A CI run starting at 23:58 UTC failed
  both Python jobs. Rows are now anchored to the day boundary.

### Internal

- The release tooling could not read this changelog: `extract_changelog_entry`
  matched only `## v1.2.3` headings, and the plugin lookup used the module
  name `llm_router` where the manifests say `llm-router` — the same naming
  confusion, in the tooling that ships releases. Both fixed in 13.0.5's
  release commit.

### Known issues

Underscore-CLI strings remain across the rest of `src/` (#82). One
order-dependent test remains in `test_session_store_concurrency.py` (#84).

## [13.0.5] — The nine issues opened this week (2026-08-30)

Fixes #59-#67. Three of them (#59, #60, #63) were retests of issues closed as
fixed in 13.0.4, where each fix had missed the real code path because it was
verified against existing DB rows or synthetic env rather than a fresh run.
Every fix here was built the other way round: the repro test written first,
observed failing, then patched, then mutation-checked by reverting the fix to
confirm the right test dies.

### Fixed

- **Session-scoped `set-enforce` never activated (#59).** `_session_enforce()`
  and `_run_set_enforce()` read a bare `CLAUDE_SESSION_ID`; Claude Code exports
  `CLAUDE_CODE_SESSION_ID`. `set-enforce` wrote the global `routing.yaml` every
  time while printing "(this session only)". Both now resolve through
  `session_store.resolve_session_id()` — writer and reader share one resolver,
  which is the property that was actually missing.

- **`routing_decisions` was always empty (#60).** `log_routing_decision` was
  gated behind `if classification_data:`, which `route_and_call` defaults to
  `None` and `tools/text.py` never passes — so the entire consolidated tool
  surface could not write a row. Fixed at the sink, covering `llm`, `llm_query`,
  `llm_code`, `llm_analyze`, `llm_generate`, `llm_research` and any future
  caller. Synthesized rows are marked `classifier_type="unhinted"` with NULL
  confidence, latency, budget and quality_mode, so an unclassified call cannot
  masquerade as a measured one. Ten `classification_accuracy is None` crash
  sites are fixed — four of them in `retrospective.py` itself, which neither the
  issue nor the plan had listed.

- **An unknown subcommand started the MCP server (#61).** `main()`'s final
  `else` was reached by any unrecognized token, so a typo launched the full
  stdio server and hung the terminal. The server now starts only with no
  arguments; anything else exits 2 with a `did you mean` suggestion.

- **The ensemble classifier failed silently on every call (#62).** `ensemble.py`
  hardcoded `ollama/qwen2.5:7b` with no check that it was pulled, so on a machine
  without it the classifier failed every time, degraded to the heuristic, and
  `doctor` still reported Ollama green. `doctor` and `verify` now compare both
  classifier models against `/api/tags`, normalizing the implicit `:latest` tag.
  `LLM_ROUTER_ENSEMBLE_SECONDARY` is new — the secondary had no override path at
  all, so suggesting one would have been false advice.

- **The statusline called a working setup an outage (#63).** The health check
  tested five cloud API keys and not `LLM_ROUTER_CLAUDE_SUBSCRIPTION`, and
  conflated "a provider is configured" with "something happened in the last 30
  minutes". Subscription mode is recognized (via `install_hooks.check_api_keys()`,
  not a second parser), and a new `idle` state separates a quiet Ollama from an
  unreachable one.

- **The quality breaker overrode explicit pins, invisibly (#64).**
  `should_skip_model()` blacklisted a model after three answers below 0.4, and a
  terse-but-correct QUERY answer scores ~0.3 because it can never earn the
  length or structure bonuses — so three *correct* short answers permanently
  disabled a pinned model. Pins are now exempt alongside `model_override`, and
  every skip appends a visible marker to `chain_attempts`: a candidate exclusion
  must leave a trace.

### Added

- `LLM_ROUTER_COST_PROFILE` for the routing cost tier (#65). `LLM_ROUTER_PROFILE`
  meant both that and the enterprise identity mode, so following the documented
  rename for one silently broke the other. The legacy name is still read, but
  only when its value is a valid routing tier, which makes the two readers
  mutually exclusive immediately rather than in 14.0. `VALID_PROFILES` now
  derives from the `RoutingProfile` enum instead of a hand-written list that had
  3 of the real 6 values.
- `LLM_ROUTER_QUALITY_MIN_CALLS`, `LLM_ROUTER_QUALITY_SKIP_THRESHOLD` and
  `LLM_ROUTER_QUALITY_SKIP=off` — the quality-skip thresholds were hardcoded and
  undiscoverable.
- A docs-lint test that derives its subcommand and hook-filename lists from
  `cli.py` and `install_hooks.py` rather than freezing a copy, so it cannot rot
  the way the docs it polices did.

### Changed

- **Documentation described a tool surface that no longer exists (#66).**
  `llm_health`, `llm_quality_report`, `llm_classify`, `llm_cache_stats`,
  `llm_cache_clear`, `llm_setup` and `llm_policy` are absent from the
  consolidated 11-tool surface. 16 files were affected, not the 4 reported.
  `LLM_ROUTER_SQL_DEBUG` and `LLM_ROUTER_HOOK_DEBUG` did not exist anywhere and
  are gone. `plugin.json`'s `mcpServers` key pointed at a `.mcp.json` that
  `.gitignore` excludes repo-wide, so it is removed rather than dangling.
- **`--help` told users to run a binary that is not on `$PATH` (#67).**
  `cli.py`'s docstring printed `llm_router <cmd>` 47 times; the installed binary
  is `llm-router`. `rules/llm_router.md`, installed as the agent-facing rules
  doc, was handing a live agent two commands that do not exist. The v5
  `rules/llm-router.md` fork is deleted — `install_hooks.py` already treated its
  installed counterpart as a pre-rebrand artifact, which is how it drifted.

### Known issues

`LLM_ROUTER_PROFILE=enterprise` still raises `ValidationError` at import time
through `config.py`'s `RouterConfig`, a third reader on a different mechanism
(#69). The enterprise surface is non-functional end-to-end: #68, #70, #71.
Remaining underscore-CLI strings in `install` and `doctor`: #72.

## [13.0.4] — Security: remove the unauthenticated SSE entry point (2026-08-28)

### Security

- **`llm-router-sse` is removed.** `server.main_sse`'s own docstring forbids
  exposing it as a console script — the prior entry point "bound 0.0.0.0 with no
  auth and exposed the full 60-tool MCP surface — including filesystem tools and
  wallet — to anyone reachable on the network" — and lists three conditions that
  must all hold before re-adding it. It was nonetheless present in
  `[project.scripts]` and shipped in 13.0.2 and 13.0.3 with none of them met: no
  auth middleware (that is `main_sse_secured`, which the script did not point
  at), and a bind of `os.environ.get("HOST", "0.0.0.0")` that never consulted
  `_allow_public_bind()` — a gate defined a few lines below it.

  Anyone who ran `llm-router-sse` published the full MCP tool surface,
  unauthenticated, on every interface. **If you have run it, stop the process
  and upgrade.** Nothing else invokes it, so an install that never ran the
  command was not exposed.

  Defence in depth: `main_sse` now defaults to `127.0.0.1` and refuses a
  non-local bind unless the shared gate allows it.

### Fixed

- **Statusline reported "✗ no provider" for every Ollama-only setup** (#50).
  `$SAVINGS_LOG` was read four times and never assigned, so `open("")` threw
  into a swallowed except and the health check could never see local activity —
  breaking the documented "route free to local Ollama with no cloud keys" path.
- **The dashboard's access URL was redacted by its own scrubber** (#48). The
  token pattern matched both the `token=` and `url=` log fields, so the only
  documented way to obtain a working URL printed `[REDACTED-TOKEN]`. The URL is
  now printed outside the logging pipeline; redaction itself is unchanged.
- **`doctor` reported three different states as "no routing decisions"** (#55).
  An unreadable table, an empty table on a machine that recorded activity
  elsewhere, and a genuinely idle machine printed the same sentence — so a user
  who had just made four calls was told to make some calls. Root cause: four
  different functions named `log_routing_decision` write to four destinations.
- **Session snapshots asserted zeros they had not measured** (#56). Downstream
  of the above; `accuracy: 1.0` derived from zero samples is what made the files
  look real. Facts now carry a `measured` flag.
- **`--help` crashed or was ignored on three console scripts** (#51, #52).
  `onboard` and `quickstart` ran their interactive flows and died on `EOFError`
  under a non-TTY stdin. `--help` is now handled first, before any other work,
  and is verified to open no socket.
- **The Textual install hint named a package that does not exist** (#47).
  `pip install llm_router[tui]` was wrong three ways; now
  `pipx inject llm-routing textual`.
- **`set-enforce` changed every running session on the machine** (#49). It is
  now scoped to the session that ran it (`--global` restores the old
  behaviour), and the messages describe what actually happens.
- **"routed" could mean a hint rather than an execution** (#53). Reserved for
  real executions; hint counts say "classified" and name their source.
- **The enforcement block demanded an attribution the agent could not honestly
  give** (#54). The route-indicator line is now offered only when the routed
  answer is what the user actually receives, and the "violations are logged and
  escalated" language is gone — nothing was escalated.

### Added

- **DIRECT-execution timeouts are surfaced** (#57). They previously failed
  silently to a debug log while routing fell through to Claude, so a local path
  that never once succeeded looked like nothing was wrong. `doctor` now
  distinguishes "raise `LLM_ROUTER_OLLAMA_TIMEOUT` to N" from "this machine is
  too slow for local routing".

## [13.0.3] — The MCP command that could never resolve (2026-08-26)

Fixes GH#41 and GH#43, and four defects found while reproducing GH#42.

### The entry point (#41)

- **Every MCP registration named a command that does not exist.** `[project.scripts]`
  declares only the hyphenated `llm-router`, so `shutil.which("llm_router")` returned
  `None` on *every* install type — pipx, pip and uv alike. Thirty call sites depended on
  it. The Claude Desktop and claw-code paths fell back to the literal string
  `"llm_router"`; the main registration fell back to
  `uv run --directory <site-packages>`, which is what produced `CONNECTION_CLOSED` on a
  clean pipx install of 13.0.2. Every pull integration — VS Code, Cursor, Windsurf, Kimi,
  Gemini CLI, Copilot CLI, OpenCode, OpenClaw, Trae, Pi, Codex — was registered dead the
  same way, along with three configs committed in this repo.

- **`doctor` reported 0 issues while the server was dead.** Every MCP check asked only
  whether the key `llm_router` was present in `mcpServers`; none read the command back.
  It now verifies the registered command exists, is executable, and — for
  `uv run --directory DIR` — that `DIR` is a real project root.

- **Both IDE config templates were invalid JSON.** `localize()` rewrites tool names to the
  1.0 surface (`llm_code` → `llm(task="code")`) and was running that substitution over a
  raw JSON document, injecting unescaped quotes into the `"description"` string. Since the
  templates are written verbatim, `install --ide` produced `.vscode/mcp.json` and
  `.windsurf/mcp.json` that no IDE could parse. Both are now built with `json.dumps`.

### The security disclosure (#43)

- **`LLM_ROUTER_DIRECT_EXECUTION` is now documented in README.md.** #36 was closed by
  writing it up in `SECURITY.md`, which the sdist excludes — so for anyone installing from
  PyPI the entire disclosure was invisible: a default-on feature handing a local model
  `write_file`/`edit_file`/`run_command` unsupervised, with no shipped text naming it or
  its off switch. A test now asserts the disclosure survives into the built artifacts.

### Install/uninstall symmetry (#42)

The two behaviours reported already had fixes in the 13.0.2 tag, and the published sdist
is byte-identical to that tag, so the report could not be reproduced as written. Asserting
the real contract — install then uninstall is a no-op on every config file — surfaced four
genuine defects instead:

- **An unguarded `unlink()` aborted uninstall partway through.** One `OSError` in the
  hook-removal loop (or on the rules file) raised straight out of `uninstall()`, so the
  statusLine restore and Claude Desktop deregistration — both later in the function —
  silently never ran. This is the most likely root cause of #42.
- **An empty `hooks` scaffold was left behind** for users who had no `hooks` section.
- **`~/.claude.json` was left as a `{"mcpServers": {}}` husk.** It is now recorded as
  `created_file` and removed only when install is the sole reason the file exists.
- **`settings.json.bak` held POST-install state**, so restoring it reinstated the very
  hooks a user was trying to remove. The snapshot is now taken before the first mutation.

### Packaging

- **`_quarantined_tests/` no longer ships to PyPI.** The exclude list anchored `/tests/`;
  the quarantine lives at the repo root, so 13.0.2 shipped 16 files of dead test code.

### Fixed

- **`KIMI.md` gained a duplicate routing block on every install.** The idempotence guard
  tested for `llm_router`, a token the block it writes never contains.

## [13.0.2] — Document what `LLM_ROUTER_DIRECT_EXECUTION` actually grants (2026-08-19)

Documentation and a test. No behaviour change.

### Security documentation

- **SECURITY.md now covers `LLM_ROUTER_DIRECT_EXECUTION`** (#36). It is **default-on**,
  and with it enabled the routing hook runs a tool-calling agent loop that hands a local,
  uncurated model `write_file`, `edit_file` and `run_command` — the last as an arbitrary
  shell string via `subprocess.run(..., shell=True)` — unsupervised, no confirmation, up
  to 15 iterations, before Claude ever sees the prompt.

  The section states what is enforced (file operations are confined to the project root)
  and what is not, with the blocklist coverage **measured against 13.0.1**: of twelve
  representative commands, **three** are blocked. `rm -rf ./src`,
  `git push --force`, `cat ../../.ssh/id_rsa`, `curl -d @.env` and
  `echo $OPENAI_API_KEY` all pass. The filter stops catastrophic *system* damage; it does
  not stop project damage, credential disclosure, or exfiltration.

  It also records that `agent_loop.py`'s docstring — *"All file operations are sandboxed
  to the project directory"* — is true of the file tools and false in effect, because
  `run_command` runs a shell string and `cat ../../.ssh/id_rsa` is not a "file operation"
  the sandbox sees.

  The entry documents the current state rather than changing it. Whether this should
  default to on is a real question and the section says so, but a default change needs
  its own decision and its own release note.

### Added

- A test re-derives the whole coverage table from the live regex. A table of
  measurements in a document rots silently — nothing fails, the file still reads
  plausibly, and the figure becomes a claim nobody re-checked. It now fails if the
  blocklist widens (so the doc stops understating its protection) or narrows.

## [13.0.1] — 13.0.0 shipped without `llm_router.agents` (2026-08-19)

**13.0.0 cannot start.** `import llm_router.server` fails with
`No module named 'llm_router.agents'`, so the MCP server exits before registering a
single tool and the client reports `CONNECTION_CLOSED` — indistinguishable from a network
fault, which is the same diagnostic dead end as #37.

### Fixed

- **Two unanchored exclusion patterns, in two different files.** Without a leading slash,
  both `.gitignore` and hatch's sdist `exclude` match a directory of that name at ANY
  depth:

  - `.gitignore`'s `agents/` and `Library/` also matched `src/llm_router/agents/` and
    `src/llm_router/library/`, so 11 source files existed locally and were never
    committed. Caught by CI, fixed before 13.0.0 was tagged.
  - `pyproject.toml`'s sdist `exclude = ["agents/", …]` also matched
    `src/llm_router/agents/`. **This one shipped.** `uv build` builds the wheel FROM the
    sdist, so the published wheel was missing the package as well.

  Both are now anchored, and a comment in each says nothing in that block may exclude
  anything under `src/`.

### Why every check passed

A local `uv build --wheel` builds straight from source and included the files, so the
wheel on this machine was correct while the published one was not. The pre-release suite,
the linters, the identity gate and CI all ran against the source tree, where nothing was
missing.

The only step that distinguishes "the release workflow succeeded" from "the artifact
works" is installing the published artifact and importing it. That is now the last step
of the release, not an optional afterthought.

## [13.0.0] — Upstream core sync: the routing engine, its guards, and four security fixes (2026-08-19)

The package is now built from the upstream routing core, rebranded, rather than from a
port of selected capabilities. 358 source files and 477 test files, ~4,956 identifiers.
`scripts/sync_downstream.py` upstream is the reviewable artifact — the sync is
reproducible, not a one-off copy.

**6,695 tests passing, 0 failing.**

### ⚠️ Breaking

- **`requires-python` is now `>=3.11`** (was `>=3.10`). The synced source uses 3.11-only
  stdlib. Leaving the floor at 3.10 would let a 3.10 user install code that cannot run —
  a silent failure rather than a resolver error.
- **`llm_router.audit_routing` is a different module.** It is now the live per-turn
  compliance log. The post-hoc misroute **scorer** that used to live there —
  `run_audit`, `score_decision`, `sample_unaudited_decisions`, `AuditedDecision` — moved
  to **`llm_router.misroute_audit`**, unchanged. Both features exist; they simply stop
  sharing a name. Update imports.

  The two shared a path across the repositories, with disjoint APIs, and a file-level
  copy in either direction would have deleted one of them in silence: no merge conflict,
  no import error, no failing test. Renaming is what makes that impossible.
- **`llm_router.observability` is a package, not a module.** Its OpenTelemetry layer is
  at `llm_router.observability.core` and re-exported from the package, so
  `observability.is_enabled()` and friends keep working. Direct imports of
  `llm_router.summary` and `llm_router.surface_status` are now
  `llm_router.observability.summary` / `.surface_status`.

### Security

- **The persistence redactor never shipped.** `persist_redact` lived under `enterprise/`,
  which is excluded from public distributions, and five write paths — result cache,
  semantic cache, idempotency, context, session store — imported it inside a `try/except`
  that fell back to a scrubber carrying none of its patterns. Measured against the
  published upstream package: JWTs, Slack tokens, emails, SSNs, phone numbers,
  credit-card numbers and prose secrets all reached disk verbatim, 7 of 7.

  The upstream suite was green throughout, including tests asserting exactly that those
  secrets never reach disk — they passed because the development tree *has* `enterprise/`.
  The control was only ever exercised in its strongest configuration. Fixed upstream and
  carried here.
- **SEC-002 layer 2 — path confinement — has landed.** `llm_fs_*` tools now reject paths
  resolving outside `project_root`. 12.0.1 shipped only the opt-in gate.
- **An optional tool group was a load-bearing import of the MCP server.** A build
  excluding `agoragentic` could not import at all — `ModuleNotFoundError` before a single
  tool registered, surfacing as `CONNECTION_CLOSED`, indistinguishable from a network
  fault. Same shape as the mcp 2.0 breakage in #37.
- **Reordering profiles resolved to no chain.** `SUBSCRIPTION_LOCAL` produced a
  one-model chain containing only the paid seat — no fallback, and the exact inverse of a
  profile whose purpose is preferring the free local bucket. `QUOTA_BALANCED` produced an
  empty chain on the two paths that exist to guarantee a non-empty one.

### Added

Whole subsystems from the upstream core, including the agentic engine, control plane,
policy runtime, semantic classification, quota and budget envelopes, the execution
ledger's realized-savings accounting, and the `misroute_audit` scorer with
`llm-router audit misroute`.

`config/` (model registry, agents, signals) and `scripts/` (the CI guards) are now
synced, so the checks that keep these fixes from regressing ship with the code.

### Not included, by design

`enterprise/`, `admin_api`, `invoice_reconciliation`, `tenant_policy_sidecar` and the
agoragentic marketplace/wallet tools. Tests covering them are skipped with that reason
rather than failing, so a red suite still means something is wrong.

## [12.0.1] — Fix the install breakage introduced by `mcp` 2.0.0 (2026-08-19)

Patch release, shipped alone and ahead of the next feature work, because 12.0.0 cannot be
installed fresh.

### Fixed

- **`mcp` was pinned `>=1.0.0` with no upper bound.** `mcp` 2.0.0 removed
  `mcp.server.fastmcp`, which seven modules here imported, so every fresh
  `pip install` / `pipx install` resolved 2.0.0 and died during import — before a single tool
  was registered. The client surfaced this as `CONNECTION_CLOSED`, which is what a network or
  provider fault also looks like, so there was no way to tell the two apart from the outside.
  The imports are ported to the 2.x API (`MCPServer`, `mcp.server.mcpserver`) and the pin is
  now `mcp>=2.0.0,<3.0.0`. (#37)
- **SECURITY.md claimed "Hooks cannot block core tools."** They can, and have since v13
  enforcement landed; the document contradicted the shipped behaviour. Rewritten to describe
  what the hook actually does. (#35)
- **`agoragentic_*` tools registered unconditionally at server startup.** They are now behind
  an explicit opt-in, so a routing install no longer exposes them by default. (#34)
- **Filesystem tools were ungated.** `llm_fs_*` now requires the same explicit opt-in.

### Added

- Two tests that keep this from recurring, deliberately not one:
  `test_pin_has_an_upper_bound` reads `pyproject.toml`, so a loosened pin fails in CI on the
  commit that loosens it; `test_every_mcp_import_in_src_resolves` parses `src/` for real
  `from mcp…` imports and checks each resolves against the *installed* package, so a partial
  port to a future major fails too. Neither subsumes the other — the first passes against a
  broken environment, the second passes against a dangerously loose pin that happens to
  resolve today.

## [12.0.0] — Chuzom capability wave: execution ledger, quality/fallback split, capability-aware shadow routing, budget envelope, misroute audit + CLI (2026-08-02)

> **Version note:** released as a major bump (v11.0.0 → v12.0.0) to mark the scale of the Chuzom capability wave, even though every routing-affecting behavior ships default-off or shadow-only.

Ports eight capabilities adapted from an internal reference implementation ("Chuzom") into llm-router. Everything here is additive; every new routing-affecting behavior ships default-off or shadow-only, so existing installs behave identically until explicitly opted in.

### Added

- **Execution ledger + session store** (`execution_ledger.py`, `session_store.py`). Append-only SQLite ledger recording every route attempt (`execution_events`) via additive `ALTER TABLE` migrations onto the existing `usage.db`, with realized-savings gating (see below) and route/cost invariants. `session_store.py` adds a durable JSONL session-context log with cross-process advisory locking, TTL/size-triggered compaction, and privacy modes.
- **Quality/fallback split** (`routing_quality.py`, `bounded_operational.py`, `quality_feedback.py`). A schema-v2 route-quality ledger (`routing_quality.py`) with fail-open recording and a `summarize()` that never conflates verified/unverified or legacy-v1 rows into v2 metrics. `bounded_operational.py` adds a bounded-operational route predicate and pricing-derived budget, gated by `LLM_ROUTER_BOUNDED_OPERATIONAL` (default off). `quality_feedback.py` gains LoopHole ground-truth verdict ingestion (`record_loophole_verdict`/`ingest_loophole_jsonl`) feeding the existing heuristic quality store.
- **Realized-savings measurement + dashboard split** (`dashboard_data.py::query_realized_savings`, `dashboard/server.py`). Realization-gated savings accounting alongside the existing potential-savings columns: only attempts with `realization_status == "verified_used"` and an adoption method that counts as realized are counted, and the figure is never reconciled against or allowed to overwrite `usage.saved_usd`. Exposed additively as `/api/stats`'s `realized_savings` key, isolated in its own fail-open block.
- **Capability-aware routing (shadow mode)** (`capabilities.py`, wired into `router.py`/`cost.py`). An 8-bit capability detector records what capability-aware routing *would* choose into `routing_decisions.capabilities_json`, without changing any live routing decision. Gated by `LLM_ROUTER_CAPABILITY_ROUTING` (default off); live routing (`needs_claude_tools()`) stays byte-identical regardless of the flag.
- **Budget envelope** (`budget_envelope.py`). Standalone `BudgetEnvelopeManager` (register/reserve/release/commit/settle/tier-state, hierarchical ancestor accounting) gated by `LLM_ROUTER_BUDGET_ENVELOPE` (default off). Ships as an accounting primitive only — no router/cost wiring — so routing and spend behavior are unchanged with the flag off; `execution_ledger.py` remains the sole source of truth for realized spend.
- **Misroute audit** (`audit_routing.py`). A fully offline, post-hoc scorer over existing `routing_decisions` rows (heuristic over judge score / complexity downgrades / downshifts), writing back new `audit_verdict`/`audit_checked_at` columns idempotently. Gated by `LLM_ROUTER_AUDIT_DISABLED`; inert until explicitly invoked via `run_audit()` or the new `llm-router audit` CLI command (below).
- **Retrospective loop + team report enrichment**. Verified the existing retrospective debrief (`retrospective.py`, native since v6.x) reads the new `audit_verdict` directly rather than re-deriving misroutes, and fails open on the context fields it reads from the items above. `commands/team.py`'s report/push surfaces gain fleet-wide realized-savings and inferred misroute-rate columns, sourced from the realized-savings query and quality-ledger summary via a fail-open helper.

- **`llm-router audit` CLI command** (`commands/audit.py`). Wires `audit_routing.py::run_audit()` to a CLI entry point (mirrors the `team` command's structure): renders sampled/audited counts, verdict breakdown, and the inferred misroute-rate baseline, with a `--json` mode and a `--limit N` flag (default 100). Respects `LLM_ROUTER_AUDIT_DISABLED`. Strictly read-only/reporting — never mutates routing state.
- **Bounded-operational routing wired into the live path** (`router.py`), strictly behind `LLM_ROUTER_BOUNDED_OPERATIONAL` (default off). When the flag is unset/false, the routing decision path is byte-identical to before, proved by an invariance test comparing route decisions with the module absent vs. present-but-disabled.

### Config

New env vars (all optional, all default off / non-disabling): `LLM_ROUTER_BOUNDED_OPERATIONAL`, `LLM_ROUTER_LOOPHOLE_JSONL`, `LLM_ROUTER_CAPABILITY_ROUTING`, `LLM_ROUTER_BUDGET_ENVELOPE`, `LLM_ROUTER_AUDIT_DISABLED` (opt-*out* — unset means audits run when explicitly invoked).

### Notes

- There is no `LLM_ROUTER_QUALITY_FEEDBACK` flag, and none is planned. The heuristic quality scorer's `should_skip_model()` check in the router's fallback-chain path is unconditional — it pre-dates this release, is unrelated to the LoopHole-verdict additions above, and is already always-on in production. Gating it now would change existing behavior, so it intentionally stays ungated; this note exists only to correct an earlier reference to a flag that was never implemented.

## [11.0.0] — Adaptive routing wave: observability, importable classifier, subscription-local profile, PII→local (2026-07-09)

A wave of routing and observability capabilities, plus a docs restructure. Everything new is additive and off-by-default where it touches routing, so existing setups behave identically until opted in.

### Added

- **Cross-surface status indicator** (`llm_router.observability.surface_status`). A stdlib-only, fail-soft "router is working" signal for hosts without a native statusline: a compact status line (`⚡ llm-router · 🎯 hermes3:8b code/moderate · $0.03 · ✓`), an OSC terminal title, and a rate-limited OS notification, all derived from the shared savings log. Answers *is it active / what did it last route / is it healthy*.
- **Session-end summary** (`llm_router.observability.summary`). A content model over the existing `usage.db` with `render_markdown()` (CI / Claude Desktop / logs) and a rich `render()` (rich is optional; falls back to markdown): headline savings vs baseline, tier mix, per-provider cost, latency p50/p95/p99, outcomes, and top routes.
- **Importable deterministic classifier** (`llm_router.classify`). The hook's weighted intent×3 + topic×2 + format×1 scorer (`score_categories`, `classify_complexity`) is now an importable module with a `classify_signals() -> ClassifySignal` wrapper, so the router core, gateway, and MCP tools can classify at 0 cost/latency — previously only the UserPromptSubmit hook could. A drift-guard test keeps it byte-identical to the hook.
- **`SUBSCRIPTION_LOCAL` routing profile** (`llm_router.subscription_local_routing` + `RoutingProfile.SUBSCRIPTION_LOCAL`). Cost-inverted routing for the "one paid seat + free bucket" shape: free-first for simple/moderate, seat-first for complex, and the seat demoted to last when its quota is strained. Wired into `chain_builder.build_chain`; a complete no-op unless `LLM_ROUTER_SUBSCRIPTION_PROVIDER` is set. Quota-pressure source is a pluggable hook.
- **PII / secret signal with force-local routing** (`llm_router.signals`). `PiiSignal` detects API keys, tokens, JWTs, private keys, and `.env`-style secrets; `force_local_for_pii(chain, prompt)` filters a chain to local providers when a secret is present and is **fail-closed** (empty chain when no local model exists) so a secret is never dispatched to an external API. Evidence names the matched pattern, never the value.
- **`run_port_tests.sh`** — one-command runner for the new modules' tests.

### Changed

- **Docs restructure.** `docs/` is now gitignored (local working notes) except the CI-generated `docs/BENCHMARKS.md`. README media moved to `assets/readme/`, and public guide pages moved to `guide/` (Getting Started, Providers, Policies, Tools, Architecture, Troubleshooting, …). README and CHANGELOG links updated accordingly. **If you linked to `docs/*.md` externally, update to `guide/*.md`.**
- Version bumped to **11.0.0** to signal the new capability surface and the docs path change.

### Config

New env vars (all optional): `LLM_ROUTER_SUBSCRIPTION_PROVIDER`, `LLM_ROUTER_INTERNAL_PROVIDERS`, `LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD`, `LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES`, `LLM_ROUTER_STATE_DIR`, `LLM_ROUTER_INDICATOR`.

### Follow-ups (not yet wired)

Call `force_local_for_pii` in the dispatch path; wire `get_subscription_pressure` to a live quota source; repoint `hooks/auto-route.py` to import `classify.py` (removing its duplicate definitions).
