# PREREG-P11B-synthetic-R2: P1.1-b Sonnet stratum after repair 1, at the MCP door (supersedes R1)

This file is frozen by the hash in `PREREG-P11B-synthetic-R2.sha256`. It was frozen before the code
for its procedure was written and before the procedure was run. It supersedes
`PREREG-P11B-synthetic-R1.md` (sha256 `203460e0…`) for the D-102 re-score. R1 stays in the tree as a
record.

## Why R1 is superseded

The independent review of PR #514 found two problems with R1:

1. **R1's proof is uninformative.** It drove the gateway and SDK doors with
   `anthropic/claude-sonnet-5-5`. Before D-101 both doors already used 32,768 for a cloud model: the
   gateway through `_GATEWAY_NON_LOCAL_WINDOW`, the SDK through `_local_num_ctx()`'s default. The
   60 items (~16k tokens each) were already sent whole there, so R1's 120/120 holds without the
   repair. The original FAIL setup (8,192) matches the **MCP** door, which R1 excluded.
2. **The router routes Sonnet as `anthropic/claude-sonnet-4-6`.** That id is not in
   `config/models.yaml`, so it kept 8,192 under the first D-101 code. Code commit 2 adds a family
   fallback (the smallest registry window among the model's family, the `model_aliases` rule).

## Question

At the MCP door, for the Sonnet id the router actually routes to, does PACK carry exactly what FULL
carries for every one of the 60 frozen items? And is the repair what changes the answer?

## Procedure (`scripts/g1_synthetic_evidence.py equivalence`, replacing R1's)

* `model = model_aliases.LATEST_CLAUDE["anthropic/claude-sonnet"]`, the routed Sonnet.
* `new = router._pack_window(model, "mcp")`.
* `old = router._MCP_NON_LOCAL_WINDOW`. This is 8,192, the pre-D-101 MCP window for every cloud
  model.
* For each item, the transcript is written to a scratch file, then
  `build_pack(item.request, door="mcp", transcript_path=<file>, session_id=item.item_id,
  target_window=W, target_provider="anthropic", project="")` is built for `W` in (`new`, `old`).
* An item is **equivalent at W** when all four checks hold:
  * (a) full mode;
  * (b) no summary;
  * (c) the pack's message texts equal, in order, the FULL arm's per-message texts;
  * (d) the answer token is present.

## Verdict

* **PASS by construction (repair-attributable):** `new` > `old`, 60/60 items equivalent at `new`,
  and 0/60 equivalent at `old`.
* **NOT INFORMATIVE:** anything else. This proof never prints FAIL.

## Output

`docs/measurements/p11b-synthetic-sonnet-r2.json`, labelled `synthetic (D-99)`. It records:

* the model, both windows and the per-item flags at each window;
* the git SHA;
* whether `src/`, `scripts/` or `config/models.yaml` were dirty;
* the registry file that was loaded.

It contains no item text.

## Limits (stated, not tested here)

* Sessions over ~52k tokens (0.8 x 65,536) still fall back to the last 7 messages plus the
  extractive summary. That path lost early tool output and early decisions in the original stratum.
* The claude-CLI dispatch path (`_cli_prompt_with_context`) is given a bare provider name, so it
  keeps 32,768.
* A wheel install has no `config/models.yaml` and uses the bundled registry, so most current models
  keep the old windows there.
* An SDK chain that starts with a cloud model sizes the pack for that model; a local fallback in the
  same chain trims it (`check_overflow`).
