# PREREG-P32 AMEND3: long-session fidelity set fid_long_120, REPORT-ONLY in 16.1 (owner decisions D-619, D-620)

Status: **DRAFT, not frozen.** Frozen only when `PREREG-P32-AMEND3.sha256` exists beside it, an independent
freeze-guard review has passed (D-122: an author-written sidecar is void) and
`scripts/check_prereg.py docs/prereg/PREREG-P32-AMEND3.md` reports a match. No sidecar is written by the PR that adds
this file. It adds a report-only set. It does not edit `PREREG-P32.md`
(sha256 `d2e175aa6dc776635b01659811266717d8759bf7923ae20426a83569a5585fa3`), `PREREG-P32-AMEND1.md`
(`76879f9830cf03a3f85d680ac3c8fbaa8a18a62a30f619dd8bb7687d74ae2afe`), `PREREG-P32-AMEND2.md`
(`32019a21f42d5ff6655fa5bde8be1115d2015aa9f251dbd67be373ef5dd1b63c`), their `.sha256` sidecars,
`release_evals/PREREG.sha256`, `release_evals/PIN.json`, `release_evals/baseline.json`,
`release_evals/manifests/*` or `scripts/g1_synthetic_evidence.py` (pin `b0d2e138973e66b58f54e1883b8801883f2e0384f74313131debe31b9964f850`).

## Why

AMEND2 Residual risk: a PASS of `fid_syn_60` at window 65536 is by construction. Its sessions are about 15.9k estimated
tokens (g1 `CYCLES=9`), below the full-mode threshold of 52,428 tokens (`whole * 5 <= window * 4`,
`src/llm_router/context_pack.py:756-761`), so `build_pack` returns the whole conversation and PACK retention equals FULL
retention. The trimming path and the rolling summary are not exercised. `fid_long_120` exercises them.

## Authority

Owner decision D-619 (2026-11-25): build the longer-session fidelity set (D-352). Owner decision D-620 (2026-11-25):
REPORT-ONLY in 16.1, retention only, window 65536 primary, 32768 descriptive. Design:
`~/.rsi/research/primary-plan/v16/coord/FIDLONG-DESIGN-20261125.md`.

## What is added (only this)

1. `scripts/release/fid_long_gen.py`: deterministic generator. `SEED_LONG=20261125`; item `i` uses
   `random.Random(SEED_LONG*1000+i)`; `CYCLES=40`; 6 categories x 20 = 120 items, ids `fidl-<i>`. It reuses g1 helpers
   (`_filler`, `_cc`, `_answer_token`, `Item`, `full_context`) and edits none. It asserts, per item, whole >= 65,535
   estimated tokens (len // 4 of the FULL context) and one occurrence of the answer token in FULL, and across the set 20
   items per category and 120 distinct tokens. A late decoy token of the same shape sits in the assistant note of step 38 (c=37).
2. `scripts/release/run_fid_long.py`: per item, FULL and PACK retention at 65536 (primary) and 32768 (descriptive), PACK built
   as `run_p32.run_fid` builds it (`g1.pack_context`: `build_pack(door="hook", target_provider="anthropic")`, extractive rolling
   summary, scratch HOME). It aborts with exit 1 and writes nothing if any item is not `mode == "pack"` at 65536. It writes
   counts only (per category: worse / better / both / neither) to `release_evals/fid_long_<sha>.json` (gitignored, not
   `release_evals/<sha>.json`). `scripts/release/pre-release-verify.sh` step 12c runs it, labelled REPORT-ONLY, and
   continues if it fails to run.
3. Categories (token position, cycle c of 40): `first_user` (first human request; control), `recent` (last assistant note,
   c=39; control), `early_user_mid` (human request opening step 9, ~20%), `early_tool` (tool result c=4, ~10%),
   `early_assistant` (assistant note c=10, ~25%), `mid_tool` (tool result c=20, ~50%).
4. Generator hash (`python scripts/release/fid_long_gen.py --hash`, identical for PYTHONHASHSEED 0 and 1 and for two runs):
   `1d5a5e5c1f1f2146c5038042cd0796f688231dbedceefef4ca3a963d901d76ae`.

## Rules

- REPORT-ONLY in 16.1: the numbers are printed and stored every release. They are not in `eval_gate.py`'s decision, not in
  `release_evals/PIN.json`, not in `baseline.json`, and never block. No manifest is added.
- Retention only (model-free, $0, `SPEND_CAP_API_USD=0` unchanged). No model answers; no Claude arm.
- Outcome per item: worse = FULL retains the token and PACK does not; better = the reverse.
- Reference bar for a later gating decision (not applied in 16.1): BLOCK iff (worse - better) / n > 0.02 (3 net at n=120).
- Gating, a Claude answer arm and 32768 as a gated stratum each need a new amendment.

## Residual risk

Retention shows the token is in the context, not that a model uses it. The extractive summary keeps the first 200 chars of
each human request, so tool and assistant facts outside the last 7 messages are expected to be lost; the set measures that
known limit (PREREG-P32 line 123) and is expected to report it. Items are synthetic (D-99, D-281); real sessions differ.

## Freeze record

FILL-AT-FREEZE: reviewer, review date, sidecar sha256 and `frozen_utc`. To freeze: after independent review write
`PREREG-P32-AMEND3.sha256` as `<sha256>  PREREG-P32-AMEND3.md` and `frozen_utc <date -u>`, then run
`scripts/check_prereg.py docs/prereg/PREREG-P32-AMEND3.md`. Any change to this file changes the hash.
