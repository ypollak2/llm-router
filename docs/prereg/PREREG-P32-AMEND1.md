# PREREG-P32 AMEND1: re-pin ITEM_SHA256 for scripts/g1_synthetic_evidence.py after F45 (PR #582)

Status: **DRAFT, not frozen.** This file is frozen only when `PREREG-P32-AMEND1.sha256` exists beside it, an
independent freeze-guard review has passed (D-122: an author-written sidecar is void) and
`scripts/check_prereg.py docs/prereg/PREREG-P32-AMEND1.md` reports a match. It amends `PREREG-P32.md`
(sha256 `d2e175aa6dc776635b01659811266717d8759bf7923ae20426a83569a5585fa3`, frozen_utc
2026-10-17T23:13:17Z), which stays byte-identical, as do its sidecars (`docs/prereg/PREREG-P32.sha256`,
`release_evals/PREREG.sha256`), `release_evals/PIN.json` and `release_evals/manifests/*`. No model has been
called and no release eval has been run for this amendment.

## Authority

Owner decision, 2026-10-27 (AskUserQuestion, option "AMEND: re-pin hash"): dispose of the item-pin drift with an
amendment that re-pins the hash. The gate (`eval_gate.py`) stays unchanged; draft PR #754 (a gate item-pin check)
is closed.

## Deviation and evidence (counts and hashes only)

* **Pin.** `PREREG-P32.md` (P32-CONSTANTS) pins
  `ITEM_SHA256_scripts/g1_synthetic_evidence.py=6fe250a707c9a7aaa2a52735a0c363e1ad3c5eb5905c357b0c8bd7f5fb58344c`.
  That is the file's hash at REPO_SHA `e96f8a076811f88045cf3a3320f0e4b23c564eb4` (recomputed with
  `git show e96f8a07:scripts/g1_synthetic_evidence.py | shasum -a 256`).
* **Drift.** On main at `d68a816488f2ed3c23e7cc43f223f142840b5265` the file hashes to
  `b0d2e138973e66b58f54e1883b8801883f2e0384f74313131debe31b9964f850`. One commit changed it after the freeze:
  `5ac322cfc77ee1b9c93b81938ef5edfda91dd641` ("F45: g1 synthetic-evidence test is deterministic and leaves no
  processes", PR #582). It is the only commit to the file between REPO_SHA and main
  (`git log -- scripts/g1_synthetic_evidence.py`: 3 commits in total, 70e556f2, 97ce4183, 5ac322cf).
* **Size of the change.** 80 insertions, 22 deletions, in 5 hunks, all at lines 451-540 of the new file: new
  `_gone` (9 lines) and `reap_children` (26 lines), and the body of `run_summary`. Nothing above line 451 changed,
  and the module imports (`contextlib`, `time`, `os`) were already present.
* **Reach into the release eval.** The release eval reads this file only through
  `scripts/release/run_p32.py` `load_g1` (run_p32.py:134-145, :278-290), which calls `items()`, `scratch_env()`,
  `write_transcript()`, `pack_context()` and `full_context()`. These are defined at lines 145, 315, 191, 177 and
  162, all before the first changed line, and none of them calls `run_summary`, `_gone` or `reap_children`
  (`grep` on those three names finds them only inside lines 451-535 and the `__main__` call at line 975).
  `run_summary` computes P1.2-a (summary coverage), which is not one of the four P32 sets.
* **Verdict on outputs.** The change cannot alter `fid_syn_60` items, the generator seed (20261014), the FULL or
  PACK contexts, or any pass/pack/full value. Therefore no item id, label or manifest changes, and the four
  `MANIFEST_SHA256_*` pins stand.
* **Companion change not under any item pin.** The same commit made `_spawn_detached` in
  `src/llm_router/statusline_tick.py` return a pid (7 insertions, 4 deletions). That file is not an ITEM_SHA256
  file and is part of the candidate code under test as before; it is on the summary-spawn path only.
* **Other pins recomputed on main, all unchanged:**
  `ITEM_SHA256_scripts/release/run_p32.py=161ba03859d9904df6ede396a51baf481825129e6b9b28e2fce3df5a83a6db17`,
  `ITEM_SHA256_tests/fixtures/agt/a1_prompts_50.jsonl=2e7cd0f9dab2c2535bf289825bd6737607c2390b7fd68d9f1c925820bd559cc6`, and the four `MANIFEST_SHA256_*` match
  `release_evals/manifests/*` (command of §10 step 2) and `release_evals/PIN.json`.
* **Baseline record.** `release_evals/baseline.json` (v15.4.0 baseline, PR #577) records
  `provenance.item_file_sha256["scripts/g1_synthetic_evidence.py"]` as the old `6fe250a707c9a7aaa2a52735a0c363e1ad3c5eb5905c357b0c8bd7f5fb58344c`. The baseline holds the
  two performance sets only, which do not read this file. That record is history and is not changed.

## What changes

1. In the `P32-CONSTANTS` block of `PREREG-P32.md`, the line
   `ITEM_SHA256_scripts/g1_synthetic_evidence.py=6fe250a707c9a7aaa2a52735a0c363e1ad3c5eb5905c357b0c8bd7f5fb58344c`
   is replaced, for every use of the pin, by
   `ITEM_SHA256_scripts/g1_synthetic_evidence.py=b0d2e138973e66b58f54e1883b8801883f2e0384f74313131debe31b9964f850`.
2. The §10 step 1 requirement "at the RC the three ITEM_SHA256 files must be byte-identical to REPO_SHA" now means:
   `scripts/release/run_p32.py` and `tests/fixtures/agt/a1_prompts_50.jsonl` byte-identical to REPO_SHA as before,
   and `scripts/g1_synthetic_evidence.py` byte-identical to the re-pinned hash above (the file as of commit
   `5ac322cf`). Any further byte change to any of the three files is a new prereg (§10 step 9).
3. The §5 coordinator comparison of `provenance.item_file_sha256` against the ITEM_SHA256_* constants uses the
   re-pinned value for the candidate file. For `baseline.json` the expected value for that one file is the
   original `6fe250a707c9a7aaa2a52735a0c363e1ad3c5eb5905c357b0c8bd7f5fb58344c` (the baseline was recorded before F45); the other two entries must equal their pins.

## What stands

* `REPO_SHA`, `BASELINE_SHA`, every threshold and constant (`DROP_BLOCK_PTS`, `FIDELITY_MAX`, `STABILITY_MIN`, the
  n and min_n values, seeds, `FID_WINDOW`, `STAB_GEN_TEXTS_SHA256`), the four manifest hashes, the four sets,
  the decision rules and the gate code.
* §5's statement that item-file hashes are "recorded, not gated": the gate compares no item-file hash, and this
  amendment adds none. The check stays a coordinator procedure.
* §13 and §9: no label edits, no fixture added, removed or reworded after the freeze; no skip or override.

## Coordinator check at the RC (replaces the item-pin part of §5 and §10 step 1)

1. `scripts/check_prereg.py docs/prereg/PREREG-P32.md` and `scripts/check_prereg.py
   docs/prereg/PREREG-P32-AMEND1.md` both exit 0 with `match` true.
2. At the RC SHA, `sha256sum scripts/release/run_p32.py tests/fixtures/agt/a1_prompts_50.jsonl
   scripts/g1_synthetic_evidence.py` prints, in that order, `161ba03859d9904df6ede396a51baf481825129e6b9b28e2fce3df5a83a6db17`,
   `2e7cd0f9dab2c2535bf289825bd6737607c2390b7fd68d9f1c925820bd559cc6` and
   `b0d2e138973e66b58f54e1883b8801883f2e0384f74313131debe31b9964f850`.
3. `provenance.item_file_sha256` in the candidate file equals those three values; in `baseline.json` the g1 entry
   is `6fe250a707c9a7aaa2a52735a0c363e1ad3c5eb5905c357b0c8bd7f5fb58344c` and the other two equal the values above.
4. `run_p32.py --dry-run` exits 0 at the RC SHA, and the four manifest hashes still equal `release_evals/PIN.json`.

## Freeze record

To freeze: after an independent review, write `PREREG-P32-AMEND1.sha256` as `<sha256>  PREREG-P32-AMEND1.md` and
`frozen_utc <date -u>` (after 2026-10-17T23:13:17Z), then run `scripts/check_prereg.py
docs/prereg/PREREG-P32-AMEND1.md`. Any change to this file changes the hash and needs a new sidecar.
