#!/usr/bin/env python3
"""Print the da31df7 `okf.find_relevant(limit=3)` false-positive baseline (PLAN v16 P1.5-a).

Loads `git show da31df7:src/llm_router/okf.py` as a temp module and runs the #233/#244
prompt sets from tests/okf/test_okf_ranked_budget.py over that test's store. The number
it prints is DA31DF7_BASELINE_FP in that file.

    PYTHONPATH=src python3 scripts/okf_fp_baseline.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "tests" / "okf"))
import test_okf_ranked_budget as t  # noqa: E402

src = subprocess.run(["git", "show", "da31df7:src/llm_router/okf.py"], cwd=repo,
                     capture_output=True, text=True, check=True).stdout
tmp = Path(tempfile.mkdtemp())
(tmp / "okf_da31df7.py").write_text(src, encoding="utf-8")
spec = importlib.util.spec_from_file_location("llm_router._okf_da31df7", tmp / "okf_da31df7.py")
old = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = old
spec.loader.exec_module(old)
base = t._fp_store(tmp / "s")
os.environ["LLM_ROUTER_PROJECT_ROOT"] = str(tmp / "s" / "repo-fp")
prompts = t._PROMPTS_233 + t._PROMPTS_244
total = 0
for p in prompts:
    old.invalidate_cache()
    total += len(old.find_relevant(p, limit=3, base=base))
print(f"da31df7 find_relevant(limit=3): FP docs={total} over n={len(prompts)} prompts")
