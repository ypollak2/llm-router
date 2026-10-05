"""`llm-router resolve --tier X --needs tools,vision` -- inspect what the resolver would pick.

Inspection only: nothing here is wired into live routing. Reads the live inventory
(read-only probes) and the stored capability profile, compares against the last
``inventory --save`` snapshot to flag vanished models, and prints the decision, the
fallback ladder, the warnings and every rejection with its reason.

Exit status: 0 routed, 3 no eligible model (the configured model is kept or the
user is asked), 2 bad arguments.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HELP = """usage: llm-router resolve --tier EASY|MEDIUM|FRONTIER [--needs tools,vision,thinking,json,long-context,ctx=N,local-only]
                          [--configured MODEL] [--allow-unmeasured] [--require-measured] [--verify] [--json]

  --configured        the model to keep when nothing qualifies (default: "model" in ~/.claude/settings.json)
  --allow-unmeasured  treat a model with no measurement and no registry class as EASY
  --require-measured  refuse tiers that rest on the registry prior instead of a calibration
  --verify            first make one zero-cost list-models call per API-key provider (no tokens) so a
                      key that works counts as a verified path; a rejected key is excluded
"""


def configured_model_from_claude_settings(path: Path | None = None) -> str | None:
    """The ``model`` key of Claude Code's settings file, or None. Reads that one key."""
    p = path or Path.home() / ".claude" / "settings.json"
    try:
        value = json.loads(p.read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def _parse(args: list[str]) -> dict | None:
    opts: dict = {"tier": None, "needs": "", "configured": None, "json": False,
                  "allow_unmeasured": False, "require_measured": False, "verify": False}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            opts["json"] = True
        elif a == "--allow-unmeasured":
            opts["allow_unmeasured"] = True
        elif a == "--require-measured":
            opts["require_measured"] = True
        elif a == "--verify":
            opts["verify"] = True
        elif a in ("--tier", "--needs", "--configured") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            i += 1
        else:
            return None
        i += 1
    return opts if opts["tier"] else None


def render(res) -> str:
    lines = [f"tier {res.tier}  needs: {res.needs.describe()}"]
    if res.status == "routed":
        lines.append(f"=> {res.model}  (route: {res.route})")
    else:
        lines.append("=> NO ELIGIBLE MODEL")
        lines.append(f"   keeping: {res.keep_configured}" if res.keep_configured
                     else "   no configured model can be kept; ask the user")
    lines.append(f"reason: {res.reason}")
    if res.fallbacks:
        lines.append("ladder:")
        for n, r in enumerate(res.fallbacks, 1):
            what = r.model or "(ask the user)"
            flag = "  [BELOW TIER]" if r.below_tier else ""
            lines.append(f"  {n}. {what}{flag}  {r.note}")
    if res.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in res.warnings)
    if res.rejected:
        lines.append("rejected:")
        for rej in res.rejected:
            lines.append(f"  - {rej.model}: " + "; ".join(rej.reasons))
    return "\n".join(lines)


def cmd_resolve(args: list[str]) -> int:
    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return 0
    opts = _parse(args)
    if opts is None:
        print("llm-router resolve: --tier is required and arguments must be valid", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        return 2
    from llm_router.resolver import inventory as inv_mod
    from llm_router.resolver.resolve import Needs, Setup, resolve
    from llm_router.resolver.types import Tier

    try:
        tier = Tier.parse(opts["tier"])
        needs = Needs.parse(opts["needs"])
    except ValueError as exc:
        print(f"llm-router resolve: {exc}", file=sys.stderr)
        return 2

    inv = inv_mod.collect_inventory(inv_mod.Probes(), previous=inv_mod.load_snapshot(),
                                    verify=opts["verify"])
    configured = opts["configured"] or configured_model_from_claude_settings()
    res = resolve(tier, needs, Setup(inv, configured),
                  allow_unmeasured=opts["allow_unmeasured"],
                  require_measured=opts["require_measured"])
    print(json.dumps(res.to_dict(), indent=2, default=str) if opts["json"] else render(res))
    return 0 if res.status == "routed" else 3
