"""`llm-router sessions` — inspect and repair the durable session-context store.

Exists for one job: recovering events stranded by the pre-S2-1 scoping bug, where
`_project_id()` hashed the raw cwd so a session that moved between directories
scattered its log across several buckets while `build_session_context` read one.

Deliberately a command and not a runtime path. Consolidating shards means reading
across project buckets, which CHZ-AUD-024 forbids a running project from doing
silently. The owner of the machine explicitly migrating their own store is a
different act, and the same one `okf adopt` already performs for the knowledge
store.
"""
from __future__ import annotations


def _b(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


def _g(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _y(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def _print_help() -> None:
    print(f"""
{_b('llm-router sessions')} — the durable session-context store

  {_b('llm-router sessions status')}         sessions whose events are split across buckets
  {_b('llm-router sessions merge')}          show what merging would do (no changes)
  {_b('llm-router sessions merge --apply')}  consolidate them

Merging rewrites each session's log into the CURRENT project's bucket, ordered by
timestamp. Originating shards are renamed to `.premerge` rather than deleted, so a
merge you dislike costs disk and not a conversation.
""".rstrip())


def cmd_sessions(args: list[str]) -> None:
    from llm_router import session_store as ss

    sub = args[0] if args else "status"
    if sub in ("-h", "--help", "help"):
        _print_help()
        return

    frag = ss.fragmented_sessions()

    if sub == "status":
        print(f"\n{_b('Session context store')}")
        if not frag:
            print(_g("  No fragmented sessions — every session's events are readable.\n"))
            return
        total = sum(v["events"] for v in frag.values())
        stranded = sum(v["stranded"] for v in frag.values())
        print(f"  {_y('fragmented sessions')}: {len(frag)}")
        print(f"  events recorded    : {total}")
        print(f"  events readable    : {total - stranded}")
        print(f"  {_y('events stranded')}    : {stranded}\n")
        for sid, v in sorted(frag.items(), key=lambda kv: -kv[1]["stranded"])[:10]:
            spread = ", ".join(str(n) for n in sorted(v["buckets"].values(), reverse=True))
            print(f"    {sid[:8]}  {v['shards']} buckets  [{spread}]  stranded={v['stranded']}")
        print(_dim("\n  `llm-router sessions merge` shows what consolidating would do.\n"))
        return

    if sub == "merge":
        apply = "--apply" in args
        if not frag:
            print(_g("\n  Nothing to merge.\n"))
            return
        print(f"\n{_b('Merging' if apply else 'Merge preview')}  ({len(frag)} session(s))\n")
        recovered = 0
        for sid in frag:
            r = ss.merge_session_shards(sid, apply=apply)
            recovered += r["merged"]
            mark = _g("merged ") if r["applied"] else _dim("would  ")
            print(f"  {mark} {sid[:8]}  {r['shards']} shards -> {r['events']} events")
        print()
        if apply:
            print(_g(f"  Recovered {recovered} previously unreadable event(s).\n"))
        else:
            print(_dim(f"  Would recover {recovered} event(s). Re-run with --apply.\n"))
        return

    print(f"Unknown subcommand: {sub!r}")
    _print_help()
