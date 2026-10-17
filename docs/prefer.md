# `llm-router prefer` (P2.13)

Natural-language routing priorities, English and Hebrew. Round A (this change)
parses, echoes and stores them. Routing does not change until the doors read the
stored rules (round B).

```
llm-router prefer "codex before claude"        # order
llm-router prefer "never claude opus"          # ban (provider + model glob)
llm-router prefer "no paid API"                # free providers + a Claude subscription only
llm-router prefer "cap claude 80%"             # or "cap codex 20/day"
llm-router prefer "only local for code"        # code, questions, research, analysis, writing
llm-router prefer "codex לפני claude"          # Hebrew forms: see `llm-router prefer --help`
llm-router prefer --list | --remove p1 | --clear
```

- Deterministic: only the listed forms parse. Anything else is refused (exit 2) with
  three English and three Hebrew examples; nothing is written. Free-form parsing is a
  separate, later item.
- The parsed rule is echoed. A terminal asks `[y/N]`; without a terminal, pass `--yes`
  (exit 3 otherwise). `--dry-run` writes nothing. Over MCP, use
  `llm_router_admin(action="prefer", value="<text>")`: it returns the parsed rules and a
  confirm token and writes nothing. Store by calling again with
  `value='{"text": "<text>", "confirm": "<token>"}'`. The token is bound to the text and
  the currently stored rules, so a stale or invented token stores nothing. (The setup
  tool's own `prefer` action does the same with `confirm=true`, but that tool is only on
  the full tool surface.)
- Contradictions are refused at write time: an order cycle, a ban on an order's first
  target, bans that exclude every provider, `only local` with ollama banned, a cap out of
  range (day n >= 1, percent 1-100).

## Where it is stored

A managed block in `~/.llm-router/routing.yaml` (`$LLM_ROUTER_HOME/routing.yaml`):

```
# >>> llm-router prefer (managed; edit with `llm-router prefer`) >>>
preferences: [
  {"id":"p1","kind":"order",...,"text":"codex before claude"}
]
# <<< llm-router prefer <<<
```

Every byte outside the markers is preserved (comments, CRLF, a missing final newline).
The first write to an existing file saves `routing.yaml.pre-prefer-<UTC timestamp>`.
Writes are atomic and take a lock. The block is JSON flow style: valid YAML for
PyYAML, and readable by hooks with `json.loads` (no PyYAML). A top-level `preferences:`
key outside the block, or damaged markers, makes the writer refuse; readers treat an
unreadable block as no preferences and record `CHZ-FO-USER-RULES-READ`.

Hand edits inside the block are allowed if the JSON stays valid; otherwise use the command.
