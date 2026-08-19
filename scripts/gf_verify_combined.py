"""Verify the COMBINED kill coverage of several test files against one function.

verify_kills.py takes a single file, which understates coverage when two files target
the same function: C6 closed the accumulator class, this one closed the untouched
branches, and neither number alone describes what the suite now catches.
"""
import collections, json, os, pathlib, shutil, subprocess, sys
REPO = pathlib.Path.home()/"Projects/LLM Router"; MUT = REPO/"mutants"; PY = str(REPO/".venv/bin/python")

#: The survivor inventory produced by `gf_kinds.py`. It is a working artefact, not a
#: committed one, so its location is configurable rather than assumed next to this file
#: — the first version resolved it relative to __file__ and broke the moment the script
#: was moved from the scratchpad into scripts/.
KINDS = pathlib.Path(
    os.environ.get("LLM_ROUTER_GF_KINDS")
    or pathlib.Path(__file__).parent / "gf_kinds.json"
)
if not KINDS.exists():
    sys.exit(
        f"survivor inventory not found at {KINDS}\n"
        f"Generate it with `python scripts/gf_kinds.py`, or point LLM_ROUTER_GF_KINDS at it."
    )

module, function = sys.argv[1], sys.argv[2]
tests = sys.argv[3:]
for t in tests:
    shutil.copy2(REPO/t, MUT/t)
rows = json.loads(KINDS.read_text())
targets = [r for r in rows if r["module"] == module and r["function"] == function]

def run(m: str) -> int:
    p = subprocess.run([PY, "-m", "pytest", *tests, "--tb=no", "-p", "no:cacheprovider"],
                       cwd=MUT, env=dict(os.environ, MUTANT_UNDER_TEST=m),
                       capture_output=True, text=True, timeout=300)
    return p.returncode

if run("") != 0:
    print("CONTROL FAILED — nothing below is attributable")
    raise SystemExit(1)
print("control passes\n", flush=True)
killed, surv = [], []
for r in targets:
    (killed if run(r["name"]) == 1 else surv).append(r)
print(f"COMBINED KILLED {len(killed)} of {len(targets)} ({len(killed)/len(targets):.0%})")
print("\nstill surviving, by kind:")
for k, v in collections.Counter(r["kind"] for r in surv).most_common():
    print(f"   {k:18} {v}")
print("\nsurviving lines:")
for line, n in collections.Counter(r["before"].strip()[:68] for r in surv).most_common(10):
    print(f"   {n}x {line}")
print("COMBINED_DONE")
