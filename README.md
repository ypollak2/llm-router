<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/hero-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/hero-light.svg">
    <img src="assets/readme/hero-light.svg" alt="llm-router routes AI coding prompts across free, budget, and premium model tiers." width="100%"/>
  </picture>
</p>

<h1 align="center">llm-router</h1>

<p align="center">
  <strong>Stop spending your Claude Pro/Max quota on questions a free model can answer.</strong>
</p>

<p align="center">
  llm-router hooks into your coding tool's own lifecycle, reads each prompt before the
  model does, and drafts an answer on a free or local model first. Zero API keys needed
  on a Claude subscription — routing runs through MCP tools and local models.
</p>

<p align="center">
  <sub><b>One caveat:</b> routed answers are advisory by default — Claude still takes the
  turn unless <code>LLM_ROUTER_ZERO_CLAUDE=1</code>. By default some tool calls wait until a
  prompt is routed; <code>LLM_ROUTER_ENFORCE=off</code> turns that off. Draft-rate and
  acceptance figures live in <a href="docs/MEASUREMENT.md">docs/MEASUREMENT.md</a>, with
  their n, window and conditions.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/llm-routing/"><img src="https://img.shields.io/pypi/v/llm-routing?style=flat-square&color=4F46E5" alt="PyPI"></a>
  <a href="https://pepy.tech/projects/llm-routing"><img src="https://static.pepy.tech/personalized-badge/llm-routing?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads" alt="PyPI Downloads"></a>
  <a href="https://github.com/ypollak2/llm-router/actions"><img src="https://img.shields.io/github/actions/workflow/status/ypollak2/llm-router/ci.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://github.com/ypollak2/llm-router/stargazers"><img src="https://img.shields.io/github/stars/ypollak2/llm-router?style=flat-square&color=F59E0B&v=2" alt="Stars"></a>
  <a href="https://github.com/RouteWorks/RouterArena"><img src="https://img.shields.io/badge/RouterArena-listed-F59E0B?style=flat-square&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMiAyTDMgN2w5IDUgOS01LTktNXpNMyAxN2w5IDUgOS01TTMgMTJsOSA1IDktNSIvPjwvc3ZnPg==" alt="Listed on RouterArena"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-10B981?style=flat-square" alt="License"></a>
</p>

<details>
<summary><b>📑 Table of Contents</b></summary>

- [Why install this](#why-install-this)
- [Quick Start](#quick-start)
- [Works With](#works-with)
- [How It Works](#how-it-works)
- [Features](#features)
- [Reference](#reference)
- [Savings](#savings)
- [Trust and Security](#trust-and-security)
- [Documentation](#documentation)
- [Enterprise, Contributing, License](#enterprise-contributing-license)

</details>

---

## Why install this

You are on a Claude Pro or Max plan, have not spent a cent beyond it, and still
hit the five-hour limit — not because you asked too much, but because *every*
prompt went to the premium model: "what does this error mean", "reformat this
JSON", "is the service up" drew the same quota as the question that needed it.

### Why a proxy cannot do this

Most routers here are a **proxy**: your agent forwards requests using *your
API keys*. That has a hard limit — a proxy cannot intercept a session
authenticated by a subscription, because there is no key to forward.

|  | Pays per token | Pays a subscription |
|---|---|---|
| What runs out | your invoice | your five-hour window |
| Needs API keys | yes | **no** |
| A proxy can help | yes | **no — nothing to intercept** |
| llm-router helps | yes | **yes** |

### Honesty as a feature

`llm-router status` reports **verified** and **unverified** savings
separately, each with its own n — not a blended percentage. This project does
not publish a general savings percentage; any number depends on your own
workload. See [Savings](#savings) below.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/why-route-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/why-route-light.svg">
    <img src="assets/readme/why-route-light.svg" alt="Animated benefits panel for llm-router showing cheaper routing, preserved quality, quota protection, and low-config setup." width="100%"/>
  </picture>
</p>

---

## Quick Start

```bash
pip install llm-routing        # installs the `llm-router` command
llm-router install             # wire up Claude Code (default host)
llm-router doctor               # check provider connectivity and setup
```

Works with **zero API keys** on a Claude Pro/Max subscription — routing goes
through MCP tools and local models. Adding provider keys widens the pool;
nothing requires them. See **[guide/PROVIDERS.md](guide/PROVIDERS.md)**. To
install into another host, see [Works With](#works-with).

---

## Works With

| Host | Install |
|------|---------|
| **Claude Code** (default) | `llm-router install` |
| **Codex CLI** | `llm-router install --host codex` |
| **OpenCode** | `llm-router install --host opencode` |
| **Gemini CLI** | `llm-router install --host gemini-cli` |
| **GitHub Copilot CLI** | `llm-router install --host copilot-cli` |
| **OpenClaw** | `llm-router install --host openclaw` |
| **Trae IDE** | `llm-router install --host trae` |
| **Pi (pi.dev)** | `llm-router install --host pi` |
| **Factory Droid** | `llm-router install --host factory` |
| **Claude Desktop** | `llm-router install --host desktop` |
| **VS Code** (native MCP) | `llm-router install --host vscode` |
| **Cursor** | `llm-router install --host cursor` |
| **GitHub Copilot in VS Code** (capability extension, no cost-routing) | `llm-router install --host copilot` |
| **Windsurf / Cascade** | `llm-router install --host windsurf` |
| **Kimi Code (Moonshot AI)** | `llm-router install --host kimi` |

`llm-router install --host all` installs or prints every host config in one
pass. Full per-host detail, including what each host genuinely cannot do:
**[guide/HOST_SUPPORT_MATRIX.md](guide/HOST_SUPPORT_MATRIX.md)**.

---

## How It Works

Hooks intercept the prompt before your coding tool's own model sees it. A free
regex heuristic classifies it instantly, scoring about half of real prompts
with confidence — **788 of 1,571 measured** (`scripts/measure_low_signal_rate.py`,
run 2026-09-23); the rest fall back to a default. A routable prompt gets a
draft from a free/local model first, then walks up the chain toward paid
models only if needed.

The draft is **advisory** — handed to Claude as an unverified hint, not a turn
replacement, unless `LLM_ROUTER_ZERO_CLAUDE=1` is set. Enforcement acts at the
tool-call level: `smart` (default) holds selected tool calls until the prompt
is routed; `off` disables that. Full modes and per-host overrides:
**[guide/GETTING_STARTED.md](guide/GETTING_STARTED.md)**.

---

## Features

- **Secrets never leave your machine.** A prompt containing an API key, token
  or private key routes to local models only — fail-closed.
- **Automatic fallback with circuit breakers.** A provider that fails or
  rate-limits is skipped, not retried into the ground.
- **You can see it working.** A status line, terminal title and OS
  notification show the last model routed, savings and health.
- **Session-end summary.** Savings vs baseline, tier mix, per-provider cost,
  latency p50/p95/p99 and top routes.
- **Media and pipelines too.** `llm_image` / `llm_video` / `llm_audio`, and
  `llm_orchestrate` for multi-step research.

---

## Reference

The default MCP surface shows **12 front-door tools**; `LLM_ROUTER_SLIM=off`
shows everything registered.

| Topic | What | Guide |
|-------|------|-------|
| CLI | `install`, `status`, `gain`, `doctor`, `okf index/status`, `sessions status` and more | [guide/GETTING_STARTED.md](guide/GETTING_STARTED.md) |
| Providers | Free-first — Ollama (local), OpenRouter, Gemini, Groq, your Claude subscription | [guide/PROVIDERS.md](guide/PROVIDERS.md) |
| Routing Policies | `conservative` → `balanced` (default) → `cost_aggressive`; thresholds and YAML schema | [guide/POLICIES.md](guide/POLICIES.md) |
| MCP Tools | Every tool with its signature | [guide/TOOLS.md](guide/TOOLS.md) |

---

## Savings

Savings figures are a *counterfactual* — what the same tokens would have cost
at API list price, against what was actually spent — not money saved on a flat
subscription. `llm-router status` and `llm-router savings-report` split
**verified** from **unverified** savings, each with its own n. Methodology,
assumptions and limitations: **[docs/MEASUREMENT.md](docs/MEASUREMENT.md)**.

---

## Trust and Security

llm-router runs entirely on your machine — no hosted proxy, nothing sent to an
llm-router service, no account required. A **grounding check**
(`src/llm_router/grounding.py`) discards any draft citing a file or function
absent from its context and the indexed repo, falling through to Claude.

`LLM_ROUTER_DIRECT_EXECUTION` (on by default) lets a local model propose file
writes and commands under a confined, allowlisted path — it does not stop
targeted deletes, `git push --force`, or exfiltration. Full analysis:
**[SECURITY.md](SECURITY.md)**.

Self-audits are published in **[audit/](audit/)** and
**[docs/repo_goals/AUDIT-2026-09-25.md](docs/repo_goals/AUDIT-2026-09-25.md)**
(13 met, 6 partial, 1 not met), including negative results.

**Ground Truth accumulation** (opt-in, `LLM_ROUTER_GROUND_TRUTH=1`) builds a
corpus of replayable routing decisions instead of unlinked telemetry; off by
default since it's the only part that writes prompt text to disk. Details:
**[guide/GROUND_TRUTH.md](guide/GROUND_TRUTH.md)**.

---

## Documentation

Full index: **[guide/README.md](guide/README.md)**

| Document | Purpose |
|----------|---------|
| [Quick Start (2 min)](guide/QUICKSTART_2MIN.md) | Fastest path to working routing |
| [Getting Started](guide/GETTING_STARTED.md) | Full setup walkthrough |
| [Host Support Matrix](guide/HOST_SUPPORT_MATRIX.md) | Per-host feature comparison |
| [Providers](guide/PROVIDERS.md) | Provider setup and model recommendations |
| [Routing Policies](guide/POLICIES.md) | `routing.yaml` schema and authoring your own policy |
| [Tool Reference](guide/TOOLS.md) | All MCP tools with examples |
| [Architecture](guide/ARCHITECTURE.md) | Internal design and module structure |
| [Troubleshooting](guide/TROUBLESHOOTING.md) | Common issues and fixes |
| [Testing the Router](guide/TESTING.md) | Isolation suite for verifying routing health |
| [Measurement](docs/MEASUREMENT.md) | What "savings" means and how it's computed |
| [RouterArena](docs/ROUTERARENA.md) | Benchmark methodology, results and negative findings |
| [Ground Truth](guide/GROUND_TRUTH.md) | Replayable routing corpus, opt-in |
| [Benchmarks](docs/BENCHMARKS.md) | Model cost/latency/quality table, regenerated by CI |
| [Changelog](CHANGELOG.md) | Release notes ([archive](CHANGELOG-ARCHIVE.md)) |

`llm-router` is scored on the [RouterArena](https://github.com/RouteWorks/RouterArena)
benchmark — full split, 8,400 queries, graded locally with this repo's harness,
**not yet independently verified**. Current Arena Score: **72.35**. Full
methodology and negative results (including a proxy split that misled tuning
by 4.25 points): **[docs/ROUTERARENA.md](docs/ROUTERARENA.md)**.

---

## Enterprise, Contributing, License

`llm-router` is built for individual developers and small teams: local cost
savings, zero ops overhead, no hosted anything. For team-wide policy
enforcement, audit export, SSO or per-org budgets, see
**[Chuzom](https://github.com/Chuzom/Chuzom)** — a separate, sibling product.

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
git clone https://github.com/ypollak2/llm-router.git
cd llm-router
uv sync --extra dev
uv run pytest tests/ -q         # Run tests (10,000+)
uv run ruff check src/ tests/   # Lint
```

MIT License. [Issues](https://github.com/ypollak2/llm-router/issues) ·
[Discussions](https://github.com/ypollak2/llm-router/discussions) ·
[PyPI](https://pypi.org/project/llm-routing/) · [Changelog](CHANGELOG.md)
