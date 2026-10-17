#!/usr/bin/env python3
"""CLS-v16 B0 library: the logic behind scripts/cls_dev_run.py (PREREG-CLS-DEV sections 1.3, 2, 3.2, 6-10).

Stdlib only at import time. numpy / scikit-learn / sentence_transformers / torch / transformers are
imported lazily inside the functions that need them; tests inject an embedder and a trainer.

Reuses build B-L (cls_send_lib: detector, custody guards, dedup helpers, private writes; cls_pins: RUN
entries) and build B1 (cls_eval.truth_for, loaded by path). Nothing here duplicates them.

Privacy: item text lives only in the 0600 input items files and in memory. Nothing here prints, logs or
raises it: exceptions carry item ids and detector names only. Every Ollama call goes to 127.0.0.1 with
no proxy. All DEV outputs are guard-free and screening-only (they say so).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

EXIT_PARK = 3
EXIT_C4_KILLED = 7
EXIT_MB_GATE = 8

T0_DEFAULT = "2026-10-12T09:18:13Z"
CUTOFF_DEFAULT = "2026-10-08T20:46:00Z"
HAIKU_LIVE_FROM = "2026-10-09T00:00:00Z"
SEED = S.SEED
FOLDS = 5
FOLD_PREFIX = "clsdev:fold:20261017:"
TIERS = ("haiku", "sonnet", "opus")
RANK = {t: i for i, t in enumerate(TIERS)}
JUDGES = ("opus", "gpt", "sonnet")
SYN_SOURCE = "dev_syn"
PRIMARY_RULE = "majority3"
# Truth-rule names exactly as scripts/cls_eval.py spells them (truth_for is the only implementation).
TRUTH_RULES = (PRIMARY_RULE, "higher2", "gpt_excluded", "agree_only", "opus_only", "gpt_only", "sonnet_only")
ENGINE_SEEN_SOURCES = ("ge3_tune", "e2_cal")  # fitted engine L3 under D-69 (PREREG section 9.2)
BANDS = ("L1", "L2", "L3", "L4")
FLOOR_N_REAL = 300
FLOOR_N_TRUTH_OPUS = 40
FLOOR_LARGEST_GROUP = 0.20
MISSING_PRIMARY_MAX = 0.05
LOW_CLUSTER_UNK_SHARE = 0.15
SYN_LEAK_FLAG = 0.03
# Frozen fitting choices (PREREG 2.1 r2; constants, not options).
C_GRID = (0.1, 1.0, 10.0)
CLIP = (0.001, 0.999)
M_ASYM = 2.0
INNER_FOLDS = 4
TOKEN_CAP = 512
#: C2 has no local tokenizer: CD-18 as amended by D-169 caps its input at 384 whitespace words, a conservative
#: approximation of the 512-token cap (digest kept whole, prompt tail kept).
C2_WORD_CAP = 384
LR_MAX_ITER = 1000
LR_TOL = 1e-4
LOGPROB_FLOOR = -30.0
T_CONF_GRID = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))
LABEL_FLAGS = {"guard_free": True, "screening_only": True}
CRIT_NAMES = {0: "data_validity", 1: "hard_fit", 2: "errors", 3: "signal", 4: "operating_point", 5: "cost",
              6: "shift", 7: "stability", 8: "judge_robustness"}
P95_MAX_MS = 1500.0
P95_ASYNC_MAX_MS = 15000.0   # D-160: async budget; above it the candidate is killed
BLOCK_SAMPLE_S = 60.0        # PREREG 10.2: sample at the start and every 60 s during a timed block
MEM_MAX_GIB = 12.0
ERR_KILL = 0.05
AUROC_MIN = 0.65
AUROC_LO_MIN = 0.55
COST_HIGH_RATIO = 1.35
STABILITY_MIN = 0.98
NIGHT_CUTOFF = (23, 50)
NIGHT_END_HOUR = 7
DEFAULT_UNIT_S = 600.0
BLOCK_GAP_S = 1800
QUIET_INTERVAL_S = 10.0
OLLAMA_URL = "http://127.0.0.1:11434"
MB_REPO = "answerdotai/ModernBERT-base"
MB_BASELINE_AUROC = 0.75
MB_LR, MB_EPOCHS, MB_BATCH = 2e-5, 4, 16


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def parse_utc(value) -> datetime.datetime | None:
    """ISO 'Z' timestamp (or epoch seconds) -> aware UTC datetime; None when absent or unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def utc_str(d: datetime.datetime) -> str:
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def length_band(text: str) -> str:
    n = len(text.split())
    return "L1" if n <= 12 else "L2" if n <= 50 else "L3" if n <= 150 else "L4"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def canonical_text_default(text: str) -> str:
    from llm_router.engine import canonical_text
    return canonical_text(text)


def cap_digest(digest) -> str:
    """A digest is capped at 400 characters (truncated) wherever it is held or fed to a model (r3-6)."""
    if digest is None:
        return ""
    if not isinstance(digest, str):
        digest = json.dumps(digest, sort_keys=True)
    return digest[:S.DIGEST_CAP]


def guard_path(path, cls_root, test_syn_root=None) -> Path:
    """Every subcommand rejects (exit 10) a path whose resolved real path is under $CLS/test-syn (or the
    --test-syn-root) or is a discarded set. Symlinks and `..` are resolved first (S.under uses realpath)."""
    root = Path(test_syn_root) if test_syn_root else S.test_syn_root(cls_root)
    if S.under(path, root):
        raise S.CLSExit(S.EXIT_CUSTODY, "path is under the TEST-SYN custody root")
    if S.is_discarded(path, cls_root):
        raise S.CLSExit(S.EXIT_CUSTODY, "path is a discarded set")
    return Path(path)


def require_run_pin(path, run_path) -> None:
    """B0 never reads a file whose PINS-RUN entry is missing or mismatched (exit 9, r4-1)."""
    p = Path(path).resolve()
    sha = S.sha256_file(p) if p.is_file() else None
    if sha is not None and run_path:
        for e in P.read_run(run_path):
            if e.get("files", {}).get(str(p)) == sha:
                return
    raise S.CLSExit(S.EXIT_PINS, "no matching PINS-RUN entry for %s" % p.name)


def append_log(log_path, record: dict) -> None:
    """cls_dev_log.jsonl is append-only: never rewritten, never truncated."""
    S.append_jsonl(log_path, record)


# ---------------------------------------------------------------------------------------------
# Local Ollama (127.0.0.1 only, no proxy)
# ---------------------------------------------------------------------------------------------

class OllamaError(Exception):
    pass


def check_local_url(url: str) -> str:
    """Only http://127.0.0.1[:port] is allowed (the real run uses :11434; tests use a fake server's port)."""
    u = urllib.parse.urlparse(str(url))
    if u.scheme != "http" or u.hostname != "127.0.0.1" or u.username or u.password:
        raise S.CLSExit(S.EXIT_REFUSE, "only the local Ollama at 127.0.0.1 is allowed")
    return str(url).rstrip("/")


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Ollama:
    def __init__(self, base: str = OLLAMA_URL, timeout: float = 300.0):
        self.base = check_local_url(base)
        self.timeout = timeout

    def _call(self, method: str, path: str, body=None, timeout=None):
        check_local_url(self.base)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with _NO_PROXY_OPENER.open(req, timeout=timeout or self.timeout) as r:  # noqa: S310 (127.0.0.1 only)
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise OllamaError(type(e).__name__) from None

    def version(self) -> str:
        return str(self._call("GET", "/api/version").get("version"))

    def tags(self) -> dict:
        """{name: sorted list of every digest listed under it}; names normalised to `<name>:latest` (D-169)."""
        return P.ollama_digest_sets(self._call("GET", "/api/tags").get("models", []))

    def ps(self) -> list[dict]:
        return list(self._call("GET", "/api/ps").get("models", []))

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        return self._call("POST", "/api/embed", {"model": model, "input": list(texts)})["embeddings"]

    def chat(self, payload: dict) -> dict:
        return self._call("POST", "/api/chat", payload)

    def unload(self, model: str) -> None:
        self._call("POST", "/api/generate", {"model": model, "keep_alive": 0})


def unload_own(ollama, own_model: str) -> bool:
    """keep_alive 0 for the harness's OWN model only, then confirm via /api/ps that it is gone. A foreign
    model is never unloaded. Returns True when the own model is confirmed gone."""
    try:
        ollama.unload(own_model)
        names = {m.get("name") for m in ollama.ps()}
    except OllamaError:
        return False
    return own_model not in names


def ollama_model_gib(ollama, model: str) -> float | None:
    """Ollama's reported resident size of the model (criterion 1 memory term)."""
    try:
        for m in ollama.ps():
            if m.get("name") == model:
                return float(m.get("size", 0)) / float(1 << 30)
    except OllamaError:
        return None
    return None


# ---------------------------------------------------------------------------------------------
# QUIET gate (PREREG 10.2) and NIGHT window (10.3)
# ---------------------------------------------------------------------------------------------

def parse_memory_free(text: str) -> float:
    m = re.search(r"free percentage:\s*(\d+(?:\.\d+)?)%", text)
    if not m:
        raise ValueError("memory_pressure output not understood")
    return float(m.group(1))


def count_claude_p(ps_text: str, own_pids=()) -> int:
    """`claude -p` processes in `ps -axo pid=,comm=,args=` output, other than the harness's own children.
    Only claude -p counts (D-132 g: codex, pytest, ruff, node are not counted). Observed only."""
    own = {int(p) for p in own_pids}
    n = 0
    for line in ps_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid = int(parts[0]) if parts[0].isdigit() else None
        if pid is not None:
            parts = parts[1:]
        if pid in own or not parts:
            continue
        if os.path.basename(parts[0]) == "claude" and ("-p" in parts[1:] or "--print" in parts[1:]):
            n += 1
    return n


def default_readers(ollama: Ollama | None, own_model: str | None, own_pids=()) -> dict:  # pragma: no cover (system)
    def run(argv):
        return subprocess.run(argv, capture_output=True, timeout=30, shell=False).stdout.decode("utf-8", "replace")

    def foreign():
        return [m.get("name") for m in (ollama.ps() if ollama else []) if m.get("name") != own_model]

    def throttled():
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", run(["pmset", "-g", "therm"]))
        return bool(m and int(m.group(1)) < 100)
    return {"free_mem_pct": lambda: parse_memory_free(run(["memory_pressure", "-Q"])),
            "load1": lambda: os.getloadavg()[0], "foreign_models": foreign,
            "claude_p": lambda: count_claude_p(run(["ps", "-axo", "pid=,comm=,args="]), own_pids),
            "on_ac": lambda: "AC Power" in run(["pmset", "-g", "batt"]), "throttled": throttled}


def take_reading(readers: dict) -> dict:
    return {"free_mem_pct": float(readers["free_mem_pct"]()), "load1": float(readers["load1"]()),
            "foreign_models": list(readers["foreign_models"]()), "claude_p": int(readers["claude_p"]()),
            "on_ac": bool(readers["on_ac"]()), "throttled": bool(readers["throttled"]())}


def check_memory(r: dict) -> bool:
    return r["free_mem_pct"] >= 40.0


def check_load_p20(r: dict) -> bool:
    return r["load1"] < 4.0


def check_foreign_model(r: dict) -> bool:
    return not r["foreign_models"]


def check_claude_p(r: dict) -> bool:
    return r["claude_p"] == 0


def quiet_gate(readers: dict, *, sleep=time.sleep, interval: float = QUIET_INTERVAL_S) -> dict:
    """Three readings `interval` seconds apart. Each: free memory >= 40 percent AND load1 < 4.0 (P2.0 L13);
    no foreign Ollama model; no foreign `claude -p`; AC power; no thermal throttling; no sample >= 3.0 and
    load1 < 2.0 at start (CLS limits). A failure parks the run (exit 3, no row). Never unloads anything."""
    readings = []
    for i in range(3):
        readings.append(take_reading(readers))
        if i < 2:
            sleep(interval)
    reason = None
    for r in readings:
        if not check_memory(r):
            reason = "memory_below_40_percent"
        elif not check_load_p20(r):
            reason = "load1_at_or_above_4"
        elif not check_foreign_model(r):
            reason = "foreign_model_resident"
        elif not check_claude_p(r):
            reason = "claude_p_running"
        elif not r["on_ac"]:
            reason = "not_on_ac_power"
        elif r["throttled"]:
            reason = "thermal_throttling"
        elif r["load1"] >= 3.0:
            reason = "load_sample_at_or_above_3"
        if reason:
            break
    if reason is None and readings[0]["load1"] >= 2.0:
        reason = "load1_at_start_at_or_above_2"
    return {"ok": reason is None, "reason": reason, "readings": readings}


def evaluate_block(readings: list[dict]) -> str | None:
    """PREREG 10.2 block validity over the readings taken at the start and every 60 s during the block:
    load1 < 2.0 at start, block mean < 1.5, no sample >= 3.0, and at EVERY sample no foreign Ollama model,
    no foreign `claude -p`, AC power, no thermal throttling. Returns `invalid:<reason>` or None."""
    if not readings:
        return "invalid:no_samples"
    loads = [r["load1"] for r in readings]
    if loads[0] >= 2.0:
        return "invalid:load_start"
    if max(loads) >= 3.0:
        return "invalid:load_sample"
    if sum(loads) / len(loads) >= 1.5:
        return "invalid:load_mean"
    for r in readings:
        if r.get("foreign_models"):
            return "invalid:foreign_model"
        if r.get("claude_p"):
            return "invalid:claude_p"
        if not r.get("on_ac", True):
            return "invalid:not_on_ac"
        if r.get("throttled"):
            return "invalid:thermal"
    return None


def run_timed_block(calls: list, score, *, take, perf, mono, interval: float = BLOCK_SAMPLE_S) -> tuple[list, list, int]:
    """Time each call with perf around the whole score(text, digest); take a reading at the start, between
    calls whenever `interval` seconds (monotonic) have passed, and at the end. Readings are never taken
    inside a timed call. Returns (timings_ms, readings, n_none)."""
    readings = [take()]
    last = mono()
    timings, n_none = [], 0
    for text, digest in calls:
        if mono() - last >= interval:
            readings.append(take())
            last = mono()
        t0 = perf()
        n_none += score(text, digest) is None
        timings.append((perf() - t0) * 1000.0)
    readings.append(take())
    return timings, readings, n_none


def local_now() -> datetime.datetime:
    """Local machine wall-clock time, tz-aware in the local zone. Log stamps stay UTC."""
    return datetime.datetime.now().astimezone()


def to_local(now: datetime.datetime) -> datetime.datetime:
    """The wall-clock reading the night rule uses: the clock's own LOCAL time (never converted to UTC)."""
    return now.replace(tzinfo=None)


def can_start_unit(now: datetime.datetime, longest_unit_s: float | None) -> tuple[bool, str | None]:
    """No heavy unit starts inside 00:00-07:00 local, nor when now + 1.5 x (longest unit measured so far
    in this run, 10 minutes before one exists) reaches 23:50 local (r2-2)."""
    wall = to_local(now)
    if wall.hour < NIGHT_END_HOUR:
        return False, "night_window"
    est = datetime.timedelta(seconds=1.5 * (longest_unit_s if longest_unit_s else DEFAULT_UNIT_S))
    cutoff = wall.replace(hour=NIGHT_CUTOFF[0], minute=NIGHT_CUTOFF[1], second=0, microsecond=0)
    if wall + est >= cutoff:
        return False, "would_cross_2350"
    return True, None


def run_units(units: list, do_unit, *, clock, state_path, ollama, own_model: str | None,
              artifact_shas: dict, quiet=None, perf=time.perf_counter) -> dict:
    """Heavy-work loop: the quiet gate first (also on every resume), the night rule before EVERY unit,
    resume state after every unit, and the harness's own model unloaded on every way out (try/finally,
    including exceptions). Returns {"status": "done"|"parked", "reason", "done"}. Resume scope: a resume is
    refused (exit 9) when `artifact_shas` differs from the saved state; the caller decides what it covers
    (c4-run: the manifest sha; the hard reference arms: the hash of the item-id list)."""
    sp = Path(state_path)
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    if state and state.get("artifact_shas") != artifact_shas:
        raise S.CLSExit(S.EXIT_PINS, "resume refused: an artifact sha changed")
    done = int(state.get("done", 0))
    longest = float(state.get("longest_unit_s", 0.0)) or None
    try:
        if quiet is not None:
            try:
                q = quiet()
            except OllamaError:
                return {"status": "parked", "reason": "quiet:ollama_unreachable", "done": done, "readings": []}
            if not q["ok"]:
                return {"status": "parked", "reason": "quiet:" + str(q["reason"]), "done": done,
                        "readings": q["readings"]}
        for i in range(done, len(units)):
            ok, why = can_start_unit(clock(), longest)
            if not ok:
                S.write_private(sp, json.dumps({"done": i, "longest_unit_s": longest or 0.0,
                                                "artifact_shas": artifact_shas}))
                return {"status": "parked", "reason": why, "done": i}
            t0 = perf()
            do_unit(i, units[i])
            longest = max(longest or 0.0, perf() - t0)
            S.write_private(sp, json.dumps({"done": i + 1, "longest_unit_s": longest, "artifact_shas": artifact_shas}))
            done = i + 1
        return {"status": "done", "reason": None, "done": done}
    finally:
        if ollama is not None and own_model:
            unload_own(ollama, own_model)


# ---------------------------------------------------------------------------------------------
# Truth (cls_eval.truth_for is the only implementation) and label collection
# ---------------------------------------------------------------------------------------------

def load_cls_eval(path):
    import importlib.util
    p = Path(path)
    if not p.is_file():
        raise S.CLSExit(S.EXIT_REFUSE, "scripts/cls_eval.py (build B1) is not on this base")
    spec = importlib.util.spec_from_file_location("cls_eval_for_b0", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "truth_for"):
        raise S.CLSExit(S.EXIT_REFUSE, "cls_eval has no truth_for")
    return mod


def truth_tier(cls_eval, rule: str, tiers: dict) -> str | None:
    """Truth under a rule through cls_eval.truth_for({"truths": {judge: tier}}, rule)."""
    t = cls_eval.truth_for({"truths": {j: tiers.get(j) for j in JUDGES}}, rule)
    return t if t in TIERS else None


def resolve_truth_rules(status_real: dict | None, status_syn: dict | None) -> dict:
    """Per-set truth rule from the label runs' status files (PREREG 3.3, D-135, r3-2, r4-2). No void:
    majority3. One voided judge: the other two, split -> higher tier (gpt voided = cls_eval gpt_excluded),
    labelled truth_fallback. Two voided judges in one set: that set is NOT INFORMATIVE (no fallback)."""
    rules, fallback, voided_by, bad = {}, {}, {}, []
    for key, st in (("real", status_real), (SYN_SOURCE, status_syn)):
        voided = sorted(set((st or {}).get("voided") or []))
        voided_by[key] = voided
        if len(voided) >= 2:
            bad.append(key)
            rules[key] = None
        elif len(voided) == 1:
            j = voided[0]
            rules[key] = "gpt_excluded" if j == "gpt" else "other_two_higher:%s" % j
            fallback[key] = j
        else:
            rules[key] = PRIMARY_RULE
    return {"rules": rules, "truth_fallback": fallback, "voided": voided_by, "not_informative": bad}


def set_key(item: dict) -> str:
    return SYN_SOURCE if item.get("kind") == "syn" else "real"


def legacy_index(rows: list[dict]) -> dict:
    """(session_id, text_sha) -> tier for existing labels ({text_sha, session_id, truth})."""
    idx = {}
    for r in rows:
        t = r.get("truth") or r.get("tier")
        if t in TIERS and r.get("text_sha"):
            idx[(r.get("session_id") or "", r["text_sha"])] = t
    return idx


def join_legacy(idx: dict, session_id: str, sha: str, used: set):
    """shadow_eval.load_labels join: (session_id, text_sha), then ("", text_sha); never guessed."""
    for key in ((session_id or "", sha), ("", sha)):
        if key in idx:
            used.add(key)
            return idx[key]
    return None


def collect_truths(items: list[dict], run_labels: dict, voided: dict) -> tuple[dict, dict]:
    """{item_id: {judge: tier}}: the existing labels the manifest joined (item["truth"]) plus new label-run
    rows ({item_id, judge, tier, ...}). A voided judge's labels for a set are discarded. An existing label
    wins over a new label of the same judge (reused, not re-bought); overlaps and unknown ids are counted."""
    kind_of = {it["item_id"]: set_key(it) for it in items}
    out, counts = {}, {"n_unknown_item_labels": {}, "n_label_overlap": {}, "n_labels": {}, "n_voided_labels_dropped": {}}
    dropped = {j: 0 for j in JUDGES}
    for it in items:
        t = {j: (it.get("truth") or {}).get(j) for j in JUDGES}
        for j in JUDGES:
            if t[j] is not None and j in voided.get(kind_of[it["item_id"]], []):
                t[j] = None
                dropped[j] += 1
        out[it["item_id"]] = {j: (v if v in TIERS else None) for j, v in t.items()}
    for j in JUDGES:
        unknown = overlap = 0
        for r in run_labels.get(j, []):
            iid, t = r.get("item_id"), r.get("tier")
            if iid not in out or t not in TIERS:
                unknown += 1
                continue
            if j in voided.get(kind_of[iid], []):
                dropped[j] += 1
                continue
            if out[iid][j] is not None:
                overlap += 1
                continue
            out[iid][j] = t
        counts["n_unknown_item_labels"][j] = unknown
        counts["n_label_overlap"][j] = overlap
        counts["n_labels"][j] = sum(1 for v in out.values() if v[j] is not None)
        counts["n_voided_labels_dropped"][j] = dropped[j]
    return out, counts


def load_run_labels(label_dirs: list) -> dict:
    """labels_<judge>.jsonl minus excluded_<judge>.jsonl (retroactive exclusions) from cls_label_run dirs."""
    out: dict = {j: [] for j in JUDGES}
    for d in label_dirs:
        for j in JUDGES:
            excl = {r.get("item_id") for r in S.read_jsonl(Path(d) / ("excluded_%s.jsonl" % j))}
            out[j] += [r for r in S.read_jsonl(Path(d) / ("labels_%s.jsonl" % j)) if r.get("item_id") not in excl]
    return out


def primary_truths(cls_eval, items: list[dict], truths: dict, rules: dict) -> dict:
    return {it["item_id"]: (truth_tier(cls_eval, rules[set_key(it)], truths[it["item_id"]])
                            if rules.get(set_key(it)) else None) for it in items}


# ---------------------------------------------------------------------------------------------
# Manifest (PREREG 1.1-1.3)
# ---------------------------------------------------------------------------------------------

def time_filter(ts: datetime.datetime | None, t0: datetime.datetime, cutoff: datetime.datetime) -> str | None:
    """Drop reason, or None: at or after T0 (amend4 window); after the P17D cutoff; or NO timestamp. The
    PREREG has no rule for a record without a timestamp; B0 drops it (it cannot be shown to precede T0) and
    counts it as n_no_timestamp_dropped, so the attach step's misses are visible."""
    if ts is None:
        return "n_no_timestamp_dropped"
    if ts >= t0:
        return "n_after_T0_dropped"
    if ts > cutoff:
        return "n_after_cutoff_dropped"
    return None


def is_near_dup(a, b) -> bool:
    """5-shingle Jaccard >= 0.8 (the supply.py rule; S.JACCARD_DROP)."""
    return S.jaccard(a, b) >= S.JACCARD_DROP


def secret_hits(text: str, digest: str, ctx) -> list[str]:
    """PREREG 3.2 detector (a)-(e) on the prompt and its full digest. Detector names only."""
    hits = S.scrub_detect(text, ctx)
    if digest:
        hits = hits + S.scrub_detect(digest, ctx)
    return sorted(set(hits))


def length_counts_from_items(real_items: list[dict]) -> dict:
    """Aggregate length-band counts of the kept real DEV items: the ONLY input the generator takes from
    real data (r3-1). Exactly the four band counts, nothing else."""
    c = {b: 0 for b in BANDS}
    for it in real_items:
        c[it["band"]] += 1
    return c


def assign_folds(items: list[dict], k: int = FOLDS) -> dict:
    """Session-grouped k-fold (PREREG 6.1). Groups sorted by sha256("clsdev:fold:20261017:" + group), dealt
    round-robin, then balanced greedily so fold sizes differ by at most one group size. A group never
    spans folds; a DEV-SYN group and a real group are never merged (the key carries the kind)."""
    sizes: dict = {}
    for it in items:
        key = (it["kind"], it["group"])
        sizes[key] = sizes.get(key, 0) + 1
    ordered = sorted(sizes, key=lambda g: (hashlib.sha256((FOLD_PREFIX + g[1]).encode()).hexdigest(), g[0]))
    fold_of = {g: i % k for i, g in enumerate(ordered)}
    load = [0] * k
    for g, f in fold_of.items():
        load[f] += sizes[g]
    for _ in range(10 * len(ordered) + 1):
        hi = max(range(k), key=lambda f: (load[f], -f))
        lo = min(range(k), key=lambda f: (load[f], f))
        spread = load[hi] - load[lo]
        movable = [g for g in ordered if fold_of[g] == hi and sizes[g] < spread]
        if not movable:
            break
        g = max(movable, key=lambda g: (sizes[g], g))
        fold_of[g] = lo
        load[hi] -= sizes[g]
        load[lo] += sizes[g]
    return {it["item_id"]: fold_of[(it["kind"], it["group"])] for it in items}


def _tally(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def build_manifest(real_items, syn_items, *, ctx, t0, cutoff, legacy=None, env_info=None,
                   denylist_info=None) -> tuple[dict, dict]:
    """PREREG 1.1-1.3 (r3 order: before any NEW label). Returns (manifest, length_counts). Item text is read
    here and never leaves: the manifest holds ids, groups, bands, folds, text_sha, counts and the EXISTING
    Opus/Sonnet labels joined through the text_sha map (item["truth"], tiers only; the B-L label runner reads
    it to skip items that already carry a Sonnet label). Unmapped existing labels are counted."""
    counts = {"n_input_real": len(real_items), "n_after_T0_dropped": 0, "n_after_cutoff_dropped": 0,
              "n_no_timestamp_dropped": 0, "n_dropped_dup": 0, "n_excluded_secret": 0,
              "excluded_secret_by_detector": {}, "excluded_secret_by_opus_tier": {}}
    leg_idx = {j: legacy_index(rows) for j, rows in (legacy or {}).items() if j in JUDGES}
    leg_used = {j: set() for j in leg_idx}
    recs = []
    for it in real_items:
        ts = parse_utc(it.get("timestamp"))
        why = time_filter(ts, t0, cutoff)
        if why:
            counts[why] += 1
            continue
        recs.append((ts, it))
    recs.sort(key=lambda x: (x[0], x[1]["item_id"]))
    kept, seen_sha, shingles = [], set(), []
    for ts, it in recs:  # earliest record first, so the earliest of a duplicate pair is kept
        sha = S.text_sha(it["text"])
        sh = S.shingle_hashes(it["text"])
        if sha in seen_sha or any(is_near_dup(sh, o) for o in shingles):
            counts["n_dropped_dup"] += 1
            continue
        seen_sha.add(sha)
        shingles.append(sh)
        kept.append((it, sha, sh))
    real_out, real_sh, n_unk = [], [], 0
    for it, sha, sh in kept:
        dig = it.get("digest")
        dig = dig if isinstance(dig, str) else (json.dumps(dig, sort_keys=True) if dig else "")
        hits = secret_hits(it["text"], dig, ctx)
        if hits:
            counts["n_excluded_secret"] += 1
            for h in hits:
                counts["excluded_secret_by_detector"][h] = counts["excluded_secret_by_detector"].get(h, 0) + 1
            tier = join_legacy(leg_idx.get("opus", {}), it.get("session_id") or "", sha, set()) or "none"
            counts["excluded_secret_by_opus_tier"][tier] = counts["excluded_secret_by_opus_tier"].get(tier, 0) + 1
            continue
        group = it.get("group") or it.get("session_id") or ""
        if not group:
            group = "unk:%s:%s" % (it.get("source_set", "real"), it.get("batch") or "0")
            n_unk += 1
        req = it.get("requested")
        truth = {j: join_legacy(leg_idx[j], it.get("session_id") or "", sha, leg_used[j]) if j in leg_idx else None
                 for j in JUDGES}
        real_out.append({"item_id": it["item_id"], "kind": "real", "source_set": it.get("source_set", "real"),
                         "group": str(group), "band": length_band(it["text"]),
                         "requested": req if req in TIERS else None, "text_sha": sha, "has_digest": bool(dig),
                         "generator": None, "task_type": None, "tool_need": None, "truth": truth})
        real_sh.append(sh)
    real_sha = {x["text_sha"] for x in real_out}
    syn_out = []
    sc = {"n_syn_input": len(syn_items), "n_syn_dropped_dup_vs_real": 0, "n_syn_dropped_dup_within": 0,
          "n_syn_excluded_secret": 0}
    syn_seen_sha, syn_seen_sh = set(), []
    for it in syn_items:
        text = it["text"]
        sha = S.text_sha(text)
        sh = S.shingle_hashes(text)
        dig = it.get("digest")
        dig = dig if isinstance(dig, str) else (json.dumps(dig, sort_keys=True) if dig else "")
        if secret_hits(text, dig, ctx):
            sc["n_syn_excluded_secret"] += 1
            continue
        if sha in real_sha or any(is_near_dup(sh, o) for o in real_sh):
            sc["n_syn_dropped_dup_vs_real"] += 1
            continue
        if sha in syn_seen_sha or any(is_near_dup(sh, o) for o in syn_seen_sh):
            sc["n_syn_dropped_dup_within"] += 1
            continue
        syn_seen_sha.add(sha)
        syn_seen_sh.append(sh)
        cell = it.get("cell") or {}
        syn_out.append({"item_id": it["item_id"], "kind": "syn", "source_set": SYN_SOURCE,
                        "group": "call:%s" % (it.get("group") or it["item_id"]), "band": length_band(text),
                        "requested": None, "text_sha": sha, "has_digest": bool(dig),
                        "generator": it.get("generator"), "task_type": cell.get("task"), "tool_need": cell.get("tool"),
                        "truth": {j: None for j in JUDGES}})
    counts.update(sc)
    counts["n_syn_dev"] = len(syn_out)
    counts["syn_leak_share"] = round(sc["n_syn_dropped_dup_vs_real"] / len(syn_items), 6) if syn_items else 0.0
    counts["syn_memorising_flag"] = bool(syn_items and sc["n_syn_dropped_dup_vs_real"] / len(syn_items) > SYN_LEAK_FLAG)
    all_items = sorted(real_out + syn_out, key=lambda x: x["item_id"])
    folds = assign_folds(all_items)
    for x in all_items:
        x["fold"] = folds[x["item_id"]]
    n_real = len(real_out)
    groups = _tally(x["group"] for x in real_out)
    counts.update({
        "n_real_dev": n_real, "n_sessions": len([g for g in groups if not g.startswith("unk:")]),
        "n_unk_group": n_unk, "unk_share": round(n_unk / n_real, 6) if n_real else 0.0,
        "low_cluster_fidelity": bool(n_real and n_unk / n_real > LOW_CLUSTER_UNK_SHARE),
        "largest_session_share": round(max(groups.values()) / n_real, 6) if groups else 0.0,
        "per_source_n": _tally(x["source_set"] for x in real_out),
        "per_length_band_n": _tally(x["band"] for x in real_out),
        "fold_sizes": _tally(x["fold"] for x in all_items),
        "n_existing_labels": {j: sum(1 for x in real_out if x["truth"][j]) for j in leg_idx},
        "n_unmapped_labels": {j: len(leg_idx[j]) - len(leg_used[j]) for j in leg_idx},
        "per_judge_existing_tier_n": {j: _tally(x["truth"][j] for x in real_out if x["truth"][j]) for j in leg_idx},
    })
    counts["validity_floor"] = validity_floor(counts)
    manifest = {"manifest_version": 1, "t0": utc_str(t0), "cutoff": utc_str(cutoff), "counts": counts,
                "files": {"denylist": denylist_info, "env": env_info}, "items": all_items, **LABEL_FLAGS}
    return manifest, length_counts_from_items(real_out)


def validity_floor(counts: dict) -> dict:
    """DEV validity floor (PREREG 1.3): n_real >= 300, n_truth_opus >= 40, largest session <= 20 percent,
    primary truth missing <= 5 percent. The truth parts are `pending` until labels exist."""
    failed, pending = [], []
    if counts["n_real_dev"] < FLOOR_N_REAL:
        failed.append("n_real_dev_below_300")
    if counts["largest_session_share"] > FLOOR_LARGEST_GROUP:
        failed.append("largest_session_above_20_percent")
    if counts.get("n_truth_opus") is None:
        pending.append("n_truth_opus")
    elif counts["n_truth_opus"] < FLOOR_N_TRUTH_OPUS:
        failed.append("n_truth_opus_below_40")
    miss = counts.get("primary_missing_share")
    if miss is not None and miss > MISSING_PRIMARY_MAX:
        failed.append("primary_truth_missing_above_5_percent")
    status = "NOT_INFORMATIVE" if failed else ("PENDING" if pending else "OK")
    return {"status": status, "failed": failed, "pending": pending}


# ---------------------------------------------------------------------------------------------
# Heads (C1-C4 share them; PREREG 2.1 r2 frozen fitting choices, 6.3 weights)
# ---------------------------------------------------------------------------------------------

def count_words(text: str) -> int:
    return len(text.split())


def input_cap(cand) -> dict:
    """The model-input cap per candidate (CD-18, D-169): 384 whitespace words for C2 (no local tokenizer),
    512 tokens otherwise."""
    if cand == "C2":
        return {"cap": C2_WORD_CAP, "unit": "whitespace_words"}
    return {"cap": TOKEN_CAP, "unit": "tokens"}


def model_input(digest, canonical: str, *, max_tokens: int = TOKEN_CAP, count_tokens=count_words) -> str:
    """"[ctx] " + digest + " [prompt] " + canonical_text with a digest, else canonical_text. The digest is
    capped at 400 characters; over the token cap the digest part is kept whole and the prompt part is
    truncated from its head (the tail is kept)."""
    d = cap_digest(digest)
    head = ("[ctx] " + d + " [prompt] ") if d else ""
    if head and count_tokens(head) > max_tokens:
        raise ValueError(f"digest part alone exceeds the {max_tokens}-token cap (CD-18): harness error, not truncated")
    body = canonical
    if count_tokens(head + body) <= max_tokens:
        return head + body
    lo, hi = 0, len(body)
    while lo < hi:  # the longest tail that fits
        mid = (lo + hi) // 2
        if count_tokens(head + body[mid:]) <= max_tokens:
            hi = mid
        else:
            lo = mid + 1
    return head + body[lo:]


def l2_normalise(vec):
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


class Scaler:
    """StandardScaler fitted on the training fold only (pure Python, so tests need no numpy)."""

    def fit(self, X):
        n, d = len(X), len(X[0])
        self.mean = [sum(r[j] for r in X) / n for j in range(d)]
        self.std = [math.sqrt(sum((r[j] - self.mean[j]) ** 2 for r in X) / n) or 1.0 for j in range(d)]
        return self

    def transform(self, X):
        return [[(r[j] - self.mean[j]) / self.std[j] for j in range(len(r))] for r in X]

    def params(self) -> dict:
        return {"mean": self.mean, "std": self.std}


def pool_weights(n_real: int, n_syn: int) -> tuple[float, float]:
    """Real items weigh 1, DEV-SYN items min(1, n_real / n_syn): real carries at least half the loss."""
    return 1.0, (min(1.0, n_real / n_syn) if n_syn else 1.0)


def head_targets(truth: str) -> tuple[int, int]:
    r = RANK[truth]
    return int(r >= 1), int(r >= 2)


def head_sample_weight(w_pool: float, truth: str, head_rank: int) -> float:
    """sample_weight = w_pool * m, m = 2.0 for items whose truth is at or above the head's tier, else 1.0."""
    return w_pool * (M_ASYM if RANK[truth] >= head_rank else 1.0)


def assemble_probs(p_ge_sonnet: float, p_ge_opus: float) -> dict:
    """Clip to [0.001, 0.999]; force P(>=opus) <= P(>=sonnet); haiku = 1 - Ps, sonnet = Ps - Po, opus = Po."""
    ps = min(max(p_ge_sonnet, CLIP[0]), CLIP[1])
    po = min(max(p_ge_opus, CLIP[0]), CLIP[1])
    po = min(po, ps)
    return {"haiku": 1.0 - ps, "sonnet": ps - po, "opus": po}


def confidence(probs: dict) -> float:
    return max(probs.values())


def l2_tier(probs: dict, t_hi: float, t_lo: float) -> str:
    """PREREG 7.1: opus if P_o >= t_hi; else haiku if P_h >= t_lo; else sonnet."""
    if probs["opus"] >= t_hi:
        return "opus"
    return "haiku" if probs["haiku"] >= t_lo else "sonnet"


def weighted_logloss(model, X, y, w) -> float:
    ps = model.predict_proba(X)
    tot = sum(w) or 1.0
    s = 0.0
    for p, yy, ww in zip(ps, y, w):
        p = min(max(p, 1e-9), 1 - 1e-9)
        s += ww * -(yy * math.log(p) + (1 - yy) * math.log(1 - p))
    return s / tot


def group_folds(groups: list[str], k: int) -> dict:
    """Deterministic group -> inner fold map (sorted by hash, dealt round-robin)."""
    uniq = sorted(set(groups), key=lambda g: hashlib.sha256(("clsdev:inner:%d:%s" % (SEED, g)).encode()).hexdigest())
    return {g: i % k for i, g in enumerate(uniq)}


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


class LinearModel:
    """A fitted binary head as plain coefficients (predicts without sklearn)."""

    def __init__(self, coef, intercept):
        self.coef, self.intercept = [float(c) for c in coef], float(intercept)

    def predict_proba(self, X):
        return [_sigmoid(self.intercept + sum(c * v for c, v in zip(self.coef, x))) for x in X]

    def params(self) -> dict:
        return {"coef": self.coef, "intercept": self.intercept}


class ConstModel(LinearModel):
    """A head whose training fold holds one class only (LR cannot fit it)."""

    def __init__(self, p: float, d: int):
        super().__init__([0.0] * d, math.log(min(max(p, 1e-6), 1 - 1e-6) / (1 - min(max(p, 1e-6), 1 - 1e-6))))


def sklearn_trainer(X, y, w, C):
    """LogisticRegression L2, lbfgs, max_iter 1000, tol 1e-4, class_weight None (lazy import)."""
    from sklearn.linear_model import LogisticRegression
    if len(set(y)) < 2:
        return ConstModel(float(y[0]) if y else 0.5, len(X[0]))
    clf = LogisticRegression(penalty="l2", solver="lbfgs", max_iter=LR_MAX_ITER, tol=LR_TOL, C=C, class_weight=None)
    clf.fit(X, y, sample_weight=w)
    return LinearModel(clf.coef_[0], clf.intercept_[0])


def pure_python_trainer(X, y, w, C, *, iters: int = 200, lr: float = 0.5):
    """A tiny weighted L2 logistic regression for tests only (injected; the real run uses sklearn)."""
    if len(set(y)) < 2:
        return ConstModel(float(y[0]) if y else 0.5, len(X[0]))
    d = len(X[0])
    coef, b = [0.0] * d, 0.0
    tot = sum(w) or 1.0
    lam = 1.0 / (C * tot)
    for _ in range(iters):
        gw, gb = [0.0] * d, 0.0
        for xi, yi, wi in zip(X, y, w):
            e = (_sigmoid(b + sum(c * v for c, v in zip(coef, xi))) - yi) * wi / tot
            gb += e
            for j in range(d):
                gw[j] += e * xi[j]
        b -= lr * gb
        coef = [c - lr * (g + lam * c) for c, g in zip(coef, gw)]
    return LinearModel(coef, b)


def choose_C(trainer, X, y, w, groups, grid=C_GRID, inner=INNER_FOLDS) -> float:
    """C by inner 4-fold group CV on the weighted log-loss of the head, grid {0.1, 1, 10}."""
    fm = group_folds(groups, inner)
    f_of = [fm[g] for g in groups]
    best, best_loss = grid[0], None
    for C in grid:
        loss, n = 0.0, 0
        for f in range(inner):
            tr = [i for i, v in enumerate(f_of) if v != f]
            te = [i for i, v in enumerate(f_of) if v == f]
            if not te or not tr:
                continue
            m = trainer([X[i] for i in tr], [y[i] for i in tr], [w[i] for i in tr], C)
            loss += weighted_logloss(m, [X[i] for i in te], [y[i] for i in te], [w[i] for i in te])
            n += 1
        loss = loss / n if n else float("inf")
        if best_loss is None or loss < best_loss - 1e-12:
            best, best_loss = C, loss
    return best


def fit_two_heads(trainer, X, rows) -> dict:
    """Fit the two binary heads on (X, rows) after a train-only scaler. Returns {scaler, heads, C}."""
    scaler = Scaler().fit(X)
    Xs = scaler.transform(X)
    heads, Cs = [], []
    for rank in (1, 2):
        y = [head_targets(r["truth"])[rank - 1] for r in rows]
        w = [head_sample_weight(r["w_pool"], r["truth"], rank) for r in rows]
        C = choose_C(trainer, Xs, y, w, [r["group"] for r in rows])
        heads.append(trainer(Xs, y, w, C))
        Cs.append(C)
    return {"scaler": scaler, "heads": heads, "C": Cs}


def predict_with(fit: dict, X) -> list[dict]:
    Xs = fit["scaler"].transform(X)
    p_s = fit["heads"][0].predict_proba(Xs)
    p_o = fit["heads"][1].predict_proba(Xs)
    return [assemble_probs(a, b) for a, b in zip(p_s, p_o)]


def oof_fold(trainer, X, rows, fold: int) -> list[tuple[int, dict]]:
    """Train on every fold but `fold`, score only `fold` (a row is never scored by a model that saw it)."""
    tr = [i for i, r in enumerate(rows) if r["fold"] != fold]
    te = [i for i, r in enumerate(rows) if r["fold"] == fold]
    if not tr or not te:
        return []
    fit = fit_two_heads(trainer, [X[i] for i in tr], [rows[i] for i in tr])
    return list(zip(te, predict_with(fit, [X[i] for i in te])))


def fit_candidate(trainer, X, rows, *, k: int = FOLDS) -> dict:
    """5-fold session-grouped OOF scores plus a final refit on all DEV. rows: item_id, fold, group, truth
    (primary tier), w_pool. Deterministic (folds are seed-derived, the trainers are deterministic)."""
    oof = [None] * len(rows)
    for f in range(k):
        for i, probs in oof_fold(trainer, X, rows, f):
            oof[i] = probs
    final = fit_two_heads(trainer, X, rows)
    table = []
    for r, p in zip(rows, oof):
        if p is None:
            continue
        table.append({"item_id": r["item_id"], "fold": r["fold"], "group": r["group"], "p_haiku": p["haiku"],
                      "p_sonnet": p["sonnet"], "p_opus": p["opus"], "confidence": confidence(p)})
    art = {"C": final["C"], "scaler": final["scaler"].params(), "heads": [h.params() for h in final["heads"]]}
    return {"oof": table, "final": final, "artifact": art, "artifact_sha256": S.sha256_bytes(canonical_json(art).encode())}


def scorer_from_artifact(art: dict):
    """features -> probs from a saved final refit (scaler + two linear heads)."""
    sc = Scaler()
    sc.mean, sc.std = art["scaler"]["mean"], art["scaler"]["std"]
    heads = [LinearModel(h["coef"], h["intercept"]) for h in art["heads"]]
    return lambda x: predict_with({"scaler": sc, "heads": heads}, [x])[0]


def hash_dir_files(directory) -> dict:
    d = Path(directory)
    return {str(p.relative_to(d)): S.sha256_file(p) for p in sorted(d.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------------------------
# C4: first-token logprob readout of the v6 prompt (thinking off; no argmax fallback)
# ---------------------------------------------------------------------------------------------

_TIER_KEY_RE = re.compile(r'"tier"\s*:\s*"\s*$')


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def tier_token_top(resp: dict):
    """The top_logprobs of the FIRST token of the "tier" value in a v6 answer, or None when the response
    carries no usable logprobs (absent, malformed, non-finite, or no tier value)."""
    lp = resp.get("logprobs") if isinstance(resp, dict) else None
    if not isinstance(lp, list) or not lp:
        return None
    acc = ""
    for e in lp:
        if not isinstance(e, dict) or not isinstance(e.get("token"), str):
            return None
        if _TIER_KEY_RE.search(acc) and e["token"].strip(" \"").strip():
            top = e.get("top_logprobs")
            if not _finite(e.get("logprob")) or not isinstance(top, list) or not top:
                return None
            if not all(isinstance(t, dict) and _finite(t.get("logprob")) for t in top):
                return None
            return top
        acc += e["token"]
    return None


def tier_logprobs(top: list[dict]) -> dict:
    """Map first-token alternatives to tiers: a token whose stripped lowercase text is a non-empty prefix of
    exactly one tier word counts for that tier (best logprob wins)."""
    out: dict = {}
    for cand in top:
        tok = str(cand.get("token", "")).strip(" \"").lower()
        lp = cand.get("logprob")
        if not tok or not _finite(lp):
            continue
        match = [t for t in TIERS if t.startswith(tok)]
        if len(match) == 1:
            out[match[0]] = max(out.get(match[0], -math.inf), float(lp))
    return out


def logprob_ok(resp: dict) -> bool:
    top = tier_token_top(resp)
    return top is not None and bool(tier_logprobs(top))


def logprobs_field_ok(resp: dict) -> bool:
    """The response carries a non-empty logprobs list whose entries have a token and a finite logprob."""
    lp = resp.get("logprobs") if isinstance(resp, dict) else None
    return (isinstance(lp, list) and bool(lp)
            and all(isinstance(e, dict) and isinstance(e.get("token"), str) and _finite(e.get("logprob")) for e in lp))


def logprob_shape(resp: dict) -> dict:
    """Counts and shapes only (never token text): for a coordinator to judge a not-located tier token."""
    lp = resp.get("logprobs") if isinstance(resp, dict) else None
    lp = lp if isinstance(lp, list) else []
    tops = [e.get("top_logprobs") for e in lp if isinstance(e, dict)]
    return {"n_tokens": len(lp), "n_with_top": sum(1 for t in tops if isinstance(t, list) and t),
            "top_len_max": max((len(t) for t in tops if isinstance(t, list)), default=0),
            "has_message": bool(isinstance(resp, dict) and (resp.get("message") or {}).get("content"))}


def c4_precheck_status(resp: dict) -> dict:
    """D-160: no logprobs field (or malformed/non-finite) -> killed by criterion 1 (no_logprobs_field);
    logprobs present but the tier token not located -> parked_for_coordinator (tier_token_not_located,
    counts and shapes only), NOT a kill; else ok."""
    if not logprobs_field_ok(resp):
        return {"c4": "killed", "criterion": 1, "reason": "no_logprobs_field"}
    if not logprob_ok(resp):
        return {"c4": "parked_for_coordinator", "reason": "tier_token_not_located", "shape": logprob_shape(resp)}
    return {"c4": "ok"}


def c4_features_from_response(resp: dict):
    """lp_haiku, lp_sonnet, lp_opus (softmax-renormalised over the three tier tokens), or None when the
    response has no usable logprobs. There is NO fallback to the answer's argmax or hard verdict."""
    top = tier_token_top(resp)
    if top is None:
        return None
    lps = tier_logprobs(top)
    if not lps:
        return None
    raw = [lps.get(t, LOGPROB_FLOOR) for t in TIERS]
    mx = max(raw)
    exps = [math.exp(v - mx) for v in raw]
    z = sum(exps)
    return [math.log(e / z) for e in exps]


def c4_payload(model: str, context: str, prompt: str) -> dict:
    """The unchanged v6 request (local_classifier._payload: rubric, item, schema, thinking off) plus
    logprobs with the top alternatives."""
    from llm_router import local_classifier as lc
    p = lc._payload(model, lc.Assembled(context or "(no context)", prompt))
    p.update({"logprobs": True, "top_logprobs": 20, "keep_alive": "30m"})
    return p


# ---------------------------------------------------------------------------------------------
# ModernBERT gates (PREREG 5, D-129)
# ---------------------------------------------------------------------------------------------

def mb_trigger(results: dict) -> dict:
    """T-MB (CD-5): fire iff no C1/C2/C4 survivor of criteria 1-4, or the best real-DEV AUROC(P(opus),
    T-primary opus) among those survivors is below 0.75. results: {cand: {"survives_1_4", "auroc"}}."""
    cands = {k: v for k, v in results.items() if k in ("C1", "C2", "C4")}
    survivors = sorted(k for k, v in cands.items() if v.get("survives_1_4"))
    pending = sorted(k for k, v in cands.items() if v.get("pending_1_4"))
    if pending and not survivors:  # D-160 nit: no "no survivor" verdict while criteria 1-4 are still pending
        return {"fire": False, "wait": True, "reasons": ["criteria_1_4_pending"], "pending": pending,
                "survivors": [], "best_auroc": None}
    aurocs = [cands[k]["auroc"] for k in survivors if cands[k].get("auroc") is not None]
    best = max(aurocs) if aurocs else None
    reasons = []
    if not survivors:
        reasons.append("no_survivor")
    if best is None or best < MB_BASELINE_AUROC:
        reasons.append("best_auroc_below_0.75")
    return {"fire": bool(reasons), "wait": False, "reasons": reasons, "survivors": survivors, "best_auroc": best,
            "pending": pending}


def system_hf_cache() -> Path:
    return (Path.home() / ".cache" / "huggingface").resolve()


def check_mb_cache(cls_cache) -> Path:
    """The ModernBERT cache directory must be a CLS-v16 directory: never the system HF cache, and HF_HOME
    (if set) must point to it."""
    if not cls_cache:
        raise S.CLSExit(EXIT_MB_GATE, "--cls-cache is required")
    p = Path(cls_cache).resolve()
    sys_cache = system_hf_cache()
    if p == sys_cache or sys_cache in p.parents:
        raise S.CLSExit(EXIT_MB_GATE, "the ModernBERT cache must not be the system HF cache")
    hf_home = os.environ.get("HF_HOME")
    if hf_home and Path(hf_home).resolve() == sys_cache:
        raise S.CLSExit(EXIT_MB_GATE, "HF_HOME is the system HF cache")
    return p


def check_mb_download_log(log_path, cls_cache) -> dict:
    """The download log needs a pre phase (official repo id, 40-hex revision, licence read from the card
    BEFORE the download, cache dir = --cls-cache) and a later post phase with file sha256s."""
    rows = S.read_jsonl(log_path) if log_path and Path(log_path).exists() else []
    pre = next((r for r in rows if r.get("phase") == "pre"), None)
    post = next((r for r in rows if r.get("phase") == "post"), None)
    if not pre or not post:
        raise S.CLSExit(EXIT_MB_GATE, "download log lacks a pre and a post phase")
    if pre.get("repo_id") != MB_REPO or not re.fullmatch(r"[0-9a-f]{40}", str(pre.get("revision", ""))) or not pre.get("licence"):
        raise S.CLSExit(EXIT_MB_GATE, "download log lacks the official repo id, revision sha or licence")
    if Path(str(pre.get("cache_dir", ""))).resolve() != Path(cls_cache).resolve():
        raise S.CLSExit(EXIT_MB_GATE, "download log cache dir differs from --cls-cache")
    if not post.get("files"):
        raise S.CLSExit(EXIT_MB_GATE, "download log lacks file sha256s")
    if rows.index(pre) > rows.index(post) or str(pre.get("utc", "")) > str(post.get("utc", "")):
        raise S.CLSExit(EXIT_MB_GATE, "the licence must be recorded before the download")
    return {"pre": pre, "post": post}


def hf_model_licence(repo: str, revision: str) -> str:  # pragma: no cover (network; never runs in tests)
    from huggingface_hub import model_info
    info = model_info(repo, revision=revision)
    return str((getattr(info, "card_data", None) or {}).get("license") or "")


def hf_snapshot_download(repo: str, revision: str, local_dir: str):  # pragma: no cover (network; never in tests)
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id=repo, revision=revision, local_dir=local_dir)


def download_modernbert(*, confirm_d129: bool, revision, cls_cache, log_path) -> dict:
    """The only download code; it never runs by default. Refuses without --confirm-d129 and a full
    --revision sha, writes the licence BEFORE the first byte and the file sha256s after, runs once."""
    if not confirm_d129 or not revision:
        raise S.CLSExit(EXIT_MB_GATE, "download-modernbert needs --confirm-d129 and --revision")
    if not re.fullmatch(r"[0-9a-f]{40}", str(revision)):
        raise S.CLSExit(EXIT_MB_GATE, "--revision must be a full commit sha")
    cache = check_mb_cache(cls_cache)
    if any(r.get("phase") == "post" for r in S.read_jsonl(log_path)):
        raise S.CLSExit(EXIT_MB_GATE, "the one approved download has already run")
    lic = hf_model_licence(MB_REPO, revision)
    S.append_jsonl(log_path, {"phase": "pre", "repo_id": MB_REPO, "revision": revision, "licence": lic,
                              "cache_dir": str(cache), "utc": S.utc_now()})
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    hf_snapshot_download(MB_REPO, revision, str(cache))
    files = hash_dir_files(cache)
    S.append_jsonl(log_path, {"phase": "post", "files": files, "utc": S.utc_now()})
    return {"licence": lic, "files": len(files)}


def modernbert_tokenizer(cache_dir, auto_tokenizer=None):
    """The C3 tokenizer, offline, RIGHT-side truncation (CD-18, D-169). Inputs arrive already fitted to 512
    tokens by model_input (digest whole, prompt tail kept), so this truncation never fires on them; a
    left-side truncation over the whole string would cut the digest first."""
    if auto_tokenizer is None:  # pragma: no cover (needs transformers + the download)
        os.environ["HF_HUB_OFFLINE"] = "1"
        from transformers import AutoTokenizer as auto_tokenizer
    return auto_tokenizer.from_pretrained(str(cache_dir), local_files_only=True, truncation_side="right")


def hf_token_counter(tok):
    """count_tokens for model_input under an HF tokenizer: the exact encoded length, special tokens included."""
    return lambda text: len(tok(text, add_special_tokens=True)["input_ids"])


def modernbert_oof(rows, texts, cache_dir, *, k: int = FOLDS, tok=None):  # pragma: no cover (needs torch + the download)
    """C3: fine-tune ModernBERT-base per outer fold (5-fold OOF) and once on all DEV. lr 2e-5, 4 epochs,
    batch 16, inputs fitted to 512 tokens by model_input (digest whole, prompt tail kept; CD-18), seed
    20261017, two ordinal heads with the C1 weights; MPS if available else CPU. Hyper-parameters are
    constants, not options. Not exercised by the offline tests (no torch there)."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from transformers import AutoModel
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    tok = tok if tok is not None else modernbert_tokenizer(cache_dir)

    def train(idx):
        torch.manual_seed(SEED)
        enc = AutoModel.from_pretrained(str(cache_dir), local_files_only=True).to(device)
        head = torch.nn.Linear(enc.config.hidden_size, 2).to(device)
        opt = torch.optim.AdamW(list(enc.parameters()) + list(head.parameters()), lr=MB_LR)
        order = list(idx)
        rng = random.Random(SEED)
        for _epoch in range(MB_EPOCHS):
            rng.shuffle(order)
            for s in range(0, len(order), MB_BATCH):
                b = order[s:s + MB_BATCH]
                batch = tok([texts[i] for i in b], truncation=True, max_length=TOKEN_CAP, padding=True,
                            return_tensors="pt").to(device)
                logits = head(enc(**batch).last_hidden_state[:, 0])
                y = torch.tensor([list(map(float, head_targets(rows[i]["truth"]))) for i in b], device=device)
                w = torch.tensor([[head_sample_weight(rows[i]["w_pool"], rows[i]["truth"], r) for r in (1, 2)] for i in b],
                                 device=device)
                loss = (torch.nn.functional.binary_cross_entropy_with_logits(logits, y, reduction="none") * w).mean()
                opt.zero_grad()
                loss.backward()
                opt.step()
        return enc, head

    def predict(model, idx):
        enc, head = model
        out = []
        with torch.no_grad():
            for s in range(0, len(idx), MB_BATCH):
                b = idx[s:s + MB_BATCH]
                batch = tok([texts[i] for i in b], truncation=True, max_length=TOKEN_CAP, padding=True,
                            return_tensors="pt").to(device)
                p = torch.sigmoid(head(enc(**batch).last_hidden_state[:, 0])).cpu().tolist()
                out += [assemble_probs(a, c) for a, c in p]
        return out

    oof = {}
    for f in range(k):
        tr = [i for i, r in enumerate(rows) if r["fold"] != f]
        te = [i for i, r in enumerate(rows) if r["fold"] == f]
        if tr and te:
            for i, p in zip(te, predict(train(tr), te)):
                oof[i] = p
    final = train(list(range(len(rows))))
    torch.save(final[1].state_dict(), str(Path(cache_dir).parent / "c3_head.pt"))
    return [{"item_id": rows[i]["item_id"], "fold": rows[i]["fold"], "group": rows[i]["group"],
             "p_haiku": p["haiku"], "p_sonnet": p["sonnet"], "p_opus": p["opus"], "confidence": confidence(p)}
            for i, p in sorted(oof.items())]


# ---------------------------------------------------------------------------------------------
# Reference arms (PREREG 2.2); cls_eval arm names
# ---------------------------------------------------------------------------------------------

ARM_KW1 = "nimble"       # K-W1 nimble:9b (cls_eval VALIDITY_ARMS name)
ARM_KB1 = "qwen3coder"   # K-B1 qwen3-coder:30b
ARM_ENGINE = "engine_alone"


def arm_constant(ids, tier) -> dict:
    return {i: tier for i in ids}


def arm_random(ids, seed: int = SEED) -> dict:
    rng = random.Random(seed)
    return {i: rng.choice(TIERS) for i in ids}


def arm_noised_oracle(ids, truth: dict, seed: int = SEED, rate: float = 0.25) -> dict:
    """Truth with 25 percent symmetric rank-1 flips (one tier up or down; at an end the only direction)."""
    rng = random.Random(seed)
    out = {}
    for i in ids:
        t = truth.get(i)
        if t is None:
            out[i] = None
            continue
        r = RANK[t]
        if rng.random() < rate:
            r = rng.choice([x for x in (r - 1, r + 1) if 0 <= x <= 2])
        out[i] = TIERS[r]
    return out


def read_rules_tier(path, run_path) -> tuple[dict, dict]:
    """rules_tier.jsonl (r2-9): header {"schema": "rules_tier/1", resolver_commit, produced_utc}, then one
    line per item. Refused (exit 9) without the header or without a matching PINS-RUN sha; refused (exit 5)
    when a row's haiku_live contradicts record_utc >= 2026-10-09T00:00:00Z (synthetic: true)."""
    header = P.rules_tier_header(path)
    require_run_pin(path, run_path)
    live_from = parse_utc(HAIKU_LIVE_FROM)
    rows = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines()[1:]:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("rules_tier") not in TIERS:
            raise S.CLSExit(S.EXIT_REFUSE, "rules_tier row has no valid tier: %s" % r.get("item_id"))
        rec = parse_utc(r.get("record_utc"))
        want = True if rec is None else rec >= live_from
        if bool(r.get("haiku_live")) != want:
            raise S.CLSExit(S.EXIT_REFUSE, "rules_tier haiku_live contradicts record_utc: %s" % r.get("item_id"))
        rows[r["item_id"]] = r
    return header, rows


def rules_arms(rows: dict, ids) -> tuple[dict, dict]:
    """rules_eff always lifts haiku to sonnet; rules_unlifted is the resolver tier for haiku_live items
    and equals rules_eff for earlier items (the live rules never served haiku before 10-09)."""
    eff, unl = {}, {}
    for i in ids:
        r = rows.get(i)
        if r is None:
            eff[i] = unl[i] = None
            continue
        tier = r["rules_tier"]
        lifted = "sonnet" if tier == "haiku" else tier
        eff[i] = lifted
        unl[i] = tier if r.get("haiku_live") else lifted
    return eff, unl


def verify_engine_pin(static_doc: dict, engine_module_path) -> None:
    """E-0 runs the frozen engine: the engine module on the import path must hash to the pinned sha."""
    want = static_doc["pins"]["engine"]["engine_py_sha256"]
    if S.sha256_file(engine_module_path) != want:
        raise S.CLSExit(S.EXIT_PINS, "engine module differs from the pinned commit")


def engine_alone_tier(text: str) -> str | None:  # pragma: no cover (loads the engine's lexical artifact)
    """E-0: the engine's L1 + L3 lexical tier through the shadow path (_decide_light: no capability
    detector, no cache, no network)."""
    from llm_router import engine
    try:
        d = engine._decide_light(text, door="router", lexical=True)
    except Exception:  # noqa: BLE001 (an engine error is a None prediction, counted)
        return None
    return "haiku" if d.tier == "local" else (d.tier if d.tier in TIERS else None)


# ---------------------------------------------------------------------------------------------
# Latency (PREREG 10.1)
# ---------------------------------------------------------------------------------------------

def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(s)))
    return s[k - 1]


def bootstrap_p95_ci(values: list[float], B: int = 2000, seed: int = SEED) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    ps = sorted(percentile([values[rng.randrange(n)] for _ in range(n)], 95) for _ in range(B))
    return ps[int(0.025 * (B - 1))], ps[int(math.ceil(0.975 * (B - 1)))]


def stratified_sample(items: list[dict], n: int, seed: int = SEED) -> list[dict]:
    """Exactly min(n, len(items)) items stratified by length band with largest-remainder allocation (ties by
    band order), seed 20261017."""
    rng = random.Random(seed)
    by = {b: sorted((x for x in items if x["band"] == b), key=lambda x: x["item_id"]) for b in BANDS}
    total = len(items)
    n = min(n, total)
    if n <= 0:
        return []
    quota = {b: n * len(by[b]) / total for b in BANDS}
    alloc = {b: int(math.floor(quota[b])) for b in BANDS}
    for b in sorted(BANDS, key=lambda b: (-(quota[b] - alloc[b]), BANDS.index(b)))[:n - sum(alloc.values())]:
        alloc[b] += 1
    out = []
    for b in BANDS:
        pool = list(by[b])
        rng.shuffle(pool)
        out += pool[:alloc[b]]
    return out


def latency_summary(blocks: list[list[float]], warmups: list[float], cold_ms: float | None, B: int = 2000) -> dict:
    """Pooled p50/p95/p99 over the timed calls of the blocks (warm-ups EXCLUDED and reported separately),
    bootstrap CI on the pooled p95, the worst block's p95, the numbers of calls and blocks."""
    pooled = [v for b in blocks for v in b]
    lo, hi = bootstrap_p95_ci(pooled, B=B)
    return {"n_calls": len(pooled), "n_blocks": len(blocks), "p50_ms": percentile(pooled, 50),
            "p95_ms": percentile(pooled, 95), "p99_ms": percentile(pooled, 99), "p95_ci_ms": [lo, hi],
            "worst_block_p95_ms": max(percentile(b, 95) for b in blocks), "n_warmups": len(warmups),
            "warmup_ms": list(warmups), "cold_first_load_ms": cold_ms}


def refuse_unmeasured_candidate(name: str) -> None:
    """Codex and OpenRouter candidates are not sync candidates and are not measured."""
    if re.match(r"(?i)^(codex|openrouter|gpt)", str(name)):
        raise S.CLSExit(S.EXIT_REFUSE, "candidate %s is not a sync candidate and is not measured" % name)


def blocks_spaced(previous_starts: list[datetime.datetime], now: datetime.datetime, gap_s: int = BLOCK_GAP_S) -> bool:
    return all((now - p).total_seconds() >= gap_s for p in previous_starts)


# ---------------------------------------------------------------------------------------------
# Arms table (the exact B1 input), criteria (PREREG 8), finalists (9)
# ---------------------------------------------------------------------------------------------

FORBIDDEN_KEYS = {"text", "prompt", "digest", "reason", "content", "canonical_text", "raw"}


def find_text_key(obj) -> str | None:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
                    return k.lower()
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def validate_arms_record(rec: dict) -> None:
    bad = find_text_key(rec)
    if bad:
        raise S.CLSExit(S.EXIT_REFUSE, "arms-table record carries a text key: %s" % bad)


def fallback_scores(rules_tier: str | None) -> dict:
    """Scores for an item a candidate could not score (error/refusal): they decode, at every grid point of
    PREREG 7.1, to the rules_eff tier, so the item is scored as rules_eff's tier (6.2) inside cls_eval fit.
    The item's pred stays None, so cls_eval counts it as a fallback."""
    t = rules_tier if rules_tier in ("sonnet", "opus") else "sonnet"
    return {"p_haiku": 0.001, "p_sonnet": 0.998 if t == "sonnet" else 0.001, "p_opus": 0.998 if t == "opus" else 0.001}


def exclude_error_rows(rows: list[dict], scored_ids: set) -> list[dict]:
    """The table rows a candidate actually scored (its fallback/error items removed), for criterion 3."""
    return [r for r in rows if r["item_id"] in scored_ids]


def build_arms_row(item: dict, *, truths: dict, preds: dict, scores: dict, weight: float) -> dict:
    vals = [t for t in truths.values() if t]
    contested = (len(set(vals)) > 1) if len(vals) == len(JUDGES) else None
    syn = item["kind"] == "syn"
    return {"item_id": item["item_id"], "group": item["group"], "kind": item["kind"],
            "requested": None if syn else item.get("requested"),
            "strata": {"length_band": item["band"], "task_type": item.get("task_type"), "tool_need": item.get("tool_need"),
                       "source": item["source_set"], "generator": item.get("generator") if syn else None,
                       "contested": contested},
            "truths": {j: truths.get(j) for j in JUDGES}, "preds": preds, "scores": scores,
            "fold": item["fold"], "weight": weight}


def _row(n: int, status: str, **numbers) -> dict:
    return {"n": n, "name": CRIT_NAMES[n], "status": status, **numbers}


def criterion0(floor: dict) -> dict:
    st = {"NOT_INFORMATIVE": "kill", "PENDING": "pending"}.get(floor["status"], "pass")
    return _row(0, st, floor=floor)


def criterion1(m: dict) -> dict:
    """Hard fit (D-160): the runtime/logprob build unavailable -> kill (runtime); peak resident memory
    (process RSS + Ollama model size) > 12 GiB -> kill (memory); pooled p95 > 15000 ms -> kill (slow beyond
    the async budget); 1500 < p95 <= 15000 with memory and runtime ok -> async_only (a finalist only with
    that label, no sync authority). No latency file yet: pending. `fired` names the conditions."""
    if m.get("runtime_unavailable"):
        return _row(1, "kill", fired=["runtime"], runtime_unavailable=True, reason=m.get("reason"))
    if m.get("runtime_parked"):
        return _row(1, "pending", fired=[], runtime_parked=True, reason=m.get("reason"))
    p95, rss, model_gib = m.get("p95_ms"), m.get("rss_gib"), m.get("model_gib")
    if p95 is None:
        return _row(1, "pending", fired=[], p95_ms=None)
    mem = (rss or 0.0) + (model_gib or 0.0)
    fired = []
    if mem > MEM_MAX_GIB:
        fired.append("memory")
    if p95 > P95_ASYNC_MAX_MS:
        fired.append("slow_beyond_async_budget")
    elif p95 > P95_MAX_MS:
        fired.append("slow")
    status = "kill" if ("memory" in fired or "slow_beyond_async_budget" in fired) else ("async_only" if fired else "pass")
    return _row(1, status, fired=fired, p95_ms=p95, worst_block_p95_ms=m.get("worst_block_p95_ms"),
                peak_mem_gib=mem, rss_gib=rss, model_gib=model_gib, n_calls=m.get("n_calls"),
                n_blocks=m.get("n_blocks"), runtime_unavailable=False, sync_budget_ms=P95_MAX_MS,
                async_budget_ms=P95_ASYNC_MAX_MS)


def criterion2(m: dict) -> dict:
    n, e = m.get("n") or 0, m.get("n_error") or 0
    rate = e / n if n else None
    return _row(2, "pending" if rate is None else ("kill" if rate > ERR_KILL else "pass"), error_rate=rate, n_items=n, n_error=e)


def criterion3(m: dict, with_fallback: dict | None = None) -> dict:
    """Signal on real DEV with fallback (error) items EXCLUDED (D-160): AUROC < 0.65 or its session-bootstrap
    lower bound < 0.55 kills. The AUROC with the fallback items included is reported beside it."""
    wf = {k: (with_fallback or {}).get(k) for k in ("auroc", "ci95", "n")} if with_fallback is not None else None
    pt, ci = m.get("auroc"), m.get("ci95") or [None, None]
    if pt is None:
        return _row(3, "kill", auroc=None, auroc_ci95=ci, basis=m.get("basis"), n_scored=m.get("n"),
                    population="real, fallback items excluded", auroc_with_fallback=wf)
    lo = ci[0]
    kill = pt < AUROC_MIN or lo is None or lo < AUROC_LO_MIN
    return _row(3, "kill" if kill else "pass", auroc=pt, auroc_ci95=ci, basis=m.get("basis"), n_scored=m.get("n"),
                population="real, fallback items excluded", auroc_with_fallback=wf)


def criterion4(m: dict) -> dict:
    """Operating point on the pooled out-of-sample decisions (cls_eval --pooled-oos): an admissible
    (t_hi, t_lo) in every second-level fit AND the bars AND the NOT-TRIVIAL guard AND the non-inferiority
    upper bounds. Point estimates alone never pass."""
    parts = {"admissible": bool(m.get("admissible")), "bars": bool(m.get("meets_bars")),
             "guard": bool((m.get("guard") or {}).get("passes")), "non_inferior": bool((m.get("non_inf") or {}).get("ok"))}
    return _row(4, "pass" if all(parts.values()) else "kill", parts=parts, under=m.get("under"), cost=m.get("cost"),
                guard_detail=m.get("guard"), non_inf_detail=m.get("non_inf"), failed_fold=m.get("failed_fold"))


def criterion5(m: dict) -> dict:
    """Cost: informative, never gating (D-130 c). A ratio above 1.35 to R-E raw cost prints COST_HIGH."""
    ratio = m.get("ratio")
    return _row(5, "report_only", raw_cost=m.get("raw"), rules_eff_raw_cost=m.get("raw_re"), ratio=ratio,
                flag="COST_HIGH" if ratio is not None and ratio > COST_HIGH_RATIO else None)


def criterion6(m: dict, *, low_cluster: bool, resemblance_failed: bool) -> dict:
    """Shift (r3-4): would-kill iff significantly worse than R-E on the real rows of the pooled OOS
    decisions (CI lower bound > 0 on either metric) while the DEV-SYN rows meet criterion 4's conditions.
    Report-only under LOW CLUSTER FIDELITY; no weight when DEV-SYN is RESEMBLANCE_FAILED."""
    real, syn = m.get("real") or {}, m.get("syn") or {}
    worse = bool(real.get("significantly_worse"))
    syn_meets = bool(syn.get("meets_criterion4_conditions"))
    would = worse and syn_meets
    report_only = bool(low_cluster or resemblance_failed)
    status = "report_only" if report_only else ("kill" if would else "pass")
    return _row(6, status, would_kill=would, worse_on_real=worse, syn_meets_criterion4=syn_meets,
                low_cluster_fidelity=bool(low_cluster), resemblance_failed=bool(resemblance_failed),
                real=real or None, syn=syn or None)


def criterion7(m: dict) -> dict:
    share = m.get("identical_share")
    if share is None:
        return _row(7, "pending", identical_share=None)
    return _row(7, "kill" if share < STABILITY_MIN else "pass", identical_share=share, n_compared=m.get("n"))


def _robust_leg_status(m, voided: list, pair: tuple) -> dict:
    """not_evaluable (passing) ONLY when a judge of the pair is voided (r3-2, D-160); any other
    non-evaluable leg is pending (blocks eligibility)."""
    v = [j for j in voided if j in pair]
    if v:
        return {"status": "not_evaluable", "voided": v}
    if not m or any(not isinstance(m.get(k), dict) or m[k].get("evaluable") is False for k in ("under", "cost")):
        return {"status": "pending", "detail": m}
    discordant = sum(bool(m[k].get("discordant")) for k in ("under", "cost"))
    return {"status": "kill" if discordant > 1 else "pass", "discordant": discordant, "detail": m}


def syn_leg_status(syn: dict | None, voided_syn, syn_rows_present: bool) -> dict:
    """The DEV-SYN leg (T-gpt vs T-sonnet). not_applicable ONLY when the table has no DEV-SYN rows at all
    (F20); DEV-SYN rows present but the candidate's arm missing from cls_eval's syn leg -> pending."""
    if not syn_rows_present:
        return {"status": "not_applicable", "note": "no DEV-SYN rows in the table"}
    if syn is None:
        return {"status": "pending", "note": "DEV-SYN rows present but cls_eval's syn leg lacks this arm"}
    return _robust_leg_status(syn, list(voided_syn), ("gpt", "sonnet"))


def criterion8(m: dict | None, *, voided: list, syn: dict | None = None, voided_syn=(),
               syn_rows_present: bool | None = None) -> dict:
    """Judge robustness through cls_eval's 1-SE floor (judge_robustness: a sign counts only when the paired
    difference is at least one bootstrap SE from 0). Real leg: T-opus vs T-gpt; DEV-SYN leg: T-gpt vs
    T-sonnet. A leg kills when its signs are discordant on more than 1 of the 2 metrics; a leg whose judge
    is voided is not evaluable (r3-2). Nothing here recomputes the floor. assemble always passes
    syn_rows_present; when omitted it is taken as `syn is not None`."""
    real = _robust_leg_status(m, list(voided), ("opus", "gpt"))
    synl = syn_leg_status(syn, voided_syn, syn is not None if syn_rows_present is None else bool(syn_rows_present))
    legs = (real["status"], synl["status"])
    if "kill" in legs:
        status = "kill"
    elif "pending" in legs:
        status = "pending"
    elif "pass" in legs:
        status = "pass"
    else:
        status = "not_evaluable"
    return _row(8, status, real_leg=real, syn_leg=synl, voided=[j for j in voided if j in ("opus", "gpt")])


PASSING = {"pass", "report_only", "not_evaluable", "async_only"}


def criteria_table(rows: list[dict]) -> dict:
    """The kill-criteria table of PREREG section 8: criteria 0-8 IN ORDER, every number reported even after
    an earlier kill. killed_by = the first kill; eligible = no kill and nothing pending."""
    table = sorted(rows, key=lambda r: r["n"])
    killed = next((r["n"] for r in table if r["status"] == "kill"), None)
    eligible = killed is None and all(r["status"] in PASSING for r in table)
    return {"rows": table, "killed_by": killed, "eligible": eligible,
            "pending": [r["n"] for r in table if r["status"] == "pending"],
            "labels": ["async_only"] if any(r["status"] == "async_only" for r in table) else [], **LABEL_FLAGS}


FAMILY = {"C1": "small_learned_head", "C2": "small_learned_head", "C3": "small_learned_head", "C4": "logprob_llm"}


def rank_survivors(survivors: dict) -> list[str]:
    """Survivors ranked by pooled out-of-sample C-lambda2-clamp, subject to under-route no worse than R-E."""
    ok = {k: v for k, v in survivors.items() if v["under"] <= v["under_re"] + 1e-12}
    return sorted(ok, key=lambda k: (ok[k]["cost"], k))


def select_finalists(survivors: dict) -> dict:
    """Top 2, preferring one logprob/LLM arm and one small learned head when both families survive. The
    candidate the diversity rule displaces is named with its rank and numbers (CD-13)."""
    ranked = rank_survivors(survivors)
    top2 = ranked[:2]
    displaced = None
    if len(ranked) > 2 and len({FAMILY.get(c) for c in top2}) == 1:
        other = next((c for c in ranked[2:] if FAMILY.get(c) not in {FAMILY.get(top2[0])}), None)
        if other is not None:
            displaced = {"candidate": top2[1], "rank": 2, "numbers": survivors[top2[1]],
                         "replaced_by": other, "replaced_by_rank": ranked.index(other) + 1}
            top2 = [top2[0], other]
    return {"ranked": ranked, "finalists": top2, "displaced": displaced}


def arm_choice_items(rows: list[dict]) -> list[dict]:
    """The rows on which arm A vs arm B is chosen: real items outside the engine's fitting sets (GE3 tune,
    E2-cal) plus DEV-SYN. Engine-seen rows are only reported."""
    return [r for r in rows if r["strata"]["source"] not in ENGINE_SEEN_SOURCES]


def engine_seen_items(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["strata"]["source"] in ENGINE_SEEN_SOURCES]


def arm_b_preds(rows: list[dict], cand: str, t_conf: float) -> dict:
    """Arm B: the engine tier (E-0) unless L2 confidence >= t_conf, then the L2 tier (the pooled OOS
    decision). A missing engine tier is None (scored as rules_eff by cls_eval)."""
    out = {}
    for r in rows:
        sc = r["scores"].get(cand)
        conf = max(sc["p_haiku"], sc["p_sonnet"], sc["p_opus"]) if sc else 0.0
        out[r["item_id"]] = r["preds"].get(cand) if (r["preds"].get(cand) and conf >= t_conf) else r["preds"].get(ARM_ENGINE)
    return out


def objective_key(m: dict) -> tuple:
    """PREREG 7.2 objective order: C-lambda2-clamp, then opus share, then raw cost."""
    return (m["cost"], m["opus_share"], m["raw_cost"])


def choose_arm(metrics_a: dict, metrics_b: dict | None) -> str:
    """The better of arm A and arm B by the deterministic objective; ties go to arm A."""
    if metrics_b is None or objective_key(metrics_a) <= objective_key(metrics_b):
        return "A"
    return "B"


def stability_share(dec_a: dict, dec_b: dict) -> tuple[float | None, int]:
    ids = sorted(set(dec_a) & set(dec_b))
    if not ids:
        return None, 0
    return sum(1 for i in ids if dec_a[i] == dec_b[i]) / len(ids), len(ids)


def peak_rss_gib() -> float:
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (r if sys.platform == "darwin" else r * 1024) / float(1 << 30)
