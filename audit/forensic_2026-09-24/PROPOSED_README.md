# llm-router

llm-router routes prompts from Claude Code (and other coding agents) to cheaper or
local models when a routed answer is likely to be good enough, so you spend less
Claude-subscription quota or API money on turns that didn't need Claude at all. It runs
as an MCP server plus a set of host-specific hooks; it does not require any API key to
try.

**What it is not, up front:** it is not a proxy that intercepts and replaces Claude's
answers by default. On Claude Code and Codex, a routed answer is a *draft* — offered to
you as advisory context, never applied without Claude (or you) accepting it. See
[How routing actually works](#how-routing-actually-works) below before you form an
expectation of what changes on your machine.

Install: `pip install llm-routing` (this is the package name; the CLI is `llm-router`).
Then `llm-router install --host claude-code` (or `codex`, `gemini-cli`, `cursor`, ...).
See [Install](#install) for what each host actually gets.

---

## 30 seconds

- **What it does:** classifies each prompt, and — on hosts with a prompt hook installed
  — tries a free/local model first. If that draft looks good, it's offered to the agent
  as a hint; if not, the prompt goes to Claude/your paid model as normal. Nothing you
  currently do changes if you ignore the hints entirely.
- **Who it's for:** developers using Claude Code (or Codex/Gemini CLI/Cursor/etc.) who
  want to burn less subscription quota or API spend on simple queries, without giving up
  Claude for anything that actually needs it.
- **What "automatic" means, exactly:** on **Claude Code** and **Codex CLI**, installing
  wires a prompt-submission hook that runs on every prompt automatically. On **Cursor,
  VS Code, OpenCode, Copilot CLI, OpenClaw, Trae, Factory**, install gives you the MCP
  tools only — you (or your agent) call them explicitly; there is no automatic
  prompt-level routing on these hosts today. See the [host table](#hosts) below for the
  exact split — it is not the same for every host, and treating them the same is the
  single most common misunderstanding of this project.
- **What "savings" means:** see [Savings, honestly](#savings-honestly). Short version:
  most of what this project can measure about itself is *quota/spend avoided by drafts
  that were actually accepted*, and that number is close to zero on real, measured usage
  — not because the mechanism doesn't work, but because most drafts today are offered,
  not accepted. Read that section before you decide whether this is worth installing for
  the savings claim specifically, as opposed to the "free tier-1 triage" claim.

---

## How routing actually works

1. A hook fires on `UserPromptSubmit` (Claude Code, Codex — installed automatically by
   `llm-router install`).
2. Your prompt is classified (query / code / research / analyze / generate /
   coordinate).
3. If direct execution is enabled (default on) and the prompt looks answerable by a
   free/local model, llm-router tries that model directly — bypassing Claude entirely
   for this attempt.
4. **The result of step 3 is a draft, not a replacement**, unless you've explicitly
   turned on `LLM_ROUTER_ZERO_CLAUDE=1` (an opt-in mode that lets a successful draft
   become the actual turn with no further confirmation — read
   [docs/MEASUREMENT.md](docs/MEASUREMENT.md) before enabling it). In the default mode,
   the draft is injected as additional context / a hint; Claude still takes the turn.
5. Separately, and always available regardless of hooks: you or your agent can call any
   of the MCP tools directly (`llm`, `llm_route`, `llm_code`, etc.) to route a specific
   call.

**A measured fact about step 4, on the maintainer's own machine, as of this commit:**
0 of roughly 1,160 local drafts produced were ever accepted as the actual answer to a
turn. Separately, the project's own `draft_acceptance()` instrumentation recorded 0
drafts used of 44 offered on its last measured day, for ~773 seconds of local-model time
spent. This is not a hidden number — it is wired into `llm-router doctor` as the
`draft_acceptance` counter, and it is the honest answer to "does the local-first path
actually replace premium work" on that specific install. Your mileage will vary by
workload; run `llm-router doctor` on your own machine rather than trusting either
number as universal.

---

## Subscription vs. API-key routing

llm-router works two different ways depending on how you pay for Claude:

- **On a Claude subscription (Pro/Max):** you don't pay per token, so a "dollars saved"
  number computed as (Opus rate − actual rate) does not correspond to any real dollar
  you avoided. `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` tells the accounting layer this,
  and the honest, subscription-aware headline (`real_dollars_avoided_usd`) is `$0.00`
  for you by design — what you actually get is *quota preserved* (fewer Claude turns
  consumed against your weekly/session limit), not money. If you haven't set this
  variable, the tool defaults to treating you as pay-per-token, which is the wrong
  direction for most Claude Code users — check `llm-router doctor` and set it correctly.
- **Paying per token via an API key:** a routed draft that gets used genuinely avoids a
  real, chargeable API call. This is the only case where a dollar figure means an actual
  dollar.

Every dollar figure this tool prints should be read against which of these two you are.
It does not currently detect this for you reliably if you skip onboarding — see
`docs/MEASUREMENT.md` for the full caveat.

---

## Hosts

| Host | Prompt-level auto-routing | MCP tools | Tool-argument enforcement (PreToolUse) |
|---|---|---|---|
| Claude Code | Yes, installed by default | Yes | Yes — the only host where this is verified working end to end |
| Codex CLI | Yes, installed by default (`llm-router install --host codex` wires the same prompt hook Claude Code uses) | Yes | **No** — not yet verified; the tool-argument payload keys this needs have not been captured from a real Codex session |
| Gemini CLI | Yes (its own hook set) | Yes | No — Gemini CLI has no equivalent hook event for this |
| Cursor | No | Yes | No |
| VS Code, OpenCode, Copilot CLI, OpenClaw, Trae, Factory | No | Yes | No |
| Claude Desktop, GitHub Copilot | No — config snippet only, no hooks | Yes | No |
| "Pi (pi.dev)" | Not currently supported | Not currently supported | — |

Two things worth being explicit about, because our own internal docs have said both at
different points and shouldn't have:

- Codex CLI **does** get automatic, default-on prompt routing today, the same
  `UserPromptSubmit` mechanism Claude Code uses. It does **not** yet have the stronger
  guarantee (verified tool-argument rewriting before a tool call executes) that Claude
  Code has — that is a real, narrower gap, not "no auto-routing at all."
- There is no working `--host pi` today. If you're looking for Pi support, it isn't
  here yet.

Full per-host detail, including exactly what each install path writes to disk:
[guide/HOST_SUPPORT_MATRIX.md](guide/HOST_SUPPORT_MATRIX.md).

---

## Install

```
pip install llm-routing
llm-router install --host claude-code   # or codex, gemini-cli, cursor, vscode, ...
```

No API key is required to try it: the MCP tools and the hooks work with a local Ollama
install or with your existing Claude Code/Codex/Gemini CLI session. Setting a provider
API key (OpenAI, Gemini, Anthropic, OpenRouter, ...) unlocks routing to that provider
as well.

`llm-router doctor` after install tells you, for your specific machine: which hosts are
detected, whether your subscription/API mode is set correctly, and your real
draft-acceptance rate.

---

## Tools

The MCP server registers **70 tools** when nothing limits the surface
(`LLM_ROUTER_SLIM=off`). By default, only **12 "front-door" tools** are exposed
(`LLM_ROUTER_SLIM=consolidated`, the shipped default) — this cuts the schema footprint
your agent sees on every turn by roughly 77% (measured: ~14.7K tokens at `off` vs. ~3.4K
tokens at `consolidated`).

The 12 default tools are: `llm`, `llm_act`, `llm_audio`, `llm_edit`, `llm_image`,
**`llm_local_task`**, `llm_route`, `llm_router_admin`, `llm_router_agent_route`,
`llm_router_agent_start_session`, `llm_router_session`, `llm_router_status`.

**`llm_local_task` is on by default and can write files and run shell commands** on
your machine through a local/free model (see [Direct execution](#direct-execution-what-run_command-actually-is)
below before assuming "default tools" means "read-only tools"). Full tool reference,
including what each of the other 58 tools does when you turn the surface up: `LLM_ROUTER_SLIM=off`,
[guide/TOOLS.md](guide/TOOLS.md).

---

## Savings, honestly

There are three different things this project can report, and they are not the same
number:

1. **Real dollars avoided.** Only meaningful if you pay per API token. `$0.00` by
   design if you're on a Claude subscription (see above).
2. **Verified savings.** The subset of routed calls the hook actually observed replace
   a Claude turn (`realized`-gated). As of this commit, this figure is genuinely
   labeled and separated from the rest — and it is **$0.00** on the codebase's own live
   ledger, for the window measured. This is the number you should trust if you want to
   know "did this actually replace a Claude turn," not the headline dashboard total.
3. **Unfiltered lifetime total, as `llm-router status` currently shows it.** This
   number sums several tables, most of which have no provenance filter applied — on the
   codebase's own measured live ledger, this figure was roughly **3,500× larger** than
   the provenance-filtered "verified" figure for the identical underlying data, at the
   same instant. This is a known, documented gap in the current release (not a secret;
   see [docs/MEASUREMENT.md](docs/MEASUREMENT.md)), and it means: **do not treat the
   headline "All time saved" number on your dashboard as the number of real dollars or
   verified turns you've saved.** Use `llm-router doctor`'s `draft_acceptance` counter
   and the verified/unverified split it prints instead.

Per-host savings percentages you may see elsewhere (in older docs) are single-machine
observations with no stated sample size or measurement window — read them as anecdotes,
the same way this project already asks you to read its RouterArena benchmark results,
not as a range you should expect to reproduce.

---

## Privacy: what actually leaves your machine

- **Your prompt and any tool-call content**, when a routed model actually answers it —
  this is unavoidable; that's what "routing to a model" means. Which host receives it
  depends on your config and provider keys.
- **A local Ollama model** never sends anything off-machine, *unless* you (or a project
  you've opened) have set `LLM_ROUTER_OLLAMA_URL`/`OLLAMA_BASE_URL` to a non-default
  host — this is allowed today, by design, for legitimate remote-Ollama setups. If you
  haven't explicitly configured a remote Ollama host, this doesn't apply to you.
- **Secrets in your prompts and tool output** are scrubbed (API keys, tokens, PEM
  blocks, `.env`-shaped assignments, JWTs, etc.) before being written to local logs or
  sent to an alert webhook. This scrubbing is applied consistently across the paths we
  checked; one debug log (`auto-route-debug.log`) does not currently pass through the
  scrubber, though a manual sample of its call sites found only metadata (ids, model
  names, latencies), not prompt content.
- **A known, real gap as of this commit:** if you set `openai_compat_base_url` or the
  pxpipe URL (directly, or via a project's own `.env` file, which is trusted the same
  as your own config) without also validating the host, your real provider API key can
  be sent to that host. This is being fixed; until it is, only set these two values to
  a host you trust, and treat any project's `.env` file as untrusted input if you don't
  control it. Full detail: [SECURITY.md](SECURITY.md).

---

## Direct execution: what `run_command` actually is

llm-router can hand a local/free model the ability to read files, propose edits, and run
shell commands in your project, so it can attempt a task end-to-end rather than just
answering a question. Read this section fully before assuming any level of sandboxing
you haven't verified yourself:

- **File reads/writes/edits are sandboxed to your project directory**, including
  against path traversal and symlink escapes — this has been adversarially tested and
  holds.
- **`run_command` is not sandboxed the same way.** It blocks a specific list of
  destructive commands and dangerous flags (`rm -rf`, `git push --force`,
  `curl | bash`, etc.), but the programs it *does* allow — `python`, `python3`, `node`,
  and others — are general-purpose interpreters with **no** argument-level containment.
  A model using `run_command` can read, write, or exfiltrate anything your OS user
  account can, outside your project directory, including with
  `LLM_ROUTER_AGENT_WRITES=off` set (that setting only governs the `write_file`/
  `edit_file` tools, not `run_command`).
- **This is a disclosed, deliberate trade-off, not an oversight** — capability over
  containment, because a program-name allowlist alone cannot give you both. If you run
  llm-router's direct execution against an untrusted repository (one you didn't write,
  haven't reviewed, or don't trust the dependency tree of), treat it the same way you'd
  treat running that repo's own scripts: use OS-level containment (a container, a VM,
  a restricted user) if you need a real boundary. Full detail, including the exact
  command corpus that is and isn't blocked: [SECURITY.md](SECURITY.md).

---

## Providers

23 providers are wired in via a single adapter (anything [litellm](https://github.com/BerriAI/litellm)
speaks): Anthropic, OpenAI, Ollama (local), OpenRouter, Gemini, Perplexity, Groq,
DeepSeek, Mistral, Together AI, xAI, Cohere, fal.ai, Stability AI, Runway, Replicate,
ElevenLabs, OpenAI TTS, Moonshot, MiniMax, Zhipu, Arcee, HuggingFace. No API key is
required for the Ollama/local path or for using Claude Code's own subscription session.
Full list with cost tier and env var per provider: [guide/PROVIDERS.md](guide/PROVIDERS.md).

---

## Everything else

This README covers what a new user needs in the first minute. Everything else is one
click away and kept up to date separately:

- [guide/GETTING_STARTED.md](guide/GETTING_STARTED.md) — full walkthrough, CLI reference
- [guide/HOST_SUPPORT_MATRIX.md](guide/HOST_SUPPORT_MATRIX.md) — exact per-host install
  behavior
- [guide/PROVIDERS.md](guide/PROVIDERS.md) — every provider, model, cost tier, env var
- [guide/TOOLS.md](guide/TOOLS.md) — every MCP tool
- [guide/POLICIES.md](guide/POLICIES.md) — routing policies and how to tune them
- [architecture/](architecture/) — design history and current/target architecture
- [docs/MEASUREMENT.md](docs/MEASUREMENT.md) — how to measure your own savings/
  acceptance rate correctly, and the pitfalls this project has already hit doing so
- [SECURITY.md](SECURITY.md) — the full, honest capability/containment breakdown for
  direct execution, and the current known gaps
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to run the test suite (it's a full suite;
  budget real time for it) and submit changes

## Contributing

```
git clone ...
uv sync
uv run pytest tests/ -q   # full suite; thousands of tests, budget real time
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the house style and PR expectations.
