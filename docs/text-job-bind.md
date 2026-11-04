# Commit messages and PR bodies bound to the local text job (W7, D-360)

Claude no longer writes the text of a commit message or a PR body. It writes a placeholder, a
PreToolUse hook has the local model write the text from the real diff, checks it with the router's
own text-job checks, and puts it in the command. The text never passes through Claude.

## Why

- Evidence: in the 30 days to 2026-11-03 Claude ran 1,563 `git commit` and `gh pr create` commands
  (n=1,563 commands: 984 commits, 579 PRs), about 89% of them inside sub-agents, and none of them was
  routed, because the router's text-job tool was advisory
  (`~/.rsi/research/primary-plan/v16/coord/ROUTING-RESEARCH-20261103.md`). Most of those lines were not
  bindable. The measured upper bound of what wiring them can save is 0.36% of output tokens (86 bindable
  real commands in 7 d, `~/.rsi/research/primary-plan/v16/coord/ROUTING-NEXT-20261104.md`, window
  2026-10-28..2026-11-04, estimate): a small, safe win, not a large one.
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
backslash, in a here-document, in a comment, as `${LLM_TEXT:-x}`) is denied unless EVERY command on the line is a
harmless reader, because anything else might hand the literal text to git: it would be committed as written. The
rule is deny-by-default and does not look for the word `git`, since a name held in a variable or built from quotes
(`G=git; timeout 5 $G commit -m '...'`, `g'i't commit`, `python3 - <<'PY'` with `['git', 'commit']` in its body) has none
to find. The harmless readers are a short list (`echo`, `printf`, `grep`, `rg`, `cat`, `ls`, `cd`, ...; not `sed` or `awk`
(whose arguments can run a program), a pager (`LESSOPEN`) or `test` / `[` (`-v 'a[$(cmd)]'`, and in zsh the operands of
`-eq`, are evaluated)), each written by its exact name (`./echo` and `/bin/echo` are not `echo`), with no variable set in
front of it, outside any group, loop or keyword, with no word of the line that runs a command (`$(...)`, backticks) or
expands anything but a plain `$NAME` / `${NAME}`, and with none of the options that name a program to run (`rg --pre`,
`ag --pager`, `sort --compress-program`, `grep -O`). `grep`, `rg`, `ag` and `sort` take no expansion in their arguments at
all (the value of a variable can be such an option, `rg -g -e "$O"` included), and a `printf` that could evaluate an
argument is refused (`-v`, a format that is not literal, a conversion other than `%s` with an expansion among the
arguments: zsh evaluates the arguments of `%d`). A line the scanner cannot read, or reads differently from other shells (a
single quote inside `"${...}"`), is judged on its text and trusts no quote: every command start (after `;`, `&`, `|`, a
parenthesis, a backtick, `$(` or a newline, quotes or not) must begin with such a reader, by its exact name, a
here-document body is judged like any other lines (which of two `<<` is real is a quoting question), and a leading
`NAME=value` is skipped only as a statement of plain assignments (`X=' echo' git commit` ends the assignment where bash
does not). What passes with no decision is a line whose commands are all harmless readers (a `grep`, an `echo`, a `cat`
with a quoted here-document). Denied whatever else the line says, on a line that binds a commit too: `$[...]` (arithmetic
that evaluates its operand), `${(...)}`, `${!...}`, `$~x` and `${~x}`, a `${NAME:?message}` whose message expands
something, `printf -v`, and an unquoted here-document whose body holds `$(...)` or backticks; and on a line with a stray
mention, a shell startup file, a git hook or git config, or `.ssh/` named anywhere. This only ever adds denials to the
rule above: every line it denied is still denied (`tests/test_w7_deny_only.py`). It is a deny-list of what is known to be
harmless, not a proof that nothing else can run git: a harmless reader can still write a file it is pointed at. When the
line has no commit or PR command in it, the refusal names what the hook cannot see through and says to put the text in a
file with the editor or `printf`, instead of saying that a commit would carry it. To commit text that itself mentions the
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
   bounded and hardened. It does not write the repository (`diff.autoRefreshIndex=false`: a plain `git diff HEAD`
   refreshes a stale index and writing it runs the `post-index-change` hook), and the repository's own configuration
   and hooks cannot start a program from it as far as the mechanisms known to do so go (file-system monitor, pager,
   external diff, textconv, clean/smudge/process filters of every name, submodule log, the gpg programs that
   `git log --show-signature` would run for a signed commit, and git hooks: `core.hooksPath` is pinned to
   `/dev/null`; see `gitinput.py`, and `tests/test_w7_gitinput.py` for a hostile configuration with a control that
   shows plain git runs each of them).
   That is a list of the mechanisms known to start a program, not a proof that git has no other, so there is a second
   layer: every git the hook starts gets a scrubbed environment (`PATH`, `HOME`, `LANG` and what git needs; none of
   the owner's provider keys, none of the `LLM_ROUTER_*` settings), and a test shows that a hostile filter, run
   with the first layer bypassed, sees no key. A diff is refused when the list of configured filters cannot be read in full (a timeout, a failure, or an answer cut at the
   byte cap: the filters after the cut would not be overridden). The true list of changed
   files comes from `git diff --numstat`, so a patch that git was stopped from printing in full still reports
   the real file count. The list shows the first names and counts the rest; the diff shows as many files as
   the size budget allows, a file that does not fit keeps its header and a note while there is room, and the
   remaining files are counted in a final line. Nothing is dropped silently. For a commit message
   the model is also shown the last five commit subjects (without their `(#123)` numbers: a model told
   to match the style copies the suffix and makes up a number) and is asked to name one of the changed
   files, by its name without the extension, with up to three offered (the router's acceptance check
   wants exactly that; without the offer the retry loop has to ask for it, and every rejected attempt
   counts toward the lane monitor described under "Measuring it").
3. Loads the user's own `$LLM_ROUTER_HOME/.env` (and ONLY that file: not the repository's `.env`, which is
   repository content and could turn the owner's off switch back on, move the state directory or change what is
   published, and not `~/.env`), so the documented off switch `LLM_ROUTER_LOCAL_TEXT_JOBS=0` and the context
   settings reach the hook. The hook process then runs from that directory (every path it uses for the repository is
   absolute), because the router's configuration reads `./.env` and searches upward for a project config: with the
   repository as the working directory a repository could set the router's database path or its raw-prompt
   persistence for this process. Then it asks the router's own questions about the flag and the lane (the same
   functions with the same arguments as `llm_text_job`, over the input cut at the router's limit; a test runs
   both over the same state files): a job that would be handed back anyway (no passed lane, a lane that cannot
   be verified, the off switch) is denied here, before the model libraries are
   imported (p50 96 ms router-added, p95 109 ms; n=20 runs of the real hook script, stub model, 2026-11-04). Otherwise it loads the router (seconds; this is router time, not the model's wait), takes the
   machine-wide job lock, and runs the router's own `llm_text_job` (same
   prompts, same acceptance checks, same ledgers), local model only: a passed `llm-router local-check` lane
   is required, and `LLM_ROUTER_LOCAL_TEXT_JOBS=1` (the full routing chain, cloud models included) is treated
   as unset for this hook. The reasons the hook reports come from the router's structured result, not from
   its text, which ends with the model's own words.

   One job at a time per machine: Ollama serves one request at a time and most commits come from parallel
   sub-agents. A job that cannot get the lock within `limits.LOCK_WAIT_SEC` is denied at once as "busy, write it
   yourself" (outcome `busy`); that is a capacity answer, it starts no cooldown and counts as no failure of the
   model. The wait is booked in its own latency phase (`text_job_queue`, a model phase for the G1 KPI: it is
   another job's model time), not as router time. Each model attempt is capped at the MCP door's per-attempt share of its deadline
   (`mcp_door.MCP_DEADLINE_DEFAULT_S * mcp_door.ATTEMPT_SHARE`, reused, not copied), inside the hook's budget, so
   one slow attempt cannot use the whole budget. The lane was validated at two attempts per job, not three; the
   cap trades a slow third attempt for time, and what that costs in accepted jobs is not quantified.
4. Cleans the text before it is published. `@name` would notify that person, `Fixes #12` or a closing keyword
   with an issue URL would close an issue, a test, testing or verification section is the model's guess
   presented as a result, and a remote image is fetched by whoever opens the PR with whatever its URL carries:
   mentions lose their `@` (however many lead the name), closing keywords become `refs` (the keyword, up to eight separators
   including line breaks and non-breaking spaces, and the reference), the content under any heading (or bold line) titled test plan, tests, testing, test results, unit
   or local or manual testing, how I tested, what was tested, checks run, results, verification, validation or QA is
   replaced by a line that says the model does not run tests, and every URL that fetches or runs something (`http(s)`,
   `ftp`, `ssh`, `git`, `file`, `ws(s)`, `gopher`, `irc(s)`, `smb`, `ldap(s)`, `vscode`, `chrome`, `blob:`, `data:`, `javascript:`, `vbscript:`, `mailto:`, `tel:`, a bare `www.` link, protocol-relative ones (`//host`, `\\host`, with whitespace before them)
   after `(`, `<`, a quote, `=` or `[`) is removed wherever it sits, after the character references and escapes that
   could spell it are undone, together with every markdown image marker and every embedding HTML tag (`a`, `img`,
   `picture`, `iframe`, `script`, ...; one too long or broken to match whole loses its opener), keeping the visible text; relative links and anchors stay, and so do ordinary
   angle brackets (`Vec<String>`). This is best effort: markdown and HTML have more ways to name a destination than
   any list, and GitHub's own sanitiser is not consulted. Every pattern is linear (a megabyte of adversarial text is
   cleaned in well under a second: `tests/test_w7_sanitize.py` asserts under 1 s per stage) and a text longer than a PR body can be is refused before cleaning
   (`sanitize.py`; the ledger counts what was done, never the text). A
   file NAME is text too: names in the `files` body are quoted when they hold a control character (a newline in
   a name could forge a `Signed-off-by:` trailer) and the list goes through the same cleaning. A commit message holds the model's subject and, by `limits.COMMIT_BODY_MODE` (override
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
Code 2.1.317 does not run a command with a braced variable it cannot check without asking ("A variable in
braces in this command can't be checked before it runs") and, where permissions are bypassed, the shell aborts it with
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

The rules text that teaches the placeholder is rendered by `check_and_update_rules` (at MCP server start) in two
parts, each only when its conditions hold at render time (all read-only: files and the process environment, no network):

- the commit part (`git commit -m "${LLM_TEXT:?}"`): the hook is registered in `settings.json`; the off switch
  `LLM_ROUTER_LOCAL_TEXT_JOBS` is unset or one of 1/true/on/yes (any other value is off, the same test the hook makes).
  The process environment is read first; when it does not set the variable, `$LLM_ROUTER_HOME/.env` is read for that
  one key (the server renders the rules while it is imported, before it loads that file, and the hook loads it, so an
  off switch kept only there must turn the rules off too); and a passed local-check lane for `commit_message` exists;
- the pull-request part (`gh pr create --body "${LLM_TEXT:?}"`): the commit part renders AND a passed lane for
  `pr_description` exists. With the commit part only, the rules say that PR bodies have no placeholder and are
  written normally.

When neither renders, the plain text-job guidance does. An install whose hook is not registered (a pip upgrade never
registers hooks) gets no placeholder at all, so the model keeps writing its own messages instead of writing a
placeholder that the shell then refuses; one that registered the hook but has no passed lane yet gets the plain
guidance too, because every placeholder would otherwise be denied after a moment for want of a lane. Each part appears
at the next MCP start once its conditions hold and goes away at the next start after one stops holding.
`llm-router doctor` and `llm-router verify` report the mismatches: rules that teach a placeholder while the hook is not
registered (or its file is missing) is the dangerous one; plain rules with no passed lane, a PR lane that appeared
or disappeared, the off switch, stale rules and a lane that is gone are information.

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

Adoption needs a denominator, and the ledger can never give one: a hook registered with the `if` filter sees only
the hand-written commands that happen to spawn it, so any rows of kind `handwritten` are a count, not a rate, and
the report never turns them into a percentage. It prints adoption as n/a, with the reason, unless it is given
`--transcripts`: it then counts the commands the model ran through the Bash tool in the Claude Code transcripts
(sub-agent transcripts included) inside the window that really RUN `git commit` or `gh pr create` (the scanner
reads the words; an `echo`, `grep` or here-document that quotes them is not counted), and how many used the placeholder, with both counts and the range of the transcripts it read.
Only DIRECT, top-level executions count: a command inside a loop, a conditional, a group, a subshell or a function body is
unknown (`for x in; do git commit -m x; done` never runs it), and so is everything after a construct the scanner cannot
place; one that runs git through `env`, `bash -c`, `timeout`, `xargs` or a variable does not count either, and neither does a line the scanner cannot read. Transcripts the host has
already deleted are not in it. "Where the model actually ran" leaves out the denials
that never reached it (no lane, off switch, lane unverified, router not loadable, no time left).

The router watches the lane behind the hook. `local_check.routing.demotion_check` turns a (model, job) lane
off (`demoted: fallbacks` in `local_routing.json`) when too large a share of its latest jobs failed
(`DEMOTE_RATE` over `DEMOTE_WINDOW` in `src/llm_router/local_check/routing.py`). A job fails only if every
try was rejected; a timeout, a busy lane, a cooldown or a model that is not available is not a quality failure
and does not count; a call whose answer was accepted counts as ok even when an earlier chain entry that the router
rescued it from had failed (`acc` rows), and under `LLM_ROUTER_LOCAL_TEXT_JOBS=1` the router's `chain_errors` tell a
rescued timeout (`infra`) from a rescued bad answer. A demoted lane answers every placeholder command with a denial (`no_lane`, given before the router is loaded, so within a
blink of an eye): Claude writes the text itself and nothing is bound until `llm-router local-check enable commit_message
--model <model>` is run, which also restarts the window. The window is shared with every other use of the
lane, so read `text_job_local_attempts.jsonl` before the hook is registered, and check `llm-router doctor` for
demotion notices afterwards.

The hook is instrumented for the G1 hook-latency KPI: the wait on the local model is the `text_job` model phase and
the wait for the job slot is `text_job_queue`, so the router-added figure counts neither. The router import is outside
those phases and is router time: a successful job costs a second or more of it, because the hook imports the router
and the model libraries before it calls the model. Because of the `if` filter every invocation is a placeholder
invocation, so text-job-bind gets its own 1,500 ms budget (D-370, router-added, `hook_latency.HOOK_BUDGETS_MS`); other
hooks keep their existing budgets. A job the router would hand back anyway is answered before the import (p50 96 ms,
p95 109 ms; n=20 runs of the real hook script, stub model, load average 3.3 to 4.0, 2026-11-04).

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
