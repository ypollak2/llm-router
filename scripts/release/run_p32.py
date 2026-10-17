#!/usr/bin/env python3
"""PREREG-P32 harness: runs the four frozen release eval sets (PLAN v16 P3.2, D-192, D-210).

Model-free by design: no model call, no network, no private or prompt-derived data. Every
item is a committed fixture or comes from a seeded generator. Output is the eval-file
format of release_evals.py / eval_gate.py ({"schema", "sha", "sets": {id: {"items": [...]}}})
plus a top-level "provenance" object the gate ignores.

  set            kind         n    source / outcome
  rt_hand_60     performance  60   classifier_gate FIXTURES (HOOK_POLICY) + FLOOR_FIXTURES
                                   (ROUTER_POLICY); pass iff (task_type, complexity) match
  rt_agt_50      performance  50   tests/fixtures/agt/a1_prompts_50.jsonl; pass iff task_type
                                   matches (tier not scored, ROUTER_POLICY)
  fid_syn_60     fidelity     60   scripts/g1_synthetic_evidence.py generator (seed 20261014,
                                   window 8192); full/pack = the answer token is retained in
                                   the FULL / PACK context (model-free retention)
  stab_syn_500   stability    500  the 110 fixtures (106 unique texts) + 394 from the seeded
                                   template generator here; a/b = canonical-JSON decision from two separate
                                   processes (PYTHONHASHSEED 0 vs 1)

  python scripts/release/run_p32.py --dry-run                         counts only, scores nothing
  python scripts/release/run_p32.py --write-manifests release_evals/manifests
  python scripts/release/run_p32.py --role candidate --sha <40hex> --out release_evals/<sha>.json
  python scripts/release/run_p32.py --role baseline  --sha <v15.4.0 sha> --worktree /tmp/base \\
         --sets rt_hand_60,rt_agt_50 --out release_evals/baseline.json

Refusals (exit 1, nothing written): --sha not 40 lowercase hex; the --worktree HEAD is not
--sha; the imported llm_router package is not under --worktree; a baseline asked to run a
non-performance set.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from release_evals import SHA_RE, build_eval_file, make_manifest  # noqa: E402

# (kind, n) per set; min_n == n for every set (PREREG-P32 §4).
SETS = {"rt_hand_60": ("performance", 60), "rt_agt_50": ("performance", 50),
        "fid_syn_60": ("fidelity", 60), "stab_syn_500": ("stability", 500)}
BASELINE_TAG = "v15.4.0"
GATE_REL = "scripts/release/classifier_gate.py"
AGT_REL = "tests/fixtures/agt/a1_prompts_50.jsonl"
G1_REL = "scripts/g1_synthetic_evidence.py"
FID_PROVIDER = "anthropic"
STAB_SEED = 20261018
STAB_N = 500
STAB_HASHSEEDS = (0, 1)
# sha256 of the 500 stability texts (106 unique fixture texts + 394 generated), in order, joined
# with "\n": pinned in PREREG-P32 constants. 4 of the 110 fixtures repeat another fixture's text
# (the FLOOR_FIXTURES prompts also appear in FIXTURES, under another policy); stability decides
# every text under both policies, so a text is one item and the generator fills up to 500.
STAB_GEN_TEXTS_SHA256 = "018cb7ec93823c113db7c827fbffb43bf4c32570af8451e7aca66ca34b8c3951"
POLICY_OF = {"fixtures": "HOOK_POLICY", "floor": "ROUTER_POLICY"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── item loaders (pure; no llm_router import) ─────────────────────────────────

def _lit(node: ast.AST):
    """literal_eval plus str/int `+` and `*` (the gate has `"what does " + "x" * 420 + " mean?"`)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
        left, right = _lit(node.left), _lit(node.right)
        return left + right if isinstance(node.op, ast.Add) else left * right
    if isinstance(node, (ast.List, ast.Tuple)):
        vals = [_lit(e) for e in node.elts]
        return vals if isinstance(node, ast.List) else tuple(vals)
    return ast.literal_eval(node)


def parse_gate_lists(source: str) -> dict[str, tuple[list, str]]:
    """FIXTURES / FLOOR_FIXTURES from classifier_gate.py source, WITHOUT importing it (that
    file prepends its own src/ to sys.path, which would hijack a baseline run). Returns
    {name: (literal value, exact source text of the assignment value)}."""
    tree = ast.parse(source)
    out: dict[str, tuple[list, str]] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target in ("FIXTURES", "FLOOR_FIXTURES"):
            out[target] = (_lit(node.value), ast.get_source_segment(source, node.value) or "")
    missing = {"FIXTURES", "FLOOR_FIXTURES"} - set(out)
    if missing:
        raise ValueError(f"classifier_gate source lacks {sorted(missing)}")
    return out


def hand_items(fixtures: list, floor: list) -> list[dict]:
    """rt_hand_60 items; id = cg-<sha256(policy|prompt)[:12]>, never positional."""
    out = []
    for lst, key in ((fixtures, "fixtures"), (floor, "floor")):
        policy = POLICY_OF[key]
        for prompt, task, cx in lst:
            out.append({"id": f"cg-{_sha(policy + '|' + prompt)[:12]}", "text": prompt,
                        "policy": policy, "task_type": task, "complexity": cx})
    return out


def load_hand(root: Path = ROOT) -> list[dict]:
    lists = parse_gate_lists((Path(root) / GATE_REL).read_text())
    return hand_items(lists["FIXTURES"][0], lists["FLOOR_FIXTURES"][0])


def load_agt(root: Path = ROOT, path: Path | None = None) -> list[dict]:
    """rt_agt_50 items; id = a1-<i> with i the record's own "i" field (not its line number)."""
    out = []
    for line in (path or Path(root) / AGT_REL).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append({"id": f"a1-{r['i']}", "text": r["text"], "task_type": r["task_type"], "tier": r["tier"]})
    return out


def load_g1(root: Path = ROOT):
    path = Path(root) / G1_REL
    spec = importlib.util.spec_from_file_location("_p32_g1", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_p32_g1"] = mod  # dataclasses resolves cls.__module__ here
    spec.loader.exec_module(mod)
    return mod


def fid_items(root: Path = ROOT) -> list:
    """The g1 generator's items (reused, not reimplemented); id fid-<generator index>."""
    return load_g1(root).items()


def fid_id(item) -> str:
    return f"fid-{int(item.item_id.split('-')[1])}"


# ── stab_syn_500: seeded template generator (invented prompts only) ───────────

_TOPICS = ("token buckets", "lunar calendars", "sourdough starters", "bridge load tables", "tide charts",
           "cache invalidation", "paper folding", "chess endgames", "orchid watering", "ring buffers",
           "kite aerodynamics", "river deltas", "board game balance", "moss gardens", "queue backpressure",
           "ceramic glazes", "bicycle gearing", "compost cycles", "retry jitter", "map projections",
           "violin tuning", "lighthouse optics", "snow load", "shelf layout")
_OBJS = ("scheduler", "parser", "inventory sheet", "newsletter", "garden planner", "ledger", "state machine",
         "rate limiter", "recipe card", "timetable", "sensor logger", "pairing helper", "label printer",
         "crossword grid")
_TEAMS = ("operations", "design", "support", "finance", "field", "facilities")
_TEMPLATES = (
    ("query", "what is {t}?"), ("query", "how does {t} work in a {o}?"),
    ("query", "why would a {o} need {t}?"), ("query", "explain {t} to a new {team} hire"),
    ("code", "implement a {o} that handles {t}"), ("code", "fix the bug in the {o} where {t} fails"),
    ("code", "write a unit test for the {o} covering {t}"), ("code", "refactor the {o} module to use {t}"),
    ("generate", "write a short blog post about {t}"), ("generate", "draft an email to the {team} team about {t}"),
    ("generate", "write release notes for the {o}"), ("generate", "rewrite this paragraph about {t} to be shorter"),
    ("analyze", "analyze the tradeoffs of {t} versus {t2} for a {o}"),
    ("analyze", "review this design for {t} and list the risks"),
    ("research", "research the current state of {t} and summarise the sources"),
    ("research", "compare the available options for {t} for the {team} team"),
    ("image", "generate an image of a {o} next to {t}"), ("image", "draw a logo for a {t} shop"),
    ("lowsignal", "{t}"), ("lowsignal", "ok thanks, now {t}"), ("lowsignal", "continue"),
)
_MODS = ("", "", " quickly", " in detail", " step by step", ", comprehensive please", " (urgent)",
         "\n- keep it brief\n- no jargon", " café éè ✓", " PLEASE ADD A SUMMARY",
         " `def {o}_{t}(): pass`", "PAD")
_PAD = ("Background: the {team} team keeps a shared notebook about {t2}; every entry is dated and "
        "reviewed on Fridays, and nothing in it is confidential. ")


def _h(i: int, salt: str, mod: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{STAB_SEED}|{i}|{salt}".encode()).digest()[:8], "big") % mod


def stab_generated(exclude: set[str], count: int) -> list[str]:
    """`count` unique invented prompts from a pinned seed. Deterministic and independent of
    the random module and of PYTHONHASHSEED (choices come from sha256 of seed|index|salt)."""
    out: list[str] = []
    seen = set(exclude)
    i = 0
    while len(out) < count:
        flavor, tpl = _TEMPLATES[_h(i, "tpl", len(_TEMPLATES))]
        fields = {"t": _TOPICS[_h(i, "t", len(_TOPICS))], "t2": _TOPICS[_h(i, "t2", len(_TOPICS))],
                  "o": _OBJS[_h(i, "o", len(_OBJS))], "team": _TEAMS[_h(i, "team", len(_TEAMS))]}
        mod = _MODS[_h(i, "mod", len(_MODS))]
        text = tpl.format(**fields)
        if mod == "PAD":
            text = (_PAD.format(**fields) * (1 + _h(i, "rep", 9))) + text
        else:
            text += mod.format(**fields)
        i += 1
        if flavor == "lowsignal" and text == "continue" and "continue" in seen:
            continue
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def stab_items(root: Path = ROOT) -> list[dict]:
    """The 110 fixtures (rt_hand_60 + rt_agt_50 texts) plus 394 generated (500 in all); id st-<sha256(text)[:12]>."""
    texts: list[str] = []
    for it in load_hand(root) + load_agt(root):
        if it["text"] not in texts:
            texts.append(it["text"])
    texts += stab_generated(set(texts), STAB_N - len(texts))
    return [{"id": f"st-{_sha(t)[:12]}", "text": t} for t in texts]


def stab_texts_hash(items: list[dict]) -> str:
    return _sha("\n".join(i["text"] for i in items))


def load_set(set_id: str, root: Path = ROOT) -> list[dict]:
    if set_id == "rt_hand_60":
        return load_hand(root)
    if set_id == "rt_agt_50":
        return load_agt(root)
    if set_id == "fid_syn_60":
        return [{"id": fid_id(it), "category": it.category} for it in fid_items(root)]
    if set_id == "stab_syn_500":
        return stab_items(root)
    raise ValueError(f"unknown set {set_id!r}")


def item_ids(set_id: str, root: Path = ROOT) -> list[str]:
    return [i["id"] for i in load_set(set_id, root)]


# ── outcomes (§4.1-4.4) ───────────────────────────────────────────────────────

def _classify():
    from llm_router import classify
    return classify


def score_hand(items: list[dict]) -> list[dict]:
    c = _classify()
    out = []
    for it in items:
        s = c.classify_signals(it["text"], getattr(c, it["policy"]))
        out.append({"id": it["id"], "pass": bool(s.task_type.value == it["task_type"]
                                                   and s.complexity.value == it["complexity"])})
    return out


def score_agt(items: list[dict]) -> list[dict]:
    c = _classify()
    return [{"id": it["id"], "pass": bool(c.classify_signals(it["text"], c.ROUTER_POLICY).task_type.value
                                          == it["task_type"])} for it in items]


def retained(token: str, context: str) -> bool:
    """Model-free fidelity outcome: the answer token is present in the context (g1's normalisation)."""
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9-]", "", (t or "").lower())
    return norm(token) in norm(context)


def run_fid(root: Path = ROOT, indices: list[int] | None = None) -> list[dict]:
    """full/pack retention per item. Reuses g1's scratch_env, transcript writer, rolling-summary
    update (extractive: no model) and pack builder."""
    from llm_router import context_summary

    g1 = load_g1(root)
    chosen = g1.items()
    if indices is not None:
        chosen = [chosen[i] for i in indices]
    rows = []
    with g1.scratch_env() as scratch:
        tdir = scratch / "transcripts"
        tdir.mkdir()
        for it in chosen:
            path = g1.write_transcript(it, tdir)
            context_summary.maybe_update(it.item_id, path, model=None, free_pct=100.0)
            pack, _mode = g1.pack_context(it, path, FID_PROVIDER)
            rows.append({"id": fid_id(it), "full": bool(retained(it.token, g1.full_context(it))),
                         "pack": bool(retained(it.token, pack))})
    return rows


# ── stability: two separate processes ─────────────────────────────────────────

def decide_all(items: list[dict]) -> dict[str, str]:
    """Canonical-JSON decision per item under both policies."""
    c = _classify()
    out = {}
    for it in items:
        d = {}
        for name in ("HOOK_POLICY", "ROUTER_POLICY"):
            s = c.classify_signals(it["text"], getattr(c, name))
            d[name] = [s.task_type.value, s.complexity.value, bool(s.confident), s.score]
        out[it["id"]] = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return out


def _deny_network() -> list[str]:
    """Refuse every outbound connection in this process; returns the (live) violation list."""
    seen: list[str] = []

    def deny(name):
        def f(*a, **k):
            seen.append(name)
            raise OSError(f"network disabled for the P32 stability child ({name})")
        return f
    socket.socket.connect = deny("connect")
    socket.socket.connect_ex = deny("connect_ex")
    socket.getaddrinfo = deny("getaddrinfo")
    return seen


def child_main(items_path: str) -> int:
    """--stab-child: decide every item and print one JSON object. Runs under the env the
    parent built (PYTHONHASHSEED, memory decide cache, scratch HOME)."""
    violations = _deny_network()
    items = json.loads(Path(items_path).read_text())
    res = {"pid": os.getpid(), "hashseed": os.environ.get("PYTHONHASHSEED"),
           "decisions": decide_all(items)}
    res["net_violations"] = len(violations)
    sys.stdout.write(json.dumps(res))
    return 0


def spawn_stab_child(items: list[dict], hashseed: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="p32-stab-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "home").mkdir()
        (tmp_path / "lr").mkdir()
        (tmp_path / "items.json").write_text(json.dumps(items))
        env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_ROUTER_")}
        env.update({"PYTHONHASHSEED": str(hashseed), "LLM_ROUTER_DECIDE_CACHE": "memory",
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True", "HOME": str(tmp_path / "home"),
                    "LLM_ROUTER_HOME": str(tmp_path / "lr"), "OLLAMA_HOST": "http://127.0.0.1:9"})
        cmd = [sys.executable, str(Path(__file__).resolve()), "--stab-child", str(tmp_path / "items.json")]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"stability child (PYTHONHASHSEED={hashseed}) failed: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def run_stability(items: list[dict], spawn=spawn_stab_child) -> tuple[list[dict], dict]:
    """a = decision from the PYTHONHASHSEED=0 process, b = from the PYTHONHASHSEED=1 process."""
    res_a, res_b = spawn(items, STAB_HASHSEEDS[0]), spawn(items, STAB_HASHSEEDS[1])
    for res, seed in ((res_a, STAB_HASHSEEDS[0]), (res_b, STAB_HASHSEEDS[1])):
        if res.get("pid") == os.getpid():
            raise RuntimeError("stability run happened in the harness process: it must be a separate process")
        if str(res.get("hashseed")) != str(seed):
            raise RuntimeError(f"stability child ran with PYTHONHASHSEED={res.get('hashseed')!r}, expected {seed}")
        if res.get("net_violations"):
            raise RuntimeError("stability child attempted network access")
        if set(res.get("decisions", {})) != {i["id"] for i in items}:
            raise RuntimeError("stability child did not decide exactly the requested items")
    if res_a["pid"] == res_b["pid"]:
        raise RuntimeError("both stability runs share one process")
    rows = [{"id": i["id"], "a": res_a["decisions"][i["id"]], "b": res_b["decisions"][i["id"]]} for i in items]
    return rows, {"pids": [res_a["pid"], res_b["pid"]], "hashseeds": list(STAB_HASHSEEDS)}


# ── provenance and refusals (§5) ──────────────────────────────────────────────

def git_head(worktree: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def check_provenance(worktree: Path, sha: str, pkg_file: str) -> list[str]:
    """Reasons to refuse to run; empty means the package under test is the --sha checkout."""
    problems = []
    if not SHA_RE.match(str(sha)):
        problems.append(f"--sha must be 40 lowercase hex chars, got {sha!r}")
    head = git_head(worktree)
    if head != sha:
        problems.append(f"HEAD of {worktree} is {head!r}, not --sha {sha!r}")
    try:
        Path(pkg_file).resolve().relative_to(Path(worktree).resolve())
    except ValueError:
        problems.append(f"imported llm_router ({pkg_file}) is not under the worktree {worktree}")
    return problems


def fixtures_identity(old_source: str, new_source: str) -> dict:
    """Byte-identity of the FIXTURES / FLOOR_FIXTURES assignments between two versions of
    classifier_gate.py. Counts and a boolean only."""
    a, b = parse_gate_lists(old_source), parse_gate_lists(new_source)
    return {name: {"identical": a[name][1].encode() == b[name][1].encode(),
                   "n_old": len(a[name][0]), "n_new": len(b[name][0])} for name in sorted(a)}


def baseline_gate_source(root: Path, ref: str = BASELINE_TAG) -> str:
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{GATE_REL}"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git show {ref}:{GATE_REL} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def file_hashes(root: Path) -> dict[str, str]:
    files = [GATE_REL, AGT_REL, G1_REL, "scripts/release/run_p32.py"]
    return {f: hashlib.sha256((Path(root) / f).read_bytes()).hexdigest() for f in files}


# ── commands ──────────────────────────────────────────────────────────────────

def dry_run(root: Path, sets: list[str], baseline_ref: str) -> tuple[int, list[str]]:
    lines, ok = [], True
    for sid in sets:
        kind, n = SETS[sid]
        items = load_set(sid, root)
        ids = [i["id"] for i in items]
        good = len(ids) == n and len(set(ids)) == len(ids)
        ok &= good
        lines.append(f"{sid} [{kind}] n={len(ids)} expected={n} unique_ids={len(set(ids))} {'ok' if good else 'FAIL'}")
        if sid == "rt_hand_60":
            cells: dict[str, int] = {}
            for i in items:
                k = f"{i['policy']}:{i['task_type']}/{i['complexity']}"
                cells[k] = cells.get(k, 0) + 1
            lines.append(f"  label cells (policy:task/complexity) = {len(cells)}: "
                         + ", ".join(f"{k}={v}" for k, v in sorted(cells.items())))
        elif sid == "rt_agt_50":
            cells = {}
            for i in items:
                k = f"{i['task_type']}/{i['tier']}"
                cells[k] = cells.get(k, 0) + 1
            lines.append(f"  label cells (task_type/tier) = {len(cells)}: "
                         + ", ".join(f"{k}={v}" for k, v in sorted(cells.items())))
        elif sid == "fid_syn_60":
            cats: dict[str, int] = {}
            for i in items:
                cats[i["category"]] = cats.get(i["category"], 0) + 1
            lines.append("  categories: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
        elif sid == "stab_syn_500":
            gh = stab_texts_hash(items)
            pinned = gh == STAB_GEN_TEXTS_SHA256
            ok &= pinned
            n_fix = len({i["text"] for i in load_hand(root) + load_agt(root)})
            lines.append(f"  fixture_texts={n_fix} (of 110 fixtures) generated={len(items) - n_fix} "
                         f"texts_sha256={gh[:16]} matches_pin={pinned}")
    try:
        ident = fixtures_identity(baseline_gate_source(root, baseline_ref), (root / GATE_REL).read_text())
        same = all(v["identical"] for v in ident.values())
        ok &= same
        lines.append(f"classifier_gate FIXTURES vs {baseline_ref}: "
                     + "; ".join(f"{k} identical={v['identical']} n_old={v['n_old']} n_new={v['n_new']}"
                                 for k, v in ident.items()))
    except (RuntimeError, ValueError) as e:
        ok = False
        lines.append(f"classifier_gate FIXTURES vs {baseline_ref}: UNVERIFIED ({e})")
    lines.append(f"dry run: {'OK' if ok else 'FAILED'} (no item scored, no model called)")
    return (0 if ok else 1), lines


def write_manifests(root: Path, sets: list[str], out_dir: Path) -> list[str]:
    """Same bytes as `release_evals.py freeze` writes: indent=1 JSON, trailing newline, min_n = n."""
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for sid in sets:
        kind, n = SETS[sid]
        m = make_manifest(sid, kind, n, item_ids(sid, root))
        (out_dir / f"{sid}.json").write_text(json.dumps(m, indent=1) + "\n")
        lines.append(f"wrote {out_dir / (sid + '.json')} (n={m['n']}, min_n={m['min_n']}, sha256={m['items_sha256'][:12]})")
    return lines


def run(a: argparse.Namespace) -> int:
    root = ROOT
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    unknown = [s for s in sets if s not in SETS]
    if unknown:
        print(f"refusing to run: unknown set(s) {unknown}", file=sys.stderr)
        return 1
    if a.role == "baseline" and any(SETS[s][0] != "performance" for s in sets):
        print("refusing to run: a baseline holds the performance sets only (PREREG-P32 §5)", file=sys.stderr)
        return 1
    worktree = Path(a.worktree or subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                                  text=True, check=False).stdout.strip() or ".")
    import llm_router
    problems = check_provenance(worktree, a.sha, llm_router.__file__)
    if problems:
        print("refusing to run: " + "; ".join(problems), file=sys.stderr)
        return 1
    results: dict[str, list[dict]] = {}
    meta: dict = {}
    for sid in sets:
        items = load_set(sid, root)
        if sid == "rt_hand_60":
            results[sid] = score_hand(items)
        elif sid == "rt_agt_50":
            results[sid] = score_agt(items)
        elif sid == "fid_syn_60":
            results[sid] = run_fid(root)
        else:
            results[sid], meta["stability"] = run_stability(items)
    doc = build_eval_file(a.sha, results, {s: SETS[s][0] for s in sets})
    doc["provenance"] = {"role": a.role, "llm_router_file": llm_router.__file__, "git_sha": git_head(worktree),
                         "worktree": str(worktree), "item_file_sha256": file_hashes(root),
                         "python": sys.version.split()[0], **meta}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out} ({', '.join(f'{s}: n={len(results[s])}' for s in sets)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", default=",".join(SETS))
    ap.add_argument("--role", choices=("baseline", "candidate"))
    ap.add_argument("--sha")
    ap.add_argument("--out")
    ap.add_argument("--worktree", help="checkout under test (default: git toplevel of the cwd)")
    ap.add_argument("--dry-run", action="store_true", help="counts only; scores nothing")
    ap.add_argument("--baseline-ref", default=BASELINE_TAG)
    ap.add_argument("--write-manifests", metavar="DIR", help="write one freeze-compatible manifest per set")
    ap.add_argument("--stab-child", metavar="ITEMS_JSON", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.stab_child:
        return child_main(a.stab_child)
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    if a.dry_run or a.write_manifests:
        if any(s not in SETS for s in sets):
            print("unknown set in --sets", file=sys.stderr)
            return 1
        code, lines = 0, []
        if a.write_manifests:
            lines += write_manifests(ROOT, sets, Path(a.write_manifests))
        if a.dry_run:
            code, more = dry_run(ROOT, sets, a.baseline_ref)
            lines += more
        print("\n".join(lines))
        return code
    if not (a.role and a.sha and a.out):
        ap.error("--role, --sha and --out are required (or use --dry-run / --write-manifests)")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
