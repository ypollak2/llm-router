#!/usr/bin/env python3
"""End-to-end north-star trace: does a routable hint actually reach a cheap model?

Unit tests prove `resolve()` is self-consistent. They cannot prove the north star,
because the north star spans three processes that each hold their own idea of the
tool surface:

    prompt ─▶ auto-route.py hook ─▶ hint naming a tool ─▶ MCP server ─▶ model

The shipped bug lived exactly in the seam: the hook's idea of the tool surface and
the server's actual registration disagreed, and nothing compared them. So this
trace resolves the tool list from the REAL server object (not from
`llm_router.tool_surface`) and checks the hook's emitted name against it.

Stages traced per case:
  1  classify      run the real hook, capture the injected directive
  2  extract       pull the tool name the caller is being told to call
  3  registered?   is that name in the server's ACTUAL registered tool list?
  4  invoke        call the resolved tool for real
  5  model         which model answered — free/local, or did it fall back?

Usage:
  python3 scripts/trace_northstar.py                # all tiers, no live calls
  python3 scripts/trace_northstar.py --live         # also stage 4/5 (needs Ollama)
  python3 scripts/trace_northstar.py --tier consolidated --live
Exit: 0 all green, 1 any stage failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "src" / "llm_router" / "hooks" / "auto-route.py"

# Prompts chosen to be genuinely offloadable and context-free — the exact class
# the report says died on a bad tool name ("the ~2 genuinely offloadable units").
CASES = [
    ("write a python function to sort a list", "code"),
    ("what is the capital of France", "query"),
    ("summarize the difference between TCP and UDP", "query"),
]

TIERS = ("consolidated", "routing", "core", "off")

G, R, Y, D, X = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def server_registered_tools() -> set[str]:
    """The tool names the MCP server ACTUALLY registers, from the server object.

    Deliberately not `llm_router.tool_surface.registered_tools()` — using the module
    under test as its own oracle is what let the hook and the server drift apart.
    """
    from llm_router import server as _srv

    tools = asyncio.run(_srv.mcp.list_tools())
    return {t.name for t in tools}


def run_hook(prompt: str, tier: str, session: str) -> str:
    """Run the real UserPromptSubmit hook; return the injected directive text."""
    env = {
        **os.environ,
        "LLM_ROUTER_SLIM": tier,
        "LLM_ROUTER_DIRECT_EXECUTION": "false",  # want the directive, not a draft
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt, "session_id": session}),
        capture_output=True, text=True, env=env, timeout=180,
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    return payload.get("hookSpecificOutput", {}).get("additionalContext", "")


# The caller reads a name out of the directive; so does this. Matches both the
# bare form (llm_code) and the door form (llm(task="code")).
_TOOL_RE = re.compile(r"\b(llm(?:_\w+)?|llm_router_\w+)(\(task=\"(\w+)\"\))?")


def extract_tool(directive: str) -> tuple[str | None, str | None]:
    """Return (bare_name, task_arg) for the tool the directive tells you to call."""
    for line in directive.splitlines():
        if not any(k in line for k in ("ROUTE", "SUGGESTED", "action:", "Call ")):
            continue
        m = _TOOL_RE.search(line)
        if m and m.group(1) not in {"llm_"}:
            return m.group(1), m.group(3)
    return None, None


def invoke(bare: str, task: str | None, prompt: str) -> tuple[bool, str]:
    """Actually call the resolved tool and report which model answered."""
    from llm_router import server as _srv  # noqa: F401  (ensures tools are registered)
    from llm_router.tools.consolidated import llm
    from llm_router.tools.text import llm_code, llm_query

    class _Ctx:  # minimal MCP Context stand-in
        async def info(self, *a, **k): pass
        async def debug(self, *a, **k): pass
        async def report_progress(self, *a, **k): pass

    async def _go():
        if bare == "llm":
            return await llm(prompt, _Ctx(), task=task or "auto", tier="fast")
        if bare == "llm_code":
            return await llm_code(prompt, _Ctx(), complexity="simple")
        return await llm_query(prompt, _Ctx(), complexity="simple")

    try:
        out = asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        return False, f"raised {type(e).__name__}: {e}"
    # The tool footer looks like:
    #   > 🤖 **🟢 openai/gpt-4o-mini** · 467 tokens · $0.000074 · 1616ms
    # Keep the PROVIDER prefix — stripping it is how "codex/gpt-5.5" (free) got
    # misread as a paid tier on the first pass of this trace.
    # Footer: `> 🤖 **⬜ cache/codex/gpt-5.5** · $0.000000 · 0ms`
    # The name can carry several segments (cache/<provider>/<model>), so match
    # slashes greedily — a single-slash pattern silently yields "unknown".
    m = re.search(r"\*\*\W*([\w.:/-]+)\*\*", out or "")
    model = m.group(1) if m else "unknown"
    c = re.search(r"\$(\d+\.?\d*)", out or "")
    cost = float(c.group(1)) if c else float("nan")
    return True, f"{model}|{cost}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=TIERS, help="only this tier")
    ap.add_argument("--live", action="store_true", help="also invoke the tool for real")
    ap.add_argument("--fresh", action="store_true",
                    help="append a nonce to each prompt so the response cache is bypassed "
                         "and a LIVE model must answer (a cache hit proves the tool is "
                         "callable, not that a cheap model did the work)")
    args = ap.parse_args()
    tiers = [args.tier] if args.tier else list(TIERS)

    failures = 0
    for tier in tiers:
        os.environ["LLM_ROUTER_SLIM"] = tier
        # Re-resolve the server surface per tier in a clean subprocess: the server
        # module gates at import time, so it must be imported under this tier.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import asyncio,json;from llm_router import server as s;"
             "print(json.dumps(sorted(t.name for t in asyncio.run(s.mcp.list_tools()))))"],
            capture_output=True, text=True,
            env={**os.environ, "LLM_ROUTER_SLIM": tier}, timeout=300,
        )
        try:
            registered = set(json.loads(proc.stdout.strip().splitlines()[-1]))
        except Exception:
            print(f"{R}✗ could not list server tools for tier {tier}{X}\n{proc.stderr[-800:]}")
            failures += 1
            continue

        print(f"\n{'═'*78}\n  LLM_ROUTER_SLIM={tier}   server registers {len(registered)} tools\n{'═'*78}")

        # "Every hint names a registered tool" is NECESSARY but NOT SUFFICIENT.
        # With a bogus name injected into CORE_TOOLS, the server simply cannot
        # register it, the tier silently shrinks 4 -> 3, and the fallback chain
        # degrades every llm_query hint to llm_code. Each hint still names a
        # registered tool, so this trace reported CLEAN while query prompts were
        # being routed to the CODE tool. Consistency preserved, correctness lost.
        #
        # So also assert the other direction: every tool the tier CLAIMS to offer
        # must actually be registered. A tool that vanishes between the constant
        # and the server is the exact seam this script exists to watch.
        try:
            from llm_router.tool_surface import registered_tools as _declared_for

            declared = _declared_for(tier)
        except Exception:
            declared = None
        if declared:
            missing = sorted(n for n in declared if n not in registered)
            if missing:
                failures += 1
                print(f"  {R}✗ tier declares {len(declared)} tools but the server "
                      f"registered {len(registered)}; missing: {', '.join(missing)}{X}")
                print(f"      {R}THIS IS THE BUG: the tier lost a tool between the "
                      f"constant and the server. Hints for it degrade silently to "
                      f"whatever the fallback chain offers.{X}")

        nonce = uuid.uuid4().hex[:6] if args.fresh else ""
        for i, (prompt, _kind) in enumerate(CASES):
            if nonce:
                prompt = f"{prompt} (variant {nonce})"
            directive = run_hook(prompt, tier, f"trace-{tier}-{i}")
            bare, task = extract_tool(directive)
            label = prompt[:44].ljust(44)

            if bare is None:
                print(f"  {Y}·{X} {label} {D}no tool named (suppressed/context-dependent){X}")
                continue

            shown = f'{bare}(task="{task}")' if task else bare
            ok_reg = bare in registered
            mark = f"{G}✓{X}" if ok_reg else f"{R}✗{X}"
            print(f"  {mark} {label} hint→ {shown:24} registered={ok_reg}")
            if not ok_reg:
                failures += 1
                print(f"      {R}THIS IS THE BUG: caller would get "
                      f"'No such tool available' and pay full price{X}")
                continue

            if args.live:
                ok, info = invoke(bare, task, prompt)
                model, _, cost_s = info.partition("|")
                try:
                    cost = float(cost_s)
                except ValueError:
                    cost = float("nan")
                # North star: the work landed on something cheaper than the host
                # model. Anything Claude-family means we did NOT save.
                on_host = any(k in model.lower() for k in ("claude", "anthropic", "opus", "sonnet"))
                cached = model.startswith("cache/")
                # Opus baseline for a prompt this size is ~$0.01+; treat <$0.005 as saved.
                cheap = (cost == cost) and cost < 0.005
                # "unknown" is NOT a pass: if the model can't be identified, the
                # north-star claim cannot be verified, so don't make it.
                known = model != "unknown"
                good = ok and known and not on_host and cheap
                m2 = f"{G}✓{X}" if good else (f"{Y}~{X}" if ok else f"{R}✗{X}")
                if not known:
                    verdict = "UNVERIFIED — could not identify the model"
                elif on_host:
                    verdict = "ran on the HOST model — no saving"
                elif cached:
                    verdict = "served from CACHE (free, but no live model ran)"
                elif cheap:
                    verdict = "NORTH STAR MET — cheap model did the work"
                else:
                    verdict = "completed, but not cheap"
                print(f"      {m2} invoked → {model} · ${cost:.6f} · {verdict}")
                if not ok or on_host or not known:
                    failures += 1

    print(f"\n{'─'*78}")
    if failures:
        print(f"{R}TRACE FAILED — {failures} stage failure(s){X}")
        return 1
    print(f"{G}TRACE CLEAN — every emitted hint names a tool the server registers{X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
