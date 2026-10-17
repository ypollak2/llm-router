#!/usr/bin/env python3
"""CLS-v16 synthetic generator runner (build B-L): CLS-SYN-GENERATOR-SPEC sections 1-8 and 12.

  alloc | run | collect | shingles | write-gen-file | freeze-spec | declare-resemblance-failed |
  regen | discard | test-syn-step

The request body is `template(cell vectors, nonces)` and nothing else: the generator never receives real
text. This process opens no DEV file. Real-DEV dedup uses a hashed-shingle file the coordinator supplies.
Generator files are versioned by name (tmpl_gpt.v<N>.txt, ...): a pinned name is never rewritten.
Output: counts and hashes only; the items file (text) is 0600 under --out-root.

Template contract: a template file holds the Appendix C body with the marker {{ITEMS}} where the item
blocks go (the spec author supplies the files; this tool does not invent their wording).

Calls: every GPT call passes `-m <gen_gpt pin>` (D-147); a codex non-zero rc (bad id, timeout, CLI error) STOPS
and parks the run (exit 24, resumable). Opus calls must report an opus model id (else exit 22). Every call writes
a `pending` row to gen_calls.jsonl (0600, under --out-root) before the child starts and a `done` row (with the
raw answer text) after it; Opus calls also write pending/final rows to the spend log. That log is the SAME file as
the labelling spend log (default $CLS/spend_log.jsonl), so generator Opus spend counts toward the D-134 $20 cap
(conservative: the cap is shared, not doubled).
Resume: `run --resume` (and `test-syn-step --resume`) continues an incomplete set in the same out-root with the
same allocation, call assignment and nonces; finished calls are replayed from gen_calls.jsonl, never re-sent.
TEST-SYN is marked generated in custody.json only when a set completes.
Regeneration nonces (spec 8.3 "no seed hunting beyond the next call-index nonces"): the one TEST-SYN regen keeps
the frozen allocation (the same cell vectors) and offsets BOTH the call indexes and the item nonce indexes by
1000 (REGEN_INDEX_BASE), beyond any index the first set used (items 0..n-1, replacements n..1.2n), so no regen
request is textually identical to a discarded-set request. Item ids keep the item index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_pins as P  # noqa: E402
import cls_resemble as R  # noqa: E402
import cls_send_lib as S  # noqa: E402

VERIFY_PINS = P.verify_at_start

TASKS = ["T%02d" % i for i in range(1, 13)]
TASK_MEANING = {
    "T01": "bug fix with a described symptom", "T02": "new feature or small addition",
    "T03": "refactor or rename", "T04": "write or fix tests",
    "T05": "debug a failure (error, test output, stack trace described)", "T06": "review a diff or PR",
    "T07": "explain code or a concept (Q&A)", "T08": "configuration, CI, build, dependency, devops",
    "T09": "git, branch, release, PR housekeeping", "T10": "research or compare options (may need the web)",
    "T11": "docs, comments, commit text, changelog", "T12": "data, analysis, measurement, prereg or plan editing"}
TOOLS = ["N0", "N1", "N2", "N3", "N4"]
TOOL_WEIGHTS = {"N1": 30, "N2": 30, "N3": 20, "N0": 12, "N4": 8}
TOOL_MEANING = {"N0": "none: answerable from general knowledge", "N1": "read-only repo: needs to read files or search the repo",
                "N2": "edit: needs to change files", "N3": "run: needs to execute commands or tests",
                "N4": "web: needs outside information"}
BANDS = ["L1", "L2", "L3", "L4"]
BAND_BOUNDS = {"L1": (1, 12), "L2": (13, 50), "L3": (51, 150), "L4": (151, None)}
DIFFICULTY = ["haiku", "sonnet", "opus"]
DIFF_WEIGHTS = [20, 40, 40]
NULL_PROJECT_SHARE = 0.15
ACK_SHARE = 0.10
ITEMS_PER_CALL = 5
REPLACEMENT_CAP = 0.20
SHORTFALL_FLOOR = 360
MAX_SPEC_VERSION = 3
MAX_REGEN = 1
REGEN_INDEX_BASE = 1000
BAND_MISS_TOLERANCE = 0.25
SET_CFG = {"dev_syn": {"n": 300, "seed": 20261017, "ns": "devsyn", "calls": 60},
           "test_syn": {"n": 400, "seed": 20261018, "ns": "testsyn", "calls": 80}}
GPT_SHARE = 0.7
DIGEST_KEYS = ["project", "turn", "files_touched", "last_tool_outcome", "branch_state", "referent"]
DIGEST_ENUMS = {"turn": {"first", "follow_up", "plan_approval", "after_error", "after_test_run"},
                "last_tool_outcome": {"none", "ok", "error", "test_fail"},
                "branch_state": {"clean", "dirty", "pr_open"}}
ITEMS_MARKER = "{{ITEMS}}"
GEN_FILE_STEMS = ["tmpl_gpt", "tmpl_opus", "task_tool_mask", "projects", "intent_phrases", "meta_patterns"]
GEN_FILE_RE = re.compile(r"^(tmpl_gpt|tmpl_opus|task_tool_mask|projects|intent_phrases|meta_patterns)\.v(\d+)\.(txt|json)$")
PROVIDER_GPT_FAMILY = "gpt"


def gen_file_names(version: int) -> dict:
    return {"tmpl_gpt": "tmpl_gpt.v%d.txt" % version, "tmpl_opus": "tmpl_opus.v%d.txt" % version,
            "task_tool_mask": "task_tool_mask.v%d.json" % version, "projects": "projects.v%d.json" % version,
            "intent_phrases": "intent_phrases.v%d.json" % version, "meta_patterns": "meta_patterns.v%d.json" % version}


# ---------------------------------------------------------------------------------------------
# Pins, versions, generator files
# ---------------------------------------------------------------------------------------------

def _static_doc(a):
    p = getattr(a, "pins_static", None)
    return json.loads(Path(p).read_text(encoding="utf-8")) if p and Path(p).exists() else None


def is_pinned(path, static_doc, run_path) -> bool:
    """A file is pinned when its basename is a STATIC generator file with an equal sha, or a PINS-RUN entry
    lists its resolved path with an equal sha."""
    p = Path(path).resolve()
    if not p.is_file():
        return False
    sha = S.sha256_file(p)
    if static_doc and static_doc.get("pins", {}).get("generator_files", {}).get(p.name) == sha:
        return True
    for e in P.read_run(run_path):
        if e.get("files", {}).get(str(p)) == sha:
            return True
    return False


def require_pinned(path, static_doc, run_path) -> None:
    if not is_pinned(path, static_doc, run_path):
        raise S.CLSExit(S.EXIT_GEN_FILE, "file has no matching pin: %s" % Path(path).name)


def write_gen_file(gen_dir, name: str, content: str, static_doc, run_path) -> Path:
    """Write a NEW-named generator file. A name already pinned (STATIC or any RUN entry) or already on disk
    is refused (exit 20): v1 files are never edited."""
    if not GEN_FILE_RE.match(name):
        raise S.CLSExit(S.EXIT_GEN_FILE, "not a versioned generator file name")
    if name in P.pinned_basenames(static_doc, run_path) or (Path(gen_dir) / name).exists():
        raise S.CLSExit(S.EXIT_GEN_FILE, "generator file name already exists or is pinned: %s" % name)
    p = Path(gen_dir) / name
    S.write_private(p, content, 0o644)
    return p


def check_version(version: int) -> None:
    if version not in range(1, MAX_SPEC_VERSION + 1):
        raise S.CLSExit(S.EXIT_SPEC_VERSION, "spec version %d is outside the allowed 1..%d" % (version, MAX_SPEC_VERSION))


def load_gen_files(gen_dir, version: int, static_doc, run_path) -> dict:
    """Read the versioned generator files; every one must carry a matching pin (exit 20)."""
    names = gen_file_names(version)
    out = {}
    for key, fn in names.items():
        p = Path(gen_dir) / fn
        if not p.is_file():
            raise S.CLSExit(S.EXIT_GEN_FILE, "generator file missing: %s" % fn)
        require_pinned(p, static_doc, run_path)
        out[key] = p.read_text(encoding="utf-8") if fn.endswith(".txt") else json.loads(p.read_text(encoding="utf-8"))
        out[key + "_sha"] = S.sha256_file(p)
    for key in ("tmpl_gpt", "tmpl_opus"):
        if ITEMS_MARKER not in out[key]:
            raise S.CLSExit(S.EXIT_REFUSE, "template lacks the %s marker" % ITEMS_MARKER)
    out["meta"] = [re.compile(p, re.IGNORECASE) for p in out["meta_patterns"]]
    out["meta_src"] = list(out["meta_patterns"])
    return out


# ---------------------------------------------------------------------------------------------
# Length shares (aggregate counts only; no fallback)
# ---------------------------------------------------------------------------------------------

def load_length_shares(counts_path) -> dict:
    """Shares = real DEV length-band counts rounded to the nearest 5 points. No counts file: exit 15 and
    nothing is built. The file may hold the four aggregate counts and nothing else."""
    if not counts_path or not Path(counts_path).is_file():
        raise S.CLSExit(S.EXIT_NO_COUNTS, "no --length-counts file: generation does not start")
    doc = json.loads(Path(counts_path).read_text(encoding="utf-8"))
    for wrap in ("bands", "counts"):
        if isinstance(doc.get(wrap), dict):
            doc = doc[wrap]
    if sorted(doc) != BANDS or not all(isinstance(v, int) and v >= 0 for v in doc.values()):
        raise S.CLSExit(S.EXIT_REFUSE, "length counts must be exactly the four aggregate band counts")
    total = sum(doc.values())
    if total <= 0:
        raise S.CLSExit(S.EXIT_REFUSE, "length counts are empty")
    shares = {b: int(round(doc[b] * 100.0 / total / 5.0)) * 5 for b in BANDS}
    diff = 100 - sum(shares.values())
    if diff:
        big = max(BANDS, key=lambda b: (doc[b], b))
        shares[big] += diff
    return shares


# ---------------------------------------------------------------------------------------------
# Allocation (spec section 3)
# ---------------------------------------------------------------------------------------------

def _weighted(rng: random.Random, keys, weights):
    return rng.choices(list(keys), weights=list(weights), k=1)[0]


def _nearest_tool(task, tool, mask):
    allowed = [i for i, v in enumerate(mask[task]) if v]
    ti = TOOLS.index(tool)
    return TOOLS[min(allowed, key=lambda i: (abs(i - ti), i))]


def band_quotas(n: int, shares: dict) -> dict:
    raw = {b: shares[b] * n / 100.0 for b in BANDS}
    q = {b: int(raw[b]) for b in BANDS}
    for b in sorted(BANDS, key=lambda b: (-(raw[b] - q[b]), b))[: n - sum(q.values())]:
        q[b] += 1
    return q


def allocate(n: int, seed: int, shares: dict, mask: dict, projects: list) -> list[dict]:
    """Independent marginals from random.Random(seed) in item order; mask by rejection (cap 50 draws, then
    the nearest valid cell); then rebalance so every task type has n/12 +-2 items and every length band is
    within 2 items of its quota. (The spec words the rebalance as swaps; a swap cannot change a count, so
    an over-quota item is reassigned to the most under-quota stratum, last item first, mask re-applied.)"""
    rng = random.Random(seed)
    cells = []
    for k in range(n):
        task = rng.choice(TASKS)
        band = _weighted(rng, BANDS, [shares[b] for b in BANDS])
        tool = _weighted(rng, TOOLS, [TOOL_WEIGHTS[t] for t in TOOLS])
        for _ in range(50):
            if mask[task][TOOLS.index(tool)]:
                break
            task = rng.choice(TASKS)
            tool = _weighted(rng, TOOLS, [TOOL_WEIGHTS[t] for t in TOOLS])
        else:
            tool = _nearest_tool(task, tool, mask)
        proj = None if rng.random() < NULL_PROJECT_SHARE else rng.choice(projects)
        diff = _weighted(rng, DIFFICULTY, DIFF_WEIGHTS)
        ack = rng.random() < ACK_SHARE
        cells.append({"item_index": k, "task": task, "band": band, "tool": tool, "project": proj,
                      "difficulty": diff, "ack": ack})
    lo, hi = int(-(-(n / 12.0 - 2) // 1)), int((n / 12.0 + 2) // 1)
    for _ in range(20 * n):
        cnt = {t: 0 for t in TASKS}
        for c in cells:
            cnt[c["task"]] += 1
        over = max(TASKS, key=lambda t: (cnt[t], t))
        under = min(TASKS, key=lambda t: (cnt[t], t))
        if cnt[over] <= hi and cnt[under] >= lo:
            break
        victim = next(c for c in reversed(cells) if c["task"] == over)
        victim["task"] = under
        if not mask[under][TOOLS.index(victim["tool"])]:
            victim["tool"] = _nearest_tool(under, victim["tool"], mask)
    quota = band_quotas(n, shares)
    for _ in range(20 * n):
        cnt = {b: 0 for b in BANDS}
        for c in cells:
            cnt[c["band"]] += 1
        dev = {b: cnt[b] - quota[b] for b in BANDS}
        over = max(BANDS, key=lambda b: (dev[b], b))
        under = min(BANDS, key=lambda b: (dev[b], b))
        if dev[over] <= 2 and dev[under] >= -2:
            break
        next(c for c in reversed(cells) if c["band"] == over)["band"] = under
    return cells


def call_order(seed: int, n_calls: int, base: int = 0) -> list[int]:
    """Call indexes ordered by sha256("clssyn:call:" + seed + ":" + index)."""
    idx = list(range(base, base + n_calls))
    return sorted(idx, key=lambda i: hashlib.sha256(("clssyn:call:%d:%d" % (seed, i)).encode()).hexdigest())


def call_families(seed: int, n_calls: int, base: int = 0) -> dict:
    order = call_order(seed, n_calls, base)
    n_gpt = int(round(GPT_SHARE * n_calls))
    return {i: ("gpt" if pos < n_gpt else "opus") for pos, i in enumerate(order)}


def nonce(namespace: str, item_index: int) -> str:
    return hashlib.sha256(("%s:%d" % (namespace, item_index)).encode()).hexdigest()[:12]


def assign_to_calls(cells: list[dict], n_calls: int) -> list[list[dict]]:
    """Item k goes to call k % n_calls (5 items per call); a call never holds two identical cells."""
    calls = [[] for _ in range(n_calls)]
    for c in cells:
        calls[c["item_index"] % n_calls].append(c)

    def key(c):
        return (c["task"], c["band"], c["tool"], c["project"], c["difficulty"], c["ack"])

    for ci, grp in enumerate(calls):
        seen = set()
        for pos, c in enumerate(grp):
            if key(c) in seen:
                other = calls[(ci + 1) % n_calls]
                grp[pos], other[0] = other[0], grp[pos]
            seen.add(key(c))
    return calls


def alloc_sha(cells: list[dict]) -> str:
    return S.sha256_bytes(json.dumps(cells, sort_keys=True, separators=(",", ":")).encode())


# ---------------------------------------------------------------------------------------------
# Request: template(cell vectors, nonces) and nothing else
# ---------------------------------------------------------------------------------------------

def band_text(band: str) -> str:
    lo, hi = BAND_BOUNDS[band]
    return "%d-%d" % (lo, hi) if hi else "more than %d" % (lo - 1)


def build_request(template: str, cells: list, nonces: list) -> str:
    """Pure function of the template, the cell vectors and the nonces. It takes no other argument, so no
    real-derived text can reach a request body."""
    blocks = []
    for pos, (c, nn) in enumerate(zip(cells, nonces), start=1):
        blocks.append("  idx: %d\n  task: %s\n  length: %s words\n  tool need: %s\n  project: %s\n"
                      "  situation: %s\n  follow-up: %s\n  nonce: %s\n" % (
                          pos, TASK_MEANING[c["task"]], band_text(c["band"]), TOOL_MEANING[c["tool"]],
                          c["project"] or "none", c["situation"],
                          "yes (the message refers to something in the digest)" if c["ack"] else "no", nn))
    return template.replace(ITEMS_MARKER, "\n".join(blocks))


# ---------------------------------------------------------------------------------------------
# Acceptance filters (spec section 6)
# ---------------------------------------------------------------------------------------------

def parse_generation(text: str) -> dict:
    """idx -> {prompt, digest} from JSON lines; non-JSON lines are ignored."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "idx" in obj:
            try:
                out[int(obj["idx"])] = obj
            except (TypeError, ValueError):
                continue
    return out


def digest_problem(d) -> str | None:
    if not isinstance(d, dict) or sorted(d) != sorted(DIGEST_KEYS):
        return "digest_schema"
    for k, enum in DIGEST_ENUMS.items():
        if d.get(k) not in enum:
            return "digest_schema"
    ft = d.get("files_touched")
    if isinstance(ft, bool) or not isinstance(ft, int) or not 0 <= ft <= 6:
        return "digest_schema"
    if d.get("project") is not None and not isinstance(d["project"], str):
        return "digest_schema"
    if d.get("referent") is not None and not isinstance(d["referent"], str):
        return "digest_schema"
    if len(json.dumps(d)) > S.DIGEST_CAP:
        return "digest_over_400"
    return None


def measured_band(words: int) -> str:
    return "L1" if words <= 12 else "L2" if words <= 50 else "L3" if words <= 150 else "L4"


def band_miss(requested: str, words: int) -> bool:
    lo, hi = BAND_BOUNDS[requested]
    if words < lo * (1 - BAND_MISS_TOLERANCE):
        return True
    return hi is not None and words > hi * (1 + BAND_MISS_TOLERANCE)


def is_dup(a, b) -> bool:
    return S.jaccard(a, b) >= S.JACCARD_DROP


class Dedup:
    """Exact text_sha and 5-shingle Jaccard against real DEV (hashed file), DEV-SYN (TEST only) and within
    the set. Returns the reason or None."""

    def __init__(self, real, devsyn):
        self.real, self.devsyn = real or {"text_sha": set(), "sets": []}, devsyn or {"text_sha": set(), "sets": []}
        self.within_sha, self.within = set(), []

    def check(self, text: str, sh) -> str | None:
        sha = S.text_sha(text)
        if sha in self.real["text_sha"]:
            return "dup_exact_real"
        if any(is_dup(sh, r) for r in self.real["sets"]):
            return "dup_jaccard_real"
        if sha in self.devsyn["text_sha"] or any(is_dup(sh, r) for r in self.devsyn["sets"]):
            return "dup_devsyn"
        if sha in self.within_sha or any(is_dup(sh, r) for r in self.within):
            return "dup_within"
        return None

    def accept(self, text, sh):
        self.within_sha.add(S.text_sha(text))
        self.within.append(sh)


def load_shingle_file(path) -> dict:
    if not path:
        return {"text_sha": set(), "sets": []}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"text_sha": set(doc.get("text_sha", [])), "sets": [frozenset(s) for s in doc.get("sets", [])]}


# ---------------------------------------------------------------------------------------------
# Custody helpers
# ---------------------------------------------------------------------------------------------

def find_finalist_files(dev_dir) -> list[str]:
    """Any finalists/thresholds file under $CLS/dev (the TEST-SYN agent must never see finalists)."""
    out = []
    if Path(dev_dir).is_dir():
        for dp, _dn, fns in os.walk(dev_dir):
            out += [os.path.join(dp, f) for f in fns if "finalists" in f.lower() or "thresholds" in f.lower()]
    return out


def custody_path(cls_root) -> Path:
    return S.test_syn_root(cls_root) / "custody.json"


def load_custody(cls_root) -> dict:
    p = custody_path(cls_root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"generated": 0, "regen_count": 0}


def save_custody(cls_root, doc) -> None:
    S.write_private(custody_path(cls_root), json.dumps(doc, sort_keys=True, indent=1) + "\n")


def labels_exist(cls_root, label_run_dir=None) -> bool:
    roots = [S.test_syn_root(cls_root)] + ([Path(label_run_dir)] if label_run_dir else [])
    for r in roots:
        if r.is_dir():
            for _dp, _dn, fns in os.walk(r):
                if any(f.startswith("labels_") and f.endswith(".jsonl") for f in fns):
                    return True
    return False


TEXT_FILES = ("items.jsonl", "gen_calls.jsonl")  # the two files of a set that hold generated text


def lock_items(items_path) -> None:
    """chmod 0000 the items file and the call log next to it (both hold generated text)."""
    os.chmod(items_path, 0)
    calls = Path(items_path).parent / "gen_calls.jsonl"
    if calls.exists():
        os.chmod(calls, 0)


def spec_log_path(a) -> Path:
    return Path(a.spec_log or Path(a.cls_root) / "spec_versions.jsonl")


def resemblance_failed_label(cls_root) -> dict | None:
    p = Path(cls_root) / "resemblance_label.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def frozen_version(a) -> int | None:
    for r in S.read_jsonl(spec_log_path(a)):
        if r.get("event") == "freeze":
            return r["version"]
    return None


def discard_set(a, reason: str) -> Path:
    """Rename the set directory to discarded-<utc>, record its sha256 (taken from the hashes file; the items
    file is never read), chmod 0000 on the items file. Any later read exits 10."""
    root = Path(a.out_root)
    if not S.under(root, S.test_syn_root(a.cls_root)):
        raise S.CLSExit(S.EXIT_CUSTODY, "discard applies to the TEST-SYN set only")
    if not root.is_dir():
        raise S.CLSExit(S.EXIT_REFUSE, "no set to discard")
    hashes = json.loads((root / "hashes.json").read_text(encoding="utf-8")) if (root / "hashes.json").exists() else {}
    stamp = S.utc_now().replace(":", "").replace("-", "")
    dest = root.parent / ("discarded-%s" % stamp)
    n = 0
    while dest.exists():
        n += 1
        dest = root.parent / ("discarded-%s-%d" % (stamp, n))
    os.rename(root, dest)
    for name in TEXT_FILES:
        if (dest / name).exists():
            os.chmod(dest / name, 0)
    S.append_jsonl(S.test_syn_root(a.cls_root) / "discarded.jsonl",
                   {"utc": S.utc_now(), "reason": reason, "path": str(dest), "items_sha256": hashes.get("items_sha256"),
                    "allocation_sha256": hashes.get("allocation_sha256")})
    return dest


# ---------------------------------------------------------------------------------------------
# The generation run
# ---------------------------------------------------------------------------------------------

class Gen:
    def __init__(self, a, set_name: str, *, regen_index: int = 0):
        self.a, self.set = a, set_name
        self.cfg = SET_CFG[set_name]
        self.out = Path(a.out_root)
        self.base = regen_index * REGEN_INDEX_BASE
        self.static = _static_doc(a)
        self.model_ids = json.loads(Path(a.pins_static).read_text(encoding="utf-8"))["pins"]["model_ids"]
        self.spend = S.SpendLog(a.spend_log or Path(a.cls_root) / "spend_log.jsonl", a.cap_usd)
        self.counters = {"generated": {}, "dropped": {}, "replaced": {}, "meta": {}, "scrub_abort": 0,
                         "calls": 0, "call_failures": 0, "cwd_leftover": 0}
        self.items: list[dict] = []
        self.band_matrix = {b: {m: 0 for m in BANDS} for b in BANDS}
        self.band_misses = 0
        self.digest_checked = self.digest_valid = 0
        self.calls_log = self.out / "gen_calls.jsonl"
        self.replay = {}

    def _bump(self, d, family, key=None, n=1):
        sub = self.counters[d].setdefault(family, {} if key is not None else 0)
        if key is None:
            self.counters[d][family] = sub + n
        else:
            sub[key] = sub.get(key, 0) + n

    # -- setup -------------------------------------------------------------------------------
    def prepare(self, version: int):
        a = self.a
        if self.set == "dev_syn":
            check_version(version)
        files = load_gen_files(a.gen_dir, version, self.static, a.pins_run)
        shares = load_length_shares(a.length_counts)
        phrases = files["intent_phrases"]
        projects = [p for p in files["projects"] if p]
        cells = allocate(self.cfg["n"], self.cfg["seed"], shares, files["task_tool_mask"], projects)
        for c in cells:
            c["situation"] = phrases[c["difficulty"]]
        self.files, self.shares, self.cells, self.version = files, shares, cells, version
        self.cfg_doc = {"set": self.set, "spec_version": version, "seed": self.cfg["seed"], "namespace": self.cfg["ns"],
                        "n": self.cfg["n"], "calls": self.cfg["calls"], "items_per_call": ITEMS_PER_CALL,
                        "gpt_share": GPT_SHARE, "length_shares": shares, "tool_weights": TOOL_WEIGHTS,
                        "difficulty_weights": dict(zip(DIFFICULTY, DIFF_WEIGHTS)), "ack_share": ACK_SHARE,
                        "null_project_share": NULL_PROJECT_SHARE, "replacement_cap": REPLACEMENT_CAP,
                        "shortfall_floor": SHORTFALL_FLOOR if self.set == "test_syn" else None,
                        "files_sha": {k: files[k + "_sha"] for k in GEN_FILE_STEMS}}
        self.alloc_sha = alloc_sha(cells)

    def alloc_bodies(self) -> dict:
        """The exact bytes write_alloc writes (name -> text). A pure function of the prepared allocation."""
        return {"allocation.json": json.dumps(self.cells, sort_keys=True) + "\n",
                "allocation.sha256": self.alloc_sha + "  allocation.json\n",
                "gen_config.json": json.dumps(self.cfg_doc, sort_keys=True, indent=1) + "\n"}

    def write_alloc(self):
        """Allocation table, its sha256 and gen_config.json are written BEFORE any generation call. Files
        that already exist (written by `alloc` and pinned with `pins --run`) are never rewritten: they must
        equal the recomputed ones."""
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.out, 0o700)
        want = self.alloc_bodies()
        for name, body in want.items():
            p = self.out / name
            if p.exists():
                if p.read_text(encoding="utf-8") != body:
                    raise S.CLSExit(S.EXIT_REFUSE, "existing %s differs from the recomputed one" % name)
            else:
                S.write_private(p, body)

    # -- calls -------------------------------------------------------------------------------
    def _template(self, family):
        return self.files["tmpl_gpt" if family == "gpt" else "tmpl_opus"], self.files[("tmpl_gpt" if family == "gpt" else "tmpl_opus") + "_sha"]

    def _log_request(self, call_id, tsha, cells, nonces, family, reported):
        S.append_jsonl(self.out / "gen_requests.jsonl", {
            "call_id": call_id, "template_sha": tsha, "cell_vectors": [{k: v for k, v in c.items()} for c in cells],
            "seed": self.cfg["seed"], "nonces": nonces, "model_id": self.model_ids["gen_" + family],
            "reported_model_ids": reported, "utc": S.utc_now(), "set": self.set, "family": family})

    def _ledger(self, **rec):
        rec["utc"] = S.utc_now()
        S.append_jsonl(self.calls_log, rec)

    def load_replay(self) -> int:
        """Finished calls of an interrupted run of this set (resume). Returns how many."""
        self.replay = {r["call_id"]: r for r in S.read_jsonl(self.calls_log) if r.get("phase") == "done"}
        return len(self.replay)

    def _outcome(self, call_id, out, outcome):
        if outcome == "scrub_abort":
            self.counters["scrub_abort"] += 1
        elif outcome != "ok":
            self.counters["call_failures"] += 1
        return call_id, out

    def call(self, call_index: int, family: str, cells: list[dict], nonces: list[str]):
        """One generation call. Returns (call_id, {idx: obj}) or (call_id, None) on failure. ScrubAbort is
        counted and returns None. A codex non-zero rc parks (exit 24); an Opus call without an opus model id
        stops (exit 22). A call already finished in an interrupted run is replayed, not re-sent."""
        call_id = "%s-c%04d" % (self.cfg["ns"], call_index)
        self.counters["calls"] += 1
        if call_id in self.replay:
            r = self.replay[call_id]
            # the raw answer text is re-parsed, so a replayed call yields byte-identical items
            out = parse_generation(r["text"]) if isinstance(r.get("text"), str) else None
            return self._outcome(call_id, out, r.get("outcome"))
        template, tsha = self._template(family)
        payload = build_request(template, cells, nonces).encode("utf-8")
        kind = "codex" if family == "gpt" else "claude"
        model = self.model_ids["gen_" + family]
        est = 0.0
        if family == "opus":
            self.spend.guard()
            est = self.spend.estimate()
            self.spend.pending(call_id, model, "claude", est)
        self._ledger(phase="pending", call_id=call_id, call_index=call_index, family=family)
        try:
            res, leftover = S.child_call(kind, model, payload, self.out, claude_bin=self.a.claude_bin,
                                         codex_bin=self.a.codex_bin, timeout=self.a.timeout, batch_id=call_id)
        except S.ScrubAbort:
            if family == "opus":
                self.spend.record(model, "claude", 0, 0.0, call_id=call_id, cost_basis="not_sent")
            self._ledger(phase="done", call_id=call_id, call_index=call_index, family=family, outcome="scrub_abort",
                         rc=None, text=None)
            return self._outcome(call_id, None, "scrub_abort")
        self.counters["cwd_leftover"] += leftover
        parsed = (S.parse_codex_jsonl if family == "gpt" else S.parse_claude_json)(res.stdout)
        ids = S.served_model_ids(kind, parsed, model)
        self._log_request(call_id, tsha, cells, nonces, family, ids)
        if family == "opus":
            self.spend.record(model, "claude", parsed.get("tokens", 0), est if res.timed_out else parsed.get("cost_usd", 0.0),
                              call_id=call_id, cost_basis="estimate" if res.timed_out else "reported")
        if res.timed_out:
            self.counters["timeouts"] = self.counters.get("timeouts", 0) + 1
        if family == "gpt" and res.returncode != 0:
            self._ledger(phase="parked", call_id=call_id, call_index=call_index, family=family, rc=res.returncode,
                         timed_out=res.timed_out)
            raise S.CLSExit(S.EXIT_GPT_PARK, "codex generation call failed (rc=%d%s); parked (resumable)" % (
                res.returncode, ", timeout" if res.timed_out else ""))
        if family == "opus" and res.returncode == 0 and not res.timed_out and not (
                ids and all(S.family_of(i) == "opus" for i in ids)):
            raise S.CLSExit(S.EXIT_MODEL_ID, "generator model id differs from the pin; call output discarded")
        out = parse_generation(parsed["text"]) if (res.returncode == 0 and parsed.get("ok")) else None
        outcome = "ok" if out is not None else ("timeout" if res.timed_out else "call_failed")
        self._ledger(phase="done", call_id=call_id, call_index=call_index, family=family, outcome=outcome,
                     rc=res.returncode, text=parsed["text"] if out is not None else None)
        return self._outcome(call_id, out, outcome)

    # -- filters -----------------------------------------------------------------------------
    def evaluate(self, cell, obj, family, call_id, dedup, ctx) -> str | None:
        """Apply the section 6 filters to one parsed item. Returns the drop reason or None (accepted)."""
        prompt, digest = obj.get("prompt"), obj.get("digest")
        if isinstance(digest, str):
            try:
                digest = json.loads(digest)
            except ValueError:
                return "parse_error"
        if not isinstance(prompt, str) or not prompt.strip() or digest is None:
            return "parse_error"
        dig_text = json.dumps(digest) if isinstance(digest, dict) else ""
        for i, pat in enumerate(self.files["meta"]):
            n_hit = 1 if (pat.search(prompt) or pat.search(dig_text)) else 0
            slot = self.counters["meta"].setdefault(self.files["meta_src"][i], {}).setdefault(family, {}).setdefault(
                cell["difficulty"], {"hits": 0, "n": 0})
            slot["n"] += 1
            slot["hits"] += n_hit
        self.digest_checked += 1
        prob = digest_problem(digest)
        if prob:
            return prob
        self.digest_valid += 1
        for pat in self.files["meta"]:
            if pat.search(prompt) or pat.search(dig_text):
                return "meta_pattern"
        sec = S.scrub_detect(prompt + "\n" + dig_text, ctx) + S.independent_scan(prompt + "\n" + dig_text)
        if sec:
            return "secret"
        words = len(prompt.split())
        measured = measured_band(words)
        self.band_matrix[cell["band"]][measured] += 1
        if band_miss(cell["band"], words):
            self.band_misses += 1
        sh = S.shingle_hashes(prompt)
        why = dedup.check(prompt, sh)
        if why:
            return why
        dedup.accept(prompt, sh)
        return None

    # -- the run -----------------------------------------------------------------------------
    def run(self, on_started=None) -> dict:
        """The generation run. `on_started()` is called once every pre-call check (self-test, pins, canary) has
        passed, right before the first generation call (the regen counts itself there)."""
        a = self.a
        S.require_fresh_selftest(a.selftest_result)
        ctx = S.ScrubContext(S.load_denylist(a.denylist), S.load_env_values(a.env_file)[0])
        S.configure(ctx)  # detector (d) (denylist + .env values) also applies inside outgoing_check at send
        env_sha = S.env_sha_at_use(a.env_file)
        require_pinned(self.out / "gen_config.json", self.static, a.pins_run)
        require_pinned(a.length_counts, self.static, a.pins_run)
        VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
        fam = call_families(self.cfg["seed"], self.cfg["calls"], self.base)
        order = call_order(self.cfg["seed"], self.cfg["calls"], self.base)
        calls = assign_to_calls(self.cells, self.cfg["calls"])
        dedup = Dedup(load_shingle_file(a.shingles_real), load_shingle_file(a.shingles_devsyn))
        if any(f == "gpt" for f in fam.values()):
            S.run_codex_canary(self.model_ids["gen_gpt"], self.out / "canary", codex_bin=a.codex_bin, timeout=a.timeout)
        if on_started:
            on_started()
        pending = []  # (cell, family) needing replacement
        for ci in order:
            idx = ci - self.base
            cells = calls[idx]
            family = fam[ci]
            nonces = [nonce(self.cfg["ns"], self.base + c["item_index"]) for c in cells]
            call_id, out = self.call(ci, family, cells, nonces)
            self._take(cells, out, family, call_id, dedup, ctx, pending)
        cap = int(REPLACEMENT_CAP * self.cfg["n"])
        used = 0
        next_index = self.cfg["n"] + self.base
        next_call = self.base + self.cfg["calls"]
        while pending and used < cap:
            cell, family = pending.pop(0)
            used += 1
            c2 = dict(cell, item_index=next_index)
            call_id, out = self.call(next_call, family, [c2], [nonce(self.cfg["ns"], next_index)])
            next_call += 1
            next_index += 1
            self._bump("replaced", family)
            self._take([c2], out, family, call_id, dedup, ctx, pending, replacement=True)
        shortfall = max(0, self.cfg["n"] - len(self.items))
        return self.finish(shortfall, used, len(pending), env_sha)

    def _take(self, cells, out, family, call_id, dedup, ctx, pending, replacement=False):
        for pos, c in enumerate(cells, start=1):
            self._bump("generated", family)
            obj = None if out is None else out.get(pos)
            reason = "call_failed" if out is None else ("missing_idx" if obj is None else
                                                        self.evaluate(c, obj, family, call_id, dedup, ctx))
            if reason:
                self._bump("dropped", family, reason)
                pending.append((c, family))
                continue
            digest = obj["digest"] if isinstance(obj["digest"], dict) else json.loads(obj["digest"])
            self.items.append({"item_id": "syn-%s-%04d" % (self.cfg["ns"], c["item_index"]), "text": obj["prompt"],
                               "digest": json.dumps(digest), "group": call_id,
                               "generator": family, "set": self.set, "spec_version": self.version,
                               "cell": {k: c[k] for k in ("task", "band", "tool", "project", "difficulty", "ack")},
                               "band_measured": measured_band(len(obj["prompt"].split())),
                               "replacement": replacement})

    def finish(self, shortfall, replacements_used, unreplaced, env_sha) -> dict:
        n = self.cfg["n"]
        tc = {t: 0 for t in TASKS}
        bc = {b: 0 for b in BANDS}
        for it in self.items:
            tc[it["cell"]["task"]] += 1
            bc[it["cell"]["band"]] += 1
        quota = band_quotas(n, self.shares)
        quota_ok = (all(abs(tc[t] - n / 12.0) <= 2 + shortfall for t in TASKS)
                    and all(abs(bc[b] - quota[b]) <= 2 + shortfall for b in BANDS))
        items_path = self.out / "items.jsonl"
        S.write_private(items_path, "".join(json.dumps(i, sort_keys=True) + "\n" for i in self.items))
        shas = {"allocation_sha256": self.alloc_sha, "gen_config_sha256": S.sha256_file(self.out / "gen_config.json"),
                "template_sha256": {"gpt": self.files["tmpl_gpt_sha"], "opus": self.files["tmpl_opus_sha"]},
                "gen_requests_sha256": S.sha256_file(self.out / "gen_requests.jsonl") if (self.out / "gen_requests.jsonl").exists() else None,
                "items_sha256": S.sha256_file(items_path)}
        report = {"set": self.set, "spec_version": self.version, "n_target": n, "n_items": len(self.items),
                  "generated": self.counters["generated"], "dropped": self.counters["dropped"],
                  "replaced": self.counters["replaced"], "replacements_used": replacements_used,
                  "replacement_cap": int(REPLACEMENT_CAP * n), "shortfall": shortfall,
                  "below_floor": self.set == "test_syn" and len(self.items) < SHORTFALL_FLOOR,
                  "unreplaced": unreplaced, "band_matrix": self.band_matrix, "band_miss": self.band_misses,
                  "band_miss_rate": round(self.band_misses / max(1, self.digest_valid), 4),
                  "digest_valid": self.digest_valid, "digest_checked": self.digest_checked,
                  "meta_pattern_rates": self.counters["meta"], "task_counts": tc, "band_counts": bc,
                  "band_quota": quota, "quota_ok": quota_ok, "quota_tolerance": 2 + shortfall, "calls": self.counters["calls"],
                  "call_failures": self.counters["call_failures"], "scrub_abort": self.counters["scrub_abort"],
                  "timeouts": self.counters.get("timeouts", 0), "resumed_calls": len(self.replay),
                  "length_shares": self.shares, "env_sha_at_use": env_sha, "sha256": shas}
        S.write_private(self.out / "report.json", json.dumps(report, sort_keys=True, indent=1) + "\n")
        return report


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------

def _guard_out(a, set_name):
    if set_name == "test_syn":
        root = S.test_syn_root(a.cls_root)
        if not S.under(a.out_root, root) or S._real(a.out_root) == S._real(root):
            raise S.CLSExit(S.EXIT_CUSTODY, "TEST-SYN output must be written under the custody root")
        if S.is_discarded(a.out_root, a.cls_root):
            raise S.CLSExit(S.EXIT_CUSTODY, "path is a discarded set")
    else:
        S.guard_dev_path(a.out_root, a.cls_root)


def _log_spec_run(a, g: Gen, report: dict):
    S.append_jsonl(spec_log_path(a), {"event": "run", "set": g.set, "version": g.version, "utc": S.utc_now(),
                                      "files_sha": g.cfg_doc["files_sha"], "gen_config_sha256": report["sha256"]["gen_config_sha256"],
                                      "items_sha256": report["sha256"]["items_sha256"],
                                      "allocation_sha256": report["sha256"]["allocation_sha256"]})


def _summary(report: dict) -> dict:
    return {k: report[k] for k in ("set", "spec_version", "n_target", "n_items", "generated", "dropped", "replaced",
                                   "shortfall", "below_floor", "band_matrix", "band_miss_rate", "digest_valid",
                                   "calls", "scrub_abort", "sha256")}


def _test_syn_preconditions(a, version: int):
    """Custody gates for generating TEST-SYN: finalists-file presence, resemblance label, frozen vN."""
    if find_finalist_files(Path(a.cls_root) / "dev"):
        raise S.CLSExit(S.EXIT_CUSTODY, "a finalists/thresholds file exists under $CLS/dev; the TEST-SYN agent must not run")
    lab = resemblance_failed_label(a.cls_root)
    if lab and lab.get("test_syn") == "NOT_INFORMATIVE_AS_PROXY":
        raise S.CLSExit(S.EXIT_RESEMBLANCE_FAILED, "TEST-SYN is NOT_INFORMATIVE_AS_PROXY (resemblance failed)")
    if load_custody(a.cls_root).get("test_syn") == "NOT_INFORMATIVE_AS_PROXY":
        raise S.CLSExit(S.EXIT_REGEN_LIMIT, "TEST-SYN is NOT_INFORMATIVE_AS_PROXY")
    fv = frozen_version(a)
    if fv is None or fv != version:
        raise S.CLSExit(S.EXIT_REFUSE, "TEST-SYN needs the frozen spec version %s" % fv)


def cmd_alloc(a) -> int:
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    _guard_out(a, a.set)
    g = Gen(a, a.set)
    g.prepare(a.spec_version)
    g.write_alloc()
    print(json.dumps({"set": a.set, "n": len(g.cells), "allocation_sha256": g.alloc_sha,
                      "gen_config_sha256": S.sha256_file(g.out / "gen_config.json"), "shares": g.shares}, sort_keys=True))
    return S.EXIT_OK


def _partial_run(out: Path) -> bool:
    rq = out / "gen_requests.jsonl"
    return (rq.exists() and rq.stat().st_size > 0) or (out / "gen_calls.jsonl").exists()


def _do_run(a, set_name: str, *, regen_index: int | None = None, verify_first: bool = True,
            on_started=None) -> tuple[Gen, dict]:
    """Generate one set. verify_first=False is the regen path: the previous set was just discarded (its pinned
    gen_config.json moved away), so pins are verified only after write_alloc has re-created the identical files
    (Gen.run verifies before any call). TEST-SYN is marked generated only when the set completes."""
    if verify_first:
        VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    _guard_out(a, set_name)
    out = Path(a.out_root)
    resume = bool(getattr(a, "resume", False))
    if set_name == "test_syn":
        _test_syn_preconditions(a, a.spec_version)
        cust = load_custody(a.cls_root)
        if regen_index is None:
            if cust.get("generated", 0) > cust.get("regen_count", 0) or (out / "items.jsonl").exists():
                raise S.CLSExit(S.EXIT_REFUSE, "TEST-SYN is generated once; use regen after a failed own check")
            regen_index = cust.get("regen_count", 0)
    if _partial_run(out) and not resume:
        raise S.CLSExit(S.EXIT_REFUSE, "out-root already holds a generation run (--resume continues an incomplete one)")
    if resume and ((out / "report.json").exists() or (out / "items.jsonl").exists()):
        raise S.CLSExit(S.EXIT_REFUSE, "the run in out-root is complete; nothing to resume")
    g = Gen(a, set_name, regen_index=regen_index or 0)
    g.prepare(a.spec_version)
    g.write_alloc()
    if resume:
        g.load_replay()
    report = g.run(on_started=on_started)
    if set_name == "test_syn":
        cust = load_custody(a.cls_root)
        save_custody(a.cls_root, dict(cust, generated=cust.get("generated", 0) + 1))
    _log_spec_run(a, g, report)
    return g, report


def cmd_run(a) -> int:
    _, report = _do_run(a, a.set)
    print(json.dumps(_summary(report), sort_keys=True))
    return S.EXIT_OK


def cmd_collect(a) -> int:
    rp = Path(a.out_root) / "report.json"
    if a.set != "test_syn":
        S.guard_dev_path(a.out_root, a.cls_root)
    print(json.dumps(_summary(json.loads(rp.read_text(encoding="utf-8"))), sort_keys=True))
    return S.EXIT_OK


def cmd_shingles(a) -> int:
    """Hashed-shingle file (no text) from an items file, for dedup in the generator."""
    S.guard_dev_path(a.items, a.cls_root)
    shas, sets = [], []
    for line in S.read_guarded_text(a.items, a.cls_root).splitlines():
        if line.strip():
            t = json.loads(line)["text"]
            shas.append(S.text_sha(t))
            sets.append(sorted(S.shingle_hashes(t)))
    S.write_private(a.out, json.dumps({"text_sha": shas, "sets": sets}) + "\n")
    print(json.dumps({"items": len(shas)}))
    return S.EXIT_OK


def cmd_write_gen_file(a) -> int:
    write_gen_file(a.gen_dir, a.name, Path(a.source).read_text(encoding="utf-8"), _static_doc(a), a.pins_run)
    print(json.dumps({"written": a.name}))
    return S.EXIT_OK


def cmd_freeze_spec(a) -> int:
    check_version(a.version)
    static = _static_doc(a)
    for fn in gen_file_names(a.version).values():
        require_pinned(Path(a.gen_dir) / fn, static, a.pins_run)
    res = json.loads(Path(a.resemble_result).read_text(encoding="utf-8"))
    if res.get("set") != "dev_syn" or res.get("spec_version") != a.version:
        raise S.CLSExit(S.EXIT_REFUSE, "resemblance result is from another set or version")
    if not res.get("pass"):
        raise S.CLSExit(S.EXIT_REFUSE, "resemblance result is a fail; this version cannot be frozen")
    if frozen_version(a) is not None:
        raise S.CLSExit(S.EXIT_REFUSE, "a spec version is already frozen")
    S.append_jsonl(spec_log_path(a), {"event": "freeze", "version": a.version, "utc": S.utc_now(),
                                      "files_sha": {k: S.sha256_file(Path(a.gen_dir) / fn) for k, fn in gen_file_names(a.version).items()},
                                      "result_sha256": S.sha256_file(a.resemble_result)})
    print(json.dumps({"frozen": a.version}))
    return S.EXIT_OK


def cmd_declare_failed(a) -> int:
    runs = {r["version"] for r in S.read_jsonl(spec_log_path(a)) if r.get("event") == "run" and r.get("set") == "dev_syn"}
    if runs != set(range(1, MAX_SPEC_VERSION + 1)) or frozen_version(a) is not None:
        raise S.CLSExit(S.EXIT_REFUSE, "resemblance failed is declared only after three failed versions and no freeze")
    S.write_private(Path(a.cls_root) / "resemblance_label.json",
                    json.dumps({"dev_syn": "RESEMBLANCE_FAILED", "test_syn": "NOT_INFORMATIVE_AS_PROXY"}, sort_keys=True) + "\n")
    print(json.dumps({"dev_syn": "RESEMBLANCE_FAILED", "test_syn": "NOT_INFORMATIVE_AS_PROXY"}))
    return S.EXIT_OK


def cmd_discard(a) -> int:
    dest = discard_set(a, a.reason)
    print(json.dumps({"discarded": dest.name}))
    return S.EXIT_OK


def regen_verify_before_discard(a, g: Gen) -> None:
    """Before anything is discarded: the pins verify, and every PINS-RUN-pinned file under the out-root (the
    set's gen_config.json, any pinned allocation file) is exactly what the regen will re-create, so the regen
    cannot fail its own pin check after the one allowed discard (exit 9 otherwise)."""
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    root = Path(a.out_root)
    want = g.alloc_bodies()
    for e in P.read_run(a.pins_run):
        for f, h in e.get("files", {}).items():
            if not S.under(f, root):
                continue
            rel = str(S._real(f).relative_to(S._real(root)))
            body = want.get(rel)
            if body is None or S.sha256_bytes(body.encode("utf-8")) != h:
                raise S.CLSExit(S.EXIT_PINS, "regen would not re-create the pinned %s; nothing discarded" % rel)


def cmd_regen(a) -> int:
    """One regeneration under the same vN. Every check, the pin verification included, runs BEFORE the previous
    set is discarded (rename + chmod 0000); a failure to discard aborts. regen_count is incremented only when the
    new run has started (all pre-call checks passed), so a regen that fails to start can be retried. A second
    regen, or a regen after any TEST label, is refused (exit 18)."""
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    _guard_out(a, "test_syn")
    cust = load_custody(a.cls_root)
    if cust.get("regen_count", 0) >= MAX_REGEN:
        save_custody(a.cls_root, dict(cust, test_syn="NOT_INFORMATIVE_AS_PROXY"))
        raise S.CLSExit(S.EXIT_REGEN_LIMIT, "second regeneration refused; TEST-SYN is NOT_INFORMATIVE_AS_PROXY")
    if labels_exist(a.cls_root, a.label_run_dir):
        raise S.CLSExit(S.EXIT_REGEN_LIMIT, "a TEST label exists; regeneration is closed")
    _test_syn_preconditions(a, a.spec_version)
    prev = json.loads(Path(a.resemble_result).read_text(encoding="utf-8"))
    if prev.get("set") != "test_syn" or prev.get("pass") is not False:
        raise S.CLSExit(S.EXIT_REFUSE, "regeneration needs the previous TEST-SYN's own failed text-only result")
    fv = [r for r in S.read_jsonl(spec_log_path(a)) if r.get("event") == "freeze"][0]
    now_files = {k: S.sha256_file(Path(a.gen_dir) / fn) for k, fn in gen_file_names(a.spec_version).items()}
    if now_files != fv["files_sha"]:
        raise S.CLSExit(S.EXIT_REFUSE, "the spec files differ from the frozen vN (no amendment between the two TEST-SYN sets)")
    probe = Gen(a, "test_syn", regen_index=1)
    probe.prepare(a.spec_version)
    regen_verify_before_discard(a, probe)
    discard_set(a, "regen: previous TEST-SYN failed its own text-only checks")
    a.resume = False

    def started():
        c = load_custody(a.cls_root)
        save_custody(a.cls_root, dict(c, regen_count=c.get("regen_count", 0) + 1))
    _g, report = _do_run(a, "test_syn", regen_index=1, verify_first=False, on_started=started)
    print(json.dumps(_summary(report), sort_keys=True))
    return S.EXIT_OK


def cmd_test_syn_step(a, *, resemble=None) -> int:
    """generate -> hash -> resemble -> chmod 0000 the items file -> write the hashes file, in that order.
    The tool never runs git or touches the network; the hashes file is ready to `git add`."""
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    resemble = resemble or R.resemble_in_step
    g, report = _do_run(a, "test_syn")
    items_path = Path(a.out_root) / "items.jsonl"
    try:
        hashes = dict(report["sha256"], spec_version=g.version, spec_files_sha=g.cfg_doc["files_sha"], utc=S.utc_now(),
                      set="test_syn")
        result = resemble(a, g.items, report)
        hashes["resemble_pass"] = bool(result.get("pass"))
        S.write_private(Path(a.out_root) / "resemble_result.json", json.dumps(result, sort_keys=True, indent=1) + "\n")
    finally:
        lock_items(items_path)
    S.write_private(Path(a.out_root) / "hashes.json", json.dumps(hashes, sort_keys=True, indent=1) + "\n")
    print(json.dumps({"resemble_pass": hashes["resemble_pass"], "items_locked": True, "sha256": report["sha256"]}, sort_keys=True))
    return S.EXIT_OK if hashes["resemble_pass"] else S.EXIT_RESEMBLE_FAIL


def build_parser():
    ap = argparse.ArgumentParser(prog="cls_gen_run")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def base(p, *, run=False):
        p.add_argument("--cls-root", required=True)
        p.add_argument("--pins-static")
        p.add_argument("--pins-run")
        if run:
            p.add_argument("--set", choices=("dev_syn", "test_syn"), required=True)
            p.add_argument("--spec-version", type=int, default=1)
            p.add_argument("--out-root", required=True)
            p.add_argument("--gen-dir", required=True)
            p.add_argument("--length-counts")
            p.add_argument("--shingles-real")
            p.add_argument("--shingles-devsyn")
            p.add_argument("--selftest-result")
            p.add_argument("--denylist")
            p.add_argument("--env-file")
            p.add_argument("--cap-usd", type=float, default=20.0)
            p.add_argument("--spend-log")
            p.add_argument("--spec-log")
            p.add_argument("--claude-bin", default="claude")
            p.add_argument("--codex-bin", default="codex")
            p.add_argument("--timeout", type=int, default=S.CLAUDE_TIMEOUT_S)
            p.add_argument("--resume", action="store_true",
                           help="continue an incomplete run in --out-root (same allocation, assignments and nonces)")

    for name in ("alloc", "run", "collect"):
        base(sub.add_parser(name), run=True)
    p = sub.add_parser("test-syn-step")
    base(p, run=True)
    p.add_argument("--real-items", required=True)
    p = sub.add_parser("regen")
    base(p, run=True)
    p.add_argument("--resemble-result", required=True)
    p.add_argument("--label-run-dir")
    p = sub.add_parser("discard")
    base(p, run=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("freeze-spec")
    base(p)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--resemble-result", required=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--spec-log")
    p = sub.add_parser("declare-resemblance-failed")
    base(p)
    p.add_argument("--spec-log")
    p = sub.add_parser("shingles")
    base(p)
    p.add_argument("--items", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("write-gen-file")
    base(p)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--source", required=True)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    handlers = {"alloc": cmd_alloc, "run": cmd_run, "collect": cmd_collect, "test-syn-step": cmd_test_syn_step,
                "regen": cmd_regen, "discard": cmd_discard, "freeze-spec": cmd_freeze_spec,
                "declare-resemblance-failed": cmd_declare_failed, "shingles": cmd_shingles,
                "write-gen-file": cmd_write_gen_file}
    try:
        return handlers[a.cmd](a)
    except S.CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
