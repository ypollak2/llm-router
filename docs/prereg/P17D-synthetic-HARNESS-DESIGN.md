# P1.7-d synthetic harness: design (not built)

Implements `PREREG-P17D-synthetic` (DRAFT). There is no code yet. It is built only after the prereg
is frozen. A pins test reads the prereg `pins` block and fails if a constant differs (the
`test_shadowlab.py` pattern).

## Files

| Path | Mode | Holds text | Written by |
|---|---|---|---|
| `scripts/p17d_synthetic.py` | repo | no | (the harness) |
| `tests/test_p17d_synthetic.py` | repo | no (synthetic fixtures) | - |
| `$RUN = $PP/v16/p17d/` | 0700 | - | `attach` creates it |
| `$RUN/attach_counts.json` | 0600 | no | `attach` (exclusion counters, pool n, sessions, largest share) |
| `$RUN/items.jsonl` | 0600 | **yes** | `attach` (§2 fields) |
| `$RUN/selection.json` | 0444 after write | no | `select` (item ids, bound to sha256 of items.jsonl; gate result) |
| `$RUN/scratch/` | 0700 | no | `shadow` (scratch HOME / LLM_ROUTER_HOME) |
| `$RUN/classifier_shadow.jsonl` | 0600 | no | `shadow`, via the production `ShadowScheduler._append` |
| `$RUN/shadow_meta.json` | 0600 | no | `shadow` (nimble digest, quiet gate, warm-up ms, errors and retries, `done` marker) |
| `$RUN/labels.jsonl` | 0600 | no | `label` (`{text_sha, session_id, truth}`) |
| `$RUN/judgements.jsonl` | 0600 | yes (judge reasons) | `label` |
| `$RUN/ledger.jsonl` | 0600 | no | `label` (per call: ts, ids, cost, tokens, model_ok, secs) |
| `$RUN/use.log` | 0600, append-only | no | `score`, first action |
| `$RUN/result.json` | 0600 | no | `score` (full) |
| `docs/measurements/p17d-synthetic.json` | repo | no | `score` (item ids, session indices, tiers, verdict, `synthetic (D-99)`) |

## CLI (`python3 scripts/p17d_synthetic.py <cmd>`)

Every command except `attach` and `status` refuses unless
`check_prereg.check(PREREG)["match"]` is true and `frozen_utc` < now. Stdout carries counts and ids
only.

| Command | Reads | Calls | Refuses when |
|---|---|---|---|
| `attach` (owner, TTY) | corpus, `~/.claude/projects` (top-level files only), `~/.llm-router/proxy_calls.jsonl`, `denylist.txt`, exec_sessions, the leakage sets | none | `$RUN/items.jsonl` exists; any pin sha differs |
| `select` | items.jsonl | none | selection.json exists |
| `shadow` | items, selection | Ollama localhost only | gate failed; labels.jsonl exists; nimble digest differs; quiet gate fails (PARKED) |
| `label [--mode batch\|single]` | items, selection, shadow_meta (`done` only) | `claude -p` (Opus) | shadow not done; calibration < 6/6; spend guard; quota pause (exit 3) |
| `score` | records, labels | none | use.log already has `started` |
| `status` | counts files | none | never; it prints aggregates only |

## Functions

**Attach and selection**
- `load_pinned(path, sha)`: imports gt500, supply and shadowlab by path after a sha256 check, and
  the rubric through `gt.load_rubric`.
- `leak_index(sets) -> (norm_map, long_shingles, sid_ts)` and `is_leak(text, sid, ts)`: the
  supply.py norm, exact match, 5-character Jaccard ≥ 0.8, and the (session, ts) match.
- `attach(corpus, projects, ledger) -> counts`: runs the frozen record rules, the exact-text join to
  the corpus, eligibility, leakage, the window cut, and the organic kind. It builds the full body
  and stores the `assemble` pieces, `requested` and its basis, and the served tier.
- `select(items, seed, n, max_share) -> ids`: a shadowlab-style capped walk, followed by the
  feasibility gate.

**Shadow**
- `min_body(item) -> dict` and `check_assemble(item, body)`: rebuild a minimal body, then assert
  that `assemble` reproduces the stored pieces.
- `rules_tier(body, policy) -> str`: `tier_for(*_classify_class(tier_text(body),
  capabilities=False)).name`.
- `nimble_preflight()`: the digest check, the quiet gate, and one discarded synthetic warm-up.
- `shadow_one(sched, item, body, rules) -> source`: `maybe_schedule` then `drain`. It handles the
  `cold` wait and the timeout cooldown, with one retry.

**Labelling**
- `judge_batch(items, ids, mode) -> labels`: builds the input from `shadowlab.scrub`,
  `gt.prompt_view` and `ITEM_TEMPLATE`/`BATCH_TAIL`, using the exact `claude -p` flags of
  `shadowlab.call_opus`. It checks the `modelUsage` family and parses with
  `gt.extract_json`/`gt.valid_label`. Retries: 3 per batch, then single items.
- `spend_guard(ledger, cap)` and `quota_guard()`.

**Scoring**
- `score(records, labels) -> result`: `shadow_eval.score` for the arms. On top of it the function
  computes:
  - the AMEND4 verdict n (llm/cache only) and the share;
  - `boot_diff` for under-route;
  - the reference arms `always_opus` and `always_sonnet`;
  - the strata;
  - the error-excluded sensitivity;
  - the verdict and the NOT INFORMATIVE reasons.

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
9. No open() goes under `~/.claude` except in `attach`.
10. A single-change mutant of each verdict comparison turns red.
