#!/usr/bin/env python3
"""Single-use, frozen P2.0-T offline requalification of technical operations (PLAN v16, D-198).

A separate module on purpose: scripts/p20_requal.py (tonight's P2.0 run is pinned to it by
REPO_SHA) is not edited. This file imports P2.0's blocks (JSONL/atomic writers, the call and
quiet gates, run, tp2, install, action_key, metrics) and adds the P2.0-T behaviour (H4-H8, H9)
behind its own subcommands, constants block (P20T-CONSTANTS) and run dir (t1).
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from statistics import median
from typing import Callable
import fcntl
from contextlib import contextmanager

import llm_router
from llm_router import eligibility, local_models, technical_ops
from llm_router.resolver.eligibility_store import record_competence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import check_prereg  # noqa: E402
import p20_requal as base  # noqa: E402

Refuse, Park, Terminal = base.Refuse, base.Park, base.Terminal
PREREG_T = REPO / "docs/prereg/PREREG-P20T-techop.md"
# AMEND1-T: the free-memory floor counts the arm's own resident model as available (a loaded
# qwen3.8 at 32k context takes ~51% of RAM, so the raw floor parked every call after warm-up).
AMEND1_T = REPO / "docs/prereg/PREREG-P20T-techop-AMEND1.md"
AMEND1_BLOCK = "```text P20T-AMEND1-CONSTANTS"
AMEND1_FIXED = {"QUIET_FREE_MEM_BASIS": "excl_own_arm"}
AMEND1_SHA_KEYS = {"REPO_SHA": r"[0-9a-f]{40}"}
BLOCK = "```text P20T-CONSTANTS"
SET = "tech_op"
STRATA = ("org", "ho")
LOCAL = "qwen3.8-uncensored:latest"
# H5: constants. frozen() requires the prereg's block to equal these byte for byte (plus the
# fill-ins below), so the draft and the harness cannot drift apart.
CONSTANTS_T: dict[str, str] = {
    "LABEL": "p20t-techop",
    "SELECT_SEED": "20261017",
    "N_TARGET_TECH_OP": "120",
    "N_MIN": "60",
    "MIN_SESSIONS": "5",
    "SESSION_CAP_SHARE": "0.20",
    "ORG_MIN_N": "20",
    "GATE_WINDOW_FACTOR": "0.9",
    "ORG_MAX_GAP_PTS": "10.0",
    "LOCAL_MODEL": LOCAL,
    f"ID_{LOCAL}": "b3f4c17c6811",
    "REFERENCE": "sonnet",
    "REPORT_ARMS": "haiku",
    "JUDGE": "opus",
    "LOCAL_TIMEOUT_S": "180",
    "REF_TIMEOUT_S": "300",
    "RETRIES": "1",
    "REF_WINDOW": "65536",
    "ANSWER_CAP_CHARS": "4000",
    "JUDGE_CONTEXT_CAP_CHARS": "24000",
    "PLANTS_SWAPPED": "6",
    "PLANTS_DEGRADED": "6",
    "PLANTS_REPEAT": "6",
    "PLANTS_GENERIC": "6",
    "PLANTS_PASS_SHARE": "5/6",
    "PLANTS_MIN_PLACED": "4",
    "REJUDGE_SHARE": "0.10",
    "REJUDGE_FLAG_BELOW": "0.80",
    "MAX_REF_MISSING_SHARE": "0.10",
    "QUIET_FREE_MEM_MIN_PCT": "40",
    "QUIET_LOAD1_MAX": "4.0",
    "QUIET_READINGS": "3",
    "QUIET_INTERVAL_S": "10",
    "QUOTA_SESSION_PAUSE_PCT": "80",
    "QUOTA_WEEKLY_PAUSE_PCT": "85",
    "QUOTA_MAX_AGE_S": "1800",
    "SPEND_CAP_USD": "40.00",
    "JUDGE_RESERVE": "1",
    "FIRST_CALL_EST_USD": "0.30",
    "SAFETY_FACTOR_SPEND": "1.25",
    "WINDOW_SINCE": "2026-09-28T00:00:00Z",
    "TIERS_WRITTEN": "simple",
    "BASIS": "all_technical_op_continuations",
    "CENSUS_PY_SHA256": "03941a4f9a8f49526eacd3ab3d116602da9a3667b2ec10c04c8f4e531d7c289a",
    "CLAUDE_SONNET_ID": "claude-sonnet-5-5",
    "CLAUDE_HAIKU_ID": "claude-haiku-5-5",
    "CLAUDE_OPUS_ID": "claude-opus-5-5",
}
FILL_KEYS_T = (
    "REPO_SHA", "SUBAGENT_BRANCH", "N_REALISED_TECH_OP_ROWSTAMP_ORG", "WINDOW_UNTIL", "PROXY_CALLS_COPY_SHA256", "BUILD_MANIFEST_SHA256",
    "POOL_TECH_OP_SHA256", "POOL_COUNT_TECH_OP_ORG", "POOL_COUNT_TECH_OP_HO",
    "N_REALISED_TECH_OP", "N_REALISED_TECH_OP_ORG", "N_REALISED_SESSIONS",
)
HEX64 = ("PROXY_CALLS_COPY_SHA256", "BUILD_MANIFEST_SHA256", "POOL_TECH_OP_SHA256")
INTS = ("N_REALISED_TECH_OP_ROWSTAMP_ORG", "POOL_COUNT_TECH_OP_ORG", "POOL_COUNT_TECH_OP_HO", "N_REALISED_TECH_OP",
        "N_REALISED_TECH_OP_ORG", "N_REALISED_SESSIONS")
# The pre-freeze view (status, select --dry-run).
STATIC = {**CONSTANTS_T, "LOCAL_MODELS": LOCAL}
REQUIRED = {"id", "set", "stratum", "source", "session", "tier", "task_type", "request",
            "messages", "tools", "organic_action"}
KINDS = ("__swap__", "__degraded__", "__repeat__", "__generic__")
SIX = ("Read", "Grep", "Glob", "Bash", "Edit", "Write")
MUTATING_TOOLS = ("Edit", "Write", "NotebookEdit")
GENERIC_PLANT = '{"tool": "Glob", "input": {"pattern": "**/*"}}'
P20_REQUAL_DIR = Path.home() / ".rsi/research/primary-plan/v16/requal"
EW_WINDOW = 32768  # equal-window sample (OPEN-6): the local window, for the Claude arms
JUDGE_BRIEF_TECH_OP_T = (
    "a next action is *acceptable* if, given the conversation and the tool output just\n"
    "returned, it is a reasonable step toward the user's request: it uses a tool that can advance\n"
    "the request, its target is one the conversation points to or a sensible neighbour of it, it\n"
    "does not repeat a call whose result is already in the conversation, it does nothing\n"
    "destructive, and it does not stop (\x60final\x60) while the request is plainly unsatisfied.\n"
    "A different but reasonable tool or argument is acceptable. A \x60final\x60 is acceptable only\n"
    "if the request is already satisfied."
)
CALL_RE = re.compile(r"^\[tool_use (\S+)\] (\{.*\})$", re.M)


@dataclass
class CfgT(base.Cfg):
    prereg: Path = PREREG_T
    p20_amend2: Path = base.AMEND2
    lock_path: Path = P20_REQUAL_DIR / "ollama.lock"
    ps_fn: Callable[[], list] = lambda: subprocess.run(
        ["ps", "-axo", "command"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=30, check=False).stdout.splitlines()
    local_touched: bool = False  # BLOCKER-1: set once this process passed the gate for a local call
    amend1_t: Path = AMEND1_T
    mem_total_fn: Callable[[], int] = lambda: int(subprocess.run(
        ["sysctl", "-n", "hw.memsize"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=30, check=True).stdout)


class OllamaT(base.OllamaHTTP):
    def resident_bytes(self, model: str) -> int:
        """AMEND1-T: bytes Ollama reports resident for `model` (0 when it is not loaded)."""
        return sum(int(row.get("size", 0)) for row in self.request("/api/ps").get("models", [])
                   if row.get("name") == model)


def default_dir() -> Path:
    return Path(os.environ.get("P20T_REQUAL_DIR", str(
        Path.home() / ".rsi/research/primary-plan/v16/requal/t1")))


# ---- H9: freeze ------------------------------------------------------------------------

def frozen(cfg: CfgT) -> dict:
    try:
        checked = check_prereg.check(cfg.prereg)
        epoch = datetime.fromisoformat(checked["frozen_utc"].replace("Z", "+00:00"))
        if epoch.tzinfo is None or epoch.timestamp() >= cfg.now() or not checked["match"]:
            raise ValueError("freeze")
        text = cfg.prereg.read_text()
    except (OSError, ValueError, IndexError) as exc:
        raise Refuse("refusing: prereg is not frozen") from exc
    constants = base.parse_block(text, BLOCK)
    fixed = CONSTANTS_T.keys() | set(FILL_KEYS_T)
    for key in constants.keys() - fixed:
        raise Refuse(f"refusing: unknown constant {key}")
    if constants.keys() != fixed:
        raise Refuse("refusing: missing constant")
    if "FILL-AT-FREEZE" in constants.values():
        raise Refuse("refusing: prereg has freeze fill-ins")
    for key, value in CONSTANTS_T.items():
        if constants[key] != value:
            raise Refuse(f"refusing: constant {key} differs from the prereg")
    bad = (not re.fullmatch(r"[0-9a-f]{40}", constants["REPO_SHA"])
           or constants["SUBAGENT_BRANCH"] not in ("A", "B")
           or any(not re.fullmatch(r"[0-9a-f]{64}", constants[k]) for k in HEX64)
           or any(not re.fullmatch(r"[0-9]+", constants[k]) for k in INTS))
    try:
        datetime.fromisoformat(constants["WINDOW_UNTIL"].replace("Z", "+00:00"))
    except ValueError:
        bad = True
    if bad:
        raise Refuse("refusing: malformed fill-in")
    copy = cfg.run_dir / "PREREG.md"
    if copy.exists() and copy.read_bytes() != cfg.prereg.read_bytes():
        raise Refuse("refusing: PREREG.md differs")
    try:
        second = check_prereg.check(cfg.p20_amend2)
        if not second["match"]:
            raise ValueError("freeze")
        second_epoch = datetime.fromisoformat(second["frozen_utc"].replace("Z", "+00:00"))
        if second_epoch.tzinfo is None or not second_epoch.timestamp() < epoch.timestamp():
            raise ValueError("freeze order")
    except (OSError, ValueError, IndexError, OverflowError) as exc:
        raise Refuse("refusing: P2.0 AMEND2 is not frozen before this prereg") from exc
    out = {"sha256": checked["sha256"], "frozen_utc": checked["frozen_utc"],
           "amend2_sha256": second["sha256"], "amend2_frozen_utc": second["frozen_utc"]}
    if cfg.amend1_t.with_suffix(".sha256").exists():
        # AMEND1-T is in force as soon as its sidecar exists; a draft without one is not.
        try:
            first = check_prereg.check(cfg.amend1_t)
            if not first["match"]:
                raise ValueError("freeze")
            first_text = cfg.amend1_t.read_text()
        except (OSError, ValueError, IndexError, OverflowError) as exc:
            raise Refuse("refusing: AMEND1 is not frozen") from exc
        try:
            first_epoch = datetime.fromisoformat(first["frozen_utc"].replace("Z", "+00:00"))
            if (first_epoch.tzinfo is None
                    or not epoch.timestamp() < first_epoch.timestamp() < cfg.now()):
                raise ValueError("freeze order")
        except (ValueError, OverflowError) as exc:
            raise Refuse("refusing: AMEND1 frozen_utc") from exc
        try:
            overrides = base.parse_block(first_text, AMEND1_BLOCK)
            if (overrides.keys() != AMEND1_FIXED.keys() | AMEND1_SHA_KEYS.keys()
                    or any(overrides[k] != v for k, v in AMEND1_FIXED.items())
                    or not all(re.fullmatch(pattern, overrides[k])
                               for k, pattern in AMEND1_SHA_KEYS.items())):
                raise ValueError("constants")
        except (Refuse, ValueError) as exc:
            raise Refuse("refusing: AMEND1 constants") from exc
        first_copy = cfg.run_dir / "PREREG-AMEND1.md"
        if first_copy.exists() and first_copy.read_bytes() != cfg.amend1_t.read_bytes():
            raise Refuse("refusing: PREREG-AMEND1.md differs")
        constants = {**constants, **overrides}
        out.update(amend1_sha256=first["sha256"], amend1_frozen_utc=first["frozen_utc"])
    out["constants"] = {**constants, "LOCAL_MODELS": constants["LOCAL_MODEL"]}
    return out


# ---- tool protocol ---------------------------------------------------------------------

def tool_names(item: dict) -> set:
    return {t.get("name") for t in item.get("tools", []) if isinstance(t, dict)}


def action_key(text: str):
    """(tool, target) for the six builder tools, ("final",), or None (no computable target)."""
    value = base.first_object(text)
    if "final" in value:
        return ("final",)
    name, args = value.get("tool"), value.get("input")
    if not isinstance(name, str) or not isinstance(args, dict):
        raise ValueError("tool protocol")
    if name not in SIX:
        return None
    return base.action_key(text)


def reply_key(text: str):
    try:
        return action_key(text)
    except (ValueError, KeyError, TypeError):
        return None


def valid_reply(text: str, item: dict) -> bool:
    """H7 (§6, N5): one object with a final string, or a listed tool and an input object;
    the key rule (file_path/path/pattern/command) binds the six builder tools only."""
    try:
        value = base.first_object(text)
    except ValueError:
        return False
    if "final" in value:
        return isinstance(value["final"], str)
    name = value.get("tool")
    if not (isinstance(name, str) and name in tool_names(item)
            and isinstance(value.get("input"), dict)):
        return False
    if name in SIX:
        try:
            base.action_key(text)
        except (ValueError, KeyError, TypeError):
            return False
    return True


def norm(value, key=""):
    if isinstance(value, str):
        if key in ("file_path", "path"):
            return os.path.normpath(value)
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: norm(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [norm(v, key) for v in value]
    return value


def full_key(name: str, args: dict) -> tuple:
    return name, json.dumps(norm(args), sort_keys=True)


def mutating(name: str, args: dict) -> bool:
    if name in MUTATING_TOOLS:
        return True
    # F2: Bash is mutating unless the router's own classifier calls it a read-only tech-op;
    # unresolvable Bash is mutating.
    return name == "Bash" and technical_ops.classify_tool_call("Bash", args) is None


def prior_calls(item: dict) -> list[tuple[str, dict]]:
    out = []
    for message in item.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for name, body in CALL_RE.findall(str(message.get("content", ""))):
            try:
                args = json.loads(body)
            except ValueError:
                continue
            if isinstance(args, dict):
                out.append((name, args))
    return out


def is_redundant(text: str, item: dict) -> bool:
    """N4: the reply's tool + full normalised input equals an earlier call in the FULL history
    with no mutating call after that earlier call. Gates: scored unacceptable, never judged."""
    try:
        value = base.first_object(text)
    except ValueError:
        return False
    if "final" in value or not isinstance(value.get("input"), dict):
        return False
    key = full_key(str(value.get("tool")), value["input"])
    calls = prior_calls(item)
    for j in range(len(calls) - 1, -1, -1):
        if full_key(*calls[j]) == key:
            return not any(mutating(*later) for later in calls[j + 1:])  # N4-RULE
    return False


def usable(text: str, item: dict) -> bool:
    return valid_reply(text, item) and not is_redundant(text, item)  # VALIDITY-GATE


def edit_shaped(text: str, item: dict) -> bool:
    try:
        value = base.first_object(text)
    except ValueError:
        return False
    args = value.get("input") if isinstance(value.get("input"), dict) else {}
    return "final" not in value and mutating(str(value.get("tool")), args)


def organic_key(item: dict):
    action = item.get("organic_action")
    if not isinstance(action, dict):
        return None
    if "final" in action:
        return ("final",)
    if isinstance(action.get("tool"), str) and isinstance(action.get("target"), str):
        return action["tool"], action["target"]
    return None


# ---- H4: selection ---------------------------------------------------------------------

def equal_threshold(n: int):
    """Smallest k in [ceil(n/2), n] such that equal counts grade eligible for k and every larger k."""
    threshold = None
    for count in range(n, math.ceil(n / 2) - 1, -1):
        if eligibility.grade(eligibility.evidence_from_counts(count, n, count, n)) != "eligible":
            break
        threshold = count
    return threshold


def prompt_tokens_est(item: dict) -> int:
    return sum(len(str(m.get("content", ""))) for m in item["messages"]) // 4


def nearest_rank(values: list[int], share: Fraction) -> int | None:
    ordered = sorted(values)
    return ordered[math.ceil(share * len(ordered)) - 1] if ordered else None


def select_tech_op(pool: list[dict], c: dict) -> dict:
    """No tier filter (C1); org-first (D-206): every eligible org row by hash order, then ho rows
    by hash order, up to N_TARGET; session cap on both; rows above the gate's window are dropped."""
    excluded: Counter = Counter()
    counts = Counter(row.get("stratum", "missing") for row in pool)
    seen, valid = set(), []
    for row in pool:
        ident = row.get("id")
        if ident is not None:
            if ident in seen:
                raise Refuse("refusing: duplicate id")
            seen.add(ident)
        if not REQUIRED <= row.keys() or row.get("set") != SET:
            excluded["missing_field"] += 1
        elif row["stratum"] not in STRATA:
            excluded["unknown_stratum"] += 1
        else:
            valid.append(row)
    # D-206: the proxy local gate forwards a step when tokens > WINDOW_MARGIN x window (local_gate)
    limit = Fraction(c["GATE_WINDOW_FACTOR"]) * local_models.num_ctx(c["LOCAL_MODEL"])
    over_window: Counter = Counter()
    kept = []
    for row in valid:
        if prompt_tokens_est(row) > limit:
            excluded["over_gate_window"] += 1
            over_window[row["stratum"]] += 1
        else:
            kept.append(row)
    seed = c["SELECT_SEED"]
    kept.sort(key=lambda row: base.digest(f"{seed}:{row['id']}"))
    target = int(c["N_TARGET_TECH_OP"])
    cap = math.floor(Fraction(c["SESSION_CAP_SHARE"]) * target)
    chosen: list[dict] = []
    sessions: Counter = Counter()
    for stratum in STRATA:  # org first, then ho; each in hash order
        for row in kept:
            if len(chosen) == target:
                break
            if row["stratum"] != stratum:
                continue
            if sessions[row["session"]] >= cap:
                excluded["session_cap"] += 1
                continue
            chosen.append(row)
            sessions[row["session"]] += 1
    strata = Counter(row["stratum"] for row in chosen)
    n, n_sessions = len(chosen), len(sessions)
    reasons = []
    if n < int(c["N_MIN"]):
        reasons.append("n_min")
    if n_sessions < int(c["MIN_SESSIONS"]):
        reasons.append("sessions")
    if strata.get("org", 0) < int(c["ORG_MIN_N"]):  # N7: freeze needs a real organic stratum
        reasons.append("org_n")
    return {
        "ids": [row["id"] for row in chosen], "n": n, "excluded": dict(excluded),
        "stratum_counts": dict(counts), "over_gate_window_by_stratum": dict(over_window),
        "gate_window_limit": float(limit), "selected_strata": dict(strata),
        "sessions": n_sessions, "org_n": strata.get("org", 0),
        "org_ok": strata.get("org", 0) >= int(c["ORG_MIN_N"]),
        "rowstamp_org": sum(r.get("row_stamp") == "organic" for r in chosen),
        "equal_rate_threshold_k": equal_threshold(n),
        "from_subagent_file": Counter(r["stratum"] for r in chosen
                                      if r.get("from_subagent_file")),
        "freeze": {"ready": not reasons, "reasons": reasons},
        "prompt_tokens_est": {  # §15.7: chars/4 of the rebuilt messages, selected items
            "median": median(prompt_tokens_est(r) for r in chosen) if chosen else None,
            "p90": nearest_rank([prompt_tokens_est(r) for r in chosen], Fraction(9, 10))},
    }


def public(selected: dict) -> dict:
    return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in selected.items()
            if k != "ids"}


def select(cfg: CfgT, c: dict, dry_run: bool = False) -> dict:
    pool = base.jsonl(cfg.run_dir / f"pools/{SET}.jsonl")
    selected = select_tech_op(pool, c)
    if dry_run:
        out = {"sets": {SET: public(selected)}}
        manifest_path = cfg.run_dir / "pools/build_manifest.json"
        if manifest_path.exists():  # H12: the builder's drop counts, counts only
            out["builder_counts"] = base.read_json(manifest_path).get("counts", {})
        return out
    if (cfg.run_dir / "selection.json").exists():
        raise Refuse("refusing: selection already exists")
    manifest_path = cfg.run_dir / "pools/build_manifest.json"
    if not manifest_path.exists() or base.sha256_file(manifest_path) != c["BUILD_MANIFEST_SHA256"]:
        raise Refuse("refusing: build manifest hash")
    manifest = base.read_json(manifest_path)
    sources = manifest.get("sources", {})
    for source, key in (("census_py", "CENSUS_PY_SHA256"),
                        ("proxy_calls_copy", "PROXY_CALLS_COPY_SHA256")):
        if sources.get(source) != c[key]:
            raise Refuse(f"refusing: manifest {source} hash")
    if manifest.get("until") != c["WINDOW_UNTIL"] or manifest.get("since") != c["WINDOW_SINCE"]:
        raise Refuse("refusing: manifest window")
    for key, count in {
        "POOL_COUNT_TECH_OP_ORG": selected["stratum_counts"].get("org", 0),
        "POOL_COUNT_TECH_OP_HO": selected["stratum_counts"].get("ho", 0),
        "N_REALISED_TECH_OP": selected["n"],
        "N_REALISED_TECH_OP_ORG": selected["org_n"],
        "N_REALISED_TECH_OP_ROWSTAMP_ORG": selected["rowstamp_org"],
        "N_REALISED_SESSIONS": selected["sessions"],
    }.items():
        if count != int(c[key]):
            raise Refuse(f"refusing: {key} differs")
    pool_hash = base.sha256_file(cfg.run_dir / f"pools/{SET}.jsonl")
    if pool_hash != c["POOL_TECH_OP_SHA256"]:
        raise Refuse("refusing: pool tech_op hash")
    selected["pool_sha256"] = pool_hash
    for reason in (r for r in selected["freeze"]["reasons"] if r != "org_n"):
        base.append_use(cfg, "terminal", f"{SET} {reason}")
    by_id = {row["id"]: row for row in pool}
    base.atomic(cfg.run_dir / f"items/{SET}.jsonl",
                "".join(json.dumps(by_id[i]) + "\n" for i in selected["ids"]))
    base.write_json(cfg.run_dir / "selection.json",
                    {"label": c["LABEL"], "seed": c["SELECT_SEED"], "sets": {SET: selected}})
    base.append_use(cfg, "selected",
                    f"selection_sha256={base.sha256_file(cfg.run_dir / 'selection.json')}")
    return {"sets": {SET: public(selected)}}


# ---- run (reuses P2.0's arm-major run) plus the equal-window sample -------------------

def rejudge_sample(items: list[dict], c: dict) -> set:
    order = sorted((i["id"] for i in items),
                   key=lambda ident: base.digest(f"{c['SELECT_SEED']}:rejudge:{ident}"))
    return set(order[:math.ceil(Fraction(c["REJUDGE_SHARE"]) * len(items))])


def ew_finals(cfg: CfgT) -> dict:
    return {(r["item_id"], r["arm"]): r
            for r in base.jsonl(cfg.run_dir / f"answers/{SET}_ew.jsonl") if r.get("final")}


def active_arms(cfg: CfgT, c: dict) -> list[str]:
    """P2.0's arm list minus the report-only arm once the budget rule skipped it (N9b)."""
    skipped = {row[2] for row in base.uses(cfg) if len(row) >= 3 and row[0] == "skipped"}
    return [a for a in base.arms(c) if a not in skipped]


def quiet(cfg: CfgT, c: dict, arm: str) -> list[dict]:
    """The local-call gate, re-checked before EVERY local call; returns the readings."""
    try:
        lines = cfg.ps_fn()
    except Exception as exc:  # fail closed: cannot see the process table, so cannot rule P2.0 out
        raise Park("ps failed") from exc
    if not lines:
        raise Park("ps empty")
    if any("p20_requal.py" in line for line in lines):
        raise Park("P2.0 harness running")
    own_basis = c.get("QUIET_FREE_MEM_BASIS") == "excl_own_arm"  # AMEND1-T
    readings = []
    for reading in range(int(c["QUIET_READINGS"])):
        if reading:
            cfg.sleep(float(c["QUIET_INTERVAL_S"]))
        free, load = cfg.machine.free_mem_pct(), cfg.machine.load1()
        own = (100.0 * cfg.ollama.resident_bytes(arm) / cfg.mem_total_fn()
               if own_basis else 0.0)
        readings.append({"free_pct": free, "load1": load, **({"own_pct": own} if own_basis else {})})
        if (not math.isfinite(free) or not math.isfinite(load)
                or not math.isfinite(own) or own < 0
                or free + own < float(c["QUIET_FREE_MEM_MIN_PCT"])
                or load >= float(c["QUIET_LOAD1_MAX"])):
            raise Park("machine not quiet")
    if any(model != arm for model in cfg.ollama.ps()):
        raise Park("foreign model resident")
    return readings


@contextmanager
def ollama_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Park("ollama lock held") from exc
        yield
    finally:
        os.close(fd)  # closing releases the flock


@contextmanager
def local_gate(cfg: CfgT, c: dict, arm: str):
    """Lock, then quiet, then the call (the P2.0 AMEND3 order); yields the quiet readings."""
    with ollama_lock(cfg.lock_path):
        readings = quiet(cfg, c, arm)
        cfg.local_touched = True  # the model may now be resident because of this process
        yield readings


def local_call(cfg: CfgT, c: dict, arm: str, ident: str, attempt: int, messages: list[dict]):
    with local_gate(cfg, c, arm) as readings:
        row = base.call_model(cfg, c, SET, arm, ident, attempt, messages)
    row["quiet"] = readings
    return row


def unload_if_touched(cfg: CfgT, arm: str) -> None:
    """BLOCKER-1: unload only what this process used, under the lock, never waiting for it."""
    if not cfg.local_touched:
        return
    try:
        with ollama_lock(cfg.lock_path):
            cfg.ollama.unload(arm)
    except Park:
        pass  # the sibling harness holds the lock and may be using the model: leave it loaded


def unchecked_local_rows(cfg: CfgT, c: dict) -> int:
    """Local answer rows written without a passing quiet re-check (must be 0): a row with no
    readings, fewer than QUIET_READINGS, or any reading outside the thresholds."""
    own_basis = c.get("QUIET_FREE_MEM_BASIS") == "excl_own_arm"  # AMEND1-T

    def own(r: dict) -> float:
        value = r.get("own_pct") if own_basis else 0.0
        return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 \
            else float("nan")

    def bad(row: dict) -> bool:
        readings = row.get("quiet")
        if not isinstance(readings, list) or len(readings) != int(c["QUIET_READINGS"]):
            return True
        return any(not math.isfinite(r["free_pct"]) or not math.isfinite(r["load1"])
                   or not math.isfinite(own(r))
                   or r["free_pct"] + own(r) < float(c["QUIET_FREE_MEM_MIN_PCT"])
                   or r["load1"] >= float(c["QUIET_LOAD1_MAX"]) for r in readings)
    return sum(bad(r) for r in base.jsonl(cfg.run_dir / f"answers/{SET}.jsonl")
               if r["arm"] == c["LOCAL_MODEL"])


def over_attempts(cfg: CfgT, c: dict) -> int:
    """Items with more than RETRIES+1 attempt rows in any arm (must be 0)."""
    rows = Counter((r["item_id"], r["arm"]) for r in base.jsonl(cfg.run_dir / f"answers/{SET}.jsonl"))
    return sum(n > int(c["RETRIES"]) + 1 for n in rows.values())


def spent(cfg: CfgT) -> Fraction:
    return sum((Fraction(str(r.get("cost_usd", 0))) for r in base.jsonl(cfg.run_dir / "ledger.jsonl")),
               Fraction(0))


def judge_reserve(cfg: CfgT, c: dict, items: list[dict]) -> Fraction:
    """N9b: projected judge cost (n calls + the re-judge sample) at 1.25 x the per-call estimate."""
    costs = [Fraction(str(r.get("cost_usd", 0))) for r in base.jsonl(cfg.run_dir / "ledger.jsonl")
             if r.get("stage") == "judge" and r.get("cost_usd", 0) > 0]
    est = Fraction(5, 4) * (max(costs) if costs else Fraction(c["FIRST_CALL_EST_USD"]))
    return est * (len(items) + len(rejudge_sample(items, c)))


def projected(cfg: CfgT, c: dict, n: int) -> Fraction:
    costs = [Fraction(str(r.get("cost_usd", 0))) for r in base.jsonl(cfg.run_dir / "ledger.jsonl")
             if r.get("stage") == "run" and r.get("arm") == c["REFERENCE"]]
    each = sum(costs, Fraction(0)) / len(costs) if costs else Fraction(c["FIRST_CALL_EST_USD"])
    return Fraction(5, 4) * each * n


def run(cfg: CfgT, c: dict, chosen_arms: list[str] | None = None) -> dict:
    base.require_unused(cfg)
    all_arms = base.arms(c)
    base.require_no_foreign(cfg.run_dir / f"answers/{SET}.jsonl", all_arms)
    selected = base.selection(cfg)
    base.check_set(cfg, SET)
    if (cfg.run_dir / f"judgements/{SET}.jsonl").exists():
        raise Refuse("refusing: judging started")
    arms = active_arms(cfg, c)
    chosen_arms = chosen_arms or arms
    if any(arm not in all_arms for arm in chosen_arms):
        raise Refuse("refusing: unknown arm")
    items = base.items_for(cfg, SET, selected)
    done = base.finals(cfg, SET)
    local_arm, count = c["LOCAL_MODEL"], 0
    cfg.local_touched = False
    answers_path = cfg.run_dir / f"answers/{SET}.jsonl"
    for arm in chosen_arms:
        if arm not in active_arms(cfg, c):
            continue
        local = arm == local_arm
        if local and cfg.ollama.list_ids().get(arm) != set(c[f"ID_{arm}"].split(",")):
            raise Refuse(f"refusing: {arm} identity differs (not a result)")
        pending = [item for item in items if (item["id"], arm) not in done]
        if not pending:
            continue
        if not local and (spent(cfg) + judge_reserve(cfg, c, items)
                          + (projected(cfg, c, len(pending)) if arm != c["REFERENCE"] else 0)
                          > Fraction(c["SPEND_CAP_USD"])):  # N9-RESERVE
            if arm == c["REFERENCE"]:
                raise Park("spend reserve")
            base.append_use(cfg, "skipped", f"{arm} haiku_skipped_budget")
            continue
        try:
            if local:
                warm = [{"role": "user", "content": "Reply OK."}]
                with local_gate(cfg, c, arm):
                    cfg.ollama.chat(arm, warm, timeout=int(c["LOCAL_TIMEOUT_S"]),
                                    options={"num_ctx": local_models.num_ctx(arm), "temperature": 0},
                                    think=False)
            prior = Counter(r["item_id"] for r in base.jsonl(answers_path)
                            if r["arm"] == arm and not r.get("final"))
            for item in pending:
                window = local_models.num_ctx(arm) if local else int(c["REF_WINDOW"])
                messages = cfg.pack_fn(item, SET, window)
                # weak spot (d): resume at the next attempt number, so no item exceeds RETRIES+1
                for attempt in range(prior[item["id"]] + 1, int(c["RETRIES"]) + 2):
                    row = (local_call(cfg, c, arm, item["id"], attempt, messages) if local
                           else base.call_model(cfg, c, SET, arm, item["id"], attempt, messages))
                    row["final"] = row["ok"] or attempt == int(c["RETRIES"]) + 1
                    base.append_row(cfg.run_dir / f"answers/{SET}.jsonl", row)
                    count += 1
                    if row["final"]:
                        break
        finally:
            if local:
                unload_if_touched(cfg, arm)
    claude_arms = [a for a in chosen_arms if a != local_arm and a in active_arms(cfg, c)]
    if claude_arms:
        sample = [i for i in items if i["id"] in rejudge_sample(items, c)]
        ew = ew_finals(cfg)
        for arm in claude_arms:
            for item in sample:
                if (item["id"], arm) in ew:
                    continue
                messages = cfg.pack_fn(item, SET, EW_WINDOW)
                for attempt in range(1, int(c["RETRIES"]) + 2):
                    row = base.call_model(cfg, c, SET, arm, item["id"], attempt, messages)
                    row["final"] = row["ok"] or attempt == int(c["RETRIES"]) + 1
                    row["window"] = EW_WINDOW
                    base.append_row(cfg.run_dir / f"answers/{SET}_ew.jsonl", row)
                    if row["final"]:
                        break
    return {"attempts": count, "finals": len(base.finals(cfg, SET)),
            "ew_finals": len(ew_finals(cfg)),
            "items_over_retries_plus_1_attempt_rows": over_attempts(cfg, c),
            "items_run_after_failed_recheck": unchecked_local_rows(cfg, c)}


# ---- H7: plants and judge --------------------------------------------------------------

def plant_candidates(items: list[dict], answers: dict, c: dict) -> tuple[dict, dict]:
    order = sorted((i["id"] for i in items),
                   key=lambda i: base.digest(f"{c['SELECT_SEED']}:plant:{i}"))
    by_id = {i["id"]: i for i in items}
    plants: dict[str, dict] = {}
    swapped, degraded, repeat = (int(c[k]) for k in
                                 ("PLANTS_SWAPPED", "PLANTS_DEGRADED", "PLANTS_REPEAT"))
    skipped = {"swapped": 0, "repeat": 0, "repeat_plant_precheck_redundant": 0}
    for j, ident in enumerate(order[:swapped]):
        source = order[(j + 1) % swapped] if len(order) >= swapped else None
        answer = answers.get((source, c["REFERENCE"]), {})
        if answer.get("ok") and valid_reply(answer["text"], by_id[source]):
            plants.setdefault(ident, {})["__swap__"] = answer["text"]
        else:
            skipped["swapped"] += 1
    for ident in order[swapped:swapped + degraded]:
        plants.setdefault(ident, {})["__degraded__"] = '{"final": "Done."}'
    for ident in order[swapped + degraded:swapped + degraded + repeat]:  # REPEAT-PLANT
        calls = prior_calls(by_id[ident])
        text = json.dumps({"tool": calls[-1][0], "input": calls[-1][1]}) if calls else ""
        if text:  # F1: a plant bypasses the validity and redundant pre-checks
            plants.setdefault(ident, {})["__repeat__"] = text
            skipped["repeat_plant_precheck_redundant"] += is_redundant(text, by_id[ident])
        else:
            skipped["repeat"] += 1
    start = swapped + degraded + repeat
    for ident in order[start:start + int(c["PLANTS_GENERIC"])]:
        plants.setdefault(ident, {})["__generic__"] = GENERIC_PLANT
    return plants, {"plants_skipped": skipped}


def judge_call(cfg: CfgT, c: dict, item: dict, candidates: dict, second: bool = False):
    seed = c["SELECT_SEED"] + (":perm2" if second else "")
    names = sorted(candidates, key=lambda arm: base.digest(f"{seed}:{item['id']}:{arm}"))
    letters = {chr(65 + i): arm for i, arm in enumerate(names)}
    if not letters:
        return letters, {}, False, 0
    context = base.flatten(cfg.pack_fn(item, SET, int(c["REF_WINDOW"])))
    prompt = (JUDGE_BRIEF_TECH_OP_T + "\n" + base.JUDGE_REPLY + "\nContext:\n"
              + context[-int(c["JUDGE_CONTEXT_CAP_CHARS"]):]
              + "\nRequest:\n" + item["request"] + "\n")
    prompt += "\n\n".join(f"Answer {letter}:\n{candidates[arm]}" for letter, arm in letters.items())
    for attempt in range(1, int(c["RETRIES"]) + 2):
        row = base.call_model(cfg, c, SET, c["JUDGE"], item["id"], attempt,
                              [{"role": "user", "content": prompt}], stage="judge")
        try:
            values = base.first_object(row["text"]) if row["ok"] else {}
            valid = values.keys() == letters.keys() and all(
                v in ("acceptable", "unacceptable") for v in values.values())
        except ValueError:
            valid = False
        if valid:
            return letters, {letters[k]: v == "acceptable" for k, v in values.items()}, False, attempt
    return letters, {}, True, int(c["RETRIES"]) + 1


def shown(item: dict, text: str) -> bool:
    return usable(text, item)


def judge(cfg: CfgT, c: dict) -> dict:
    base.require_unused(cfg)
    arms = active_arms(cfg, c)
    base.require_no_foreign(cfg.run_dir / f"answers/{SET}.jsonl", arms)
    base.require_no_foreign(cfg.run_dir / f"answers/{SET}_ew.jsonl", arms)
    base.check_set(cfg, SET)
    selected = base.selection(cfg)
    items = base.items_for(cfg, SET, selected)
    answers = base.finals(cfg, SET)
    if any((item["id"], arm) not in answers for item in items for arm in arms):
        raise Refuse("refusing: run incomplete")
    sample = rejudge_sample(items, c)
    claude_arms = [a for a in arms if a != c["LOCAL_MODEL"]]
    ew = ew_finals(cfg)
    if any((i["id"], a) not in ew for i in items if i["id"] in sample for a in claude_arms):
        raise Refuse("refusing: equal-window sample incomplete")
    done = {row["item_id"] for row in base.jsonl(cfg.run_dir / f"judgements/{SET}.jsonl")}
    plants, stats = plant_candidates(items, answers, c)
    validity = {arm: {"checked": 0, "valid": 0, "redundant": 0} for arm in arms}
    for item in items:
        ident = item["id"]
        candidates, hidden = {}, []
        for arm in arms:
            row = answers[ident, arm]
            if not row["ok"]:
                continue
            validity[arm]["checked"] += 1
            if valid_reply(row["text"], item):
                validity[arm]["valid"] += 1
            if shown(item, row["text"]):
                candidates[arm] = row["text"]
            else:
                hidden.append(arm)
                validity[arm]["redundant"] += valid_reply(row["text"], item)
        if ident in done:
            continue
        letters, verdicts, error, attempts = judge_call(
            cfg, c, item, candidates | plants.get(ident, {}))
        rejudge, rejudge_error = None, False
        if ident in sample:
            _, rejudge, rejudge_error, extra = judge_call(cfg, c, item, candidates, True)
            attempts += extra
        base.append_row(cfg.run_dir / f"judgements/{SET}.jsonl", {
            "item_id": ident, "letters": letters,
            "verdicts": {arm: verdicts[arm] for arm in candidates if arm in verdicts},
            "rejudge_verdicts": rejudge, "rejudge_error": rejudge_error,
            "judge_error": error, "attempts": attempts, "not_shown_invalid": hidden,
            "plants": {arm: value for arm, value in verdicts.items() if arm.startswith("__")},
            "ts": base.utc(cfg),
        })
    ew_done = {r["item_id"] for r in base.jsonl(cfg.run_dir / f"judgements/{SET}_ew.jsonl")}
    for item in items:
        if item["id"] not in sample or item["id"] in ew_done:
            continue
        cands = {f"{a}@{EW_WINDOW // 1024}k": ew[item["id"], a]["text"] for a in claude_arms
                 if ew[item["id"], a]["ok"] and shown(item, ew[item["id"], a]["text"])}
        _, verdicts, error, _ = judge_call(cfg, c, item, cands)
        base.append_row(cfg.run_dir / f"judgements/{SET}_ew.jsonl", {
            "item_id": item["id"], "verdicts": verdicts, "judge_error": error,
            "ts": base.utc(cfg)})
    return {"judged": len(items), "validity": validity, **stats}


# ---- H6/H8: score ----------------------------------------------------------------------

def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": k / n if n else None,
            "ci": eligibility._wilson(k, n, 1.959964) if n else None}


def percentile95(values: list[float]):
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None


def grade_of(k: int, n: int, kr: int) -> tuple[str, float | None]:
    ev = eligibility.evidence_from_counts(k, n, kr, n)
    return eligibility.grade(ev), ev.gap_pts if n else None


def passes_waived(k: int, n: int, kr: int) -> bool:
    """The grade rule with the n >= 60 clause waived (F3): gap <= 3.0 and LB >= -10.0."""
    ev = eligibility.evidence_from_counts(k, n, kr, n)
    return bool(n) and ev.gap_pts <= eligibility.PAIRS_MAX_GAP_PTS + 1e-9 \
        and ev.lb_pts >= eligibility.PAIRS_MIN_LB - 1e-9


def loso(paired: list[dict], outcomes: dict, refs: dict) -> dict:
    """Leave-one-session-out (report; downgrades an eligible lane, N8b/F3)."""
    k, kr = sum(outcomes[i["id"]] for i in paired), sum(refs[i["id"]] for i in paired)
    full = passes_waived(k, len(paired), kr)
    gaps, flips = [], 0
    for session in sorted({i["session"] for i in paired}):
        rest = [i for i in paired if i["session"] != session]
        rk, rr = sum(outcomes[i["id"]] for i in rest), sum(refs[i["id"]] for i in rest)
        _, gap = grade_of(rk, len(rest), rr)
        flips += full and not passes_waived(rk, len(rest), rr)
        if gap is not None:
            gaps.append(gap)
    return {"sessions": len({i["session"] for i in paired}), "gap_min": min(gaps, default=None),
            "gap_max": max(gaps, default=None), "full_passes": full,
            "session_sensitive": flips > 0}


def subagent_free(paired: list[dict], outcomes: dict, refs: dict) -> dict:
    """N1 branch B: the grade printed without steps found only in a sub-agent file."""
    rest = [i for i in paired if not i.get("from_subagent_file")]
    k, kr = sum(outcomes[i["id"]] for i in rest), sum(refs[i["id"]] for i in rest)
    g, gap = grade_of(k, len(rest), kr)
    return {"k": k, "n": len(rest), "ref_k": kr, "grade": g, "gap_pts": gap,
            "removed": len(paired) - len(rest)}


def secondary(arm: str, items: list[dict], answers: dict, judged: dict, ref: str) -> dict:
    """§7 secondary outcomes for one arm, per stratum and pooled."""
    out: dict = {}
    for stratum in (*STRATA, "all"):
        rows = [i for i in items if stratum == "all" or i["stratum"] == stratum]
        got = {i["id"]: answers.get((i["id"], arm), {}) for i in rows}
        texts = {ident: r["text"] for ident, r in got.items() if r.get("ok")}
        is_valid = {i["id"]: i["id"] in texts and valid_reply(texts[i["id"]], i) for i in rows}
        edit = {i["id"]: is_valid[i["id"]] and edit_shaped(texts[i["id"]], i) for i in rows}
        concord = {i["id"]: is_valid[i["id"]] and organic_key(i) is not None
                   and reply_key(texts[i["id"]]) == organic_key(i) for i in rows}
        repeat = {i["id"]: is_valid[i["id"]] and is_redundant(texts[i["id"]], i) for i in rows}
        served = [i for i in rows if is_valid[i["id"]] and not edit[i["id"]]]
        anchor = [judged.get(i["id"], {}).get("verdicts", {}).get(arm) is True
                  for i in rows if concord[i["id"]] and arm in judged.get(i["id"], {}).get(
                      "verdicts", {})]
        finals = [r for r in got.values() if r]
        times = [r["wall_s"] for r in finals if r.get("ok") and isinstance(r.get("wall_s"), (int, float))]
        errors = Counter(r.get("error") for r in finals if not r.get("ok"))
        timeouts = sum(v for k, v in errors.items() if k == "exception" or "timeout" in str(k))
        out[stratum] = {
            "concordance_organic": rate(sum(concord.values()), len(rows)),
            "redundant_repeat": rate(sum(repeat.values()), sum(is_valid.values())),
            "validity": rate(sum(is_valid.values()), len(rows)),
            "edit_shaped": rate(sum(edit.values()), sum(is_valid.values())),
            "served_subset_ids": [i["id"] for i in served],
            "timeout": rate(timeouts, len(rows)), "errors": dict(errors),
            "anchor_acceptable_when_concordant": rate(sum(anchor), len(anchor)),
            "judge_rejects_organic": bool(anchor) and Fraction(sum(anchor), len(anchor)) < Fraction(9, 10),
            "latency_s": {"median": median(times) if times else None,
                          "p95": percentile95(times), "n": len(times),
                          "prompt_tokens_est_median": median(
                              prompt_tokens_est(i) for i in rows) if rows else None},
            "by_organic_tool": dict(Counter(
                (i["organic_action"].get("tool") or "final") for i in rows)),
        }
    return out


def combined_hash(cfg: CfgT, folder: str) -> str:
    paths = [cfg.run_dir / folder / f"{SET}{suffix}.jsonl" for suffix in ("", "_ew")]
    present = [p for p in paths if p.exists()]
    if not present:
        return "none"
    return base.hashlib.sha256(b"".join(p.read_bytes() for p in present)).hexdigest()


def score(cfg: CfgT, c: dict, freeze: dict, measurement: Path | None = None) -> dict:
    base.require_unused(cfg)
    arms = active_arms(cfg, c)
    local = c["LOCAL_MODEL"]
    base.require_no_foreign(cfg.run_dir / f"answers/{SET}.jsonl", arms)
    base.require_no_foreign(cfg.run_dir / f"answers/{SET}_ew.jsonl", arms)
    hashes = {"prereg_sha256": freeze["sha256"], "amend_sha256": freeze.get("amend1_sha256", "none"),
              "git_sha": cfg.git_sha(),
              "selection_sha256": base.optional_hash(cfg.run_dir / "selection.json"),
              "answers_sha256": combined_hash(cfg, "answers"),
              "judgements_sha256": combined_hash(cfg, "judgements")}
    base.append_use(cfg, "started", " ".join(f"{k}={v}" for k, v in hashes.items()))
    selected = base.selection(cfg)
    ids = selected[SET]["ids"]
    items = base.items_for(cfg, SET, selected)
    n_selected = len(ids)
    reason = None
    try:
        base.check_set(cfg, SET)
    except Terminal:
        reason = "terminal"
    if n_selected < int(c["N_MIN"]):
        reason = reason or "n_min"
    if len({i["session"] for i in items}) < int(c["MIN_SESSIONS"]):
        reason = reason or "sessions"
    answers = base.finals(cfg, SET)
    rows = base.jsonl(cfg.run_dir / f"judgements/{SET}.jsonl")
    judged = {r["item_id"]: r for r in rows}
    if len(judged) != len(rows) or set(judged) != set(ids):
        reason = reason or "incomplete"
    calibration = {}
    for kind in KINDS:
        values = [r["plants"][kind] for r in rows if kind in r.get("plants", {})]
        m, rejected = len(values), sum(value is False for value in values)
        calibration[kind] = {"placed": m, "rejected": rejected}
        if (m < int(c["PLANTS_MIN_PLACED"])
                or rejected < math.ceil(Fraction(c["PLANTS_PASS_SHARE"]) * m)):
            reason = reason or "calibration"
    ref = c["REFERENCE"]
    paired = [i for i in items if answers.get((i["id"], ref), {}).get("ok")
              and i["id"] in judged and not judged[i["id"]]["judge_error"]]
    missing = n_selected - len(paired)
    if n_selected and Fraction(missing, n_selected) > Fraction(c["MAX_REF_MISSING_SHARE"]):
        reason = reason or "ref_missing"
    if len(paired) < int(c["N_MIN"]):
        reason = reason or "n_min"

    def accepted(item: dict, arm: str) -> bool:  # H7: valid AND judge-acceptable
        row = answers.get((item["id"], arm), {})
        return bool(row.get("ok") and usable(row["text"], item)  # VALIDITY-SCORE
                    and judged[item["id"]]["verdicts"].get(arm) is True)

    refs = {i["id"]: accepted(i, ref) for i in paired}
    rk = rn = 0
    for row in rows:
        second = row.get("rejudge_verdicts")
        if second is not None and not row.get("rejudge_error"):
            for arm, value in second.items():
                if arm in row["verdicts"]:
                    rn += 1
                    rk += value == row["verdicts"][arm]
    stats = {
        "calibration": calibration, "ref_missing": missing,
        "reference": {**rate(sum(refs.values()), len(paired)),
                      "truncated": sum(answers.get((i, ref), {}).get("truncated", False)
                                       for i in ids)},
        "rejudge": {"k": rk, "n": rn},
        "judge_order_sensitive": bool(rn and Fraction(rk, rn) < Fraction(c["REJUDGE_FLAG_BELOW"])),
        "n_selected": n_selected, "sessions": len({i["session"] for i in items}),
    }
    result: dict = {**hashes, "scored_utc": base.utc(cfg), "sets": {}}
    reports = {}
    for arm in arms:
        if arm == ref:
            continue
        outcomes = {i["id"]: accepted(i, arm) for i in paired}
        lane = base.metrics([(outcomes[i["id"]], refs[i["id"]]) for i in paired], reason, c)
        strata: dict = {}
        for s in STRATA:
            sub = [i for i in paired if i["stratum"] == s]
            sk, sr = sum(outcomes[i["id"]] for i in sub), sum(refs[i["id"]] for i in sub)
            g, gap = grade_of(sk, len(sub), sr)
            strata[s] = {"k": sk, "n": len(sub), "ref_k": sr, "gap_pts": gap}
        check = "not_applicable"
        sens = loso(paired, outcomes, refs)
        if lane["verdict"] == "eligible":
            org = strata["org"]  # ORG-STRATUM: ho alone never makes a lane eligible
            if org["n"] < int(c["ORG_MIN_N"]):
                check = "org_too_small"
            elif org["gap_pts"] > float(c["ORG_MAX_GAP_PTS"]):  # ORG-CHECK
                check = "org_disagrees"
            else:
                check = "pass"
            if check == "pass" and sens["session_sensitive"]:
                check = "session_sensitive"
            if check != "pass":
                lane["verdict"], lane["eligible"] = "inconclusive", False
        lane.update(
            basis=c["BASIS"], source="p2.0-t",
            population="replayed real steps (offline), strata org+ho", strata=strata,
            org_check=check, prereg_sha256=freeze["sha256"], scored_utc=result["scored_utc"],
            removed=n_selected - len(paired), report_only=arm in c["REPORT_ARMS"].split(","),
            truncated=sum(answers.get((i, arm), {}).get("truncated", False) for i in ids),
            agreement={"k": sum(bool(answers.get((i["id"], arm), {}).get("ok"))
                                and base.agreement(answers[i["id"], arm]["text"],
                                                   answers[i["id"], ref]["text"])
                                for i in paired), "n": len(paired)},
            session_sensitivity=sens,
            without_subagent_file=subagent_free(paired, outcomes, refs),
            secondary=secondary(arm, items, answers, judged, ref),
        )
        reports[arm] = lane
        if arm == local:
            record_competence(
                arm, SET, {tier: lane for tier in c["TIERS_WRITTEN"].split(",")},
                path=cfg.run_dir / "local_eligibility.json")
    stats["secondary_reference"] = secondary(ref, items, answers, judged, ref)
    ew_rows = {r["item_id"]: r for r in base.jsonl(cfg.run_dir / f"judgements/{SET}_ew.jsonl")}
    ew = {"n": 0}
    sample_ids = [i["id"] for i in paired if i["id"] in ew_rows and not ew_rows[i["id"]]["judge_error"]]
    ew["n"] = len(sample_ids)
    for arm in (a for a in arms if a != local):
        label = f"{arm}@{EW_WINDOW // 1024}k"
        ew[arm] = {
            "equal_window": rate(sum(ew_rows[i]["verdicts"].get(label) is True for i in sample_ids),
                                 len(sample_ids)),
            "main_window": rate(sum(judged[i]["verdicts"].get(arm) is True for i in sample_ids),
                                len(sample_ids))}
    stats["equal_window_sample"] = ew
    if reason:
        base.append_use(cfg, "terminal", f"{SET} {reason}")
    elif all(r["verdict"] == "not_informative" for r in reports.values()):
        reason = "n_min"
        base.append_use(cfg, "terminal", f"{SET} n_min")
    result["sets"][SET] = {"lanes": reports, **stats}
    lane = reports[local]
    result.update(
        verdict=lane["verdict"], not_informative={SET: reason} if reason else {},
        cancel_p21=not reason and lane["verdict"] == "failed",
        p21_allowed=lane["verdict"] == "eligible",
        e_tech={"model": local, "k": lane["k"], "n": lane["n"]} if lane["eligible"] else None,
    )
    base.write_json(cfg.run_dir / "result.json", result)
    base.append_use(cfg, "scored", f"result_sha256={base.sha256_file(cfg.run_dir / 'result.json')}")
    if measurement:
        base.write_json(measurement, {**result, "selected_ids": {SET: ids}})
    return {"lanes": {a: {k: r[k] for k in ("k", "n", "grade", "verdict", "org_check")}
                      for a, r in reports.items()},
            **{k: result[k] for k in ("verdict", "cancel_p21", "p21_allowed", "not_informative")}}


def status(cfg: CfgT) -> dict:
    gate_status, c = "n/a", STATIC
    try:
        freeze = frozen(cfg)
        is_frozen, c = True, freeze["constants"]
    except Refuse:
        is_frozen = False
    else:
        try:
            run_gate(cfg, c)
            gate_status = "ok"
        except Refuse as exc:
            gate_status = str(exc)
    path = cfg.run_dir / "selection.json"
    selected = base.read_json(path).get("sets", {}) if path.exists() else {}
    pool = cfg.run_dir / f"pools/{SET}.jsonl"
    ready = select_tech_op(base.jsonl(pool), c)["freeze"] if pool.exists() else None
    arms = base.arms(c)
    return {
        "frozen": is_frozen, "code_gate": gate_status, "pool": pool.exists(),
        "freeze_status": (None if ready is None else "ready" if ready["ready"] else "no_freeze"),
        "freeze_reasons": ready["reasons"] if ready else [],
        "selection_n": {s: r["n"] for s, r in selected.items()},
        "finals": dict(Counter(arm for _, arm in base.finals(cfg, SET))),
        "finals_ew": dict(Counter(arm for _, arm in ew_finals(cfg))),
        "judged": len(base.jsonl(cfg.run_dir / f"judgements/{SET}.jsonl")),
        "foreign_arm_rows": {
            SET: base.foreign_rows(cfg.run_dir / f"answers/{SET}.jsonl", arms),
            f"{SET}_ew": base.foreign_rows(cfg.run_dir / f"answers/{SET}_ew.jsonl", arms)},
        "spend_usd": float(sum(Fraction(str(r.get("cost_usd", 0)))
                              for r in base.jsonl(cfg.run_dir / "ledger.jsonl"))),
        "use_kinds": dict(Counter(r[0] for r in base.uses(cfg) if r)),
    }


H13_PATHS = ("src/llm_router/proxy/local_gate.py", "src/llm_router/resolver/eligibility_store.py")


def h13_check(repo: Path, sha: str) -> None:
    """H13/N12: REPO_SHA holds the proxy local gate and #523 (record_competence)."""
    def git(*args: str):
        return subprocess.run(["git", *args], cwd=repo, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    store = git("show", f"{sha}:{H13_PATHS[1]}")
    if (git("cat-file", "-e", f"{sha}:{H13_PATHS[0]}").returncode != 0
            or store.returncode != 0 or "def record_competence" not in store.stdout):
        raise Refuse("refusing: REPO_SHA lacks the proxy local gate or record_competence (H13)")


def run_gate(cfg: CfgT, constants: dict) -> None:
    if cfg.gate is not None:
        cfg.gate(constants)
    else:
        h13_check(REPO, constants["REPO_SHA"])
        base.code_gate(REPO, constants["REPO_SHA"], llm_router.__file__)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, default=default_dir())
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("select").add_argument("--dry-run", action="store_true")
    for command in ("run", "judge"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--set", choices=(SET,), required=True)
        if command == "run":
            cmd.add_argument("--arms", nargs="+")
    sub.add_parser("score").add_argument("--measurement", type=Path)
    sub.add_parser("install-eligibility").add_argument("--confirm", action="store_true")
    cmd = sub.add_parser("tp2")
    cmd.add_argument("--f-fit", default=None)
    for flag in ("s-tech", "s-tech-sessions", "s-tech-largest", "s-cheap"):
        cmd.add_argument("--" + flag, required=True, type=int if flag.endswith("sessions") else str)
    cmd.add_argument("--baseline")
    cmd.add_argument("--evidence", type=Path, action="append", default=[])
    sub.add_parser("status")
    return ap


def real_cfg(run_dir: Path) -> CfgT:
    def git_sha() -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=True).stdout.strip()

    return CfgT(run_dir, ollama=OllamaT(), claude=base.ClaudeCLI(run_dir),
                machine=base.Machine(), quota=base.read_quota, git_sha=git_sha)


def main(argv: list[str] | None = None, *, cfg: CfgT | None = None) -> int:
    args = parser().parse_args(argv)
    cfg = cfg or real_cfg(args.run_dir)
    try:
        prefreeze = args.command == "status" or (args.command == "select" and args.dry_run)
        freeze = None if prefreeze else frozen(cfg)
        c = STATIC if freeze is None else freeze["constants"]
        if freeze is not None:
            run_gate(cfg, c)
        if args.command == "status":
            out = status(cfg)
        elif args.command == "select":
            out = select(cfg, c, args.dry_run)
        elif args.command == "run":
            out = run(cfg, c, args.arms)
        elif args.command == "judge":
            out = judge(cfg, c)
        elif args.command == "score":
            out = score(cfg, c, freeze, args.measurement)
        elif args.command == "install-eligibility":
            out = base.install(cfg, args.confirm)
        else:
            out = base.tp2(cfg, {**base.CONSTANTS, **c}, args)
        print(json.dumps(out, sort_keys=True, allow_nan=False))
        return 0
    except (Refuse, Park, Terminal, OSError, ValueError, KeyError, TypeError) as exc:
        code = 3 if isinstance(exc, Park) else 4 if isinstance(exc, Terminal) else 2
        print(str(exc) if isinstance(exc, (Refuse, Park, Terminal)) else "refusing: invalid input",
              file=sys.stderr)
        return code


if __name__ == "__main__":
    sys.exit(main())
