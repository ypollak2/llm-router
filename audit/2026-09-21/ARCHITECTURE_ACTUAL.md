# Architecture as built

v14.1.0 · 2026-09-21. Derived from code, not documentation. Where the diagrams
differ from the docs, the diagrams are right.

**Scale:** 413 `.py` files under `src/llm_router/`; `router.py` is 5,050 lines and
`cost.py` 3,763. 680 test files. `~/.llm-router` holds 1,096 entries and 5.0 GB —
of which **4.8 GB is a vendored RouterArena checkout**, not router state.

---

## 1. Runtime routing

```mermaid
flowchart TD
    subgraph Entry
      H["Hooks<br/>auto-route.py · enforce-route.py"]:::advisory
      M["MCP tools<br/>llm · llm_act · llm_edit"]
      G["Gateway<br/>/v1/chat/completions · /v1/messages"]
      C["CLI"]
    end

    H -.->|"advisory only —<br/>never calls the router"| STDOUT[/"decision JSON<br/>+ sidecar file"/]
    STDOUT -.->|"120s TTL, fails silently"| M

    M --> RC["route_and_call()"]
    G --> FLAT["_flatten()<br/>messages → text blob"]
    FLAT -->|"TOOLS DROPPED"| RC
    C --> RC

    RC --> RP["_resolve_profile()"]
    RP -->|"only 'complexity' is used"| CH["get_model_chain()"]
    CH --> BAN["bandit reorder<br/>(on by default)"]
    BAN --> LOOP{"try each model"}

    LOOP -->|success| FIN["_finalize_successful_route()"]
    LOOP -->|all fail| FAIL["_emit_ledger_terminal('failed')"]
    LOOP -->|cache hit| CACHE["served_from_cache=True"]

    FIN --> LEDGER[("routing_quality.jsonl")]
    FIN --> CAP["prompt_capture.capture()"]
    FAIL -.->|"NEVER reaches"| LEDGER
    CACHE -.->|"skipped by the gate<br/>at router.py:1958"| LEDGER
    FAIL --> EXEC[("execution_ledger<br/>SQLite")]
    CACHE --> EXEC

    classDef advisory stroke-dasharray: 4 4
```

**Three things this diagram makes visible that the docs do not:**
- Hooks never call the router. They are gates; the sidecar bridging their
  classification to the MCP tool has a 120-second TTL and fails silently.
- The gateway flattens messages to text, and **tool definitions are discarded by
  Pydantic before the handler body runs**.
- Two of the three terminal outcomes — failure and cache hit — **cannot reach the
  quality ledger**.

---

## 2. Fallback and attribution

```mermaid
flowchart LR
    A["chosen_model<br/>(tier N)"] -->|"error"| B["fallback"]
    B -->|"provider_failure<br/>rate_limit<br/>quality_failure"| C["final_model<br/>(tier M)"]
    C --> R["ledger row"]

    R --> OK["reason recorded<br/>3,217 rows"]
    R --> BAD["NO reason<br/>34 rows"]

    style BAD fill:#fdd,stroke:#900
```

19.3% of real rows show `chosen_model ≠ final_model` — expected escalation. **34
of those carry `fallback_occurred=False` and `fallback_reason=None`**: the model
that ran differs from the one recorded as chosen, with no persisted explanation.

---

## 3. Telemetry — four stores, four provenance schemes

```mermaid
flowchart TD
    R["route_and_call"] --> RQ[("routing_quality.jsonl<br/>23.7k rows")]
    R --> UD[("usage.db")]
    R --> EL[("execution_ledger")]
    R --> MT[("model_tracking.jsonl")]

    RQ --> SUM["summarize()"]
    UD --> SE["session-end panel<br/>(what users see)"]
    UD --> SAV["get_savings_by_period()"]

    SUM -.->|"never calls"| IE["is_evaluable()<br/>CORRECT"]
    SE -->|"uses"| IR["is_real<br/>defaults 1, heuristic-maintained"]
    SAV -->|"filters on"| IS["is_simulated<br/>NEVER WRITTEN"]
    UD --> ITM["_is_test_model()<br/>name matching"]

    ATTR["attribution.py<br/>'canonical, one definition'"] -.->|"0 callers"| X(("nothing"))

    style IE fill:#dfd,stroke:#090
    style IS fill:#fdd,stroke:#900
    style IR fill:#fdd,stroke:#900
    style X fill:#fdd,stroke:#900
```

The green box is the correct mechanism. Nothing that produces a user-visible
number uses it.

---

## 4. Ground Truth accumulation

```mermaid
flowchart TD
    FIN["_finalize_successful_route"] --> CAP["capture()"]
    CAP --> SCR["scrub()<br/>fails closed"]
    SCR --> DS{"detect_synthetic()"}
    DS -->|"synthetic"| SKIP["outcome: skipped"]
    DS -->|"real"| EL["eligibility.assess()<br/>pass 1"]
    EL --> ENV["envelope.build()<br/>repo commit + patch"]
    ENV --> EL2["assess() pass 3<br/>re-check vs captured state"]
    EL2 --> POOL[("candidate pool")]
    POOL --> PROP["propose.py<br/>contract + verifier"]
    PROP --> MUT["mutants.py<br/>validate"]
    MUT --> REG["registry<br/>PROPOSED→VALIDATED→APPROVED→ACTIVE"]
    REG --> RM["run_matrix.py"]
    RM --> LAB["label.py<br/>cheapest_acceptable_model"]

    ENV -.->|"captures repo state"| REPLAY(("NO REPLAYER<br/>0 checkout/apply sites"))
    RM -.->|"single-turn text only"| REPLAY

    style REPLAY fill:#fdd,stroke:#900
```

The envelope captures everything needed to replay. **Nothing consumes it** — so
EDIT tasks, which the gate is tuned to admit, cannot be graded.

---

## 5. Verifier lifecycle

```mermaid
stateDiagram-v2
    [*] --> PROPOSED
    PROPOSED --> VALIDATED: mutation evidence<br/>baseline passes + mutants die
    PROPOSED --> REJECTED: cannot discriminate
    VALIDATED --> APPROVED: human<br/>(string check only)
    APPROVED --> ACTIVE: human
    ACTIVE --> [*]: run_matrix may execute
    VALIDATED --> REJECTED
    APPROVED --> REJECTED
```

Confidence is computed **from mutation evidence, never asserted** — genuinely
sound. The human gate is `actor == "assistant"`; any other string passes.

---

## Component classification

| Class | Modules |
|---|---|
| **Runtime-critical** | `router`, `cost`, `config`, `server`, `cli`, `context`, `classify`, `providers`, `profiles`, `policy`, `budget`, `session_store`, `routing_quality`, `pricing`, `paths` |
| **Optional / gated** | `bandit`, `judge`, `semantic_classify`, `semantic_cache`, `result_cache`, `prompt_cache`, `ensemble`, `grounding` |
| **Tooling-only** | `onboard`, `quickstart`, `banner`, `install_hooks`, all 43 `commands/` |
| **Benchmark-only** | `benchmark_fetcher`, `benchmarks`, `model_evaluator` |
| **Apparently dead** | `budget_lineage_reconciliation`, `feedback_handler`, `hook_deadlock_checker`, `oauth_token_rotation`, `service_manager`, `attribution` |
| **Broken** | `control_plane/api` (ImportError on import) |
| **Decoy** | `context_signal` — docstring says "NOT the one in production" |

---

## Persistence

| Store | Size | Concurrency safety |
|---|---|---|
| `routing_quality.jsonl` | 23 MB | **Safe** — 2,000 concurrent appends, zero corruption |
| `usage.db` | 6 MB | 1/12 cold starts `database is locked`; 5/12 swallowed migration failures |
| `result_cache.db` | 256 KB | 2/12 cold starts locked — `busy_timeout` set *after* WAL, opposite of the project's own fix |
| `quota_tracker` usage.json | small | **32–38% read failure** under load — non-atomic whole-file rewrite, 10 hooks consume it |
| `ground_truth_candidates.jsonl` | — | `Pool.admit` loses increments (19 vs 21) |
| `harness/RouterArena/` | **4.8 GB** | Not router state — a vendored eval framework inside the state dir |

`LLM_ROUTER_HOME` is **not universally honoured**: 120 sites compose
`~/.llm-router` directly, and five modules honour five *different* override
variables.

---

## The shape of the system

Two halves with very different maturity.

**The execution half** — entry points, classification, chain building, provider
calls, fallback — is coherent, well-commented, and works.

**The measurement half** — ledgers, provenance, attribution, savings,
explanation — is a set of correct primitives with missing adoption, four
competing provenance schemes, and two of three terminal outcomes unrepresented.

Ground Truth sits on top of the second half. That is why it cannot yet deliver
what it was built for.
