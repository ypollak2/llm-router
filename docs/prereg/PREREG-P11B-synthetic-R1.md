# PREREG-P11B-synthetic-R1: P1.1-b Sonnet stratum after repair 1 (real per-model windows)

This file is frozen by the hash in `PREREG-P11B-synthetic-R1.sha256`. It was frozen before the
proof below was run and before the code it tests was merged. It does not change
`PREREG-P11B-synthetic.md` (sha256 `231fe6a4…`) or AMEND1 (`f8d97895…`).

## Owner decisions (in session, 2026-10-15)

* **D-101: repair 1 = real per-model windows.** A cloud door builds the context pack for the model's
  registry context window, capped at 65,536 tokens (`context_pack.CLOUD_WINDOW_CAP`). Before, it
  used a flat 8,192 (MCP/SDK) or 32,768 (gateway). This choice followed a $0 check: a local-model
  summary alone could not pass, because the summary worker shows the model only the first 600
  characters of each tool result.
* **D-102: re-score the Sonnet stratum by a $0 equivalence proof, with no model calls.** At the real
  window every item the plan allows (FULL ≤ 40k tokens, R3) fits whole, so PACK and FULL send the
  same conversation. A model run would only measure Sonnet's sampling noise. The real fidelity
  test moves to the local stratum (PACK vs HOOK6), which still needs a local model.

## Question

At the Sonnet doors' window after repair 1, does PACK carry exactly what FULL carries for every one
of the 60 frozen P1.1-b items? The items are `scripts/g1_synthetic_evidence.py items()`, seed
20261014, unchanged.

## Procedure

`scripts/g1_synthetic_evidence.py equivalence`, for each item and each Sonnet-reachable door that
takes a caller conversation (`gateway`, `sdk`):

1. `window = router._pack_window("anthropic/claude-sonnet-5-5", door)`.
   (`sdk._pack_window` gives the same value for the SDK's chain entries.)
2. `pack = build_pack(item.request, messages=<the item's messages>, door=door,
   target_window=window, target_provider="anthropic", project="")`.
3. The item is **equivalent** when all of these hold:
   * (a) `pack.mode == "full"`;
   * (b) `pack.summary is None`;
   * (c) `[m["content"] for m in pack.recent]` equals, in order, the FULL arm's per-message texts
     (`context_pack.extract_turn_text(content, keep_tools=True, cap=False).strip()` for every
     message with text);
   * (d) the item's answer token appears in the pack.

## Verdict

* **PASS by construction:** 60/60 items equivalent at both doors. Then PACK and FULL send the
  same conversation, so (worse - better)/n = 0 in expectation, and any observed difference would
  be sampling noise.
* **NOT INFORMATIVE:** any item not equivalent at either door. A model-scored run would then be
  needed, under a new prereg. This proof never prints FAIL.

The output is `docs/measurements/p11b-synthetic-sonnet-r1.json`, labelled `synthetic (D-99)`. It
records the per-door window, the per-item flags and the code SHA. It contains no item text.

## Limits

* The proof covers sessions up to the plan's 40k-token FULL cap. Above ~52k tokens (0.8 x 65,536)
  the pack still falls back to the last 7 messages plus the extractive summary. The original
  stratum showed that path loses early tool output and early decisions (0/20).
* The MCP door uses the same window rule, but packs from the session store rather than a caller
  conversation, so it is not driven here.
