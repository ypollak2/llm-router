# Model resolver (classifier-v2 release, P3)

Maps an abstract tier (EASY / MEDIUM / FRONTIER) plus needs (tools, vision, long
context, thinking, local-only) to a model the user can actually run. Not wired
into live routing: it is a library plus three inspection commands.

Rule (classifier-v2 plan, owner decision 4): **any setup can be represented;
active routing requires a verified execution path and qualified capabilities.
Missing coverage preserves the configured model or asks the user.**

| Command | Does | Spends |
|---|---|---|
| `llm-router inventory [--json] [--save]` | detect models, route, path, quota, privacy, capabilities | nothing; read-only |
| `llm-router calibrate [--models ..] [--budget-s N] [--tokens N] [--allow-paid] [--dry-run]` | short capability probes per model | nothing for local models; cloud only with `--allow-paid`, estimate printed first |

## Inventory

Each model carries: provider, route kind (`subscription` / `api` / `local`),
execution path verified (yes/no and why), authorized (yes/no and why), quota state
and pressure, privacy (`local` / `cloud`), declared capabilities (tools, vision,
thinking, json), and context window with its source.

| Source | What is read | Never read |
|---|---|---|
| Ollama | `/api/tags`, `/api/show` (capabilities, context length), `/api/ps` (loaded) | anything else |
| Claude Code subscription | `claude` binary path; cached `usage.json` via `proxy/quota_pressure.py` | credentials |
| Codex | binary path, `codex login status` (classified ChatGPT / API key / none; raw output dropped) | credentials |
| Gemini CLI | binary path, whether a login file exists | its contents |
| API providers | environment variable NAMES that are set | values |
| Benches / pressure | `provider_reset`, Codex request counter, curated registry (`config/models.yaml`) | |

Unknown stays unknown: an unreadable or stale quota reading is `unknown`, never
0%; an Ollama build that reports no capabilities leaves them `unknown`, never
`no`. A remote Ollama host (not loopback) is treated as non-local for privacy,
and its URL is stored without userinfo.

An API key is **not** a verified path. API models stay `path verified = no` until a
calibration round-trip succeeds (`calibrate --allow-paid`).

## Calibration

Five probes, each graded deterministically:

| Probe | Pass condition |
|---|---|
| `json` | reply parses; `a == 17`, `b == "x"` |
| `edit` | the old/new pair applies exactly once to a fixture and the result is the expected program (compared by AST; model text is never executed) |
| `tool_call` | calls `lookup(key="alpha")`, then uses the tool result in its answer |
| `vision` | only where vision is claimed: reads 3 random digit codes exactly |
| `long_context` | recalls a needle from mid-prompt at a stated size (default 8000 tokens; skipped if the window is smaller) |

A probe that was not run, or cannot run on the route, is `ok=None`, which is not a
pass. Tier ceilings are derived per model and stored with their basis:

* EASY: `json` and `edit` pass.
* MEDIUM: EASY, plus a tool round-trip (if the model is meant to use tools) and recall at >= 8000 tokens.
* FRONTIER: **short probes cannot establish it.** A FRONTIER ceiling is granted only
  when a measured MEDIUM is combined with the registry's premium class, and is
  labelled `prior`.

Results go to `state_path("capability_profile.json")` (versioned, merged per
model). They describe this setup on the day measured. They are not a ranking.

### Reference priors (not encoded anywhere)

From the 2026-10-01 routing experiment, as supplied with the build brief: qwen3.6
10/20, Codex (astra) 15/20, Claude 15/20, qwen3-coder 4/20. N=20 per model on one
task set; the experiment's own graders disagreed on magnitude (see
`~/.rsi/research/routing-experiment-2026-10-01/results_p2.md`), and Astra's review
notes that a 15/20 tie is weak evidence of equivalence. They are shown here to
explain why a local model is measured before it is trusted (4/20 and 10/20 are
far apart), not as a user-independent order. No code reads them.
