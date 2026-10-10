---
id: TURNFIRST-1
status: fixed in `fix/turn-first-population` (deploy: restart the proxy; rows written before it keep the old label)
---
## TURNFIRST-1. `turn_first` rows were sub-agent calls and notifications, so 95.6% of them classed as code

- **Symptom (2026-10-09).** Live ledger `~/.llm-router/proxy_calls.jsonl`, window 2026-10-08 17:50 to
  2026-10-09 21:05 local, counts only (no text field read): 320 rows labelled `step_class == turn_first`.
  113 of them were classified (`tier_task_type` set), all 113 `claude-sonnet-5-5` requests (111
  `tier_reason == policy`), median `req_bytes` 580,669; 108 of 113 (95.6%) were classed `code`, 5
  `research`. The user's main loop (Opus, `tier_reason == config_pinned`, 129 rows, median `req_bytes`
  2,122,359) was never classified. Every metric read "on turn-first rows" (the P0.9-e decision p95, the
  D-31 Haiku arm's eligibility, the classifier mix) was measured mostly on Sonnet sub-agent calls:
  delegation briefs, task notifications and SendMessage resumes mid-run.
- **Cause.** `proxy/steps.step_kind` looked at the tool list (the `Agent`/`Task` launcher) only on a
  conversation's first call; every later call that was not a tool-result continuation fell through to
  `turn_first`, whatever thread sent it and whatever its newest user turn held. Separately,
  `steps._human_text` stripped only `<system-reminder>...</system-reminder>`, non-greedily: the harness
  tags the repo already lists as non-human (`groundtruth_sources._NOISE_PREFIX`: `<task-notification>`,
  `<command-*>`, `<local-command-*>`, `<agent-message>`, ...; `Caveat:` lines) were read as typed text, so
  a notification turn was classified as a new prompt, and a literal `</system-reminder>` quoted inside a
  reminder (a CLAUDE.md that names the tag) ended the strip early and leaked the rest of the reminder
  into the classifier's text (300 synthetic turns in the test below: the old pattern leaks on more than
  half of them).
- **Fix.** `step_kind` keeps its four values and adds two: `subagent_turn` (no launcher, not the first
  call, not a continuation) and `harness_turn` (main thread, newest user turn only harness messages).
  `turn_first` now means a main-thread human turn; first calls keep their old label. Every proxy row gets
  text-free `is_main_thread`, `is_first_call`, `turn_origin` (`typed | subagent_brief | task_notification
  | command | other_tag`, null for continuations and side calls) and `tier_text_len`
  (`steps.turn_fields`). `_human_text` strips every tag in the shared list (`HARNESS_TAGS`, now exported
  by `groundtruth_sources` and used to build `_NOISE_PREFIX`), counts an open tag at a line start (and a
  `<system-reminder>` mid-line too, as the old pattern did, when it is closed), and ends a block at the
  first close tag alone on its line (falling back to the first close tag only when there is none, for
  one-line blocks). Attributes are single-line and at most 200 characters, and opens and closes are found
  in one pass each and paired by bisection (review of #394). The first version's `\s[^>]*` crossed
  newlines. Body shape: one text block of N line-start `<command-name foo` lines with no `>`, followed
  by 3,000,000 `x`. Before, one run each on the first version: N=100 took 1.94 s, N=1,000 took 19.3 s.
  After, median of n=5 runs with `python3 scripts/bench_harness_strip.py` (2026-10-10, load1 ~1.4):
  N=100 0.70 ms, N=1,000 0.83 ms, N=10,000 1.9 ms, N=150,000 18.5 ms; the worst other shape in the
  script (150,000 uniquely named closed blocks, 9.2 MB) 215 ms. On 30 KB of tag-free text the strip
  takes 0.007 ms against 0.004 ms for the pre-TURNFIRST-1 reminder regex (same script). A turn that is only harness messages is skipped by
  `newest_human_text`, so the tier keeps classifying the prompt before it. Consumers: the Haiku arm
  (`tiers._pinned_or_arm`: `subagent_turn` -> `not_main_thread`, `harness_turn` -> new `harness_turn`),
  `kpi` G1_proxy (both new kinds excluded and counted), `offload_share` / the Haiku guard (`harness_turn`
  still begins a turn, as it did; `subagent_turn` follows the `subagent_first` rule). A main session
  without the `Agent`/`Task` launcher (tool disabled, a restricted `-p` run) now records its later turns
  as `subagent_turn`, and they leave the O3 turn count unless the transcript join puts them on the main
  thread. Labelling is fail-open (NFR-FAIL): if `step_kind` raises, the row says `step_class ==
  unknown`, `step_error: true` (fail-open code `LR-FO-PROXY-STEP-KIND`) and the call is forwarded without
  the classifier, the D-31 arm or the classifier shadow (`ClaudeTierPolicy.decide_unclassified`, in
  `_decide`'s order: a `pinned_models` pin kept, an `opus:` pin served on Opus, a `/model` choice kept,
  else the sticky tier held when the body is accepted on it, else forwarded as sent;
  `tier_detail: step_error`; it reads stickiness and does not write it, the request path's usage and
  tier-retry hooks still may). Only these labels are guarded: other body-shape reads on the request path
  (`has_served_turn`, `session_id_of`) are as before;
  `kpi` G1 counts such rows as `step_error`, O3 counts them with the null-step rows, never as turns. Residual: a reminder
  whose content holds a close tag alone on its own line still ends there.
- **Test.** `tests/proxy/test_turn_population.py`: sub-agent first call vs mid-run SendMessage vs mid-run
  task notification vs main-thread typed / notification / command turns get the right kind, origin and
  flags; every shared tag is stripped; a literal close tag inside a reminder does not leak (fixed case
  and 300 random ones), nor does a close tag at a line start followed by text; a closed reminder that
  starts mid-line is stripped while other tags named inside a typed sentence are kept; the open-tag
  pattern cannot cross a newline or run past 200 attribute characters, and 1,000 adversarial lines on
  3 MB finish under 200 ms (`timing`); an error in `turn_fields` leaves the row's other fields set; a
  `step_kind` that raises still returns 200 with an `unknown` / `step_error` row, no classifier call and no
  arm and no classifier-shadow scheduling (pinned and unpinned), an unlabelled call honours `opus:` and
  `/model` before a sticky tier and keeps a non-Haiku sticky tier; a different tag nested in a reminder
  does not leak the reminder; the tier classifier sees the prompt
  before a notification; every ledger row carries the four fields and no text. Consumer tests updated:
  `tests/test_proxy_step_kind.py`, `tests/test_kpi_command.py` (G1 excluded counts),
  `tests/test_proxy_haiku_arm.py` (notification turn -> `harness_turn`).
- **Gates to recompute after deploy, on rows written after it:** P0.9-e turn-first decision p95,
  the D-31 Haiku arm counts by `tier_arm_reason`, the classifier mix on turn-first rows, and O3 turns
  (unjoined sub-agent calls leave the turn count). A window that spans the deploy mixes both labels.
