# `llm-router prefer` (P2.13)

Natural-language routing priorities, English and Hebrew. Round A parses, echoes and
stores them. Round B reads them where chains are built: the MCP/router door, the hook
and SDK chain builder, and the proxy's non-Claude model choice (see "What routing does
with each rule" below).

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
  separate, later item. The forms that parse and are enforced identically at the router,
  hook and proxy non-Claude doors are pinned by `tests/test_p213_round_b_doors.py`
  (20 phrasings). Beyond the examples above they include `never use gemini`, `stop
  using gemini`, `I don't want gemini`, `use openai before ollama`, `limit gemini to two calls a
  day`, `cap gemini at eighty percent`, `keep gemini under <percent>%`, `keep code local`, and
  `don't use paid APIs`; a count may be a digit or an English number word. Wordings that
  still refuse (pinned as strict xfails in that test): `I'd rather use openai than ollama`,
  `gemini is off limits`, `free providers only`, `gemini max 2 per day`, `don't let gemini
  go over <percent>%`, `code stays local`. `avoid gemini` is refused on purpose: "avoid" reads as a soft
  preference, not the hard ban `never use gemini` gives.
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

## What routing does with each rule

- **order / ban**: an order puts the first target ahead of the second in the chain; a ban
  removes the target.
- **cap N/day**: once the router has executed N calls for the target in the rolling 24 hours,
  the target drops from the chain until older calls age out. Only calls the router itself
  sends count and are limited (MCP llm tools, hook- and SDK-routed calls). Calls the proxy
  serves from a non-Claude model are limited by a hit cap but are not counted toward it.
  Claude Code turns forwarded to Claude are neither counted nor limited.
- **cap N%**: the target drops while its provider's quota pressure on the resource map
  (`llm-router map`) is at or above N%. No fresh map, or no measured or estimated pressure,
  means the cap is not hit.
- **only local for <task>**: for that task type the chain keeps only Ollama models on a
  loopback daemon that are not `-cloud` tags. LM Studio, vLLM and llama.cpp do not count as
  local for this rule. With no such model the request gets the usual no-route result. Other
  task types are unchanged.
- **no paid API**: keeps local runtimes (Ollama, LM Studio, vLLM, llama.cpp), Codex and
  Gemini CLI (treated as unpaid because they run on your seat), an `openai_compat` endpoint
  only when its base URL is loopback, and Claude only in Claude subscription mode. Every
  API-key provider is dropped.
- Bans, caps, no paid API and only-local also apply to a pinned model, including the proxy's
  configured `--model` pin (an excluded pin keeps the call on Claude). Order rules do not move
  a pin. The `--serve local-agent` pin has no task type, so only-local does not apply to it.
- Not implemented: a ban or cap on a Claude model at the proxy's tier decision
  (`proxy/tiers.py`). A cap that cannot be read never blocks routing.

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
