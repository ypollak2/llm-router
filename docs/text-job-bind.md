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

Other options stay (`--no-verify`, `-a`, `--trailer`, `--draft`, `--base`). Attribution lines go in a
second `-m`, or on lines after the placeholder inside the PR body quotes; they are kept as written.
The rules text that teaches this is the section "Commit messages and PR bodies" of
`src/llm_router/rules/llm_router.md`, which every session and every sub-agent loads.

## What the hook does

`src/llm_router/hooks/text-job-bind.py` (installed as `llm_router-text-job-bind.py`, PreToolUse,
matcher `Bash`):

1. Reads the command with a strict shell scanner (`text_job_bind/shellscan.py`). A command without the
   placeholder is left alone (a hand-written commit or PR command only leaves a ledger row).
2. Builds the job input from the repository as it is now: `git diff --cached` (`git diff HEAD` with
   `-a`), or for a PR the commits, the changed files and the diff against the base. A diff far larger
   than the model can read is cut per file; every changed file is still named.
3. Runs the router's own `llm_text_job` (same prompts, same acceptance checks, same ledgers), local
   model only: a passed `llm-router local-check` lane is required, and `LLM_ROUTER_LOCAL_TEXT_JOBS=1`
   (the full routing chain, cloud models included) is treated as unset for this hook.
4. Writes the accepted text to a private file (`$LLM_ROUTER_HOME/text_job_bind/`, 0600) and rewrites
   the command to `-F <file>` or `--body-file <file>` through `hookSpecificOutput.updatedInput`, with
   every other field of the tool input copied. No `permissionDecision` is returned, so the rewritten
   command goes through the normal permission flow and existing allow rules apply to it.
5. Re-reads the rewritten command and proves it (same commands, no placeholder left, no message option
   left, the file is read) before it is emitted. An unverified rewrite is never emitted.

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
GitHub user. The hook also keeps a deadline guard that answers with a deny shortly before the host's
silent kill.

## Shapes that are refused, and what to do

| Shape | Why | Do instead |
|---|---|---|
| `git add x && git commit -m "${LLM_TEXT:?}"` (any file-changing command before it) | the text is built from the repository before the line runs | stage in its own command, then commit alone |
| `git commit --amend`, `git commit -F`, paths after the message, `--fill`, `--web`, `-R` | no single staged diff to describe, or a conflicting message source | write the text yourself |
| a heredoc or `$(...)` in the message, wrappers such as `env`, `sudo`, loops, groups | cannot be read safely | simplify the command |
| nothing staged, no commits ahead of the base | nothing to describe | stage or commit first |

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
PreToolUse entry with matcher `Bash` and its own timeout (`130`) into `~/.claude/settings.json`; the
plugin bundle (`hooks/hooks.json`, `.codex-plugin/hooks.json`) carries the same entry. The rules text
refreshes by itself at the next MCP server start. Until the hook is registered a model that follows the
new rules gets Claude Code's refusal or the shell's abort (see "Fail-closed by construction") and writes
the text itself.

Only one router hook may rewrite Bash input: Claude Code does not merge `updatedInput` from several hooks.

## Measuring it

`python -m llm_router.text_job_bind report [--days N] [--json]` reads `text_job_binds.jsonl`
(text-free: sizes, a reason code, latency; two generations of 2 MiB, 0600). One row per `git commit` or
`gh pr create` command the hook sees: `bound` (the checked local text was put in the command), `denied`
(with an outcome and a reason) or `handwritten` (the denominator). The report prints adoption
(placeholder commands of all such commands), the bound rate, bound latency p50 and p95, and the
estimated Claude output tokens avoided (characters bound divided by 4, an estimate; the hand-written
characters are printed the same way as the upper bound of what is left). Read every figure with its N.

The hook is instrumented for the G1 hook-latency KPI: the wait on the local model is the `text_job`
model phase, so the router-added figure does not count it.

## Not covered

- `git commit --amend`, PR edits, `gh pr create --fill`, and any shape in the table above.
- Interactive approval prompts, Claude Code versions other than 2.1.317 and a real local model were not
  exercised in the live canary (see PR #820, which lists what was).
- Hosts other than Claude Code: the rules are not taught there, and the hook is in the Codex plugin
  bundle only because the bundle is generated from one table.
