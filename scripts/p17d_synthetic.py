#!/usr/bin/env python3
"""P1.7-d synthetic, steps 2-6 (docs/prereg/PREREG-P17D-synthetic.md, FROZEN; harness design
docs/prereg/P17D-synthetic-HARNESS-DESIGN.md). Built after the freeze; its commit is HARNESS_SHA.

    python3 scripts/p17d_synthetic.py select  [--run-dir DIR]
    python3 scripts/p17d_synthetic.py shadow  [--run-dir DIR]
    python3 scripts/p17d_synthetic.py label   [--run-dir DIR] [--usage FILE]
    python3 scripts/p17d_synthetic.py score   [--run-dir DIR] --p17c passed|not_passed
    python3 scripts/p17d_synthetic.py status  [--run-dir DIR]

``$RUN`` is ``--run-dir``, else ``$P17D_RUN_DIR``, else ``~/.rsi/research/primary-plan/v16/p17d`` (real home).
Every command except ``status`` refuses unless ``check_prereg.check(PREREG)["match"]`` and
``frozen_utc`` < now, and while ``use.log`` holds a ``terminal`` line. Stdout is one JSON object of counts
and ids; no prompt, context or judge reason is ever printed.

Exit: 0 done, 1 terminal (use consumed, NOT INFORMATIVE), 2 refused, 3 paused / parked (resumable, nothing
written), 4 judge was not Opus (terminal), 5 spend cap reached (nothing terminal).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pwd
import random
import re
import shlex
import subprocess
import sys
import time
import types
import urllib.request
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)  # never $HOME: a scratch HOME must not move $RUN
PP = REAL_HOME / ".rsi" / "research" / "primary-plan"
DEFAULT_RUN = PP / "v16" / "p17d"
RUN_ENV = "P17D_RUN_DIR"
PREREG = ROOT / "docs" / "prereg" / "PREREG-P17D-synthetic.md"
ATTACH_SCRIPT = ROOT / "scripts" / "p17d_attach.py"
MEASUREMENT = ROOT / "docs" / "measurements" / "p17d-synthetic.json"
GT_SCRIPTS = PP / "v16" / "gt500" / "scripts"
SHADOWLAB_PY = PP / "v16" / "shadowlab" / "shadowlab.py"
GE3_LABELS = PP / "v16" / "gt500" / "work" / "labels_opus.jsonl"
#: The quota file the GE3 / shadowlab guards read (PREREG-shadowlab section 6). Read-only; the one path under
#: ~/.llm-router this harness opens (D-110: solely for the section 8 quota pause).
USAGE_JSON = REAL_HOME / ".llm-router" / "usage.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- pins (prereg ```pins``` block)
# tests/test_p17d_synthetic.py parses the frozen block and fails on any difference.
LABEL = "synthetic (D-99)"
FRAME_SHA256 = "3f5d96eee34ed72b90f62ab462b8f452b6ca2d465c0beb6158dcb94b4a85b7f4"
WINDOW_END_UTC = "2026-10-08T20:46:00Z"
SESSION_KIND = "organic"
SELECT_SEED = 20261015
N_SELECT = 150
SELECT_MAX_SHARE = 0.50
ADMIT_TRIGGER_F_LT = 100
ADMIT_SET = ("ge3_heldout", "organic", "claude", "not_p_eval_cal")
ADMIT_SEED_FORMAT = "p17d:admit:20261015:{ge3_id}"
N_SELECT_ADMIT = 250
ADMIT_REUSE_GE3_LABELS = False
TIER_POLICY_SHA256 = "04b5f4641bcc8dd7afee38c869950e82eed00533b227d4d2f97af884ac42be93"
ITEMS_SHA256 = "4dfb2a1132f759198da4a7548001b9547418ec2140aafd90c9bf55a81de16041"
ATTACH_COUNTS_SHA256 = "662c8073f1bc482f66565326b46ae6d91ec018ecd5ee8f23d0b0c74cb984d4ca"
ITEM_ID_FORMAT = "p17d:item:{session_id}:{record_timestamp}"
SHADOW_RECORD_RULE = "last_per_key"
ENGINE_MODE = "shadow"
REPO_SHA = "64e3c84cbf0a3160d134a9f3ba19035bf1f1b3e8"
ATTACH_SCRIPT_SHA256 = "9d9ffbe41301efb0aebc914f8b332a4a3a2b938bdcad8144e07f423dbc99e046"
FEASIBILITY_MIN_SELECTED = 100
FEASIBILITY_MAX_SHARE = 0.55
GT500_SHA256 = "9cde774dfe93387d6ee3dfcbd1b5481a537f265e5961d82f85a87d29d6328959"
SUPPLY_SHA256 = "12cd3e1eed7fc15658b4b44246d83422e2b61adc4ccb270dd9f5e729608fb0e8"
SHADOWLAB_SHA256 = "0d2d2b58bde9cb8a5466782da866c218a244595d1485ef8d2563ac8c02fb4def"
RUBRIC_MD5 = "1bcb3f441a0c9d056b3343a9d47c5565"
SHADOW_EVAL_SHA256 = "e9b89943f33b68b3095c5d2ea57976147368e0e6f28490aea7e74a82e32b1c57"
NIMBLE_BLOB = "sha256-bbf1d6fc03bb0ed24d88f4c214ed7b5d1768aeb43d5cf433fb69eff0c8578013"
NIMBLE_TIMEOUT_MS = 2000
NIMBLE_KEEP_ALIVE = "10m"
NIMBLE_RETRIES = 1
LABEL_BATCH = 8
LABEL_SEED_FORMAT = "p17d:label:20261015:{item_id}"
CALIBRATION_REQUIRED = "6/6"
SPEND_CAP_USD = 10.00
BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 20261003
MIN_N = 100
MAX_SESSION_SHARE = 0.60
MAX_ERROR_SHARE = 0.10
COST = {"haiku": 1.0, "sonnet": 2.7, "opus": 6.15}
LAMBDA = 2

# ----------------------------------------------------------------------------- prereg text constants (not pinned)
SELECT_SEED_FORMAT = "p17d:select:20261015:{item_id}"        # section 2 "Selection"
SELECT_SHARE_FLOOR = 2                                        # max(2, floor(0.50 x (N + 1)))
CALIBRATION_N = 6                                             # the planted items of rubric.py
CALIBRATION_REAL_ITEMS = 2                                    # "plus the first 2 real items in label order"
CALIBRATION_SHUFFLE_SEED = 20261015                           # section 6
LABEL_BATCH_RETRIES = 3                                       # "retried up to 3 times, then one at a time"
LABEL_SINGLE_ATTEMPTS = 1                                     # not stated in the prereg: one call per single item
OPAQUE_ID_FORMAT = "L{n:03d}"                                 # "opaque ids L001..."
SPEND_FIRST_EST_USD = 0.30                                    # section 8 spend guard
SPEND_EST_FACTOR = 1.25
JUDGE_MODEL = "opus"
JUDGE_TIMEOUT_S = 900
NIMBLE_MODEL = "nimble:9b"
NIMBLE_BLOB_SIZE = 9527501312                                 # section 4 "size 9,527,501,312 B"
OLLAMA_URL = "http://localhost:11434"                         # localhost only
QUIET_FREE_MEM_MIN_PCT = 40.0                                 # amend5 section 6.2 as section 4 quotes it
QUIET_LOAD1_MAX = 4.0                                         # "load1 under 4" (float, strict)
COLD_WAIT_S = 120.0                                           # "a wait of up to 120 s for /api/ps"
COLD_POLL_S = 2.0
TIMEOUT_COOLDOWN_S = 30.0                                     # local_classifier.COOLDOWN_S
PROXY_ERA_UTC = "2026-09-28T14:41:00Z"                        # strata: era
TRUTH_OPUS_MIN = 10                                           # section 7 design validity check
WIDE_SESSIONS_LT = 30                                         # statistics rule 1
Z95 = 1.959964
MISMATCH = "assemble_mismatch"
NO_RECORD = "no_record"                                       # scheduled, but the production seam wrote nothing
STUB_TOOL = {"name": "p17d_stub", "description": "stub", "input_schema": {"type": "object", "properties": {}}}

EXIT_OK, EXIT_TERMINAL, EXIT_REFUSED, EXIT_PAUSED, EXIT_NOT_OPUS, EXIT_SPEND_CAP = 0, 1, 2, 3, 4, 5
PASS, FAIL, NOT_INFORMATIVE = "PASS (synthetic, D-99)", "FAIL", "NOT INFORMATIVE"
HARNESS_RE = re.compile(r"^harness (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) sha=([0-9a-f]{7,40})$")


DISCLOSURES = [
    "D-110: label opens ~/.llm-router/usage.json read-only, solely for the section 8 quota pause; "
    "the harness opens no other ~/.llm-router path."
]


def _load_verified_attach(name: str, path: Path):
    raw = path.read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != ATTACH_SCRIPT_SHA256:
        print(f"refusing: p17d_attach.py sha256 {got} != pinned {ATTACH_SCRIPT_SHA256}", file=sys.stderr)
        raise SystemExit(2)
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    sys.modules[name] = mod
    exec(compile(raw, str(path), "exec"), mod.__dict__)
    return mod


pa = _load_verified_attach("_p17d_attach_helpers", ATTACH_SCRIPT)
check_prereg = _load("_p17d_check_prereg", ROOT / "scripts" / "check_prereg.py")
Refuse = pa.Refuse
sha256_file = pa.sha256_file
read_jsonl = pa.read_jsonl


def default_pins() -> dict:
    return {"items": ITEMS_SHA256, "attach_counts": ATTACH_COUNTS_SHA256, "attach_script": ATTACH_SCRIPT_SHA256,
            "tier_policy": TIER_POLICY_SHA256, "shadow_eval": SHADOW_EVAL_SHA256, "gt500": GT500_SHA256,
            "supply": SUPPLY_SHA256, "shadowlab": SHADOWLAB_SHA256}


def git_head() -> str:
    p = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    return p.stdout.strip() if p.returncode == 0 else ""


def repo_src_matches(sha: str) -> bool:
    """The router code under test (``src/llm_router``, working tree included) equals REPO_SHA."""
    p = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", sha, "--", "src/llm_router"],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return p.returncode == 0


def repo_tree_clean() -> bool:
    p = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "scripts/", "src/"],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return p.returncode == 0 and not p.stdout.strip()


@dataclass
class Cfg:
    run: Path
    prereg: Path = PREREG
    attach_script: Path = ATTACH_SCRIPT
    measurement: Path = MEASUREMENT
    gt_scripts: Path = GT_SCRIPTS
    shadowlab_py: Path = SHADOWLAB_PY
    ge3_labels: Path = GE3_LABELS
    usage: Path = USAGE_JSON
    pins: dict = field(default_factory=default_pins)
    repo_sha: str = REPO_SHA
    now: object = time.time
    head_sha: object = git_head
    src_matches: object = repo_src_matches
    tree_clean: object = repo_tree_clean
    asleep: object = asyncio.sleep

    def p(self, name: str) -> Path:
        return self.run / name

    @property
    def items(self): return self.p("items.jsonl")
    @property
    def attach_counts(self): return self.p("attach_counts.json")
    @property
    def tier_policy(self): return self.p("tier_policy.yaml")
    @property
    def selection(self): return self.p("selection.json")
    @property
    def scratch(self): return self.p("scratch")
    @property
    def shadow(self): return self.p("classifier_shadow.jsonl")
    @property
    def shadow_meta(self): return self.p("shadow_meta.json")
    @property
    def labels(self): return self.p("labels.jsonl")
    @property
    def labels_planted(self): return self.p("labels_planted.jsonl")
    @property
    def judgements(self): return self.p("judgements.jsonl")
    @property
    def ledger(self): return self.p("ledger.jsonl")
    @property
    def use_log(self): return self.p("use.log")
    @property
    def result(self): return self.p("result.json")
    @property
    def claude_cwd(self): return self.p("claude_cwd")


# ----------------------------------------------------------------------------- io
def utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def append_line(path: Path, line: str) -> None:
    """Append one line (0600, fsync). Used for use.log, the label files and the ledger: never rewritten."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def append_json(path: Path, obj: dict) -> None:
    append_line(path, json.dumps(obj, ensure_ascii=False, sort_keys=True))


def write_json_private(path: Path, obj: dict, *, exclusive: bool = False) -> None:
    pa.write_private(path, (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode("utf-8"), exclusive=exclusive)


def touch_private(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def json_rows(path: Path) -> list:
    """Every JSON object line; a missing file is empty; a bad line is skipped."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        return []
    return out


def line_count(path: Path) -> int:
    """Non-empty lines, never parsed (status must not read ``truth``)."""
    try:
        with open(path, "rb") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


def emit(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True))


# ----------------------------------------------------------------------------- use.log and refusals
def use_lines(cfg: Cfg) -> list[str]:
    try:
        return [x for x in cfg.use_log.read_text(encoding="utf-8").splitlines() if x.strip()]
    except FileNotFoundError:
        return []


def has_terminal(cfg: Cfg) -> bool:
    return any(x.startswith("terminal ") for x in use_lines(cfg))


def has_started(cfg: Cfg) -> bool:
    return any(x.startswith("started ") for x in use_lines(cfg))


def harness_line_ok(cfg: Cfg) -> bool:
    """The first use.log line is ``harness <UTC> sha=<HARNESS_SHA>`` and names the commit this file runs from."""
    lines = use_lines(cfg)
    m = HARNESS_RE.match(lines[0]) if lines else None
    if not m:
        return False
    head = cfg.head_sha() or ""
    return head.startswith(m.group(2))


def label_finished(cfg: Cfg) -> bool:
    return any(x.startswith(("label done ", "label stopped spend_cap ")) for x in use_lines(cfg))


def check_code_identity(cfg: Cfg) -> None:
    if not harness_line_ok(cfg):
        raise Refuse("refusing: use.log's first line is not `harness <UTC> sha=<HARNESS_SHA>` for this commit")
    if not cfg.tree_clean():
        raise Refuse("refusing: uncommitted changes under scripts/ or src/ (git status --porcelain)")


def terminal(cfg: Cfg, stage: str, reason: str) -> None:
    """Consume the use (section 10): the terminal use-log record and the NOT INFORMATIVE gate file."""
    append_line(cfg.use_log, f"terminal {utc(cfg.now())} {reason}")
    write_json_private(cfg.result, {"label": LABEL, "verdict": NOT_INFORMATIVE, "stage": stage, "reason": reason,
                                    "utc": utc(cfg.now()), "disclosures": DISCLOSURES})


def check_common(cfg: Cfg, *, policy: bool = False) -> dict:
    """Refusals shared by select/shadow/label/score: frozen prereg, frozen_utc in the past, every pin."""
    try:
        res = check_prereg.check(cfg.prereg)
    except (OSError, ValueError, IndexError) as exc:
        raise Refuse(f"refusing: prereg freeze record unreadable ({type(exc).__name__})") from None
    if not res["match"]:
        raise Refuse("refusing: the prereg sha256 differs from its freeze record")
    if not parse_utc(res["frozen_utc"]) < cfg.now():
        raise Refuse("refusing: frozen_utc is not in the past")
    if has_terminal(cfg):
        raise Refuse("refusing: use.log holds a terminal record")
    checks = [("attach_script", cfg.attach_script), ("items", cfg.items), ("attach_counts", cfg.attach_counts)]
    if policy:
        checks.append(("tier_policy", cfg.tier_policy))
    for name, path in checks:
        try:
            got = sha256_file(path)
        except OSError:
            raise Refuse(f"refusing: {path.name} missing") from None
        if got != cfg.pins[name]:
            raise Refuse(f"refusing: {path.name} sha256 {got} != pinned {cfg.pins[name]}")
    import llm_router
    from llm_router import shadow_eval

    if not pa._inside(Path(llm_router.__file__).resolve().parent, ROOT / "src"):
        raise Refuse(f"refusing: llm_router is not imported from {ROOT / 'src'}")
    if sha256_file(Path(shadow_eval.__file__)) != cfg.pins["shadow_eval"]:
        raise Refuse("refusing: shadow_eval.py sha256 differs from its pin")
    if not cfg.src_matches(cfg.repo_sha):
        raise Refuse(f"refusing: src/llm_router differs from REPO_SHA {cfg.repo_sha[:12]}")
    if (shadow_eval.COST != COST or shadow_eval.LAMBDA != LAMBDA or shadow_eval.BOOT_B != BOOTSTRAP_B
            or shadow_eval.BOOT_SEED != BOOTSTRAP_SEED or shadow_eval.MIN_N != MIN_N
            or shadow_eval.MAX_SESSION_SHARE != MAX_SESSION_SHARE):
        raise Refuse("refusing: shadow_eval constants differ from the prereg pins")
    return res


# ----------------------------------------------------------------------------- body, key, assemble check
def build_body(item: dict) -> dict:
    """The minimal replay body (section 4): system cwd line, earlier prompts, assistant tail, full prompt,
    one custom client tool stub, the requested model id."""
    body = pa.min_body(item.get("cwd") or "", list(item.get("earlier") or []), item.get("assistant_tail") or "",
                       item.get("prompt") or "", model=item.get("requested_model"))
    body["tools"] = [dict(STUB_TOOL)]
    return body


def record_key(body: dict) -> str:
    """``text_sha = prompt_key.key(newest_human_text(body))``: the live key (section 4)."""
    from llm_router.prompt_key import key
    from llm_router.proxy.steps import newest_human_text

    return key(newest_human_text(body))


def check_assemble(item: dict, body: dict | None = None) -> bool:
    """False is ``assemble_mismatch``: attach's strict rebuild check failed, or assemble of the replay body
    does not give the stored attach-time context."""
    from llm_router.proxy import cls_input

    asm = cls_input.assemble(body if body is not None else build_body(item))
    return item.get("assemble_ok") is True and asm.context == item.get("assembled_context")


def item_key(item: dict) -> tuple[str, str]:
    return (item.get("session_id") or "", record_key(build_body(item)))


def load_items(cfg: Cfg) -> dict:
    items = read_jsonl(cfg.items)
    out = {}
    for it in items:
        want = hashlib.sha256(ITEM_ID_FORMAT.format(session_id=it.get("session_id"),
                                                    record_timestamp=it.get("ts")).encode()).hexdigest()[:16]
        if it.get("item_id") != want:
            raise Refuse("refusing: an item_id does not follow ITEM_ID_FORMAT")
        out[it["item_id"]] = it
    return out


def load_selection(cfg: Cfg) -> dict:
    try:
        doc = json.loads(cfg.selection.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refuse("refusing: selection.json missing (run select)") from None
    if doc.get("items_sha256") != cfg.pins["items"]:
        raise Refuse("refusing: selection.json is bound to another items.jsonl")
    return doc


# ----------------------------------------------------------------------------- select (sections 2, 2a)
def seed_order(key_fmt: str, field_name: str):
    return lambda it: hashlib.sha256(key_fmt.format(**{field_name: it[field_name]}).encode()).hexdigest()


def share_cap(total_after: int) -> int:
    """max(2, floor(0.50 x total_after)), exactly."""
    return max(SELECT_SHARE_FLOOR, int(Fraction(str(SELECT_MAX_SHARE)) * total_after))


def walk(pool: list, order_key, counts: Counter, n0: int, n_max: int) -> list:
    """The capped walk: accept when the session's count + 1 <= share_cap(N + 1); repeat until a walk accepts
    nothing or N reaches n_max. Returns the accepted items in acceptance order."""
    pending = sorted(pool, key=order_key)
    counts, n, accepted = Counter(counts), n0, []
    while pending and n < n_max:
        rest, any_ok = [], False
        for it in pending:
            if n < n_max and counts[it["session_id"]] + 1 <= share_cap(n + 1):
                accepted.append(it)
                counts[it["session_id"]] += 1
                n += 1
                any_ok = True
            else:
                rest.append(it)
        pending = rest
        if not any_ok:
            break
    return accepted


def select(items: list) -> dict:
    """F walk to N_SELECT; if fewer than ADMIT_TRIGGER_F_LT F items, G continues the walk (F refusals never
    re-enter) to N_SELECT_ADMIT. Then the feasibility gate."""
    f_pool = [it for it in items if it.get("group") == "F"]
    g_pool = [it for it in items if it.get("group") == "G" and it.get("ge3_id") and it.get("kind") == SESSION_KIND]
    f_sel = walk(f_pool, seed_order(SELECT_SEED_FORMAT, "item_id"), Counter(), 0, N_SELECT)
    admitted = len(f_sel) < ADMIT_TRIGGER_F_LT
    g_sel = []
    if admitted:
        counts = Counter(it["session_id"] for it in f_sel)
        g_sel = walk(g_pool, seed_order(ADMIT_SEED_FORMAT, "ge3_id"), counts, len(f_sel), N_SELECT_ADMIT)
    chosen = f_sel + g_sel
    sess = Counter(it["session_id"] for it in chosen)
    n = len(chosen)
    top = sess.most_common(1)[0][1] if n else 0
    feasible = n >= FEASIBILITY_MIN_SELECTED and Fraction(top, n) <= Fraction(str(FEASIBILITY_MAX_SHARE))
    return {"f_ids": [it["item_id"] for it in f_sel], "g_ids": [it["item_id"] for it in g_sel],
            "admitted": admitted, "n": n, "sessions": len(sess),
            "largest_session_share": round(top / n, 4) if n else None,
            "top3_session_counts": [c for _, c in sess.most_common(3)], "feasible": feasible,
            "pool": {"F": len(f_pool), "G": len(g_pool)}}


def error_gate_ok(errors: int, n: int) -> bool:
    """Errors may not exceed MAX_ERROR_SHARE of the selected items."""
    return n > 0 and Fraction(errors, n) <= Fraction(str(MAX_ERROR_SHARE))


def cmd_select(cfg: Cfg) -> int:
    check_common(cfg)
    if cfg.selection.exists():
        raise Refuse("refusing: selection.json exists; select is single-shot")
    items = load_items(cfg)
    sel = select(list(items.values()))
    ids = sel["f_ids"] + sel["g_ids"]
    mism = [i for i in ids if not check_assemble(items[i])]
    gate_ok = error_gate_ok(len(mism), len(ids))
    ok = sel["feasible"] and gate_ok
    doc = {"label": LABEL, "items_sha256": sha256_file(cfg.items), "seed": SELECT_SEED, "ids": ids, **sel,
           "assemble_mismatch": len(mism), "assemble_mismatch_ids": mism, "error_gate_ok": gate_ok, "ok": ok,
           "utc": utc(cfg.now())}
    write_json_private(cfg.selection, doc, exclusive=True)
    os.chmod(cfg.selection, 0o444)
    if not sel["feasible"]:
        terminal(cfg, "select", f"feasibility insufficient supply n={sel['n']} "
                                f"largest_share={sel['largest_session_share']}")
    elif not gate_ok:
        terminal(cfg, "select", f"early_error_gate assemble_mismatch={len(mism)}/{len(ids)}")
    emit({"step": "select", "n": sel["n"], "n_f": len(sel["f_ids"]), "n_g": len(sel["g_ids"]),
          "admitted": sel["admitted"], "sessions": sel["sessions"],
          "largest_session_share": sel["largest_session_share"], "assemble_mismatch": len(mism),
          "feasible": sel["feasible"], "error_gate_ok": gate_ok, "ok": ok})
    return EXIT_OK if ok else EXIT_TERMINAL


# ----------------------------------------------------------------------------- shadow (section 4)
def load_policy(cfg: Cfg):
    """The live tier policy copy, from the very bytes whose sha256 was checked."""
    import yaml
    from llm_router.proxy.tiers import ClaudeTierPolicy

    raw = cfg.tier_policy.read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg.pins["tier_policy"]:
        raise Refuse("refusing: tier_policy.yaml sha256 differs from its pin")
    data = yaml.safe_load(raw) or {}
    if data.get("complexity_knn"):
        raise Refuse("refusing: complexity_knn is true; its kNN training set was not joined to section 3 "
                     "before the freeze")
    return ClaudeTierPolicy.from_dict(data)


def rules_tier(body: dict, policy, requested_model: str | None) -> tuple:
    """``(task, cx, tier)``: ``(tier_for(*_classify_class(tier_text(body), capabilities=False)) or
    tier_of(requested)).name`` (tiers.py:1031-1032)."""
    from llm_router.proxy import backends
    from llm_router.proxy.steps import tier_text

    task, cx = backends._classify_class(tier_text(body), capabilities=False)
    t = policy.tier_for(task, cx) or policy.tier_of(requested_model)
    return task, cx, (t.name if t else None)


@contextlib.contextmanager
def harness_env(cfg: Cfg):
    """Scratch HOME / LLM_ROUTER_HOME under $RUN/scratch (0700) and the section 4 environment."""
    keep = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("LLM_ROUTER_")}
    keep["HOME"] = os.environ.get("HOME")
    home, lr = cfg.scratch / "home", cfg.scratch / "lr"
    for d in (cfg.scratch, home, lr):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    try:
        for k in list(os.environ):
            if k.startswith("LLM_ROUTER_"):
                del os.environ[k]   # drops ABSTAIN_BELOW (0.0) and SHADOW_TEXT_SAMPLE (sidecar off)
        os.environ.update({
            "HOME": str(home), "LLM_ROUTER_HOME": str(lr), "LLM_ROUTER_ENGINE_MODE": ENGINE_MODE,
            "LLM_ROUTER_LOCAL_CLASSIFIER": "shadow", "LLM_ROUTER_CLASSIFIER_BACKEND": "systemone",
            "LLM_ROUTER_DECISION_MODEL": NIMBLE_MODEL, "LLM_ROUTER_CLASSIFIER_KEEP_ALIVE": NIMBLE_KEEP_ALIVE,
            "LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS": str(NIMBLE_TIMEOUT_MS), "LLM_ROUTER_OLLAMA_URL": OLLAMA_URL,
        })
        yield
    finally:
        for k in list(os.environ):
            if k.startswith("LLM_ROUTER_"):
                del os.environ[k]
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def ollama_modelfile() -> str:
    env = dict(os.environ)
    env["HOME"] = str(REAL_HOME)
    p = subprocess.run(["ollama", "show", NIMBLE_MODEL, "--modelfile"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL, env=env)
    if p.returncode != 0:
        raise Refuse("refusing: `ollama show` failed; the nimble digest cannot be checked")
    return p.stdout


def blob_size(path: str) -> int:
    return os.stat(path).st_size


def free_mem_pct() -> float:
    p = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    m = re.search(r"free percentage:\s*([0-9.]+)%", p.stdout)
    return float(m.group(1)) if m else -1.0


def load1() -> float:
    return os.getloadavg()[0]


def ollama_ps() -> list[str]:
    with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as r:  # localhost only
        data = json.loads(r.read() or b"{}")
    return [m.get("name") or m.get("model") or "" for m in (data or {}).get("models", [])]


def digest_check() -> dict:
    """``ollama show nimble:9b --modelfile`` names blob NIMBLE_BLOB of NIMBLE_BLOB_SIZE bytes; else refuse."""
    m = re.search(r"^FROM\s+(\S+)\s*$", ollama_modelfile(), re.M)
    path = m.group(1) if m else ""
    if os.path.basename(path) != NIMBLE_BLOB:
        raise Refuse("refusing: nimble:9b blob digest differs from the pin (not a result)")
    size = blob_size(path)
    if size != NIMBLE_BLOB_SIZE:
        raise Refuse(f"refusing: nimble:9b blob size {size} != {NIMBLE_BLOB_SIZE} (not a result)")
    return {"blob": NIMBLE_BLOB, "size": size}


def quiet_gate() -> dict:
    free, ld = free_mem_pct(), load1()
    try:
        resident = ollama_ps()
    except (OSError, ValueError):
        resident = []
    foreign = sorted(n for n in resident if n and n != NIMBLE_MODEL)
    ok = free >= QUIET_FREE_MEM_MIN_PCT and ld < QUIET_LOAD1_MAX
    return {"ok": ok, "free_pct": free, "load1": round(ld, 2), "foreign_models": foreign}


async def wait_resident(cfg: Cfg, budget_s: float = COLD_WAIT_S) -> bool:
    """Poll ``/api/ps`` (production ``_is_loaded``) until nimble is resident, at most ``budget_s``."""
    from llm_router import local_classifier as lc

    for i in range(int(budget_s / COLD_POLL_S) + 1):
        try:
            ok = await lc._is_loaded(NIMBLE_MODEL, 1.0)
        except Exception:  # noqa: BLE001 - not reachable yet: keep polling inside the budget
            ok = False
        if ok:
            return True
        if i < int(budget_s / COLD_POLL_S):
            await cfg.asleep(COLD_POLL_S)
    return False


async def wait_cooldown(cfg: Cfg) -> None:
    """Before every attempt (first or retry), wait for local_classifier's timeout cooldown on its own clock.
    This prevents one item's final timeout from spilling into the next item's first attempt.
    """
    from llm_router import local_classifier as lc

    while True:
        left = lc._cool_until - lc._now()
        if left <= 0:
            return
        await cfg.asleep(left)


async def warm_up(cfg: Cfg) -> float:
    """One discarded synthetic warm-up call (the production warm-up: ``warm-up`` text, no record)."""
    from llm_router import local_classifier as lc

    t0 = time.perf_counter()
    await lc._warm(NIMBLE_MODEL)
    await wait_resident(cfg)
    return round((time.perf_counter() - t0) * 1000.0, 1)


def shadow_records(cfg: Cfg) -> dict:
    """``{(session_id, text_sha): [source, ...]}`` in file order: the record file is the authority."""
    out: dict = defaultdict(list)
    for r in json_rows(cfg.shadow):
        if r.get("kind") == "classifier_shadow":
            out[(r.get("session_id") or "", r.get("text_sha"))].append(r.get("source"))
    return out


def is_final(sources: list) -> bool:
    if not sources:
        return False
    return sources[-1] in ("llm", "cache") or sources[-1] == MISMATCH or len(sources) >= 1 + NIMBLE_RETRIES


def resume_state(selected: list, records: dict) -> list:
    """Items still to run: no record (run), or one failed attempt (its retry). Final keys are skipped."""
    todo, seen = [], set()
    for it in selected:
        k = item_key(it)
        if k in seen:
            continue
        seen.add(k)
        if not is_final(records.get(k, [])):
            todo.append(it)
    return todo


def make_row(cfg: Cfg, item: dict, key: str, rules: tuple) -> dict:
    task, cx, tier = rules
    from llm_router.proxy.steps import STEP_TURN_FIRST

    return {"ts": cfg.now(), "session_id": item.get("session_id"), "text_sha": key, "session_kind": SESSION_KIND,
            "step_class": STEP_TURN_FIRST, "tier_task_type": task, "tier_complexity": cx, "tier_proposed": tier,
            "tier_reason": None, "tier": item.get("served_tier"), "requested_model": item.get("requested_model"),
            "tier_policy_version": "sha256:" + cfg.pins["tier_policy"][:12]}


def synthetic_record(sched, row: dict, source: str, capped) -> None:
    """A record the seam cannot write itself (no nimble call made), through the production writer."""
    sched._append({"kind": "classifier_shadow", **sched._fields(row), "llm": None, "source": source, "ms": None,
                   "model": NIMBLE_MODEL, "prompt_version": None, "backend": "systemone",
                   "assemble_capped": capped})


def save_meta(cfg: Cfg, meta: dict) -> None:
    write_json_private(cfg.shadow_meta, meta)


async def shadow_one(cfg: Cfg, sched, item: dict, body: dict, rules: tuple, meta: dict) -> str:
    """Run one item to its final record: ``maybe_schedule`` then ``drain``; a ``cold`` retry waits for /api/ps.
    Every attempt waits out the timeout cooldown; each failure is retried NIMBLE_RETRIES times."""
    from llm_router.proxy import llm_shadow

    key = record_key(body)
    k = (item.get("session_id") or "", key)
    sources = shadow_records(cfg).get(k, [])
    while not is_final(sources):
        if sources and sources[-1] == "cold":
            await wait_resident(cfg)
        await wait_cooldown(cfg)
        row = make_row(cfg, item, key, rules)
        status = sched.maybe_schedule(body, row)
        if status == llm_shadow.SCHEDULED:
            await sched.drain()
        elif status == llm_shadow.SKIPPED_KNOWN:
            return "known"
        else:
            raise Refuse(f"refusing: the shadow seam returned {status!r} (environment broken)")
        now_sources = shadow_records(cfg).get(k, [])
        if len(now_sources) == len(sources):
            synthetic_record(sched, row, NO_RECORD, None)
            now_sources = shadow_records(cfg).get(k, [])
        sources = now_sources
        meta["attempts"].append({"item_id": item["item_id"], "attempt": len(sources), "source": sources[-1]})
        save_meta(cfg, meta)
    return sources[-1]


async def shadow_all(cfg: Cfg, selected: list, policy, meta: dict) -> None:
    from llm_router.proxy import llm_shadow

    sched = llm_shadow.ShadowScheduler(path=cfg.shadow)
    try:
        for it in resume_state(selected, shadow_records(cfg)):
            body = build_body(it)
            rules = rules_tier(body, policy, it.get("requested_model"))
            k = item_key(it)
            if not shadow_records(cfg).get(k) and not check_assemble(it, body):
                synthetic_record(sched, make_row(cfg, it, k[1], rules), MISMATCH, it.get("capped"))
                meta["attempts"].append({"item_id": it["item_id"], "attempt": 1, "source": MISMATCH})
                save_meta(cfg, meta)
                continue
            await shadow_one(cfg, sched, it, body, rules, meta)
    finally:
        await sched.aclose()


def final_sources(cfg: Cfg, selected: list) -> dict:
    recs = shadow_records(cfg)
    return {it["item_id"]: (recs.get(item_key(it)) or [None])[-1] for it in selected}


def cmd_shadow(cfg: Cfg) -> int:
    check_common(cfg, policy=True)
    check_code_identity(cfg)
    sel = load_selection(cfg)
    if not sel.get("ok"):
        raise Refuse("refusing: the selection gate failed")
    if cfg.labels.exists():
        raise Refuse("refusing: labels.jsonl exists; no verdict may be made after a label")
    items = load_items(cfg)
    selected = [items[i] for i in sel["ids"]]
    policy = load_policy(cfg)
    try:
        meta = json.loads(cfg.shadow_meta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        meta = {"label": LABEL, "attempts": [], "preflight": [], "done": False}
    if meta.get("done"):
        raise Refuse("refusing: shadow is already done")
    digest = digest_check()
    with harness_env(cfg):
        quiet = quiet_gate()
        if not quiet["ok"]:
            emit({"step": "shadow", "status": "PARKED", "quiet_gate": quiet})
            return EXIT_PAUSED
        touch_private(cfg.shadow)

        async def run() -> None:
            warm_ms = await warm_up(cfg)
            meta["preflight"].append({"utc": utc(cfg.now()), "digest": digest, "quiet_gate": quiet,
                                      "warmup_ms": warm_ms})
            save_meta(cfg, meta)
            await shadow_all(cfg, selected, policy, meta)

        asyncio.run(run())
    fin = final_sources(cfg, selected)
    by_src = Counter(str(s) for s in fin.values())
    errors = sum(1 for s in fin.values() if s not in ("llm", "cache"))
    n = len(selected)
    gate_ok = error_gate_ok(errors, n)
    meta.update({"done": True, "n_selected": n, "final_sources": dict(sorted(by_src.items())), "errors": errors,
                 "assemble_mismatch": by_src.get(MISMATCH, 0), "error_gate_ok": gate_ok,
                 "retries": sum(1 for a in meta["attempts"] if a["attempt"] > 1), "done_utc": utc(cfg.now())})
    save_meta(cfg, meta)
    if not gate_ok:
        terminal(cfg, "shadow", f"post_shadow_error_gate errors={errors}/{n}")
    emit({"step": "shadow", "n_selected": n, "final_sources": dict(sorted(by_src.items())), "errors": errors,
          "error_gate_ok": gate_ok, "done": True})
    return EXIT_OK if gate_ok else EXIT_TERMINAL


# ----------------------------------------------------------------------------- label (section 6)
class SpendCap(Exception):
    pass


class NotOpus(Exception):
    pass


class CalibrationFail(Exception):
    pass


def load_label_mods(cfg: Cfg):
    """gt500 and shadowlab by path after their sha256 checks, the rubric through ``gt.load_rubric``."""
    if sha256_file(cfg.gt_scripts / "supply.py") != cfg.pins["supply"]:
        raise Refuse("refusing: supply.py sha256 differs from its pin")
    gt = pa.load_pinned("gt500", cfg.gt_scripts / "gt500.py", cfg.pins["gt500"])
    sl = pa.load_pinned("shadowlab", cfg.shadowlab_py, cfg.pins["shadowlab"])
    if gt.RUBRIC_MD5 != RUBRIC_MD5 or sl.RUBRIC_MD5 != RUBRIC_MD5 or sl.GT500_SHA256 != cfg.pins["gt500"]:
        raise Refuse("refusing: rubric / gt500 pins of the loaded helpers differ from the prereg")
    rub = gt.load_rubric(gt.RUBRIC_PATH)
    return gt, rub, sl


def spent(cfg: Cfg) -> tuple[float, float]:
    rows = json_rows(cfg.ledger)
    completed = {r.get("call_id") for r in rows if r.get("kind") == "call" and r.get("call_id")}
    costs = [float(r.get("total_cost_usd") or 0) for r in rows if r.get("kind") == "call"]
    costs.extend(float(r.get("est_usd") or 0) for r in rows
                 if r.get("kind") == "call_pending" and r.get("call_id") not in completed)
    return sum(costs), max(costs, default=0.0)


def spend_guard(cfg: Cfg, cap: float = SPEND_CAP_USD) -> float:
    """Stop when spent + estimate (1.25 x the dearest call so far; first call $0.30) would exceed the cap."""
    total, top = spent(cfg)
    est = top * SPEND_EST_FACTOR if top > 0 else SPEND_FIRST_EST_USD
    if total + est > cap:
        raise SpendCap(f"spent {total:.4f} + est {est:.4f} > cap {cap:.2f}")
    return est


def claude_cmd() -> list:
    return shlex.split(os.environ.get("P17D_CLAUDE", "claude"))


def judge_call(cfg: Cfg, mods, entries: list, mode: str) -> dict:
    """One ``claude -p`` call with shadowlab.call_opus's flags. Returns {opaque id: valid label}."""
    gt, rub, sl = mods
    est = spend_guard(cfg)
    sl.quota_guard(gt, str(cfg.usage))  # raises gt.Paused: nothing called, nothing terminal
    ids = [e["oid"] for e in entries]
    if mode == "single":
        e = entries[0]
        user = (rub.ITEM_TEMPLATE.format(id=e["oid"], context=e["context"], prompt=e["prompt"])
                + "\n\n" + rub.SINGLE_TAIL)
    else:
        user = "\n\n".join(rub.ITEM_TEMPLATE.format(id=e["oid"], context=e["context"], prompt=e["prompt"])
                           for e in entries) + "\n\n" + rub.BATCH_TAIL
    cfg.claude_cwd.mkdir(mode=0o700, exist_ok=True)
    cmd = claude_cmd() + ["-p", "--no-session-persistence", "--model", JUDGE_MODEL, "--setting-sources",
                          "project,local", "--strict-mcp-config", "--output-format", "json", "--max-turns", "1",
                          "--tools", "", "--system-prompt", rub.RUBRIC, "--", user]
    env = dict(os.environ)
    env.pop("ANTHROPIC_BASE_URL", None)
    cid = uuid.uuid4().hex
    append_json(cfg.ledger, {"kind": "call_pending", "call_id": cid, "ts": utc(cfg.now()),
                             "mode": mode, "ids": ids, "est_usd": est})
    t0 = time.time()
    d = None
    try:
        p = subprocess.run(cmd, cwd=str(cfg.claude_cwd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=JUDGE_TIMEOUT_S, env=env)
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        d = None
    row = {"kind": "call", "call_id": cid, "ts": utc(cfg.now()), "mode": mode, "ids": ids,
           "item_ids": [e.get("item_id") or e.get("planted_id") for e in entries], "total_cost_usd": est,
           "cost_basis": "estimate", "tokens_in": None, "tokens_out": None, "model_ok": None,
           "secs": round(time.time() - t0, 1)}
    if not isinstance(d, dict):
        append_json(cfg.ledger, row)
        return {}
    cost = d.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        row["total_cost_usd"] = cost
        row["cost_basis"] = "reported"
    u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    row["tokens_in"] = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens")) if u else None
    row["tokens_out"] = int(u.get("output_tokens") or 0) if u else None
    mu = d.get("modelUsage") if isinstance(d.get("modelUsage"), dict) else {}
    row["model_ok"] = all(JUDGE_MODEL in str(k).lower() for k in mu) if mu else None
    append_json(cfg.ledger, row)
    if p.returncode != 0:
        if mu and not row["model_ok"]:
            raise NotOpus(f"modelUsage {sorted(mu)}")
        return {}
    if not row["model_ok"]:
        raise NotOpus(f"modelUsage {sorted(mu)}")
    try:
        obj = gt.extract_json(d.get("result", ""))
    except Exception:  # noqa: BLE001 - an unparsable answer labels nothing; the batch rules retry
        return {}
    if mode == "single":
        labs = {ids[0]: obj} if isinstance(obj, dict) else {}
    else:
        rows = obj.get("labels") if isinstance(obj, dict) else None
        labs = {str(x.get("id")): x for x in rows if isinstance(x, dict)} if isinstance(rows, list) else {}
    return {oid: lab for oid, lab in labs.items() if oid in ids and gt.valid_label(lab, rub)}


def label_order(selected: list, f_ids: set) -> list:
    """F before G; each group by sha256(LABEL_SEED_FORMAT)."""
    def k(it):
        return hashlib.sha256(LABEL_SEED_FORMAT.format(item_id=it["item_id"]).encode()).hexdigest()
    return sorted([i for i in selected if i["item_id"] in f_ids], key=k) + \
        sorted([i for i in selected if i["item_id"] not in f_ids], key=k)


def judge_context(item: dict, sl) -> str:
    ctx = item.get("assembled_context") or ""
    if item.get("context_withheld"):
        first = ctx.split("\n", 1)[0]
        ctx = (first if first.startswith("Working directory:") else "Working directory: ?") + "\n\n" + sl.WITHHELD
    return sl.scrub(ctx)


class Labeller:
    def __init__(self, cfg: Cfg, mods, selected: list, f_ids: set):
        self.cfg, self.mods = cfg, mods
        gt, rub, sl = mods
        self.order = label_order(selected, f_ids)
        self.entries: dict = {}
        for n, it in enumerate(self.order, 1):
            sid, key = item_key(it)
            self.entries[it["item_id"]] = {"oid": OPAQUE_ID_FORMAT.format(n=n), "item_id": it["item_id"],
                                           "group": "F" if it["item_id"] in f_ids else "G", "session_id": sid,
                                           "text_sha": key, "context": judge_context(it, sl),
                                           "prompt": gt.prompt_view(sl.scrub(it.get("prompt") or ""))}
        base = len(self.order)
        self.planted = []
        for j, p in enumerate(rub.PLANTED, 1):
            self.planted.append({"oid": OPAQUE_ID_FORMAT.format(n=base + j), "planted_id": p["id"],
                                 "expect": p["expect"], "context": sl.scrub(p["context"]),
                                 "prompt": gt.prompt_view(sl.scrub(p["prompt"]))})
        self.labelled = {(r.get("session_id") or "", r.get("text_sha")) for r in json_rows(cfg.labels)}
        self.planted_rows = {r.get("planted_id"): r for r in json_rows(cfg.labels_planted)}
        self.errors = {r.get("item_id") for r in json_rows(cfg.ledger) if r.get("kind") == "label_error"}
        self.calls = 0

    def done(self, e: dict) -> bool:
        return (e["session_id"], e["text_sha"]) in self.labelled or e["item_id"] in self.errors

    def planted_ok(self) -> int:
        return sum(1 for p in self.planted if (self.planted_rows.get(p["planted_id"]) or {}).get("ok") is True)

    def _store(self, e: dict, lab: dict) -> None:
        gt, rub, sl = self.mods
        keep = {k: lab[k] for k in list(rub.DIMS) + ["needs_tools", "tier", "reason"]}
        if "planted_id" in e:
            ok = lab["tier"] == e["expect"]
            row = {"id": e["oid"], "planted_id": e["planted_id"], "tier": lab["tier"], "expect": e["expect"], "ok": ok}
            append_json(self.cfg.labels_planted, row)
            append_json(self.cfg.judgements, {"id": e["oid"], "planted_id": e["planted_id"], **keep})
            self.planted_rows[e["planted_id"]] = row
            if not ok:
                raise CalibrationFail(f"planted {e['oid']} wrong")
            return
        append_json(self.cfg.judgements, {"id": e["oid"], "item_id": e["item_id"], "group": e["group"],
                                          "session_id": e["session_id"], "text_sha": e["text_sha"], **keep})
        append_json(self.cfg.labels, {"text_sha": e["text_sha"], "session_id": e["session_id"], "truth": lab["tier"]})
        self.labelled.add((e["session_id"], e["text_sha"]))

    def run_batch(self, entries: list, mode: str) -> None:
        """Up to 1 + LABEL_BATCH_RETRIES calls for the batch's missing items, then one at a time."""
        missing = list(entries)
        for _ in range(1 + LABEL_BATCH_RETRIES):
            if not missing:
                return
            labs = judge_call(self.cfg, self.mods, missing, mode)
            self.calls += 1
            for e in missing:
                if e["oid"] in labs:
                    self._store(e, labs[e["oid"]])
            missing = [e for e in missing if e["oid"] not in labs]
        for e in missing:
            got = None
            for _ in range(LABEL_SINGLE_ATTEMPTS):
                labs = judge_call(self.cfg, self.mods, [e], "single")
                self.calls += 1
                got = labs.get(e["oid"])
                if got is not None:
                    break
            if got is not None:
                self._store(e, got)
            elif "planted_id" in e:
                raise CalibrationFail(f"planted {e['oid']} unlabelled")
            else:
                append_json(self.cfg.ledger, {"kind": "label_error", "ts": utc(self.cfg.now()), "id": e["oid"],
                                              "item_id": e["item_id"]})
                self.errors.add(e["item_id"])

    def run(self) -> None:
        if any(r.get("ok") is not True for r in self.planted_rows.values()):
            raise CalibrationFail("a planted label on file is wrong")
        todo_planted = [p for p in self.planted if p["planted_id"] not in self.planted_rows]
        if todo_planted:
            first = [self.entries[it["item_id"]] for it in self.order[:CALIBRATION_REAL_ITEMS]]
            cal = todo_planted + [e for e in first if not self.done(e)]
            random.Random(CALIBRATION_SHUFFLE_SEED).shuffle(cal)
            self.run_batch(cal, "batch")
        if self.planted_ok() < CALIBRATION_N:
            raise CalibrationFail(f"calibration {self.planted_ok()}/{CALIBRATION_N}")
        todo = [self.entries[it["item_id"]] for it in self.order]
        todo = [e for e in todo if not self.done(e)]
        seen, uniq = set(), []
        for e in todo:   # two selected items with one (session, text_sha) share one label
            if (e["session_id"], e["text_sha"]) not in seen:
                seen.add((e["session_id"], e["text_sha"]))
                uniq.append(e)
        for i in range(0, len(uniq), LABEL_BATCH):
            self.run_batch(uniq[i:i + LABEL_BATCH], "batch")


def cmd_label(cfg: Cfg) -> int:
    check_common(cfg)
    check_code_identity(cfg)
    try:
        meta = json.loads(cfg.shadow_meta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        meta = {}
    if meta.get("done") is not True:
        raise Refuse("refusing: shadow is not done")
    sel = load_selection(cfg)
    items = load_items(cfg)
    mods = load_label_mods(cfg)
    lab = Labeller(cfg, mods, [items[i] for i in sel["ids"]], set(sel["f_ids"]))
    gt = mods[0]
    status, code = "done", EXIT_OK
    try:
        lab.run()
        append_line(cfg.use_log, f"label done {utc(cfg.now())}")
    except gt.Paused:
        status, code = "PAUSED", EXIT_PAUSED
    except SpendCap:
        append_line(cfg.use_log, f"label stopped spend_cap {utc(cfg.now())}")
        status, code = "SPEND_CAP", EXIT_SPEND_CAP
    except NotOpus:
        terminal(cfg, "label", "judge_not_opus exit4")
        status, code = "STOPPED", EXIT_NOT_OPUS
    except CalibrationFail:
        terminal(cfg, "label", f"calibration {lab.planted_ok()}/{CALIBRATION_N}")
        status, code = "TERMINAL", EXIT_TERMINAL
    total, _ = spent(cfg)
    emit({"step": "label", "status": status, "labelled": line_count(cfg.labels),
          "planted_ok": lab.planted_ok(), "label_errors": len(lab.errors), "calls": lab.calls,
          "spend_usd": round(total, 4)})
    return code


# ----------------------------------------------------------------------------- score (sections 5, 7, 10)
def last_per_key(records: list) -> list:
    """The final attempt per (session_id, text_sha), in first-seen order (declared deviation, AMEND4 2(e))."""
    last, order = {}, []
    for r in records:
        if r.get("kind") != "classifier_shadow":
            continue
        k = (r.get("session_id") or "", r.get("text_sha"))
        if k not in last:
            order.append(k)
        last[k] = r
    return [last[k] for k in order]


def build_turns(records: list, labels: dict) -> list:
    """shadow_eval.score's turns (same arms, same imputation, same skip of records without a rules tier), kept
    so the harness can run boot_diff for under-route and the strata on exactly the scorer's rows."""
    from llm_router import shadow_eval as se

    by_sess_req: dict = defaultdict(Counter)
    for r in records:
        req = se.requested_of(r)
        if req:
            by_sess_req[r.get("session_id") or ""][req] += 1
    turns, seen = [], set()
    for r in records:
        key = (r.get("session_id") or "", r.get("text_sha"))
        rules = se.rules_eff(r)
        if rules is None or key in seen:
            continue
        seen.add(key)
        llm, fell_back = se.llm_pick(r)
        real = se.requested_of(r)
        sess = r.get("session_id") or ""
        imputed = by_sess_req[sess].most_common(1)[0][0] if by_sess_req.get(sess) else "sonnet"
        turns.append({"session_id": sess, "text_sha": r.get("text_sha"), "llm": llm, "rules_eff": rules,
                      "fallback": fell_back, "requested": real or imputed, "source": r.get("source"),
                      "ms": r.get("ms"), "truth": se._truth(labels, r)})
    return turns


def _clamp_cost(arm: str):
    from llm_router import shadow_eval as se
    return lambda t: se.cost_eff(se.clamp(t[arm], t["requested"]), t["truth"])


def _under(arm: str):
    from llm_router import shadow_eval as se
    return lambda t: 1.0 if se.RANK[t[arm]] < se.RANK[t["truth"]] else 0.0


def normal_interval(lab: list, fa, fb) -> list | None:
    """Session-clustered normal interval of mean(fa - fb) (CR1 standard error)."""
    if not lab:
        return None
    d = [fa(t) - fb(t) for t in lab]
    mean = sum(d) / len(d)
    by: dict = defaultdict(float)
    for t, x in zip(lab, d):
        by[t["session_id"]] += x - mean
    s = len(by)
    if s < 2:
        return None
    se_ = math.sqrt(s / (s - 1) * sum(v * v for v in by.values())) / len(d)
    return [round(mean - Z95 * se_, 4), round(mean + Z95 * se_, 4)]


def boot_pair(lab: list, fa, fb) -> dict:
    """shadow_eval.boot_diff (B 2,000, seed 20261003); with < 30 sessions also the normal and the wider one."""
    from llm_router import shadow_eval as se

    out = se.boot_diff(lab, fa, fb, B=BOOTSTRAP_B, seed=BOOTSTRAP_SEED)
    if out.get("clusters", 0) < WIDE_SESSIONS_LT and out.get("diff") is not None:
        nrm = normal_interval(lab, fa, fb)
        pct = out["ci95"]
        wide = pct if nrm is None or (pct[1] - pct[0]) >= (nrm[1] - nrm[0]) else nrm
        out = {**out, "ci95_normal": nrm, "ci95_wide": wide, "wide_rule": "fewer than 30 sessions"}
    return out


def arms_block(records: list, labels: dict) -> dict:
    """shadow_eval.score plus the under-route bootstrap, on one set of rows."""
    from llm_router import shadow_eval as se

    base = se.score(records, labels)
    lab = [t for t in build_turns(records, labels) if t["truth"]]
    out = {"n_labeled": base["n_labeled"], "n_sessions": base["n_labeled_sessions"],
           "n_fallback_labeled": sum(t["fallback"] for t in lab), "arms": base["arms"],
           "boot_c_lambda2_clamp": boot_pair(lab, _clamp_cost("llm"), _clamp_cost("rules_eff")) if lab else None,
           "boot_under": boot_pair(lab, _under("llm"), _under("rules_eff")) if lab else None}
    return out


def decide(*, p17c_passed: bool, pre_freeze_rows: int, calibration_ok: bool, feasibility_ok: bool, pins_ok: bool,
           errors: int, n_selected: int, n: int, top_session: int, c_llm, c_rules, under_llm: int,
           under_rules: int) -> tuple[str, str]:
    """Section 7, in order; the first match is the verdict. Point estimates only."""
    if not p17c_passed:
        return NOT_INFORMATIVE, "procedural: P1.7-c has not passed"
    if pre_freeze_rows > 0:
        return NOT_INFORMATIVE, f"procedural: {pre_freeze_rows} classifier_shadow rows dated before frozen_utc"
    if not calibration_ok:
        return NOT_INFORMATIVE, "procedural: labeller calibration below 6/6"
    if not feasibility_ok:
        return NOT_INFORMATIVE, "procedural: feasibility gate failed"
    if not pins_ok:
        return NOT_INFORMATIVE, "procedural: a pin sha differs"
    if not error_gate_ok(errors, n_selected):
        return NOT_INFORMATIVE, f"errors: {errors} of {n_selected} selected items > 10%"
    if n < MIN_N:
        return NOT_INFORMATIVE, f"n: {n} verdict rows < {MIN_N}"
    if Fraction(top_session, n) > Fraction(str(MAX_SESSION_SHARE)):
        return NOT_INFORMATIVE, f"share: largest session holds {top_session}/{n} > 60%"
    if c_llm <= c_rules and under_llm <= under_rules:
        return PASS, "C-lambda2-clamp(llm) <= C-lambda2-clamp(rules_eff) and under(llm) <= under(rules_eff)"
    return FAIL, "a bar does not hold on the point estimates"


def _arm(block: dict, arm: str, what: str):
    a = (block.get("arms") or {}).get(arm)
    if not a:
        return None
    return a["under"]["k"] if what == "under_k" else a[what]


def reference_arms(lab: list) -> dict:
    from llm_router import shadow_eval as se

    out = {}
    m = len(lab)
    for name, tier in (("always_opus", "opus"), ("always_sonnet", "sonnet")):
        if not m:
            out[name] = None
            continue
        out[name] = {"c_lambda2_clamp": round(sum(se.cost_eff(se.clamp(tier, t["requested"]), t["truth"])
                                                  for t in lab) / m, 4),
                     "under": se._rate(sum(se.RANK[tier] < se.RANK[t["truth"]] for t in lab), m)}
    return out


def pct(values: list, q: float):
    v = sorted(x for x in values if isinstance(x, (int, float)))
    if not v:
        return None
    return v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))]


def load_ge3_tiers(cfg: Cfg) -> dict:
    return {r.get("id"): r.get("tier") for r in json_rows(cfg.ge3_labels)}


def score(cfg: Cfg, records_raw: list, labels: dict, items_by_key: dict, selection: dict, *, p17c_passed: bool,
          pre_freeze_rows: int, calibration_ok: bool, label_error_ids: set, ge3_tiers: dict, kappa_fn=None,
          pins_ok: bool = True, pin_failures: list | None = None) -> dict:
    """The full section 7 analysis on the last record per key."""
    from llm_router import shadow_eval as se

    records = last_per_key(records_raw)
    attempts = Counter((r.get("session_id") or "", r.get("text_sha")) for r in records_raw
                       if r.get("kind") == "classifier_shadow")
    sel_ids = list(selection["ids"])
    n_selected = len(sel_ids)
    key_of = {it["item_id"]: k for k, its in items_by_key.items() for it in its}
    rec_by_key = {(r.get("session_id") or "", r.get("text_sha")): r for r in records}
    finals = {i: (rec_by_key.get(key_of.get(i)) or {}).get("source") for i in sel_ids}
    by_source = Counter(str(s) for s in finals.values())
    mismatch = by_source.get(MISMATCH, 0)
    nimble_err = sum(1 for s in finals.values() if s not in ("llm", "cache", MISMATCH))
    lab_err = sum(1 for i in sel_ids if i in label_error_ids and key_of.get(i) not in labels)
    errors = nimble_err + mismatch + lab_err

    main = arms_block(records, labels)
    base = se.score(records, labels)
    turns = build_turns(records, labels)
    lab = [t for t in turns if t["truth"]]
    for arm in ("llm", "rules_eff"):     # the harness's turns are the scorer's turns
        if lab and round(sum(_clamp_cost(arm)(t) for t in lab) / len(lab), 4) != base["arms"][arm]["c_lambda2_clamp"]:
            raise RuntimeError("turn reconstruction disagrees with shadow_eval.score")
    verdict_rows = [t for t in lab if t["source"] in se.OK_SOURCES]
    n = len(verdict_rows)
    vs = Counter(t["session_id"] for t in verdict_rows)
    top = vs.most_common(1)[0][1] if n else 0
    verdict, why = decide(p17c_passed=p17c_passed, pre_freeze_rows=pre_freeze_rows, calibration_ok=calibration_ok,
                          feasibility_ok=bool(selection.get("ok")), pins_ok=pins_ok, errors=errors,
                          n_selected=n_selected, n=n, top_session=top, c_llm=_arm(main, "llm", "c_lambda2_clamp"),
                          c_rules=_arm(main, "rules_eff", "c_lambda2_clamp"),
                          under_llm=_arm(main, "llm", "under_k"), under_rules=_arm(main, "rules_eff", "under_k"))

    def sub(pred) -> dict:
        rs = [r for r in records if pred(r)]
        return arms_block(rs, labels)

    group_of = {}
    for gname in ("f_ids", "g_ids"):
        for i in selection.get(gname, []):
            if key_of.get(i):
                group_of[key_of[i]] = "F" if gname == "f_ids" else "G"

    def rkey(r):
        return (r.get("session_id") or "", r.get("text_sha"))

    groups = {g: sub(lambda r, g=g: group_of.get(rkey(r)) == g) for g in ("F", "G")}
    for g, blk in groups.items():
        blk["n_verdict_rows"] = sum(1 for t in verdict_rows if group_of.get((t["session_id"], t["text_sha"])) == g)
    sens_records = [r for r in records if r.get("source") in se.OK_SOURCES]
    sens = arms_block(sens_records, labels)
    sens_ok = (_arm(sens, "llm", "c_lambda2_clamp") is not None
               and _arm(sens, "llm", "c_lambda2_clamp") <= _arm(sens, "rules_eff", "c_lambda2_clamp")
               and _arm(sens, "llm", "under_k") <= _arm(sens, "rules_eff", "under_k"))

    def first_item(r):
        its = items_by_key.get(rkey(r)) or [{}]
        return its[0]

    era_cut = parse_utc(PROXY_ERA_UTC)
    strata_fns = {
        "length_band": lambda r: first_item(r).get("length_band"),
        "truth_tier": lambda r: se._truth(labels, r),
        "first_in_session": lambda r: first_item(r).get("first_in_session"),
        "requested_tier": lambda r: se.requested_of(r) or "imputed",
        "era": lambda r: ("proxy" if pa.parse_ts(first_item(r).get("ts")) and
                          pa.parse_ts(first_item(r).get("ts")).timestamp() >= era_cut else "pre_proxy"),
    }
    strata = {}
    for name, fn in strata_fns.items():
        vals = sorted({str(fn(r)) for r in records})
        strata[name] = {}
        for v in vals:
            blk = sub(lambda r, fn=fn, v=v: str(fn(r)) == v)
            strata[name][v] = {"n": blk["n_labeled"], "arms": {
                a: (None if not blk["arms"].get(a) else {"c_lambda2_clamp": blk["arms"][a]["c_lambda2_clamp"],
                                                         "under": blk["arms"][a]["under"]})
                for a in ("llm", "rules_eff")}}
    refs = reference_arms(lab)
    truth_opus = sum(1 for t in lab if t["truth"] == "opus")
    opus_share = {a: (round(sum(1 for t in lab if t[a] == "opus") / len(lab), 4) if lab else None)
                  for a in ("llm", "rules_eff")}
    kappa = None
    g_pairs = []
    for i in selection.get("g_ids", []):
        it = next((x for x in items_by_key.get(key_of.get(i), []) if x["item_id"] == i), None)
        fresh = labels.get(key_of.get(i)) if key_of.get(i) else None
        old = ge3_tiers.get((it or {}).get("ge3_id"))
        if fresh and old:
            g_pairs.append((fresh, old))
    if g_pairs and kappa_fn is not None:
        kappa = {"n": len(g_pairs), "kappa": kappa_fn([a for a, _ in g_pairs], [b for _, b in g_pairs])}

    lead = []
    if truth_opus < TRUTH_OPUS_MIN:
        lead.append(f"under-route cannot discriminate: {truth_opus} truth-opus rows < {TRUTH_OPUS_MIN}")
    def side(blk):
        """True when both bars hold on the block's point estimates; None without labelled rows."""
        if _arm(blk, "llm", "c_lambda2_clamp") is None:
            return None
        return (_arm(blk, "llm", "c_lambda2_clamp") <= _arm(blk, "rules_eff", "c_lambda2_clamp")
                and _arm(blk, "llm", "under_k") <= _arm(blk, "rules_eff", "under_k"))

    for g, blk in groups.items():
        if side(main) is not None and side(blk) is not None and side(blk) != side(main):
            lead.append(f"group {g} takes the opposite side from the combined point estimates")
    if verdict == PASS and not sens_ok:
        lead.append("the error-excluded sensitivity verdict differs from the primary verdict")
    if verdict == PASS and opus_share["llm"] is not None and opus_share["llm"] > opus_share["rules_eff"]:
        lead.append("nimble picks opus more often than rules_eff: check it does not win only through opus share")

    lat = [t["ms"] for t in turns if t["source"] == "llm"]
    return {
        "label": LABEL, "verdict": verdict, "verdict_reason": why, "lead": lead,
        "pin_failures": pin_failures,
        "n_selected": n_selected, "n_f": len(selection.get("f_ids", [])), "n_g": len(selection.get("g_ids", [])),
        "admitted": selection.get("admitted"), "n": n, "n_verdict_sessions": len(vs),
        "largest_session_share_verdict_rows": round(top / n, 4) if n else None,
        "top3_session_counts_verdict_rows": [c for _, c in vs.most_common(3)],
        "n_labeled": main["n_labeled"], "n_fallback": base["n_fallback"], "n_joined": base["n_joined"],
        "n_capped": base["n_capped"],
        "n_capped_attach": sum(1 for its in items_by_key.values() for it in its if it.get("capped") is True),
        "requested_basis": dict(sorted(Counter(str(it.get("requested_basis")) for its in items_by_key.values()
                                               for it in its).items())),
        "n_attempts": {"records": sum(attempts.values()), "keys": len(attempts),
                                                     "max_per_key": max(attempts.values(), default=0)},
        "errors": {"nimble": nimble_err, "assemble_mismatch": mismatch, "label": lab_err, "total": errors,
                   "by_source": dict(sorted(by_source.items()))},
        "arms": main["arms"], "boot_c_lambda2_clamp": main["boot_c_lambda2_clamp"], "boot_under": main["boot_under"],
        "reference_arms": refs, "truth_opus_rows": truth_opus, "opus_pick_share": opus_share,
        "groups": groups, "kappa_fresh_vs_ge3_on_G": kappa, "strata": strata,
        "top3_session_counts": base["top3_session_counts"],
        "sensitivity_error_excluded": {**sens, "would_pass": sens_ok},
        "raw_cost": {a: base[f"craw_{a}"] for a in ("llm", "rules_eff")},
        "raw_cost_clamp": {a: base[f"craw_clamp_{a}"] for a in ("llm", "rules_eff")},
        "latency_ms_llm": {"n": len(lat), "p50": pct(lat, 0.50), "p95": pct(lat, 0.95)},
        "shadow_eval": base,
    }


def measurement(result: dict, records: list, labels: dict, items_by_key: dict, selection: dict) -> dict:
    """The repo copy: item ids, session indices (never uuids), tiers, sources, the verdict."""
    turns = {(t["session_id"], t["text_sha"]): t for t in build_turns(last_per_key(records), labels)}
    key_of = {it["item_id"]: (k, it) for k, its in items_by_key.items() for it in its}
    sess_index: dict = {}
    rows = []
    for gname, g in (("f_ids", "F"), ("g_ids", "G")):
        for i in selection.get(gname, []):
            k, it = key_of.get(i, (None, {}))
            sess_index.setdefault(it.get("session_id"), len(sess_index))
            t = turns.get(k) or {}
            rows.append({"item_id": i, "group": g, "session_index": sess_index[it.get("session_id")],
                         "llm": t.get("llm"), "rules_eff": t.get("rules_eff"), "requested": t.get("requested"),
                         "truth": t.get("truth"), "source": t.get("source")})
    keep = ("verdict", "verdict_reason", "lead", "n_selected", "n_f", "n_g", "n", "n_labeled", "n_fallback",
            "errors", "arms", "boot_c_lambda2_clamp", "boot_under", "reference_arms")
    return {"label": LABEL, "disclosures": DISCLOSURES,
            "what": "P1.7-d synthetic: nimble:9b vs rules_eff on replayed past prompts",
            "prereg": "docs/prereg/PREREG-P17D-synthetic.md", **{k: result[k] for k in keep}, "items": rows}


def rows_from_bytes(raw: bytes) -> list:
    """Every object line from the snapshot; malformed lines are skipped."""
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def labels_from_bytes(raw: bytes) -> dict:
    """shadow_eval.load_labels on the exact bytes hashed in the started record."""
    from llm_router import shadow_eval as se

    out = {}
    for r in rows_from_bytes(raw):
        if not isinstance(r.get("text_sha"), str):
            continue
        truth = se.tier_name(r.get("truth"))
        if truth:
            out[(r.get("session_id") or "", r["text_sha"])] = truth
    return out


def before_freeze_count(records: list, frozen_utc: str) -> int:
    cutoff = parse_utc(frozen_utc)
    count = 0
    for r in records:
        ts = r.get("ts") if r.get("kind") == "classifier_shadow" else None
        if isinstance(ts, str):
            try:
                ts = parse_utc(ts)
            except ValueError:
                ts = None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts < cutoff:
            count += 1
    return count


def cmd_score(cfg: Cfg, p17c_passed: bool) -> int:
    check_common(cfg, policy=True)
    if has_started(cfg):
        raise Refuse("refusing: use.log already holds a started record; score is single-use")
    if not label_finished(cfg):
        raise Refuse("refusing: label has not finished (no `label done` / `label stopped spend_cap` in use.log)")
    check_code_identity(cfg)
    sel = load_selection(cfg)
    items = load_items(cfg)
    res = check_prereg.check(cfg.prereg)
    try:
        labels_raw, shadow_raw = cfg.labels.read_bytes(), cfg.shadow.read_bytes()
    except FileNotFoundError:
        raise Refuse("refusing: labels.jsonl or classifier_shadow.jsonl missing") from None
    append_line(cfg.use_log, f"started {utc(cfg.now())} prereg_sha256={res['sha256']} git_sha={cfg.head_sha()} "
                             f"labels_sha256={hashlib.sha256(labels_raw).hexdigest()} "
                             f"shadow_sha256={hashlib.sha256(shadow_raw).hexdigest()}")
    pins_ok, pin_failures, gt = True, None, None
    try:
        gt, _, _ = load_label_mods(cfg)
    except (Refuse, SystemExit, OSError) as exc:
        pins_ok = False
        pin_failures = [exc.msg if isinstance(exc, Refuse) else type(exc).__name__ + ": " + str(exc)]
    labels = labels_from_bytes(labels_raw)
    records = rows_from_bytes(shadow_raw)
    items_by_key: dict = defaultdict(list)
    for i in sel["ids"]:
        items_by_key[item_key(items[i])].append(items[i])
    planted = {r.get("planted_id"): r for r in json_rows(cfg.labels_planted)}
    calibration_ok = len(planted) == CALIBRATION_N and all(r.get("ok") is True for r in planted.values())
    label_errors = {r.get("item_id") for r in json_rows(cfg.ledger) if r.get("kind") == "label_error"}
    kappa_fn = None
    ge3 = {}
    if sel.get("g_ids") and gt is not None:
        kappa_fn = lambda a, b: gt.cohen_kappa(a, b)  # noqa: E731
        ge3 = load_ge3_tiers(cfg)
    result = score(cfg, records, labels, dict(items_by_key), sel, p17c_passed=p17c_passed,
                   pre_freeze_rows=before_freeze_count(records, res["frozen_utc"]), calibration_ok=calibration_ok,
                   label_error_ids=label_errors, ge3_tiers=ge3, kappa_fn=kappa_fn,
                   pins_ok=pins_ok, pin_failures=pin_failures)
    total, _ = spent(cfg)
    result.update({"spend_usd": round(total, 4), "p17c_passed": p17c_passed, "utc": utc(cfg.now()),
                   "prereg_sha256": res["sha256"], "harness_sha": cfg.head_sha(), "disclosures": DISCLOSURES})
    write_json_private(cfg.result, result)
    cfg.measurement.parent.mkdir(parents=True, exist_ok=True)
    cfg.measurement.write_text(json.dumps(measurement(result, records, labels, dict(items_by_key), sel), indent=1,
                                          sort_keys=True) + "\n", encoding="utf-8")
    emit({"step": "score", "verdict": result["verdict"], "n": result["n"], "n_selected": result["n_selected"],
          "errors": result["errors"]["total"], "lead": len(result["lead"])})
    return EXIT_OK


# ----------------------------------------------------------------------------- status
def cmd_status(cfg: Cfg) -> int:
    """Counts only. Never parses labels (no truth value, no distribution of them)."""
    try:
        sel = json.loads(cfg.selection.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        sel = {}
    try:
        meta = json.loads(cfg.shadow_meta.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        meta = {}
    recs = [r for r in json_rows(cfg.shadow) if r.get("kind") == "classifier_shadow"]
    calls = [r for r in json_rows(cfg.ledger) if r.get("kind") == "call"]
    lines = use_lines(cfg)
    emit({"step": "status", "selected": len(sel.get("ids", [])), "selection_ok": sel.get("ok"),
          "shadow_records": len(recs),
          "shadow_sources": dict(sorted(Counter(str(r.get("source")) for r in recs).items())),
          "shadow_done": meta.get("done") is True, "labels_lines": line_count(cfg.labels),
          "planted_lines": line_count(cfg.labels_planted), "judge_calls": len(calls),
          "spend_usd": round(spent(cfg)[0], 4),
          "use_log": {"harness_line": bool(lines) and bool(HARNESS_RE.match(lines[0])),
                      "started": any(x.startswith("started ") for x in lines),
                      "terminal": any(x.startswith("terminal ") for x in lines)}})
    return EXIT_OK


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("select", "shadow", "label", "score", "status"):
        s = sub.add_parser(name)
        s.add_argument("--run-dir", default=os.environ.get(RUN_ENV) or str(DEFAULT_RUN))
        if name == "label":
            s.add_argument("--usage", default=str(USAGE_JSON))
        if name == "score":
            s.add_argument("--p17c", choices=("passed", "not_passed"), required=True,
                           help="PLAN P1.7 task 3 state; not_passed makes the verdict NOT INFORMATIVE")
    a = ap.parse_args(argv)
    cfg = Cfg(run=Path(a.run_dir))
    if a.cmd == "label":
        cfg.usage = Path(a.usage)
    try:
        if a.cmd == "status":
            return cmd_status(cfg)
        if not cfg.run.is_dir():
            raise Refuse("refusing: the run dir does not exist")
        if a.cmd == "select":
            return cmd_select(cfg)
        if a.cmd == "shadow":
            return cmd_shadow(cfg)
        if a.cmd == "label":
            return cmd_label(cfg)
        return cmd_score(cfg, a.p17c == "passed")
    except Refuse as r:
        print(r.msg, file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
