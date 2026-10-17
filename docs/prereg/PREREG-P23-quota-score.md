# PREREG-P23: `resource_map.score` weights and the P2.3-a/b checks

Status: **FROZEN (r4).** Frozen after an independent freeze-guard review (D-122); the
sha256 and frozen_utc are in `PREREG-P23-quota-score.sha256`. It sets the weights and the replay rule
before any replay runs. PLAN v16 P2.3 task 1 says the weights are pre-registered. D-122,
D-122a and D-122b (coordinator, 2026-10-16) settle the design points below. After the freeze,
changing a weight or a rule needs an amendment file (`PREREG-P23-quota-score-AMEND<n>.md`)
that says why.

## 1. Formula

PLAN v16 P2.3 task 1: `score = priority - w1*pressure - w2*(1/reset_in_h) - w3*marginal_cost`.

**Deviation (D-122a a):** the reset term is **added**, not subtracted:
`score = priority - w1*pressure + w2/max(reset_in_h, 0.25) - w3*marginal_cost`.
The plan text gives no rationale for the minus sign. The weight is unchanged. A window
that resets soon gets a small bonus, because its unused quota is about to expire anyway.

| Term | Input (map row) | Known when | Weight |
|---|---|---|---|
| priority | `1 / effective_priority` | `effective_priority` is an `int` (not `bool`) >= 1, **and** the map's `policy` sets an order (`map_reader._expresses_order`). Under `balanced` the ranks are only row order, so the term is unknown | 1; the term lies in (0, 1] |
| pressure | `pmax` = the max of the **known** values of `quota.pressure` and `quota.weekly_pressure` | `quota_value` dict, provenance `measured` or `estimated`, value an `int`/`float` (not `bool`), finite, in [0, 1]. Provenance `default`, `stale` or any other is unknown (R-DIS-13) | **w1 = 10.0** |
| reset | `1 / max(reset_in_h, 0.25)`; `reset_in_h` = hours from `now` to `reset_at` | `reset_at` is a str that `datetime.fromisoformat` parses (naive = UTC) and lies in the future | **w2 = 0.05** |
| marginal_cost | `marginal_cost.value` (USD, as the map writes it) | `int`/`float` (not `bool`), finite, >= 0 | **w3 = 1.0** per USD |

Known values only (P2.3 task 3). A term whose input is unknown is absent, not zero. The
score is the sum of the known terms, and 0.0 when none is known. The explain record
writes `"unknown"` for each absent term. No unknown value reaches arithmetic: inputs are
type-checked before any `float()`, comparison or operator touches them.

Units: one score unit equals 0.1 of pressure (because w1 = 10). The reset term is at most
`0.05 / 0.25 = 0.2` score units, which is 0.02 of pressure (2 percentage points).

## 2. Filters (before scoring) and sort key

`_eligible(row, decision)` returns `None` (eligible) or a reason string.

* `row` is a dict whose `status` is exactly the str `"connected"`. A missing or unknown
  status is filtered out ("status unknown").
* `capabilities` is a list or tuple that contains `"generate"`. A missing or
  non-sequence value is filtered out ("capabilities unknown").
* Every str in `decision["needs"]` must be in `capabilities`. A `needs` value that is not
  a list, tuple, set or frozenset is ignored. `route_and_call` passes `needs = ()`: the MCP
  door is a text completion with no tool schema, and P2.4 owns capability exclusion.
* Competence **failed** means: `row["competence"]` is a dict, `decision["task_type"]` is a
  str, `competence[task_type]` is a dict with at least one tier dict, and every tier dict
  has `eligible is False`. Anything else passes (null = "not measured"). This is moot for
  subscriptions, because the map writes competence only on `ollama/*` rows.

Sort key in `order_resources` (D-122a d, D-122b), ascending:
`(cost_group, pressure_group, -score, input_index)`

* `cost_group` = 1 only for a **non-subscription** row with an unknown marginal cost.
  This is the plan's "unknown marginal_cost sorts after known ones", applied only among
  rows where cost separates anything. Between subscriptions, cost never forms a group;
  an unknown cost only drops the term.
* `pressure_group` (D-122b, three-valued) applies to **subscription** rows only (0 for
  every other row):
  * 0 when `pmax` is known and `< SCORE_PMAX_HIGH`;
  * 1 when `pmax` is unknown;
  * 2 when `pmax` is known and `>= SCORE_PMAX_HIGH`.

  `SCORE_PMAX_HIGH = 0.95` is a module constant read at call time. An unknown pressure
  never acts as a de facto 0.0. It ranks behind every known pressure below 0.95, and ahead
  of a known pressure at or above 0.95. So Claude cannot saturate while Codex stays
  unmeasured. **Disclosed remaining behaviour:** an unmeasured subscription still ranks
  behind a measured one that is below 0.95.
* Ties keep the input order.

## 3. Why these weights (margin)

Two eligible subscriptions with known pressure: A has `pmax >= 0.95` and B has
`pmax < 0.80`. Then `score(B) - score(A) > w1*0.15 - 1 - 0.2 - w3*|cost_A - cost_B|`:

* the pressure gap is > 0.15, worth 1.5;
* the priority gap is <= 1 (both terms lie in (0, 1], or one of them is absent);
* A's reset bonus is at most 0.2 (B's own bonus only helps B).

The gap is > 0.3 - |Δcost| > 0 **as long as |Δcost| < 0.3 USD**. Precondition: every
subscription row in the map carries marginal cost 0.0 (`resource_map._claude_row`,
`_codex_row`, `_gemini_row`), so Δcost = 0. The replay asserts this precondition on every
call. w1 = 10 makes a pressure gap of 0.10 worth the whole priority span, the same 10-point
band the old `quota_balance.get_balanced_provider_order` used.

Under D-122b, A (`pmax >= 0.95`, group 2) already sorts after B (`pmax < 0.80`, group 0)
because of the group alone. The score margin is a second, independent guard, used when
the group is disabled (`SCORE_PMAX_HIGH` patched) and among rows in the same group.

**Margin grid sweep (pass rule, deterministic).** For every cell of:

* A `pmax` in {0.95, 0.97, 1.0} and B `pmax` in {0, 0.5, 0.79, 0.7999};
* `effective_priority` per row in {absent, 1, 2, 3}, with `priority_ordered = True`;
* reset per row in {0.25, 1, 5, 168 h, absent}

(3 x 4 x 4 x 4 x 5 x 5 = 4,800 cells, marginal cost 0.0), both of these must hold:

1. B ranks above A in `order_resources`, with the default `SCORE_PMAX_HIGH`.
2. `score(B) - score(A) > 0.3` (pure score, no group).

The test reports the minimum gap. The expected minimum is 0.301, at A 0.95 / rank 1 /
reset 0.25 h and B 0.7999 / no priority / no reset.

## 4. Where the score runs (doors)

* **Router: one site.** `route_and_call` makes a single score pass right after the P0.15
  `sink_unvalidated` step (router.py ~4805 at `caaf8d28`) and before the D-116
  `_strip_local_for_qa` step (~4815), gated on `profile == RoutingProfile.QUOTA_BALANCED`
  and `not model_override`. The pass is `router._quota_score_chain(models, task_type,
  exempt=pinned_model)`. It reads `map_reader.load_data()` (the fresh map) or, with no fresh
  map, `resource_map.live_subscription_rows()`.
* The old QUOTA_BALANCED pass in `_build_and_filter_chain` (quota_balance
  `get_balanced_provider_order` + `reorder_chain_by_providers`) is **removed**, not
  duplicated. Under QUOTA_BALANCED, `map_reader.apply_to_chain` in that function only
  filters (`reorder=False`), so the map's effective_priority cannot undo the score order.
  The score already uses effective_priority.
* **Deliberate placement change.** The old pass moved every `ollama/*` entry to the head and
  every non-subscription entry to the tail. The new pass moves only subscription entries
  (providers `anthropic`, `codex`, `gemini_cli` that have a `kind == "subscription"` row).
  They trade places among the slots subscription entries already hold. Eligible providers
  go first by rank, then ineligible ones in their old relative order. Local and API entries
  and the exempt pin keep their slots.
* **Before the pass:** the precision tier, the subject specialist and the bandit all run
  before it. The pass sorts subscription entries by rank whatever their input order. These
  steps can change only ties, and **the pass overrides an explicit specialist or bandit
  choice among subscriptions** (for example, a bandit exploit pick of `anthropic/*` moves
  back behind a lower-pressure codex entry). A specialist or bandit pick that is a local
  or API entry keeps its slot. The relative order of entries **within** one provider is
  preserved, because the re-sort is stable and keyed by provider.
* **After the pass:**
  * The D-116 strip runs after the pass. **D-114/D-116 (D-122 item 3):** for Q&A task types
    (`qa_policy.QA_TASK_TYPES`), the pass holds `anthropic` entries in their slots
    (`hold_providers={"anthropic"}`). The pass keeps anthropic entries in their BASE slots,
    so Claude is not a spread target. For Q&A, the spread runs between codex and gemini_cli
    only.
  * **TQ-007:** under an exceeded daily cap, the chain is then confined to
    ollama/codex/gemini_cli, so Claude is not a spread target under a cap. With no free entry
    left, smart/soft modes fall through to Claude only, so there is nothing to spread. The
    confined chain keeps the score order (filter only).
  * `_demote_timed_out_local` (dispatch) moves only local entries. It is out of scope.
* **Compatibility builder:** `src/llm_router/chain_builder.py::build_chain` (the plan's
  `chain_builder.build_chain`, which has no production caller) gets the same pass under
  QUOTA_BALANCED. `src/llm_router/hooks/chain_builder.py` (hook drafts, SDK) has no profile
  setting and is **not covered** (D-122 item 5).
* **Agent-route Codex delegation** (`hooks/agent-route.py::_try_codex_subagent_delegation`).
  The gate applies only when `LLM_ROUTER_PROFILE` (or `LLM_ROUTER_COST_PROFILE`) is
  `quota_balanced` and a fresh map exists. `order_resources` ranks the claude and codex
  subscription rows. If both are eligible and claude ranks first, the delegation is
  declined (outcome `quota_spread`) and Claude handles the sub-agent. Otherwise nothing
  changes; any error fails open. **Disclosed consequence of D-122b:** a codex row with
  unknown pressure (for example `default` 0.0 when the map has no Codex counter or rollout
  reading for today) ranks behind a measured claude row below 0.95 and ahead of one at or
  above 0.95. This holds at this door and in the router pass. Put plainly: **Codex can get
  no QUOTA_BALANCED traffic until Claude is >= 0.95 or a Codex reading exists.** The hook
  path imports only
  `llm_router.map_reader` and `llm_router.resource_map` (both are stdlib-only at module
  level; `resource_map` loads `urllib` lazily), never `llm_router.router`. Door test D4
  checks this.
* **Proxy (OD-5):** the proxy builds with `complexity_to_profile`, which never yields
  QUOTA_BALANCED. It gets no cross-provider reorder; its Claude tiers are one subscription.

## 5. P2.3-a replay (synthetic, $0, no prompt text, no model calls, no network)

**Deviation (D-122 item 4):** the plan asks for a replay over 7 days of recorded
`quota_history.jsonl` plus Codex samples plus synthetic traces. This replay is synthetic
only.

### Function under test

```
setup (once per policy P in {"balanced", "cost"}):
  config  = _FakeConfig(                       # copy of tests/audit/test_chain_correctness._FakeConfig
              available_providers={"anthropic","openai"},
              ollama_models=["ollama/qwen3:8b"], routing_policy=P)
  stubs   = router.is_codex_available -> True, router.is_gemini_cli_available -> True,
            router.get_active_agent -> "claude_code", map_reader.load_view -> None,
            llm_router.agentic_registry.best_agentic_model -> None   # deterministic BASE
  # BASE is built once per policy run (4 chains per P), before the first call,
  # and reused by that run and by its negative control.
  BASE[P][cls][bucket] = await router._build_and_filter_chain(
              TT[cls], RoutingProfile.QUOTA_BALANCED, None,
              Complexity.MODERATE, Complexity.MODERATE, config)
     with claude_usage.get_claude_pressure -> 0.50 (bucket "lo") or 0.97 (bucket "hi")
     TT = {"code": TaskType.CODE, "qa": TaskType.QUERY}
  # The probe below found the "hi" and "lo" buckets equal for every (P, cls): the
  # agent-context reorder regroups the P0.2-c front injection. The replay keeps both
  # buckets and records them in the JSON summary.
  # BASE built with QUOTA_BALANCED equals BASE built with BALANCED once the old pass is
  # removed: QUOTA_BALANCED's base lookup is BALANCED, and the only other QUOTA_BALANCED
  # branch in _build_and_filter_chain is the map reorder-off, which is inert here
  # because load_view -> None.
  precondition: every BASE chain holds >= 1 entry each of anthropic/, codex/, gemini_cli/

route(call):                                   # mirrors route_and_call's order
  base   = BASE[P][call.cls]["hi" if pmax_true(claude) >= 0.95 else "lo"]
  map    = map_dict(state, P, now)             # see "Map rows"
  map_reader.load_data -> map                  # monkeypatched for this call
  chain  = router._quota_score_chain(base, TT[call.cls], exempt=None)
  chain  = qa_policy.strip_local_for_qa(chain, TT[call.cls])   # same call as route_and_call
  return chain
```

The pass composes with the D-116 strip exactly as `route_and_call` composes them. The
JSON summary records the 4 BASE chains of each run.

Probe of BASE at `caaf8d28` (`$S/p23-baseprobe.py`; built with BALANCED, which is
QUOTA_BALANCED's base minus the old pass; real environment, so not the replay's stubs):

* code, `balanced`: `[ollama, codex x3, gemini_cli x2, openai/gpt-4o, anthropic sonnet,
  anthropic haiku]`;
* `cost`: `claude-haiku` sits before `openai/gpt-4o`;
* Q&A: the same shape plus a second local.

Each chain holds every subscription, so the precondition is expected to hold.

What is left out, and why:

| Step | Replay | Why |
|---|---|---|
| precision tier, subject specialist | off (no openai-exact prompt, no subject) | they run before the pass, which re-sorts subscription entries by rank |
| bandit | off (`LLM_ROUTER_BANDIT=off`) | same reason |
| TQ-007 cap | off (no cap) | disclosed in section 4; a door test covers cap plus score |

### State and generator

* `rng = random.Random(2303)`, re-seeded at the start of each run. Simulated `now` starts at
  `T0 = 1_760_000_000` and advances 120 s per call.
* Subscriptions:

| sub | primary window | initial primary | weekly window | initial weekly | first primary reset |
|---|---|---|---|---|---|
| claude | 5 h | 0.30 | 168 h | 0.20 | T0 + 5 h |
| codex | 5 h | 0.20 | 168 h | 0.15 | T0 + 3 h |
| gemini_cli | 24 h | 0.10 | none | none | T0 + 24 h |

* Routed-call increment `inc[sub]`: claude 0.004, codex 0.005, gemini_cli 0.003 added to the
  serving sub's primary, and `inc/10` to its weekly. Values are capped at 1.0.
* Reset: at each step, before routing, every primary whose `reset_at <= now` is set to 0.0
  and its `reset_at` moves forward by the window. Exception: the segment's A (below). Weekly
  windows do not reset inside a run, since a run lasts 40 h.
* Schedule per run, 1,200 calls: 9 blocks of [40-step free phase, then an 80-step segment],
  then a final 120-step free phase.
  * Free phase: the class is `code` with p = 0.6, else `qa` (`rng.random()`). External Claude
    usage adds `rng.uniform(0, 0.004)` to claude's primary each step.
  * Segment for ordered pair (A, B) with third sub C, fixed class K. The 8 pair segments are
    the 6 code pairs (A, B over claude/codex/gemini_cli) and the 2 Q&A pairs (codex, gemini_cli)
    and (gemini_cli, codex), in that order, then the unknown segment. At segment start: A
    primary = 0.90, A weekly = min(A weekly, 0.50), B primary = 0.10, B weekly =
    min(B weekly, 0.30), C primary = 0.85. Each step: A primary += 0.0015 (external use),
    capped at 1.0. A's primary reset is suspended in its own segment, because the trace
    models an exhaustion that a reset would end. All calls in the segment have class K.
  * Unknown segment (class code, 80 steps): at start, claude primary = 0.94, gemini_cli
    primary = 0.50. Each step, claude primary += 0.0015 (external use), so claude crosses
    0.95 once (at step 7 by the design check). During the segment, the codex map row shows
    `pressure = quota_value(0.0, "default")` and no weekly value. Its true pressure still
    accumulates.
* Step order within each call: reset, then external use, then route (the map and `pmax`
  are read here), then the routed increment.
* Calls carry `needs = ()` and `task_type = TT[cls]`, as `route_and_call` does. The
  capability-filter path (`needs` non-empty) is therefore not exercised by the replay. It
  is covered by the unit tests and the fuzz only.

### Design check (dry arithmetic simulation, before any replay)

`$S/p23-drysim.py` (sha256 `e152a88c85cf8d847d928c00530f484770eb974bd69cfd0412078d92b188814f`;
output `$S/p23-drysim.out`, sha256
`20a44b1196ab801c231ba430cfe5d9727fa0e0525f021cd7a7fd79a965b48139`; `$S` is the coordinator
scratchpad) follows only the generator's pressure trajectories. It uses no router code
and no score code. Assumptions:

* score-like routing: B serves every segment call (worst case for B), and A gets
  external use only;
* control-like routing: A serves until it is exhausted;
* B resets are ignored (conservative), and A's reset is suspended.

| pair (class) | exposure (A in [0.95, 1.0), B < 0.80) | crossings | control cases | max pmax(B) |
|---|---|---|---|---|
| claude>codex (code) | 33 | 1 | 9 | 0.50 |
| claude>gemini_cli (code) | 33 | 1 | 9 | 0.34 |
| codex>claude (code) | 33 | 1 | 9 | 0.42 |
| codex>gemini_cli (code) | 33 | 1 | 9 | 0.34 |
| gemini_cli>claude (code) | 33 | 1 | 11 | 0.42 |
| gemini_cli>codex (code) | 33 | 1 | 11 | 0.50 |
| codex>gemini_cli (qa) | 33 | 1 | 9 | 0.34 |
| gemini_cli>codex (qa) | 33 | 1 | 11 | 0.50 |

Control cases: 78 per run, 156 over two runs. Unknown segment: claude crosses 0.95 at
step 7, which leaves 74 steps after the crossing. Exposure 33 >= 20 per pair, so the
non-vacuity bar is reachable. The real replay measures its own numbers; this table is
only a design check and not a result.

### Map rows (per call)

For each sub, the row is:

```
{"resource": sub, "kind": "subscription",
 "chain_id": {"claude": "anthropic/claude-subscription",
              "codex": "codex/codex-subscription",
              "gemini_cli": "gemini_cli/gemini-subscription"}[sub],
 "status": "connected",
 "capabilities": claude ["generate","tools","vision","thinking"],
                 codex ["generate","tools"],
                 gemini_cli ["generate","tools","vision"],
 "quota": {"pressure": quota_value(primary, "measured", now),
           "weekly_pressure": quota_value(weekly, "measured", now)},   # gemini: no weekly
 "reset_at": iso(primary reset_at), "marginal_cost": {"value": 0.0, "label": "synthetic"},
 "competence": None}
```

`effective_priority` comes from `resource_map._prioritise(rows, P, now)`, and the map is
`{"schema": 1, "policy": P, "resources": rows}`. Precondition asserted on every call: every
subscription row has marginal cost 0.0. The Claude pressure source is the map row; the
replay does not read usage.json.

### Serving entry

Walk the final chain in order. Local (`ollama/*`) and API (`openai/*`) entries are simulated
as failing on every call. This is the worst case: every call lands on a subscription. A
subscription entry serves when its sub's **true** primary and weekly are both < 1.0;
otherwise it fails over to the next entry. The first entry that serves is the serving
entry, and its sub gets `inc`. If no entry serves, the call is `unserved`; it is counted
and is not a case.

### Case, crossing, non-vacuity

* `pmax(X)`: the known pressure of X as the map shows it before the call (section 1 rule).
* X is **eligible for the call** when all of these hold:
  * `_eligible(row_X, {"task_type": tt, "needs": ()})` is None;
  * X is not claude when the class is qa (D-116);
  * X has at least one entry in the final chain;
  * X's true primary and weekly are both < 1.0.
* **Case:** a served call whose serving sub S has known `pmax(S)`, with `pmax(S) >= 0.95` or
  `pmax(S) + inc[S] >= 0.95` (crossed by this call), while some other eligible E has known
  `pmax(E) < 0.80`. A crossing caused by external use is not a routed call, so it is never a
  case: the router could not avoid it.
* **Crossing** for (P, K, A, B): a step where `pmax(A)` goes from < 0.95 to >= 0.95 while
  `pmax(B) < 0.80`, with both eligible for class K.
* **Exposure** for (P, K, A, B): the number of routed class-K calls with
  `pmax(A) >= 0.95`, `pmax(B) < 0.80`, and A and B both eligible.
* **Unknown-pressure check (D-122b)**, on every call of the unknown segment, in the
  score-pass output:
  * while `pmax(claude) < 0.95`, every anthropic and every gemini_cli entry precedes
    every codex entry;
  * once `pmax(claude) >= 0.95`, every codex entry precedes every anthropic entry, and
    gemini_cli (still < 0.95) precedes codex.

  Both phases must occur at least once.
* **Within-provider order:** on every call, the entries of each provider appear in the
  final chain in the same relative order as in BASE.

### Runs

| Run | Map policy | Weights | `SCORE_PMAX_HIGH` | Purpose |
|---|---|---|---|---|
| R1 | `balanced` | real | 0.95 | main |
| R2 | `cost` | real | 0.95 | main, with the priority term active |
| R3 (S1) | `cost` | real | **2.0** (high-pressure group off) | tests the weights on their own, without the D-122b group |

R3 patches only `SCORE_PMAX_HIGH`, as a module attribute. It uses its own BASE[`cost`]
and the same generator and seed.

### PASS (each run separately)

* N >= 1,000 routed calls, and all preconditions hold.
* For each of the 8 (K, A, B) pairs: crossing >= 1 **and** exposure >= 20, reported per pair.
* Cases = 0.
* The unknown-pressure check holds on every call, and both of its phases occur (R1 and R2
  only). In R3 the group is off by design, so its second phase cannot hold there. R3 records
  the check's outcome without asserting it.
* Within-provider order holds on every call.
* The D-116 invariant holds on every Q&A chain:
  * the anthropic indices in the score-pass output equal those in BASE (held);
  * in the final chain, the local entries form one block placed directly after the last
    non-Claude, non-local entry (the `strip_local_for_qa` rule).

  This is not "before the first anthropic entry": under `cost`, BASE already has a
  Claude entry (`claude-haiku`) ahead of `openai/gpt-4o` (see the probe under "Function under test"), and D-116
  places locals after the last non-Claude entry.

### Negative control (shows the case rule can fire)

Same generator, seed and BASE chains (each policy run's `BASE[P]`). The control
patches **module attributes** of `llm_router.resource_map` with `monkeypatch.setattr`.
`score`, `order_resources`, `order_chain` and `_quota_score_chain` must read every `SCORE_*`
constant from `llm_router.resource_map` at call time. None of them may import a constant by
value (`from ... import SCORE_W1`) or bind it as a default argument. The patches are:
`SCORE_W1 = SCORE_W2 = SCORE_W3 = 0.0` and `SCORE_PMAX_HIGH = 2.0`. The last one disables
the D-122b high-pressure group, which would otherwise sink A by itself. Map policy
`cost`. Effective priority is overridden as follows: during a pair segment, A = 1 and the
others keep their `cost` order after A; in free phases and the unknown segment, it follows
`cost`. That is a quota-blind router whose static order prefers A. Run once alongside each
of R1 and R2, on that run's BASE; state evolves with the control's own routing.

PASS of the control: >= 1 case for every one of the 8 pairs, and >= 50 cases in total over
the two runs. The control fails (red test) if it does not fire.

Output: a JSON summary per run, written to `tmp_path` and printed under `pytest -s`, with
N, unserved, crossings, exposure and cases per pair, and control cases per pair.

## 6. P2.3-b fuzz

* `rng = random.Random(2304)`, 500 cases.
* Each case has `rng.randint(1, 6)` rows. Each field is independently, with p = 0.5, a valid
  value, else a uniform draw from the unknown pool. The fields are `resource`, `kind`,
  `chain_id`, `status`, `capabilities`, `quota`, and each quota_value's `value`,
  `provenance` and `as_of`, plus `reset_at`, `marginal_cost` and its `value`,
  `effective_priority` and `competence`.
* Unknown pool: key missing, `None`, `"unknown"`, `""`, `float("nan")`, `float("inf")`,
  `-1`, `True`, `False`, `[]`, `{}`, `object()`, and a `Poison()`. `-1` stays in the pool:
  for cost and pressure it is out of range, so unknown.
* Decision fields (`task_type`, `needs`, `now`, `priority_ordered`) are drawn the same way,
  and so is the decision itself (missing or None).
* `Poison` defines `__float__`, `__int__`, `__index__`, `__bool__`, `__add__`, `__radd__`,
  `__sub__`, `__rsub__`, `__mul__`, `__rmul__`, `__truediv__`, `__rtruediv__`, `__lt__`,
  `__le__`, `__gt__`, `__ge__`, `__neg__`, `__abs__` and `__round__`. Each increments a
  class-level touch counter and raises `AssertionError`. The test asserts the counter is 0
  at the end, so a broad `except` cannot hide a touch.
* Oracle: the test computes knownness on its own with the rules of sections 1-2, written
  independently of the module.
  * `bool` is never a number. `effective_priority` `True`/`False` is unknown.
  * A str `status` other than `"connected"` and a non-sequence `capabilities` are filtered.
* Explain record shapes:
  * eligible row: `{"resource": str | "unknown", "score": finite float, "rank": int >= 1,
    "terms": {"priority", "pressure", "reset", "marginal_cost"}, "inputs": {...}}`. Each
    term is a finite float or `"unknown"`.
  * filtered row: `{"resource": ..., "filtered": str}`.
* PASS:
  * all 500 cases run;
  * `score`, `order_resources` and `order_chain` never raise;
  * `order_resources` returns exactly the rows the oracle calls eligible, in the section 2
    key order;
  * every term the oracle calls unknown is `"unknown"` in the explain record;
  * at least 1 case has all four terms unknown;
  * the Poison touch counter is 0.

## 7. Door tests (pre-registered fixtures and expected outputs)

Common fixture, map `M1`:

* policy `balanced`;
* subscription rows for claude, codex and gemini_cli, as in section 5, with no
  `reset_at` and marginal cost 0.0;
* `pressure` measured: claude 0.20, codex 0.97, gemini_cli 0.10;
* written as a fresh file with `resource_map.write_map`, with a 1 ms mtime bump.

`BASE_D = ["ollama/qwen3:8b", "anthropic/claude-sonnet-4-6", "codex/gpt-5.5",
"gemini_cli/gemini-2.5-pro", "openai/gpt-4o-mini"]`, where `ollama/qwen3:8b` is
user-configured so P0.15 keeps it.

Score order under M1: gemini_cli (-1.0, group 0), then claude (-2.0, group 0), then codex
(group 2).

| ID | Setup | Expected |
|---|---|---|
| D1 TQ-007 + score | `route_and_call` with the harness of `tests/test_tq007_daily_cap_downgrade.py`; profile QUOTA_BALANCED; task CODE; `_build_and_filter_chain` returns `BASE_D`; M1; bandit off; dispatch captured | no cap: `[ollama/qwen3:8b, gemini_cli/gemini-2.5-pro, anthropic/claude-sonnet-4-6, codex/gpt-5.5, openai/gpt-4o-mini]`. Daily cap exceeded (free entries exist): `[ollama/qwen3:8b, gemini_cli/gemini-2.5-pro, codex/gpt-5.5]` |
| D2 Q&A hold | as D1 with no cap, task QUERY | score-pass output `[ollama, anthropic, gemini_cli, codex, openai]` (anthropic held at index 1). Dispatch chain after the D-116 strip: `[anthropic/claude-sonnet-4-6, gemini_cli/gemini-2.5-pro, codex/gpt-5.5, openai/gpt-4o-mini, ollama/qwen3:8b]` |
| D3 door off | as D1 with no cap, profile BALANCED, and also with `model_override="codex/gpt-5.5"` under QUOTA_BALANCED | `_quota_score_chain` is not called. The BALANCED dispatch chain is identical under M1 and under M1 with the pressures swapped |
| D4 agent-route gate | `_codex_quota_spread_declines("code", "general-purpose", "moderate", "s")` with `LLM_ROUTER_PROFILE=quota_balanced` | (a) M1 (claude 0.20, codex 0.97): True, outcome `quota_spread`. (b) claude 0.97 / codex 0.10: False. (c) codex `default` 0.0 / claude 0.40: True. (d) codex `default` / claude 0.96: False. (e) `map_reader.load_data` raises `RuntimeError`: False, plus one `failopen` record `CHZ-FO-AGENT-ROUTE-QUOTA-SCORE`. (f) env unset: False. (g) no map: False. (h) in a fresh subprocess (env `PYTHONPATH=<worktree>/src`, scratch HOME), load the hook module and run (a): `"llm_router.router" not in sys.modules` afterwards |
| D5 map filter-only | `_build_and_filter_chain` with the `_FakeConfig` of section 5, task CODE. Map `M2`: policy `cost` (orders); rows connected for ollama/qwen3:8b (local, prio 3), codex (prio 1), gemini_cli (prio 2) and claude (prio 4), and `openai/api` with status `absent` | QUOTA_BALANCED with M2 equals QUOTA_BALANCED with no map (`load_view -> None`) minus every `openai/*` entry (filter on, reorder off). BALANCED with M2 differs from BALANCED with no map minus `openai/*` (reorder on, existing behaviour) |
| D6 within-provider | `order_chain(["codex/a", "anthropic/x", "codex/b", "gemini_cli/g", "anthropic/y", "ollama/l"], M1, {"task_type": "code"})` | `["gemini_cli/g", "anthropic/x", "anthropic/y", "codex/a", "codex/b", "ollama/l"]`. Within each provider, input order is kept; the local keeps index 5 |
| D7 no fresh map | `live_subscription_rows` patched to return M1's shape; no map file | `_quota_score_chain(BASE_D, CODE)` gives the D1 no-cap order |
| D8 compatibility builder | `chain_builder.build_chain(TaskType.CODE, "moderate", QUOTA_BALANCED)` with `_build_dynamic_chain` returning `BASE_D`, subscription-local inactive, M1 | D1 no-cap order. With BALANCED: `BASE_D` unchanged |

How D6 is derived: slots 0-4 hold subscription entries and slot 5 holds the local. The
movable entries are stably sorted by provider rank (gemini_cli, anthropic, codex) into
g, x, y, a, b and written back into slots 0-4.
