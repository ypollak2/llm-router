# LEARNING_SYSTEM.md

What the system learns, from what evidence, with what safeguards.

**The governing constraint:** at **13 episodes/week**, this is a low-data system
permanently. Anything requiring hundreds of samples per cell is research, not a
deliverable. Everything below is sized against that number.

---

## 1. Four learners, ranked by achievability

| Learner | Evidence needed | Reachable? |
|---|---|---|
| **Workflow conventions** (§5, §6) | 5–20 observations | **This quarter** |
| **Anti-patterns** (§21) | 3–6 observations + harm measurement | This quarter |
| **Project knowledge** (§2) | Zero — it is derived from the AST | Immediately |
| **Capability/outcome** (§4) | Hundreds per cell | **Not this year** |

This ordering is the single most important design decision in the document, and
it is the inverse of the brief's. Shipping them together means shipping neither.

---

## 2. Project knowledge — derived, not learned

Nothing is inferred. The import graph is parsed from the AST; `evidence='ast'`
is a fact. Concept groupings (`TokenManager belongs_to Authentication`) are
`evidence='heuristic'` and never mixed into the same confidence.

**Safeguard:** an entity whose `content_hash` no longer matches the working tree
is **stale, not wrong** — excluded from retrieval and flagged, never silently
used. Branch/version fields prevent a fact learned on one branch asserting
itself on another (§24).

---

## 3. Workflow conventions

### Evidence source (exists)

`scripts/groundtruth/sources.py` already yields real prompts with documented
drop rules — n=1571 after removing 1,389 system-noise, synthetic-session,
benchmark-sandbox and too-short records. **Do not re-implement those rules**;
two ad-hoc parsers of this repo's traffic have already disagreed.

### Detection

```
1. Extract the procedural clause from each intent
   ("implement it in a loop, run tests and audit it")
2. Embed (Ollama nomic-embed-text — already present, no new dependency)
3. Cluster by cosine within a scope
4. A cluster with ≥3 members across ≥2 DISTINCT SESSIONS → candidate
```

**Distinct sessions, not distinct prompts.** Three rephrasings in one frustrated
afternoon is one observation, not three. This is the cheapest guard against
learning a mood.

### Promotion

```
observed(1) → candidate(3, ≥2 sessions) → suggested(5) → accepted(explicit act)
            → default(auto-applies) → demoted(2 consecutive overrides)
```

Never auto-promote past **suggested**. Accepted requires a human act — enforced
in the schema, not in prose.

### The honest gate for this feature

> Run detection over the existing transcripts. Does it surface the convention
> the user knows is there, with ≤1 false candidate?

If it cannot, the feature does not work, regardless of how good the schema is.
This is a falsifiable test that can be run before building the rest.

---

## 4. Learning from corrections (§20)

Human correction is the highest-value signal and currently **zero is captured**:
the `corrections` table has 9 wired writers and 0 rows.

| Correction | Becomes | Not |
|---|---|---|
| "Always run an audit before finalizing" | a **hard** convention | a model preference |
| "Don't use Opus for documentation" | a **model preference** for that node kind | a workflow change |
| Cancelling an auto-applied convention | `episode_event(kind='override')` | silence |
| Re-running a task differently | a negative example for the first strategy | |

**Workflow preference and model preference are separate stores** (§20 requires
it). A user who dislikes Opus for docs has said nothing about whether docs
should exist in the workflow.

**An explicit instruction outranks everything learned** (see precedence in
`KNOWLEDGE_MODEL.md` §3). A learned convention that contradicts a live
instruction is not a conflict — it is simply overridden, and the override is
recorded as evidence against it.

---

## 5. Negative learning (§21)

An anti-pattern requires **evidence of harm**, not absence of benefit:

```
occurrences ≥ 3
AND cost_ratio  = tokens_median / baseline_tokens_median  ≥ 3
AND verdict_delta = pass_rate(with) − pass_rate(without)  ≤ 0
```

`verdict_delta ≤ 0` is load-bearing. Without it, "this used a lot of tokens" is
not evidence of an anti-pattern — it may have been the reason the task
succeeded. This is the same discipline as the repo's existing rule that a
saving claim needs tokens-before, tokens-after, same task, **scored for
correctness both ways**.

---

## 6. Capability/outcome — deferred, with a stated entry condition

**Do not start until:**

> ≥ 200 episodes with a real `verdict`, spread over ≥ 3 models, with ≥ 30 in
> each of ≥ 4 `(node_kind, model)` cells.

At 13 episodes/week that is not soon. **That is a finding, not a failure** — the
MVP delivers §28's scenario without it.

When it arrives: Beta-Bernoulli per cell with a shared prior across scopes,
ranked by Wilson lower bound. Not a neural ranker; not a contextual bandit. The
data will not support either, and a model that fits noise while looking
authoritative is worse than an abstention.

---

## 7. Safeguards against incorrect learning

Each maps to a failure this repo has already had.

| Safeguard | The incident behind it |
|---|---|
| **Refuse to rank below n=30** | A rate without its denominator: days with 21–64 prompts produced 2.5%/1.6%/0%/4.3%, all noise, all briefly reported as a collapse |
| **`cost_measured` / `unmeasured_rows` travel with every aggregate** | 1,387 of 1,601 rows carry a flat $0.01 placeholder |
| **Three-valued verdict; UNCERTAIN never collapses** | S9: a NULL confidence coerced to 0.0 produced 213 "High"-confidence `CLASSIFIER_ERROR` findings from a column nobody wrote |
| **Exclude, never delete, superseded evidence** | Deleting loses the negative signal that made it superseded |
| **Contradictions retained and surfaced, not averaged** | Averaging hides the only thing worth seeing |
| **Model version is part of the key; a new version starts at n=0** | §24. The most commonly skipped part of "forgetting" |
| **A convention's `confidence` and a model's `pass_rate` are separate fields** | §29's "usually does this" ≠ "performs better" |
| **Counterfactual recorded per node** | 87% of history is two models; without the rejected set, the bubble is unfixable retroactively |
| **Detection must be proven on a known positive** | A filter that drops nothing has not been shown to work — the near-duplicate stage once reported "0 collapses" while comparing raw whitespace splits |
| **Learning reads only the episode log** | One path in. No route by which a model's opinion becomes training data |

---

## 8. The observer-effect rule

Reading a learned value must not change it, and using a convention must not
increase its own confidence.

> A convention that auto-applies, succeeds, and counts that success as fresh
> evidence for itself is a self-reinforcing loop with no external input.

Confidence rises only from **independent observations** — the user asking for
that workflow again unprompted, or an override not happening when the user had
a clear opportunity to intervene. A successful auto-application is evidence that
the *workflow* worked; it is **not** evidence that the *convention was the right
one to select*. Those are different claims and the schema keeps them apart
(`convention.confidence` versus `capability_outcome.pass_rate`).

This is the §29 bubble in its subtlest form, and the easiest one to build by
accident.
