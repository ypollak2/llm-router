"""The text-free ledger of the text-job-bind hook, and its report.

One row per ``git commit`` or ``gh pr create`` command the hook sees: those that used the
placeholder (``bound`` when the local job's checked text was put in the command, ``denied``
when it was not) and those the model wrote by hand (``handwritten``, the denominator of
adoption). A row holds sizes and a reason code, never the text of the diff, the message, the
body or the command.

Rows go to ``text_job_binds.jsonl`` in the state directory through ``capped_log`` (two
generations of 2 MiB, 0600, one append, no lock). Writing is best-effort and never raises: a
failed row must not change what the hook says about a command.

``python -m llm_router.text_job_bind report`` prints the outcome and latency figures from these rows and,
when asked to scan the Claude Code transcripts, the adoption figure. The token figures are estimates:
characters divided by 4. A ledger written by a hook registered with an ``if`` filter has no hand-written rows,
so it cannot say what share of commit and PR commands used the placeholder: that denominator comes from the
transcripts, or the report says it is not available.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from llm_router import capped_log, paths

LEDGER_FILENAME = "text_job_binds.jsonl"
MAX_LEDGER_BYTES = 2 * 1024 * 1024

KIND_BOUND = "bound"
KIND_DENIED = "denied"
KIND_HANDWRITTEN = "handwritten"

#: Characters per token used for every estimate in the report. A rule of thumb, labelled as one.
CHARS_PER_TOKEN = 4


def ledger_path() -> Path:
    return paths.state_path(LEDGER_FILENAME)


def record_row(row: dict) -> None:
    """Append one row (a ``ts`` is added). Best-effort: never raises."""
    try:
        data = dict(row)
        data.setdefault("ts", time.time())
        capped_log.append(
            ledger_path(), (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
            MAX_LEDGER_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 -- a ledger row never changes the hook's answer
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-TEXTJOB-BIND-LEDGER", exc)
        except Exception:  # noqa: BLE001 -- no package, no counter: nothing else to try
            return


def read_rows(since: float | None = None) -> list[dict]:
    rows = capped_log.read_dicts(ledger_path())
    if since is not None:
        rows = [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] >= since]
    return rows


#: Denied outcomes by what went wrong. A command-shape refusal says nothing about the model; a capacity refusal says
#: nothing about the model either (the lane was busy or paused); only the third group is the model's.
SHAPE_OUTCOMES = frozenset({"unsupported", "refused"})
CAPACITY_OUTCOMES = frozenset({"busy", "cooldown"})


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _lat(rows: list[dict]) -> list[float]:
    return [float(r["latency_ms"]) for r in rows if isinstance(r.get("latency_ms"), (int, float))]


def _latency_block(rows: list[dict]) -> dict:
    lat = _lat(rows)
    return {"n": len(rows), "n_timed": len(lat), "p50": statistics.median(lat) if lat else None,
            "p95": _pct(lat, 0.95), "max": max(lat) if lat else None}


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(rows: list[dict], *, transcripts: dict | None = None) -> dict:
    """The report as a dict. Every count carries its own N so nothing is a bare percentage.

    ``transcripts`` is the result of :func:`scan_transcripts`; without it adoption is not computed."""
    bound = [r for r in rows if r.get("kind") == KIND_BOUND]
    denied = [r for r in rows if r.get("kind") == KIND_DENIED]
    hand = [r for r in rows if r.get("kind") == KIND_HANDWRITTEN]
    used = len(bound) + len(denied)
    total = used + len(hand)
    by_job: dict[str, dict[str, int]] = {}
    for r in rows:
        j = by_job.setdefault(str(r.get("job")), {KIND_BOUND: 0, KIND_DENIED: 0, KIND_HANDWRITTEN: 0})
        if r.get("kind") in j:
            j[r["kind"]] += 1
    reasons: dict[str, int] = {}
    for r in denied:
        key = f"{r.get('outcome')}:{r.get('reason') or '-'}"
        reasons[key] = reasons.get(key, 0) + 1
    shape = [r for r in denied if r.get("outcome") in SHAPE_OUTCOMES]
    capacity = [r for r in denied if r.get("outcome") in CAPACITY_OUTCOMES]
    model = [r for r in denied if r.get("outcome") not in SHAPE_OUTCOMES | CAPACITY_OUTCOMES]
    # Latency of EVERY outcome, so the budget and the cooldown can be tuned from the slow ones too.
    groups: dict[str, list[dict]] = {}
    for r in (*bound, *denied):
        groups.setdefault(f"{r.get('kind')}:{r.get('outcome')}", []).append(r)
    by_outcome = {k: _latency_block(v) for k, v in sorted(groups.items())}
    reached_model = len(bound) + len(model)
    out_chars = sum(int(r.get("out_chars") or 0) for r in bound)
    hand_chars = sum(int(r.get("cmd_chars") or 0) for r in hand)
    stamps = [r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))]
    adoption = None
    reason = None
    if transcripts is not None and transcripts.get("n_commit_pr"):
        adoption = transcripts["n_placeholder"] / transcripts["n_commit_pr"]
    elif transcripts is not None:
        reason = "the transcripts in the window hold no git commit or gh pr create command"
    elif hand:
        adoption, reason = (used / total if total else None), None
    else:
        reason = ("the ledger has no hand-written rows (the hook is registered with an `if` filter and never sees a "
                  "command without the placeholder); run the report with --transcripts for a denominator")
    return {
        "n_rows": len(rows),
        "n_commit_pr_commands": total,
        "n_placeholder": used,
        "n_bound": len(bound),
        "n_denied": len(denied),
        "n_denied_shape": len(shape),
        "n_denied_capacity": len(capacity),
        "n_denied_model": len(model),
        "n_handwritten": len(hand),
        "adoption": adoption,
        "adoption_source": ("transcripts" if transcripts is not None else "ledger" if hand else None),
        "adoption_unavailable_reason": reason,
        "transcripts": transcripts,
        "denominator_known": bool(hand) or bool(transcripts and transcripts.get("n_commit_pr")),
        "bound_rate": (len(bound) / used) if used else None,
        "bound_rate_when_model_ran": (len(bound) / reached_model) if reached_model else None,
        "n_reached_model": reached_model,
        "by_job": by_job,
        "denied_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "latency_by_outcome": by_outcome,
        "bound_latency_ms_p50": by_outcome.get("bound:accepted", {}).get("p50"),
        "bound_latency_ms_p95": by_outcome.get("bound:accepted", {}).get("p95"),
        "bound_out_chars": out_chars,
        "est_output_tokens_avoided": out_chars // CHARS_PER_TOKEN,
        "handwritten_cmd_chars": hand_chars,
        "est_output_tokens_still_handwritten": hand_chars // CHARS_PER_TOKEN,
        "since": min(stamps, default=None),
        "until": max(stamps, default=None),
        "units": "latency in milliseconds, from the hook's start to its answer; times in UTC; token figures are characters / 4",
    }


def _ts(value: object) -> float | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def default_transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


def scan_transcripts(roots: list[Path], since: float, until: float | None = None) -> dict:
    """Count the ``git commit`` / ``gh pr create`` commands the model ran through the Bash tool in Claude Code
    transcripts (sub-agent transcripts included), and how many of them used the placeholder.

    This is the denominator the ledger cannot give. Only ``tool_use`` blocks are counted (a command the model
    wrote, not one a document quotes), each once per ``tool_use`` id, inside ``[since, until]``. Transcripts older
    than the host's retention are gone, so the window is whatever the files still hold; ``files`` and
    ``first``/``last`` say what was read."""
    from llm_router.text_job_bind.plan import COMMIT_CMD, PR_CMD
    from llm_router.text_job_bind.shellscan import MENTION

    seen: set[str] = set()
    n_total = n_placeholder = files = 0
    stamps: list[float] = []
    for root in roots:
        try:
            candidates = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
        except OSError:
            continue
        for path in candidates:
            try:
                if path.stat().st_mtime < since:
                    continue
                files += 1
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"tool_use"' not in line or "Bash" not in line:
                            continue
                        if "commit" not in line and "pr create" not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        ts = _ts(obj.get("timestamp"))
                        if ts is not None and (ts < since or (until is not None and ts > until)):
                            continue
                        msg = obj.get("message")
                        content = msg.get("content") if isinstance(msg, dict) else None
                        for block in content if isinstance(content, list) else []:
                            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"):
                                continue
                            cmd = (block.get("input") or {}).get("command") if isinstance(block.get("input"), dict) else None
                            if not isinstance(cmd, str) or not (COMMIT_CMD.search(cmd) or PR_CMD.search(cmd)):
                                continue
                            uid = str(block.get("id") or f"{path}:{hash(cmd)}")
                            if uid in seen:
                                continue
                            seen.add(uid)
                            n_total += 1
                            n_placeholder += 1 if MENTION.search(cmd) else 0
                            if ts is not None:
                                stamps.append(ts)
            except OSError:
                continue
    return {"n_commit_pr": n_total, "n_placeholder": n_placeholder, "files": files,
            "first": min(stamps, default=None), "last": max(stamps, default=None)}


def render(summary: dict) -> str:
    s = summary

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x * 100:.0f}%"

    def num(x: float | None) -> str:
        return "n/a" if x is None else f"{x:,.0f}"

    days = s.get("window_days")
    lines = [
        f"text-job-bind ledger: {s['n_rows']} rows, {s['n_placeholder']} placeholder commands "
        f"({s['n_bound']} bound, {s['n_denied']} denied), {s['n_handwritten']} hand-written rows",
        f"  window: {_iso(s['since']) or 'n/a'} to {_iso(s['until']) or 'n/a'}"
        + (f" (the last {days:g} days requested)" if days else ""),
        f"  units: {s['units']}",
    ]
    t = s.get("transcripts")
    if s["adoption"] is not None and s["adoption_source"] == "transcripts" and t:
        lines.append(
            f"  adoption: {t['n_placeholder']} of {t['n_commit_pr']} git commit / gh pr create commands in "
            f"{t['files']} transcript files used the placeholder ({pct(s['adoption'])}); transcripts: "
            f"{_iso(t['first']) or 'n/a'} to {_iso(t['last']) or 'n/a'}"
        )
    elif s["adoption"] is not None:
        lines.append(f"  adoption: {s['n_placeholder']} of {s['n_commit_pr_commands']} commands in the ledger "
                     f"({pct(s['adoption'])})")
    else:
        lines.append(f"  adoption: n/a - {s['adoption_unavailable_reason']}")
    lines += [
        f"  bound: {s['n_bound']} of {s['n_placeholder']} placeholder commands ({pct(s['bound_rate'])}); "
        f"when the model actually ran: {s['n_bound']} of {s['n_reached_model']} ({pct(s['bound_rate_when_model_ran'])})",
        f"  denied: {s['n_denied_model']} by the model or its lane (rejected, fallback, timeout, error), "
        f"{s['n_denied_capacity']} for capacity (busy, cooldown), {s['n_denied_shape']} for the command's shape "
        "(unsupported, refused)",
        "  latency by outcome (ms): outcome, n, p50, p95, max",
    ]
    for key, b in s["latency_by_outcome"].items():
        lines.append(f"    {key}: n={b['n']} p50 {num(b['p50'])} p95 {num(b['p95'])} max {num(b['max'])}"
                     + ("" if b["n_timed"] == b["n"] else f" (timed {b['n_timed']})"))
    lines.append(
        f"  est. Claude output tokens avoided: {num(s['est_output_tokens_avoided'])} "
        f"({num(s['bound_out_chars'])} chars / {CHARS_PER_TOKEN}, estimate)"
    )
    if s["n_handwritten"]:
        lines.append(
            f"  est. tokens still spent on hand-written commands: {num(s['est_output_tokens_still_handwritten'])} "
            f"({num(s['handwritten_cmd_chars'])} command chars / {CHARS_PER_TOKEN}, estimate, upper bound)"
        )
    for job, c in sorted(s["by_job"].items()):
        lines.append(f"  {job}: bound {c[KIND_BOUND]}, denied {c[KIND_DENIED]}, hand-written {c[KIND_HANDWRITTEN]}")
    if s["denied_by_reason"]:
        lines.append("  denied by outcome:reason")
        for k, v in list(s["denied_by_reason"].items())[:10]:
            lines.append(f"    {k}: {v}")
    return "\n".join(lines)
