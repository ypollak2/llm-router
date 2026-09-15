# Plan: the same context for every model, every time

Written 2026-09-14. Supersedes nothing; it is the follow-on from the draft-quality
and wall-clock work committed in `445108c`.

## The problem in one table

`context_injection.inject()` was built as the choke point that guarantees every
execution path gets repo knowledge. It reaches `direct_executor`, `codex_agent`,
`gemini_cli_agent`, `claude_agent`, `agent_loop` and `local_task`, and
`tests/test_okf_choke_point.py` enumerates them so a new path cannot skip it.

It carries **only OKF**. Verified:

| Context source | Hook draft path | MCP tools / Codex / Gemini |
|---|---|---|
| OKF repo documents | yes | yes |
| Conversation history | yes | **no** |
| Tool facts — what was actually done | yes | **no** |
| `build_session_context` calls | 2 (`hooks/auto-route.py`) | **0** (`router.py`) |

So a prompt routed through `llm()` or Codex gets documents and no idea what
happened in the session. The enforcement landed on one third of the context.

## Baseline to beat

Three measurements exist. Only the third describes production.

| Design | Drafts | Acceptable | Status |
|---|---|---|---|
| One shared session id, shuffled corpus | 72% | 55% | **withdrawn** — 29 of 144 drafts carried "Gable 5" into unrelated prompts |
| Isolated empty session per prompt | 44% | 33% | floor — 95 of 200 gated with nothing to resolve them |
| Real sessions, real order, real history (`bench_session_replay.py`) | **76%** | **66%** | n=115, complete; this is the baseline |

The completed session-ordered replay shows ZERO `context-dependent` gates. The only
recorded blocker is `no free-tier model available` (complex code/research routed
to Claude by design, the user's own carve-out).

Any change below must be measured against `bench_session_replay.py`, not against
the withdrawn or floor numbers.

## Steps

### 1. Widen the choke point from OKF to all context  — the keystone
`context_injection.inject()` assembles OKF plus session conversation plus recent
tool facts, instead of OKF alone. Every caller inherits it; the existing choke
point test keeps new execution paths honest.

* Touches: `src/llm_router/context_injection.py`, and `router.py` to route its
  OKF call through the widened function rather than calling `okf` directly.
* GATE: a Codex or `llm()` call in a session with history receives the session
  block. Test asserts the block is present for a non-hook path, which fails today.
* Risk: low. Additive, behind `LLM_ROUTER_CONTEXT_INJECTION` like the rest.

### 2. Budget and order the context
Context is not free — the first model has ~37s. Priority order by what resolves
the most prompts: recent tool facts, then last N conversation turns, then OKF
docs, truncated to a token ceiling.

* GATE: payload size measured before and after; p90 stays under the ceiling and
  the replay's draft rate does not fall.
* Risk: low, but it must be measured rather than assumed — the current payload is
  ~457 tokens and nothing has ever been sized against the clock.

### 3. Stop truncation destroying the tool facts
`context-capture.py:91-92` uses `_stringify(tool_input, 200)` and
`_stringify(tool_result, 500)`. Real session data: 128 tool events, p50 661
chars, p90 710, max 711 — pinned at the ceiling. The filenames and branch names
that continuation prompts point at are cut off before they are ever stored.

* Raise the limits for high-signal tools (Write, Edit, Bash) only.
* GATE: re-capture a session; the share of events sitting at the cap falls, and
  a prompt naming a recently written file resolves where it did not before.
* Risk: near zero — same data, less lossy. Storage grows; cap it.

### 4. Structured facts slot
`{branch, head_sha, last_tool, last_command_head, last_exit_code}`, refreshed
from `git` on every `PostToolUse`, overwritten in place rather than appended,
unconditionally included (~20-40 tokens).

* GATE: "check if windows was already failing on main" and similar branch-aware
  prompts resolve; the slot is never populated by a model's own text.
* Risk: low ONLY under the invariant below.

### 5. Relax OKF's anchor gate for session-local tokens
`okf.py:643` requires a >=6-char identifier-shaped token, so "W3", "Q-L" and
"the run" can never match.

* Scope the relaxation to tokens seen in THIS session. Never the bulk index —
  that is where precision collapsed before (OKF-INDEX-01).
* GATE: short-reference prompts resolve; bulk-index precision is unchanged,
  measured on the same corpus.
* Risk: moderate. Do this last, and revert on any precision regression.

## The invariant

**Store and inject observations, never claims.** A command's exit code is a fact;
a model's sentence about it is not.

The reason is on record: a draft's invented "63.2% complete (5,309/8,400)" was
written into session memory and became the next turn's context, where it escalated
to "78.5% complete (6,600/8,400)" — for a project that does not exist. Gating that
write (`grounding.draft_is_memorable`, `445108c`) closed it.

Therefore:
* Only `PostToolUse` writes structured facts. A routed model can never write to
  them, only to the prose log.
* Anything derived from model output goes through `draft_is_memorable` first.
* The existing `SENTINEL_OPEN`/`SENTINEL_CLOSE` guard (`session_store.py:85`) must
  wrap any new injected block, so injected context is never re-recorded as new
  ground truth.

## Out of scope, and why

* **A new short-term memory or cache subsystem.** The store, the retrieval and
  the rescue arms all exist; `session_store` already holds 128 tool-call events
  in a real session alongside conversation. A fifth context source would
  duplicate it. Steps 1-5 are plumbing and tuning of what is there.
* **`semantic_cache` for continuations.** It caches prompt->answer by embedding
  similarity and has previously served one passport's answer for another at
  cosine >= 0.95. "keep going" is textually near-identical across unrelated
  sessions, so similarity caching here is a fabrication engine.
* **Live external state** — CI status, PR mergeability. No memory contains what
  nothing has queried. `_tool_loop_rescue` already exists for those prompts and
  is the correct arm.

## Order

1 is the keystone and is largely plumbing. 3 and 4 are cheap and independent.
2 must follow 1. 5 is last and most reversible-on-regression.

Nothing merges without a `bench_session_replay.py` run before and after.
