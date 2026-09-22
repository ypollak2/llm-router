# Git history forensics — repository historian, 2026-09-22 (round 3)

HEAD `357a402`, branch `fix/audit-2026-09-22`. 937 commits total. Method:
`git log`/`show`/`log -S`/`blame` (read-only) across the full history, cross-checked
against the two prior audit rounds (`audit/2026-09-21`, `audit/2026-09-22`) and
against the current tree via probes under an isolated `LLM_ROUTER_HOME`.

The last 30 commits are two remediation rounds: `aff5e81..8c7366b` (round 1,
2026-09-21 fixes, M-04/H-*/C-* ids) and `a23aff1..357a402` (round 2, 2026-09-22
fixes, T-*/F-*/S-* ids). Both rounds are unusually well-documented: every commit
message states what was measured before and after, and several document defects
the fix itself introduced and then closed in the same commit. That rigor is real
and should not be discounted by what follows.

---

## Recurring bug classes, with counts and dates

### Class 1 — Import-time / static binding of a path or config table that should be resolved per-call

The single largest recurring class in this repository's history. Each instance is
independently discovered, independently fixed, and declared to close the class —
and the next instance surfaces within days, sometimes hours.

| Date | Commit | Instance |
|---|---|---|
| 2026-05-30 | `c0c8d1b` | Agent usage written to an orphan `llm_usage.db` instead of the canonical `usage.db` — a hardcoded path diverged from the one everything else reads |
| 2026-06-02 | `6023e8d` | `ROUTING_TABLE` hydrated from `standard.yaml` **at module-import time** (same class, config not path: a value that should track runtime state is frozen at first import) |
| 2026-08-26 | `f7051a0` | test suite found writing to the developer's real repo/machine config |
| 2026-09-04 | `7b673f1` | test suite found touching real host configs again |
| 2026-09-10 | `eb76237` | `cost.py` savings-log path resolved once, not per call |
| 2026-09-21 | `aff5e81` | **repo-wide sweep**, "resolve runtime state paths at access time, not at import" (M-04) — declared as closing the class |
| 2026-09-22 | `c05bb7e`, `8c7366b` | **two more instances found the same day**, one in the dashboard auth-token path, both filed under the same M-04 id as the sweep that was supposed to have caught them |
| 2026-09-22 | `fe91cbf` | `_CLAUDE_DIR = Path.home() / ".claude"` and 3 siblings in `install_hooks.py` — survived the sweep because the sweep scoped itself to `~/.llm-router` *state*, and this is host *config* |
| 2026-09-22 | `d766ec6` | `agentic/telemetry._db_path()` ignored `LLM_ROUTER_HOME` — "a survivor of the repo-wide T00b sweep... it simply never asked the resolver" (commit's own words). Caused a real, if small, contamination of the operator's live `usage.db` (see FROZEN_STATE.md) |
| 2026-09-22 | `a1ce804` (T-18) | `commands/profile.py` imported `PROFILE_PATH`, a constant the *same sweep* had replaced with a function `_profile_path()`, and never updated the one caller |

**Count: at least 9 dated instances across 4 months**, 5 of them surfacing on the
single day the class was declared closed by a repo-wide sweep. This is not a
class that is being eliminated structurally — it is a class being played
whack-a-mole with, one call site at a time, by an author who is honest enough to
name each recurrence in the commit message.

**A tenth, still-live instance, not in any commit**: `src/llm_router/claude_jsonl_usage.py:15`

```python
_CC_DIR = Path.home() / ".claude" / "projects"
```

is a genuine module-level constant, frozen at import, in a file none of the
above commits touched. It has one production caller
(`dashboard/tui.py`), reached via `base = cc_dir or _CC_DIR`, and no test file
references `claude_jsonl_usage` at all — so nothing would currently catch a
regression here, and nothing caught this one being added. Confidence: **CONFIRMED**
by direct read; not yet reproduced with a failing scenario, so classify the
*risk* as **STRONGLY SUPPORTED** rather than proven-broken today (its only
caller doesn't appear to need `LLM_ROUTER_HOME` redirection since it reads
Claude Code's own directory, not llm-router's — but a test that tries to fixture
it, the exact mechanism that bit every prior instance, will hit this).

### Class 2 — Provider family name divergence (`'gemini'` vs `'google'`)

T-20 (`26a80c8`, 2026-09-22) fixed exactly one instance: `quota_tracker.py`
queried `provider = 'gemini'` while `model_registry.py` tags every real Gemini
model `provider = "google"`, so real Gemini spend read as $0. The fix created
`model_registry.GOOGLE_PROVIDERS` as the intended single source of truth and
rewired `quota_tracker` to use it.

**A second, independent enumeration of "valid provider strings" was not
updated, and it now rejects the very rows T-20 fixed the query for.**
`cost.py::_validate_routing_insert` (line 1624) hand-maintains its own
`VALID_PROVIDERS` allowlist:

```
{'ollama', 'openai', 'gemini', 'codex', 'claude_subscription', 'subscription',
 'anthropic', 'perplexity', 'groq', 'deepseek', 'cc', 'claude'}
```

`'google'` is not in it. `provider_from_model("google/gemini-1.5-pro")` (the
function that actually derives `LLMResponse.provider` for a real routed call,
`profiles.py:610`) returns `"google"`. Reproduced live, under isolated
`LLM_ROUTER_HOME`:

```
_validate_routing_insert('google/gemini-1.5-pro', 'google', 0.001)
  -> ValueError: invalid provider 'google'. Valid providers: [... no 'google' ...]
_validate_routing_insert('google/gemini-1.5-pro', 'gemini', 0.001)
  -> accepted
```

The call site (`router.py:2275-2368`) wraps the whole `log_routing_decision`
call in `except Exception as e: log.warning(...)` — so **every real Gemini
routing decision through the main router path raises inside the write, is
silently swallowed, and never reaches `routing_decisions`.** This is Class 2
recurring through a *different* table (`cost.VALID_PROVIDERS` vs
`quota_tracker`'s old inline query) than the one T-20 closed, introduced or left
behind by the same commit that fixed the sibling instance — `model_registry`
gained a canonical `GOOGLE_PROVIDERS` set in the same commit that this file was
not updated to consult.

**Confidence: CONFIRMED** (reproduced with a probe, `LLM_ROUTER_HOME` isolated,
no writes to real state). **Severity: CRITICAL** — this is not a cosmetic
mismatch, it silently drops real telemetry (quality ledger, bandit reward,
cost analytics) for an entire provider family, on the main execution path, and
stacks with Class 3 below (the failure is swallowed with no counter, no
`failopen.record()`, just a `log.warning`).

### Class 3 — Silent `except: pass` around a state-mutating write

Documented at scale by the 2026-09-22 audit (1046 broad excepts, 276 bare
`pass`, 84 wrapping a mutation) and partially addressed by `451565d`
(2026-09-22), which added a ratchet test and `scripts/silent_mutation_census.py`
and instrumented **4** of the highest-value sites (money/ledger data) with a
`failopen` counter.

Re-run at HEAD under isolated `LLM_ROUTER_HOME`:

```
PERSISTENCE sites with a bare `except: pass`: 88
```

(vs. 84 at the 2026-09-22 audit). **The population did not shrink — it grew by
4 while 4 specific sites were made observable.** The ratchet test bounds future
growth; it does not reduce the existing backlog. This is the honest reading of
`451565d`'s own framing ("Removing them all is a rewrite, not a remediation") —
worth stating plainly because a naive read of "we ratcheted it" implies
improvement, and the number went the wrong way.

This class goes back further than the audit rounds: `fc2ec37` ("revive
PostToolUse compression, dead ... since it shipped"), `d454e57`, `8d8fb01`
("never drop output silently"), `7b6d73e` ("when the hook loses evidence, it
now says so"), `71ae01e` ("make the fallback loud") are all the same shape —
a silent swallow discovered, fixed at that one site, with no structural change
that would prevent the next one — stretching back to at least mid-2026.

### Class 4 — Duplicated implementations that drift

**Secret scrubbers — now resolved at the call sites the last two audits found.**
`library/store.scrub_secrets` and `hooks/agent-route._scrub_agent_prompt` both
now delegate to `secret_scrubber.scrub_text` (`3dc57c5`); `hooks/auto-route.py`'s
early-boot fallback table now derives from the canonical table via
`_load_fallback_patterns()` rather than being hand-copied, with a comment that
names its own history of drifting (T-16/S-08). Verified at HEAD: all
non-canonical scrubber-adjacent files (`hooks/tool_intercept.py`,
`observability/core.py`) call `scrub_text` rather than defining their own
patterns. **This specific instance of Class 4 is closed**, and closed the right
way — by delegation, not by copying the current state of the canonical table
(which is exactly what broke it the first time: `hooks/auto-route.py`'s old
literal comment claimed to be "kept in sync" and drifted three secret classes
behind).

**But Class 4 recurred elsewhere in the same remediation window** — see Class 2
above: `model_registry.GOOGLE_PROVIDERS`/`OPENAI_PROVIDERS` vs
`cost.py::VALID_PROVIDERS` is a fresh, live pair of independent enumerations of
the same concept (valid provider identity) that disagree today. See
`15_DEAD_DUPLICATED_SUSPICIOUS_CODE.md` for the full duplicate-set inventory.

### Class 5 — Denominators that disappear

Documented extensively in this repo's own `CLAUDE.md` (auto-route-debug.log
54% test contamination, benchmark sandbox traffic, hand-typed fixture session
ids, near-duplicate stage reporting 0 collapses because it compared raw
whitespace tokens). In the audited window: T-26 (`calls` incremented before a
subscription-row skip, so "N calls, $X saved" quoted two populations — fixed in
`d766ec6` by making `calls`/dollars share one skip point) and T-21 (a
provenance cutover silently zeroed lifetime savings for pre-upgrade rows, with
the drop-count written to `provenance_meta` and read by nothing — fixed by
adding `provenance_exclusion_summary()` and wiring it into `doctor`). Both are
the same shape as the CLAUDE.md incidents: a rate or a total computed over a
population that quietly changed shape.

### Class 6 — Metrics that measure something other than their name

`is_real` claimed (in a code comment) to be filtered by every downstream query;
it was filtered by none (fixed in `d766ec6` — not by making `is_real` correct,
but by explicit deprecation: the column stays, `DEFAULT 1` stays for backward
compatibility, and a large docstring now says "DO NOT RELY ON `is_real` AS A
PROVENANCE FILTER", pointing at `provenance`/`is_simulated`/`production_only()`
instead). `direct_diagnostics` labelled every DIRECT failure `timed_out=True`
regardless of elapsed time (T-23, fixed). The bandit's "success" signal meant
"non-empty, not a deferral" while being read as a quality signal (T-09/T-10,
fixed by `quality_signal()` returning `(value, source)` and by a
`quality_degraded` field). `commands/demo.py` priced its baseline from a
constant per row regardless of what the row actually cost, so "savings" could
be negative and still print green (T-02, fixed).

---

## Is the defect rate falling?

**No — not yet, and the git history is unusually explicit about why.**

The remediation practice in this repo is genuinely rigorous: every fix in the
last 30 commits ships with a reproduction, a regression test, and — repeatedly —
an honest note that the fix itself surfaced a new defect, fixed in the same
commit (`d766ec6` lists three; `a23aff1`/`3dc57c5` were themselves round-2
fixes for round-1 verification gaps). That is real progress on *verification
rigor*.

What is not falling is the **rate of new sightings of the same five or six
classes**. The evidence:

1. Class 1 (import-time binding) had a "repo-wide sweep" commit on 2026-09-21
   that explicitly claimed to resolve the class, and produced five further,
   independently-discovered instances of the identical class over the next 24
   hours, one of which (`d766ec6`'s telemetry path) actually wrote a fake row
   into the operator's real database before it was caught (see
   `FROZEN_STATE.md`). A tenth instance (`claude_jsonl_usage.py`) is still live
   at HEAD, untouched by any commit or test.
2. Class 2 (`'gemini'`/`'google'`) was declared fixed in the exact same commit
   series and immediately recurred one file over, through a second
   independent enumeration that the fixing commit did not know existed.
3. Class 3 (silent write-swallows): the raw count went from 84 to 88 across the
   remediation. A ratchet was added — a rate-of-growth control, not a
   population reduction.
4. Class 4 (drifted duplicates): the one instance that was actually closed
   (secret scrubbers) was closed by *delegation*, which is a structural fix —
   the other duplicate set found in this round (provider allowlists) was not
   delegated, it was independently re-invented, which is exactly the failure
   mode Class 4 keeps recurring through.

**The structural fix that works — delegate to one canonical function/table
instead of copying its current state — is known in this codebase and was
applied successfully to secret scrubbing. It has not yet been applied
systematically to path resolution (10 independent call sites resolved
independently instead of through one lazy accessor pattern used everywhere) or
to provider identity (two independent sets instead of one imported constant).**
Until those two get the same delegation treatment secret scrubbing got, the
next audit round should expect to find an eleventh path instance and a third
provider-identity instance, in roughly the same 24-48 hour cadence as this
round did.

---

## Abandoned architecture, found in passing

`llm_router.enterprise.*` is imported (lazily, inside function bodies, deliberately
never at module top level) from at least 6 files — `server.py`, `identity.py`,
`quota_routing.py`, `control_plane/api.py`, `commands/audit.py`,
`plugins/__init__.py`'s own docstring — and does not exist anywhere in this
repository. `server.py:333` states this is intentional: "Enterprise-only
critical modules... intentionally [absent so] the published MCP server
refuse[s] to boot" rather than silently degrading. This is not dead code in the
usual sense — it is the seam of a proprietary fork, deliberately left visible
and fail-closed rather than stubbed. `commands/sse.py`'s `main_sse_secured`
(flagged as "shipped but broken" by the 2026-09-22 dead-code audit) is one call
site of this same pattern, not an isolated bug: the module imports cleanly
(verified at HEAD), the function raises `ImportError` only when actually
invoked, which is the same fail-closed shape as every other enterprise call
site in the tree. See `15_DEAD_DUPLICATED_SUSPICIOUS_CODE.md` for the
classification.
