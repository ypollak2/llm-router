"""What did Claude actually DO here in the last 5 days?

Every tool call from every transcript, categorised by what kind of work it is,
so the question "which of these could a local model do" is answered from the
real distribution rather than from imagination.
"""
import json, glob, os, re, time, collections
from pathlib import Path

CUTOFF = time.time() - 5 * 86400
files = [f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
         if os.path.getmtime(f) >= CUTOFF]

tools = collections.Counter()
bash_verbs = collections.Counter()
result_chars = collections.Counter()
pending = {}
examples = collections.defaultdict(list)

# What the command actually does, ignoring where it does it.
def verb(cmd: str) -> str:
    c = cmd.strip()
    for _ in range(4):
        m = re.match(r"^(?:cd|pushd|env|time|nohup|exec)\b[^\n;&|]*(?:&&|;|\n)\s*", c)
        if not m: break
        c = c[m.end():].strip()
    c = c.lstrip("(")
    m = re.match(r"([A-Za-z0-9_./-]+)", c)
    prog = (m.group(1).split("/")[-1] if m else "?")
    rest = c[len(m.group(1)):].strip() if m else ""
    sub = re.match(r"([a-z-]+)", rest)
    if prog in ("git", "gh", "npm", "cargo", "uv", "pip", "docker"):
        return f"{prog} {sub.group(1) if sub else ''}".strip()
    if prog in ("python", "python3") and "-m" in rest:
        m2 = re.search(r"-m\s+([A-Za-z0-9_.]+)", rest)
        return f"python -m {m2.group(1)}" if m2 else "python"
    return prog

for f in files:
    for line in open(f, errors="ignore"):
        try: rec = json.loads(line)
        except Exception: continue
        msg = rec.get("message") or {}
        for c in (msg.get("content") or []):
            if not isinstance(c, dict): continue
            if c.get("type") == "tool_use":
                name = c.get("name", "?")
                tools[name] += 1
                inp = c.get("input") or {}
                if name == "Bash":
                    v = verb(inp.get("command", ""))
                    bash_verbs[v] += 1
                    if len(examples[v]) < 2:
                        examples[v].append(inp.get("command", "")[:70].replace("\n", " ; "))
                pending[c.get("id")] = name
            elif c.get("type") == "tool_result":
                name = pending.pop(c.get("tool_use_id"), None)
                if not name: continue
                body = c.get("content")
                text = (json.dumps(body) if not isinstance(body, str) else body) or ""
                result_chars[name] += len(text)

total = sum(tools.values())
print(f"=== {len(files)} transcripts, last 5 days — {total:,} tool calls ===\n")
print(f"{'tool':26s}{'calls':>7s}{'share':>8s}{'result tokens':>15s}")
for name, n in tools.most_common(12):
    print(f"{name:26s}{n:>7d}{n/total:>8.1%}{result_chars[name]//4:>15,}")
print(f"\ntotal result payload: {sum(result_chars.values())//4:,} tokens")

print(f"\n=== Bash broken down by what the command DOES ({sum(bash_verbs.values()):,}) ===")
for v, n in bash_verbs.most_common(22):
    ex = examples[v][0] if examples[v] else ""
    print(f"  {v:20s}{n:>6d}{n/sum(bash_verbs.values()):>7.1%}   {ex[:56]}")

json.dump({"tools": dict(tools), "bash": dict(bash_verbs),
           "result_chars": dict(result_chars)},
          open("/Users/yaliandrona/.claude/jobs/82d6664b/tmp/ops.json", "w"), indent=2)
