# Architecture as built — 2026-09-22

Derived from traced execution, not documentation. Where a diagram and a docstring
disagree, the diagram is right.

**Scale:** 412 `.py` under `src/llm_router` (127,401 LOC), 723 test files, 159
scripts. 5 console scripts, 51 dispatchable subcommands, 38 hook scripts (13
installed on the audited machine), 52 distinct persistence stores, 11 HTTP routes
on the gateway + 6 on the control plane.

---

## 1. Entry points, and where information is lost

```mermaid
flowchart TD
    subgraph clients
      SDK["OpenAI / Anthropic / Ollama SDK"]
      MCP["MCP tools (llm, llm_code, …)"]
      HOOK["Claude Code hooks"]
      CLI["CLI"]
    end

    SDK --> GW["gateway.py"]
    GW --> FLAT["_flatten(messages)"]
    FLAT -->|"system + history collapsed<br/>into ONE string"| CLS["classify_signals()"]
    CLS -->|"thresholds on CHAR LENGTH"| PROF["_resolve_profile()"]

    MCP --> RC["route_and_call()"]
    HOOK --> HCLS["hook's own classifier<br/>(59.7% agreement, documented)"]
    HCLS --> RC
    CLI --> RC
    PROF --> RC

    FLAT -.->|"tools DROPPED by pydantic<br/>— now refused with 400"| X1(("400"))
    FLAT -.->|"system_prompt never<br/>reaches route_and_call"| LOSS(("T-03<br/>complexity inflated"))

    style LOSS fill:#fdd,stroke:#900
    style FLAT fill:#fdd,stroke:#900
```

Only the gateway path concatenates the system prompt before classification. The
MCP and hook paths do not.

---

## 2. Terminal outcomes and which ones reach the quality ledger

```mermaid
flowchart TD
    RC["route_and_call"] --> LOOP{"dispatch loop"}

    LOOP -->|success| FIN["_finalize_successful_route"]
    LOOP -->|all models fail| FAIL["_emit_quality_terminal('failed')"]
    LOOP -->|semantic cache hit| SC["_emit_quality_terminal('cache_hit')"]
    LOOP -->|idempotency dedupe| IDEM["_finalize(served_from_cache=True)"]
    LOOP -->|every candidate gate-rejected| FLOOR["exhaustion floor<br/>_finalize(served_from_cache=True)"]

    FIN --> QL[("routing_quality.jsonl")]
    FAIL --> QL
    SC --> QL
    IDEM -.->|"NO ROW"| GAP1(("T-08"))
    FLOOR -.->|"NO ROW"| GAP2(("T-08"))

    FLOOR --> USER["returned to caller<br/>with no degradation marker"]
    USER -.-> BANDIT["counted as success<br/>in the bandit reward"]

    style GAP1 fill:#fdd,stroke:#900
    style GAP2 fill:#fdd,stroke:#900
    style BANDIT fill:#fdd,stroke:#900
```

Three of five terminal states reach the ledger. The two that do not are the two
that describe degradation.

---

## 3. Money surfaces and provenance coverage

```mermaid
flowchart LR
    W1["log_usage()"] -->|is_simulated stamped| USAGE[("usage")]
    W2["cc-usage-track"] -->|is_simulated stamped| USAGE
    W3["log_claude_usage"] --> CU[("claude_usage<br/>NO provenance column")]
    W4["log_codex_usage"] --> CU
    W5["import_savings_log"] --> SS[("savings_stats<br/>NO provenance column")]

    USAGE --> A["get_savings_by_period<br/>FILTERS ✓"]
    USAGE --> B["get_team_savings<br/>NO FILTER"]
    USAGE --> C["get_daily/monthly_spend<br/>NO FILTER"]
    CU --> D["get_realized_savings<br/>NO FILTER POSSIBLE"]
    SS --> E["get_lifetime_savings<br/>NO FILTER POSSIBLE"]

    B --> SLACK(("broadcast to<br/>Slack / Discord"))
    C --> CAP(("gates real<br/>budget caps"))

    style B fill:#fdd,stroke:#900
    style C fill:#fdd,stroke:#900
    style D fill:#fdd,stroke:#900
    style E fill:#fdd,stroke:#900
    style A fill:#dfd,stroke:#090
```

The one green box is yesterday's fix. The surface that broadcasts publicly is red.

---

## 4. Ground Truth — two pipelines that never meet

```mermaid
flowchart TD
    subgraph live["accumulation pipeline (gtc- ids)"]
      CAP["prompt_capture.capture()"] --> SCR["scrub (fails closed)"]
      SCR --> EL["eligibility.assess()"]
      EL --> ENV["envelope.build()"]
      ENV --> POOL[("candidate pool")]
      POOL --> PROP["propose.py"]
      PROP --> MUT["mutants.py"]
      MUT --> REG["verifier_registry<br/>PROPOSED→VALIDATED→APPROVED→ACTIVE"]
    end

    subgraph legacy["labelling pipeline (gt- ids)"]
      EX["extract_corpus.py"] --> AUTH["author_tasks.py<br/>hand-authored"]
      AUTH --> FREEZE["freeze.py"]
      FREEZE --> RM["run_matrix.py"]
      RM --> LAB["label.py<br/>cheapest_acceptable_model"]
    end

    REG -.->|"active_for(gtc-…) never matches gt-…"| VOID(("T-06<br/>NO BRIDGE"))
    ENV -.->|"captured, never dereferenced"| VOID2(("no replayer<br/>run_verifier gets no cwd"))

    style VOID fill:#fdd,stroke:#900
    style VOID2 fill:#fdd,stroke:#900
```

Everything left of the gap is rigorous and unreachable. Everything right of it is
manual and is the only thing producing labels.

---

## 5. Observability — the counter nobody reads

```mermaid
flowchart LR
    S1["router.py ×5"] --> FO["failopen.record()"]
    S2["cost.py ×N"] --> FO
    S3["execution_ledger"] --> FO
    S4["…58 call sites total"] --> FO
    FO --> STORE[("failopen store")]
    FO -.->|"store unwritable →<br/>swallowed at :112"| LOST(("recorded nowhere"))
    FO -.->|"fallback is debug;<br/>effective level WARNING"| SILENT(("printed nowhere"))
    STORE -.->|"0 readers in src/"| NOONE(("T-07"))
    STORE --> TESTS["19 readers in tests/"]

    style LOST fill:#fdd,stroke:#900
    style SILENT fill:#fdd,stroke:#900
    style NOONE fill:#fdd,stroke:#900
```

---

## Component classification

| Class | Modules |
|---|---|
| **Runtime-critical** | `router`, `cost`, `config`, `server`, `cli`, `classify`, `providers`, `profiles`, `policy`, `budget`, `session_store`, `routing_quality`, `pricing`, `paths`, `secret_scrubber` |
| **Optional / gated** | `bandit`, `judge`, `semantic_classify`, `semantic_cache`, `result_cache`, `prompt_cache`, `ensemble`, `grounding` |
| **Tooling-only** | `onboard`, `quickstart`, `banner`, `install_hooks`, most of 51 subcommands |
| **Broken at HEAD** | `commands/profile` (ImportError), `commands/dev_refresh` (wrong script name), `budget_lineage_reconciliation.reconcile_budget_lineage_audited` (ImportError on install) |
| **Unshipped by design** | `control_plane/api`, `control_plane/reconciliation` |
| **Orphaned** | the `propose → mutants → verifier_registry` half of Ground Truth |
| **Write-only** | `failopen` |

---

## The shape of the system

Unchanged from the previous audit in kind, changed in degree.

**The execution half** — entry points, classification, chain building, dispatch,
fallback — is coherent and works, with one defect (gateway classification) on its
most advertised path.

**The measurement half** is now *partly* honest. Yesterday's work made the
canonical implementations correct. It did not reach the call sites, and the
verification was aimed at the canonical implementations rather than at adoption,
so nothing noticed.

**The observability half** — the layer that should tell you when either of the
above stops being true — is the weakest of the three, and was not touched.
