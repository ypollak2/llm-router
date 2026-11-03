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
import contextlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

from llm_router import paths, state_files
from llm_router.text_job_bind import gitinput, ledger, limits, sanitize
from llm_router.text_job_bind.plan import (
    COMMIT_CMD, JOB_COMMIT, JOB_PR, PR_CMD, Plan, Unsupported, analyse, option_flags,
)
from llm_router.text_job_bind.shellscan import MENTION, ScanError, scan_commands

LABEL = {JOB_COMMIT: "commit message", JOB_PR: "PR description"}
COOLDOWN_FILE = "cooldown.json"

_COMMIT_CMD = COMMIT_CMD
_PR_CMD = PR_CMD
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


def result_from_sink(sink: dict) -> GenResult:
    """Read what ``llm_text_job`` recorded in its structured sink (``tools/text.py``, ``_llm_text_job``).

    Nothing is parsed out of the returned string: that string ends with the model's own last answer, so a
    reason read from it could come from the model's words. The sink holds fixed codes the router set itself."""
    status = sink.get("status")
    attempts = int(sink.get("attempts") or 0)
    model = str(sink.get("model") or "unknown")
    if status == "accepted":
        return GenResult("accepted", str(sink.get("text") or ""), model, attempts, "")
    if status == "rejected":
        codes = [c for c in (sink.get("codes") or []) if isinstance(c, str)]
        return GenResult("rejected", "", model, attempts, codes[0] if codes else "failed_checks")
    if status == "fallback":
        if sink.get("infra"):
            return GenResult("fallback", "", model, attempts, "local_infra")
        code = sink.get("fallback")
        return GenResult("fallback", "", model, attempts, code if isinstance(code, str) and code in _WHY else "local_failed")
    return GenResult("error", "", "unknown", 0, "unreadable_result")


def attempt_cap_s() -> float | None:
    """The longest one model attempt may take inside the budget: the MCP door's own per-attempt share of its
    deadline (``mcp_door``), reused, not copied. One slow attempt then cannot use the whole budget, and the
    next attempt still has time. ``None`` (no cap) if the constants cannot be read."""
    try:
        from llm_router import mcp_door

        return float(mcp_door.MCP_DEADLINE_DEFAULT_S) * float(mcp_door.ATTEMPT_SHARE)
    except Exception as exc:  # noqa: BLE001 -- no cap is the old behaviour, not a failure
        _failopen("CHZ-FO-TEXTJOB-BIND-ATTEMPT-CAP", exc)
        return None


def load_router() -> None:
    """Import the router's text-job tool. Slow (the model libraries load here), and none of it is the model's
    wait: ``_decide`` calls this BEFORE the ``text_job`` phase so the hook-latency KPI books it as router-added."""
    from llm_router.tools import text as text_tools  # noqa: F401


def load_env() -> None:
    """Apply ``$LLM_ROUTER_HOME/.env`` (and the project's, filtered) the way the MCP server does, so the off switch
    ``LLM_ROUTER_LOCAL_TEXT_JOBS=0`` and the context/think settings the lane was checked with reach this process.
    The real environment always wins. Best-effort."""
    try:
        from llm_router.env_loader import load_dotenv_files

        load_dotenv_files()
    except Exception as exc:  # noqa: BLE001 -- a missing .env loader never changes the answer
        _failopen("CHZ-FO-TEXTJOB-BIND-ENV", exc)


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

    sink: dict = {}
    cap = attempt_cap_s()

    async def run() -> str:
        return await asyncio.wait_for(
            text_tools._llm_text_job(job, input_text, _Ctx(), hint, sink, attempt_timeout_s=cap),
            timeout=max(1.0, timeout_s),
        )

    try:
        asyncio.run(run())
    except (asyncio.TimeoutError, TimeoutError):
        return GenResult("timeout", "", "none", 0, "timeout")
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-TEXTJOB-BIND-GENERATE", exc)
        return GenResult("error", "", "none", 0, "job_raised")
    return result_from_sink(sink)


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
    idx = plan.index
    argv = [w.literal() for w in after[idx].argv]
    if option_flags(plan.job, new_command, idx) != plan.flags:
        raise Unsupported("verify", "The rewritten command lost or changed an option.")
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
    "local_infra": "The local model could not be reached or was too busy to answer in time.",
    "lane_unverified": "A local model passed the checks for this job, but it could not be verified on this machine "
                       "(its installed version could not be read or no longer matches the one that passed), so it was not used.",
    "busy": "Another commit-message or PR job is using the local model on this machine and none finished in time.",
    "deadline_guard": "The text-job hook ran out of time.",
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
    prepare: Callable[[], None] | None = None,
    lock_wait_s: float = limits.LOCK_WAIT_SEC,
    body_mode: str | None = None,
) -> Decision:
    """What to say about the Bash call in ``payload``. Never raises.

    ``prepare`` loads what the model call needs (default: the router, only when ``generate`` is the real
    one); it runs outside the model phase. ``lock_wait_s`` is how long a job waits for the machine-wide job
    lock. ``body_mode`` overrides ``LLM_ROUTER_TEXT_JOB_BODY`` / ``limits.COMMIT_BODY_MODE``."""
    t0 = mono() if started is None else started
    if prepare is None and generate is generate_with_text_job:
        prepare = load_router
    try:
        return _decide(payload, generate, t0, budget_s, root or paths.state_path("text_job_bind"), record, now, mono,
                       prepare, lock_wait_s, body_mode)
    except Exception as exc:  # noqa: BLE001 -- see below: a placeholder command must still get a deny
        _failopen("CHZ-FO-TEXTJOB-BIND-DECIDE", exc)
        cmd = _command_of(payload)
        if cmd is not None and MENTION.search(cmd) and handwritten_job(cmd):
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


def _body_mode(override: str | None) -> str:
    mode = (override or os.environ.get("LLM_ROUTER_TEXT_JOB_BODY", "") or limits.COMMIT_BODY_MODE).strip().lower()
    return mode if mode in limits.BODY_MODES else limits.COMMIT_BODY_MODE


def _subject(text: str) -> str:
    return next((ln.strip() for ln in text.split("\n") if ln.strip()), "")


def _assemble(job: str, text: str, mode: str, stats: list) -> str:
    """The text the file holds, by body mode. A PR body is the model's own (cleaned); a commit message is the
    model's subject plus, by ``mode``, nothing, a list of the changed files built from git, or the model's body."""
    if job != JOB_COMMIT or mode == "model":
        return text
    subject = _subject(text)
    if mode == "none" or not stats:
        return subject
    return subject + "\n\n" + gitinput.files_body(stats)


def _describe(plan: Plan, text: str) -> str:
    """What the human who approves the command sees next to it: the command's text is a file path, so the
    Bash ``description`` carries the generated subject (or the PR title)."""
    one = _subject(text) if plan.job == JOB_COMMIT else (plan.title or _subject(text.lstrip("# ")))
    one = re.sub(r"[\x00-\x1f\x7f]+", " ", one).strip()[:100]
    head = "git commit" if plan.job == JOB_COMMIT else "gh pr create"
    return f"{head} (local text): {one}" if one else f"{head} (local text)"


@contextlib.contextmanager
def _job_lock(root: Path, wait_s: float, mono: Callable[[], float] = time.monotonic):
    """The machine-wide job lock: one text job at a time, because Ollama serves one request at a time and most
    commits come from parallel sub-agents. Yields True when held, False when it was not free within ``wait_s``.
    An ``flock`` on a file: released by the OS when the process exits, however it exits. Where ``fcntl`` does not
    exist the lock is not taken (yields True)."""
    try:
        import fcntl
    except ImportError:
        yield True
        return
    fd = -1
    try:
        state_files.private_dir(root)
        fd = os.open(root / "job.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        _failopen("CHZ-FO-TEXTJOB-BIND-LOCK", exc)
        yield True  # no lock file: run unserialised rather than refuse every commit
        return
    held = False
    try:
        end = mono() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if mono() >= end:
                    break
                time.sleep(0.05)
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


_model_started: list[float] = []


def record_deadline(payload: dict) -> dict[str, float]:
    """The deadline guard fired: the hook is about to answer with a deny and exit without having decided.
    Leave what a timeout inside the engine leaves: a ledger row and the cooldown. Returns the model wait that is
    still open, as ``{"text_job": ms}`` for ``hook_latency.finish_now`` to book (so the G1 figure does not count it
    as router time), or ``{}``. Never raises."""
    try:
        cmd = _command_of(payload) or ""
        job = handwritten_job(cmd) or JOB_COMMIT
        _safe_record(ledger.record_row, _row(payload, job, cmd, ledger.KIND_DENIED, "timeout", "deadline_guard"))
        _start_cooldown(paths.state_path("text_job_bind"), time.time())
        if _model_started:
            return {"text_job": (time.monotonic() - _model_started[0]) * 1000.0}
    except Exception as exc:  # noqa: BLE001 -- the deny must still go out
        _failopen("CHZ-FO-TEXTJOB-BIND-DEADLINE", exc)
    return {}


def _decide(payload, generate, t0, budget_s, root, record, now, mono, prepare=None,
            lock_wait_s=limits.LOCK_WAIT_SEC, body_mode=None) -> Decision:
    cmd = _command_of(payload)
    if cmd is None:
        return Decision("pass")
    if not MENTION.search(cmd):
        job = handwritten_job(cmd)
        if job:
            _safe_record(record, _row(payload, job, cmd, ledger.KIND_HANDWRITTEN, "none", ""))
        return Decision("pass")

    from llm_router.hooks import hook_payload as hp

    load_env()
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

    if prepare is not None:  # the router import: seconds, and not the model's wait
        try:
            prepare()
        except Exception as exc:  # noqa: BLE001
            _failopen("CHZ-FO-TEXTJOB-BIND-IMPORT", exc)
            return deny(plan.job, "error", "router_import_failed", _deny_text(plan.job, _WHY["router_import_failed"]),
                        in_chars=len(inp.text))

    with _job_lock(root, min(lock_wait_s, max(0.0, budget_s - (mono() - t0) - 5)), mono) as got:
        if not got:
            return deny(plan.job, "busy", "lock_held", _deny_text(plan.job, _WHY["busy"]), in_chars=len(inp.text))
        remaining = budget_s - (mono() - t0)
        if remaining < 5:
            return deny(plan.job, "timeout", "no_time_left", _deny_text(plan.job, "There was no time left to run the local text job."),
                        in_chars=len(inp.text))
        _model_started[:] = [mono()]
        with _model_phase():
            res = generate(plan.job, inp.text, inp.hint, remaining)
        _model_started.clear()
    extra = {"in_chars": len(inp.text), "attempts": res.attempts, "model": res.model}
    if res.status == "timeout":
        _start_cooldown(root, now())
        return deny(plan.job, "timeout", "budget", _deny_text(plan.job, f"The local {LABEL[plan.job]} job did not finish in {int(budget_s)} s."), **extra)
    if res.status != "accepted":
        outcome = res.status if res.status in ("rejected", "fallback") else "error"
        return deny(plan.job, outcome, res.reason, _deny_text(plan.job, _WHY.get(res.reason, "The local text job did not produce a checked text.")), **extra)

    text = clean_text(res.text)
    cleaned = sanitize.clean(text, pr_body=plan.job == JOB_PR)
    text = cleaned.text
    mode = _body_mode(body_mode)
    extra.update(body_mode=mode if plan.job == JOB_COMMIT else "model", stripped_mentions=cleaned.mentions,
                 stripped_closing=cleaned.closing, replaced_test_plan=cleaned.test_plan)
    text = _assemble(plan.job, text, mode, inp.stats)
    if not text.strip():
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
        except OSError as unlink_exc:
            _failopen("CHZ-FO-TEXTJOB-BIND-CLEANUP", unlink_exc)
        code = exc.code if isinstance(exc, Unsupported) else "rewrite_failed"
        if not isinstance(exc, Unsupported):
            _failopen("CHZ-FO-TEXTJOB-BIND-REWRITE", exc)
        return deny(plan.job, "error", code, _deny_text(plan.job, "The command could not be rewritten safely."), **extra)

    new_input = dict(tool_input)  # every other field of the tool input stays as the model wrote it
    new_input["command"] = new_command
    new_input["description"] = _describe(plan, text)
    row = _row(payload, plan.job, cmd, ledger.KIND_BOUND, "accepted", "", latency_ms=lat(), out_chars=len(text), **extra)
    _safe_record(record, row)
    note = f"llm-router: {LABEL[plan.job]} written by {res.model}, checked, {len(text)} chars (not written by Claude)."
    return Decision("rewrite", tool_input=new_input, note=note, row=row)
