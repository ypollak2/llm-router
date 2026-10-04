"""`llm-router inventory [--json] [--save]` -- what models this machine can actually run.

Read-only. Probes Ollama (/api/tags, /api/show, /api/ps), the Claude/Codex/Gemini
CLIs (binary path and login STATE) and API-key variable NAMES. No secret value is
ever read into the output. ``--save`` additionally records the snapshot at
``state_path("inventory.json")`` so a later run can tell which models vanished.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace

_HELP = """usage: llm-router inventory [--json] [--save] [--verify]

  --json    machine-readable output (no secrets: variable names only)
  --save    record this snapshot so a later run can flag models that were removed
  --verify  one zero-cost list-models call per API-key provider (no tokens used) to learn
            whether the key is accepted; results are cached for 10 minutes. Off by default.
"""


def cmd_inventory(args: list[str]) -> int:
    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0
    unknown = [a for a in args if a not in ("--json", "--save", "--verify")]
    if unknown:
        print(f"llm-router inventory: unknown argument {unknown[0]!r}", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        return 2
    from llm_router.resolver import inventory as inv_mod
    from llm_router.resolver.types import inventory_to_dict

    inv = inv_mod.collect_inventory(previous=inv_mod.load_snapshot(), verify="--verify" in args)
    if "--save" in args:
        # A snapshot that already carries removed models would resurrect them on
        # the next run; save only what is present now.
        live = replace(inv, models=[m for m in inv.models if m.present], removed=[])
        inv_mod.save_snapshot(live)
    if "--json" in args:
        print(json.dumps(inventory_to_dict(inv), indent=2))
    else:
        print(inv_mod.render_table(inv))
    return 0
