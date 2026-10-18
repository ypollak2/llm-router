# PREREG-P32: frozen release eval sets for the v16 gate (PLAN v16 P3.2, §7.1 criterion 5)

Status: **FREEZE CANDIDATE d4 (2026-10-17; d3 plus freeze-guard r2 fixes F1-F7; d1-d3 kept beside it; written from code, plans and counts only).** Frozen only when `PREREG-P32.sha256` exists beside it, an independent freeze-guard review has passed (D-122: an author-written sidecar is void) and `scripts/check_prereg.py` reports a match. The baseline is run only after the freeze (§10 step 6). No model has been called. No real item has been scored for a result under this file; pre-freeze observations are listed in §15. All OPEN-n items are settled (D-209, D-210); see 'Settled questions'.

Models on: PREREG-P20-requal (frozen), PREREG-P20T-techop (d6), docs/prereg/PREREG-P11B-synthetic*.md. Gate code: `scripts/release/eval_gate.py`, `scripts/release/release_evals.py`, `.github/workflows/release.yml` (main) plus PIN.json from PR #571 (`release_evals.py pin`, `pin_problems`, `item_problems`).

## 0. Contradictions and settled points (read first)

| # | Contradiction | Settled as |
|---|---|---|
| C1 | PLAN P3.2 names GT-500 routing, p_eval Q&A, code hidden tests, O2 — all prompt-derived or model-graded; D-192(a) forbids committing prompt-derived data; D-192(b) the GT held-out (n=234) is not re-spent by the release gate. | Default gate = synthetic and hand-authored sets only (§4). Model-graded sets = later prereg or owner ruling (settled D-210). |
| C2 | Plan says "against the previous release file"; §7.1-5 says "v15.4 baseline". | Baseline = v15.4.0 for v16.0 (§5). After v16.0 an amendment picks the previous release. |
| C3 | Plan fidelity is a cross-release drop; P1.1-b is PACK vs FULL within one SHA; PACK does not exist at v15.4.0. | Fidelity is within-SHA (worse-better)/n <= 0.02, candidate file only, no baseline entry. |
| C4 | Stability is research-only today (`cls_dev_lib.stability_share`); P1.6-b says 500x2 from `door_corpus` (private). | Synthetic 500x2 (§4.4). The real 500x2 is not a gate set. |
| C5 | A fixture set is not held out; single-use protects held-out sets; these are public regression pins. | §13 replaces single-use with a no-tuning rule plus per-SHA determinism. |
| C6 | Most fixtures (a1, golden_30, tier goldens) do not exist at the v15.4.0 tree. | Harness and item files come from the freeze commit; only the code under test is v15.4.0 (§5). |

## 1. Authority
PLAN-v16 §7.1-5, P3.2-a/b, R-EVL-5, R-CTX-9, R-CLS-3, D-192 (a-d), D-27=A, brief-p32, rv-564 r1 verdict (pin set list, manifest hashes and baseline sha; bool strictness), PR #571.

## 2. Question and decision rule
Question: does the candidate SHA regress routing decisions, context fidelity or decision stability relative to v15.4.0 on frozen synthetic sets?

The rule is the code's, unchanged (do not re-implement):
* Performance: per set, paired by item id. b = baseline pass and candidate fail, c = the reverse; drop = (b-c)/n. **BLOCK iff drop > 3.0 pts, strictly**, Fraction arithmetic.
* Fidelity: BLOCK iff (worse-better)/n > 0.02 (worse = PACK failed where FULL passed).
* Stability: BLOCK iff identical share < 0.98.
* NOT INFORMATIVE (exit 2, fails the release): no manifests; malformed manifest or n < min_n; set missing in the candidate (or baseline for performance); duplicate/missing/extra ids; recorded n differs from manifest; sha mismatch; any pin mismatch (#571); non-boolean pass/pack/full values; pinned manifest missing or extra; manifest sha256, n or min_n differing from the pin; baseline sha differing from baseline_sha; `release_evals/PREREG.sha256` absent or differing from the pin's prereg_sha256; `docs/prereg/PREREG-P32.md` absent when a pin exists or its sha256 differing from the sidecar; candidate file sha differing from `--sha`; stability decision not a non-empty string (eval_gate.py:15-26, :282-293).
* The gate must be PASS on every set. One BLOCK or one NOT INFORMATIVE means no tag.

## 3. Arms
Baseline: v15.4.0 (`9e8b1460eea9a48f2686eacca184e5a2e7a232e5`). Candidate: the SHA to be tagged. No models in the design. Model-graded sets, Sonnet and local-model arms are NOT IN SCOPE for v16.0 (D-209).

## 4. Sets and item sources
All item text is already committed or generated from a committed seed. No file under `requal`, `p17d`, `gt500`, `door_corpus` or any pool/item/answer directory is read. Manifests hold opaque ids only.

Item ids: `cg-<sha256(policy|prompt)[:12]>` (rt_hand_60), `a1-<i>` (rt_agt_50), `fid-<index>` (fid_syn_60), `st-<sha256(text)[:12]>` (stab_syn_500). Never positional ids alone.

### 4.1 rt_hand_60 (performance, n=60, min_n=60)
Source: `scripts/release/classifier_gate.py` FIXTURES (56, HOOK_POLICY) + FLOOR_FIXTURES (4, ROUTER_POLICY); hand-labelled; exists at v15.4.0. `run_p32.py --dry-run` verifies the FIXTURES lists at v15.4.0 and at REPO_SHA are byte-identical (counts only); it is not enforced during baseline or candidate runs.
Outcome: `classify_signals(prompt, policy)` gives (task_type.value, complexity.value) equal to the label; pass iff both match.

### 4.2 rt_agt_50 (performance, n=50, min_n=50)
Source: `tests/fixtures/agt/a1_prompts_50.jsonl`, 50 synthetic prompts for the SDK test; 15 (task_type, tier) cells: haiku 4 and sonnet 4 per task type x5, opus 2 each.
Outcome: classified task_type (under ROUTER_POLICY, the SDK/router path) equals labelled task_type. Tier not scored (D-209). Not D-178 door_agreement (agreement, not accuracy).

### 4.3 fid_syn_60 (fidelity, n=60, min_n=60 = P1.1-b MIN_PAIRED)
Source: `scripts/g1_synthetic_evidence.py` seeded generator (SEED 20261014; MIX recent 30 / early_user 10 / early_tool 10 / early_assistant 10).
Outcome default (a), model-free retention: pass_full = answer token present in FULL context; pass_pack = present in PACK context at the 8192 window. $0, deterministic, runs in CI.
Alternative (b) (NOT IN SCOPE for v16.0, D-209; kept for the record): Sonnet answers over each context via `claude -p --model sonnet`, 120 calls; stochastic — at n=60 with a 2% bar one net flip is 1.67% (pass), two is 3.33% (block), so noise alone can block. Option (a) tests that the fact survives packing, not an answer.

### 4.4 stab_syn_500 (stability, n=500, min_n=500)
Source: the 106 unique fixture texts of 4.1+4.2 (4 of the 110 fixtures repeat another fixture's text; a text is one item and is decided under both policies) plus 394 items from a seeded template generator (seed 20261018; choices from sha256(seed|index|salt), independent of PYTHONHASHSEED; texts sha256 pinned as STAB_GEN_TEXTS_SHA256; invented prompts, no real text) = 500.
Decision: (task_type, complexity, confident, score) from `classify_signals` under both policies, canonical JSON. Identical = exact string equality between run a and run b.
Runs a and b are separate processes: PYTHONHASHSEED=0 vs 1 (deliberate, exposes ordering nondeterminism), decide cache memory-only (`LLM_ROUTER_DECIDE_CACHE=memory`), `LITELLM_LOCAL_MODEL_COST_MAP=True`, no network (netguard), scratch HOME. Block at n=500: more than 10 differing items.
Fallback if the generator is not built: `stab_syn_110` with n=min_n=110 (98% needs >= 108 identical).

### 4.5 Excluded
Prompt-derived/private sets (GT held-out, p_eval, door_corpus, P2.0 pools) — excluded by D-210. `quality_gate.py` pinned pair (n=2). Tier and pack goldens (CI unit tests; self-recorded snapshots, not gold).

## 5. Baseline
* Commit: v15.4.0 = `9e8b1460eea9a48f2686eacca184e5a2e7a232e5` (verified with `git tag -l 'v15*'`, `git rev-list -n1 v15.4.0`).
* Who: the coordinator (not the harness author, not CI). CI verifies the committed file against the pin. No owner action (no model, no spend).
* How: fresh clean worktree at v15.4.0 for the code under test, harness and item files from a clean checkout of REPO_SHA. `git worktree add /tmp/base v15.4.0`; then, from the clean REPO_SHA checkout: `PYTHONPATH=/tmp/base/src LITELLM_LOCAL_MODEL_COST_MAP=True python scripts/release/run_p32.py --role baseline --sha 9e8b1460eea9a48f2686eacca184e5a2e7a232e5 --worktree /tmp/base --sets rt_hand_60,rt_agt_50 --out release_evals/baseline.json`. The baseline script runs from a clean checkout of REPO_SHA (`git status --porcelain` empty, `git rev-parse HEAD` equals REPO_SHA). The harness records `llm_router.__file__`, git sha and item-file hashes (`provenance.item_file_sha256`).
* Refusals: the harness refuses (exit 1, nothing written) if: --sha is not 40 lowercase hex; the --worktree HEAD is not --sha; the imported llm_router is not under --worktree; the worktree has uncommitted tracked changes (`git status --porcelain --untracked-files=no`; for a candidate, paths under release_evals/ are exempt); for --role candidate, --worktree is not the script's own checkout (check_root); a baseline is asked for a non-performance set; an unknown set. (run_p32.py:392-416, :509-518.) The harness does not know BASELINE_SHA. The tie to 9e8b1460… is the coordinator comparing `--sha`, `baseline.json.sha` and `PIN.json.baseline_sha` with the BASELINE_SHA constant, because `release_evals.py pin` copies baseline_sha from baseline.json.
* Item and label edits are not detected by the gate (ids `a1-<i>` and `cg-<sha256(policy|prompt)>` do not depend on labels; `provenance.item_file_sha256` is recorded, not gated; the baseline role has no ROOT-clean or REPO_SHA check). The coordinator therefore compares the three ITEM_SHA256_* constants (each filled with `sha256sum <file>` at REPO_SHA) with `provenance.item_file_sha256` in baseline.json and in the candidate file. The coordinator compares `provenance.item_file_sha256` in baseline.json with the three ITEM_SHA256_* constants before step 7. The classifier_gate.py hash is informational; the binding check is FIXTURES byte-identity in `--dry-run`.
* Baseline holds the two performance sets only; fidelity and stability are candidate-only.
* Once only (§13). Baseline `sha` must equal pinned `baseline_sha`.

## 6. Outcomes and statistics
The gate's statistics: n, b, c, discordant rate d, drop, exact McNemar p, Newcombe paired CI are printed for performance sets only (eval_gate.py:164-167); fidelity prints worse/better/gap, stability prints identical/share.
Granularity per kind (exact), smallest blocking change:
| Kind | Set, n | Smallest blocking change |
|---|---|---|
| performance (drop > 3.0 pts strictly) | n=50 | 2 net losses |
| performance | n=60 | 2 net losses |
| fidelity (floor(0.02n)+1) | n=60 | 2 net |
| stability | n=500 | 11 differing blocks (10 allowed) |
| stability fallback | n=110 | 3 differing blocks |
Population power (informational; sets are fixed pins, not samples): one-sided alpha 0.05, power 0.8, paired: n ~ (1.645+0.842)^2 * d / delta^2 = 6872*d for delta=3 pts (d=0.02 → ~137; 0.05 → ~344; 0.10 → ~687); delta=5 pts: 2474*d. A two-sided 95% half-width of 3 pts needs 1.96^2*d/0.0009 (d=0.10 → 427, matches PLAN §0.4 item 9). At n=60/50 a 3-pt population drop is not detectable by sampling logic; the gate is a deterministic item-level guard and a PASS does not show absence of regression outside the items (POWER_NOTE printed every run).
Ceiling: if the baseline passes 60/60 only losses can show — by design for a pin.

## 7. Verdicts
GATE PASS: all four sets PASS. BLOCK: any BLOCK and no NOT INFORMATIVE. NOT INFORMATIVE outranks BLOCK and fails the release. Informativeness by construction (min_n = n).

## 8. Spend and windows
**$0**, no model, no network; hard cap 0 API cash. Sonnet (fidelity option b) and local-model sets are NOT IN SCOPE for v16.0 (D-209); adding either needs a separate prereg (and, for local models, the D-199/D-202 quiet gate and shared `ollama.lock`).

## 9. Skip and override policy
`--skip-quality` refused on a tag unless `LLM_ROUTER_RELEASE_OWNER_OVERRIDE=1`; off a tag allowed and logged. `release.yml` has no skip path. An owner override means v16 is tagged WITHOUT P3.2 evidence; release notes must say so (§7.1-7). For v16.0 no owner override is used (D-210). LLM_ROUTER_RELEASE_OWNER_OVERRIDE stays in the code as a mechanism; setting it for the v16.0 tag voids this prereg's gate claim. Nobody lowers a bar, min_n or n after the freeze; a change is a new prereg.

## 10. Pin step and freeze procedure
Order: pin needs baseline; baseline needs the frozen prereg; prereg lists manifest hashes.
1. Harness, manifests tool, generator and rv-564 fixes merged and reviewed. REPO_SHA = `git rev-parse HEAD` of main immediately before the freeze commit, after #571 and #573 are both merged, with a clean tree (re-run steps 3-4 if the merged tree differs from e5ae848b). At the RC the three ITEM_SHA256 files must be byte-identical to REPO_SHA and `run_p32.py --dry-run` must exit 0. Freeze commit contents: `docs/prereg/PREREG-P32.md`, `docs/prereg/PREREG-P32.sha256`, `release_evals/manifests/*`, `release_evals/PREREG.sha256`. `baseline.json` and `PIN.json` go in a later commit.
2. Manifests written with `run_p32.py --write-manifests release_evals/manifests` (identical bytes to `release_evals.py freeze`); one per set. The ITEM_SHA256_* constants are `sha256sum <file>` at REPO_SHA. The MANIFEST_SHA256_* constants come from `python3 -c "import sys,json,pathlib; sys.path.insert(0,'scripts/release'); import release_evals as r; print(r.manifest_sha256(json.loads(pathlib.Path('release_evals/manifests/<set>.json').read_text())))"` (run from the REPO_SHA checkout after the manifests are written); the reviewer recomputes independently.
3. Independent freeze-guard review (READY TO FREEZE required). The bytes hashed in step 5 are exactly the bytes the freeze-guard reviewed; fill-ins and the Status line are final before step 3.
4. **Real-path precondition dry run (house rule), counts only, before writing the sidecar**, from a clean worktree at REPO_SHA (the checkout must have the v15.4.0 tag). Run `run_p32.py --dry-run` (expect exit 0; it prints n and unique ids per set, label-cell counts, fid categories, stab texts_sha256 matches_pin=True, and FIXTURES identical vs v15.4.0). Then, in an uncommitted staging directory `$STG` (cwd for all commands below, so the defaults `release_evals/…` resolve there) containing `release_evals/manifests/` (the step 2 manifests), `release_evals/baseline.json` (`sha` = BASELINE_SHA; rt_hand_60 and rt_agt_50 sets, every item `pass: true`), `docs/prereg/PREREG-P32.md` (the reviewed bytes), a staging `docs/prereg/PREREG-P32.sha256` (two lines, as step 5) and `release_evals/PREREG.sha256` (one line, as step 5). Run `python3 <REPO_SHA checkout>/scripts/release/release_evals.py pin` (PYTHONPATH `<checkout>/src`), then three stub candidate files, each with all four sets, `sha` = any 40-hex value passed as `--sha`: (i) rt_hand_60 and rt_agt_50 all pass, fid_syn_60 `pack` and `full` true, stab_syn_500 `a` equal `b` → `eval_gate.py compare --candidate <i> --sha <sha> --prereg-file docs/prereg/PREREG-P32.md` exits 0; (ii) as (i) with 2 rt_agt_50 items `pass: false` (4.00 pts) → exit 1; (iii) as (i) with one rt_agt_50 item removed → exit 2. Then `scripts/check_prereg.py docs/prereg/PREREG-P32.md` in `$STG`, and `load_manifests` / `manifest_problems` on `$STG/release_evals/manifests`. Stub item ids come from the manifests only; no real item is scored. Delete all staging files. No real item scored, no outcome read, no model called. Any refusal voids the freeze.
5. Freeze file and sidecars. Copy the reviewed bytes to `docs/prereg/PREREG-P32.md` (the path release.yml `--prereg-file` reads). Write `docs/prereg/PREREG-P32.sha256` as two lines: `<sha256>  PREREG-P32.md` and `frozen_utc <date -u +%Y-%m-%dT%H:%M:%SZ>`. Write `release_evals/PREREG.sha256` as exactly ONE line, the `sha256sum` output, with no frozen_utc. Both hashes equal the sha256 of the committed `docs/prereg/PREREG-P32.md`. Make a byte-identical 0444 copy at `~/.rsi/research/primary-plan/v16/coord/PREREG-P32.0444.md`. Check with `scripts/check_prereg.py docs/prereg/PREREG-P32.md` (exit 0, `match` true) and `sha256sum docs/prereg/PREREG-P32.md` equal to the first token of both sidecars. The `eval_gate.py compare --prereg-file` check runs at step 4 (staging) and at the RC (step 8).
6. **Baseline run** (§5) after the freeze, before any candidate run.
7. **`release_evals.py pin --baseline release_evals/baseline.json`** right after the baseline exists → `release_evals/PIN.json` (baseline_sha; prereg_sha256; per set manifest_sha256, n, min_n). Pinned values must equal the constants here (coordinator checks, reviewer recomputes). Copy `baseline.json` into a checkout of the freeze commit and run `release_evals.py pin --baseline release_evals/baseline.json` there; commit `baseline.json` and `PIN.json` together in the later commit. Committed.
8. Candidate run at the RC SHA writes `release_evals/<sha>.json`; `release.sh` attaches it; `release.yml` runs the comparator.
9. After the freeze any byte change is a new prereg (AMEND with its own sidecar).

## 11. Pre-tag checklist (rv-564)
`docs/prereg/PREREG-P32.md` hashed with both sidecars (§10 step 5), 0444 applying to the coord copy only (git cannot store 0444); all four manifests, matching PIN.json; `baseline.json` for v15.4.0 on the same ids; `release_evals/<HEAD>.json` attached; bool strictness verified on the comparator path (#571); pin fix merged; real-path precondition dry run (§10 step 4); `run_p32.py --dry-run` exit 0 at the RC SHA; actionlint green.

## 12. Effort and wall time (estimates, not measured)
Harness build: done (PR #573). Baseline: ~15 min coordinator time. First candidate run: ~15 min. Sonnet option (b) and local models: out of scope for v16.0 (D-209).

## 13. Single use and no-tuning
Baseline run once (second baseline = new prereg). Candidate run per SHA; a re-run on the same SHA must give byte-identical item results (a coordinator procedure, not enforced by the gate; a difference = stability finding, BLOCKs). Public sets: no label edits, no fixture added/removed/reworded after the freeze; no `classify.py` change made to flip a fixture without saying so; fixing a fixture = new prereg. Goodhart risk accepted and stated (§15).

## 14. What this cannot show
That routing is good (rt_hand_60 labels are designed behaviour: a pin, not accuracy); production behaviour (S8: about half of real prompts hit the low-signal default); answer quality (no Q&A, code, O2); latency; population drops below ~10 pts at n=50/60 (POWER_NOTE, eval_gate.py:68); answer fidelity under option (a) (retention only); tier choice. A PASS is not evidence of no regression outside the items. Trust boundary (eval_gate.py:28-32): CI does not recompute candidate results; candidate and baseline pass values are trusted beyond sha and ids.

## 15. Known biases (stated before any data)
**Pre-freeze observation (disclosed, rv-573):** on 2026-10-17, during code review of the harness (PR #573), the reviewer ran rt_hand_60 once at v15.4.0 in a scratch directory and saw its pass count, 60/60. No other set was scored; the file was deleted and is not the baseline. Impact: the set's items, labels and rule were fixed before this run and cannot change (§13); the same 60 fixtures are already asserted by the CI classifier gate, so their v15.4.0 pass state was already public: `classifier_gate.py:92-97` pins this set at 56/56 and 4/4 with BASELINE = 1.0, so rt_hand_60 passing at v15.4.0 was already implied by the CI gate. It is recorded so the freeze record is complete.
**Pre-freeze computation in tests (disclosed, rv-573 r2):** from PR #573's first head (96cc45ef) until the r2 repair, the harness test `check_end_to_end_file_passes_the_gate_and_manifests_match_freeze` ran the real scorer over rt_hand_60 and rt_agt_50 on every local and CI test run. It asserted only exit code, file shape, provenance and an identical-vs-identical gate PASS; no pass count was printed or read by anyone, and the output was a temp file. The repair replaces the items with stand-ins and adds a guard that makes real-set scoring raise in that test file.
In the same file, `check_hand_outcome` and `check_agt_outcome` each scored one real item text (the classifier_gate fixture "implement a function that reverses a linked list", classifier_gate.py:125,191, and the a1 prompt "What is the capital of Portugal?") and asserted its outcome, so those two single-item outcomes were known before the freeze. The r2 repair replaced them with invented texts; the guard (`forbid_scoring_real_items`, autouse) now raises on any real P32 text reaching `classify_signals` in that file.
Also in that file until the r3 repair: `check_fidelity_retention_outcome` computed full/pack retention on 4 real fid_syn_60 items (indices 0, 30, 40, 50) and asserted full retention (`full is True`: no information, since FULL retention holds by construction and the pack side was not asserted); `check_stability_real_children_are_separate_processes` decided 10 real stab_syn_500 texts (4 hand, 2 a1 and the last 4 of the 500) and asserted a == b in two child processes. No count was printed; the assertions passed, so those item-level outcomes (fidelity FULL retention on 4 items, stability agreement on 10 items) were implicitly known. The r3 repair replaces them with invented items and adds caller-side guards on the real-set loaders.
None of these observations could affect item, label or rule choice: items and labels were committed first. The a1 jsonl landed in b987307c on 2026-10-17, after v15.4.0, for `tests/test_agt_a1_sdk_router.py`, which asserts SDK-vs-MCP decision equality, not label accuracy; the origin of the a1 labels relative to classifier output is unverified.
The coordinator attests that, to the coordinator's knowledge from the build and review reports, no builder or reviewer run scored a real set outside the occasions listed above.
Circular labels; ceiling effects; public-fixture tuning; synthetic generator style; a1 labels written for a test (gold status unverified); the 8192 fidelity window; option (a) retention overstates fidelity.

## Fill-ins at the freeze
FILL-AT-FREEZE keys: REPO_SHA (§10 step 1), the four MANIFEST_SHA256_* (command in §10 step 2) and the three ITEM_SHA256_* (each filled with `sha256sum <file>` at REPO_SHA).

```text P32-CONSTANTS
LABEL=p32-release-evals
REPO_SHA=e96f8a076811f88045cf3a3320f0e4b23c564eb4
BASELINE_TAG=v15.4.0
BASELINE_SHA=9e8b1460eea9a48f2686eacca184e5a2e7a232e5
PREREG_PATH=docs/prereg/PREREG-P32.md
DROP_BLOCK_PTS=3
FIDELITY_MAX=0.02
STABILITY_MIN=0.98
SETS=rt_hand_60,rt_agt_50,fid_syn_60,stab_syn_500
N_rt_hand_60=60
N_rt_agt_50=50
N_fid_syn_60=60
N_stab_syn_500=500
STAB_N=500
MIN_N_rt_hand_60=60
MIN_N_rt_agt_50=50
MIN_N_fid_syn_60=60
MIN_N_stab_syn_500=500
MANIFEST_SHA256_rt_hand_60=c2d9bcffd2536f1507b006e1d2d80c9964ebb36ff7d84e8c50e93d6c33ef1005
MANIFEST_SHA256_rt_agt_50=3dc6217a733d6af275dbb1f902b1f23ee38fd3991222300f5de3a19da656e6cd
MANIFEST_SHA256_fid_syn_60=1c7fadb02e580e11f5f2bfe9fc8e42654a12c18799dbf872a8bc54aae95d14ec
MANIFEST_SHA256_stab_syn_500=736a65cda3475509051de4513dc0dfe810de1414ad15479ecd559b30a023724a
ITEM_SHA256_scripts/release/run_p32.py=161ba03859d9904df6ede396a51baf481825129e6b9b28e2fce3df5a83a6db17
ITEM_SHA256_tests/fixtures/agt/a1_prompts_50.jsonl=2e7cd0f9dab2c2535bf289825bd6737607c2390b7fd68d9f1c925820bd559cc6
ITEM_SHA256_scripts/g1_synthetic_evidence.py=6fe250a707c9a7aaa2a52735a0c363e1ad3c5eb5905c357b0c8bd7f5fb58344c
FID_SEED=20261014
FID_WINDOW=8192
FID_MODE=retention
FID_PROVIDER=anthropic
STAB_GEN_SEED=20261018
STAB_GEN_TEXTS_SHA256=018cb7ec93823c113db7c827fbffb43bf4c32570af8451e7aca66ca34b8c3951
STAB_PYTHONHASHSEEDS=0,1
SPEND_CAP_API_USD=0
OVERRIDE_ENV=LLM_ROUTER_RELEASE_OWNER_OVERRIDE
OVERRIDE_ALLOWED_V16_0=no
```

## Settled questions (D-209 coordinator defaults, D-210 owner decisions)
Numbered as in the d1 list below. Q1 and Q8: synthetic regression pins only (owner D-210). Q2: fidelity (a) retention. Q3: a1 task_type only, ROUTER_POLICY. Q4: generator built (PR #573). Q5: comparator verifies release_evals/PREREG.sha256 against the pin (PR #573). Q6: no owner override for v16.0 (owner D-210). Q7: release.sh attaches the candidate file.

## Original open questions (d1; SUPERSEDED by 'Settled questions', kept for the record)
1. OPEN-1 (owner, release-bar scope): include model-graded sets (p_eval Q&A, code hidden tests, O2) in v16.0, or defer (default: defer; v16.0 gates routing/fidelity/stability on synthetic sets; D-192(b) "fresh release sets" may be synthetic).
2. OPEN-2 (coordinator default (a)): fidelity mode — (a) model-free retention, $0; (b) Sonnet via claude -p, noisy at 2%/n=60.
3. OPEN-3 (coordinator default): a1 labels — score task_type only; tier dropped.
4. OPEN-4 (coordinator default): build the seeded stability generator (n=500); fallback n=110.
5. OPEN-5 (coordinator default yes): comparator verifies release_evals/PREREG.sha256 against the pin.
6. OPEN-6 (owner): is an owner override allowed for v16.0, and must release notes say so.
7. OPEN-7 (coordinator default): release.sh attaches the candidate file at tag push.
8. OPEN-8 (owner, release-bar scope): is a hand-labelled "designed behaviour" routing pin acceptable as the v16.0 routing bar, or is an organic/GT-derived set required (needs ratification and a single-use ruling).

## What was looked at before writing (code and plan text only)
PLAN-v16 §7.1, P3.2, D-191/D-192; brief-p32; rv-564 verdict; eval_gate.py, release_evals.py, release.yml on main; PR #571 diff; classifier_gate.py; fixture file sizes and the a1 label distribution; PREREG-P20-requal and P20T (structure only).

## Freeze record
The freeze record is `docs/prereg/PREREG-P32.sha256` (hash and frozen_utc) plus the freeze-guard verdict file. Nothing is written here after hashing.
