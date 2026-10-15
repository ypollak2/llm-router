# P1.7-d synthetic harness: design (`attach` built; the rest not built)

Implements `PREREG-P17D-synthetic` (DRAFT). A pins test reads the prereg `pins` block and fails if a
constant differs (the `test_shadowlab.py` pattern).

**Build order.**
1. **`attach`** is built **before** the freeze, in its own file `scripts/p17d_attach.py`. It is
   independently reviewed, run once by a sub-agent, and its file sha256 is pinned at the freeze
   (`ATTACH_SCRIPT_SHA256`).
2. **`select`, `shadow`, `label`, `score` and `status`** (`scripts/p17d_synthetic.py`) are built
   **after** the freeze. Their commit SHA (`HARNESS_SHA`) is logged in PROGRESS.md and as the first
   line of `use.log` (`harness <UTC> sha=<…>`) before the first shadow row. `shadow` refuses
   without that line.
3. `REPO_SHA` in the prereg is the router code under test (`src/llm_router`), pinned at the freeze.
   It is not the harness: the harness does not exist at freeze time.

## Files

| Path | Mode | Holds text | Written by |
|---|---|---|---|
| `scripts/p17d_attach.py` | repo | no | (built before the freeze; its sha256 is pinned) |
| `tests/test_p17d_attach.py` | repo | no (synthetic fixtures) | - |
| `scripts/p17d_synthetic.py` | repo | no | (built after the freeze; `HARNESS_SHA` logged) |
| `tests/test_p17d_synthetic.py` | repo | no (synthetic fixtures) | - |
| `$RUN = $PP/v16/p17d/` | 0700 | - | `attach` creates it |
| `$RUN/attach_counts.json` | 0600 | no | `attach` (exclusion counters, pool n, sessions, largest share) |
| `$RUN/items.jsonl` | 0600 | **yes** | `attach` (§2 fields; `group` F or G) |
| `$RUN/tier_policy.yaml` + `tier_policy.json` | 0600 | no | `attach` (a copy of the live policy; path, basis, sha256, route, complexity_knn) |
| `$RUN/selection.json` | 0444 after write | no | `select` (item ids, bound to sha256 of items.jsonl; gate result) |
| `$RUN/scratch/` | 0700 | no | `shadow` (scratch HOME / LLM_ROUTER_HOME) |
| `$RUN/classifier_shadow.jsonl` | 0600 | no | `shadow`, via the production `ShadowScheduler._append` |
| `$RUN/shadow_meta.json` | 0600 | no | `shadow` (nimble digest, quiet gate, warm-up ms, errors and retries, `done` marker) |
| `$RUN/labels.jsonl` | 0600 | no | `label` (`{text_sha, session_id, truth}`) |
| `$RUN/labels_planted.jsonl` | 0600 | no | `label` (first valid label per planted id; final, never re-asked) |
| `$RUN/attach.pinned` | 0444 | no | written at freeze (sha256 of items.jsonl and attach_counts.json); attach refuses once it exists |
| `$RUN/judgements.jsonl` | 0600 | yes (judge reasons) | `label` |
| `$RUN/ledger.jsonl` | 0600 | no | `label` (per call: ts, ids, cost, tokens, model_ok, secs) |
| `$RUN/use.log` | 0600, append-only | no | `score` (`started` + labels/shadow shas, first action); `select` and `label` (`terminal <reason>`) |
| `$RUN/result.json` | 0600 | no | `score` (full) |
| `docs/measurements/p17d-synthetic.json` | repo | no | `score` (item ids, session indices, tiers, verdict, `synthetic (D-99)`) |

## CLI (`python3 scripts/p17d_synthetic.py <cmd>`)

Every command except `attach` and `status` refuses unless
`check_prereg.check(PREREG)["match"]` is true and `frozen_utc` < now. Stdout carries counts and ids
only.

| Command | Reads | Calls | Refuses when |
|---|---|---|---|
| `attach` (sub-agent, D-107 c; read-only; single-shot) | corpus, GE3 items/split (for §2a), `~/.claude/projects` (top-level files only), `~/.llm-router/proxy_calls.jsonl`, `~/.llm-router/.env` (one key only), `denylist.txt`, the proxy launchd plist and process argv, exec_sessions, the leakage sets | none | `$RUN/items.jsonl` or `attach.pinned` exists, or the frozen prereg exists; any pin sha differs; any write target outside `$PP` or any mode other than 0600/0700 |
| `select` | items.jsonl | none | selection.json exists; use.log has `terminal`. A feasibility stop writes `terminal` |
| `shadow` | items, selection | Ollama localhost only | gate failed; labels.jsonl exists; nimble digest differs; quiet gate fails (PARKED) |
| `label` | items, selection, shadow_meta (`done` only) | `claude -p` (Opus) | shadow not done; use.log has `terminal`. It writes `terminal` on calibration < 6/6 or exit 4. A quota pause (exit 3) and the spend cap write nothing terminal, and resume skips labelled and planted ids |
| `score` | records, labels | none | use.log already has `started` or `terminal` |
| `status` | counts files, label line counts | none | never. It never parses `truth` |

## Functions

**Attach and selection**
- `load_pinned(path, sha)`: imports gt500, supply and shadowlab by path after a sha256 check, and
  the rubric through `gt.load_rubric`.
- `leak_index(sets) -> (norm_map, long_shingles, sid_ts)` and `is_leak(text, sid, ts)`: the
  supply.py norm, exact match, 5-character Jaccard ≥ 0.8, and the (session, ts) match.
- `attach(corpus, projects, ledger) -> counts`: runs the frozen record rules, the exact-text join to
  the corpus, eligibility, leakage, the window cut, and the organic kind. It builds the full body
  and stores the `assemble` pieces, `requested` and its basis, and the served tier.
- `select(items, seed, n, max_share) -> ids`: a shadowlab-style capped walk over F.
- `admit(ge3_items, split, f_ids) -> ids`: runs only if fewer than 100 F items were selected (§2a).
  It filters to held-out, organic, claude, not `p_eval_cal`, and not a near duplicate of a GE3 tune
  or F item, then continues the 0.50 walk in `p17d:admit` order up to 250 in total. Then the
  feasibility gate runs.
- `resolve_tier_policy() -> (path, basis)`: running-proxy argv `--tier-policy`, else the plist
  `ProgramArguments`; else `LLM_ROUTER_PROXY_TIER_POLICY` from the plist env, then
  `~/.llm-router/.env`; else `<live llm_router>/proxy/claude_tiers.yaml`. Read-only. It never
  invokes `doctor`.

**Shadow**
- `min_body(item) -> dict` and `check_assemble(item, body)`: rebuild a minimal body, then assert
  that `assemble` reproduces the stored pieces.
- `rules_tier(body, policy) -> str` (`policy` = `$RUN/tier_policy.yaml`, sha-checked against the pin): `tier_for(*_classify_class(tier_text(body),
  capabilities=False)).name`.
- `nimble_preflight()`: the digest check, the quiet gate, and one discarded synthetic warm-up.
- `shadow_one(sched, item, body, rules) -> source`: `maybe_schedule` then `drain`. It handles the
  `cold` wait and the timeout cooldown, with one retry.
- `resume_state(records, meta) -> todo`: reconciles by key, with the record file as the authority.
- Harness env: `LLM_ROUTER_ENGINE_MODE=shadow` and a scratch HOME. `rules_tier` uses
  `tier_for(...) or tier_of(requested)` (`tiers.py:1031`).

**Labelling**
- `judge_batch(items, ids, mode) -> labels`: builds the input from `shadowlab.scrub`,
  `gt.prompt_view` and `ITEM_TEMPLATE`/`BATCH_TAIL`, using the exact `claude -p` flags of
  `shadowlab.call_opus`. It checks the `modelUsage` family and parses with
  `gt.extract_json`/`gt.valid_label`. Retries: 3 per batch, then single items.
- `spend_guard(ledger, cap)` and `quota_guard()`.

**Scoring**
- `last_per_key(records)`: keeps the final attempt per `(session_id, text_sha)` before
  `shadow_eval.score`. This is the declared deviation from AMEND4 2(e).
- `score(records, labels) -> result`: `shadow_eval.score` for the arms. On top of it the function
  computes:
  - the AMEND4 verdict n (llm/cache only) and the share;
  - `boot_diff` for under-route;
  - the reference arms `always_opus` and `always_sonnet`;
  - the strata;
  - the error-excluded sensitivity;
  - per-group (F, G) blocks, and fresh-vs-GE3 label κ on G;
  - the verdict, evaluated in the prereg §7 precedence order (procedural, errors, n, share, then
    PASS or FAIL);
  - the wide/normal intervals when there are fewer than 30 sessions, and Wilson intervals.

## Tests (synthetic fixtures, no real text, no network)

1. The pins match the prereg.
2. The leakage rule catches exact, near (Jaccard 0.8) and (session, ts) duplicates, and passes a
   0.79 near-miss.
3. `min_body` round-trips `assemble` for each case: first prompt, three earlier prompts, a long
   assistant text (the 501 tail), and a prompt longer than 2,000 characters.
4. The selection cap never exceeds 0.50 at N ≥ 4, and the feasibility gate fires.
5. `shadow` refuses when labels exist, and `label` refuses before `done`.
6. `score` writes `started` before it opens labels, and refuses on a second run.
7. A verdict fixture for each outcome: PASS, FAIL, and NOT INFORMATIVE for each of n, share,
   errors, calibration and pre-freeze rows.
8. Nothing prints prompt text (capsys scan for fixture sentinels).
9. No open() goes under `~/.claude` or `~/.llm-router` except in `attach`, and `attach` never opens
   a path for writing outside `$PP`.
10. A single-change mutant of each verdict comparison turns red.
11. Admission: E2-cal-flagged and codex GE3 items never enter. G follows F in order. The trigger does
   not fire at 100 F items.
12. `resolve_tier_policy` respects the precedence order (fixtures for argv, plist env, .env and
   bundled).
13. A successful retry is the scored row (last record per key). A crash after the record append
   and before the meta write resumes without a third call.
14. Calibration: a wrong planted label writes `terminal`, and a resumed `label` never resends a
   planted id. A pause writes no use-log line.
15. `ENGINE_MODE=decide` in the environment is overridden; the test asserts
   `engine_door.decision` is never consulted.
16. Verdict precedence: the error and procedural NOT INFORMATIVE triggers beat PASS on a fixture
   that would otherwise PASS.

## Shadowlab pin: the fix design (not applied; `shadowlab.py` is unchanged)

**Defect.** `shadowlab.py` (sha256 `b89c7e17…`) hard-codes `GT500_SHA256 = c46d5d3c…` (line 45, the
AMEND3 pin). `load_gt500` (lines 165-171) refuses on any other hash. PREREG-GT-amend6 changed
`gt500.py` to `9cde774d…` and recorded it in `gt500/SHA256SUMS`. Since then every run exits with
"refusing" (`work/launchd.err.log`), so the live labeller has made **0 labels since 2026-10-13**.

**Evidence that a re-pin is safe.** `gt500.py.pre-ge3b-20261013` hashes to `c46d5d3c…` (the old
pin). An AST source comparison against `gt500.py` (`9cde774d…`) shows these functions
byte-identical: `load_rubric`, `render_context`, `prompt_view`, `extract_json`, `valid_label`,
`quota_check`, `call_claude`, `claude_records`, `claude_records_ctx`. Amend6 touched only
report/kappa fields.

**Rejected option: read the pin from `SHA256SUMS`.** That file is mode 0644, in the same directory as
`gt500.py` and written by the same hands. Whoever edits `gt500.py` can update it in the same step,
so the check would verify only self-consistency. That weakens integrity.

**Fix: pin the amend6 hash through a frozen amendment, and cross-check `SHA256SUMS`.**
1. Write `AMEND5-shadowlab.md` (0444; sha256 logged in PROGRESS.md) before any code edit. It names
   amend6, carries the evidence above, and has a pins block `GT500_SHA256=9cde774d…`.
2. Edit `test_shadowlab.py:157-159`: the pin overlay reads PREREG plus AMEND1-3 only, so add
   `AMEND5-shadowlab.md`. AMEND4's pins are informational (its §4) and name no module constant.
   The test overlays them under an explicit allowlist and asserts that none of their keys is a
   `shadowlab` attribute, so AMEND4 is read and cannot silently diverge. In `shadowlab.py`, change
   the constant to `9cde774d…`. In `load_gt500`, add a second, AND-ed
   check: `SHA256SUMS` must list `scripts/gt500.py` with the **same** hash, otherwise refuse with
   exit 2. A refusal stays fatal, and nothing falls back. The constant is the authority and
   `SHA256SUMS` can only add a refusal, so no path accepts a hash that is not frozen in an
   amendment.
3. Tests:
   * red on the old constant;
   * red when `SHA256SUMS` disagrees;
   * red when `gt500.py` differs from both;
   * green on the amend6 pair;
   * mutants: drop either check, which must turn a test red.
4. **Order.** Editing `shadowlab.py` changes its sha (today `b89c7e17…`), and the P1.7-d prereg
   pins that sha. So the fix lands first: AMEND5-shadowlab frozen, then the code and test change,
   then an independent review. Only after that is the P1.7-d prereg frozen, pinning the **new**
   sha. Then reload the plist for the live labeller. The `SHA256SUMS` agreement refusal stays.

## PROGRESS-ready note (attach after BASE)

The freeze-guard BASE for the prereg stays `44956ed1`. `scripts/p17d_attach.py` changed after BASE
(review fixes: parsed UTC timestamps, refusals on a missing denylist or overrides, on zero
transcripts and on zero corpus matches, real-home session kind, llm_router provenance, served model
bounded by the next prompt). The sha256 that gets pinned at the freeze is the **final reviewed** file's
sha256, the one in the commit attach runs from, not the BASE one.
