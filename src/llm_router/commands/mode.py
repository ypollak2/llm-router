"""`llm-router mode [advise|route]` -- the routing authority switch (P2.2-A).

Writes one small state file the proxy re-reads on every request, so nothing is
restarted and neither ``settings.json`` nor a service definition is touched.
"""

from __future__ import annotations

import sys

_HELP = """usage: llm-router mode [advise|route]

  (no argument)  print the current routing authority and where it comes from
  route          the proxy may rewrite the model and serve locally (the default)
  advise         the proxy forwards the model you asked for, byte for byte, and
                 only logs what it would have proposed (tier_proposed)

  The proxy reads the mode on every request: no restart. The environment
  variable LLM_ROUTER_AUTHORITY, when set, overrides the file.
"""

_SOURCE = {"env": "env", "file": "file", "default": "default"}


def cmd_mode(args: list[str]) -> int:
    from llm_router import authority

    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0
    if len(args) > 1 or (args and args[0] not in authority.MODES):
        print(f"llm-router mode: expected advise or route, got {' '.join(args)!r}", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        return 2
    if not args:
        mode, source = authority.describe()
        print(f"mode: {mode} ({_SOURCE[source]})")
        return 0
    value = authority.set_mode(args[0])
    env_mode, source = authority.describe()
    if value == authority.ADVISE:
        print("mode: advise -- the proxy now forwards the model you asked for and only logs its proposal.")
    else:
        print("mode: route -- the proxy may rewrite the model and serve locally (the default).")
    if source == "env" and env_mode != value:
        print(f"note: {authority.ENV_VAR} is set in this environment and wins over the file "
              f"(effective mode here: {env_mode}).")
    return 0
