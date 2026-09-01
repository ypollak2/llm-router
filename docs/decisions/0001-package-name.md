# ADR 0001 — One name: `llm-routing`

**Status:** accepted · **Date:** 2026-08-31 · **Decider:** project owner

## Context

The project answered to three names, and the README had to apologise for it twice:

| Surface | Name |
|---|---|
| PyPI package | `llm-routing` |
| CLI command | `llm-router` |
| GitHub repo | `llm-router` |
| Python module | `llm_router` |

This was not only cosmetic. The three-way split produced real defects:

- **The statusline savings figure silently vanished** because its interpreter
  probe looked for `command -v llm_router` (underscore) when the installed
  executable is `llm-router` (hyphen). No underscore executable has ever
  shipped. The failure was invisible because the query is wrapped in a
  never-break-the-statusline fallback.
- Issues #72 and #82 were both naming sweeps, through `install.py`, `doctor.py`
  and then the rest of `src/`. Neither covered the statusline, which is how the
  bug above survived them.

Shipping an npm distribution (task 37) forces the question, because the name has
to be chosen before it is baked into a second registry.

## The npm constraint

`llm-router` on npm **is taken**: owner `logsv`, v1.1.0, published 2025-08-04,
untouched since 2025-08-05 — abandoned, but occupied.

More importantly, npm rejects new package names that are *too similar* to
existing ones, comparing after punctuation is stripped. So `llm_router` and
`llmrouter` both normalise to `llmrouter` and collide with the existing
`llm-router`. They appear free on a registry lookup and would be refused at
publish time.

## Decision

**`llm-routing` everywhere it can be.**

| Surface | Name | Change |
|---|---|---|
| PyPI package | `llm-routing` | none — already correct |
| npm package | `llm-routing` | new; verified available, normalises to `llmrouting`, no collision |
| CLI command | `llm-router` | **kept** — see below |
| Python module | `llm_router` | **kept** — PEP 8 requires the underscore |
| GitHub repo | `llm-router` | kept; renaming costs the little inbound link equity that exists |

## Why the CLI keeps the hyphen

Renaming the command would break every existing user's muscle memory, shell
history, scripts and the hook `command` strings already written into
`settings.json` on installed machines — to buy consistency with a package name
users type exactly once. The distribution name and the command name differing is
ordinary (`ripgrep`/`rg`, `python-dateutil`/`dateutil`).

What was never acceptable is *code* that confuses the two. That is the actual
defect, and it is addressed by the lint that already covers `src/` (#82) plus the
statusline fix.

## Consequences

- Task 37 (npm wrapper) is unblocked and publishes `llm-routing`.
- Task 31's sweep is narrower than originally scoped: it aligns documentation and
  any remaining `llm_router`-as-a-command references, rather than performing a
  rename.
- File a dispute for the abandoned npm `llm-router` in parallel. It costs
  nothing, and if granted it becomes an alias pointing at `llm-routing` — never
  a second published surface.
- One rule for contributors: **`llm_router` is a Python module and nothing else.**
  It is never an executable, never a package name, never a CLI invocation.
