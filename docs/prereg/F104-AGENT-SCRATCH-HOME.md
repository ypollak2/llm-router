# F104: benchmark coding agents run with a scratch HOME

Status: code change, not a prereg amendment. Frozen preregs and their `.sha256` sidecars are not edited.

## Why

During P2.0 code-arm runs the coding agents ran with the owner's real HOME and twice performed an
`llm-router uninstall`-shaped deletion of the live install (F86 2026-10-18: proxy LaunchAgent
plists and `proxy_default.json`; F86-2 2026-10-19: `~/.claude/settings.json` hook wiring,
statusLine, `rules/llm_router.md`). Cause: `claude_env()` in `scripts/p20_requal.py` copied
`os.environ` (real HOME), and Pi's `lib_pi.run_pi` does `dict(os.environ)`.

## What changed

* `agent_env()` / `claude_env(home)` build an allow-listed env: `HOME=<scratch>`, `PATH`, `LANG`,
  `LC_ALL`, `USER`, `LOGNAME` (USER is the macOS Keychain account name), and for Claude only
  `LLM_ROUTER_SESSION_KIND=research` plus auth. Nothing else, so no `LLM_ROUTER_*`,
  `ANTHROPIC_*`, or other keys leak.
* Scratch HOME: `<run_dir>/agent_home/<arm>/<item>/a<attempt>` (fresh, 0700, outside the work
  tree); the Claude Q&A CLI uses `<run_dir>/claude_home`.
* Pi: the helper lives outside the repo (hashed research code), so the runner swaps `os.environ`
  for the call (`scrubbed_environ`) and restores it.
* Guard: `assert_agent_env` refuses (exit 2) when HOME is the real HOME (user database, via
  `pwd`) or the caller's `$HOME`, or when an unlisted key is present. There is no
  `--i-accept-real-home` flag: the markers always exist on the owner's machine, so a
  marker-based refusal would be a standing prompt; refusing the env itself has no override to misuse.

## Auth (never printed or logged)

Claude authenticates by one of: `CLAUDE_CODE_OAUTH_TOKEN` passed through, or (F113)
`P20_CLAUDE_CREDENTIALS_FILE`. Under the scratch HOME the macOS Keychain is not visible:
`claude -p` answers "Not logged in" (confirmed 2026-10-19), so the Keychain is not a usable path.

### Owner step (F113)

1. Run `claude setup-token` in a terminal and put the printed token in
   `~/.llm-router-bench/claude-oauth-token` (suggested path), mode 0600
   (`chmod 600`; the directory 0700).
2. Export `P20_CLAUDE_CREDENTIALS_FILE=~/.llm-router-bench/claude-oauth-token` for the harness.

The harness reads the file and sets `CLAUDE_CODE_OAUTH_TOKEN` only in the Claude child's env
(`claude_env`, not the Pi env). It never logs, prints or writes the value (nothing is copied into
the scratch HOME; the earlier copy to `.claude/.credentials.json` is gone). It refuses, naming no
path and no value, when the file is group/world accessible, empty or unreadable.

## Limits

A scratch HOME is not a sandbox. An agent can still use absolute paths or `pwd` lookups. Stronger
isolation (`sandbox-exec` or a separate OS user) is not part of F104 or F110.

### F110 hardening (rv-642 residual)

* PATH: the agent PATH is an allow-list built by `agent_path()`: `/usr/bin /bin /usr/sbin /sbin`
  plus caller directories that hold node, npm, npx, git, python, python3 or uv. Any directory
  holding an `llm-router` or `llm_router` executable is dropped (so the run's venv bin
  is gone), as are `~/.local/bin` and the uv tool dirs. A directory with both node and
  llm-router is dropped whole and fails closed (the Pi dry run shows it). `assert_agent_env`
  refuses (exit 2, no secret named) if `shutil.which("llm-router")` still resolves on that PATH.
* `launchctl` remains reachable (it lives in `/bin`). The threat is `llm-router uninstall`
  calling it against the user's launchd domain, which HOME does not scope; removing llm-router
  from PATH is the guard. An agent that calls `launchctl` directly by name is not prevented.
* Not covered: the legacy-brand CLI is not on the deny list (the identity gate forbids its name
  in runtime code); it is only dropped when its directory is dropped for another reason.
* HOME: `assert_agent_env` also refuses a HOME inside `real_home()` unless it is under the run dir.
* The hidden-test runner (`p20_code_runner`, a grader, not an agent) kept the caller PATH in F110; F113 replaces that (below).
* Because `~/.local/bin` is dropped, a `claude` installed only there will not resolve in the
  agent env and the call fails closed (`claude_error`).

### F113 hardening (rv-643 residual)

* Import guard: `assert_agent_env` runs every `python` / `python3` that resolves on the agent PATH
  as `python -I -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('llm_router')
  is None else 1)"` with the agent env, and refuses (exit 2) if any can import `llm_router`
  (exit 1 or a probe timeout both refuse). `PYTHONNOUSERSITE=1` is in the agent env and the
  allow-list.
* Hidden-test grader: `p20_code_runner` now runs agent-written code with `agent_path(...)` as PATH
  and an interpreter picked by `grader_python`: the first python on that PATH that passes the
  import guard and can `import pytest`. It no longer uses the run venv python. Consequence:
  the owner needs a python on the filtered PATH that has pytest and not llm_router (for example a
  Homebrew python with pytest); otherwise the grader refuses (fails closed) instead of grading.
* uvx note (not guarded): `uvx` / `uv tool run` can fetch `llm-router` from the index into a cache
  under the scratch HOME. That leaves the owner install untouched, but the agent could then run a
  fresh copy. `uv` stays on the PATH because the agents need it for their own tasks.

### F115 follow-ups (rv-647 residual)

* Import guard is fail-closed: `assert_no_llm_router_import` refuses unless the probe exits 0.
  Exit 1 (importable), any other exit (2, 126/127), a signal, a timeout or an OSError all refuse.
  It now probes every `python*` / `pypy*` executable (`python3.13`, `pypy3`, `python3.13t`,
  `python3.12d`, `python3m`, ...) in each agent PATH dir, not only `python` / `python3`;
  `python3-config` is not an interpreter and is skipped. `grader_python` is cached per PATH for
  one run (cleared at the run preflight).
* Grader: a hung or unrunnable `import pytest` probe is a `Refuse`, never an agent failure.
  `p20_code_runner.run` calls `grader_python` once at run start (when an `rx_edit` task is
  pending), before any agent runs. `p20_prefreeze_smoke.py` has a `grader_python` check
  (PASS/FAIL), so a missing pytest-capable python shows up before the run.
* Token exposure: the Claude coding arm has Bash and `CLAUDE_CODE_OAUTH_TOKEN` in its env, so it
  can `printenv` the token. We did not verify that Claude Code scrubs it from Bash subprocess
  env. Instead, after every Claude agent call and before anything is stored or graded,
  `scrub_claude_artifacts` compares the token value in memory against the call's stdout/stderr,
  the work tree, the scratch HOME and the `git diff HEAD` patch. A hit refuses the run with
  `refusing: token found in <agent output|work files|agent scratch home|patch>`; the value and
  paths are never printed. The scrub also runs after a timed-out call (output, work files,
  scratch HOME), and refuses if the Claude arm is active with no token to scan for. The tree
  walk never follows a symlink and never opens blocking: only regular files and plain
  directories pass; a symlink, FIFO, device or socket, a file over 64 MiB, more than 512 MiB in
  total or more than 120 s refuses with the kind only (a FIFO or `/dev/zero` link made by the
  agent is treated as hostile, not skipped). `git log -p --all` of the work repo is scanned as
  well, because `.git` objects are zlib-compressed and the file walk cannot see a token the
  agent committed. On a hit the work and scratch-HOME copies are deleted (only inside the run
  dir the harness created), but the run dir (ledger, stored output) may still hold the token:
  **purge that run dir and rotate the token (`claude setup-token`)**; the refusal says so.
  F117: an unreadable (chmod 000) directory in a scanned tree refuses (`unreadable directory
  in <kind>`, the walk no longer skips it), and the purge first gives each plain directory
  owner rwx so it can remove it. Every git run on an agent-touched repo goes through
  `run_git(..., sandbox=True)`: stdout is bytes (a non-UTF-8 diff or log is scanned, not a
  `UnicodeDecodeError`), capped at 128 MiB and 120 s, and a broken `.git`, timeout, cap or exit
  code is a `Refuse` that names the git subcommand only; any other scan error also purges before
  refusing. **Agent-written git config never runs**: git has no switch to ignore a repo's
  config, so the sandbox mode never reads it. It builds a harness-written git dir (own config,
  copy of HEAD and index, the agent's objects as alternates and its refs by symlink, data
  only), runs with `GIT_DIR` there, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `HOME` a temp dir, `--no-pager --no-textconv --no-ext-diff --ignore-submodules=all`, and
  `-c` overrides for core.fsmonitor, core.hooksPath, diff.external, core.sshCommand, core.gitProxy,
  core.pager, core.editor, core.askPass, core.alternateRefsCommand, core.attributesFile,
  gpg.*.program, log.showSignature, credential.helper, protocol.ext.allow and more
  (`GIT_HARDEN`). The few non-sandbox calls (`init`/`add`/`commit`/`rev-parse` on a repo the
  harness just made, `worktree remove` of the noncloud worktree) use the overrides only: a
  `filter.<name>.clean` driver or `diff.<name>.command` named by an in-tree `.gitattributes`
  cannot be listed away, so `diff`/`status` are never run that way. Limits: unreachable git
  objects (not on any ref, e.g. after the agent deletes a branch or `git gc`) are not in
  `git log --all` and are not scanned; core.ignorecase/filemode of the agent's repo is not
  mirrored in the sandbox config; the noncloud clone's own config is still read by
  `worktree remove`.
  The opt-in smoke `--claude-call` also scans its scratch HOME and output (PASS/FAIL), to catch
  Claude Code persisting the auth value there, which would make every Claude-arm call refuse
  (not run by F115). The Pi arms and the grader never receive the token. Residual: this
  detects the literal value only (an agent that encodes it, e.g. base64, or splits it is not caught), and the
  agent could still send it over the network; the token should be treated as exposed to the
  coding agent and be a revocable, single-purpose token.
* `nc.grade_all` (the `noncloud` stratum) lives in the external `run_codex` module, outside this
  repo. Which interpreter and PATH it uses to run agent-written code is unverified; the
  `grader_python` guard does not cover it. Check it before a `noncloud` code-arm run.
* `read_oauth_token` errors use `from None`, so no traceback carries the credentials file path.

### Pre-freeze smoke checklist (F113; none of it was run by F104, F110 or F113's build)

Helper: `python scripts/p20_prefreeze_smoke.py [--claude-call]` prints `PASS`/`FAIL` per check and
nothing else. Exit 0 when all pass.

1. `claude -p` under the scratch HOME with the token (owner-run, spends one call):
   `P20_CLAUDE_CREDENTIALS_FILE=... python scripts/p20_prefreeze_smoke.py --claude-call`
   checks `claude_token` (file mode and content) and `claude_call` (one real call).
2. Pi node resolution: `node_on_agent_path` confirms `node` resolves on the filtered PATH.
3. Import guard: `import_guard` confirms at least one python is on the agent PATH and none
   imports `llm_router`.
4. Print `agent_path(os.environ["PATH"])` and confirm no llm-router or `~/.local/bin`.

## Prereg

PREREG-P20-requal section 6 says children run "with the real HOME (auth only)". F104 supersedes
that line. Any new P2.0 amendment (and the P20T techop sidecar, which imports `p20_requal`) must
reference F104 and re-pin `CODE_RUNNER_SHA256` / the repo SHA.
