# PREREG-P11B-synthetic: context-pack fidelity on synthetic sessions (PLAN v16 P1.1-b)

Status: frozen by the hash in `PREREG-P11B-synthetic.sha256`. Changing any byte of this file
changes the hash; a changed file is a new prereg with a new, later `frozen_utc`. Nothing below
reports a result, and no item had been sent to a model when it was frozen.

## Authority

Owner decision **D-99** (2026-10-14, in session): "The G1 evidence can be done synthetically by the
machine." This replaces, for G1 only, D-42's "P1.1-b fidelity stays organic-only". Every number this
prereg produces is labelled **synthetic (D-99)** and is never mixed into organic counts.

## Question

P1.1-b: does the context pack lose answer-relevant context compared with the reference context?

* **Sonnet stratum:** PACK vs FULL on the same model.
* **Local stratum:** PACK vs HOOK6 on the same local model. There is no local FULL arm in v16.0 (R3).

Bar, per stratum: `(worse - better) / n <= 0.02`, where `worse` counts items PACK fails and the
reference arm passes, and `better` counts the reverse. The exact McNemar p (two-sided binomial on the
discordant pairs, p = 0.5) is printed and does not gate.

## Items

`scripts/g1_synthetic_evidence.py fidelity` generates them deterministically from `SEED = 20261014`.
All text is invented, and no real prompt is read.

* Each item is one synthetic coding session, written as a Claude Code transcript JSONL. It contains
  typed user requests, assistant text with `tool_use` blocks, and `tool_result` carriers with
  code-like and log-like filler.
* The session is long enough that the whole conversation exceeds 0.8 x the 8,192-token window, so
  `build_pack` is in pack mode. It stays under 40k estimated tokens, so FULL is a valid cloud arm (R3).
* Exactly one **answer token** is planted in each session. It is unique per item and appears nowhere
  else in that item. The final request asks for it.
* **Categories** (where the token sits), n = 60 per stratum:

| Category | Where the token is | Items |
|---|---|---|
| A `recent` | the last tool result, inside the last 7 messages | 30 |
| B `early_user` | the first typed user request, in its first sentence | 10 |
| C `early_tool` | a tool result in the first third of the session | 10 |
| D `early_assistant` | an assistant decision in the first third of the session | 10 |

  The mix weights recency because a follow-up request usually builds on the last turns. This is a
  design choice, not a measured distribution: no organic request-position statistics are available
  to this run. Results are also reported per category, so the owner can reweight them.

## Arms

All arms share one system prompt:

> You are a coding assistant continuing the session below. Answer the final user request using only
> the session. Be brief.

Each arm's context is serialized as `[role]\ntext` blocks.

* **FULL:** every message, tool blocks uncapped (`context_pack.extract_turn_text(keep_tools=True,
  cap=False)`), then the request.
* **PACK:** `context_pack.build_pack(request, door="hook", session_id=<item>, transcript_path=<item>,
  target_window=8192, target_provider=<stratum provider>)`, rendered with `to_messages`.
  * `LLM_ROUTER_ROLLING_SUMMARY=1`, and the stored summary is the P1.2 summary that
    `context_summary.maybe_update` writes for the transcript.
  * The summary is extractive in the Sonnet stratum. In the local stratum it comes from the local model
    when `--summary-model` is given, else it is extractive.
  * There is no project root and `LLM_ROUTER_SESSION_CONTEXT=all`.
* **HOOK6** (local stratum only): the last 6 messages with text, rendered without tool blocks
  (`keep_tools=False`), with no summary. This is the pre-pack hook context.

## Models

* **Sonnet stratum:** `claude -p --model sonnet --safe-mode --tools "" --strict-mcp-config
  --no-session-persistence --output-format json --system-prompt <above>`. The served model id is
  recorded from `modelUsage`.
* **Local stratum:** the Ollama `/api/chat` model named by `--local-model`, with `temperature 0`.

## Grading

Deterministic, with no judge. An answer passes when the item's answer token appears in the answer
after normalization: lowercase, with every character except letters, digits and `-` removed.

## Run rule

* One run per stratum, n = 60 items, items in generator order.
* A harness error (CLI or HTTP failure, timeout, empty output) is retried once and then counted as an
  error. Items with an error in either arm are excluded from both arms and counted.
* More than 10% errored items makes the stratum **NOT INFORMATIVE**.
* No item is regenerated, and no re-run is scored, without a new prereg.
* Spend: the Sonnet stratum is estimated at $5 or less (120 calls). The plan's AMEND3 cap for P1.1-b
  is about $18.

## Output

`docs/measurements/p11b-synthetic-<stratum>.json` records:

* `n`, `errors`, and passes per arm;
* `worse`, `better`, `(worse - better) / n`, the exact McNemar p and the verdict;
* the per-category table, the model id and the prereg sha256;
* the label `synthetic (D-99)`.

The output contains no answer text, only pass/fail per item, with item ids.
