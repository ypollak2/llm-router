# Integration Plan: RTK + Token-Savior → LLM-Router

## Vision

Embed command output compression (RTK) and response compression (Token-Savior) into llm-router to create a **three-layer compression pipeline**:

```
Input
  ↓
┌─────────────────────────────────────────┐
│ Layer 1: RTK Compression (auto-route)   │
│ Compress shell outputs before routing    │
│ 80-90% context reduction                │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│ Layer 2: LLM-Router Selection            │
│ Choose model by complexity               │
│ 70-90% cost reduction                   │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│ Layer 3: Token-Savior Post-Processing   │
│ Compress LLM response                    │
│ 60-75% output reduction                 │
└─────────────────────────────────────────┘
  ↓
Output (99% token savings possible)
```

---

## Phase 1: RTK Integration (Output Compression)

### Location: `src/llm_router/compression/rtk_adapter.py`

**Goal:** Compress shell command outputs before they reach the LLM context.

### Implementation Strategy

```python
class RTKAdapter:
    """Compress Bash/shell outputs like RTK does."""

    def compress(self, command: str, output: str) -> str:
        """
        Apply RTK-style compression to command output.

        Examples:
        - git log (500 lines) → 20 key commits
        - pytest (1000 lines) → pass/fail summary
        - cargo build (2000 lines) → build status only
        """

    def get_filters(self) -> dict[str, Callable]:
        """Return command-specific filters."""
```

### Compression Filters (100+ like RTK)

```python
FILTERS = {
    "git": {
        "log": deduplicate_commits,
        "status": summarize_changes,
        "diff": keep_hunks_only,
    },
    "pytest": {
        "*": aggregate_test_results,  # PASS/FAIL summary
    },
    "cargo": {
        "build": keep_errors_only,  # Warnings → output only
    },
    "docker": {
        "ps": deduplicate_containers,
        "logs": tail_recent,
    },
}
```

### Wire-up Points

1. **Session-start hook** — Inject compression filter list
2. **Auto-route hook** — Apply compression to tool outputs
3. **LLM response** — Include compression metadata

---

## Phase 2: Token-Savior Integration (Response Compression)

### Location: `src/llm_router/compression/response_compressor.py`

**Goal:** Compress LLM responses to remove verbosity.

### Implementation Strategy

```python
class ResponseCompressor:
    """Compress LLM responses like Token-Savior."""

    def compress(self, response: str, target_reduction: float = 0.6) -> str:
        """
        Compress response while preserving key information.

        Target: 60% reduction (full response → key points)

        Strategies:
        1. Extract key sentences (TF-IDF or TextRank)
        2. Remove filler ("I think", "basically", "actually")
        3. Consolidate examples
        4. Collapse boilerplate
        """

    def get_compression_ratio(self) -> float:
        """Report actual compression achieved."""
```

### Compression Stages

| Stage | Strategy | Reduction |
|-------|----------|-----------|
| **1. Filler removal** | Strip "I think", "basically", articles | 5-10% |
| **2. Example consolidation** | Keep 1 example, tag others as "similar" | 15-20% |
| **3. Boilerplate collapse** | Long explanations → bullet points | 20-30% |
| **4. Semantic extraction** | Keep key sentences, remove elaboration | 10-20% |
| **Total** | Combined effect | 60-75% |

### Wire-up Points

1. **After LLM response** — Compress before returning to user
2. **Session-end hook** — Track compression stats
3. **Quality report** — Show original vs. compressed length

---

## Phase 3: Integration with `llm_gain`

Update `llm_gain` dashboard to show **three layers**:

```
═══════════════════════════════════════════════════════
          💰 TOKEN SAVINGS DASHBOARD
═══════════════════════════════════════════════════════

LAYER 1: RTK OUTPUT COMPRESSION
  Commands processed: 147
  Total output tokens compressed: 23,450 → 2,345 (90%)
  Tokens saved: 21,105

LAYER 2: MODEL ROUTING (existing)
  Cost actual: $0.0456
  Cost baseline (Opus): $2.3400
  Tokens saved: $2.2944

LAYER 3: RESPONSE COMPRESSION
  Responses generated: 147
  Total response tokens: 45,600 → 18,240 (60%)
  Tokens saved: 27,360

═══════════════════════════════════════════════════════
TOTAL EFFICIENCY
  Input tokens saved: 21,105 (RTK)
  Model cost saved: $2.2944 (Router)
  Output tokens saved: 27,360 (Token-Savior)
  ────────────────────────────────────
  Combined efficiency: 97x vs Opus baseline
```

---

## Implementation Sequence

### Week 1: RTK Integration
- [ ] Create RTK adapter with 20 core filters (git, pytest, cargo)
- [ ] Wire into auto-route hook
- [ ] Add compression stats to usage.db schema
- [ ] Test with real command outputs

### Week 2: Token-Savior Integration
- [ ] Create response compressor with 4-stage pipeline
- [ ] Add semantic extraction (use existing nltk or spaCy)
- [ ] Wire into response formatting
- [ ] Test compression ratio on 100 sample responses

### Week 3: Dashboard Integration
- [ ] Update `llm_gain` to show all 3 layers
- [ ] Add compression stats to session-end hook
- [ ] Create compression heatmap (by tool/model/complexity)
- [ ] Document compression behavior in README

---

## Database Schema Changes

Add to `usage.db`:

```sql
CREATE TABLE compression_stats (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    layer TEXT,  -- "rtk", "token-savior"
    input_tokens INTEGER,
    output_tokens INTEGER,
    compression_ratio FLOAT,
    timestamp TEXT
);
```

---

## Key Design Decisions

| Decision | Why |
|----------|-----|
| **RTK layer in auto-route hook** | Compresses outputs before routing decision |
| **Token-Savior after response** | Doesn't affect model choice, purely cosmetic |
| **Tracked separately** | Shows user which layer saved what |
| **User-configurable** | Can disable compression per session if needed |

---

## Success Criteria

- [ ] RTK compression reduces output tokens 80-90% on Bash commands
- [ ] Token-Savior reduces response tokens 60-75% without losing information
- [ ] `llm_gain` shows all 3 compression layers
- [ ] Combined savings = 97x vs Opus baseline
- [ ] No performance regression (<10ms overhead per compression)
- [ ] All tests pass (including compression integration tests)

---

## Open Questions

1. **RTK filters complexity** — Should we implement all 100+ RTK filters or start with top 20?
   - Recommendation: Start with 20 (git, pytest, cargo, docker, npm), expand later

2. **Token-Savior semantic model** — Use pretrained model or simple TF-IDF?
   - Recommendation: Start with TF-IDF (no ML deps), upgrade to spaCy if needed

3. **Compression user control** — Should users be able to disable per-layer?
   - Recommendation: Yes (env var: `LLM_ROUTER_COMPRESSION_LAYERS=rtk,router,token-savior`)

4. **Cost accounting** — How to show "effective Opus cost" with all layers?
   - Recommendation: Show matrix: actual cost, opus baseline, per-layer savings

