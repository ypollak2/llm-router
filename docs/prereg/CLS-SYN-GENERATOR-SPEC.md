# CLS-SYN-GENERATOR-SPEC: frozen synthetic Claude-Code-style prompt generator (PLAN v16 CLS-v16, D-130 a)

Spec version: 1 (draft r3, after the r1/r2 freeze-guard reviews and D-132, D-134, D-135). Status: DRAFT, not frozen. Frozen when `CLS-SYN-GENERATOR-SPEC.sha256` exists beside
it after an independent freeze-guard review says READY. Generation may start only after the freeze and after the
DEV manifest exists (the manifest's length-band counts file is an input; no counts file, no generation).
**r3 order (D-135):** DEV-SYN iterates alone (at most 3 spec versions, only DEV-SYN numbers visible); the first
passing version is frozen as vN; **TEST-SYN is then generated once under vN**, before any DEV screening, by a
separate agent, with one regeneration allowed on its own failure (section 8.3, section 12).

Purpose. Produce DEV-SYN (n = 300) and TEST-SYN (n = 400): synthetic prompts that look like Claude Code
turns, each with a short synthetic context digest, so that learned tier classifiers can be screened (DEV) and
scored once (TEST) when no fresh real set of the needed size exists (design finding 6: organic supply is
about 22-40 eligible per week). Truth is made later by judges (section 9 of the TEST prereg and section 3 of
PREREG-CLS-DEV). **Intent is not truth**: the intended difficulty below steers generation only and is never
used as a label.

## 1. Hard rule: no prompt is derived from a specific real prompt

Enforced by construction, then checked:

1. **The generator never receives real text.** Its inputs are only (a) the cell vector of one item (section
   2), (b) the seed and the per-item nonce, and (c) the frozen prompt templates of section 5, whose style
   descriptors are aggregate statistics (length band, mood, sentence count, share of file references),
   never quotations. The spec file contains no real prompt text and no example copied from one; this was
   written by the spec author from the stratum definitions alone.
2. **No filesystem or network access during generation.** GPT: `codex exec` in a read-only sandbox with an
   empty scratch directory as cwd (no repo, no DEV files). Opus: `claude -p` with `--tools ""`,
   `--strict-mcp-config`, an empty cwd and no project settings. The generator process never gets a path to
   `$PP`, the corpus, the ledgers or the DEV manifest.
3. **Request audit log.** Every generation request is written to an append-only `gen_requests.jsonl`
   (call id, template sha256, cell vectors, seed, nonce, model id, timestamp). The log contains no
   real-prompt-derived field. The freeze-guard reviewer (and the later verifier) can check that every
   request body is `template(cell vectors, nonce)` and nothing else; the bodies are reproducible from the
   log.
4. **Length-band and tier targets use aggregate counts only.** Section 3 takes the length-band shares
   from the counts-only DEV manifest (words per item bucketed to 4 bands). An aggregate share is not a
   prompt. No per-item attribute of a real prompt reaches the generator.
5. **Post-hoc leakage check** (dedup vs DEV, section 7) finds any accidental near-copy: exact `text_sha` or
   5-shingle Jaccard >= 0.8 against any DEV item (real or DEV-SYN) drops the item. More than 3 percent hits
   of a set means the generator is generic or memorising and is reported; the set stays INFORMATIVE only if
   dropped items are replaced under section 7.3.
6. **Generator call hygiene.** Scratch config, no hooks: `claude -p` runs with `--safe-mode`, `--settings
   <run-dir>/claude_settings.json` holding `{"disableAllHooks": true}`, `--tools ""`, `--strict-mcp-config`,
   `--no-session-persistence`, `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` removed from the child
   environment, an empty scratch cwd, the prompt on stdin (the P2.0 pattern). `codex exec` runs `-s read-only`,
   in an empty cwd, prompt on stdin, after the Codex read-scope canary of PREREG-CLS-DEV section 3.2 (the generator
   needs no file access, so a canary that shows the model can read outside the cwd parks the run). Every
   generator request goes through `outgoing_check` on its exact bytes (the cell vectors, template and nonce hold no
   secret, but the check runs and its self-test is part of PINS).
7. **The generator models never labelled or saw DEV in this workflow.** The GPT used for generation and
   the GPT used for relabelling are separate processes with separate scratch dirs; no relabel prompt is
   reused as a generation prompt.

## 2. Strata

An item is the cell vector (task type, length band, tool need, project, intended difficulty, ack flag).
Sampling is by independent marginals (section 3) then rebalanced to the quotas; the joint is not
enumerated (about 12 x 4 x 5 x 6 x 3 cells is more than 4,000, far above n).

### 2.1 Task type (12)

| Id | Meaning |
|---|---|
| T01 | bug fix with a described symptom |
| T02 | new feature or small addition |
| T03 | refactor or rename |
| T04 | write or fix tests |
| T05 | debug a failure (error, test output, stack trace described) |
| T06 | review a diff or PR |
| T07 | explain code or a concept (Q&A) |
| T08 | configuration, CI, build, dependency, devops |
| T09 | git, branch, release, PR housekeeping |
| T10 | research or compare options (may need the web) |
| T11 | docs, comments, commit text, changelog |
| T12 | data, analysis, measurement, prereg or plan editing |

Plus the **ack flag** (about 10 percent of items, **[CD]**): a terse referential follow-up ("go ahead",
"fix that", "same for the other one" in spirit; the generator writes its own wording) whose meaning lives
in the context digest, to cover the terse real turns the design flags as the proxy gap. An ack item carries
a task type like any other (the type of the work the referent implies).

### 2.2 Length band (4), words

| Band | Words | Share rule |
|---|---|---|
| L1 | <= 12 | target = real DEV share of that band, rounded to the nearest 5 points |
| L2 | 13-50 | same |
| L3 | 51-150 | same |
| L4 | > 150 | same |

The real DEV shares come from the **manifest's length-band counts file** (aggregate counts only). **There is no
provisional-shares fallback**: the generator refuses to start without that file. The used shares are written into
`gen_config.json` (hashed) before generation starts; a later change is a new spec version. A generated item's band is **measured** (whitespace words), not trusted: an item outside
its requested band by more than 25 percent of the band bounds counts as a band miss; misses are reported
and the item stays in its measured band.

### 2.3 Tool need (5)

| Id | Meaning |
|---|---|
| N0 | none: answerable from general knowledge |
| N1 | read-only repo: needs to read files or search the repo |
| N2 | edit: needs to change files |
| N3 | run: needs to execute commands or tests |
| N4 | web: needs outside information |

Joint weights: N1 30, N2 30, N3 20, N0 12, N4 8 percent **[CD]**, then constrained by task type (T07 is
mostly N0/N1, T09 mostly N3, T10 mostly N4/N0; the fixed 12 x 5 compatibility mask is Appendix A (hashed in PINS-STATIC (v1)),
incompatible pairs have weight 0).

### 2.4 Project (6)

Names only, no content: the six owner repo names of Appendix B (confirmed at freeze, hashed in PINS-STATIC (v1)) plus one
"unnamed/other" value for 15 percent of items where the prompt names no project. Equal weight across the six.

### 2.5 Intended difficulty (3), steering only

Intended tier haiku / sonnet / opus, weights **20 / 40 / 40** **[CD]**. Source of the weights: the P17D truth
mix implied by the always_haiku and always_sonnet under-route rates (80.8 and 41.7 percent, design
section 1: roughly 19 / 39 / 42 percent haiku / sonnet / opus). Intent is an instruction to the
generator ("a task a small model would finish", "a task that needs careful multi-file reasoning"), not a
label, and no later step reads it as truth. A resemblance check (section 8) compares the **judged**
truth shares, not the intent.

## 3. Quotas and counts

| Set | n | Seed (allocation) | Seed namespace for nonces | Generation calls | GPT calls | Opus calls |
|---|---|---|---|---|---|---|
| DEV-SYN | 300 | 20261017 | `devsyn` | 60 (5 items per call) | 42 (70 percent) | 18 (30 percent) |
| TEST-SYN | 400 | 20261018 | `testsyn` | 80 (5 items per call) | 56 (70 percent) | 24 (30 percent) |

* The 5 items of a call are drawn from different cells and are the cluster unit for bootstrap and folds
  (`group` = call id); items in a call share generator, model state and style drift.
* Allocation: for item k of set S, draw (task type, length band, tool need, project, difficulty, ack) from
  the marginals with a `random.Random(seed)` stream in item order; apply the compatibility mask by
  rejection (fixed cap 50 draws, then the nearest valid cell). Then rebalance by swapping cells between
  items so that every task type has n/12 +- 2 items and every length band is within 2 items of its quota.
  The allocation table (cell vector per item id) is hashed before any generation call.
* Call assignment to GPT or Opus: calls ordered by `sha256("clssyn:call:" + seed + ":" + call_index)`; the
  first round(0.7 x calls) are GPT. Items in a call all come from one generator.
* Nonce per item: `sha256(namespace + ":" + item_index)` first 12 hex characters, inserted into the
  request so two requests are never textually identical. Model sampling seeds are not controllable on either
  CLI; reproducibility is **frozen outputs with sha256**, not regeneration. This is stated.
* DEV-SYN and TEST-SYN use different seeds, different namespaces, different call sets. No item is shared.

## 4. Synthetic context digest

Each item carries a digest, at most 400 characters, in a fixed schema produced in the same call:

`{"project": <name|null>, "turn": <first|follow_up|plan_approval|after_error|after_test_run>,
"files_touched": <0-6>, "last_tool_outcome": <none|ok|error|test_fail>, "branch_state": <clean|dirty|pr_open>,
"referent": <short phrase for an ack item or null>}`

The digest feeds `ctx_digest` of the engine and the judge's context view. Fixed digest facts describe a
scene; they do not state the difficulty. The generator must not write the intended tier or any rating
into the prompt or the digest (a check in section 6 rejects items that make a **meta-statement about difficulty or routing**, by the fixed
patterns of Appendix D: the model-tier names as words, "difficulty", "tier", and "easy/hard/trivial/simple/
complex/difficult" directly followed by "task/one/prompt/request/job", and "complexity level/score/rating". The
list is deliberately narrow so that ordinary developer wording ("a simple fix", "this is hard to reproduce") is not
rejected, which would bias the set toward bland prompts. **Residual:** the filter can still pass difficulty cues carried by
ordinary words, and it can reject a few legitimate phrasings; this is reported, not corrected. The rate of every pattern, by generator family and by
intended difficulty, is **reported** so the rejection bias is visible.)

## 5. Generator prompts (frozen templates)

Two templates, `tmpl_gpt.txt` and `tmpl_opus.txt`, identical in content apart from the wrapper each CLI
needs; the body is Appendix C and both files get sha256 recorded in `gen_config.json` and PINS. Content, in order:

1. Role: write realistic prompts a developer types into a coding agent, one per cell vector listed.
2. The cell vector of each item as plain field lines (task type meaning, band bounds in words, tool need
   meaning, project name or none, intended difficulty phrase, ack flag).
3. Style rules: human-typed, imperfect, lowercase allowed, no quotation marks around the whole prompt, no
   numbering of items inside the prompt, no mention of tiers, models, cost or rating, no code fences unless
   the band is L3 or L4 and the task type is T01-T05, no secrets of any shape (fake or not), no real
   people or companies, no URLs except `example.com`.
4. Output schema: JSON lines, one per item, `{"idx", "prompt", "digest"}`; nothing else.
5. Diversity rule: items in a call must differ in vocabulary and sentence shape.

The same template, same cell vectors and same nonces go to both generator families only through the 70/30
call assignment; no call is generated by both.

## 6. Acceptance filters (applied per item before the item is counted)

1. Parse: valid JSON; prompt and digest present; digest within schema and 400 characters.
2. Meta-statement patterns (section 4, Appendix D) and the secret detector of PREREG-CLS-DEV section 3.2 (any hit drops the item).
3. Band check (section 2.2): measured band recorded; band miss counted, not dropped.
4. Dedup (section 7).
5. A dropped item is replaced by one fresh item for the **same cell vector** from a fresh call whose
   nonce is the next index in the namespace, up to a cap of 20 percent of n; beyond that cap the shortfall is
   reported and the set is smaller. Replacement count and reasons are reported (no text).

## 7. Dedup and leakage

1. DEV-SYN against all real DEV items (the manifest `text_sha` plus shingle sets): exact + 5-shingle
   Jaccard >= 0.8 (the `supply.py` rule).
2. TEST-SYN against **all** real DEV, against DEV-SYN, and against any real item that exists in the repo's
   labelled sets. Same rule.
3. Within a set: pairwise Jaccard >= 0.8 inside the set drops the later item.
4. Report counts per rule, per generator family; more than 3 percent hits in a set is a flag (section 1.5).

## 8. Resemblance checks (TEST-SYN and DEV-SYN against real DEV)

All thresholds are frozen here. Computed on text and measured quantities only until labelling exists.

### 8.1 Text-only checks (before any label; run on both sets before any label exists)

(a) **Discriminators (two; both must pass).** Each separates the synthetic set from **all** real DEV prompts, with
balanced class weights (no subsampling), session/call-grouped 5-fold with `group` as in the DEV prereg.
 (a1) char-n-gram (2-5, TF-IDF, sublinear tf) logistic regression, C = 1.
 (a2) **embedding + logistic regression**: `bge-small-en-v1.5` embedding (the pinned snapshot), L2-normalised,
 logistic regression C = 1. This is the stronger discriminator: it sees semantics and style, not only characters.
 **Gate: pooled out-of-fold AUROC <= 0.75 (D-130 a) AND the 95 percent group-bootstrap CI upper bound <= 0.80
 [CD]**, for each of a1 and a2. A point estimate under the bar with a CI that reaches 0.85 is not a pass.
 The tool prints pass/fail and the AUROC with CI only (section 12).
(b) **Length-band shares** of the synthetic set within **10 points** of the real DEV shares in every band.
(c) **Length distribution**: median words within 30 percent of real DEV's median, reported
(a drift check, not a gate). **[CD]**
(d) **Quota fidelity**: every marginal quota of section 3 met within the stated tolerance; band misses at
most 15 percent of items.

### 8.2 Truth-dependent checks (after labels)

(e) **Truth-tier shares** (judged, T-primary of the relevant set) within **15 points** of real DEV
T-primary shares in each tier.
(f) **rules_eff under-route** on the synthetic set within **+-10 points** of its real-DEV value (real-DEV
about 40 percent on P17D).
(g) **n_truth_opus >= 80** on TEST-SYN (at 42 percent opus share n = 400 gives about 170 [EST]); below 40,
NOT INFORMATIVE; 40-79, "low power" label.

### 8.3 Consequence (r3, D-135)

* **DEV-SYN iterates alone.** DEV-SYN is generated under spec v1 and run through the text-only checks (a), (b), (d).
  If any fails, spec v2 may change strata weights, templates or filters, **using only the pass/fail results and
  the AUROC numbers of DEV-SYN** (no TEST-SYN exists yet). **No discriminator feature, weight, top term, error case
  or real text is shown to the amendment author.** At most **3 spec versions in all** (v1, v2, v3); each version
  and each DEV-SYN set is hashed and logged. The first version that passes is **frozen as vN**.
* **Pins.** The v1 generator files are in `PINS-STATIC.json` (frozen with this spec). Every amendment (v2, v3) and
  every `gen_config.json` writes a new **`PINS-RUN`** entry (hashed, logged before use, never overwritten); the
  frozen vN's files are pinned there before TEST-SYN is generated.
* **TEST-SYN is generated once under vN**, by the TEST-SYN agent, before any DEV screening. If it fails its own
  text-only checks (a), (b), (d), **one** regeneration under the same vN is allowed (no further amendment, no
  seed hunting beyond the next call-index nonces), before any TEST label exists and before any candidate is
  scored. If the second TEST-SYN also fails, or any of (e), (f), (g) fails after labelling, TEST-SYN is
  **NOT INFORMATIVE as a proxy**: no candidate verdict, no regeneration, no re-label. A replaced TEST-SYN is a
  **discarded set**: the regeneration command itself calls `discard` on the previous set (rename + chmod 0000),
  and it is hashed, never read, never scored, never used to choose anything.
* **If no DEV-SYN version passes within 3:** screening proceeds with DEV-SYN labelled `RESEMBLANCE_FAILED`
  (reported with that label; it carries no weight in criterion 6), and TEST-SYN is declared **NOT INFORMATIVE as a
  proxy** without being generated.
* **Non-independence, stated.** An amended spec is tuned until it passes the same checks that then certify it.
  A pass shows the spec was made to pass, not that the set resembles traffic in every respect; the checks cannot
  be read as independent validation.

## 9. What makes TEST-SYN not informative

TEST-SYN is NOT INFORMATIVE (no verdict on any candidate, recorded as such) if any of these holds:

1. a resemblance check of section 8 fails after the allowed regeneration, or the discriminator AUROC > 0.75;
2. any judge's calibration fails (the first-call 6/6 or any interleaved plant, as PREREG-CLS-DEV section 3.3; the TEST prereg adopts the same rule);
3. the known-better arm is not separated from the known-worse arm on TEST-SYN (D-130 e);
4. `n_truth_opus` < 40, or truth is missing for more than 5 percent of items;
5. any TEST-SYN item is a leak against real DEV or DEV-SYN (a hit that survived section 7);
6. the generator spec, templates or allocation table sha differ from the frozen values;
7. thresholds or candidate artifacts changed after TEST-SYN labels exist, or labels were not hashed
   before the first candidate ran;
8. an error or fallback rate above 10 percent for a candidate (that candidate only);
9. more than 20 percent of items replaced (section 6.5), or shortfall that leaves n below 360.

Also stated in every output (the proxy-to-target gap): synthetic prompts are cleaner, longer on average and
less context-dependent than terse real turns; truth is a rubric label with kappa to outcome 0.096; a
generator that writes in GPT or Opus style is a style the candidates and judges may find easy. A TEST-SYN
PASS licenses the shadow canary only (D-130 a). It is not evidence for unattended authority.

Per-stratum tables are published for every arm: length band, truth tier, task type, tool need, generator
family (GPT vs Opus), contested vs agreed. The score is never a mean alone.

## 10. Reported at generation time (counts only)

Items generated, dropped and replaced per reason; per generator family counts; band measured vs requested
matrix; digest schema validity; discriminator AUROC with CI; length-band shares; sha256 of the allocation
table, `gen_config.json`, both templates, `gen_requests.jsonl`, and each set's `items.jsonl` (the item files
themselves hold text and stay 0600 outside the repo; the repo gets the hashes and counts).

## 12. Separation and custody (D-132 b, D-135)

* The **TEST-SYN agent** generates and hashes TEST-SYN. It never sees finalists, thresholds, DEV labels or real text.
  Its allocation table, `gen_requests.jsonl` and items-file hashes are **committed to git** before DEV screening
  starts.
* Storage: `$CLS/test-syn/`, items file mode 0600, directory 0700, **outside every DEV tooling path**. No DEV tool
  takes a path under it (the B0 path guard refuses it, tested).
* The **resemblance tool** (`cls_resemble.py`, build B-L) runs on TEST-SYN **inside the TEST-SYN agent's own step**.
  It reads TEST-SYN and the real DEV text and prints **only** pass/fail and AUROC with CI (no features, terms,
  weights, errors or text); it is the only tool that reads TEST-SYN text before release. Immediately afterwards the
  items file is made **chmod 0000**.
* Release: **the coordinator holds the release token.** After the finalists file is hashed, the coordinator
  releases TEST-SYN to the TEST prereg, whose three-judge labelling happens before any finalist is scored.
  Same Unix user: the control is procedural plus the path guard and the file mode (stated residual).
* The TEST prereg must report a **real-only comparator and a per-source breakdown**.

## 13. What was read when writing this spec

Read: `cls-v16-design.md`, `cls-v16-candidates.md`, PLAN-v16 decisions D-127..D-132, `BUILDER-RULES.md`,
`PREREG-P17D-synthetic.md` (structure, label rules), `PREREG-P20-requal.md` sections 6 and 13 (hygiene, night window),
`src/llm_router/shadow_eval.py`, `engine.py`, `persist_redaction.py`, `secret_scrubber.py` (names and shapes), the
counts in `gates/v16/GE3.json` and `P1.json`, a directory listing of the owner's Projects directory (names only),
the cached bge-small snapshot directory name and `sysctl hw.memsize`. **Not read:** any real prompt text, any
prompt-derived directory, `~/.codex`, `~/.llm-router`, `~/.claude`. The spec's numbers come from those counts and the design, not from real prompts.

## 14. Coordinator defaults used in this spec

| # | Default |
|---|---|
| G-1 | 5 items per generation call, call = cluster unit; 60 and 80 calls |
| G-2 | length shares from the manifest counts file rounded to 5 points; no provisional fallback |
| G-3 | ack flag share 10 percent |
| G-4 | intended difficulty weights 20/40/40 |
| G-5 | tool-need weights 30/30/20/12/8 with the Appendix A task-type mask |
| G-6 | project list = Appendix B, confirmed at freeze |
| G-7 | replacement cap 20 percent; shortfall floor n >= 360 for TEST-SYN |
| G-8 | one regeneration of TEST-SYN allowed before labels exist |
| G-9 | median-length drift is reported, not gated |
| G-10 | seeds 20261017 (DEV-SYN) and 20261018 (TEST-SYN) |
| G-11 | discriminator gate: AUROC <= 0.75 and CI upper <= 0.80, for a1 and a2 |

## Appendix A. Task-type by tool-need compatibility mask (12 x 5; 1 = allowed, 0 = weight 0)

| | N0 | N1 | N2 | N3 | N4 |
|---|---|---|---|---|---|
| T01 bug fix | 0 | 1 | 1 | 1 | 0 |
| T02 feature | 0 | 1 | 1 | 1 | 0 |
| T03 refactor | 0 | 1 | 1 | 1 | 0 |
| T04 tests | 0 | 1 | 1 | 1 | 0 |
| T05 debug a failure | 1 | 1 | 0 | 1 | 1 |
| T06 review | 1 | 1 | 0 | 1 | 0 |
| T07 explain | 1 | 1 | 0 | 0 | 1 |
| T08 config/devops | 0 | 1 | 1 | 1 | 1 |
| T09 git/release | 0 | 1 | 0 | 1 | 0 |
| T10 research | 1 | 1 | 0 | 0 | 1 |
| T11 docs/commit text | 1 | 1 | 1 | 0 | 0 |
| T12 data/analysis/plan editing | 0 | 1 | 1 | 1 | 1 |

Within an allowed cell the tool-need weights of section 2.3 are renormalised over the allowed columns. The file
form is `task_tool_mask.json`, hashed in PINS-STATIC (v1).

## Appendix B. Project list (names only; disclosed to the generators and judges)

`llm-router`, `rsi-engine`, `stockagent`, `callrec`, `loophole`, `agenticgraphs`, plus `null` (15 percent of
items). The names come from a directory listing of the owner's Projects directory at the time of writing; the
freeze-guard reviewer confirms the list at freeze. Hashed in PINS as `projects.json`.

## Appendix C. Generator template (both families; wrapper differs only as the CLI needs)

```
You write realistic messages a developer types into an AI coding agent. For each item below write ONE message
and ONE context digest.
Items (one block per item):
  idx: <n>
  task: <task type meaning>
  length: <band bounds> words
  tool need: <meaning>
  project: <name or none>
  situation: <intended-difficulty phrase, written as a scene, never as a rating>
  follow-up: <yes/no; if yes the message refers to something in the digest>
  nonce: <12 hex>
Rules: human-typed, imperfect, lowercase allowed; no quotation marks around the whole message; no numbering of
items inside a message; never mention models, tiers, cost, rating or difficulty; code fences only for bands 51+ and
task types 1-5; no secrets of any shape, real or fake; no real people or companies; URLs only at example.com;
items in this call must differ in vocabulary and sentence shape.
Digest schema (JSON, <= 400 chars): {"project","turn":first|follow_up|plan_approval|after_error|after_test_run,
"files_touched":0-6,"last_tool_outcome":none|ok|error|test_fail,"branch_state":clean|dirty|pr_open,"referent":str|null}
Output: one JSON object per line: {"idx","prompt","digest"}. Nothing else.
```
Intended-difficulty phrases (frozen): haiku-intent "a small, well-bounded change that one careful read settles";
sonnet-intent "a normal multi-step change in one area of the code"; opus-intent "a change that needs careful
reasoning across several files or an unclear root cause". Hashed in PINS as `tmpl_gpt.txt` / `tmpl_opus.txt` (the
body above plus the CLI wrapper) and `intent_phrases.json`.

## Appendix D. Meta-statement patterns (case-insensitive)

`\b(haiku|sonnet|opus)\b`; `\bdifficulty\b`; `\btier\b`; `\b(easy|hard|trivial|simple|complex|difficult)\s+(task|one|prompt|request|job)\b`;
`\bcomplexity\s+(level|score|rating)\b`. Hashed in PINS as `meta_patterns.json`. Per-pattern hit rates are reported.
