"""Does a search/replace BLOCK beat JSON arguments for the edit payload?

Aider measured text edit formats beating function calls, but that is their
models on their task. This asks our model, on our repo, with mechanical scoring:
apply the edit, then check the file content. No judge.

Two formats, same edits, same model:
  A. JSON arguments   {"tool":"edit_file","arguments":{"path","old_string","new_string"}}
  B. Fenced block     path + <<<<<<< SEARCH / ======= / >>>>>>> REPLACE

Scored: did the file end up with exactly the intended content?
"""
import json, re, shutil, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, "/Users/yaliandrona/Projects/llm-router/src")
from llm_router.hooks.agent_loop import TOOL_DEFINITIONS

MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3-coder:30b"
WORK = Path("/Users/yaliandrona/.claude/jobs/82d6664b/tmp/editbench")

CASES = [
    ("version.txt", 'version = "1.0.0"\nname = "widget"\n',
     "Change the version from 1.0.0 to 2.0.0.",
     'version = "2.0.0"\nname = "widget"\n'),
    ("conf.py", "DEBUG = True\nTIMEOUT = 30\nRETRIES = 3\n",
     "Set TIMEOUT to 60.",
     "DEBUG = True\nTIMEOUT = 60\nRETRIES = 3\n"),
    ("calc.py", "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
     "The add function is wrong — it subtracts. Fix it to add.",
     "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"),
    ("greet.py", "def greet(name):\n    print('hi ' + name)\n",
     "Use an f-string instead of concatenation, keeping the same output.",
     None),  # checked by predicate
    ("app.py", "import os\nimport sys\n\n\ndef main():\n    return 0\n",
     "Add `import json` after the `import sys` line.",
     "import os\nimport sys\nimport json\n\n\ndef main():\n    return 0\n"),
]

JSON_SYS = ("You edit code. Reply with ONE JSON object and nothing else:\n"
            '{"path": "<file>", "old_string": "<exact text to replace>", '
            '"new_string": "<replacement>"}\n'
            "old_string must match the file EXACTLY and appear exactly once.")

BLOCK_SYS = ("You edit code. Reply with a search/replace block and nothing else:\n"
             "<path>\n```\n<<<<<<< SEARCH\n<exact lines to replace>\n=======\n"
             "<replacement lines>\n>>>>>>> REPLACE\n```\n"
             "The SEARCH section must match the file EXACTLY.")

BLOCK_RE = re.compile(
    r"<<<<<<<\s*SEARCH\s*\n(.*?)\n?=======\s*\n(.*?)\n?>>>>>>>\s*REPLACE", re.S)


def ask(system, user, fmt=None):
    body = {"model": MODEL, "stream": False, "think": False,
            "options": {"temperature": 0.1},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if fmt: body["format"] = fmt
    req = urllib.request.Request("http://localhost:11434/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("message", {}).get("content", "")


def apply_json(reply, path: Path) -> bool:
    try:
        obj = json.loads(reply)
        old, new = obj["old_string"], obj["new_string"]
    except Exception:
        return False
    text = path.read_text()
    if old not in text or text.count(old) != 1:
        return False
    path.write_text(text.replace(old, new, 1))
    return True


def apply_block(reply, path: Path) -> bool:
    m = BLOCK_RE.search(reply or "")
    if not m:
        return False
    old, new = m.group(1), m.group(2)
    text = path.read_text()
    if old not in text:                      # try line-stripped match
        old2 = "\n".join(l.rstrip() for l in old.splitlines())
        text2 = "\n".join(l.rstrip() for l in text.splitlines())
        if old2 not in text2:
            return False
        path.write_text(text2.replace(old2, "\n".join(l.rstrip() for l in new.splitlines()), 1) + "\n")
        return True
    path.write_text(text.replace(old, new, 1))
    return True


JSON_SCHEMA = {"type": "object",
               "properties": {"path": {"type": "string"},
                              "old_string": {"type": "string"},
                              "new_string": {"type": "string"}},
               "required": ["path", "old_string", "new_string"]}

rows = []
for name, before, instruction, expected in CASES:
    for label, system, applier, fmt in (
            ("json", JSON_SYS, apply_json, JSON_SCHEMA),
            ("block", BLOCK_SYS, apply_block, None)):
        shutil.rmtree(WORK, ignore_errors=True); WORK.mkdir(parents=True)
        f = WORK / name; f.write_text(before)
        user = f"File `{name}`:\n```\n{before}```\n\n{instruction}"
        t0 = time.time()
        try:
            reply = ask(system, user, fmt)
            applied = applier(reply, f)
        except Exception as e:
            reply, applied = f"<{type(e).__name__}>", False
        got = f.read_text()
        if expected is None:
            ok = applied and "f'" in got or 'f"' in got
            ok = ok and "+" not in got.split("print")[-1]
        else:
            ok = applied and got == expected
        rows.append({"case": name, "fmt": label, "ok": bool(ok),
                     "applied": applied, "s": round(time.time()-t0, 1)})
        print(f"{name:12s} {label:6s} applied={'Y' if applied else 'n'} "
              f"correct={'Y' if ok else 'n'} {rows[-1]['s']:5.1f}s", flush=True)

print()
for label in ("json", "block"):
    sub = [r for r in rows if r["fmt"] == label]
    print(f"{label:6s} parsed+applied {sum(r['applied'] for r in sub)}/{len(sub)}"
          f"   correct {sum(r['ok'] for r in sub)}/{len(sub)}"
          f"   mean {sum(r['s'] for r in sub)/len(sub):.1f}s")
json.dump(rows, open("/Users/yaliandrona/.claude/jobs/82d6664b/tmp/editfmt.json","w"), indent=2)
