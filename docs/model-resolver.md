# Model resolver (classifier-v2 release, P3)

Maps an abstract tier (EASY / MEDIUM / FRONTIER) plus needs (tools, vision, long
context, thinking, local-only) to a model the user can actually run. Not wired
into live routing: it is a library plus three inspection commands.

Rule (classifier-v2 plan, owner decision 4): **any setup can be represented;
active routing requires a verified execution path and qualified capabilities.
Missing coverage preserves the configured model or asks the user.**

| Command | Does | Spends |
|---|---|---|
| `llm-router inventory [--json] [--save] [--verify]` | detect models, route, path, quota, privacy, capabilities | nothing; read-only. `--verify` makes one zero-cost list-models call per API-key provider (no tokens) |
| `llm-router calibrate [--models ..] [--budget-s N] [--tokens N] [--allow-paid] [--yes] [--max-usd N] [--dry-run]` | short capability probes per model | nothing for local models; cloud only with `--allow-paid`, estimate and total printed first; more than 3 paid models without `--models` needs `--yes`; refuses above `--max-usd` (default 1.00) |

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

**Privacy is decided per model, not per server.** A loopback Ollama can list models
it does not run: Ollama's cloud models appear in `/api/tags` with `remote_host` /
`remote_model` (also checked in `/api/show`) and a `-cloud` / `:cloud` tag. Any of
those marks the model `privacy=cloud` on a `subscription` route, so `local-only`
never selects it and `calibrate` treats it as paid. The server URL is parsed with
`urlsplit(...).hostname` (never string-split), and only a loopback host counts as
local. **Limit:** an SSH tunnel that forwards a remote GPU box to `localhost` is
indistinguishable from a local server; treat such a setup as local only if you
accept that.

Unknown stays unknown: an unreadable or stale quota reading is `unknown`, never
0%; an Ollama build that reports no capabilities leaves them `unknown`, never
`no`. A remote Ollama host (not loopback) is treated as non-local for privacy,
and its URL is stored without userinfo.

An API key is **not** a verified path. API models (and a Codex login that uses an API
key) stay `path verified = no` until checked:

* `inventory --verify` (or `resolve --verify`) makes one authenticated list-models
  call per provider (no completion, no tokens). 200 sets `path verified = yes`;
  401/403 sets it `no` and `authorized = no` with the status in `path_detail`; no
  response or any other status leaves it unverified, labelled as such. The key goes
  only to its own provider's official https endpoint (`resolver/auth_ping.py`), in a
  header, never logged or stored; results are cached for 10 minutes under a
  12-character fingerprint (file mode 0600), never the key. Redirects are never followed (a
  redirect would forward the key to another host) and proxy environment variables are
  ignored; a 3xx reads as "unexpected status", i.e. unverified. Perplexity has no zero-cost endpoint and
  is not pinged. Moonshot keys issued on the China platform (`.cn`) are rejected by the
  international endpoint and read as unverified. The endpoint table is pinned by a test and
  was checked on 2026-10-04 with a keyless GET per URL (all answer 401/403, none redirect). Off by default so `inventory --json` is stable.
* `calibrate --allow-paid --models <one>` for a full capability measurement (also
  verifies the path). Codex credentials are never read, so a Codex API-key login can
  only be verified this way.

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
pass. Nothing a model or provider said is ever written to the profile: a probe stores
`ok` plus a fixed reason string (`wrong value`, `no response`, `provider error`, ...).
If the run is inconclusive (every probe hit a transport error, or json/edit did), the
model is reported `unreachable, not recorded` and the earlier measurement is kept. Tier ceilings are derived per model and stored with their basis:

* EASY: `json` and `edit` pass.
* MEDIUM: EASY, plus a tool round-trip (if the model is meant to use tools) and recall at >= 8000 tokens.
* FRONTIER: **short probes cannot establish it.** A FRONTIER ceiling is granted only
  when a measured MEDIUM is combined with the registry's premium class, and is
  labelled `prior`.

Results go to `state_path("capability_profile.json")` (versioned, merged per
model). They describe this setup on the day measured. They are not a ranking.

### Reference priors (not encoded anywhere)

From `~/.rsi/research/routing-experiment-2026-10-01/`, n=20 tasks each, **different
harnesses, so they are not a like-for-like ranking**:

| Figure | Harness | Source |
|---|---|---|
| Claude 15/20 (75%) | Claude-only, plan_implement arm A | `plan_implement/analysis_output.txt` |
| qwen3-coder 3/20 (15%) | local one-shot, plan_implement arm C | `plan_implement/analysis_output.txt` |
| Codex gpt-6-astra 15/20 (75%) | Codex with tools (Claude 15/20 on the same set) | `codex_agent/analysis_output_v2.txt` |
| qwen3.6-35b-a3b-coding 10/20 (50%) | local agent loop, arm f | `local_agent/analysis_new_models.txt` |
| qwen3-coder 4/20 (20%) | local agent in Pi (f_regraded) | `local_agent/analysis_new_models.txt` |

Wilson 95% intervals are wide (for 15/20, [53%, 89%]); Astra's review notes a 15/20 tie
is weak evidence of equivalence. They are shown to explain why a local model is
measured before it is trusted, not as a user-independent order. No code reads them.
