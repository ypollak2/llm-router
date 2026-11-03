# Commit messages and PR bodies bound to the local text job (W7, D-360)

Claude no longer writes the text of a commit message or a PR body. It writes a placeholder, a
PreToolUse hook has the local model write the text from the real diff, checks it with the router's
own text-job checks, and puts it in the command. The text never passes through Claude.

## Why

- Evidence: in the 30 days to 2026-11-03 Claude ran 1,563 `git commit` and `gh pr create` commands
  (n=1,563 commands: 984 commits, 579 PRs), about 89% of them inside sub-agents, and none of them was
  routed, because the router's text-job tool was advisory. The research puts the ceiling of what
  wiring these commands can save at about 4% of Claude's output tokens
  (`~/.rsi/research/primary-plan/v16/coord/ROUTING-RESEARCH-20261103.md`, same window, same n).
- The local model passes the commit-message and PR-description checks where it fails open-ended
  questions (same file), so this is the one kind of Claude output the router can take over whole.
- A tool the model may call and then has to copy is not a handoff: the copy costs the output tokens
  the handoff was meant to save. Rewriting the command in the hook costs none.

## What the model writes

```
git commit -m "${LLM_TEXT:?}"
gh pr create --title "<short title>" --body "${LLM_TEXT:?}"
```

Other options stay (`--no-verify`, `-a`, `--trailer`, `--draft`, `--base`), including a flag that shares a
short cluster with the message option (`-am`, `-sm`, `-db`): the rewrite keeps it, and the hook proves that the
valueless options of the rewritten command equal the original's before it emits anything. Attribution lines
go in a second `-m`, or on lines after the placeholder inside the PR body quotes; they are kept as written.

The placeholder must be written exactly as `"${LLM_TEXT:?}"`. Only a spelling that the shell expands AND that
aborts the command when the variable is unset is the placeholder (proved against `bash` and `zsh` in
`tests/test_w7_shellscan.py`): `$LLM_TEXT` and `${LLM_TEXT}` expand to nothing and would post an empty body, so
they are not accepted. A mention of `$LLM_TEXT` anywhere the shell does not replace it (single quotes, after a
backslash, in a here-document, in a comment, as `${LLM_TEXT:-x}`) in a command that commits or opens a PR is
denied, never passed through: it would be committed as literal text. To commit text that itself mentions the
placeholder, write the message to a file and run `git commit -F <file>`.
The rules text that teaches this is the section "Commit messages and PR bodies" of
`src/llm_router/rules/llm_router.md`, which every session and every sub-agent loads.

## What the hook does

`src/llm_router/hooks/text-job-bind.py` (installed as `llm_router-text-job-bind.py`, PreToolUse,
matcher `Bash`):

0. Is not even started for an ordinary Bash call: it is registered with `"if": "Bash(*LLM_TEXT*)"`
   (Claude Code permission-rule syntax), so the host spawns it only when some subcommand contains the
   placeholder, or when it cannot read the command statically (`$(...)`); the hook's own fast path then
   exits at once. A hang or a crash in it cannot slow any other Bash call.
1. Reads the command with a strict shell scanner (`text_job_bind/shellscan.py`). A command without the
   placeholder is left alone.
2. Builds the job input from the repository as it is now: `git diff --cached` (`git diff HEAD` with
   `-a`), or for a PR the commits, the changed files and the diff against the base. Every git read is
   read-only, bounded and hardened: the repository's own configuration cannot run a program from it
   (file-system monitor, pager, external diff, textconv, clean/smudge filters, submodule log; see
   `gitinput.py`, and `tests/test_w7_gitinput.py` for a hostile configuration). The true list of changed
   files comes from `git diff --numstat`, so a patch that git was stopped from printing in full still reports
   the real file count. The list shows the first names and counts the rest; the diff shows as many files as
   the size budget allows, a file that does not fit keeps its header and a note while there is room, and the
   remaining files are counted in a final line. Nothing is dropped silently. For a commit message
   the model is also shown the last five commit subjects (without their `(#123)` numbers: a model told
   to match the style copies the suffix and makes up a number) and is asked to name one of the changed
   files, by its name without the extension, with up to three offered (the router's acceptance check
   wants exactly that; without the offer the retry loop has to ask for it, and every rejected attempt
   counts toward the lane monitor described under "Measuring it").
3. Loads `$LLM_ROUTER_HOME/.env` the way the MCP server does (so the documented off switch
   `LLM_ROUTER_LOCAL_TEXT_JOBS=0` and the context settings reach the hook), loads the router (this is router
   time, not the model's wait), takes the machine-wide job lock, and runs the router's own `llm_text_job` (same
   prompts, same acceptance checks, same ledgers), local model only: a passed `llm-router local-check` lane
   is required, and `LLM_ROUTER_LOCAL_TEXT_JOBS=1` (the full routing chain, cloud models included) is treated
   as unset for this hook. The reasons the hook reports come from the router's structured result, not from
   its text, which ends with the model's own words.

   One job at a time per machine: Ollama serves one request at a time and most commits come from parallel
   sub-agents. A job that cannot get the lock within `limits.LOCK_WAIT_SEC` is denied at once as "busy, write it
   yourself" (outcome `busy`); that is a capacity answer, it starts no cooldown and counts as no failure of the
   model. Each model attempt is capped at the MCP door's per-attempt share of its deadline
   (`mcp_door.MCP_DEADLINE_DEFAULT_S * mcp_door.ATTEMPT_SHARE`, reused, not copied), inside the hook's budget, so
   one slow attempt cannot use the whole budget. The lane was validated at two attempts per job, not three; the
   cap trades a slow third attempt for time, and what that costs in accepted jobs is not quantified.
4. Cleans the text before it is published. `@name` would notify that person, `Fixes #12` or a closing keyword
   with an issue URL would close an issue, and a `Test plan` section is the model's guess presented as a
   result: mentions lose their `@`, closing keywords become `refs`, and the content under a `Test plan` heading
   is replaced by a line that says the model does not run tests (`sanitize.py`; the ledger counts what was done,
   never the text). A commit message holds the model's subject and, by `limits.COMMIT_BODY_MODE` (override
   `LLM_ROUTER_TEXT_JOB_BODY`), one of: `files` (the default: the body is a list of the changed files with
   their added and deleted line counts, built from git, with no model text), `none` (the subject only) or
   `model` (the model's own body). An independent read of 39 real messages found 5 clearly false bodies among the 18
   messages that had one (`~/.rsi/research/primary-plan/v16/coord/rv-PR820-delta-quality-20261103.md`), so the
   model's body is opt-in. A PR body is the model's own, cleaned as above.
5. Writes the text to a private file (`$LLM_ROUTER_HOME/text_job_bind/`, 0600) and rewrites the command to
   `-F <file>` or `--body-file <file>` through `hookSpecificOutput.updatedInput`. Every other field of the tool
   input is kept, and `description` is set to `git commit (local text): <subject>` (or `gh pr create (local
   text): <title>`), because the person who approves the command otherwise sees only a file path. No
   `permissionDecision` is returned, so the rewritten command goes through the normal permission flow and
   existing allow rules apply to it.
6. Re-reads the rewritten command and proves it (same commands, no placeholder left, no message option
   left, the same valueless options, the file is read) before it is emitted. An unverified rewrite is never
   emitted.

If the job is rejected, fails, times out or is not available, or the command has a shape that cannot
be bound, the hook DENIES the command with a short reason ("Nothing was run. Write the text yourself
and run the command again without the placeholder"). There is no allow path for a placeholder command.

## Fail-closed by construction

The placeholder is a shell variable with `:?`. If nothing replaced it (the hook is not registered, was
killed at its timeout, or runs on a host that ignores `updatedInput`) the command does not run: Claude
Code 2.1.317 refuses a command with a braced variable it cannot check ("A variable in braces in this
command can't be checked before it runs") and, where permissions are bypassed, the shell aborts it with
`LLM_TEXT: parameter not set`. Either way the placeholder text is never committed or posted; a literal
`@llm`-style placeholder would be committed in the bypassed case, and in a PR body it would mention a
GitHub user. This holds only for the spelling that expands and aborts (see "What the model writes"); a
mention written where the shell does not expand it has no such protection, which is why the hook denies it.
The hook also keeps a deadline guard that answers with a deny shortly before the host's silent kill, and
before it exits it leaves what a timeout inside the engine leaves: a ledger row, the cooldown and the latency
row with the model wait booked as the model's.

A hook that is killed at the host's timeout writes nothing, and the host then runs the command. The host's
documented default for a command hook is 600 seconds (30 on `UserPromptSubmit`); this hook registers its own
`timeout` so the kill comes after the deadline guard.

## Shapes that are refused, and what to do

| Shape | Why | Do instead |
|---|---|---|
| `git add x && git commit -m "${LLM_TEXT:?}"` (any file-changing command before it) | the text is built from the repository before the line runs | stage in its own command, then commit alone |
| `git commit --amend`, `git commit -F`, paths after the message, `--fill`, `--web`, `-R` | no single staged diff to describe, or a conflicting message source | write the text yourself |
| a heredoc or `$(...)` in the message, wrappers such as `env`, `sudo`, loops, groups | cannot be read safely | simplify the command |
| nothing staged, no commits ahead of the base | nothing to describe | stage or commit first |
| `$LLM_TEXT` in single quotes, after a backslash, in a here-document, as `${LLM_TEXT:-x}` or without `:?` | the shell would not replace it, or would replace it with nothing | write exactly `"${LLM_TEXT:?}"`; for text that mentions it, use `git commit -F <file>` |

`cd <dir> && git commit ...` and `git -C <dir> commit ...` are supported.

## Stdout

A hook's stdout must be exactly one JSON object or nothing (BIND-1, PR #818). The script keeps a
private duplicate of file descriptor 1 for the decision and points descriptor 1 and `sys.stdout` at
stderr for everything else, so a library print, a logging handler, a raw `os.write(1, ...)`, a child
process or an `atexit` writer cannot reach the host's channel. `tests/test_w7_hook_script.py` runs the
real script as a subprocess, parses stdout whole with `json.loads`, and has a control run (guard off)
that shows the canary does reach stdout.

## Deploy

A new hook has to be registered; copying the file is not enough (`check_and_update_hooks` copies a
missing hook file but never registers it). Run `llm-router install --force` once. It writes the
PreToolUse entry with matcher `Bash`, its own timeout (`130`) and the `if` rule into `~/.claude/settings.json`;
the Claude plugin bundle (`hooks/hooks.json`) carries the same entry. The Codex bundle does not: the
placeholder convention and the `updatedInput` protocol are Claude Code's.

The rules text that teaches the placeholder is rendered by `check_and_update_rules` (at MCP server start) only
when the registration is present in `settings.json`; an install whose hook is not registered (a pip upgrade
never registers hooks) gets the rules without that section, so the model keeps writing its own messages
instead of writing a placeholder that the shell then refuses. Registering later adds the section at the next
MCP start, and removing the registration removes it. `llm-router doctor` and `llm-router verify` report the
mismatch: rules that teach the placeholder while the hook is not registered (or its file is missing) is the
dangerous one.

Before registering, read `text_job_local_attempts.jsonl` (see "Measuring it"): the lane that backs the hook
may already be near its safety valve.

Only one router hook may rewrite Bash input: Claude Code does not merge `updatedInput` from several hooks.

## Measuring it

`python -m llm_router.text_job_bind report [--days N] [--json] [--transcripts [PATH ...]]` reads
`text_job_binds.jsonl` (text-free: sizes, a reason code, latency; two generations of 2 MiB, 0600). One row
per command the hook sees: `bound` (the checked local text was put in the command) or `denied` (with an
outcome and a reason). The report states its window and its units, and prints:

- the latency (n, p50, p95, max, in milliseconds) of EVERY outcome, not only the bound ones, so that the
  budget and the cooldown can be tuned from the slow denials too;
- denials in three groups: the command's shape (`unsupported`, `refused`), capacity (`busy`, `cooldown`) and the
  model or its lane (`rejected`, `fallback`, `timeout`, `error`), and the bound rate both over all placeholder
  commands and over those where the model actually ran;
- the estimated Claude output tokens avoided (characters bound divided by 4, an estimate). Read every figure
  with its N.

Adoption needs a denominator, and the registered `if` filter takes it away: the hook never sees a hand-written
commit or PR command, so the ledger has no `handwritten` rows. The report therefore prints adoption as n/a, with
the reason, unless it is given `--transcripts`: it then counts the `git commit` and `gh pr create` commands the
model ran through the Bash tool in the Claude Code transcripts (sub-agent transcripts included) inside the
window, and how many used the placeholder, with both counts and the range of the transcripts it read. A
percentage over the ledger alone would only reflect the filter. Transcripts the host has already deleted are
not in it.

The router watches the lane behind the hook. `local_check.routing.demotion_check` turns a (model, job) lane
off (`demoted: fallbacks` in `local_routing.json`) when too large a share of its latest jobs failed
(`DEMOTE_RATE` over `DEMOTE_WINDOW` in `src/llm_router/local_check/routing.py`). A job fails only if every
try was rejected; a timeout, a busy lane, a cooldown or a model that is not available is not a quality failure
and does not count. A demoted lane answers every placeholder command with a denial (`no_lane`, a second or
two): Claude writes the text itself and nothing is bound until `llm-router local-check enable commit_message
--model <model>` is run, which also restarts the window. The window is shared with every other use of the
lane, so read `text_job_local_attempts.jsonl` before the hook is registered, and check `llm-router doctor` for
demotion notices afterwards.

The hook is instrumented for the G1 hook-latency KPI: the wait on the local model is the `text_job`
model phase, so the router-added figure does not count it. The router import is outside that phase and is
router time. The router-added time of the placeholder path is therefore seconds, not the few hundred
milliseconds a hook that runs on every call is held to; because of the `if` filter every invocation is a
placeholder invocation.

## The limits on this path

All of them are in `src/llm_router/text_job_bind/limits.py` (and the files named there): the hook timeout and
the model budget inside it, the margin kept to say no, the deadline guard, the cooldown after a timeout, the
lock wait, the age at which message files are swept, the size of the model input (`INPUT_BUDGET_CHARS`, under
the router's own cut), the per-file and per-line caps, the git read size and time, and the per-attempt cap
taken from `mcp_door`. The subject line is checked by the router's own validator (the contract the lane was
validated against, shared with the MCP tool); the hook does not tighten it. The prompt asks for a subject
under 72 characters, the validator accepts up to the router's own longer limit, and a long subject is a style
matter, not a failure.

## Not covered

- `git commit --amend`, PR edits, `gh pr create --fill`, and any shape in the table above.
- Hosts that run the command in a shell other than `bash` or `zsh`.
- Interactive approval prompts and Claude Code versions other than 2.1.317 were not exercised in the live
  canary, and the live canary stubs the local model (a real model was run separately; see PR #820, which
  lists what was).
- Hosts other than Claude Code: the rules are not taught there and the Codex bundle does not carry the hook.
