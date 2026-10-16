# PREREG-P17D-synthetic AMEND1: the nimble:9b artifact per amend2-errata1 (D-117)

This file is frozen only when `PREREG-P17D-synthetic-AMEND1.sha256` exists beside it and
`scripts/check_prereg.py` reports a match; until then it is a draft. It amends
`PREREG-P17D-synthetic.md` (sha256 `6454d61d…c6b8c5`, frozen 2026-10-15T16:32:58Z), which stays
byte-identical. It governs the P1.7-d synthetic run in `$PP/v16/p17d` from its first shadow row on.
Authority: D-117 (coordinator, 2026-10-16T06:35:03Z).

## 1. Contradiction (first)

* **What the frozen prereg says.** §4 "Warm model": `ollama show nimble:9b --modelfile` names blob
  `sha256-bbf1d6fc…`, size 9,527,501,312 B (amend2); pin `NIMBLE_BLOB`.
* **Why it cannot match this host.** That pin was copied from `eval/PREREG-v2-amend2.md` §3. On
  2026-10-07 `eval/PREREG-v2-amend2-errata1.md` (sha256 `82a6b4cf…de21e6`) superseded exactly
  those rows: under Ollama 0.40.0, `ollama pull nimble:9b` stores the MLX build, and
  `ollama show --modelfile` prints `FROM nimble:9b`, not a blob path. The P17D prereg cited amend2
  and missed the erratum.
* **Evidence (read-only, this host, 2026-10-16T06:40Z).** `ollama --version` and `/api/version`:
  0.40.0. `ollama show nimble:9b --modelfile`: `FROM nimble:9b`. No file
  `~/.ollama/models/blobs/sha256-bbf1d6fc…` exists. The stored manifest-list blob
  `sha256:726d5c99…` names two builds, MLX `13ae0a50…` and GGUF `9b953de7…`; the stored manifest
  is the MLX one (§2).
* **What happened.** The harness at `7e20664d` refused `shadow` at 2026-10-16T06:33Z ("nimble:9b
  blob digest differs from the pin (not a result)", exit 2). The digest check runs before the quiet
  gate and the warm-up, so no nimble call was made and no shadow row, `started` or `terminal` line
  was written. Nothing was consumed. This is a defect of the prereg, not an outcome.

## 2. The nimble artifact and its check (errata1 §2, adopted unchanged)

This is the artifact M1.8 round 3 and M1.9 (amend5) measured. The GGUF Q8_0 build is not in the
field (errata1 §2).

| Field (errata1 §2) | Value | This host, 2026-10-16 |
|---|---|---|
| Tag | `nimble:9b` as pulled by Ollama 0.40.0 | 0.40.0 |
| Local manifest | `~/.ollama/models/manifests/registry.ollama.ai/library/nimble/9b`, 92,938 B, sha256 `dfed4f707396966698374b7ffe21bd4586271b772551a02f0114e5c79fd24a21` | 92,938 B, sha256 equal, mtime 2026-10-07 19:11 local |
| Config blob | `sha256:770ba8d48b0527d2a5f1128031941be701599766d3477ca8cd6a13d99995f74f` (safetensors, qwen3_5, 9.0B, mxfp8) | blob sha256 equal |
| Layers | 437 entries, sum 9,267,659,870 B | 437, sum equal, 0 blobs missing, 0 size mismatches |
| `ollama list` ID | `13ae0a506102` | `13ae0a506102`, 9.3 GB |
| Runner / quantisation | mlx, mxfp8 | `/api/show` details: format safetensors, quantization_level mxfp8, runner mlx |
| Params layer `sha256:fc258d6a…` | `{"num_ctx":8192,"temperature":0}` | equal |

**The check** (replaces the §4 "nimble digest" bullet; it is errata1's digest check as
`scripts/m18_r3.py` `preflight` ran it for round 3). Before the quiet gate, on every `shadow`
invocation:

1. sha256 of the local manifest file equals `dfed4f70…4a21`;
2. the `ollama list` rows named `nimble:9b` have exactly the ID list `[13ae0a506102]`;
3. `/api/show` `{"model":"nimble:9b"}` reports `details` (format, quantization_level, runner) =
   (safetensors, mxfp8, mlx).

A mismatch, or a value that cannot be read, refuses: not a result, nothing written. The other
table rows are descriptive and not checked, as in errata1.

**Erratum text vs this host (descriptive rows only, none is checked).** Errata1 calls the system
layer `sha256:5ae11c8d…` "the same 385-byte text as in amendment 2"; on this host the blob is
383 B (amend2's system layer was a different blob, `47bec446…`, 385 B). The config JSON on disk
has two keys errata1 does not quote (`model_families`, `capabilities_explicit`); its sha256 equals
the pinned digest. Errata1 names the layer types of 436 of its 437 entries; the 437th is the
manifest entry `sha256:13ae0a50…`, 92,618 B.

`NIMBLE_BLOB` stays in the frozen pins block as the record of the superseded pin. The harness no
longer checks it. Tag, request, timeout, keep-alive, retries, environment, quiet gate and warm-up
are unchanged.

## 3. The harness line

The fix needs a new harness commit. Before the first shadow row a new line
`harness <UTC> sha=<new HARNESS_SHA>` is **appended** to `use.log`; the `7e20664d` line stays as
history (append-only). The new SHA is logged in PROGRESS.md as §4 requires.

* The governing line is the **last** `harness` line of the run of `harness` lines that opens
  `use.log`, before the first line of any other kind. Its SHA must prefix HEAD.
* A `harness` line after any other `use.log` line, or dated after the earliest row of
  `classifier_shadow.jsonl`, refuses.
* `shadow` records the governing line in `shadow_meta.json` at its first preflight. Later `shadow`,
  `label` and `score` runs refuse if the governing line differs from that record.

## 4. The select output stands

`select` ran once under `7e20664d` (admitted, n = 151: F 32, G 119, 25 sessions); `selection.json`
is 0444. AMEND1 changes no code that `select` runs (`git diff 7e20664d -- scripts/p17d_synthetic.py`
shows no change in `select`, `walk`, `share_cap`, `seed_order`, `load_items`, `build_body`,
`check_assemble` or `cmd_select`; the shared `check_common` gains only the AMEND1 freeze check).
So `select` is **not** re-run (§10: no re-selection). Instead:

* `items.jsonl` and `attach_counts.json` stay checked against their pins, as before;
* before the first shadow row, `shadow` re-derives the selection from the pinned `items.jsonl` with
  the unchanged `select` and refuses unless `ids`, `f_ids`, `g_ids`, `admitted` and `n` equal
  `selection.json`;
* `shadow` records the sha256 of `selection.json` in `shadow_meta.json`. `score` refuses, before
  its `started` line, if the file's sha256 differs from that record.

## 5. Disclosure

The scored `result.json` and `docs/measurements/p17d-synthetic.json` carry this file's sha256 and
`frozen_utc` and a disclosure naming the superseded GGUF pin, the errata1 identity, the 06:33Z
refusal before any row, and select not re-run. The scored `result.json` also lists every `harness`
line of `use.log` and the `selection.json` sha256. A NOT INFORMATIVE gate file carries the same
disclosure and this file's sha256. Shadow rows dated before this file's `frozen_utc` count as
pre-freeze rows, like rows before the prereg's.

## 6. Nothing else changes

Items, selection, arms, labels, calibration, statistics, bars, spend cap, single-use rules and
outputs are as frozen in `PREREG-P17D-synthetic.md`.
