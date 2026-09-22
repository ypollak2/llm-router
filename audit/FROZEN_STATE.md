# FROZEN STATE — Ultimate Adversarial Audit

Every finding in `audit/*.md` refers to THIS state. If the tree changes during
the audit, the change and its reason are appended at the bottom of this file.

## Subject

| | |
|---|---|
| Repository | `~/Projects/llm-router` |
| Remote | `https://github.com/ypollak2/llm-router` (public) |
| Branch | `fix/audit-2026-09-22` |
| HEAD | `357a402e8f462f913cf9368244557eaaf7711beb` |
| HEAD message | `chore: regenerate the plugin bundle and mutation scope` |
| HEAD date | 2026-09-22 17:20:51 +0100 |
| Dirty files | 0 |
| `origin/fix/audit-2026-09-22` | identical to HEAD (pushed) |
| Latest tag | `v14.1.0` |
| Package name | `llm-routing` |
| `pyproject` version | `14.1.0` |

**HEAD is not a released state.** `v14.1.0` is an ancestor; this branch carries
15 unreleased commits. What a `pip install llm-routing` user gets today is the
tag, not this tree. Phase 42 must treat those as two different subjects.

## Platform

| | |
|---|---|
| Python | 3.11.15 (`.venv`) |
| OS | Darwin 25.5.0 arm64 / macOS 26.5.1 |
| Lock | `uv.lock`, 558,136 bytes, mtime 2026-09-21 17:30 |

## Size

| | |
|---|---|
| `src/**/*.py` | 412 |
| `tests/test_*.py` | 721 |
| `scripts/**/*.py` | 160 |
| Suite at HEAD | 9,140 results, 0 failed, 0 error, 189 skipped (`-p no:randomly`) |

## Ambient configuration — NOT a clean machine

These are set in the auditing shell and MUST be neutralised by any probe that
claims to measure default behaviour:

```
LLM_ROUTER_BASH_INTERCEPT=off          (set by the auditor, not a product default)
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true
LLM_ROUTER_ENSEMBLE_PRIMARY=ollama/qwen3.8:latest
LLM_ROUTER_ENSEMBLE_SECONDARY=ollama/qwen3-coder:30b
LLM_ROUTER_SIDECAR_PREFETCH=1
```

Provider credentials present: `XAI_API_KEY`, `CLAUDE_CODE_MESSAGING_TOKEN`.
Absent: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`PERPLEXITY_API_KEY`. **Any claim about a provider whose key is absent is
untested here and must be marked as such.**

## Local backends

* `ollama` at `~/.local/bin/ollama`, reachable. Models: `qwen3.5:latest`,
  `qwen3.8:latest`, `qwen3-coder:30b`, `nomic-embed-text:latest`.
* `codex` at `~/.local/bin/codex`.
* `gemini`: absent.

## Live state stores

`~/.llm-router/` exists and holds real operator data (`usage.db`,
`audit.db`, `attempts.jsonl`, `auto-route-debug.log`, `agent_edits/`,
per-session `agent_depth_*.json`, `backups/`, and more).

### Contamination already recorded against this state

Earlier on 2026-09-22, during remediation, a probe that believed it was
sandboxed wrote a **fake $1.25 savings row into the real `usage.db`**
(`savings_stats` id 8827). It was deleted by hand; the table went 8827 -> 8826.
Root cause: `agentic/telemetry._db_path()` did not honour `LLM_ROUTER_HOME`.
Fixed in commit `d766ec6`.

**Consequence for this audit:** the live stores are not pristine, and every
probe MUST run under an isolated `LLM_ROUTER_HOME`. A finding derived from
`~/.llm-router` counts as observational, not experimental.

## Auditor conflict of interest — READ THIS

The 15 commits between `8c7366b` and HEAD were written by the same model
running this audit, in the immediately preceding session. Self-audit is the
correlated-judge failure this audit's own Phase 20 exists to detect.

Mitigation: all Phase 1-47 discovery is delegated to specialist agents that
have not seen that session. The orchestrator performs Phase 0, reconciliation,
Phase 48 (attack the audit) and Phase 50 (reproduce from clean). Findings that
exist only because the orchestrator asserted them are marked as such and
carry reduced confidence.

## Changes to the tree during the audit

_(none yet)_
