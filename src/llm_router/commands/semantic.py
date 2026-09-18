"""``llm-router semantic`` — build, inspect and explain the derived index.

The index answers "where is X defined" and "what have we learned about this
file". Both are questions you want to ask from a terminal before you trust the
thing to answer them inside a prompt, which is what these subcommands are for.

``status`` is the one that matters most in practice. Every field it prints
answers a question that has bitten this project: which project am I actually
scoped to, is the index built, how stale is it, and — the one people forget —
which arm is currently selected, because a run labelled with the wrong arm is
a measurement of nothing.
"""

from __future__ import annotations

import sys


def _b(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


_USAGE = """\
llm-router semantic <command>

  index     build or refresh the derived index for this project
  status    scope, index size, staleness, and the selected arm
  explain   show what a prompt would retrieve, without sending anything
  lessons   engineering experience that applies to a path or symbol
  seed      write the starter experience records (never overwrites)
"""


def _scope_line() -> str:
    from llm_router.semantic.scope import resolve_scope, scope_key
    root = resolve_scope()
    return f"{root}  {_dim('(' + scope_key(root) + ')')}"


def cmd_semantic(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0

    sub, rest = argv[0], argv[1:]

    if sub == "index":
        from llm_router.semantic import indexer
        import time

        started = time.monotonic()
        result = indexer.index()
        elapsed = time.monotonic() - started
        print(f"{_b('scope')}      {result.root}")
        print(f"{_b('parsed')}     {result.files_parsed}")
        print(f"{_b('unchanged')}  {result.files_skipped}")
        print(f"{_b('failed')}     {result.files_failed}")
        print(f"{_b('forgotten')}  {result.files_forgotten}")
        print(f"{_b('entities')}   {result.entities}")
        print(f"{_b('elapsed')}    {elapsed:.1f}s")
        if result.files_failed:
            print(_dim("\nfiles that failed to parse are recorded as "
                       "unavailable, not left with their old structure"))
        return 0

    if sub == "status":
        from llm_router.semantic import modes, store as sstore
        from llm_router.semantic.scope import resolve_scope

        root = resolve_scope()
        print(f"{_b('project')}    {_scope_line()}")
        db = sstore.index_path(root)
        if not db.exists():
            print(f"{_b('index')}      {_dim('not built')} — run "
                  f"`llm-router semantic index`")
        else:
            conn = sstore.connect(root)
            try:
                files = conn.execute("SELECT COUNT(*) n FROM file").fetchone()["n"]
                ents = conn.execute("SELECT COUNT(*) n FROM entity").fetchone()["n"]
                failed = conn.execute(
                    "SELECT COUNT(*) n FROM file WHERE parse_status != 'parsed'"
                ).fetchone()["n"]
            finally:
                conn.close()
            size_kb = db.stat().st_size // 1024
            print(f"{_b('index')}      {files} files · {ents} entities · {size_kb} KB")
            if failed:
                print(f"{_b('unparsed')}   {failed} "
                      f"{_dim('(reported as unavailable, never as stale structure)')}")

        try:
            cfg = modes.current()
        except ValueError as exc:
            print(f"{_b('arm')}        {_dim('INVALID')} — {exc}")
            return 1
        import os
        arm = os.environ.get("LLM_ROUTER_SEMANTIC_ARM", "").strip()
        print(f"{_b('arm')}        {arm or _dim('none')}")
        print(f"{_b('source')}     {cfg.source.value}")
        print(f"{_b('history')}    {cfg.history.value}")
        print(f"{_b('intervene')}  {cfg.intervention.value}")
        if not cfg.any_enabled:
            print(_dim("\nall three are off, which is the default: the "
                       "semantic layer has not been shown to help yet, and "
                       "shipping it on would skip the experiment it exists for"))
        return 0

    if sub == "explain":
        if not rest:
            print("usage: llm-router semantic explain <prompt>")
            return 2
        from llm_router.semantic import pack as spack
        from llm_router.semantic.experience import ExperienceStore
        from llm_router.semantic.scope import resolve_scope
        from llm_router import okf

        root = resolve_scope()
        exp_root = okf.project_knowledge_dir(root=root) / "experience"
        built = spack.build(" ".join(rest), root=root,
                            experience=ExperienceStore(exp_root))
        print(f"{_b('status')}   {built.retrieval_status}")
        print(f"{_b('snapshot')} {built.snapshot_id}")
        print(f"{_b('tokens')}   {built.retrieved_tokens} / {built.budget_tokens}")
        if built.missing_requirements:
            print(f"{_b('missing')}  {', '.join(built.missing_requirements)}")
        print()
        rendered = spack.render(built)
        print(rendered or _dim("(nothing retrieved — which is an answer, not a "
                               "failure; `status` above says which)"))
        if built.omissions:
            print(f"\n{_b('omitted')}")
            for note in built.omissions:
                print(f"  {note}")
        return 0

    if sub == "lessons":
        from llm_router.semantic.experience import ExperienceStore, record_id
        from llm_router.semantic.scope import resolve_scope
        from llm_router import okf

        root = resolve_scope()
        store = ExperienceStore(okf.project_knowledge_dir(root=root) / "experience")
        records = (store.applicable(paths=rest, symbols=rest) if rest
                   else store.all())
        if not records:
            print(_dim("no applicable records — abstaining is the correct "
                       "answer when nothing applies"))
            return 0
        for record in records:
            repair = getattr(record, "repair_status", None)
            # Built outside the f-string on purpose: nesting the same quote
            # character inside an f-string is a 3.12 feature and this project
            # supports 3.11, where it is a SyntaxError at import time — which
            # would take the whole CLI down, not just this command.
            axes = (f"review={record.review.value} "
                    f"validation={record.validation.value} "
                    f"enforcement={record.enforcement.value}")
            if repair is not None:
                axes += f" repair={repair.value}"
            print(f"{_b(record_id(record))}")
            print(f"  {record.statement}")
            print(f"  {_dim(axes)}")
            if record.check_refs:
                print(f"  {_dim('checked by: ' + ', '.join(record.check_refs))}")
        for a, b in store.conflicts():
            print(f"\n{_b('CONFLICT')} {record_id(a)} vs {record_id(b)} — both "
                  f"apply and they disagree")
        return 0

    if sub == "seed":
        from llm_router.semantic import seed_lessons
        from llm_router.semantic.scope import resolve_scope
        from llm_router import okf

        root = resolve_scope()
        target = okf.project_knowledge_dir(root=root) / "experience"
        written = seed_lessons.seed(target)
        print(f"{_b('wrote')}  {written} record(s) to {target}")
        if written == 0:
            print(_dim("everything was already there — seeding never "
                       "overwrites, because that would silently revert a "
                       "correction someone made by hand"))
        return 0

    print(f"unknown subcommand: {sub}\n\n{_USAGE}", file=sys.stderr)
    return 2
