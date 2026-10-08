"""Frontier shadow (PLAN v16 GE4; R-EVL-6; owner decision OD-4 = A). OFF BY DEFAULT.

WHAT IT DOES. When the proxy's tier policy served a call on Haiku (``tier_reason ==
haiku_rewrite``), then after the Haiku reply has been relayed to the client, a background
asyncio task replays the client's UNMODIFIED original request bytes to the model the client
asked for (the "Frontier" model). The two answers are kept as a pair for a blind A/B judge
(``judge_pending``), whose verdicts land in ``verdicts.jsonl`` in the row shape
``proxy/haiku_guard.py`` reads for its D-20 ``shadow`` trigger.

SWITCH. ``LLM_ROUTER_SHADOW_FRONTIER=on`` (or ``1``/``true``/``yes``). Anything else is off:
the proxy attaches nothing, the judge makes no call, and no file is written, so with the
flag unset this module makes 0 Frontier calls. Switching it on needs the owner's OD-4 consent.

CAPS (all enforced here; OD-4 = A):

* sample rate: 100% of Haiku-rewrite turns for the first ``FULL_RATE_DAYS`` (14) days after
  the shadow first ran (``started.json``), then ``STEADY_RATE`` (2%);
* at most ``MAX_CALLS_PER_DAY`` (20) Frontier replays per UTC day;
* at most ``MAX_INPUT_TOKENS_PER_DAY`` (400k) Frontier input tokens per UTC day, summed from
  each replay's own usage (input + cache read + cache write). A replay is skipped when the
  Haiku call's input size (the same body) would take the day over the cap;
* paused when ``usage.json`` says session >= 80% or weekly >= 85%, and when the reading is
  missing, a placeholder, or older than 30 minutes (unknown quota = no shadow);
* at most one replay in flight; each replay is bounded by ``REPLAY_TIMEOUT_S`` and
  ``MAX_REPLY_BYTES``. The forwarded response is never awaited on any of this.

FILES, under ``<state dir>/shadow_frontier/``:

* ``ledger.jsonl``: one row per considered Haiku turn (sampled or skipped, with the reason,
  model ids, ``body_sha``, token counts, status, latency). NO prompt or response text;
* ``pairs/<UTC day>.jsonl``: the only text store (the request context the judge needs and the
  two answers), mode 0600, files older than ``RETENTION_DAYS`` (14) are deleted;
* ``judge_ledger.jsonl``: one row per judge call (cost, model check, outcome). NO text;
* ``verdicts.jsonl`` (``haiku_guard.shadow_verdicts_path()``): ``{ts, task_id, body_sha,
  acceptable, frontier_acceptable, cannot_judge, judge_model, cheap_label}``. NO text.

JUDGE. ``python -m llm_router.shadow_frontier judge``: isolated ``claude -p`` (Sonnet) with
``--no-session-persistence`` (a judge prompt must never be saved as a Claude Code
transcript), ``--setting-sources project,local`` (the user's settings.json, which points
``ANTHROPIC_BASE_URL`` at the local proxy, is not loaded), and ``ANTHROPIC_BASE_URL`` removed
from the child env, so judge traffic never goes through the local proxy. Same quota guard as
the replay, at most ``JUDGE_MAX_CALLS_PER_DAY`` calls per UTC day, one attempt per pair.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from llm_router import failopen, paths
from llm_router.proxy import haiku_guard
from llm_router.proxy.steps import _text_of, newest_human_text, non_system
from llm_router.proxy.translate import parse_sse_usage

ENV_FLAG = "LLM_ROUTER_SHADOW_FRONTIER"
_ON = ("1", "on", "true", "yes")

DOOR_PROXY = "proxy"
TIER_REASON = "haiku_rewrite"

MAX_CALLS_PER_DAY = 20
MAX_INPUT_TOKENS_PER_DAY = 400_000
SESSION_PAUSE_PCT = 80.0
WEEKLY_PAUSE_PCT = 85.0
QUOTA_MAX_AGE_S = 1800.0
FULL_RATE_DAYS = 14
FULL_RATE = 1.0
STEADY_RATE = 0.02
RETENTION_DAYS = 14
REPLAY_TIMEOUT_S = 300.0
MAX_REPLY_BYTES = 8 * 1024 * 1024
TEXT_CAP = 20_000
CONTEXT_CAP = 8_000

JUDGE_MODEL = "sonnet"
JUDGE_MAX_CALLS_PER_DAY = 20
JUDGE_TIMEOUT_S = 900

OUT_REPLAYED = "replayed"
OUT_REPLAY_FAILED = "replay_failed"
OUT_SKIPPED = "skipped"
_CALL_OUTCOMES = (OUT_REPLAYED, OUT_REPLAY_FAILED)

R_IN_FLIGHT = "in_flight"
R_NOT_REWRITTEN = "not_rewritten"
R_CHEAP_FAILED = "cheap_failed"
R_QUOTA_UNKNOWN = "quota_unknown"
R_QUOTA_STALE = "quota_stale"
R_QUOTA_SESSION = "quota_session"
R_QUOTA_WEEKLY = "quota_weekly"
R_CAP_CALLS = "cap_calls"
R_CAP_TOKENS = "cap_tokens"
R_SIZE_UNKNOWN = "size_unknown"
R_NOT_SAMPLED = "not_sampled"

JUDGE_BRIEF = (
    "You review two answers to the same developer request. They are labelled A and B in a "
    "random order; you do not know which model wrote either. The item gives the developer's "
    "request (and, inside an agent loop, the latest tool calls and results), then answer A and "
    "answer B. An answer may be a tool call rather than prose.\n"
    "For each answer decide: would a competent developer accept it as a correct and complete "
    "handling of the request at this point in the conversation?\n"
    "If the item does not hold enough evidence to decide, set cannot_judge to true and both "
    "acceptable fields to null.\n"
    'Output only one JSON object: {"acceptable_A": true|false|null, "acceptable_B": '
    'true|false|null, "cannot_judge": true|false}'
)


# ── switch, paths, small IO ─────────────────────────────────────────────────


def enabled() -> bool:
    return (os.environ.get("LLM_ROUTER_SHADOW_FRONTIER") or "").strip().lower() in _ON


def shadow_dir() -> Path:
    return paths.state_path("shadow_frontier")


def ledger_path() -> Path:
    return shadow_dir() / "ledger.jsonl"


def judge_ledger_path() -> Path:
    return shadow_dir() / "judge_ledger.jsonl"


def pairs_dir() -> Path:
    return shadow_dir() / "pairs"


def verdicts_path() -> Path:
    """The file ``haiku_guard`` reads, so writer and reader cannot drift apart."""
    return haiku_guard.shadow_verdicts_path()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _append_private(path: Path, row: dict) -> None:
    """Append one JSON line to a 0600 file (directory 0700)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _read_rows(path: Path) -> list[dict]:
    return haiku_guard.read_jsonl(path)


def body_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ── quota and caps ──────────────────────────────────────────────────────────


def _pct(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def quota_skip(now: float, path: Path | None = None) -> str | None:
    """Why the subscription quota forbids a Frontier call now, else None. An unreadable,
    placeholder or stale reading forbids it too: unknown quota means no shadow."""
    p = Path(path) if path is not None else paths.state_path("usage.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return R_QUOTA_UNKNOWN
    if not isinstance(data, dict) or data.get("pending") or data.get("is_fallback"):
        return R_QUOTA_UNKNOWN
    session, weekly, updated = _pct(data.get("session_pct")), _pct(data.get("weekly_pct")), _pct(data.get("updated_at"))
    if session is None or weekly is None or updated is None or updated <= 0:
        return R_QUOTA_UNKNOWN
    if now - updated > QUOTA_MAX_AGE_S:
        return R_QUOTA_STALE
    if session >= SESSION_PAUSE_PCT:
        return R_QUOTA_SESSION
    if weekly >= WEEKLY_PAUSE_PCT:
        return R_QUOTA_WEEKLY
    return None


def input_tokens(usage: Any) -> int | None:
    """Prompt-side tokens of one call: input + cache read + cache write. None if unknown."""
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    vals = [usage.get(k) for k in keys]
    if not any(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return None
    return sum(v for v in vals if isinstance(v, int) and not isinstance(v, bool))


def day_spend(day: str) -> tuple[int, int]:
    """(Frontier calls, Frontier input tokens) the shadow spent on ``day``. A failed replay
    counts as a call, and its input size counts at the estimate it was admitted with."""
    calls = tokens = 0
    for r in _read_rows(ledger_path()):
        if r.get("day") != day or r.get("outcome") not in _CALL_OUTCOMES:
            continue
        calls += 1
        used = r.get("input_tokens")
        est = r.get("est_input_tokens")
        tokens += used if isinstance(used, int) else (est if isinstance(est, int) else 0)
    return calls, tokens


def sample_rate(now: float) -> float:
    """OD-4 = A: every Haiku turn for the first 14 days after the shadow first ran, then 2%."""
    marker = shadow_dir() / "started.json"
    started = None
    try:
        started = _pct(json.loads(marker.read_text(encoding="utf-8")).get("ts"))
    except (OSError, ValueError, AttributeError):
        started = None
    if started is None:
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(json.dumps({"ts": now}) + "\n", encoding="utf-8")
        started = now
    return FULL_RATE if now - started < FULL_RATE_DAYS * 86400.0 else STEADY_RATE


# ── reply and request rendering (pair store only) ───────────────────────────


def reply_text(buf: bytes, ctype: str) -> tuple[str, dict]:
    """(text, usage) of one Anthropic reply, SSE or JSON. Tool calls are rendered as
    ``[tool_use NAME] {input}`` so the judge sees what the answer did."""
    parts: list[str] = []
    usage: dict = {}
    if "event-stream" in (ctype or ""):
        usage, _, _ = parse_sse_usage(buf)
        tool_json: dict[int, list[str]] = {}
        for line in buf.decode("utf-8", "replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                d = json.loads(line[6:])
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("type") == "content_block_start":
                block = d.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_json[d.get("index", -1)] = []
                    parts.append(f"[tool_use {block.get('name')}] ")
                elif block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
            elif d.get("type") == "content_block_delta":
                delta = d.get("delta") or {}
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text") or "")
                elif delta.get("type") == "input_json_delta":
                    parts.append(delta.get("partial_json") or "")
    else:
        try:
            data = json.loads(buf or b"{}")
        except ValueError:
            data = {}
        if isinstance(data, dict):
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            for block in data.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool_use {block.get('name')}] {json.dumps(block.get('input'))}")
    return "".join(parts)[:TEXT_CAP], usage


def _render_block(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text":
        return block.get("text") or ""
    if kind == "tool_use":
        return f"[tool_use {block.get('name')}] {json.dumps(block.get('input'))[:500]}"
    if kind == "tool_result":
        inner = block.get("content")
        return "[tool_result] " + (inner if isinstance(inner, str) else _text_of(inner))[:1000]
    return f"[{kind}]"


def request_context(body: Any) -> str:
    """What the judge needs to know about the request: the newest human prompt, plus the
    messages after it (an agent loop's latest tool calls and results), capped."""
    if not isinstance(body, dict):
        return ""
    ask = newest_human_text(body)
    msgs = non_system(body.get("messages") or [])
    tail: list[str] = []
    for m in reversed(msgs):
        content = m.get("content")
        blocks = content if isinstance(content, list) else [content]
        if m.get("role") == "user" and any(isinstance(b, dict) and b.get("type") == "text" for b in blocks) \
                and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
            break
        if m.get("role") == "user" and isinstance(content, str):
            break
        tail.append(f"{m.get('role')}: " + "\n".join(_render_block(b) for b in blocks))
    text = ask[-CONTEXT_CAP:]
    if tail:
        text += "\n\n[after the request]\n" + "\n".join(reversed(tail))[-CONTEXT_CAP:]
    return text


# ── the sampler ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Replay:
    status: int
    body: bytes
    ctype: str


ReplayFn = Callable[[bytes], Awaitable[Replay]]


class ReplyTooLarge(Exception):
    pass


async def replay_http(client, url: str, headers: dict, content: bytes, *,
                      max_bytes: int = MAX_REPLY_BYTES, timeout_s: float = REPLAY_TIMEOUT_S) -> Replay:
    """Send ``content`` (the client's original bytes) straight to ``url`` (Anthropic), not
    through the local proxy, reading at most ``max_bytes`` of the reply."""
    req = client.build_request("POST", url, headers=headers, content=content, timeout=timeout_s)
    resp = await client.send(req, stream=True)
    try:
        buf = bytearray()
        async for chunk in resp.aiter_raw():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ReplyTooLarge(f"reply over {max_bytes} bytes")
        return Replay(resp.status_code, bytes(buf), resp.headers.get("content-type", ""))
    finally:
        await resp.aclose()


class FrontierShadow:
    """One per proxy process. ``maybe_sample`` is called after the cheap reply is complete;
    it only schedules a task and returns, so the forwarded response never waits on it."""

    def __init__(self, *, rng: Callable[[], float] = random.random, clock: Callable[[], float] = time.time,
                 usage_path: Path | None = None):
        self._rng = rng
        self._clock = clock
        self._usage_path = usage_path
        self._busy = False
        self._tasks: set[asyncio.Task] = set()

    def maybe_sample(self, request_body: bytes, served_model: str | None, requested_model: str | None,
                     door: str, task_id: str | None, *, replay: ReplayFn, cheap_status: int = 200,
                     cheap_reply: bytes = b"", cheap_ctype: str = "", cheap_usage: dict | None = None
                     ) -> asyncio.Task | None:
        """Schedule the background job for one cheap-served call. Never raises, never awaits."""
        owns = False
        try:
            if not enabled():
                return None
            pre = R_IN_FLIGHT if self._busy else None
            if pre is None:
                self._busy = owns = True
            job = dict(raw=request_body, served=served_model, requested=requested_model, door=door,
                       task_id=task_id or uuid.uuid4().hex, replay=replay, cheap_status=cheap_status,
                       cheap_reply=cheap_reply, cheap_ctype=cheap_ctype, cheap_usage=cheap_usage)
            task = asyncio.get_running_loop().create_task(self._run(job, pre, owns))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return task
        except Exception as exc:  # noqa: BLE001 - the shadow must never cost the call
            if owns:
                self._busy = False
            failopen.record("LR-FO-SHADOW-FRONTIER-START", exc)
            return None

    async def drain(self) -> None:
        """Wait for every scheduled job (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.drain()

    def _gate(self, now: float, day: str, job: dict, est: int | None) -> str | None:
        if not job["served"] or job["served"] == job["requested"]:
            return R_NOT_REWRITTEN
        if job["cheap_status"] != 200:
            return R_CHEAP_FAILED
        quota = quota_skip(now, self._usage_path)
        if quota is not None:
            return quota
        calls, tokens = day_spend(day)
        if calls >= MAX_CALLS_PER_DAY:
            return R_CAP_CALLS
        if est is None:
            return R_SIZE_UNKNOWN
        if tokens + est > MAX_INPUT_TOKENS_PER_DAY:
            return R_CAP_TOKENS
        if self._rng() >= sample_rate(now):
            return R_NOT_SAMPLED
        return None

    async def _run(self, job: dict, pre: str | None, owns: bool) -> None:
        try:
            now = self._clock()
            est = input_tokens(job["cheap_usage"])
            base = {"ts": round(now, 3), "day": _day(now), "task_id": job["task_id"], "door": job["door"],
                    "served": job["served"], "requested": job["requested"], "body_sha": body_sha(job["raw"]),
                    "est_input_tokens": est}
            reason = pre or await asyncio.to_thread(self._gate, now, base["day"], job, est)
            if reason is not None:
                await asyncio.to_thread(_append_private, ledger_path(),
                                        dict(base, outcome=OUT_SKIPPED, reason=reason))
                return
            t0 = time.monotonic()
            try:
                rep = await asyncio.wait_for(job["replay"](job["raw"]), REPLAY_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed replay is a ledger row, not an error
                await asyncio.to_thread(_append_private, ledger_path(), dict(
                    base, outcome=OUT_REPLAY_FAILED, reason=type(exc).__name__, frontier_status=None,
                    input_tokens=None, output_tokens=None, latency_s=round(time.monotonic() - t0, 3)))
                return
            await asyncio.to_thread(self._record, job, base, rep, round(time.monotonic() - t0, 3))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - background evidence must never break the proxy
            failopen.record("LR-FO-SHADOW-FRONTIER-RUN", exc)
        finally:
            if owns:
                self._busy = False

    def _record(self, job: dict, base: dict, rep: Replay, latency_s: float) -> None:
        text, usage = reply_text(rep.body, rep.ctype) if rep.status == 200 else ("", {})
        out = usage.get("output_tokens") if isinstance(usage, dict) else None
        ok = rep.status == 200
        _append_private(ledger_path(), dict(
            base, outcome=OUT_REPLAYED if ok else OUT_REPLAY_FAILED, reason=None if ok else "status",
            frontier_status=rep.status, input_tokens=input_tokens(usage) if ok else None,
            output_tokens=out if isinstance(out, int) else None, latency_s=latency_s))
        if not ok:
            return
        try:
            body = json.loads(job["raw"])
        except ValueError:
            body = None
        cheap_text, _ = reply_text(job["cheap_reply"], job["cheap_ctype"])
        _append_private(pairs_dir() / f"{base['day']}.jsonl", {
            "ts": base["ts"], "task_id": base["task_id"], "door": base["door"], "served": base["served"],
            "requested": base["requested"], "body_sha": base["body_sha"],
            "request_context": request_context(body), "cheap_response": cheap_text,
            "frontier_response": text})
        prune_pairs(base["ts"])


def prune_pairs(now: float) -> list[str]:
    """Delete pair files (the text store) older than ``RETENTION_DAYS``. Returns the names."""
    cutoff = (datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    gone = []
    for f in sorted(pairs_dir().glob("*.jsonl")) if pairs_dir().is_dir() else []:
        if f.stem < cutoff:
            f.unlink(missing_ok=True)
            gone.append(f.name)
    return gone


# ── blind A/B judge ─────────────────────────────────────────────────────────


def judge_env(environ: dict | None = None) -> dict:
    """The judge child's env: ANTHROPIC_BASE_URL removed, so it never reaches the local proxy."""
    env = dict(os.environ if environ is None else environ)
    env.pop("ANTHROPIC_BASE_URL", None)
    env["LLM_ROUTER_SESSION_KIND"] = "research"
    return env


def judge_cmd(brief: str, item: str) -> list[str]:
    return ["claude", "-p", "--no-session-persistence", "--model", JUDGE_MODEL,
            "--setting-sources", "project,local", "--strict-mcp-config", "--output-format", "json",
            "--max-turns", "1", "--tools", "", "--system-prompt", brief, "--", item]


def claude_judge(brief: str, item: str) -> dict:
    """One isolated ``claude -p`` judge call: ``{result, cost_usd, model_ok}``."""
    cwd = shadow_dir() / "judge_cwd"
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    proc = subprocess.run(judge_cmd(brief, item), cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=JUDGE_TIMEOUT_S, env=judge_env())
    if proc.returncode:
        raise RuntimeError(f"claude exit {proc.returncode}")
    data = json.loads(proc.stdout)
    used = data.get("modelUsage") or {}
    return {"result": data.get("result") or "", "cost_usd": float(data.get("total_cost_usd") or 0),
            "model_ok": bool(used) and all(JUDGE_MODEL in k.lower() for k in used)}


def judge_item(pair: dict, cheap_first: bool) -> str:
    a, b = ((pair.get("cheap_response"), pair.get("frontier_response")) if cheap_first
            else (pair.get("frontier_response"), pair.get("cheap_response")))
    return (f"DEVELOPER REQUEST:\n{pair.get('request_context') or ''}\n\n"
            f"ANSWER A:\n{a or ''}\n\nANSWER B:\n{b or ''}")


def parse_verdict(text: str) -> dict | None:
    """``{acceptable_A, acceptable_B, cannot_judge}`` from the judge's reply, else None."""
    text = (text or "").strip()
    i = text.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("cannot_judge") is True:
        return {"acceptable_A": None, "acceptable_B": None, "cannot_judge": True}
    a, b = obj.get("acceptable_A"), obj.get("acceptable_B")
    if not isinstance(a, bool) or not isinstance(b, bool):
        return None
    return {"acceptable_A": a, "acceptable_B": b, "cannot_judge": False}


def judge_pending(*, run: Callable[[str, str], dict] = claude_judge, rng: Callable[[], float] = random.random,
                  clock: Callable[[], float] = time.time, usage_path: Path | None = None) -> dict:
    """Judge every stored pair not yet attempted, within the caps. Returns a count summary."""
    summary = {"status": "ok", "judged": 0, "verdicts": 0, "cannot_judge": 0, "failed": 0}
    if not enabled():
        return dict(summary, status="off")
    now = clock()
    quota = quota_skip(now, usage_path)
    if quota is not None:
        return dict(summary, status=quota)
    prune_pairs(now)
    day = _day(now)
    attempts = _read_rows(judge_ledger_path())
    tried = {r.get("task_id") for r in attempts}
    calls_today = sum(1 for r in attempts if r.get("day") == day)
    pairs = []
    for f in sorted(pairs_dir().glob("*.jsonl")) if pairs_dir().is_dir() else []:
        pairs.extend(_read_rows(f))
    for pair in sorted(pairs, key=lambda p: p.get("ts") or 0):
        if pair.get("task_id") in tried:
            continue
        if calls_today >= JUDGE_MAX_CALLS_PER_DAY:
            summary["status"] = R_CAP_CALLS
            break
        cheap_first = rng() < 0.5
        row = {"ts": round(clock(), 3), "day": day, "task_id": pair.get("task_id"),
               "body_sha": pair.get("body_sha"), "judge_model": JUDGE_MODEL}
        calls_today += 1
        tried.add(pair.get("task_id"))
        try:
            res = run(JUDGE_BRIEF, judge_item(pair, cheap_first))
        except Exception as exc:  # noqa: BLE001 - one failed judge call is a ledger row
            _append_private(judge_ledger_path(), dict(row, outcome="error", reason=type(exc).__name__,
                                                      cost_usd=None, model_ok=None))
            summary["failed"] += 1
            continue
        summary["judged"] += 1
        verdict = parse_verdict(res.get("result", "")) if res.get("model_ok") else None
        outcome = "model_mismatch" if not res.get("model_ok") else ("verdict" if verdict else "unparsed")
        _append_private(judge_ledger_path(), dict(row, outcome=outcome, cost_usd=res.get("cost_usd"),
                                                  model_ok=res.get("model_ok")))
        if verdict is None:
            summary["failed"] += 1
            continue
        cheap_key, frontier_key = ("acceptable_A", "acceptable_B") if cheap_first else ("acceptable_B", "acceptable_A")
        _append_private(verdicts_path(), {
            "ts": pair.get("ts"), "judged_at": row["ts"], "task_id": pair.get("task_id"),
            "body_sha": pair.get("body_sha"), "served": pair.get("served"), "requested": pair.get("requested"),
            "acceptable": verdict[cheap_key], "frontier_acceptable": verdict[frontier_key],
            "cannot_judge": verdict["cannot_judge"], "judge_model": JUDGE_MODEL,
            "cheap_label": "A" if cheap_first else "B"})
        summary["verdicts"] += 1
        summary["cannot_judge"] += 1 if verdict["cannot_judge"] else 0
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.shadow_frontier",
                                 description="GE4 Frontier shadow: blind A/B judge over stored pairs.")
    ap.add_argument("command", choices=["judge"])
    ap.parse_args(argv)
    print(json.dumps(judge_pending(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
