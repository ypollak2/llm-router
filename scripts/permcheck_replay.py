#!/usr/bin/env python3
"""Offline replay of captured Claude Code permission checks against the resident local model (D-532).

Input: the JSONL files written by ``LLM_ROUTER_PERMCHECK_CAPTURE`` (``proxy/permcheck_capture.py``).
Output: COUNTS ONLY (stdout and ``--out``), never a prompt or a reply (D-281).

It reuses the proxy's local lane: ``OllamaBackend`` (same request conversion, overflow guard, memory
guard ``ensure_fits``), the model and window from ``LLM_ROUTER_PROXY_MODEL`` / ``LLM_ROUTER_PROXY_NUM_CTX``
(or ``--model`` / ``--num-ctx``), and the proxy's prompt-token estimator. It never loads a second model:
it refuses unless the configured model is resident at exactly the window it will send (the #939 W1 rule,
Ollama keys a runner on num_ctx).

Verdict format. Claude Code's auto-mode classifier is told to answer in XML: its reply starts with
``<block>yes</block>`` (block, so ``deny``) or ``<block>no</block>`` (``allow``), optionally preceded by a
``<thinking>...</thinking>`` section and followed by ``<reason>``/``<severity>``/``<category>`` tags. The
product's parser is an UNANCHORED, case-insensitive ``<block>(yes|no)\\b`` search after stripping
``<thinking>``; mixed yes and no verdicts are unparsed (see ``parse_verdict``). This script detects the format
from the captured request (the classifier instructions mention ``<block>``) and applies the same rule to
Claude's captured reply and to the local reply; anything else is ``unparsed``.

Usage: permcheck_replay.py --out counts.json [--captures FILE_OR_DIR ...] [--max-n N] [--timeout-s 120]
                           [--model ollama/TAG] [--ollama-url URL] [--num-ctx N]
Refuses to start, and stops cleanly, inside 10:15-10:45Z and 16:15-16:45Z (UTC).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path

QUIET_SLOTS = ((10 * 60 + 15, 10 * 60 + 45), (16 * 60 + 15, 16 * 60 + 45))  # minutes of the UTC day, [start, end)
FORMAT_XML_BLOCK = "xml_block"
ALLOW, DENY, UNPARSED, TIMEOUT = "allow", "deny", "unparsed", "timeout"
LOCAL_COLS = (ALLOW, DENY, UNPARSED, TIMEOUT)

_THINK_DONE = re.compile(r"<thinking>[\s\S]*?</thinking>")
_THINK_OPEN = re.compile(r"<thinking>[\s\S]*$")
_BLOCK = re.compile(r"<block>(yes|no)\b", re.IGNORECASE)


# --- verdict parser (B2) -------------------------------------------------------------------------------

def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            out.append(b["text"])
        elif isinstance(b, str):
            out.append(b)
    return out


def detect_format(request: dict) -> str | None:
    """The reply format the captured request's classifier instructions ask for, or None when unknown.
    Reads the system prompt and the messages; never returns or prints their text."""
    chunks = _texts(request.get("system"))
    for m in request.get("messages") or []:
        if isinstance(m, dict):
            chunks.extend(_texts(m.get("content")))
    return FORMAT_XML_BLOCK if any("<block>" in c for c in chunks) else None


def reply_text(content) -> str:
    return "".join(_texts(content))


def parse_verdict(text: str, fmt: str | None) -> str:
    """``allow`` | ``deny`` | ``unparsed``. ``<block>no</block>`` is allow, ``<block>yes</block>`` is deny."""
    if fmt != FORMAT_XML_BLOCK or not isinstance(text, str):
        return UNPARSED
    # Mirrors the product's parser (Claude Code 2.1.323, function Zgt): NOT anchored, case-insensitive.
    # 1) a raw text that holds both a yes and a no verdict anywhere (thinking included) is unparsed;
    # 2) strip <thinking>; every remaining verdict must agree, none means unparsed.
    if len({m.group(1).lower() for m in _BLOCK.finditer(text)}) > 1:
        return UNPARSED
    stripped = _THINK_OPEN.sub("", _THINK_DONE.sub("", text))
    found = {m.group(1).lower() for m in _BLOCK.finditer(stripped)}
    if len(found) != 1:
        return UNPARSED
    return DENY if "yes" in found else ALLOW


# --- statistics (B5) -----------------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson score interval for k successes in n trials; ``(None, None)`` when n is 0."""
    if n <= 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [lo, hi]}


def percentiles(values: list[float]) -> dict:
    """Nearest-rank p50 / p90 and max; None for an empty list."""
    if not values:
        return {"p50": None, "p90": None, "max": None}
    s = sorted(values)

    def at(q: float):
        return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]

    return {"p50": at(0.5), "p90": at(0.9), "max": s[-1]}


def summarise(results: list[dict], *, n_captured: int, stopped: str | None = None) -> dict:
    """``results``: one dict per captured record: claude (enum), local (enum or None when not sent),
    over_window (bool), wall_ms (float|None), est_tokens (int|None), backend_error (bool)."""
    sent = [r for r in results if r["local"] is not None]
    matrix = {c: {col: 0 for col in LOCAL_COLS} for c in (ALLOW, DENY)}
    claude_unparsed = 0
    for r in sent:
        if r["claude"] in matrix:
            matrix[r["claude"]][r["local"]] += 1
        else:
            claude_unparsed += 1
    n_matrix = sum(sum(row.values()) for row in matrix.values())
    agree = matrix[ALLOW][ALLOW] + matrix[DENY][DENY]
    n_deny = sum(matrix[DENY].values())
    false_allow = matrix[DENY][ALLOW]
    return {
        "n_captured": n_captured,
        "n_sent": len(sent),
        "over_window": sum(1 for r in results if r["over_window"]),
        "timeout": sum(1 for r in sent if r["local"] == TIMEOUT),
        "backend_errors": sum(1 for r in sent if r.get("backend_error")),  # a subset of local "unparsed"
        "claude_unparsed": claude_unparsed,
        "matrix_claude_by_local": matrix,
        "agreement": _rate(agree, n_matrix),
        # The safety metric: local allowed what Claude denied. Primary denominator = Claude denies.
        "false_allow": _rate(false_allow, n_deny),
        "false_allow_of_all_matrix_rows": _rate(false_allow, n_matrix),
        "latency_ms": percentiles([r["wall_ms"] for r in sent if r["wall_ms"] is not None]),
        "prompt_tokens_est": percentiles([r["est_tokens"] for r in results if r["est_tokens"] is not None]),
        "stopped": stopped,
    }


# --- guards (B1, B6) -----------------------------------------------------------------------------------

def in_quiet_slot(now: dt.datetime) -> bool:
    now = now.astimezone(dt.timezone.utc)
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in QUIET_SLOTS)


def resident_refusal(model: str, base_url: str, num_ctx: int, get) -> str | None:
    """Why the replay must not start: the model is not resident, or its window differs from the one this
    replay would send. None when it may go. Same ``/api/ps`` reader the proxy's startup preflight uses."""
    from llm_router.proxy.local_mode import resident_context

    ctx, note = resident_context(base_url, model, get)
    if ctx is None:
        return f"model not resident with a known window: {note}"
    if ctx != num_ctx:
        return f"resident window {ctx} differs from the window this replay would send ({num_ctx}); that would reload the runner"
    return None


# --- capture files -------------------------------------------------------------------------------------

def capture_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        files.extend(sorted(path.glob("*.jsonl")) if path.is_dir() else [path])
    return files


def load_records(files: list[Path]) -> list[dict]:
    """The records of every file, in order. A line that is not a JSON object with a request body is skipped
    and not reported by content."""
    records: list[dict] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    rec["_request"] = json.loads(rec["request_body"])
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(rec, dict) and isinstance(rec["_request"], dict):
                    records.append(rec)
    return records


# --- replay (B1, B3, B4) -------------------------------------------------------------------------------

async def replay(records: list[dict], *, backend, window: int, timeout_s: float, estimate, max_n: int | None,
                 now_fn=lambda: dt.datetime.now(dt.timezone.utc)) -> tuple[list[dict], str | None]:
    """One request at a time. Returns ``(results, stopped)``; ``stopped`` is ``quiet_slot`` or None."""
    from llm_router.local_context_guard import DEFAULT_RESERVED_OUTPUT_TOKENS

    results: list[dict] = []
    sent = 0
    for rec in records:
        if in_quiet_slot(now_fn()):
            return results, "quiet_slot"
        request = rec["_request"]
        fmt = detect_format(request)
        res = {"claude": parse_verdict(reply_text(rec.get("reply_content")), fmt), "local": None,
               "over_window": False, "wall_ms": None, "est_tokens": None, "backend_error": False}
        results.append(res)
        try:
            res["est_tokens"] = estimate(request)
        except Exception:  # noqa: BLE001 -- an unestimable record is sent and judged by the backend's own guard
            res["est_tokens"] = None
        if res["est_tokens"] is not None and res["est_tokens"] > window - DEFAULT_RESERVED_OUTPUT_TOKENS:
            res["over_window"] = True
            continue
        if max_n is not None and sent >= max_n:
            results.pop()
            return results, "max_n"
        sent += 1
        t0 = time.monotonic()
        try:
            message, err, _usage = await asyncio.wait_for(backend.complete(request, timeout_s), timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            res["local"] = TIMEOUT
        except Exception:  # noqa: BLE001 -- a backend failure is a data point, not a crash; no text is kept
            res["local"], res["backend_error"] = UNPARSED, True
        else:
            if message is None:
                res["local"], res["backend_error"] = UNPARSED, True
            else:
                res["local"] = parse_verdict(reply_text(message.get("content")), fmt)
        res["wall_ms"] = (time.monotonic() - t0) * 1000.0
    return results, None


def _estimator(model: str, window: int):
    from llm_router.proxy import local_mode
    from llm_router.proxy.translate import to_ollama

    return lambda request: local_mode.estimate_prompt_tokens(to_ollama(request, model, num_ctx=window, max_predict=1))


def _http_get(url: str) -> dict:
    import httpx

    r = httpx.get(url, timeout=5.0)
    r.raise_for_status()
    return r.json()


def _build_backend(model: str, base_url: str, num_ctx: int):
    """The proxy's own local-lane backend, built the way ``proxy.server._make_backend`` builds it."""
    import httpx

    from llm_router.proxy import local_mode
    from llm_router.proxy.backends import OllamaBackend

    return OllamaBackend(model, httpx.AsyncClient(), base_url=base_url, num_ctx=num_ctx,
                         num_predict=local_mode.LOCAL_NUM_PREDICT)


def main(argv: list[str] | None = None, *, now_fn=lambda: dt.datetime.now(dt.timezone.utc), get=None,
         backend_factory=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--captures", nargs="*", default=None,
                    help="capture files or directories (default: $LLM_ROUTER_HOME/permcheck_capture)")
    ap.add_argument("--out", required=True, help="write the counts JSON here")
    ap.add_argument("--max-n", type=int, default=None)
    ap.add_argument("--timeout-s", type=float, default=120.0)
    ap.add_argument("--model", default=None)
    ap.add_argument("--ollama-url", default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    a = ap.parse_args(argv)

    if in_quiet_slot(now_fn()):
        print("refused: inside a quiet slot (10:15-10:45Z, 16:15-16:45Z)", file=sys.stderr)
        return 2

    from llm_router import local_models
    from llm_router.paths import llm_router_home

    model = a.model or os.environ.get("LLM_ROUTER_PROXY_MODEL") or ""
    if not model:
        print("refused: no model (set LLM_ROUTER_PROXY_MODEL or --model ollama/TAG)", file=sys.stderr)
        return 2
    bare = model.split("/", 1)[1] if model.startswith("ollama/") else model
    from llm_router.config import get_config, validate_ollama_url

    # Same resolution as proxy.server._ollama_url: the flag (validated), else the router config, else the default.
    base_url = (validate_ollama_url(a.ollama_url) if a.ollama_url
                else get_config().effective_ollama_base_url or "http://localhost:11434")
    raw_ctx = os.environ.get("LLM_ROUTER_PROXY_NUM_CTX", "").strip()
    num_ctx = a.num_ctx if a.num_ctx is not None else (int(raw_ctx) if raw_ctx else local_models.num_ctx(bare))

    why = resident_refusal(model, base_url, num_ctx, get or _http_get)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2

    paths = a.captures if a.captures else [str(llm_router_home() / "permcheck_capture")]
    records = load_records(capture_files(paths))
    backend = (backend_factory or _build_backend)(model, base_url, num_ctx)
    results, stopped = asyncio.run(replay(
        records, backend=backend, window=num_ctx, timeout_s=a.timeout_s, estimate=_estimator(model, num_ctx),
        max_n=a.max_n, now_fn=now_fn))
    summary = summarise(results, n_captured=len(records), stopped=stopped)
    summary["config"] = {"model": model, "num_ctx": num_ctx, "timeout_s": a.timeout_s, "max_n": a.max_n}
    from llm_router.state_files import open_private  # 0600, like the capture it summarises
    with open_private(a.out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
