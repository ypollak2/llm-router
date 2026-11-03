"""The text-free ledger of the text-job-bind hook, and its report.

One row per ``git commit`` or ``gh pr create`` command the hook sees: those that used the
placeholder (``bound`` when the local job's checked text was put in the command, ``denied``
when it was not) and those the model wrote by hand (``handwritten``, the denominator of
adoption). A row holds sizes and a reason code, never the text of the diff, the message, the
body or the command.

Rows go to ``text_job_binds.jsonl`` in the state directory through ``capped_log`` (two
generations of 2 MiB, 0600, one append, no lock). Writing is best-effort and never raises: a
failed row must not change what the hook says about a command.

``python -m llm_router.text_job_bind report`` prints the adoption and savings figures from
these rows. The token figures are estimates: characters divided by 4.
"""

from __future__ import annotations

import json
import statistics
import time
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


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summarize(rows: list[dict]) -> dict:
    """The report as a dict. Every count carries its own N so nothing is a bare percentage."""
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
    lat = [float(r["latency_ms"]) for r in bound if isinstance(r.get("latency_ms"), (int, float))]
    out_chars = sum(int(r.get("out_chars") or 0) for r in bound)
    hand_chars = sum(int(r.get("cmd_chars") or 0) for r in hand)
    return {
        "n_rows": len(rows),
        "n_commit_pr_commands": total,
        "n_placeholder": used,
        "n_bound": len(bound),
        "n_denied": len(denied),
        "n_handwritten": len(hand),
        # Without hand-written rows there is no denominator: a hook registered with an `if` filter (the installer's
        # default) never sees a command that lacks the placeholder, so 100% would be an artefact, not a measurement.
        "adoption": (used / total) if (total and hand) else None,
        "denominator_known": bool(hand),
        "bound_rate": (len(bound) / used) if used else None,
        "by_job": by_job,
        "denied_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "bound_latency_ms_p50": statistics.median(lat) if lat else None,
        "bound_latency_ms_p95": _pct(lat, 0.95),
        "bound_out_chars": out_chars,
        "est_output_tokens_avoided": out_chars // CHARS_PER_TOKEN,
        "handwritten_cmd_chars": hand_chars,
        "est_output_tokens_still_handwritten": hand_chars // CHARS_PER_TOKEN,
        "since": min((r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))), default=None),
        "until": max((r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))), default=None),
    }


def render(summary: dict) -> str:
    s = summary

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x * 100:.0f}%"

    def num(x: float | None) -> str:
        return "n/a" if x is None else f"{x:,.0f}"

    lines = [
        f"text-job-bind ledger: {s['n_rows']} rows, {s['n_commit_pr_commands']} git commit / gh pr create commands",
        f"  placeholder used : {s['n_placeholder']} of {s['n_commit_pr_commands']} ({pct(s['adoption'])}); "
        f"hand-written: {s['n_handwritten']}",
    ]
    if not s["denominator_known"] and s["n_placeholder"]:
        lines.append("  adoption is n/a: no hand-written rows, because the hook is registered with an `if` filter and never "
                     "sees a command without the placeholder; take the denominator from the outcome audit (transcripts)")
    lines += [
        f"  bound (local text in the command): {s['n_bound']} of {s['n_placeholder']} placeholder uses "
        f"({pct(s['bound_rate'])}); denied: {s['n_denied']}",
        f"  bound latency ms : p50 {num(s['bound_latency_ms_p50'])}, p95 {num(s['bound_latency_ms_p95'])} "
        f"(n={s['n_bound']})",
        f"  est. Claude output tokens avoided: {num(s['est_output_tokens_avoided'])} "
        f"({num(s['bound_out_chars'])} chars / {CHARS_PER_TOKEN}, estimate)",
    ]
    if s["denominator_known"]:
        lines.append(
            f"  est. tokens still spent on hand-written commands: {num(s['est_output_tokens_still_handwritten'])} "
            f"({num(s['handwritten_cmd_chars'])} command chars / {CHARS_PER_TOKEN}, estimate, upper bound)"
        )
    for job, c in sorted(s["by_job"].items()):
        lines.append(
            f"  {job}: bound {c[KIND_BOUND]}, denied {c[KIND_DENIED]}, hand-written {c[KIND_HANDWRITTEN]}"
        )
    if s["denied_by_reason"]:
        lines.append("  denied by outcome:reason")
        for k, v in list(s["denied_by_reason"].items())[:8]:
            lines.append(f"    {k}: {v}")
    return "\n".join(lines)
