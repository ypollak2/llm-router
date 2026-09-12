"""What would the revived compression hook have saved on REAL sessions?

Replays actual Bash tool results from Claude Code transcripts through the same
RTK compressor the PostToolUse hook uses. Nothing simulated: these are commands
that really ran and outputs that really entered a context window.
"""
import json, glob, os, sys
sys.path.insert(0, "/Users/yaliandrona/Projects/llm-router/src")
from llm_router.compression.rtk_adapter import RTKAdapter

adapter = RTKAdapter(enable=True)
files = sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
               key=os.path.getmtime, reverse=True)[:6]

pending = {}          # tool_use_id -> command
orig = comp = 0
n = skipped = 0
by_strategy = {}

for f in files:
    for line in open(f, errors="ignore"):
        try: rec = json.loads(line)
        except Exception: continue
        msg = rec.get("message") or {}
        for c in (msg.get("content") or []):
            if not isinstance(c, dict): continue
            if c.get("type") == "tool_use" and c.get("name") == "Bash":
                pending[c.get("id")] = (c.get("input") or {}).get("command", "")
            elif c.get("type") == "tool_result":
                cmd = pending.pop(c.get("tool_use_id"), None)
                if cmd is None: continue
                body = c.get("content")
                if isinstance(body, list):
                    text = "\n".join(b.get("text", "") for b in body
                                     if isinstance(b, dict))
                elif isinstance(body, str):
                    text = body
                else:
                    continue
                if not text or text.count("\n") < 4:
                    skipped += 1
                    continue
                try:
                    # .compress returns an OBJECT with attributes, not a dict.
                    # Calling .get() on it fell through to str(res), which
                    # stringified the whole object and measured compression as
                    # NEGATIVE 7.3% — the instrument, not the compressor.
                    res = adapter.compress(cmd, text)
                    out, strat = res.output, res.strategy
                except Exception:
                    continue
                n += 1
                orig += len(text); comp += len(out)
                s = by_strategy.setdefault(strat, [0, 0, 0])
                s[0] += 1; s[1] += len(text); s[2] += len(out)

print(f"{n} real Bash outputs compressed ({skipped} too short to bother)\n")
print(f"  before : {orig:>10,} chars  ≈ {orig//4:>8,} tokens")
print(f"  after  : {comp:>10,} chars  ≈ {comp//4:>8,} tokens")
if orig:
    print(f"  saved  : {orig-comp:>10,} chars  ≈ {(orig-comp)//4:>8,} tokens"
          f"   ({(1-comp/orig):.1%})")
print("\ntop strategies by tokens saved:")
for strat, (cnt, o, c) in sorted(by_strategy.items(),
                                 key=lambda kv: kv[1][1]-kv[1][2], reverse=True)[:8]:
    print(f"  {strat:18s} {cnt:4d} calls  {(o-c)//4:>7,} tokens saved"
          f"  ({(1-c/o) if o else 0:>5.1%})")
