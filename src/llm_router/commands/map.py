"""`llm-router map [--json]` -- the one resource map (P1.8)."""

from __future__ import annotations

import sys

_HELP = """usage: llm-router map [--json]

  Every resource the router can spend (subscription seats, local models, API
  keys) with status, quota (labelled measured / estimated / default), marginal
  cost, capabilities and effective priority. Reads ~/.llm-router/resource_map.json
  when it is under 300 s old, else rebuilds it. Key values are never shown.
  --json    the same JSON the MCP tool llm_router_status(view="map") returns
"""


def cmd_map(args: list[str]) -> int:
    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0
    unknown = [a for a in args if a != "--json"]
    if unknown:
        print(f"llm-router map: unknown argument {unknown[0]!r}", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        return 2
    from llm_router import resource_map as rm

    data = rm.refresh_if_stale()
    print(rm.view_json(data) if "--json" in args else rm.render_table(data))
    return 0
