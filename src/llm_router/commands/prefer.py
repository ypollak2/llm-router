"""`llm-router prefer "<text>"` -- natural-language routing priorities (P2.13).

Parses English or Hebrew with the deterministic grammar, echoes what it
understood, and stores the rules in the managed block of routing.yaml. Round A
only stores: routing is not changed until the doors read the rules.
"""

from __future__ import annotations

import json
import sys

from llm_router import prefer_grammar, user_rules

_HELP = """usage: llm-router prefer "<text>" [--yes] [--dry-run]
       llm-router prefer --list | --remove <id> | --clear

  Store a routing priority in the managed block of ~/.llm-router/routing.yaml
  (everything else in the file is left byte-for-byte alone). Forms:
    X before Y [before Z]  | prefer X over Y   order
    never X | no X         ban (X may be "claude opus", "gemini cli", ...)
    no paid API            only free providers and a Claude subscription
    cap X N/day | cap X N% limit a provider
    only local for <task>  code, questions, research, analysis, writing
  Hebrew equivalents work too, e.g. "codex לפני claude", "בלי API בתשלום".

  --yes       store without asking (required when stdin is not a terminal)
  --dry-run   show what was understood, write nothing
  --list      show stored preferences
  --remove    delete one preference by id (p1, p2, ...)
  --clear     delete every stored preference
  Exit codes: 0 ok, 2 refused (unknown phrase or conflict), 3 needs --yes.
"""


def evaluate(text: str) -> tuple[list[dict], str | None, list[str]]:
    """(rules, refusal, echo) for *text*, including the conflict check."""
    result = prefer_grammar.parse(text)
    if result.refusal:
        return [], result.refusal, []
    reason = user_rules.check_conflicts(user_rules.read_rules(), result.rules)
    if reason:
        return [], f"Conflicts with the stored preferences: {reason}.", []
    return result.rules, None, result.echo


def _confirm(question: str, assume_yes: bool) -> int | None:
    """None to proceed; otherwise the exit code to stop with."""
    if assume_yes:
        return None
    if not sys.stdin.isatty():
        print("Nothing written: re-run with --yes to confirm (stdin is not a terminal).",
              file=sys.stderr)
        return 3
    if input(f"{question} [y/N] ").strip().lower() in ("y", "yes"):
        return None
    print("Not stored.")
    return 1


def _list() -> int:
    rules = user_rules.read_rules()
    if not rules:
        print("No stored preferences.")
        return 0
    for rule in rules:
        print(f"{rule.get('id', '?'):>4}  {prefer_grammar.describe(rule)}   [from: {rule.get('text', '')}]")
    print(f"\n{user_rules.routing_path()}")
    return 0


def cmd_prefer(args: list[str]) -> int:
    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0
    assume_yes = any(a in ("--yes", "-y") for a in args)
    dry = "--dry-run" in args
    rest = [a for a in args if a not in ("--yes", "-y", "--dry-run")]
    try:
        if "--list" in rest:
            return _list()
        if "--clear" in rest:
            if not user_rules.read_rules():
                print("No stored preferences.")
                return 0
            if dry:
                print("Would delete every stored preference.")
                return 0
            stop = _confirm("Delete every stored preference?", assume_yes)
            if stop is not None:
                return stop
            user_rules.write_rules([])
            print("All preferences cleared.")
            return 0
        if "--remove" in rest:
            i = rest.index("--remove")
            if i + 1 >= len(rest):
                print("llm-router prefer: --remove needs an id (see --list)", file=sys.stderr)
                return 2
            rule_id = rest[i + 1]
            if dry:
                print(f"Would remove {rule_id}.")
                return 0
            stop = _confirm(f"Remove preference {rule_id}?", assume_yes)
            if stop is not None:
                return stop
            user_rules.remove_rule(rule_id)
            print(f"Removed {rule_id}.")
            return 0
        flags = [a for a in rest if a.startswith("--")]
        if flags:
            print(f"llm-router prefer: unknown argument {flags[0]!r}", file=sys.stderr)
            print(_HELP, file=sys.stderr)
            return 2
        text = " ".join(rest).strip()
        rules, refusal, echo = evaluate(text)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
        for line in echo:
            print(line)
        if dry:
            print("Dry run: nothing written.")
            return 0
        stop = _confirm("Store this rule?", assume_yes)
        if stop is not None:
            return stop
        _all, added = user_rules.add_rules(rules, text)
        if not added:
            print("Already stored; nothing changed.")
        else:
            print(f"Stored {', '.join(r['id'] for r in added)} in {user_rules.routing_path()}")
        print("Note: stored only; routing starts honouring preferences when the doors read them.")
        return 0
    except user_rules.StoreError as exc:
        print(f"llm-router prefer: {exc}", file=sys.stderr)
        return 2


def _confirm_token(text: str) -> str:
    """Stateless token bound to the normalized text and the stored rules.

    A confirm computed for different text, or before the stored rules changed,
    no longer matches, so a stale or invented token stores nothing.
    """
    import hashlib

    basis = prefer_grammar.normalize(text) + json.dumps(user_rules.read_rules(), sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def admin_prefer(value: str) -> str:
    """`llm_router_admin(action="prefer")`: two-step, token-confirmed.

    ``value`` is the priority text (step 1: parses, returns the rules and a
    confirm token, writes nothing) or JSON ``{"text": ..., "confirm": token}``
    (step 2: stores when the token matches).
    """
    text, token = value or "", None
    if text.lstrip().startswith("{"):
        try:
            body = json.loads(text)
            text, token = str(body["text"]), body.get("confirm")
        except (ValueError, KeyError, TypeError):
            return ('Bad value: pass the priority text, or JSON {"text": "...", "confirm": "<token>"}.')
    if not text.strip():
        return 'Give the priority in `value`, e.g. value="codex before claude".'
    rules, refusal, echo = evaluate(text)
    if refusal:
        return refusal
    shown = "\n".join(echo)
    expected = _confirm_token(text)
    if token is None:
        return (f"{shown}\n\nNothing stored yet. To store, call again with "
                f'value=\'{json.dumps({"text": text, "confirm": expected}, ensure_ascii=False)}\'.')
    if token != expected:
        return f"{shown}\n\nNot stored: the confirm token does not match this text and the current preferences; start again without a token."
    try:
        _all, added = user_rules.add_rules(rules, text.strip())
    except user_rules.StoreError as exc:
        return f"Not stored: {exc}"
    if not added:
        return f"{shown}\n\nAlready stored; nothing changed."
    ids = ", ".join(r["id"] for r in added)
    return f"{shown}\n\nStored {ids} in {user_rules.routing_path()}. Stored only: routing honours it once the doors read it."
