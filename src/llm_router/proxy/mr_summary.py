"""P9: map-reduce summarizer for a local-bound session whose history is over the local window.

Promoted from the v16 research code (coord/local-levers/mr/: mr_prompt.py partition / units_of /
block_texts, mr_map.py map prompt, validity check, extractive fallback and 300-est summary ceiling),
measured under PREREG-MR-20261109. Changed from the research: est_tokens is the repo's one formula
(llm_router.token_estimate, the same digit/wide/other formula the research copied), the model call is an
injected coroutine (the proxy passes the live backend, so the map call carries the live model and num_ctx),
and there is no ladder or pinned-paths block. The chunk store (P10, LLM_ROUTER_CHUNK_STORE, chunk_store.py) is an optional injected cache.

Flag LLM_ROUTER_PROXY_MR, default off: while off nothing here runs. On, ``shorten`` closes the OLDEST
chunks (~CHUNK_TARGET est each, whole tool_use+tool_result units, never one of the last W_CLOSE units),
runs one synchronous map call per chunk and replaces those turns with the summaries, appended to the first
user message. Any failure, or a result that still does not fit, returns the ORIGINAL body and says why:
the caller then behaves exactly as without the flag (the prompt cap sends the turn to Claude). Nothing is
ever silently truncated.

Ledger (``MRResult.row``, counts and seconds only, never text): chunks, map_calls, tokens_before,
tokens_after, map_wall_s, outcome.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from llm_router.token_estimate import est_tokens

#: Env-registered parameters LLM_ROUTER_MR_CHUNK_TARGET / _CLOSE_THRESHOLD / _MAP_NUM_PREDICT (defaults from PLAN-SUMMARY-SPEED-256K-20261110 section 4, row P9).
DEFAULT_CHUNK_TARGET = 8_000       # est tokens of map input per chunk
DEFAULT_CLOSE_THRESHOLD = 36_000   # est tokens the verbatim window is brought down to
DEFAULT_MAP_NUM_PREDICT = 450      # body clipped to BODY_EST afterwards
W_CLOSE = 6                        # a chunk must lie wholly outside the last 6 units
SUMMARY_CEIL = 300                 # est, wrapper included (PREREG-MR 2.4, AMEND3)
BODY_EST = 270
BLOCK_CHARS = 6_000                # one block longer than this is cut to head 3,000 + marker + tail 3,000
INPUT_EST = 24_000                 # hard cap on the whole map input
MAP_TIMEOUT_S = 120.0
MAP_TRIES = 2
PROMPT_VERSION = "mr-map-v1"       # P10: part of every chunk-store key; bump on any change that alters map replies

OUTCOME_SUMMARIZED = "summarized"
OUTCOME_NO_CHUNKS = "no_closable_chunks"
OUTCOME_CANNOT_FIT = "cannot_fit"
OUTCOME_MAP_FAILED = "map_failed"
OUTCOME_STILL_OVER = "still_over"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_WASTED = "wasted"  # summarized, but the turn was then forwarded to Claude (row carries wasted_reason)


_CODE_HASH: str | None = None


def key_salt() -> str:
    """Everything besides prompt/model/num_predict that shapes a stored summary: the clip constants (read now,
    so a patched value changes the key) and a hash of the render / validity code, so an edit to either misses
    old entries without a hand-bumped version."""
    global _CODE_HASH
    if _CODE_HASH is None:
        import hashlib
        import inspect
        try:
            src = "".join(inspect.getsource(f) for f in (render_message, _cut_block, _cut_by_est, _result_text,
                                                         _head_by_est, _tail_by_est, valid_summary, _sections,
                                                         map_prompt, est_tokens))
        except (OSError, TypeError):
            src = "nosource"
        _CODE_HASH = hashlib.sha256(src.encode("utf-8", "surrogatepass")).hexdigest()
    return "%d|%d|%d|%d|%r|%r|%s" % (BODY_EST, BLOCK_CHARS, INPUT_EST, SUMMARY_CEIL, BLOCK_MARK, INPUT_MARK,
                                     _CODE_HASH)


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_PROXY_MR", "").strip().lower() in ("1", "true", "yes", "on")


def _positive(raw: str, default: int) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def chunk_target() -> int:
    return _positive(os.environ.get("LLM_ROUTER_MR_CHUNK_TARGET", ""), DEFAULT_CHUNK_TARGET)


def close_threshold() -> int:
    return _positive(os.environ.get("LLM_ROUTER_MR_CLOSE_THRESHOLD", ""), DEFAULT_CLOSE_THRESHOLD)


def map_num_predict() -> int:
    return _positive(os.environ.get("LLM_ROUTER_MR_MAP_NUM_PREDICT", ""), DEFAULT_MAP_NUM_PREDICT)


# ------------------------------------------------------------------ chunker (mr_prompt.units_of / partition)
def _blocks(m: dict) -> list:
    return m["content"] if isinstance(m.get("content"), list) else []


def _has_use(m: dict) -> bool:
    return m.get("role") == "assistant" and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in _blocks(m))


def _has_result(m: dict) -> bool:
    return m.get("role") == "user" and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(m))


def units_of(messages: list[dict]) -> list[tuple[int, int]]:
    """[(start, end)) spans: one message per unit, except an assistant tool_use message fused with the user
    message carrying its tool_results, so a chunk never splits a tool call from its result."""
    out, i, n = [], 0, len(messages)
    while i < n:
        if _has_use(messages[i]) and i + 1 < n and _has_result(messages[i + 1]):
            out.append((i, i + 2))
            i += 2
        else:
            out.append((i, i + 1))
            i += 1
    return out


def partition(uests: list[int], target: int) -> list[tuple[int, int, int]]:
    """Greedy partition of units into chunks (first unit, last unit, est sum). A unit joins the current chunk
    unless the chunk is non-empty AND est + est(u) > target; a unit larger than the target is alone."""
    chunks: list[tuple[int, int, int]] = []
    cur: list[int] | None = None
    for t, e in enumerate(uests):
        if cur is not None and cur[2] + e > target:
            chunks.append((cur[0], cur[1], cur[2]))
            cur = None
        if cur is None:
            cur = [t, t, e]
        else:
            cur[1] = t
            cur[2] += e
    if cur is not None:
        chunks.append((cur[0], cur[1], cur[2]))
    return chunks


# ------------------------------------------------------------------ map prompt (mr_map)
HEADINGS = ("## Goal and user constraints", "## Decisions made", "## Files and ids",
            "## Open tasks", "## Errors and failed attempts", "## State at end")
BLOCK_MARK = "\n[... %d chars omitted ...]\n"
INPUT_MARK = "\n[... map input cut: %d chars omitted ...]\n"
MAP_PROMPT = (
    "You compress one slice of an agent coding session into a fixed-size memory note for the same agent. "
    "Reply with ONLY the six sections below, in this order, each starting with its exact heading line, and nothing else. "
    "Be terse: at most 200 words in total. Copy file paths, ids, hashes, commands and error messages verbatim. Write \"none\" for a section with nothing.\n\n"
    + "\n".join(HEADINGS) + "\n\nSESSION SLICE\n<slice>\n%s\n</slice>\n")


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in content)
    return "" if content is None else str(content)


def _head_by_est(text: str, n_est: int) -> str:
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if est_tokens(text[:mid]) <= n_est:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _tail_by_est(text: str, n_est: int) -> str:
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if est_tokens(text[len(text) - mid:]) <= n_est:
            lo = mid
        else:
            hi = mid - 1
    return text[len(text) - lo:]


def _cut_block(text: str, cap: int = BLOCK_CHARS) -> str:
    if len(text) <= cap:
        return text
    h = cap // 2
    return text[:h] + BLOCK_MARK % (len(text) - 2 * h) + text[len(text) - h:]


def render_message(m: dict) -> str:
    c = m.get("content")
    role = str(m.get("role", "?"))
    if isinstance(c, str):
        return "[%s] %s" % (role, _cut_block(c))
    out = []
    for b in c or []:
        if not isinstance(b, dict):
            out.append(_cut_block(str(b)))
            continue
        t = b.get("type")
        if t == "text":
            out.append("[%s] %s" % (role, _cut_block(str(b.get("text", "")))))
        elif t == "tool_use":
            out.append("[tool_use %s] %s" % (b.get("name", "?"), _cut_block(json.dumps(b.get("input"), ensure_ascii=False, default=str))))
        elif t == "tool_result":
            out.append("[tool_result%s] %s" % (" error" if b.get("is_error") else "", _cut_block(_result_text(b.get("content")))))
        else:
            out.append("[%s]" % t)
    return "\n".join(out)


def _cut_by_est(text: str, cap_est: int = INPUT_EST) -> str:
    if est_tokens(text) <= cap_est:
        return text
    h = _head_by_est(text, cap_est // 2 - 20)
    t = _tail_by_est(text, cap_est // 2 - 20)
    out = h + INPUT_MARK % max(0, len(text) - len(h) - len(t)) + t
    while est_tokens(out) > cap_est and len(h) > 100:
        h = h[:-max(1, len(h) // 50)]
        out = h + INPUT_MARK % max(0, len(text) - len(h) - len(t)) + t
    return out


def map_prompt(messages: list[dict]) -> str:
    return MAP_PROMPT % _cut_by_est("\n".join(render_message(m) for m in messages))


def _sections(body: str) -> dict[str, str] | None:
    lines = (body or "").replace("\r\n", "\n").split("\n")
    pos, k = [], 0
    for i, ln in enumerate(lines):
        if k < len(HEADINGS) and ln.strip() == HEADINGS[k]:
            pos.append(i)
            k += 1
    if k != len(HEADINGS):
        return None
    out = {}
    for n, h in enumerate(HEADINGS):
        end = pos[n + 1] if n + 1 < len(pos) else len(lines)
        out[h] = "\n".join(lines[pos[n] + 1:end]).strip()
    return out


def valid_summary(body: str) -> bool:
    """Accepted iff all six headings are present in order and every section is non-empty."""
    s = _sections(body)
    return bool(s) and all(v for v in s.values())


_ERR = re.compile(r"(error|exception|traceback|failed|fatal|denied|not found)", re.I)
_PATH = re.compile(r"(?<![\w/.\-~:])(?:~|\.{1,2})?(?:/[\w.@+\-]+){2,}|(?<![\w/.\-~:])[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]{1,6}\b")


def _block_texts(m: dict) -> list[str]:
    c = m.get("content")
    if isinstance(c, str):
        return [c]
    out = []
    for b in c or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str):
            out.append(b["text"])
        elif t == "tool_result":
            out.append(_result_text(b.get("content")))
        elif t == "tool_use":
            out.append(json.dumps(b.get("input"), ensure_ascii=False, default=str))
    return out


def extractive(messages: list[dict]) -> str:
    """Deterministic fallback for a map reply that stays invalid after a retry (tagged source=extractive):
    first lines of user turns, file paths and error lines, clipped to BODY_EST."""
    users, paths, errs = [], [], []
    for m in messages:
        for text in _block_texts(m):
            if m.get("role") == "user" and text.strip():
                users.append(text.strip().split("\n", 1)[0][:200])
            for e in _PATH.finditer(text):
                if e.group(0) not in paths:
                    paths.append(e.group(0))
            for ln in text.split("\n"):
                if _ERR.search(ln) and len(errs) < 20:
                    errs.append(ln.strip()[:160])
    body = "user: " + " | ".join(users[:6]) + "\npaths: " + " ".join(paths[:30]) + "\nerrors: " + " | ".join(errs[:8])
    return body if est_tokens(body) <= BODY_EST else _head_by_est(body, BODY_EST)


def _wrapped(i: int, n: int, a: int, b: int, body: str) -> str:
    return "<chunk-summary n=%d of %d covers messages %d-%d>\n%s\n</chunk-summary>" % (i, n, a, b, body)


def fit_summary(body: str, i: int, n: int, a: int, b: int) -> str:
    """The wrapped summary is at most SUMMARY_CEIL est (wrapper counted inside); the BODY tail is cut, never the wrapper."""
    w = _wrapped(i, n, a, b, body)
    if est_tokens(w) <= SUMMARY_CEIL:
        return w
    lo, hi = 0, len(body)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if est_tokens(_wrapped(i, n, a, b, body[:mid])) <= SUMMARY_CEIL:
            lo = mid
        else:
            hi = mid - 1
    return _wrapped(i, n, a, b, body[:lo])


# ------------------------------------------------------------------ orchestration
#: ``MapCall(prompt, num_predict) -> reply text`` or raises; supplied by the proxy (live backend).
MapCall = Callable[[str, int], Awaitable[str | None]]


@dataclass
class MRResult:
    body: dict
    changed: bool
    row: dict = field(default_factory=dict)


def _with_summaries(body: dict, cut: int, summaries: list[str]) -> dict:
    """Copy of ``body``: messages[1:cut] dropped, the summaries appended to the first user message."""
    out = copy.copy(body)
    msgs = body["messages"]
    first = copy.deepcopy(msgs[0])
    blocks = [{"type": "text", "text": first["content"]}] if isinstance(first.get("content"), str) else list(first.get("content") or [])
    blocks += [{"type": "text", "text": s} for s in summaries]
    first["content"] = blocks
    out["messages"] = [first] + list(msgs[cut:])
    return out


async def shorten(body: dict, map_call: MapCall, *, cap: int, est: Callable[[dict], int],
                  store=None, model: str = "", progress: dict | None = None) -> MRResult | None:
    """The body with its oldest history replaced by chunk summaries, or ``None`` when it is under ``cap`` (nothing to do).

    ``cap``: the local prompt cap (local_mode.prompt_cap_tokens, derived from the window). ``est``: the
    estimator the cap is tested with (the Ollama payload's, so one source). A result with ``changed`` False
    carries the ORIGINAL body and the outcome that explains it.

    ``store`` (P10, a ``chunk_store.ChunkStore`` or None): a closed chunk whose key (raw chunk, prompt version,
    ``model``, num_predict) is stored costs no map call; a valid model summary is stored, an extractive one never.
    ``progress`` (P10): a dict this updates with ``map_calls`` so far, for a caller that cancels the call on a
    deadline and has no row to read."""
    messages = body.get("messages") or []
    before = est(body)
    if before <= cap or len(messages) < 3:
        return None
    t0 = time.monotonic()
    row = {"outcome": OUTCOME_NO_CHUNKS, "chunks": 0, "map_calls": 0, "tokens_before": before,
           "tokens_after": before, "map_wall_s": 0.0}

    def refuse(outcome: str) -> MRResult:
        row["outcome"] = outcome
        row["map_wall_s"] = round(time.monotonic() - t0, 3)
        _finish_store()
        return MRResult(body, False, row)

    def _finish_store() -> None:
        if store is not None:
            store.flush()
            row.update(store.counts())

    hist = messages[1:]
    units = units_of(hist)
    uests = [est_tokens(hist[a:b]) for a, b in units]
    closable = [c for c in partition(uests, chunk_target())[:-1] if c[1] < len(units) - W_CLOSE]
    goal = min(close_threshold(), cap)
    k, saved = 0, 0
    while k < len(closable) and before - saved + k * SUMMARY_CEIL > goal:
        saved += closable[k][2]
        k += 1
    if not closable:
        return refuse(OUTCOME_NO_CHUNKS)
    if before - saved + k * SUMMARY_CEIL > goal:
        return refuse(OUTCOME_CANNOT_FIT)  # every closable chunk would not bring it down: no model call is spent
    spans = [(units[c[0]][0] + 1, units[c[1]][1] + 1) for c in closable[:k]]  # message indices into ``messages``
    row["chunks"] = k
    summaries: list[str] = []
    num_predict = map_num_predict()
    for i, (a, b) in enumerate(spans, 1):
        chunk = messages[a:b]
        text = None
        skey = None
        if store is not None:
            from llm_router.proxy import chunk_store
            try:
                skey = chunk_store.chunk_key(chunk, prompt_version=PROMPT_VERSION, prompt_template=MAP_PROMPT,
                                             model=model, num_predict=num_predict, salt=key_salt())
            except Exception as exc:  # noqa: BLE001 - no key, no store for this chunk; the map still runs
                from llm_router import failopen
                failopen.record("LR-FO-PROXY-CHUNK-STORE", exc, detail="key")
                skey = None
            if skey is not None:
                text = store.get(skey)
                # a stored summary is the BODY_EST-clipped reply (validated before the clip), so a clipped one is
                # no longer a full valid_summary: a hit needs the first heading and to fit, nothing stricter
                if text is not None and not (text.startswith(HEADINGS[0]) and est_tokens(text) <= BODY_EST):
                    store.reject(skey)  # stale or poisoned entry: a miss, dropped, never served
                    text = None
        for _try in range(MAP_TRIES if text is None else 0):
            row["map_calls"] += 1
            if progress is not None:
                progress["map_calls"] = row["map_calls"]
            try:
                reply = await map_call(map_prompt(chunk), num_predict)
            except Exception:  # noqa: BLE001 - any map failure is the documented fallback, never a 500
                return refuse(OUTCOME_MAP_FAILED)
            if reply is None:
                return refuse(OUTCOME_MAP_FAILED)
            reply = reply.strip()
            if valid_summary(reply):
                text = reply if est_tokens(reply) <= BODY_EST else _head_by_est(reply, BODY_EST)
                if skey is not None:
                    store.put(skey, text)
                    store.flush()  # kept even if a later chunk hits the turn deadline
                break
        if text is None:
            text = extractive(chunk)
            row["extractive_chunks"] = row.get("extractive_chunks", 0) + 1
        summaries.append(fit_summary(text, i, k, a, b - 1))
    new = _with_summaries(body, spans[-1][1], summaries)
    after = est(new)
    row["tokens_after"] = after
    if after > cap:
        return refuse(OUTCOME_STILL_OVER)
    row["outcome"] = OUTCOME_SUMMARIZED
    row["map_wall_s"] = round(time.monotonic() - t0, 3)
    _finish_store()
    return MRResult(new, True, row)
