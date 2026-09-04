# Plan: one install that wires Claude Code AND Codex, and knows which seats you pay for

Status: 2026-09-04 — PR 1 (#128, seats) open; PR 2 (Codex writer, autodetect, doctor, push hook) on `feat/codex-dual-host`. PR 3 = #130 (seat-derived defaults); PR 4 (`--project`) on `feat/project-agents-md`. All four coded; merge in order.

## Goal

A user with Claude Code and Codex CLI installed runs `pip install llm-routing && llm-router install`
and, with no flags, gets:

- llm-router registered as an MCP server in **both** hosts, so routing works in both directions
  (cheap work from Claude Code lands on Codex/Ollama; hard work from Codex lands on the Claude seat).
- A **seat table**: which subscriptions are logged in (Claude Max/Pro, ChatGPT Plus/Pro/Team,
  Google account for Gemini CLI, local Ollama) and which API keys are set.
- Routing defaults derived from that table, so a seat that is already paid for is the free tier and
  API keys are used only where no seat covers the tier.
- `llm-router doctor` that fails loudly when either host has lost its registration.

Non-goals: fixing the Codex gateway wire format (`model_provider = "llm_router"`), Codex rate-limit
pressure tracking, and any change to the classifier.

## What is true today (verified on this machine, 2026-09-04)

| Fact | Where |
|---|---|
| Claude Code → llm-router works. `llm_router` is in `~/.claude.json`; llm-router dispatches to `codex exec`. | `install_hooks.py:776`, `codex_agent.py` |
| Codex → llm-router has **never** worked for anyone. The installer writes the MCP entry to `~/.codex/config.yaml`; Codex reads only `config.toml`. | `commands/install.py:525` |
| A second, divergent Codex installer writes `~/.codex/config.json` and `~/.codex/rules/llm_router.md`. Codex reads neither. | `cli.py:590` |
| Codex gateway mode (default for `--host codex`) is documented in-code as broken. | `codex_agent.py:318` |
| doctor's Codex check validates `config.yaml`, so it reports OK for a broken install. | `commands/doctor.py:223` |
| Subscription provider is a manual env var (`LLM_ROUTER_SUBSCRIPTION_PROVIDER`), never detected. | `subscription_local_routing.py:86` |
| `claude auth status` returns JSON with `loggedIn`, `authMethod` (`claude.ai` vs API), `subscriptionType` (`max`/`pro`/…). Official, no keychain scraping. | verified |
| `codex login status` prints `Logged in using ChatGPT` / `... an API key` / `Not logged in`. Plan type is a claim in the `id_token` JWT in `~/.codex/auth.json` (`chatgpt_plan_type`). | verified |
| That claim can be stale: this machine's token says `plus`, active until 2026-07-12, yet Codex works today. Treat plan type as a hint, login status as the fact. | verified |
| Gemini CLI: `gemini` binary + `~/.gemini/oauth_creds.json`. Not installed here. | — |
| `codex mcp add <name> -- <cmd>` and `codex mcp list` exist in Codex 0.153. | verified |

## Design

### A. `llm_router/hosts.py` — host detection (new, pure)

```
detect_hosts() -> dict[str, HostInfo]   # {"claude-code": ..., "codex": ..., "gemini-cli": ...}
HostInfo(binary: Path|None, config_dir: Path|None, version: str|None, present: bool)
```
Present = binary on PATH **or** config dir exists. No network. Injectable `home` and `which` for tests.

### B. `llm_router/seats.py` — subscription detection (new)

```
detect_seats(timeout=3.0) -> Seats
Seats(
  claude:  Seat(kind="claude.ai"|"api-key"|None, plan="max"|"pro"|"team"|"enterprise"|None, source),
  codex:   Seat(kind="chatgpt"|"api-key"|None, plan="plus"|"pro"|"team"|"business"|None, plan_stale: bool),
  gemini:  Seat(kind="google"|"api-key"|None),
  ollama:  Seat(kind="local"|None, models=[...]),
  api_keys: {"OPENAI_API_KEY": bool, "GEMINI_API_KEY": bool, "ANTHROPIC_API_KEY": bool, ...},
  detected_at: iso8601,
)
```
Probes, in order, each with a timeout and each optional:

- Claude: `claude auth status` → parse JSON. `authMethod == "claude.ai"` ⇒ seat; `subscriptionType` ⇒ plan.
  Fallback: `ANTHROPIC_API_KEY` ⇒ api-key.
- Codex: `codex login status` ⇒ kind. Then decode the JWT payload of `tokens.id_token` in
  `~/.codex/auth.json` **without verification** (read-only, base64 only, no network) and read
  `https://api.openai.com/auth.chatgpt_plan_type`. Set `plan_stale` when
  `chatgpt_subscription_active_until` is in the past. Never store the token.
- Gemini: binary present and `~/.gemini/oauth_creds.json` exists ⇒ google; else `GEMINI_API_KEY`.
- Ollama: GET `127.0.0.1:11434/api/tags` (reuse existing probe).

Persist to `~/.llm-router/seats.json`. Refresh on `install`, `doctor`, and the session-start hook
(cheap: two subprocesses, ~200 ms). Stale after 24 h ⇒ doctor warns.

### C. Seat-derived routing defaults

In `chain_builder` / `subscription_local_routing`:

- **Free bucket** = `ollama` ∪ {`codex` if codex seat is chatgpt} ∪ {`gemini_cli` if gemini seat is google}
  ∪ {`claude` if claude seat is claude.ai}. Today `LOCAL_PROVIDERS` is a constant; make it a function
  of `Seats`.
- **Subscription provider** defaults to the claude seat when present; env var still overrides.
- **Tier assignment**: fast → ollama, then codex; balanced → codex, then claude; best → claude seat
  (subscription) when present, else codex, else API key. Same table used from either host, so the
  "back" direction is the same code path — a Codex session calling `llm(task="analyze")` gets the
  Claude seat.
- API-key providers enter a chain only for a tier no seat covers, and doctor says so.

### D. One Codex writer

Delete `cli._install_codex_cli_files` and rewrite `commands.install._install_codex_files`:

1. MCP: try `codex mcp add llm_router -- <abs path to llm-router>`; on failure, surgical insert of
   `[mcp_servers.llm_router]` into `config.toml` with the existing `_ensure_toml_table_block`.
   Absolute path because Codex does not inherit the shell PATH reliably. Record in the manifest.
2. Instructions: marked block (`<!-- llm-router:start -->…<!-- llm-router:end -->`) appended to
   `~/.codex/AGENTS.md`, replaced on re-run. Codex reads AGENTS.md; it does not read `rules/*.md`
   or `instructions.md` (legacy).
3. Migration: remove the `config.yaml` block, `config.json` entry, `rules/llm_router.md`,
   `instructions.md` block, and any `model_provider = "llm_router"` this installer previously wrote,
   using the manifest so hand-written content is untouched.
4. Gateway mode becomes `--mode gateway` opt-in with a warning; default is MCP only.
5. Keep the PostToolUse telemetry hook.
6. Push routing, same as Claude Code: install a `UserPromptSubmit` hook in `~/.codex/hooks.json`
   that runs the same auto-route script and injects the `⚡ ROUTE:` hint. Without it Codex is pull
   only (it has to decide to call the tool from AGENTS.md); with it both hosts behave identically.
   **Found while building:** Codex silently skips any hook without a
   `[hooks.state."<hooks.json>:<event>:<group>:<handler>"] trusted_hash` record in `config.toml`.
   The hash is SHA-256 of a canonical-JSON identity of the hook (`llm_router.codex_host`), verified
   against a real run. The real `UserPromptSubmit` payload is in `tests/fixtures/`.

### E. `llm-router install` with no `--host`

1. `detect_hosts()`; `detect_seats()`.
2. Install every present host (Claude Code path unchanged; Codex per D; Gemini CLI per existing writer).
3. Print the seat table and the derived free bucket. `--host X` and `--skip-host X` remain.
4. Exit non-zero if a present host could not be registered.

### F. doctor

- Replace the `config.yaml` check with: `[mcp_servers.llm_router]` in `config.toml`, command path
  exists and is executable (same as the GH-41 Claude check), and `codex mcp list` shows it
  (3 s timeout; "unsupported auth" column is fine).
- Same for `claude mcp list`.
- Print the seat table; warn if `seats.json` is older than 24 h or a plan claim is stale.
- A present host with no registration is a **fail**, not a warn.

### G. Session-start banner

Add one line under the existing usage line: `seats: claude=max · codex=chatgpt(plus) · ollama=3 models`.

### H. Project level (optional, last)

`llm-router install --project` writes `AGENTS.md` (from a template naming the MCP tools) and a
`CLAUDE.md` symlink to it (copy on Windows). Both agents read one file.

## Delivery — four PRs, each green on its own

| PR | Scope | Tests |
|---|---|---|
| 1 | `hosts.py`, `seats.py`, seat table in `doctor` and the banner. No behaviour change. | fake `claude`/`codex` shims on a temp PATH returning canned output; fake `auth.json` with a hand-built JWT; stale-claim case; every probe missing; timeout. |
| 2 | Codex writer rewrite (D), host autodetect in `install` (E), doctor Codex checks (F), legacy migration. | temp `$HOME` with a hand-edited `config.toml` (must survive byte-for-byte outside the inserted table); `codex mcp add` shim failing ⇒ text fallback; re-run is idempotent; legacy files removed only when manifest says we wrote them. Update `test_codex_gateway_install.py`, `test_codex_self_heal.py`, `test_doctor_truth.py`. |
| 3 | Seat-derived routing defaults (C). | chain_builder table tests per seat combination: {max, chatgpt}, {max only}, {chatgpt only}, {none, API keys}, {none, nothing} ⇒ expected chain per tier. |
| 4 | `--project` (H). | symlink vs copy; existing AGENTS.md preserved. |

Docs: update `guide/HOST_SUPPORT_MATRIX.md` (Codex from "Strong" to "Full" once PR2 lands),
`guide/QUICKSTART_2MIN.md`, and the README install section.

## Risks and decisions

- **JWT plan claim staleness** — observed. Login status is authoritative; plan is advisory and
  labelled stale when its window has passed.
- **Hand-edited `config.toml`** — never rewrite the file; insert one table, remove only what the
  manifest recorded.
- **Absolute command path** — breaks when the venv moves. doctor checks the path; install re-run heals.
- **Privacy** — seats.json stores kinds and plan names only, never tokens, emails, or account ids.
- **No post-install hook** — pip has none and npm's must not edit `~/.codex`. "Automatic" means one
  command detects and configures everything; same contract Claude Code has today.
- **Decision needed**: should a Codex seat marked `plan_stale` still count as free? Proposed: yes,
  because `codex exec` either works or fails fast, and the chain already skips a failing provider.

## Acceptance

Fresh machine, both CLIs logged in, Ollama running:

```
pip install llm-routing && llm-router install
claude mcp list | grep llm_router      # present
codex  mcp list | grep llm_router      # present
llm-router doctor                      # seats table, no fails
```
From a Codex session: `llm(task="analyze", …)` is served by the Claude seat.
From a Claude Code session: `llm(task="query", …)` is served by Ollama or Codex.
Removing either CLI's login and re-running doctor shows the seat gone and the chain re-derived.
