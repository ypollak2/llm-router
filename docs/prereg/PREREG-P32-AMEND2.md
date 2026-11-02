# PREREG-P32 AMEND2: fidelity window 8192 -> 65536 (POST-HOC, owner decision D-351)

Status: **DRAFT, not frozen.** Frozen only when `PREREG-P32-AMEND2.sha256` exists beside it, an independent
freeze-guard review has passed (D-122: an author-written sidecar is void) and
`scripts/check_prereg.py docs/prereg/PREREG-P32-AMEND2.md` reports a match. It amends `PREREG-P32.md`
(sha256 `d2e175aa6dc776635b01659811266717d8759bf7923ae20426a83569a5585fa3`, frozen_utc 2026-10-17T23:13:17Z),
which stays byte-identical, as do `docs/prereg/PREREG-P32.sha256`, `release_evals/PREREG.sha256`,
`release_evals/PIN.json`, `release_evals/manifests/*` and `PREREG-P32-AMEND1.md`. No release eval has been run
at the amended window and no `release_evals/<sha>.json` is written by its PR. The
scoring that preceded the amendment is listed under 'Scoring occasions after the P32 freeze'.

## POST-HOC disclosure

**This amendment was chosen after the result was known.** The release eval for v16.0.0 RC `2f22034d` returned
BLOCK on `fid_syn_60`: worse = 20 of 60 (early_tool 10/10, early_assistant 10/10; early_user 0/10, recent 0/30;
better = 0). A model-free window sweep on the same 60 items (build_pack only, deterministic, run twice on items
40 and 50 with identical output) then gave:

| FID_WINDOW | pack retains token (n=60) |
|---|---|
| 8192 | 40/60 |
| 16384 | 40/60 |
| 32768 | 60/60 |
| 65536 | 60/60 |

Source: `~/.rsi/research/primary-plan/v16/coord/RC-FID-BLOCK-20261102.md` (scratch scripts
`fidinv/probe.py`, `fidinv/win.py`). The value was therefore not fixed before seeing the outcome. 32768 was
rejected because it is merely the smallest value that passes; 65536 is picked because it is the production cap
for cloud doors (D-101, PR #514: cloud doors use the model-registry window capped at 64k), not because it passes.
The BLOCK at `2f22034d` is a recorded result (candidate file and sha256 under 'Scoring occasions after the P32 freeze'),
not deleted or rewritten. It stands as evidence that the pack at the original frozen window of 8192 loses early
tool/assistant facts (a known limitation, PREREG-P32 line 123; `docs/measurements/p11b-synthetic-sonnet.json`, window
8192: pack passed 40 of 60, worse 20, verdict FAIL; `docs/measurements/p11b-synthetic-sonnet-r2.json`, MCP door: pack
equivalent to FULL in 60 of 60 sessions at 65536 and in 0 of 60 at 8192).

## Scoring occasions after the P32 freeze (extends PREREG-P32 §15)

PREREG-P32 §15 attests that no builder or reviewer run scored a real set outside the occasions it lists. These runs came
after the freeze and are added to that list. All were model-free and used committed fixtures or seeded generators only.

1. RC candidate run, 2026-11-02, output written 22:01:40Z, coordinator: `run_p32.py --role candidate` at
   `2f22034dd829e8ef7768838fd899b52511d8fc9a` (Python 3.14.6), then `eval_gate.py compare`. All four sets were scored.
   Result: BLOCK fid_syn_60 (worse 20, better 0, (worse-better)/n 0.3333 > 0.02); PASS rt_agt_50 (b 0, c 0, drop 0.00
   pts); PASS rt_hand_60 (b 0, c 0, drop 0.00 pts); PASS stab_syn_500 (500 of 500 identical). Record: the candidate file
   `release_evals/2f22034dd829e8ef7768838fd899b52511d8fc9a.json` (gitignored), sha256
   `6a0758881650cfe7582083bd59064b9ef3a0cf36458b6319c5c4f889d4982f03`, copy kept as
   `coord/RC-CANDIDATE-2f22034d-BLOCK.json`.
2. Investigation of that BLOCK, between 22:01Z and 22:05Z the same day, fid_syn_60 only, no model: the per-item flags of the
   candidate file (which ids were worse, by category); `fidinv/probe.py` (sha256
   `8603119b3bfef1817f4df9249cdac1a5ab75ab45ee8fee4870baae01fbed5f19`) on items 40 and 50, run twice; `fidinv/win.py`
   (sha256 `a0401395565132afec2e3270dad9b8f025b759063f776c51dc4d720619ce0261`) over all 60 items at windows 8192, 16384,
   32768 and 65536 (`build_pack` only), the table above. Copies are in `coord/` as `RC-FID-BLOCK-probe.py` and
   `RC-FID-BLOCK-win.py`.

To the coordinator's knowledge from the build, investigation and review reports, no other builder, investigator or
reviewer run scored a real set after the P32 freeze, apart from the occasions in PREREG-P32 §15 and the two above.

## Authority

Owner decision D-351, 2026-11-02: option AMEND2, `FID_WINDOW = 65536`, labelled post-hoc.

## What changes (only this)

1. In the `P32-CONSTANTS` block, `FID_WINDOW=8192` becomes `FID_WINDOW=65536`. `FID_MODE=retention`,
   `FID_PROVIDER=anthropic` and `FID_SEED=20261014` are unchanged.
2. `scripts/release/run_p32.py` gains `FID_WINDOW = 65536` and `run_fid` sets `g1.WINDOW = FID_WINDOW` before
   building packs. `scripts/g1_synthetic_evidence.py` is NOT edited (it keeps `WINDOW = 8192`, its AMEND1 pin
   `b0d2e138...` stands). `--dry-run` now prints `FID_WINDOW=65536` on the fid_syn_60 categories line.
3. The pin `ITEM_SHA256_scripts/release/run_p32.py=161ba03859d9904df6ede396a51baf481825129e6b9b28e2fce3df5a83a6db17`
   is replaced by `ITEM_SHA256_scripts/release/run_p32.py=035954340fb4b2fb7f3c2d9782a71c8e04a668e3186ed8802652c29c05d1fbb9`.
   (AMEND1 left run_p32.py unchanged; this amendment changes it, so its pin moves.) The `a1_prompts_50.jsonl`
   pin (`2e7cd0f9...`) and the g1 pin (`b0d2e138...`) stand.
4. PREREG-P32 line 49 ("PACK context at the 8192 window") reads "at the FID_WINDOW (65536)" from now on.

## What stands

Items, ids, labels, generator seed, the four manifest hashes (`items_sha256` does not depend on the window),
`DROP_BLOCK_PTS`, `FIDELITY_MAX`, `STABILITY_MIN`, n and min_n values, stability constants, the decision rules,
the gate code, and "no owner override for v16.0" (D-210). 
`release_evals/baseline.json` is not changed; it holds the two performance sets only. Its `provenance.item_file_sha256`
stays as recorded: `scripts/release/run_p32.py` `161ba03859d9904df6ede396a51baf481825129e6b9b28e2fce3df5a83a6db17` (the
harness that scored the baseline; this amendment's 7 insertions and 2 deletions are in the fidelity path only, and
`score_hand`, `score_agt` and the stability code are untouched), `tests/fixtures/agt/a1_prompts_50.jsonl` `2e7cd0f9...`
and `scripts/g1_synthetic_evidence.py` `6fe250a7...` (AMEND1). For `baseline.json` the coordinator check of AMEND1 item 3
therefore keeps `161ba038...` for run_p32.py; only the candidate file moves to `035954340fb4b2fb7f3c2d9782a71c8e04a668e3186ed8802652c29c05d1fbb9`.

## Residual risk

A PASS at 65536 is by construction. `build_pack` returns `mode=full` whenever the whole conversation fits in 0.8 x the
window (`src/llm_router/context_pack.py:757`; 52,428 tokens at 65536). The sessions in this set are about 15.8k estimated
tokens (stand-in items of the same generator, indices 1000-1003) and switch from `mode=pack` to `mode=full` between windows
16384 and 32768, where the sweep above flips from 40/60 to 60/60. At 65536 the PACK context is therefore the whole
conversation and PACK retention equals FULL retention. The set can now fail only if full mode drops text. It no longer
exercises the trimming or the rolling summary, and a regression in them would not change this gate. At 8192 and 16384
fidelity is 40/60 (early tool and early assistant facts are lost). The pack is built at `door="hook"`
(`scripts/g1_synthetic_evidence.py:181`). In production the hook and agent doors take their window from the first chain
model's local `num_ctx` (table value, default 32768, or the operator override; `src/llm_router/hooks/auto-route.py:6253`,
`hooks/agent-route.py:1374`). 65536 is the D-101 cap for cloud models at the MCP, gateway and SDK doors
(`src/llm_router/router.py:6147-6160`, `sdk.py:311-323`; R1 and R2 measured those doors). Not covered by a passing gate:
sessions longer than the window (about 52k tokens at 65536, G1 doc), local models with small windows, and wheel installs
without `config/models.yaml` (these fall back to a small window). Retention is model-free: it shows the token is in the
context, not that a model uses it.

## Coordinator check at the RC

1. `scripts/check_prereg.py` on `PREREG-P32.md`, `PREREG-P32-AMEND1.md` and `PREREG-P32-AMEND2.md` all exit 0, match true.
2. `sha256sum scripts/release/run_p32.py tests/fixtures/agt/a1_prompts_50.jsonl scripts/g1_synthetic_evidence.py`
   prints `035954340fb4b2fb7f3c2d9782a71c8e04a668e3186ed8802652c29c05d1fbb9`, `2e7cd0f9dab2c2535bf289825bd6737607c2390b7fd68d9f1c925820bd559cc6`,
   `b0d2e138973e66b58f54e1883b8801883f2e0384f74313131debe31b9964f850`.
3. `provenance.item_file_sha256` in the candidate file equals those three values.
4. `run_p32.py --dry-run` exits 0 and prints `FID_WINDOW=65536`; manifests still equal `release_evals/PIN.json`.
5. The candidate is scored at an RC SHA that contains this PR; the 2f22034d BLOCK stays in the record.

## Freeze record

To freeze: after independent review, write `PREREG-P32-AMEND2.sha256` as `<sha256>  PREREG-P32-AMEND2.md` and
`frozen_utc <date -u>`, then run `scripts/check_prereg.py docs/prereg/PREREG-P32-AMEND2.md`. Any change to this
file changes the hash and needs a new sidecar.
