# KNOWLEDGE_MODEL.md

Schemas for the four knowledge systems the brief names — collapsed to **three
stores**, because §3 (Experience) and §4 (Capability/Outcome) are the same
events at two aggregations. Storing them twice guarantees they disagree.

All SQLite or YAML. No graph database, no vector database (§30).

---

## 0. The keystone: the Episode

Everything in §3, §4, §11, §16, §19, §20, §21 and §29 is a query over this.
Nothing else is worth building first, because **no table today has
`parent_task_id`, `workflow_id` or `node_id`** — every one of the 1,601 routing
decisions is an isolated call.

```sql
CREATE TABLE episode (
  id              TEXT PRIMARY KEY,
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  intent_text     TEXT NOT NULL,      -- what the user actually typed
  project_scope   TEXT NOT NULL,      -- scope_key(); NEVER NULL
  graph_ref       TEXT,               -- the AGR graph that ran
  convention_id   TEXT,               -- which convention chose it, if any
  convention_mode TEXT,               -- 'auto' | 'suggested' | 'explicit' | 'none'
  outcome         TEXT,               -- PASS | FAIL | ABANDONED | SURFACED
  human_corrected INTEGER DEFAULT 0,
  schema_version  INTEGER NOT NULL
);

CREATE TABLE episode_node (
  episode_id      TEXT NOT NULL REFERENCES episode(id),
  node_id         TEXT NOT NULL,      -- AGR node id, e.g. 'implement.api'
  node_kind       TEXT NOT NULL,      -- agent | verifier | human | router
  attempt_n       INTEGER NOT NULL,
  model           TEXT,
  model_version   TEXT,               -- §24: a model is not a constant
  tier            TEXT,
  tools_offered   TEXT,               -- JSON array; capability context
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  cost_usd        REAL,
  cost_measured   INTEGER NOT NULL,   -- 0/1 — NOT an assumption
  latency_ms      REAL,
  verdict         TEXT,               -- PASS | FAIL | UNCERTAIN
  check_kind      TEXT,               -- cmd | lint | diff | canary | none
  check_detail    TEXT,
  candidates_json TEXT,               -- §29: what was considered and REJECTED
  PRIMARY KEY (episode_id, node_id, attempt_n)
);

CREATE TABLE episode_event (
  episode_id  TEXT NOT NULL REFERENCES episode(id),
  ts          TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- correction | escalation | gate |
                                      -- override | anti_pattern
  payload     TEXT NOT NULL
);
```

Four properties, each bought with a scar already in this repo:

| Property | Why |
|---|---|
| `cost_measured` is a flag, not an assumption | 1,387 of 1,601 existing rows carry a flat $0.01 placeholder. AGR already does this (`usd_measured`) |
| `verdict` has **three** values | UNCERTAIN must not collapse into PASS or FAIL. S9: a NULL confidence coerced to 0.0 produced 213 "High"-confidence `CLASSIFIER_ERROR` findings from a column nobody wrote |
| `candidates_json` records the **counterfactual** | Without it no later analysis can recover what was never tried — §29 is unfixable after the fact |
| `project_scope` is NOT NULL | `subject` is NULL in 1,601 of 1,601 rows *while the bandit keys on it*. A dimension that is always NULL is worse than absent — it looks like a feature |

---

## 1. Project Index (§2)

Extends `semantic/store.py`, which already has `find_definitions`,
`find_importers`, `known_files`, `content_hash()`.

```sql
CREATE TABLE entity (               -- extends the existing index
  id, kind,                         -- repo|module|class|function|test|schema|endpoint
  path, name, scope_key,
  content_hash,                     -- reuse the existing hash
  last_seen_commit, last_seen_at    -- §24 staleness
);
CREATE TABLE relation (
  src_id, dst_id, kind,             -- imports|defines|tests|calls|belongs_to
  evidence,                         -- 'ast' | 'heuristic' | 'human'
  confidence REAL
);
```

**`evidence` matters more than `confidence`.** An AST-derived import is a fact;
a concept grouping ("TokenManager belongs to Authentication") is a guess. Mixing
them into one score loses the only distinction that tells you which to trust.

**Highest-value query, available immediately from the import graph:**

```
blast_radius(file) = transitive closure of `imports` inbound, depth ≤ 2
                     ∩ entities of kind=test  →  the tests to run
```

This alone improves verification selection and needs no new data.

**Staleness:** an entity whose `content_hash` no longer matches the working tree
is **stale, not wrong** — it is excluded from retrieval and flagged, never
silently used. A repo-version field exists so a fact learned on one branch does
not assert itself on another.

---

## 2. Experience and Capability/Outcome — derived views, not stores (§3, §4)

Both are `SELECT`s over `episode_node`. Writing them as tables would create the
drift this repo has already paid for twice.

```sql
-- §4 "who is good at this"
CREATE VIEW capability_outcome AS
SELECT model, model_version, node_kind, project_scope,
       COUNT(*) AS n,
       AVG(verdict='PASS')                    AS pass_rate,
       SUM(tokens_in+tokens_out)              AS tokens,
       AVG(latency_ms)                        AS latency,
       SUM(cost_usd * cost_measured)          AS measured_cost,
       SUM(1 - cost_measured)                 AS unmeasured_rows
FROM episode_node
WHERE verdict IS NOT NULL
GROUP BY 1,2,3,4;

-- §3 "what happened here before"
CREATE VIEW experience AS
SELECT e.project_scope, n.node_id, n.verdict, n.check_detail,
       e.intent_text, e.human_corrected, e.ended_at
FROM episode_node n JOIN episode e ON e.id = n.episode_id;
```

**`unmeasured_rows` travels with every aggregate.** A pass rate over rows whose
cost was a placeholder is a different claim from one over measured rows, and the
denominator is the only thing that says which you have.

### Confidence, decay, contradiction, supersession (§3, §24)

| Concern | Rule |
|---|---|
| Minimum evidence | No ranking below `MIN_SAMPLES_FOR_SIGNAL = 30` (already the constant). **Refuse to rank**, do not rank on noise |
| Temporal decay | Half-life on `n`, not on the rate: an old observation counts less, but an old *rate* is not made wronger by age. Default 90 days, per scope |
| Model version | `model_version` is part of the key. A new version starts at **n=0**, not inheriting its predecessor's record. §24 requires this and it is the most commonly skipped part |
| Contradiction | Two experiences with opposite verdicts on the same `(project_scope, node_id)` are **both retained** and surfaced as a conflict. Averaging them hides the thing worth seeing |
| Supersession | An `episode_event` of kind `correction` marks the superseded episode; superseded rows are excluded from retrieval, **not deleted** — deletion loses the negative evidence |
| Never immutable | Every derived claim carries `n`, window, and the query that produced it. A fact without its denominator is not retained |

---

## 3. Workflow Conventions (§5, §22, §23, §24)

A **file**, not a database. Tens of entries. Inspectability is a requirement,
and a DB is the opposite of it.

```yaml
# ~/.llm-router/conventions/conventions.yaml
- id: implementation_with_verification_loop
  kind: learned                 # hard | learned | contextual   (§6 — NEVER merged)
  status: accepted              # observed|candidate|suggested|accepted|default|demoted
  confidence: 0.86              # evidence only; NOT preference strength
  scope:                        # §22 — MANDATORY. No scope = invalid entry
    task_type: [implementation]
    min_complexity: moderate
    repo: [llm-router]
    language: [python]
    risk_max: medium            # §23 carve-out: never auto-applies above this
  graph_ref: workflows/implementation_with_verification_loop.yaml
  evidence:
    occurrences: 17
    first_seen: 2026-07-02
    last_seen: 2026-09-21
    sessions: 11                # distinct sessions, not distinct prompts
    examples: [ep_8f21, ep_9a04, ep_a771]
  overrides: 1                  # §20 — cancellations are the strongest signal
  decay_half_life_days: 120
  supersedes: null
```

### Three kinds, never one number

| Kind | Source | May it auto-apply? | May it be demoted by evidence? |
|---|---|---|---|
| **hard** | The user said "always X" | Yes | **No** — only the user removes it |
| **learned** | Inferred from repetition | Yes, above threshold | Yes |
| **contextual** | Scoped to a situation | Only within scope | Yes |

Collapsing these into one confidence is the failure mode that makes the whole
feature untrustworthy: a hard instruction decaying because it has not recurred
lately is indefensible.

### Lifecycle and thresholds (§6)

```
observed (1)
  → candidate    (≥3 similar, ≥2 distinct sessions)
  → suggested    (≥5, user is shown it)
  → accepted     (explicit human act — NEVER automatic)
  → default      (auto-applies; decided 2026-09-23)
  → demoted      (2 consecutive overrides, or confidence < floor)
```

**Auto-apply carries four hard requirements**, not one threshold:

1. A **one-command undo** named in the report, which disables the convention and
   drops its confidence below threshold. Without it, a convention learned from a
   coincidence runs real work with no brake.
2. An **override is recorded as evidence** (`episode_event.kind='override'`) —
   the strongest negative signal available.
3. Confidence is a **floor, not the gate**: auto-apply also requires matching
   scope and no conflicting hard convention.
4. **Never auto-apply where the graph does destructive work** — migrations,
   releases, force-pushes, anything outward-facing. Those route to AGR's
   `kind: human`, whose runner already refuses to self-sign. A hard carve-out,
   not a confidence question.

### Precedence (§23) — one change to the brief's ordering

```
1. explicit instruction in THIS request
2. safety / destructive-action policy
3. hard convention ("always…")
4. task-specific contextual convention
5. repository convention
6. user convention
7. general optimisation
```

The brief puts safety **above** explicit instruction. **Wrong for a developer
tool.** The user must be able to say "skip the audit, it's a typo fix" and be
obeyed. Safety outranks *learned* conventions and *optimisation* — never a live
human instruction. What safety may legitimately do is **require the override to
be explicit and recorded**, which is a different mechanism from overruling it.

Ties inside a level break by: **specificity** (narrower scope wins) →
**explicitness** (hard > learned) → **recency** → **confidence**. Confidence is
last on purpose: a confidently-learned general rule must not beat a
narrowly-scoped explicit one.

### Anti-patterns (§21)

Same file, same lifecycle, inverted sign. An anti-pattern needs *evidence of
harm*, not just absence of benefit:

```yaml
- id: full_repository_context_for_small_fix
  kind: anti_pattern
  scope: { task_type: [code], max_diff_lines: 50 }
  evidence:
    occurrences: 6
    harm: { tokens_median: 84000, vs_baseline: 6200, verdict_delta: 0.0 }
```

`verdict_delta: 0.0` is the load-bearing field — it says the cost bought
nothing. Without it, "used many tokens" is not evidence of an anti-pattern.
