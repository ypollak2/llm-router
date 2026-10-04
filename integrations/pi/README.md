# Local Pi agent profile (`llm-router pi`)

A fully local coding agent: the [Pi](https://pi.dev) harness
(`@earendil-works/pi-coding-agent`, tested with 0.99.1) driving one Ollama model
(tested with `qwen3.6:35b-a3b-coding` at a 32k context), plus a set of Pi extensions
that close the gaps a capability probe found. Nothing leaves the machine: Pi runs with
`--offline` and its only provider is the local Ollama server.

```bash
npm install -g @earendil-works/pi-coding-agent
llm-router pi --model qwen3.6:35b-a3b-coding                   # interactive
llm-router pi --model qwen3.6:35b-a3b-coding -- -p "fix the failing test"   # one shot
llm-router pi --model qwen3.6:35b-a3b-coding --print-command   # show what would run
```

`llm-router pi` writes a Pi agent directory under `$LLM_ROUTER_HOME/pi/agent`
(`models.json` for the one model, `settings.json`, the sub-agent definitions) and starts
`pi --offline --provider ollama` with every extension below and `system-rules.md`. It
reads the context window from Ollama (`/api/ps`, then `OLLAMA_CONTEXT_LENGTH`; reading
does not load a model) and prints it with its source; `--context N` overrides it. Image
input is declared only with `--vision` (see Images below). It never changes Ollama's
settings.
Your own `~/.pi/agent` is not used or modified.

## What each piece fixes

The gaps come from the 2026-10-04 harness-parity probes (Pi + qwen3.6, Route C):
8 of 11 required capabilities passed at >=19/20 before this profile. The
"after" numbers are in the PR that added this directory, with their n and dates.

| Gap (measured before) | Fix | Where |
|---|---|---|
| **Cancellation.** SIGINT ended Pi, but the bash command kept running and finished its work (0/2). Pi's print/JSON mode handles SIGTERM and SIGHUP, not SIGINT. | On SIGINT, abort the run (which kills the command's process group), then exit 130. | `extensions/cancel.ts` |
| **Write paths.** The model dropped the leading `/` or mistyped one character of a long working directory; the write "succeeded" elsewhere (7/9). | Rewrite a write/edit path only when the target directory does not exist and the repaired path is inside the working directory. The tool result says what changed. Plus a rule: use relative paths. | `extensions/paths.ts`, `lib/paths.mjs` |
| **AskUserQuestion.** With a question tool offered, the model asked in prose (0/2); Pi's example tool has no answer channel outside the TUI. | A `question` tool in every mode, answered by the UI (TUI or RPC client), `LLM_ROUTER_PI_ANSWER`, or `LLM_ROUTER_PI_ANSWER_CMD` (question as JSON on stdin, answer on stdout); with none, it tells the model to stop. A run that ends with a choice asked in prose is continued once with an instruction to call the tool. | `extensions/question.ts` |
| **Sub-agents.** The model skipped the tool, or gave the child a task scoped to an ancestor directory (answer 1453, truth 3-7) (0/2). | A `subagent` tool that always runs the child in the working directory, prefixes its task with that directory and a scope rule, and narrows any ancestor path in the task to the working directory (and says so in the result). Children get the safety extensions but not `subagent` or `question`. Interrupting the parent interrupts the child, whose own cancel extension stops its shell commands. Agents: `agent/agents/worker.md`, `scout.md`. | `extensions/subagent.ts`, `lib/scope.mjs` |
| **Compaction lost the task** (13 of 14 compacted sessions wrong). Pi summarized the user's request away and kept the huge tool results; its serializer also cuts each tool result to 2,000 characters. | A replacement summary: every user request verbatim, every tool call listed with its output labelled as tool output, the lines of large outputs that the request needs (copied by the model, each checked to really occur, plus a plain search for identifiers from the request), and nothing else retained, so the context really shrinks. | `extensions/compaction.ts`, `lib/compaction.mjs` |
| **Silent truncation.** Ollama's `/v1` endpoint drops leading messages when a prompt exceeds the loaded context, without an error, and ignores `truncate: false` there. Pi's chars/4 estimate undercounts digit-heavy text about 2x for qwen, so its threshold does not fire first. | Before each request, estimate the prompt with a digit-aware estimate (`lib/tokens.mjs`: fitted on 8 measured prompt deltas of 2k-24k tokens, each within 5%; +2.7% on one 25,247-token log checked against Ollama) and refuse it if it exceeds the window minus a reply reserve, with an error Pi treats as context overflow. After each reply, a prompt that filled the window is turned into the same error. | `extensions/context-guard.ts` |
| **Images.** `models.json` declared text only, so Pi dropped an attached image and the model said it could not see it (10/10 explicit). | `--vision` declares image input. **Off by default**: with images declared, `qwen3.6:35b-a3b-coding` returned a confident wrong code in 20 of 20 trials, although Ollama lists `vision` for it. An explicit "cannot see" is safer than a wrong answer; enable it only for a model whose vision you have checked. | `src/llm_router/commands/pi.py` |

Every intervention (pre-send refusal, truncation detected, a reply that may have
outgrown the window, a failed compaction extraction) is appended to
`$LLM_ROUTER_HOME/pi/events.jsonl`. Questions are logged to
`LLM_ROUTER_PI_QUESTION_LOG` when set.

## Configuration

| Variable | Read by | Meaning |
|---|---|---|
| `LLM_ROUTER_PI_MODEL` | launcher | default for `--model` |
| `LLM_ROUTER_PI_CONTEXT` | launcher | default for `--context` |
| `LLM_ROUTER_PI_BIN` | launcher | the `pi` executable (else `pi` on PATH) |
| `LLM_ROUTER_PI_PROFILE_DIR` | launcher | use profile files from this directory |
| `LLM_ROUTER_PI_ANSWER` | question.ts | fixed answer to every question (scripted runs) |
| `LLM_ROUTER_PI_ANSWER_CMD` | question.ts | command that answers: question JSON on stdin, answer on stdout |
| `LLM_ROUTER_PI_QUESTION_LOG` | question.ts | append every question and its answer here |
| `LLM_ROUTER_PI_REPLY_RESERVE` | context-guard.ts | tokens kept free for the reply (default 4096) |
| `LLM_ROUTER_PI_COMPACT_VERBATIM_CHARS` | compaction.ts | tool outputs up to this size stay verbatim (default 1200) |
| `LLM_ROUTER_PI_COMPACT_CHUNK_TOKENS` | compaction.ts | cap on one extraction chunk (default 16000, also bounded by the window) |

The launcher sets `PI_CODING_AGENT_DIR`, `PI_OFFLINE=1`, `LLM_ROUTER_PI_EVENT_LOG` and
`LLM_ROUTER_PI_CHILD_EXTENSIONS` (what sub-agents load) itself. If neither `/api/ps` nor
`OLLAMA_CONTEXT_LENGTH` reports the window, it refuses to start until `--context` is
given: a wrong guess either way breaks the pre-send check.

## Not fixed here

- **Ollama memory.** Concurrency and memory headroom are Ollama server settings
  (`OLLAMA_NUM_PARALLEL`); this profile does not touch them.
- **Pi core bugs** are worked around from extensions, and reproduced without a model
  in `upstream/repro_pi_core.py` (SIGINT, compaction). Ollama's silent message drop is
  reproduced in `upstream/repro_ollama_v1_drop.py` (needs a model).
- **Model quality.** The profile makes the harness behave; it does not make the model
  answer correctly. A vision-capable model can still misread an image.

## Files

```
system-rules.md          appended to Pi's system prompt
agent/settings.json      compaction settings (threshold = context - 16384)
agent/agents/*.md        sub-agent definitions
extensions/*.ts          Pi extensions, loaded with -e
extensions/lib/*.mjs     their pure logic, unit-tested with node --test
tests/                   node tests, a scripted OpenAI server, a Pi test harness
upstream/                reproductions for the upstream bugs
```

## Tests

```bash
node --test integrations/pi/tests/lib.test.mjs
uv run --extra dev pytest tests/test_pi_profile.py      # end-to-end parts need Pi:
LLM_ROUTER_PI_BIN=$(which pi) uv run --extra dev pytest tests/test_pi_profile.py
```

The end-to-end tests run the real Pi binary against `tests/mock_openai.py`, a scripted
OpenAI-compatible server, so they need no model and make no network call beyond
127.0.0.1.
