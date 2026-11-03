"""Decide what the text-job-bind hook says about one Bash command.

The hook (``hooks/text-job-bind.py``) is a thin shell around :func:`decide`. For a Bash command
that does not use the placeholder ``"${LLM_TEXT:?}"`` the answer is "no opinion" and, for a
hand-written ``git commit`` or ``gh pr create``, one ledger row. For one that does, the answer is
exactly one of:

* a REWRITE: the local text job produced a text that passed its checks, the text is in a private
  file, and the command now reads ``-F <file>`` / ``--body-file <file>``;
* a DENY with a short reason that says what to do instead.

There is no third answer. A placeholder command is never allowed through unchanged and a text
that did not pass its checks is never used. A failure of any kind, in this module or in the
model, ends in a deny.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

from llm_router import paths, state_files
from llm_router.text_job_bind import gitinput, ledger, limits
from llm_router.text_job_bind.plan import JOB_COMMIT, JOB_PR, Plan, Unsupported, analyse
from llm_router.text_job_bind.shellscan import TOKEN_ANYWHERE, ScanError, scan_commands

LABEL = {JOB_COMMIT: "commit message", JOB_PR: "PR description"}
COOLDOWN_FILE = "cooldown.json"

_COMMIT_CMD = re.compile(r"(?<![\w-])git\s+(?:(?:-C|-c)\s+\S+\s+|--[\w-]+(?:=\S+)?\s+)*commit(?![\w-])")
_PR_CMD = re.compile(r"(?<![\w-])gh\s+pr\s+create(?![\w-])")
_STRUCTURED = re.compile(r"_Structured: status=(accepted|rejected|fallback), attempts=(\d+)_")
_MODEL = re.compile(r"[\w.\-]+/[\w.:\-@]+")
_ON = frozenset({"1", "true", "on", "yes"})


class GenResult(NamedTuple):
    """What the text job said. ``status`` is ``accepted``, ``rejected``, ``fallback``, ``timeout``
    or ``error``; ``reason`` is a short fixed code, never model text."""

    status: str
    text: str
    model: str
    attempts: int
    reason: str


Generate = Callable[[str, str, "str | None", float], GenResult]


@dataclass
class Decision:
    kind: str  # "pass" | "rewrite" | "deny"
    tool_input: dict | None = None  # the full replacement input, for "rewrite"
    reason: str | None = None  # what the model is told, for "deny"
    note: str | None = None  # a one-line message for the user, for "rewrite"
    row: dict = field(default_factory=dict)


# ---- the production generator: the same text job the MCP tool runs ---------------------------------

class _Ctx:
    """The two calls ``llm_text_job`` makes on its MCP context; there is no client to tell here."""

    async def info(self, *_a, **_k) -> None:
        return None

    async def report_progress(self, *_a, **_k) -> None:
        return None


def _classify_reason(text: str) -> str:
    t = text.lower()
    for needle, code in (
        ("empty commit message", "empty"), ("subject line too long", "subject_too_long"),
        ("generic placeholder subject", "generic_subject"), ("any changed file", "names_no_changed_file"),
        ("reference any changed file", "names_no_changed_file"), ("summary section", "no_summary"),
        ("test plan", "no_test_plan"), ("too short", "too_short"),
    ):
        if needle in t:
            return code
    return "failed_checks"


def parse_tool_result(out: str) -> GenResult:
    """Read the string ``llm_text_job`` returns. Anything unexpected is an ``error``, never a text."""
    m = _STRUCTURED.search(out)
    if not m:
        return GenResult("error", "", "unknown", 0, "unreadable_result")
    status, attempts = m.group(1), int(m.group(2))
    head = out[:m.start()]
    first = out.split("\n", 1)[0]
    mm = _MODEL.search(first)
    model = mm.group(0) if mm else "unknown"
    if status == "accepted":
        rest = out[m.end():]
        if rest.startswith("\n"):
            rest = rest[1:]
        if rest.startswith("_input_text truncated"):
            rest = rest.split("\n", 1)[1] if "\n" in rest else ""
        if rest.startswith("\n"):
            rest = rest[1:]
        return GenResult("accepted", rest, model, attempts, "")
    if status == "rejected":
        reasons = re.findall(r"(?m)^\s+\d+\.\s+(.*)$", out[m.end():])
        return GenResult("rejected", "", model, attempts, _classify_reason(reasons[-1] if reasons else ""))
    low = head.lower()
    if "turned off" in low:
        code = "flag_off"
    elif "no passed local-check lane" in low:
        code = "no_lane"
    elif "no longer matches" in low or "not in the router's chain" in low or "does not fit" in low:
        code = "lane_refused"
    else:
        code = "local_failed"
    return GenResult("fallback", "", model, attempts, code)


def generate_with_text_job(job: str, input_text: str, hint: str | None, timeout_s: float) -> GenResult:
    """Run the router's own ``llm_text_job`` (same checks, same eligibility, same ledgers) and wait
    at most ``timeout_s`` for it. Bound text jobs are LOCAL-ONLY: ``LLM_ROUTER_LOCAL_TEXT_JOBS``
    set to a falsy value turns them off, and a truthy value (which would let the full routing
    chain, cloud models included, see the diff) is treated as unset for this process."""
    flag = os.environ.get("LLM_ROUTER_LOCAL_TEXT_JOBS", "").strip().lower()
    if flag and flag not in _ON:
        return GenResult("fallback", "", "none", 0, "flag_off")
    os.environ.pop("LLM_ROUTER_LOCAL_TEXT_JOBS", None)
    try:
        from llm_router.tools import text as text_tools
    except Exception as exc:  # noqa: BLE001 -- reported as a failure of the job, never as a text
        _failopen("CHZ-FO-TEXTJOB-BIND-IMPORT", exc)
        return GenResult("error", "", "none", 0, "router_import_failed")

    async def run() -> str:
        return await asyncio.wait_for(
            text_tools.llm_text_job(job, input_text, _Ctx(), hint), timeout=max(1.0, timeout_s)
        )

    try:
        out = asyncio.run(run())
    except (asyncio.TimeoutError, TimeoutError):
        return GenResult("timeout", "", "none", 0, "timeout")
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-TEXTJOB-BIND-GENERATE", exc)
        return GenResult("error", "", "none", 0, "job_raised")
    return parse_tool_result(out)


def _failopen(code: str, exc: BaseException) -> None:
    try:
        from llm_router import failopen
        failopen.record(code, exc)
    except Exception:  # noqa: BLE001 -- no counter available: nothing else to try
        return


# ---- helpers ------------------------------------------------------------------------------------------

def handwritten_job(command: str) -> str | None:
    """The job a hand-written ``git commit`` / ``gh pr create`` command is, else ``None``. A regex,
    not the scanner: it only feeds the adoption count, and must work on any command the model writes."""
    if _COMMIT_CMD.search(command):
        return JOB_COMMIT
    if _PR_CMD.search(command):
        return JOB_PR
    return None


def clean_text(text: str) -> str:
    """The model's text as it will be written: LF newlines, no NUL, no wrapping code fence, no
    trailing whitespace on a line."""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [ln.rstrip() for ln in t.strip("\n").split("\n")]
    if len(lines) >= 2 and re.fullmatch(r"```\w*", lines[0].strip()) and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines).strip("\n")


def _cooldown_remaining(root: Path, now: float) -> float:
    try:
        until = float(json.loads((root / COOLDOWN_FILE).read_text(encoding="utf-8")).get("until", 0))
    except (OSError, ValueError, AttributeError):
        return 0.0
    return max(0.0, until - now)


def _start_cooldown(root: Path, now: float) -> None:
    try:
        state_files.private_dir(root)
        with state_files.open_private(root / COOLDOWN_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"until": now + limits.COOLDOWN_SEC}))
    except OSError as exc:
        _failopen("CHZ-FO-TEXTJOB-BIND-COOLDOWN", exc)


def _sweep(root: Path, now: float) -> None:
    try:
        for p in root.glob("*.txt"):
            try:
                if now - p.stat().st_mtime > limits.FILE_TTL_SEC:
                    p.unlink()
            except OSError:
                continue
    except OSError as exc:
        _failopen("CHZ-FO-TEXTJOB-BIND-SWEEP", exc)


def _write_message_file(root: Path, text: str, now: float) -> Path:
    state_files.private_dir(root)
    _sweep(root, now)
    path = root / f"{int(now)}-{uuid.uuid4().hex}.txt"
    with state_files.open_private(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def _verify(plan: Plan, new_command: str, path: str) -> None:
    """Re-read the rewritten command and prove it is what we meant: the same commands as before,
    no placeholder left, no message option left, and the job command points at the file."""
    before, after = scan_commands(plan.command), scan_commands(new_command)
    if len(before) != len(after):
        raise Unsupported("verify", "The rewritten command changed shape.")
    if any(w.has_token for c in after for w in (*c.assigns, *c.argv, *(t for _o, t in c.redirs))):
        raise Unsupported("verify", "A placeholder is still in the rewritten command.")
    idx = next(k for k, c in enumerate(before) if any(w.has_token for w in c.argv))
    argv = [w.literal() for w in after[idx].argv]
    if plan.job == JOB_COMMIT:
        wants, gone = ("-F", "--file"), re.compile(r"^(?:--message(?:=.*)?|-[A-Za-z]*m)$")
        eq_prefix = "--file="
    else:
        wants, gone = ("--body-file",), re.compile(r"^(?:--body(?:=.*)?|-b)$")
        eq_prefix = "--body-file="
    if any(a is not None and gone.match(a) for a in argv):
        raise Unsupported("verify", "A message option is still in the rewritten command.")
    ok = any(a in wants and k + 1 < len(argv) and argv[k + 1] == path for k, a in enumerate(argv)) or any(
        a == eq_prefix + path for a in argv
    )
    if not ok:
        raise Unsupported("verify", "The rewritten command does not read the message file.")


def _deny_text(job: str, why: str) -> str:
    label = LABEL[job]
    return (f"{why} Nothing was run. Write the {label} yourself and run the command again "
            "without the placeholder.")


_WHY = {
    "flag_off": "Local text jobs are turned off on this machine.",
    "no_lane": "No local model has passed the checks for this job, so none was used.",
    "lane_refused": "The checked local model could not be used for this input.",
    "local_failed": "The local model did not answer.",
    "router_import_failed": "The router could not be loaded to run the text job.",
    "job_raised": "The text job failed.",
    "unreadable_result": "The text job answered in a form this hook could not read.",
    "empty": "The local model's text was empty.",
    "subject_too_long": "The local model's text failed the checks (subject line too long).",
    "generic_subject": "The local model's text failed the checks (generic subject).",
    "names_no_changed_file": "The local model's text failed the checks (it names no changed file).",
    "no_summary": "The local model's text failed the checks (no Summary section).",
    "no_test_plan": "The local model's text failed the checks (no Test plan section).",
    "too_short": "The local model's text failed the checks (too short).",
    "failed_checks": "The local model's text failed the checks.",
}


# ---- the decision -------------------------------------------------------------------------------------

def decide(
    payload: dict,
    *,
    generate: Generate = generate_with_text_job,
    started: float | None = None,
    budget_s: float = limits.BUDGET_SEC,
    root: Path | None = None,
    record: Callable[[dict], None] = ledger.record_row,
    now: Callable[[], float] = time.time,
    mono: Callable[[], float] = time.monotonic,
) -> Decision:
    """What to say about the Bash call in ``payload``. Never raises."""
    t0 = mono() if started is None else started
    try:
        return _decide(payload, generate, t0, budget_s, root or paths.state_path("text_job_bind"), record, now, mono)
    except Exception as exc:  # noqa: BLE001 -- see below: a placeholder command must still get a deny
        _failopen("CHZ-FO-TEXTJOB-BIND-DECIDE", exc)
        cmd = _command_of(payload)
        if cmd is not None and TOKEN_ANYWHERE.search(cmd) and handwritten_job(cmd):
            job = handwritten_job(cmd) or JOB_COMMIT
            row = _row(payload, job, cmd, ledger.KIND_DENIED, "error", type(exc).__name__)
            _safe_record(record, row)
            return Decision("deny", reason=_deny_text(job, "The text-job hook failed."), row=row)
        return Decision("pass")



def _command_of(payload: dict) -> str | None:
    from llm_router.hooks import hook_payload as hp

    if not hp.is_tool(payload, "Bash"):
        return None
    cmd = hp.tool_input(payload).get("command")
    return cmd if isinstance(cmd, str) and cmd else None


def _s(v: object, n: int = 80) -> str | None:
    return v[:n] if isinstance(v, str) and v else None


def _row(payload: dict, job: str, cmd: str, kind: str, outcome: str, reason: str, **extra) -> dict:
    row = {
        "kind": kind, "job": job, "outcome": outcome, "reason": reason,
        "session_id": _s(payload.get("session_id")), "tool_use_id": _s(payload.get("tool_use_id")),
        "agent_type": _s(payload.get("agent_type")), "subagent": bool(payload.get("agent_id")),
        "cmd_chars": len(cmd),
    }
    row.update(extra)
    return row


def _model_phase():
    """The wait on the local model, named for the G1 hook-latency KPI: it is the answer, not
    router-added time (``hook_latency.MODEL_PHASES``). A no-op outside an armed hook process."""
    try:
        from llm_router.hook_latency import phase

        return phase("text_job")
    except Exception:  # noqa: BLE001 -- timing metadata never changes the answer
        import contextlib

        return contextlib.nullcontext()


def _safe_record(record: Callable[[dict], None], row: dict) -> None:
    try:
        record(row)
    except Exception as exc:  # noqa: BLE001 -- the ledger never changes the answer
        _failopen("CHZ-FO-TEXTJOB-BIND-RECORD", exc)


def _decide(payload, generate, t0, budget_s, root, record, now, mono) -> Decision:
    cmd = _command_of(payload)
    if cmd is None:
        return Decision("pass")
    if not TOKEN_ANYWHERE.search(cmd):
        job = handwritten_job(cmd)
        if job:
            _safe_record(record, _row(payload, job, cmd, ledger.KIND_HANDWRITTEN, "none", ""))
        return Decision("pass")

    from llm_router.hooks import hook_payload as hp

    tool_input = hp.tool_input(payload)
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()

    def lat() -> int:
        return int((mono() - t0) * 1000)

    def deny(job: str, outcome: str, reason: str, text: str, **extra) -> Decision:
        row = _row(payload, job, cmd, ledger.KIND_DENIED, outcome, reason, latency_ms=lat(), **extra)
        _safe_record(record, row)
        return Decision("deny", reason=text, row=row)

    guess = handwritten_job(cmd) or JOB_COMMIT
    try:
        plan = analyse(cmd, cwd)
    except Unsupported as u:
        return deny(guess, "unsupported", u.code, f"{u.message} Nothing was run. Or write the text yourself and run it without the placeholder.")
    except ScanError as e:
        if handwritten_job(cmd) is None:
            return Decision("pass")  # a doc or script that merely mentions the placeholder
        return deny(guess, "unsupported", "unreadable",
                    f"The command could not be read as a plain git/gh command ({e}). Nothing was run. "
                    "Simplify it, or write the text yourself and run it without the placeholder.")
    if plan is None:
        return Decision("pass")

    cool = _cooldown_remaining(root, now())
    if cool > 0:
        return deny(plan.job, "cooldown", "after_timeout",
                    _deny_text(plan.job, f"The local text job timed out a moment ago and is paused for {int(cool // 60) + 1} more minutes."))

    try:
        inp = (gitinput.build_commit_input(plan.repo_dir, all_tracked=plan.all_tracked)
               if plan.job == JOB_COMMIT
               else gitinput.build_pr_input(plan.repo_dir, base=plan.base, head=plan.head, title=plan.title))
    except gitinput.InputRefused as r:
        return deny(plan.job, "refused", r.code, f"{r.message} Nothing was run. Or write the text yourself and run it without the placeholder.")

    remaining = budget_s - (mono() - t0)
    if remaining < 5:
        return deny(plan.job, "timeout", "no_time_left", _deny_text(plan.job, "There was no time left to run the local text job."),
                    in_chars=len(inp.text))
    with _model_phase():
        res = generate(plan.job, inp.text, inp.hint, remaining)
    extra = {"in_chars": len(inp.text), "attempts": res.attempts, "model": res.model}
    if res.status == "timeout":
        _start_cooldown(root, now())
        return deny(plan.job, "timeout", "budget", _deny_text(plan.job, f"The local {LABEL[plan.job]} job did not finish in {int(budget_s)} s."), **extra)
    if res.status != "accepted":
        outcome = res.status if res.status in ("rejected", "fallback") else "error"
        return deny(plan.job, outcome, res.reason, _deny_text(plan.job, _WHY.get(res.reason, "The local text job did not produce a checked text.")), **extra)

    text = clean_text(res.text)
    if not text:
        return deny(plan.job, "rejected", "empty", _deny_text(plan.job, _WHY["empty"]), **extra)
    try:
        path = _write_message_file(root, plan.final_text(text), now())
    except OSError as exc:
        _failopen("CHZ-FO-TEXTJOB-BIND-WRITE", exc)
        return deny(plan.job, "error", "write_failed", _deny_text(plan.job, "The text file could not be written."), **extra)
    try:
        new_command = plan.rewrite(str(path))
        _verify(plan, new_command, str(path))
    except Exception as exc:  # noqa: BLE001 -- an unverified rewrite is never emitted
        try:
            path.unlink()
        except OSError:
            pass
        code = exc.code if isinstance(exc, Unsupported) else "rewrite_failed"
        if not isinstance(exc, Unsupported):
            _failopen("CHZ-FO-TEXTJOB-BIND-REWRITE", exc)
        return deny(plan.job, "error", code, _deny_text(plan.job, "The command could not be rewritten safely."), **extra)

    new_input = dict(tool_input)
    new_input["command"] = new_command
    row = _row(payload, plan.job, cmd, ledger.KIND_BOUND, "accepted", "", latency_ms=lat(), out_chars=len(text), **extra)
    _safe_record(record, row)
    note = f"llm-router: {LABEL[plan.job]} written by {res.model}, checked, {len(text)} chars (not written by Claude)."
    return Decision("rewrite", tool_input=new_input, note=note, row=row)
