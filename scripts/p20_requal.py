#!/usr/bin/env python3
"""Single-use, frozen P2.0 offline requalification."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

import llm_router
from llm_router import eligibility, local_models
from llm_router.context_pack import build_pack, to_messages
from llm_router.resolver.eligibility_store import record_competence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import check_prereg  # noqa: E402

PREREG = REPO / "docs/prereg/PREREG-P20-requal.md"
AMEND1 = REPO / "docs/prereg/PREREG-P20-requal-AMEND1.md"
AMEND2 = REPO / "docs/prereg/PREREG-P20-requal-AMEND2.md"
AMEND3 = REPO / "docs/prereg/PREREG-P20-requal-AMEND3.md"
AMEND4 = REPO / "docs/prereg/PREREG-P20-requal-AMEND4.md"
# AMEND3 (D-202): the exclusive lock a local model call holds; the P2.0-T harness takes the same
# one (fcntl.flock LOCK_EX on this path). Tests point Cfg.lock_path or this name elsewhere.
OLLAMA_LOCK = Path.home() / ".rsi/research/primary-plan/v16/requal/ollama.lock"
SETS = ("qa", "tech_op", "code")
CONSTANTS: dict[str, str] = {
    "LABEL": "p20-requal",
    "SELECT_SEED": "20261016",
    "N_TARGET_QA": "120",
    "N_TARGET_TECH_OP": "120",
    "N_MIN": "60",
    "SESSION_CAP_SHARE": "0.20",
    "LOCAL_MODELS": "qwen3.6:35b-a3b-coding,qwen3-coder:30b,qwen3.5:latest",
    "CODE_LOCAL_MODELS": "qwen3.6:35b-a3b-coding,qwen3-coder:30b",
    "SIZE_ORDER": "qwen3.5:latest,qwen3-coder:30b,qwen3.6:35b-a3b-coding",
    "REFERENCE": "sonnet",
    "REPORT_ARMS": "haiku",
    "JUDGE": "opus",
    "REF_WINDOW": "65536",
    "LOCAL_TIMEOUT_S": "180",
    "REF_TIMEOUT_S": "300",
    "RETRIES": "1",
    "ANSWER_CAP_CHARS": "4000",
    "JUDGE_CONTEXT_CAP_CHARS": "24000",
    "PLANTS_SWAPPED": "6",
    "PLANTS_DEGRADED": "6",
    "PLANTS_PASS_SHARE": "5/6",
    "PLANTS_MIN_PLACED": "4",
    "DEGRADED_MIN_CHARS": "400",
    "DEGRADED_KEEP_SHARE": "0.20",
    "REJUDGE_SHARE": "0.10",
    "REJUDGE_FLAG_BELOW": "0.80",
    "MAX_REF_MISSING_SHARE": "0.10",
    "QA_TYPE_MIN_ITEMS": "20",
    "QUIET_FREE_MEM_MIN_PCT": "40",
    "QUIET_LOAD1_MAX": "4.0",
    "QUIET_READINGS": "3",
    "QUIET_INTERVAL_S": "10",
    "NIGHT_WINDOW": "00:00-07:00",
    "QUOTA_SESSION_PAUSE_PCT": "80",
    "QUOTA_WEEKLY_PAUSE_PCT": "85",
    "QUOTA_MAX_AGE_S": "1800",
    "SPEND_CAP_USD": "60.00",
    "FIRST_CALL_EST_USD": "0.30",
    "SAFETY_FACTOR": "0.8",
    "G_MIN_PTS": "5.0",
    "TP2_REPORT_BELOW": "0.20",
    "CEILING_NOTE_BELOW": "0.10",
    "S_TECH_MIN_N": "60",
    "S_TECH_MIN_SESSIONS": "3",
    "S_TECH_MAX_SESSION_SHARE": "0.60",
    "S_TECH_FALLBACK": "1/45",
    "S_TECH_FALLBACK_HIGH": "24/45",
    "TIERS_WRITTEN": "simple,moderate",
    "QA_BASELINE": "7/22",
    "FITNESS_PASS_MARK": "1.0",
    "ID_qwen3.6:35b-a3b-coding": "017eda0b7c34",
    "ID_qwen3-coder:30b": "06c1097efce0",
    "ID_qwen3.5:latest": "2e16a80fe3d7,44a38603922e",
    "CENSUS_PY_SHA256": "03941a4f9a8f49526eacd3ab3d116602da9a3667b2ec10c04c8f4e531d7c289a",
    "GT_SPLIT_SHA256": "8921a1549e458c8e648b8070ce84a8b97a1b9f0b055c99d6e932bcbc3ca5405b",
    "GT_MANIFEST_SHA256": "453ad8bd93782dcf4457715a37f98c20d3d234f4eded95ce988885f5a464e4cc",
    "NONCLOUD_TASK_IDS_SHA256": "e493f84f5f5ddba32311e6f9903783982834aaff41cbbf4fcddeca4a7ee92638",
    "RX_PACKETS_SHA256": "252a2d664b01b7c6bc7aeef1f2a2453aa57bcedef4d01cfa374dd9a854aa9ce2",
    "RX_RESULTS_P2B_SHA256": "f25e5c7d30d18ca8debdcb3262229fd04059514d2dea292155e4983fc07b57ff",
    "RX_GRADES_GRADER1_SHA256": "d1162590142798cc054df7696da3548a860a8a1537fcced7f18a17c812c8d79f",
    "RX_CORPUS_SHA256": "083ea397dc8842e47c3ceca514d60dd4b6fbda11c603dcef6280c451ed9e599f",
    "RX_REAL_CORPUS_SHA256": "0a7fd1f96c96e39362705b4ff3a1cf5537c43ade2c6684827102daa058736cf7",
    "RX_EDIT_FIXTURES_TREE_SHA256": (
        "7c191707ca5de5753b60a363fec6de637a563105c31aa656e75d5b252a48524f"
    ),
}
FILL_KEYS = (
    "REPO_SHA", "CLAUDE_SONNET_ID", "CLAUDE_HAIKU_ID", "CLAUDE_OPUS_ID",
    "PROXY_CALLS_COPY_SHA256", "BUILD_MANIFEST_SHA256", "POOL_QA_SHA256",
    "POOL_TECH_OP_SHA256", "POOL_CODE_SHA256", "CODE_RUNNER_SHA256",
    "POOL_COUNT_QA_RX", "POOL_COUNT_QA_GT", "POOL_COUNT_TECH_OP",
    "POOL_COUNT_CODE_NONCLOUD", "POOL_COUNT_CODE_RX_EDIT",
    "N_REALISED_QA", "N_REALISED_TECH_OP", "N_REALISED_CODE",
)
# AMEND2 (D-166): the only local arm, qa and code, is qwen3.8-uncensored:latest. The prereg's
# three local arms and their ID_ pins are void; CONSTANTS above stays the prereg's frozen values.
QWEN38 = "qwen3.8-uncensored:latest"
AMEND2_FIXED = {
    "LOCAL_MODELS": QWEN38,
    "CODE_LOCAL_MODELS": QWEN38,
    "SIZE_ORDER": QWEN38,
    f"ID_{QWEN38}": "b3f4c17c6811",
}
AMEND2_SHA_KEYS = {"REPO_SHA": r"[0-9a-f]{40}", "CODE_RUNNER_SHA256": r"[0-9a-f]{64}"}
# AMEND3 (D-202): no night window (the quiet gate before every local call replaces it); its two
# pins replace AMEND2's. AMEND2's other keys stay.
AMEND3_FIXED = {"NIGHT_WINDOW": "none"}
AMEND3_SHA_KEYS = AMEND2_SHA_KEYS
# AMEND4: the harness fix (AMEND3 is a plain merge; it must not drop AMEND2's ID_ pin) and a
# re-pin of REPO_SHA. It sets no other key.
AMEND4_SHA_KEYS = {"REPO_SHA": r"[0-9a-f]{40}"}


def amended(constants: dict, overrides: dict) -> dict:
    """Apply AMEND2: every prereg ID_ pin is dropped, then the AMEND2 block is applied."""
    out = {key: value for key, value in constants.items() if not key.startswith("ID_")}
    out.update(overrides)
    return out


# The pre-freeze view (status, select --dry-run, the code runner's --arm choices).
STATIC = amended(CONSTANTS, AMEND2_FIXED)
JUDGE_BRIEF_QA = (
    "an answer is *acceptable* if a competent senior engineer would accept it as the\n"
    "reply without asking again: it is correct, it addresses the actual ask, "
    "and it suggests nothing\n"
    "destructive. Length and style do not count."
)
JUDGE_BRIEF_TECH_OP = (
    "a next action is *acceptable* if it is a reasonable next step toward the\n"
    "request given the conversation. A different but reasonable tool or argument is acceptable. A\n"
    "\x60final\x60 is acceptable only if the request is already satisfied."
)
JUDGE_REPLY = 'JSON {"<letter>": "acceptable" | "unacceptable", ...} covering every letter.'


class Refuse(Exception):
    """Invalid protocol or evidence."""


class Park(Exception):
    """Resumable operational stop."""


class Terminal(Exception):
    """This set cannot be resumed."""


def parse_constants(text: str) -> dict[str, str]:
    return parse_block(text, "```text P20-CONSTANTS")


def parse_block(text: str, marker: str) -> dict[str, str]:
    lines = text.splitlines()
    try:
        start = lines.index(marker) + 1
        end = lines.index("\x60\x60\x60", start)
    except ValueError as exc:
        raise Refuse("refusing: missing constants block") from exc
    out = {}
    for line in lines[start:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or key in out:
            raise Refuse("refusing: malformed constants block")
        out[key] = value
    return out


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def utc(cfg: Cfg) -> str:
    return datetime.fromtimestamp(cfg.now(), timezone.utc).isoformat().replace("+00:00", "Z")


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic(path: Path, text: str) -> None:
    private_dir(path.parent)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic(path, json.dumps(value, sort_keys=True, allow_nan=False) + "\n")


def append(path: Path, text: str) -> None:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def append_row(path: Path, row: dict) -> None:
    append(path, json.dumps(row, sort_keys=True, allow_nan=False) + "\n")


def append_use(cfg: Cfg, kind: str, rest: str) -> None:
    append(cfg.run_dir / "use.log", f"{kind} {utc(cfg)} {rest}\n")


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise Refuse(f"refusing: invalid JSONL in {path.name}") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise Refuse(f"refusing: non-object in {path.name}")
    return rows


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Refuse(f"refusing: invalid {path.name}") from exc
    if not isinstance(value, dict):
        raise Refuse(f"refusing: non-object {path.name}")
    return value


def uses(cfg: Cfg) -> list[list[str]]:
    path = cfg.run_dir / "use.log"
    return [line.split() for line in path.read_text().splitlines()] if path.exists() else []


def require_unused(cfg: Cfg) -> None:
    if any(row and row[0] == "started" for row in uses(cfg)):
        raise Refuse("refusing: score started")


def check_set(cfg: Cfg, name: str) -> None:
    if any(len(row) >= 4 and row[0] == "terminal" and row[2] == name for row in uses(cfg)):
        raise Terminal(f"terminal: {name}")


def logged_hash(cfg: Cfg, kind: str, key: str, path: Path) -> None:
    matches = [x for row in uses(cfg) if row and row[0] == kind
               for x in row[2:] if x.startswith(key + "=")]
    if not matches or matches[-1] != f"{key}={sha256_file(path)}":
        raise Refuse(f"refusing: {kind} hash mismatch")


def frozen(cfg: Cfg) -> dict:
    try:
        checked = check_prereg.check(cfg.prereg)
        epoch = datetime.fromisoformat(checked["frozen_utc"].replace("Z", "+00:00"))
        if epoch.tzinfo is None or epoch.timestamp() >= cfg.now() or not checked["match"]:
            raise ValueError("freeze")
        text = cfg.prereg.read_text()
    except (OSError, ValueError, IndexError) as exc:
        raise Refuse("refusing: prereg is not frozen") from exc
    constants = parse_constants(text)
    for key in constants.keys() - (CONSTANTS.keys() | set(FILL_KEYS)):
        raise Refuse(f"refusing: unknown constant {key}")
    if constants.keys() != CONSTANTS.keys() | set(FILL_KEYS):
        raise Refuse("refusing: missing constant")
    if "FILL-AT-FREEZE" in constants.values():
        raise Refuse("refusing: prereg has freeze fill-ins")
    for key, value in CONSTANTS.items():
        if constants[key] != value:
            raise Refuse(f"refusing: constant {key} differs from the prereg")
    copy = cfg.run_dir / "PREREG.md"
    if copy.exists() and copy.read_bytes() != cfg.prereg.read_bytes():
        raise Refuse("refusing: PREREG.md differs")
    try:
        amendment = check_prereg.check(cfg.amend1)
        if not amendment["match"]:
            raise ValueError("freeze")
        amendment_text = cfg.amend1.read_text()
    except (OSError, ValueError, IndexError, OverflowError) as exc:
        raise Refuse("refusing: AMEND1 is not frozen") from exc
    try:
        amendment_epoch = datetime.fromisoformat(amendment["frozen_utc"].replace("Z", "+00:00"))
        if (amendment_epoch.tzinfo is None
                or not epoch.timestamp() < amendment_epoch.timestamp() < cfg.now()):
            raise ValueError("freeze order")
    except (ValueError, OverflowError) as exc:
        raise Refuse("refusing: AMEND1 frozen_utc") from exc
    try:
        overrides = parse_block(amendment_text, "```text P20-AMEND1-CONSTANTS")
        if (overrides.keys() != {"REPO_SHA", "CODE_RUNNER_SHA256"}
                or not re.fullmatch(r"[0-9a-f]{40}", overrides["REPO_SHA"])
                or not re.fullmatch(r"[0-9a-f]{64}", overrides["CODE_RUNNER_SHA256"])):
            raise ValueError("constants")
    except (Refuse, ValueError) as exc:
        raise Refuse("refusing: AMEND1 constants") from exc
    amendment_copy = cfg.run_dir / "PREREG-AMEND1.md"
    if amendment_copy.exists() and amendment_copy.read_bytes() != cfg.amend1.read_bytes():
        raise Refuse("refusing: PREREG-AMEND1.md differs")
    try:
        second = check_prereg.check(cfg.amend2)
        if not second["match"]:
            raise ValueError("freeze")
        second_text = cfg.amend2.read_text()
    except (OSError, ValueError, IndexError, OverflowError) as exc:
        raise Refuse("refusing: AMEND2 is not frozen") from exc
    try:
        second_epoch = datetime.fromisoformat(second["frozen_utc"].replace("Z", "+00:00"))
        if (second_epoch.tzinfo is None
                or not amendment_epoch.timestamp() < second_epoch.timestamp() < cfg.now()):
            raise ValueError("freeze order")
    except (ValueError, OverflowError) as exc:
        raise Refuse("refusing: AMEND2 frozen_utc") from exc
    try:
        second_overrides = parse_block(second_text, "```text P20-AMEND2-CONSTANTS")
        if (second_overrides.keys() != AMEND2_FIXED.keys() | AMEND2_SHA_KEYS.keys()
                or any(second_overrides[k] != v for k, v in AMEND2_FIXED.items())
                or not all(re.fullmatch(pattern, second_overrides[k])
                           for k, pattern in AMEND2_SHA_KEYS.items())):
            raise ValueError("constants")
    except (Refuse, ValueError) as exc:
        raise Refuse("refusing: AMEND2 constants") from exc
    second_copy = cfg.run_dir / "PREREG-AMEND2.md"
    if second_copy.exists() and second_copy.read_bytes() != cfg.amend2.read_bytes():
        raise Refuse("refusing: PREREG-AMEND2.md differs")
    constants.update(overrides)
    constants = amended(constants, second_overrides)
    out = {"sha256": checked["sha256"], "frozen_utc": checked["frozen_utc"],
           "amend1_sha256": amendment["sha256"],
           "amend1_frozen_utc": amendment["frozen_utc"],
           "amend2_sha256": second["sha256"], "amend2_frozen_utc": second["frozen_utc"]}
    if cfg.amend3.with_suffix(".sha256").exists():
        # AMEND3 (D-202) is in force as soon as its sidecar exists; a draft without one is not.
        try:
            third = check_prereg.check(cfg.amend3)
            if not third["match"]:
                raise ValueError("freeze")
            third_text = cfg.amend3.read_text()
        except (OSError, ValueError, IndexError, OverflowError) as exc:
            raise Refuse("refusing: AMEND3 is not frozen") from exc
        try:
            third_epoch = datetime.fromisoformat(third["frozen_utc"].replace("Z", "+00:00"))
            if (third_epoch.tzinfo is None
                    or not second_epoch.timestamp() < third_epoch.timestamp() < cfg.now()):
                raise ValueError("freeze order")
        except (ValueError, OverflowError) as exc:
            raise Refuse("refusing: AMEND3 frozen_utc") from exc
        try:
            third_overrides = parse_block(third_text, "```text P20-AMEND3-CONSTANTS")
            if (third_overrides.keys() != AMEND3_FIXED.keys() | AMEND3_SHA_KEYS.keys()
                    or any(third_overrides[k] != v for k, v in AMEND3_FIXED.items())
                    or not all(re.fullmatch(pattern, third_overrides[k])
                               for k, pattern in AMEND3_SHA_KEYS.items())):
                raise ValueError("constants")
        except (Refuse, ValueError) as exc:
            raise Refuse("refusing: AMEND3 constants") from exc
        third_copy = cfg.run_dir / "PREREG-AMEND3.md"
        if third_copy.exists() and third_copy.read_bytes() != cfg.amend3.read_bytes():
            raise Refuse("refusing: PREREG-AMEND3.md differs")
        # A plain merge: amended() would drop AMEND2's ID_ pin (AMEND4 fixes that bug).
        constants = {**constants, **third_overrides}
        out.update(amend3_sha256=third["sha256"], amend3_frozen_utc=third["frozen_utc"])
        if cfg.amend4.with_suffix(".sha256").exists():
            # AMEND4 is in force as soon as its sidecar exists; a draft without one is not.
            try:
                fourth = check_prereg.check(cfg.amend4)
                if not fourth["match"]:
                    raise ValueError("freeze")
                fourth_text = cfg.amend4.read_text()
            except (OSError, ValueError, IndexError, OverflowError) as exc:
                raise Refuse("refusing: AMEND4 is not frozen") from exc
            try:
                fourth_epoch = datetime.fromisoformat(fourth["frozen_utc"].replace("Z", "+00:00"))
                if (fourth_epoch.tzinfo is None
                        or not third_epoch.timestamp() < fourth_epoch.timestamp() < cfg.now()):
                    raise ValueError("freeze order")
            except (ValueError, OverflowError) as exc:
                raise Refuse("refusing: AMEND4 frozen_utc") from exc
            try:
                fourth_overrides = parse_block(fourth_text, "```text P20-AMEND4-CONSTANTS")
                if (fourth_overrides.keys() != AMEND4_SHA_KEYS.keys()
                        or not all(re.fullmatch(pattern, fourth_overrides[k])
                                   for k, pattern in AMEND4_SHA_KEYS.items())):
                    raise ValueError("constants")
            except (Refuse, ValueError) as exc:
                raise Refuse("refusing: AMEND4 constants") from exc
            fourth_copy = cfg.run_dir / "PREREG-AMEND4.md"
            if fourth_copy.exists() and fourth_copy.read_bytes() != cfg.amend4.read_bytes():
                raise Refuse("refusing: PREREG-AMEND4.md differs")
            constants = {**constants, **fourth_overrides}
            out.update(amend4_sha256=fourth["sha256"], amend4_frozen_utc=fourth["frozen_utc"])
    elif cfg.amend4.with_suffix(".sha256").exists():
        raise Refuse("refusing: AMEND4 requires a frozen AMEND3")
    out["constants"] = constants
    return out


def code_gate(repo: Path, repo_sha: str, module_file: str, run=subprocess.run) -> None:
    def git(*args: str):
        try:
            return run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refuse("refusing: code gate git error") from exc

    diff = git("diff", "--quiet", repo_sha, "--", "scripts", "src", "tests")
    if diff.returncode not in (0, 1):
        raise Refuse("refusing: code gate git error")
    if diff.returncode:
        raise Refuse("refusing: code differs from REPO_SHA")
    state = git("status", "--porcelain", "--", "scripts", "src", "tests")
    if state.returncode:
        raise Refuse("refusing: code gate git error")
    if state.stdout:
        raise Refuse("refusing: uncommitted code under scripts/src/tests")
    if not Path(module_file).resolve().is_relative_to(repo.resolve()):
        raise Refuse("refusing: llm_router imported from outside the run worktree")


def run_gate(cfg: Cfg, constants: dict) -> None:
    if cfg.gate is not None:
        cfg.gate(constants)
    else:
        code_gate(REPO, constants["REPO_SHA"], llm_router.__file__)


def flatten(messages: list[dict]) -> str:
    return "\n\n".join(f"### {m['role']}\n{m['content']}" for m in messages)


def default_pack(item: dict, name: str, window: int) -> list[dict]:
    if name == "qa":
        return to_messages(build_pack(item["request"], context=item.get("context") or "",
                                      door="mcp", project="", target_window=window))
    messages = to_messages(build_pack(
        item["request"], messages=item.get("messages") or [], door="proxy", project="",
        query=item.get("request"), target_window=window,
    ))
    tools = item.get("tools") or []
    if isinstance(tools, dict):
        tools = [{"name": key, "description": value} for key, value in tools.items()]
    protocol = "\n".join(f"- {t['name']}: {t.get('description', '')}" for t in tools)
    protocol += ('\nReply with exactly one JSON object {"tool": <name>, "input": {...}}'
                 ' or {"final": <text>}.')
    return messages + [{"role": "user", "content": protocol}]


@dataclass
class Cfg:
    run_dir: Path
    prereg: Path = PREREG
    amend1: Path = AMEND1
    amend2: Path = AMEND2
    amend3: Path = AMEND3
    amend4: Path = AMEND4
    lock_path: Path | None = None
    local_used: set = dc_field(default_factory=set)
    gate: Callable[[dict], None] | None = None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    ollama: Any = None
    claude: Any = None
    machine: Any = None
    quota: Callable[[], dict | None] = lambda: None
    pack_fn: Callable[[dict, str, int], list[dict]] = default_pack
    git_sha: Callable[[], str] = lambda: ""


class OllamaHTTP:
    def request(self, route: str, body: dict | None = None, timeout: int = 60) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = Request("http://localhost:11434" + route, data=data,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)

    def chat(self, model: str, messages: list[dict], *, timeout: int, options: dict,
             think: bool) -> dict:
        result = self.request("/api/chat", {
            "model": model, "messages": messages, "stream": False, "think": think,
            "options": options, "keep_alive": "10m",
        }, timeout)
        return {"text": result.get("message", {}).get("content", ""),
                "error": result.get("error")}

    def unload(self, model: str) -> None:
        self.request("/api/generate", {"model": model, "keep_alive": 0})

    def list_ids(self) -> dict[str, set[str]]:
        output = subprocess.run(["ollama", "list"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60, check=True)
        ids: dict[str, set[str]] = {}
        for line in output.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                ids.setdefault(fields[0], set()).add(fields[1])
        return ids

    def ps(self) -> list[str]:
        return [row["name"] for row in self.request("/api/ps").get("models", [])]


def claude_settings(run_dir: Path) -> Path:
    path = run_dir / "claude_settings.json"
    write_json(path, {"disableAllHooks": True})
    return path


def claude_argv(model_id: str, run_dir: Path, tools: str | None) -> list[str]:
    args = ["claude", "-p", "--safe-mode", "--no-session-persistence",
            "--settings", str(claude_settings(run_dir)),
            "--model", model_id, "--output-format", "json"]
    return args + (["--tools", tools] if tools is not None else [])


# F104 (incidents F86 2026-10-18, F86-2 2026-10-19): agent subprocesses ran with the owner's
# real HOME and twice deleted the live llm-router install. Agents now get a per-task scratch
# HOME and an allow-listed environment; nothing else from the caller's environment is passed.
AGENT_PASS_ENV = ("PATH", "LANG", "LC_ALL", "USER", "LOGNAME")  # USER: macOS Keychain account
CLAUDE_AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)  # subscription token; forwarded, never logged
CLAUDE_CREDENTIALS_ENV = "P20_CLAUDE_CREDENTIALS_FILE"  # optional file copied into scratch HOME
AGENT_ENV_ALLOWED = frozenset(
    AGENT_PASS_ENV + CLAUDE_AUTH_ENV + ("HOME", "LLM_ROUTER_SESSION_KIND")
)


# F110 (rv-642): PATH was still the owner's, so an agent could run `llm-router uninstall`.
# `launchctl` stays reachable (it lives in /bin); the threat is llm-router calling it, so
# removing every llm-router executable from PATH is the guard. Not a sandbox.
SYSTEM_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
AGENT_PATH_TOOLS = ("node", "npm", "npx", "git", "python", "python3", "uv")  # dirs kept
# The legacy-brand CLI is not named here: the identity gate forbids that string in runtime code.
DENIED_EXECUTABLES = ("llm-router", "llm_router")


def has_denied_executable(path_dirs) -> bool:
    joined = os.pathsep.join(map(str, path_dirs))
    return any(shutil.which(name, path=joined) for name in DENIED_EXECUTABLES)


def agent_path(path: str) -> str:
    """Allow-listed PATH: system dirs plus caller dirs that hold a needed tool (node, git,
    python, uv), minus any dir with an llm-router executable, ~/.local/bin and the uv
    tool dirs. A dir holding both node and llm-router is dropped whole (fails closed)."""
    home = real_home()
    banned = {home / ".local/bin", home / ".local/share/uv"}
    kept: list[str] = [d for d in SYSTEM_PATH_DIRS if Path(d).is_dir()]
    for entry in path.split(os.pathsep):
        if not entry or entry in kept:
            continue
        d = Path(entry)
        resolved = d.resolve()
        if any(resolved == b or b in resolved.parents for b in banned):
            continue
        if has_denied_executable([d]) or not any(
            shutil.which(t, path=str(d)) for t in AGENT_PATH_TOOLS
        ):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def real_home() -> Path:
    """The account's home from the user database, which an overridden $HOME cannot move."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def scratch_home(path: Path) -> Path:
    """A fresh private directory to serve as an agent's HOME."""
    if path.resolve() == real_home():
        raise Refuse("refusing: agent scratch HOME is the real HOME")
    if path.exists():
        shutil.rmtree(path)
    private_dir(path)
    return path


def assert_agent_env(env: dict[str, str], run_dir: Path | None = None) -> None:
    """Refuse to launch an agent whose environment could reach the owner's install."""
    home = env.get("HOME")
    if not home:
        raise Refuse("refusing: agent environment has no HOME")
    resolved = Path(home).resolve()
    caller = os.environ.get("HOME")
    if resolved == real_home() or (caller and resolved == Path(caller).resolve()):
        raise Refuse("refusing: agent HOME is the real HOME")
    if resolved.is_relative_to(real_home()) and not (
        run_dir is not None and resolved.is_relative_to(Path(run_dir).resolve())
    ):
        raise Refuse("refusing: agent HOME is inside the real HOME outside the run dir")
    if env.get("PATH") is not None and has_denied_executable(env["PATH"].split(os.pathsep)):
        raise Refuse("refusing: llm-router is reachable on the agent PATH")
    if set(env) - AGENT_ENV_ALLOWED:
        raise Refuse("refusing: unexpected key in agent environment")


def agent_env(home: Path, *, claude: bool = False, run_dir: Path | None = None) -> dict[str, str]:
    """Minimal agent environment: scratch HOME, PATH/LANG, and (Claude only) auth material.

    Auth is either CLAUDE_CODE_OAUTH_TOKEN passed through, or the file named by
    P20_CLAUDE_CREDENTIALS_FILE copied to <home>/.claude/.credentials.json (0600). Values are
    never printed or logged; failures name no path.
    """
    env = {k: os.environ[k] for k in AGENT_PASS_ENV if k in os.environ}
    if "PATH" in env:
        env["PATH"] = agent_path(env["PATH"])
    env["HOME"] = str(home)
    if claude:
        env["LLM_ROUTER_SESSION_KIND"] = "research"
        env.update({k: os.environ[k] for k in CLAUDE_AUTH_ENV if os.environ.get(k)})
        source = os.environ.get(CLAUDE_CREDENTIALS_ENV)
        if source:
            target = home / ".claude" / ".credentials.json"
            try:
                private_dir(target.parent)
                shutil.copyfile(source, target)
                target.chmod(0o600)
            except OSError as exc:
                raise Refuse("refusing: Claude credentials file unusable") from exc
    assert_agent_env(env, run_dir)
    return env


@contextlib.contextmanager
def scrubbed_environ(env: dict[str, str]):
    """Run code that builds a child env from os.environ (the Pi helper) with `env` instead."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def claude_env(home: Path, run_dir: Path | None = None) -> dict[str, str]:
    return agent_env(home, claude=True, run_dir=run_dir)


class ClaudeCLI:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    def __call__(self, model_id: str, prompt: str, *, timeout: int) -> dict:
        env = claude_env(scratch_home(self.run_dir / "claude_home"), self.run_dir)
        if "ANTHROPIC_BASE_URL" in env or "ANTHROPIC_API_KEY" in env:
            raise Refuse("refusing: Claude environment")
        cwd = self.run_dir / "claude_cwd"
        private_dir(cwd)
        result = subprocess.run(
            claude_argv(model_id, self.run_dir, tools=""), input=prompt, text=True,
            capture_output=True, timeout=timeout, cwd=cwd, env=env, check=False,
        )
        value = json.loads(result.stdout)
        usage = value.get("modelUsage", {})
        served = next(iter(usage)) if isinstance(usage, dict) and len(usage) == 1 else ""
        return {"text": value.get("result", ""), "cost_usd": value.get("total_cost_usd", 0),
                "error": "claude_error" if value.get("is_error") or result.returncode else None,
                "served_model": served}


class Machine:
    def free_mem_pct(self) -> float:
        result = subprocess.run(["memory_pressure", "-Q"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60, check=True)
        match = re.search(r"free percentage:\s*([\d.]+)%", result.stdout, re.I)
        if not match:
            raise Park("memory reading unavailable")
        return float(match[1])

    def load1(self) -> float:
        return os.getloadavg()[0]


def read_quota() -> dict | None:
    try:
        data = json.loads((Path.home() / ".llm-router/usage.json").read_text())
        values = [data[key] for key in ("session_pct", "weekly_pct", "updated_at")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) for v in values):
            return None
        return {"session_pct": values[0], "weekly_pct": values[1],
                "age_s": time.time() - values[2]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def foreign_rows(path: Path, allowed: list[str]) -> int:
    return sum(row.get("arm") not in allowed for row in jsonl(path))


def require_no_foreign(path: Path, allowed: list[str]) -> None:
    count = foreign_rows(path, allowed)
    if count:
        raise Refuse(f"refusing: {count} foreign-arm rows in {path.parent.name}/{path.name}")


def arms(c: dict, code: bool = False) -> list[str]:
    local = c["CODE_LOCAL_MODELS" if code else "LOCAL_MODELS"].split(",")
    return local + [c["REFERENCE"]] + ([] if code else c["REPORT_ARMS"].split(","))


def select_sets(pools: dict, constants: dict) -> dict:
    output = {}
    seen = set()
    required = {"id", "set", "stratum", "source", "session", "tier", "task_type"}
    for name in SETS:
        excluded: Counter = Counter()
        counts = Counter(row.get("stratum", "missing") for row in pools.get(name, []))
        valid = []
        extra = {"qa": {"request", "context"}, "tech_op": {"request", "messages", "tools"},
                 "code": {"task_ref"}}[name]
        for row in pools.get(name, []):
            ident = row.get("id")
            if ident is not None:
                if ident in seen:
                    raise Refuse("refusing: duplicate id")
                seen.add(ident)
            reason = None
            if not required | extra <= row.keys() or row.get("set") != name:
                reason = "missing_field"
            elif name != "code":
                if row["tier"] == "complex":
                    reason = "tier_complex"
                elif row["tier"] not in ("simple", "moderate"):
                    reason = "no_tier"
                elif name == "qa" and len(row["request"].split()) < 4:
                    reason = "too_short"
            if reason:
                excluded[reason] += 1
            else:
                valid.append(row)
        seed = constants["SELECT_SEED"]
        valid.sort(key=lambda row: (
            0 if name == "qa" and row["stratum"] == "rx" else 1,
            digest(f"{seed}:{row['id']}"),
        ))
        chosen = []
        sessions: Counter = Counter()
        target = int(constants[f"N_TARGET_{name.upper()}"]) if name != "code" else len(valid)
        cap = math.floor(Fraction(constants["SESSION_CAP_SHARE"]) * target)
        for row in valid:
            if len(chosen) == target:
                break
            if name != "code" and sessions[row["session"]] >= cap:
                excluded["session_cap"] += 1
                continue
            chosen.append(row["id"])
            sessions[row["session"]] += 1
        output[name] = {"ids": chosen, "n": len(chosen), "excluded": dict(excluded),
                        "stratum_counts": dict(counts)}
    return output


SOURCE_PINS = {
    "rx_packets": "RX_PACKETS_SHA256", "rx_results_p2b": "RX_RESULTS_P2B_SHA256",
    "rx_grades_grader1": "RX_GRADES_GRADER1_SHA256", "rx_corpus": "RX_CORPUS_SHA256",
    "rx_real_corpus": "RX_REAL_CORPUS_SHA256",
    "rx_edit_fixtures_tree": "RX_EDIT_FIXTURES_TREE_SHA256",
    "gt_split": "GT_SPLIT_SHA256", "gt_manifest": "GT_MANIFEST_SHA256",
    "noncloud_task_ids": "NONCLOUD_TASK_IDS_SHA256", "census_py": "CENSUS_PY_SHA256",
    "proxy_calls_copy": "PROXY_CALLS_COPY_SHA256",
}


def select(cfg: Cfg, c: dict, dry_run: bool = False) -> dict:
    pools = {name: jsonl(cfg.run_dir / f"pools/{name}.jsonl") for name in SETS}
    selected = select_sets(pools, c)
    manifest_path = cfg.run_dir / "pools/build_manifest.json"
    if dry_run:
        out = {"sets": {s: {k: v for k, v in r.items() if k != "ids"}
                        for s, r in selected.items()}}
        if manifest_path.exists():
            out["rx_membership"] = read_json(manifest_path).get("rx_membership", {})
        return out
    if (cfg.run_dir / "selection.json").exists():
        raise Refuse("refusing: selection already exists")
    if not manifest_path.exists() or sha256_file(manifest_path) != c["BUILD_MANIFEST_SHA256"]:
        raise Refuse("refusing: build manifest hash")
    manifest = read_json(manifest_path)
    sources = manifest.get("sources", manifest)
    for source, key in SOURCE_PINS.items():
        value = sources.get(source)
        if isinstance(value, dict):
            value = value.get("sha256")
        if value != c[key]:
            raise Refuse(f"refusing: manifest {source} hash")
    count_checks = {
        "POOL_COUNT_QA_RX": selected["qa"]["stratum_counts"].get("rx", 0),
        "POOL_COUNT_QA_GT": selected["qa"]["stratum_counts"].get("gt", 0),
        "POOL_COUNT_TECH_OP": len(pools["tech_op"]),
        "POOL_COUNT_CODE_NONCLOUD": selected["code"]["stratum_counts"].get("noncloud", 0),
        "POOL_COUNT_CODE_RX_EDIT": selected["code"]["stratum_counts"].get("rx_edit", 0),
    }
    for key, count in count_checks.items():
        if count != int(c[key]):
            raise Refuse(f"refusing: {key} differs")
    for name in SETS:
        pool_hash = sha256_file(cfg.run_dir / f"pools/{name}.jsonl")
        if pool_hash != c[f"POOL_{name.upper()}_SHA256"]:
            raise Refuse(f"refusing: pool {name} hash")
        if selected[name]["n"] != int(c[f"N_REALISED_{name.upper()}"]):
            raise Refuse(f"refusing: N_REALISED_{name.upper()} differs")
        selected[name]["pool_sha256"] = pool_hash
    for name in SETS:
        if selected[name]["n"] < int(c["N_MIN"]):
            append_use(cfg, "terminal", f"{name} n_min")
        by_id = {row["id"]: row for row in pools[name]}
        atomic(cfg.run_dir / f"items/{name}.jsonl",
               "".join(json.dumps(by_id[i]) + "\n" for i in selected[name]["ids"]))
    write_json(cfg.run_dir / "selection.json",
               {"label": c["LABEL"], "seed": c["SELECT_SEED"], "sets": selected})
    append_use(cfg, "selected",
               f"selection_sha256={sha256_file(cfg.run_dir / 'selection.json')}")
    return {"sets": {s: {k: v for k, v in r.items() if k != "ids"}
                     for s, r in selected.items()}}


def selection(cfg: Cfg) -> dict:
    path = cfg.run_dir / "selection.json"
    logged_hash(cfg, "selected", "selection_sha256", path)
    return read_json(path)["sets"]


def items_for(cfg: Cfg, name: str, selected: dict) -> list[dict]:
    rows = jsonl(cfg.run_dir / f"items/{name}.jsonl")
    by_id = {row["id"]: row for row in rows}
    ids = selected[name]["ids"]
    if len(by_id) != len(rows) or set(by_id) != set(ids):
        raise Refuse("refusing: selected item ids differ")
    return [by_id[i] for i in ids]


def finals(cfg: Cfg, name: str) -> dict:
    return {(r["item_id"], r["arm"]): r
            for r in jsonl(cfg.run_dir / f"answers/{name}.jsonl") if r.get("final")}


def quota_guard(cfg: Cfg, c: dict) -> None:
    quota = cfg.quota()
    try:
        bad = quota is None or any(
            not math.isfinite(float(quota[key])) for key in ("age_s", "session_pct", "weekly_pct")
        )
        bad = bad or quota["age_s"] > int(c["QUOTA_MAX_AGE_S"])
        bad = bad or quota["session_pct"] >= int(c["QUOTA_SESSION_PAUSE_PCT"])
        bad = bad or quota["weekly_pct"] >= int(c["QUOTA_WEEKLY_PAUSE_PCT"])
    except (KeyError, TypeError, ValueError):
        bad = True
    if bad:
        raise Park("quota pause")
    costs = [Fraction(str(r.get("cost_usd", 0))) for r in jsonl(cfg.run_dir / "ledger.jsonl")]
    positive = [cost for cost in costs if cost > 0]
    estimate = Fraction(5, 4) * max(positive) if positive else Fraction(c["FIRST_CALL_EST_USD"])
    if sum(costs) + estimate > Fraction(c["SPEND_CAP_USD"]):
        raise Park("spend cap")


def call_model(cfg: Cfg, c: dict, name: str, arm: str, ident: str, attempt: int,
               messages: list[dict], stage: str = "run") -> dict:
    local = arm in c["LOCAL_MODELS"].split(",")
    model_id = arm if local else c[f"CLAUDE_{arm.upper()}_ID"]
    if not local:
        quota_guard(cfg, c)
    start = cfg.now()
    try:
        if local:
            with local_call(cfg, c, arm, {"stage": stage, "set": name, "item_id": ident,
                                          "attempt": attempt}):
                result = cfg.ollama.chat(
                    arm, messages, timeout=int(c["LOCAL_TIMEOUT_S"]),
                    options={"num_ctx": local_models.num_ctx(arm), "temperature": 0},
                    think=False,  # AMEND2: as production (providers._ollama_think_enabled off)
                )
        else:
            result = cfg.claude(model_id, flatten(messages), timeout=int(c["REF_TIMEOUT_S"]))
    except (Refuse, Park, Terminal):
        raise
    except Exception:
        result = {"text": "", "error": "exception"}
    error = result.get("error")
    served = result.get("served_model", "")
    if not local and served != model_id:
        error = "served_model_mismatch"
    text = result.get("text", "")
    if not isinstance(text, str):
        text, error = "", "empty"
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    error = error or (None if text else "empty")
    cost = result.get("cost_usd", 0)
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
        raise Refuse("refusing: invalid call cost")
    if cost < 0:
        raise Refuse("refusing: negative call cost")
    wall = cfg.now() - start
    append_row(cfg.run_dir / "ledger.jsonl", {
        "ts": utc(cfg), "stage": stage, "set": name, "arm": arm, "item_id": ident,
        "attempt": attempt, "ok": error is None, "cost_usd": cost,
        "wall_s": wall, "served_model": served,
    })
    cap = int(c["ANSWER_CAP_CHARS"]) if stage == "run" else len(text)
    return {"item_id": ident, "arm": arm, "attempt": attempt, "ok": error is None,
            "text": text[:cap] if error is None else "", "truncated": len(text) > cap,
            "error": error, "wall_s": wall, "ts": utc(cfg)}


def quiet(cfg: Cfg, c: dict, arm: str) -> dict:
    """The quiet gate (AMEND3: any time of day). Returns the readings it passed on."""
    free_all: list[float] = []
    load_all: list[float] = []
    for reading in range(int(c["QUIET_READINGS"])):
        if reading:
            cfg.sleep(float(c["QUIET_INTERVAL_S"]))
        free, load = cfg.machine.free_mem_pct(), cfg.machine.load1()
        if (not math.isfinite(free) or not math.isfinite(load)
                or free < float(c["QUIET_FREE_MEM_MIN_PCT"])
                or load >= float(c["QUIET_LOAD1_MAX"])):
            raise Park("machine not quiet")
        free_all.append(free)
        load_all.append(load)
    resident = cfg.ollama.ps()
    if any(model != arm for model in resident):
        raise Park("foreign model resident")
    return {"free_mem_pct": free_all, "load1": load_all, "resident": resident}


@contextlib.contextmanager
def ollama_lock(cfg: Cfg):
    """Exclusive flock on the shared Ollama lock file; Park if another process holds it."""
    path = Path(cfg.lock_path or OLLAMA_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Park("ollama lock held") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def local_call(cfg: Cfg, c: dict, arm: str, tag: dict):
    """AMEND3 (D-202): hold the exclusive Ollama lock and pass the quiet gate for ONE local
    call. Held lock or a failed gate parks (exit 3) before the call and before any answer row;
    the lock is released on every exit path. Passed readings go to quiet.jsonl."""
    with ollama_lock(cfg):
        readings = quiet(cfg, c, arm)
        append_row(cfg.run_dir / "quiet.jsonl", {"ts": utc(cfg), "arm": arm, **tag, **readings})
        cfg.local_used.add(arm)
        yield readings


def unload_local(cfg: Cfg, arm: str) -> None:
    """Unload the arm model only if this run called it, and never while another process holds
    the lock (it may be using the same model)."""
    if arm not in cfg.local_used:
        return
    try:
        with ollama_lock(cfg):
            cfg.ollama.unload(arm)
        cfg.local_used.discard(arm)
    except Park:
        pass


def run(cfg: Cfg, c: dict, name: str, chosen_arms: list[str] | None = None) -> dict:
    require_unused(cfg)
    require_no_foreign(cfg.run_dir / f"answers/{name}.jsonl", arms(c))
    selected = selection(cfg)
    check_set(cfg, name)
    if (cfg.run_dir / f"judgements/{name}.jsonl").exists():
        raise Refuse("refusing: judging started")
    chosen_arms = chosen_arms or arms(c)
    if any(arm not in arms(c) for arm in chosen_arms):
        raise Refuse("refusing: unknown arm")
    items = items_for(cfg, name, selected)
    done = finals(cfg, name)
    count = 0
    for arm in chosen_arms:
        local = arm in c["LOCAL_MODELS"].split(",")
        if local and cfg.ollama.list_ids().get(arm) != set(c[f"ID_{arm}"].split(",")):
            raise Refuse(f"refusing: {arm} identity differs (not a result)")
        pending = [item for item in items if (item["id"], arm) not in done]
        if not pending:
            continue
        try:
            if local:
                with local_call(cfg, c, arm, {"stage": "warmup", "set": name,
                                              "item_id": None, "attempt": 0}):
                    cfg.ollama.chat(arm, [{"role": "user", "content": "Reply OK."}],
                                    timeout=int(c["LOCAL_TIMEOUT_S"]),
                                    options={"num_ctx": local_models.num_ctx(arm),
                                             "temperature": 0},
                                    think=False)
            for item in pending:
                window = local_models.num_ctx(arm) if local else int(c["REF_WINDOW"])
                messages = cfg.pack_fn(item, name, window)
                for attempt in range(1, int(c["RETRIES"]) + 2):
                    row = call_model(cfg, c, name, arm, item["id"], attempt, messages)
                    row["final"] = row["ok"] or attempt == int(c["RETRIES"]) + 1
                    append_row(cfg.run_dir / f"answers/{name}.jsonl", row)
                    count += 1
                    if row["final"]:
                        break
        finally:
            if local:
                unload_local(cfg, arm)
    return {"attempts": count, "finals": len(finals(cfg, name))}


def first_object(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("no object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("non-object")
    return value


def plant_candidates(items: list[dict], answers: dict, c: dict, name: str) -> tuple[dict, dict]:
    order = sorted((i["id"] for i in items), key=lambda i: digest(f"{c['SELECT_SEED']}:plant:{i}"))
    plants: dict[str, dict] = {}
    swapped = int(c["PLANTS_SWAPPED"])
    skipped = 0
    for j, ident in enumerate(order[:swapped]):
        source = order[(j + 1) % swapped] if len(order) >= swapped else None
        answer = answers.get((source, c["REFERENCE"]), {})
        if answer.get("ok"):
            plants.setdefault(ident, {})["__swap__"] = answer["text"]
        else:
            skipped += 1
    placed = 0
    for ident in order[swapped:]:
        if placed == int(c["PLANTS_DEGRADED"]):
            break
        answer = answers.get((ident, c["REFERENCE"]), {})
        if name == "tech_op":
            text = '{"final": "Done."}'
        elif answer.get("ok") and len(answer["text"]) >= int(c["DEGRADED_MIN_CHARS"]):
            end = math.floor(len(answer["text"]) * Fraction(c["DEGRADED_KEEP_SHARE"]))
            text = answer["text"][:end]
        else:
            continue
        plants.setdefault(ident, {})["__degraded__"] = text
        placed += 1
    return plants, {"swapped_skipped": skipped}


def judge_call(cfg: Cfg, c: dict, name: str, item: dict, candidates: dict,
               second: bool = False) -> tuple[dict, dict, bool, int]:
    seed = c["SELECT_SEED"] + (":perm2" if second else "")
    names = sorted(candidates, key=lambda arm: digest(f"{seed}:{item['id']}:{arm}"))
    letters = {chr(65 + i): arm for i, arm in enumerate(names)}
    if not letters:
        return letters, {}, False, 0
    brief = JUDGE_BRIEF_QA if name == "qa" else JUDGE_BRIEF_TECH_OP
    context = flatten(cfg.pack_fn(item, name, int(c["REF_WINDOW"])))
    prompt = (brief + "\n" + JUDGE_REPLY + "\nContext:\n"
              + context[-int(c["JUDGE_CONTEXT_CAP_CHARS"]):]
              + "\nRequest:\n" + item["request"] + "\n")
    prompt += "\n\n".join(f"Answer {letter}:\n{candidates[arm]}" for letter, arm in letters.items())
    for attempt in range(1, int(c["RETRIES"]) + 2):
        row = call_model(cfg, c, name, c["JUDGE"], item["id"], attempt,
                         [{"role": "user", "content": prompt}], stage="judge")
        try:
            values = first_object(row["text"]) if row["ok"] else {}
            valid = values.keys() == letters.keys() and all(
                v in ("acceptable", "unacceptable") for v in values.values()
            )
        except ValueError:
            valid = False
        if valid:
            verdicts = {letters[k]: v == "acceptable" for k, v in values.items()}
            return letters, verdicts, False, attempt
    return letters, {}, True, int(c["RETRIES"]) + 1


def judge(cfg: Cfg, c: dict, name: str) -> dict:
    require_unused(cfg)
    require_no_foreign(cfg.run_dir / f"answers/{name}.jsonl", arms(c))
    check_set(cfg, name)
    selected = selection(cfg)
    items = items_for(cfg, name, selected)
    answers = finals(cfg, name)
    if any((item["id"], arm) not in answers for item in items for arm in arms(c)):
        raise Refuse("refusing: run incomplete")
    done = {row["item_id"] for row in jsonl(cfg.run_dir / f"judgements/{name}.jsonl")}
    plants, stats = plant_candidates(items, answers, c, name)
    order = sorted((i["id"] for i in items),
                   key=lambda ident: digest(f"{c['SELECT_SEED']}:rejudge:{ident}"))
    sample = set(order[:math.ceil(Fraction(c["REJUDGE_SHARE"]) * len(items))])
    for item in items:
        ident = item["id"]
        if ident in done:
            continue
        candidates = {arm: answers[ident, arm]["text"] for arm in arms(c)
                      if answers[ident, arm]["ok"]}
        letters, verdicts, error, attempts = judge_call(
            cfg, c, name, item, candidates | plants.get(ident, {}),
        )
        rejudge, rejudge_error = None, False
        if ident in sample:
            _, rejudge, rejudge_error, extra = judge_call(cfg, c, name, item, candidates, True)
            attempts += extra
        append_row(cfg.run_dir / f"judgements/{name}.jsonl", {
            "item_id": ident, "letters": letters,
            "verdicts": {arm: verdicts[arm] for arm in candidates if arm in verdicts},
            "rejudge_verdicts": rejudge, "rejudge_error": rejudge_error,
            "judge_error": error, "attempts": attempts,
            "plants": {arm: value for arm, value in verdicts.items() if arm.startswith("__")},
            "ts": utc(cfg),
        })
    return {"judged": len(items), **stats}


def epoch(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def ingest_code(cfg: Cfg, c: dict, freeze: dict, results: Path, runner: Path) -> dict:
    require_unused(cfg)
    selected = selection(cfg)
    check_set(cfg, "code")
    if sha256_file(runner) != c["CODE_RUNNER_SHA256"]:
        raise Refuse("refusing: runner sha256")
    require_no_foreign(results, arms(c, True))
    rows = jsonl(results)
    expected = {(ident, arm) for ident in selected["code"]["ids"] for arm in arms(c, True)}
    seen = set()
    for row in rows:
        if row.get("task_id") not in selected["code"]["ids"]:
            raise Refuse("refusing: task_id")
        if row.get("arm") not in arms(c, True):
            raise Refuse("refusing: arm")
        if "hidden_pass" not in row or not any(
            row["hidden_pass"] is v for v in (True, False, None)
        ):
            raise Refuse("refusing: hidden_pass")
        try:
            ts = epoch(row["ts"])
            if not math.isfinite(ts) or ts <= epoch(freeze["frozen_utc"]):
                raise ValueError("ts")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise Refuse("refusing: ts") from exc
        pair = row["task_id"], row["arm"]
        if pair in seen:
            raise Refuse("refusing: duplicate pair")
        seen.add(pair)
    if seen != expected:
        raise Refuse("refusing: missing pair")
    atomic(cfg.run_dir / "code_results.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    append_use(cfg, "ingested", f"code_sha256={sha256_file(cfg.run_dir / 'code_results.jsonl')}")
    return {"rows": len(rows)}


def combined_hash(cfg: Cfg, folder: str) -> str:
    paths = [cfg.run_dir / folder / f"{name}.jsonl" for name in SETS]
    present = [path for path in paths if path.exists()]
    if not present:
        return "none"
    return hashlib.sha256(b"".join(p.read_bytes() for p in present)).hexdigest()


def optional_hash(path: Path) -> str:
    return sha256_file(path) if path.exists() else "none"


def metrics(pairs: list[tuple[bool, bool]], reason: str | None, c: dict) -> dict:
    n = len(pairs)
    if n < int(c["N_MIN"]):
        reason = reason or "n_min"
    if reason:
        pairs, n = [], 0
    k = sum(a for a, _ in pairs)
    kr = sum(b for _, b in pairs)
    ev = eligibility.evidence_from_counts(k, n, kr, n)
    g = eligibility.grade(ev)
    threshold = None
    if n:
        for count in range(n, math.ceil(n / 2) - 1, -1):
            equal = eligibility.evidence_from_counts(count, n, count, n)
            if eligibility.grade(equal) != "eligible":
                break
            threshold = count
    powered = threshold is not None and kr >= threshold
    verdict = ("not_informative" if reason else "eligible" if g == "eligible"
               else "failed" if powered else "inconclusive")
    b = sum(a and not r for a, r in pairs)
    cc = sum(r and not a for a, r in pairs)
    discordant = b + cc
    p = min(Fraction(1), 2 * Fraction(sum(math.comb(discordant, i)
                                        for i in range(min(b, cc) + 1)), 2 ** discordant))
    lo, hi = eligibility.newcombe_diff_ci(k, n, kr, n) if n else (None, None)
    return {
        "grade": g, "verdict": verdict, "eligible": verdict == "eligible",
        "rate": k / n if n else None, "ci": eligibility._wilson(k, n, 1.959964) if n else None,
        "n": n, "k": k, "gap_pts": ev.gap_pts, "lb_pts": ev.lb_pts,
        "ub_pts": hi * 100 if hi is not None else None,
        "ref": {"model": c["REFERENCE"], "k": kr, "n": n, "rate": kr / n if n else None,
                "ci": eligibility._wilson(kr, n, 1.959964) if n else None},
        "powered": powered, "equal_threshold_k": threshold, "reason": reason,
        "b": b, "c": cc, "mcnemar_p": float(p) if discordant else 1.0,
        "d": discordant / n if n else None,
        "implied_n": math.ceil(Fraction("1.96") ** 2 * Fraction(discordant, n)
                              / Fraction("0.03") ** 2) if n else None,
        "newcombe_ci": [lo, hi], "basis": "pooled_simple_moderate",
        "source": "p2.0-requal", "population": "replayed held-out (offline)",
    }


def action_key(text: str) -> tuple:
    value = first_object(text)
    if "final" in value:
        return ("final",)
    name, args = value["tool"], value["input"]
    if not isinstance(name, str) or not isinstance(args, dict):
        raise ValueError("tool protocol")
    for key in ("file_path", "path", "pattern", "command"):
        if key in args:
            target = args[key]
            if not isinstance(target, str):
                raise ValueError("target")
            if key in ("file_path", "path"):
                target = os.path.normpath(target)
            elif key == "pattern":
                target = target.strip()
            else:
                target = " ".join(target.split())
            return name, target
    raise ValueError("missing target")


def agreement(a: str, b: str) -> bool:
    try:
        return action_key(a) == action_key(b)
    except (ValueError, KeyError, TypeError):
        return False


def descriptive(items: list[dict], outcomes: dict) -> dict:
    out: dict[str, dict] = {"stratum": {}, "tier": {}}
    for field in out:
        for item in items:
            bucket = out[field].setdefault(item[field], {"k": 0, "n": 0})
            bucket["k"] += int(outcomes[item["id"]])
            bucket["n"] += 1
    return out


def score(cfg: Cfg, c: dict, freeze: dict, measurement: Path | None = None) -> dict:
    require_unused(cfg)
    for name in ("qa", "tech_op"):
        require_no_foreign(cfg.run_dir / f"answers/{name}.jsonl", arms(c))
    require_no_foreign(cfg.run_dir / "code_results.jsonl", arms(c, True))
    hashes = {"prereg_sha256": freeze["sha256"], "amend1_sha256": freeze["amend1_sha256"],
              "amend2_sha256": freeze["amend2_sha256"],
              **({"amend3_sha256": freeze["amend3_sha256"]} if "amend3_sha256" in freeze else {}),
              **({"amend4_sha256": freeze["amend4_sha256"]} if "amend4_sha256" in freeze else {}),
              "git_sha": cfg.git_sha(),
              "selection_sha256": optional_hash(cfg.run_dir / "selection.json"),
              "answers_sha256": combined_hash(cfg, "answers"),
              "judgements_sha256": combined_hash(cfg, "judgements"),
              "code_sha256": optional_hash(cfg.run_dir / "code_results.jsonl")}
    append_use(cfg, "started", " ".join(f"{k}={v}" for k, v in hashes.items()))
    selected = selection(cfg)
    result: dict = {**hashes, "scored_utc": utc(cfg), "sets": {}, "d21": {}}
    ni_sets = {}
    qa_types: Counter = Counter()
    code_rows = jsonl(cfg.run_dir / "code_results.jsonl")
    code = {(r["task_id"], r["arm"]): r["hidden_pass"] for r in code_rows}
    for name in SETS:
        items = items_for(cfg, name, selected)
        ids = selected[name]["ids"]
        n_selected = len(ids)
        reason = None
        try:
            check_set(cfg, name)
        except Terminal:
            reason = "terminal"
        if n_selected < int(c["N_MIN"]):
            reason = reason or "n_min"
        reports: dict = {}
        stats: dict = {}
        paired = []
        answers, judged = {}, {}
        if name != "code":
            answers = finals(cfg, name)
            rows = jsonl(cfg.run_dir / f"judgements/{name}.jsonl")
            judged = {r["item_id"]: r for r in rows}
            if len(judged) != len(rows) or set(judged) != set(ids):
                reason = reason or "incomplete"
            calibration = {}
            for kind in ("__swap__", "__degraded__"):
                values = [r["plants"][kind] for r in rows if kind in r.get("plants", {})]
                m, rejected = len(values), sum(value is False for value in values)
                calibration[kind] = {"placed": m, "rejected": rejected}
                if (m < int(c["PLANTS_MIN_PLACED"])
                        or rejected < math.ceil(Fraction(c["PLANTS_PASS_SHARE"]) * m)):
                    reason = reason or "calibration"
            paired = [item for item in items
                      if answers.get((item["id"], c["REFERENCE"]), {}).get("ok")
                      and item["id"] in judged and not judged[item["id"]]["judge_error"]]
            missing = n_selected - len(paired)
            if n_selected and Fraction(missing, n_selected) > Fraction(c["MAX_REF_MISSING_SHARE"]):
                reason = reason or "ref_missing"
            if len(paired) < int(c["N_MIN"]):
                reason = reason or "n_min"
            ref_k = sum(judged[i["id"]]["verdicts"].get(c["REFERENCE"]) is True
                        for i in paired)
            ref_n = len(paired)
            stats.update(calibration=calibration, ref_missing=missing, reference={
                "k": ref_k, "n": ref_n, "rate": ref_k / ref_n if ref_n else None,
                "ci": eligibility._wilson(ref_k, ref_n, 1.959964) if ref_n else None,
                "truncated": sum(answers.get((i, c["REFERENCE"]), {}).get("truncated", False)
                                 for i in ids),
            })
            if name == "qa" and not reason:
                qa_types.update(item["task_type"] for item in paired)
            rk = rn = 0
            for row in rows:
                second = row.get("rejudge_verdicts")
                if second is not None and not row.get("rejudge_error"):
                    for arm, value in second.items():
                        if arm in row["verdicts"]:
                            rn += 1
                            rk += value == row["verdicts"][arm]
            stats["rejudge"] = {"k": rk, "n": rn}
            stats["judge_order_sensitive"] = bool(
                rn and Fraction(rk, rn) < Fraction(c["REJUDGE_FLAG_BELOW"])
            )
        for arm in arms(c, name == "code"):
            if arm == c["REFERENCE"]:
                continue
            lane_reason = reason
            if name == "code":
                lane_items = [item for item in items
                              if code.get((item["id"], arm)) is not None
                              and code.get((item["id"], c["REFERENCE"])) is not None]
                removed = n_selected - len(lane_items)
                if (n_selected
                        and Fraction(removed, n_selected) > Fraction(c["MAX_REF_MISSING_SHARE"])):
                    lane_reason = lane_reason or "null_share"
                outcomes = {i["id"]: code[i["id"], arm] is True for i in lane_items}
                refs = {i["id"]: code[i["id"], c["REFERENCE"]] is True for i in lane_items}
            else:
                lane_items = paired
                outcomes = {
                    i["id"]: bool(answers.get((i["id"], arm), {}).get("ok")
                                  and judged[i["id"]]["verdicts"].get(arm) is True)
                    for i in paired
                }
                refs = {i["id"]: judged[i["id"]]["verdicts"].get(c["REFERENCE"]) is True
                        for i in paired}
                removed = n_selected - len(paired)
            lane = metrics([(outcomes[i["id"]], refs[i["id"]]) for i in lane_items], lane_reason, c)
            lane.update(prereg_sha256=freeze["sha256"], scored_utc=result["scored_utc"])
            lane["removed"] = removed
            lane["descriptive"] = descriptive(lane_items, outcomes)
            lane["report_only"] = arm in c["REPORT_ARMS"].split(",")
            if name != "code":
                lane["truncated"] = sum(
                    answers.get((i, arm), {}).get("truncated", False) for i in ids
                )
                if name == "qa" and lane["n"]:
                    bk, bn = map(int, c["QA_BASELINE"].split("/"))
                    lane["qa_baseline_ci"] = eligibility.newcombe_diff_ci(
                        lane["k"], lane["n"], bk, bn,
                    )
                if name == "tech_op":
                    k_agree = sum(
                        bool(answers.get((i["id"], arm), {}).get("ok"))
                        and agreement(answers[i["id"], arm]["text"],
                                      answers[i["id"], c["REFERENCE"]]["text"])
                        for i in paired
                    )
                    lane["agreement"] = {"k": k_agree, "n": len(paired)}
            reports[arm] = lane
            if arm in c["LOCAL_MODELS"].split(","):
                record_competence(
                    arm, name, {tier: lane for tier in c["TIERS_WRITTEN"].split(",")},
                    path=cfg.run_dir / "local_eligibility.json",
                )
        if reason:
            ni_sets[name] = reason
            append_use(cfg, "terminal", f"{name} {reason}")
        elif reports and all(r["verdict"] == "not_informative" for r in reports.values()):
            ni_sets[name] = "null_share" if name == "code" else "n_min"
            append_use(cfg, "terminal", f"{name} {ni_sets[name]}")
        result["sets"][name] = {"lanes": reports, **stats}
    local = c["LOCAL_MODELS"].split(",")

    def eligible(name: str) -> bool:
        return any(r["verdict"] == "eligible" for a, r in result["sets"][name]["lanes"].items()
                   if a in local)

    def decisive(name: str) -> bool:
        lanes = [r for a, r in result["sets"][name]["lanes"].items()
                 if a in local and r["verdict"] != "not_informative"]
        return name not in ni_sets and bool(lanes) and all(r["verdict"] == "failed" for r in lanes)

    result.update(
        cancel_p21=decisive("tech_op"), cancel_p210=decisive("qa"),
        no_decision=sorted(s for s in ("qa", "tech_op") if not eligible(s) and not decisive(s)),
        p21_allowed=eligible("tech_op"),
        kill_all=decisive("tech_op") and not any(eligible(s) for s in SETS),
        not_informative=ni_sets,
    )
    all_types = {i["task_type"] for i in items_for(cfg, "qa", selected)}
    for arm in local:
        verdict = result["sets"]["qa"]["lanes"][arm]["verdict"]
        result["d21"][arm] = {
            "qa_verdict": verdict,
            "task_types": {t: {
                "n": qa_types[t],
                "action": ("re-enable local for Q&A at tiers simple and moderate"
                           if verdict == "eligible" and qa_types[t] >= int(c["QA_TYPE_MIN_ITEMS"])
                           else "stays stripped"),
            } for t in sorted(all_types)},
        }
    size = c["SIZE_ORDER"].split(",")
    candidates = [(arm, lane) for arm, lane in result["sets"]["tech_op"]["lanes"].items()
                  if arm in local and lane["eligible"]]
    candidates.sort(key=lambda pair: (-Fraction(pair[1]["k"], pair[1]["n"]), size.index(pair[0])))
    result["e_tech"] = ({"model": candidates[0][0], "k": candidates[0][1]["k"],
                         "n": candidates[0][1]["n"]} if candidates else None)
    write_json(cfg.run_dir / "result.json", result)
    append_use(cfg, "scored", f"result_sha256={sha256_file(cfg.run_dir / 'result.json')}")
    if measurement:
        write_json(measurement, {**result, "selected_ids": {s: selected[s]["ids"] for s in SETS}})
    return {
        "sets": {s: {a: {k: r[k] for k in ("k", "n", "grade", "verdict")}
                     for a, r in result["sets"][s]["lanes"].items()} for s in SETS},
        **{k: result[k] for k in ("cancel_p21", "cancel_p210", "kill_all",
                                 "no_decision", "not_informative")},
    }


def scored(cfg: Cfg) -> dict:
    logged_hash(cfg, "scored", "result_sha256", cfg.run_dir / "result.json")
    return read_json(cfg.run_dir / "result.json")


def install(cfg: Cfg, confirm: bool) -> dict:
    if not confirm:
        raise Refuse("refusing: --confirm required")
    scored(cfg)
    data = read_json(cfg.run_dir / "local_eligibility.json")
    count = 0
    for model, tasks in data.items():
        for name, lanes in tasks.items():
            if name in SETS:
                count += record_competence(model, name, lanes)
    return {"installed": count}


def counts(value: str, allow_zero: bool = False) -> tuple[int, int]:
    try:
        k, n = map(int, value.split("/"))
        if k < 0 or n < 0 or k > n or (not n and not allow_zero):
            raise ValueError("counts")
        return k, n
    except ValueError as exc:
        raise Refuse("refusing: invalid counts") from exc


def tp2(cfg: Cfg, c: dict, args: argparse.Namespace) -> dict:
    result = scored(cfg)
    sk, sn = counts(args.s_tech)
    candidate = result.get("e_tech")
    if args.f_fit is None and candidate:
        raise Refuse("refusing: --f-fit required when e_tech > 0")
    fk, fn = counts(args.f_fit) if args.f_fit is not None else (0, 0)
    ck, cn = counts(args.s_cheap, True)
    if args.s_tech_sessions < 0:
        raise Refuse("refusing: session count")
    try:
        largest = Fraction(args.s_tech_largest)
        if not 0 <= largest <= 1:
            raise ValueError("largest share")
    except (ValueError, ZeroDivisionError) as exc:
        raise Refuse("refusing: largest share") from exc
    informative = (sn >= int(c["S_TECH_MIN_N"])
                   and args.s_tech_sessions >= int(c["S_TECH_MIN_SESSIONS"])
                   and largest <= Fraction(c["S_TECH_MAX_SESSION_SHARE"]))
    flags = []
    if not informative:
        flags.append("s_tech_not_informative")
    if not cn:
        flags.append("s_cheap_n0")
    e = Fraction(candidate["k"], candidate["n"]) if candidate else Fraction(0)
    if not candidate:
        flags.append("no_eligible_tech_op")
    s = Fraction(sk, sn) if informative else Fraction(c["S_TECH_FALLBACK"])
    if args.f_fit is None:
        flags.append("f_fit_not_required")  # e_tech = 0: the tech term is 0 (prereg §11)
    f = Fraction(fk, fn) if fn else Fraction(0)
    cheap = Fraction(ck, cn) if cn else Fraction(0)
    factor = Fraction(c["SAFETY_FACTOR"])
    ceiling = s * f * e + cheap
    ceiling_alt = s * f * e + (1 - s) * cheap
    total = factor * ceiling
    if total < Fraction(c["TP2_REPORT_BELOW"]):
        flags.append("report_to_owner")
    if ceiling < Fraction(c["CEILING_NOTE_BELOW"]):
        flags.append("ceiling_below_10pct")
    values = {"T_P2": total, "ceiling": ceiling, "T_P2_alt": factor * ceiling_alt,
              "ceiling_alt": ceiling_alt, "e_tech": e, "gating_s_tech": s}
    if not informative:
        values["tp2_high"] = factor * (Fraction(c["S_TECH_FALLBACK_HIGH"]) * f * e + cheap)
    out = {
        "inputs": {"s_tech": f"{sk}/{sn}", "f_fit": f"{fk}/{fn}" if args.f_fit is not None else "n/a", "s_cheap": f"{ck}/{cn}",
                   "s_tech_sessions": args.s_tech_sessions,
                   "s_tech_largest": str(largest)},
        "gating_s_tech_basis": "census" if informative else "fallback_1/45",
        **{k: float(v) for k, v in values.items()},
        "fractions": {k: str(v) for k, v in values.items()}, "flags": flags,
        "evidence": [{"sha256": sha256_file(path)} for path in args.evidence],
        "g_min": {"points": float(c["G_MIN_PTS"]),
                  "rule": ("offload >= max(T_P2, baseline + 0.05) "
                           "and Newcombe LB(P2 - baseline) > 0")},
    }
    if args.baseline:
        bk, bn = counts(args.baseline)
        out["baseline"] = {"counts": f"{bk}/{bn}", "rate": float(Fraction(bk, bn)),
                           "note": "offload per action over all actions"}
    write_json(cfg.run_dir / "tp2.json", out)
    append_use(cfg, "tp2", f"tp2_sha256={sha256_file(cfg.run_dir / 'tp2.json')}")
    return out


def status(cfg: Cfg) -> dict:
    gate_status = "n/a"
    c = STATIC
    try:
        freeze = frozen(cfg)
        is_frozen = True
        c = freeze["constants"]
    except Refuse:
        is_frozen = False
    else:
        try:
            run_gate(cfg, freeze["constants"])
            gate_status = "ok"
        except Refuse as exc:
            gate_status = str(exc)
    path = cfg.run_dir / "selection.json"
    selected = read_json(path).get("sets", {}) if path.exists() else {}
    return {
        "frozen": is_frozen,
        "code_gate": gate_status,
        "pools": {s: (cfg.run_dir / f"pools/{s}.jsonl").exists() for s in SETS},
        "selection_n": {s: r["n"] for s, r in selected.items()},
        "finals": {s: dict(Counter(arm for _, arm in finals(cfg, s))) for s in ("qa", "tech_op")},
        "judged": {s: len(jsonl(cfg.run_dir / f"judgements/{s}.jsonl")) for s in ("qa", "tech_op")},
        "code_rows": len(jsonl(cfg.run_dir / "code_results.jsonl")),
        "foreign_arm_rows": {
            **{s: foreign_rows(cfg.run_dir / f"answers/{s}.jsonl", arms(c))
               for s in ("qa", "tech_op")},
            "code_raw": foreign_rows(cfg.run_dir / "code_results.raw.jsonl", arms(c, True)),
            "code": foreign_rows(cfg.run_dir / "code_results.jsonl", arms(c, True)),
        },
        "spend_usd": float(sum(Fraction(str(r.get("cost_usd", 0)))
                              for r in jsonl(cfg.run_dir / "ledger.jsonl"))),
        "use_kinds": dict(Counter(r[0] for r in uses(cfg) if r)),
    }


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    default = os.environ.get("P20_REQUAL_DIR", str(
        Path.home() / ".rsi/research/primary-plan/v16/requal/b2",
    ))
    ap.add_argument("--run-dir", type=Path, default=Path(default))
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("select").add_argument("--dry-run", action="store_true")
    for command in ("run", "judge"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--set", choices=("qa", "tech_op"), required=True)
        if command == "run":
            cmd.add_argument("--arms", nargs="+")
    cmd = sub.add_parser("ingest-code")
    cmd.add_argument("--results", type=Path, required=True)
    cmd.add_argument("--runner", type=Path, required=True)
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


def real_cfg(run_dir: Path) -> Cfg:
    def git_sha() -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=True).stdout.strip()

    return Cfg(run_dir, ollama=OllamaHTTP(), claude=ClaudeCLI(run_dir), machine=Machine(),
               quota=read_quota, git_sha=git_sha)


def main(argv: list[str] | None = None, *, cfg: Cfg | None = None) -> int:
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
            out = run(cfg, c, args.set, args.arms)
        elif args.command == "judge":
            out = judge(cfg, c, args.set)
        elif args.command == "ingest-code":
            out = ingest_code(cfg, c, freeze, args.results, args.runner)
        elif args.command == "score":
            out = score(cfg, c, freeze, args.measurement)
        elif args.command == "install-eligibility":
            out = install(cfg, args.confirm)
        else:
            out = tp2(cfg, c, args)
        print(json.dumps(out, sort_keys=True, allow_nan=False))
        return 0
    except (Refuse, Park, Terminal, OSError, ValueError, KeyError, TypeError) as exc:
        code = 3 if isinstance(exc, Park) else 4 if isinstance(exc, Terminal) else 2
        print(str(exc) if isinstance(exc, (Refuse, Park, Terminal)) else "refusing: invalid input",
              file=sys.stderr)
        return code


if __name__ == "__main__":
    sys.exit(main())
