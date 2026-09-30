"""Tool retrieval + history compaction for a locally served proxy step.

Why: on the 2026-09-28 realistic A/B (``~/.rsi/research/proxy-realistic-ab-
2026-09-28.md``), ``qwen3-coder:30b`` returned an empty reply 18/18 times when
it was sent the full Claude Code request (every tool, ~25-30k prompt tokens,
prompt eval ~2 ms/token). This module sends it a small request instead:

Tools
    Each tool's ``name: description`` is embedded once with a local Ollama
    embedding model (``nomic-embed-text`` by default) and cached, in memory
    and on disk under the state dir. A step ranks the request's tools by cosine
    similarity to (original ask + newest tool output) and keeps the top-K, plus
    every tool the session already used, plus the edit tools the request
    carries (so that ``capability.check_reply`` can SEE an edit-shaped step and
    route it to the edit protocol instead of the model answering it some other
    way). The filter itself is ``proxy.backends._trim_unused_tools(body,
    keep=...)``. Tool names and ``input_schema`` are byte-identical to the
    request's, so a tool call the model emits is valid for the client as-is
    (the "translation back" is the identity on names); only long descriptions
    are cut. A call to a request tool that was NOT offered this step is still
    validated against the request's full client tool list
    (``translate.VALIDATE_TOOLS_KEY``), since the client offered it. Tools marked ``defer_loading`` (Claude Code Tool Search) are not
    offered unless the session already loaded and used them. If the embedding
    call fails, a lexical overlap ranking is used and the ledger says so.

History
    The system prompt becomes the condensed step prompt plus the environment
    lines (working directory, git, platform). The first user turn is reduced to
    the user's actual ask (``<system-reminder>`` blocks removed). Then the last
    ``keep_results`` tool exchanges, newest output kept longest, plus any older
    ``Read`` of a file the recent window mentions ("the file contents the step
    needs"). Everything else is dropped, with a one-line note saying how many
    steps were omitted. If the estimate is still over ``max_prompt_tokens``,
    a fixed ladder shrinks it (older outputs, then the lowest-ranked tools,
    then older exchanges, then the newest output, then descriptions). The
    estimate is chars/4 of the translated Ollama payload; the backend's real
    ``prompt_eval_count`` is in the ledger row next to it.

Nothing here writes prompt or tool text to the ledger: ``info`` holds counts,
tool names, the ranking method and token estimates only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Awaitable, Callable

from llm_router.local_agent import LocalAgentConfig
from llm_router.proxy.backends import CONDENSED_SYSTEM, _trim_unused_tools
from llm_router.proxy.steps import non_system, prev_tools
from llm_router.proxy.translate import VALIDATE_TOOLS_KEY, ollama_tools, system_text, to_ollama_messages

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_ENV_RE = re.compile(r"^\s*-?\s*(Primary working directory|Working directory|Is a git repository|"
                     r"Platform|Shell|OS Version):.*$", re.M)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_OMIT = "\n[... {n} chars omitted by llm-router compaction ...]\n"

# Char caps per ladder rung: (newest output, older outputs, needed-file reads).
_CAPS = [(6000, 1500, 3000), (6000, 600, 1500), (6000, 600, 0), (2500, 400, 0), (1200, 300, 0)]
_MIN_TOOLS = 3
_ASSISTANT_TEXT_CAP = 500
_TOOL_INPUT_STR_CAP = 800
_ASK_CAP = 4000


# ── text helpers (also used by capability) ───────────────────────────────────


def strip_reminders(text: str) -> str:
    return _REMINDER_RE.sub("", text or "").strip()


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)


def result_text(block: dict) -> str:
    """A tool_result block's content as text (``tool_reference`` entries by name)."""
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    parts = []
    for b in inner or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_reference":
            parts.append(f"[tool loaded: {b.get('tool_name', '')}]")
    return "\n".join(parts)


def original_ask(body: dict) -> str:
    """The user's actual first message, without the reminder blocks Claude Code
    prepends (CLAUDE.md, memory, attribution: ~20k chars on this machine)."""
    turns = non_system(body.get("messages") or [])
    if not turns:
        return ""
    return strip_reminders(_blocks_text(turns[0].get("content")))


def newest_results_text(body: dict, limit: int = 2000) -> str:
    turns = non_system(body.get("messages") or [])
    last = turns[-1].get("content") if turns else None
    out = [result_text(b) for b in last if isinstance(b, dict) and b.get("type") == "tool_result"] \
        if isinstance(last, list) else []
    return "\n".join(out)[:limit]


def env_lines(body: dict) -> list[str]:
    """Working directory / git / platform lines from the system prompt or a
    ``role: system`` message, deduplicated, in order."""
    sources = [system_text(body.get("system"))]
    sources += [_blocks_text(m.get("content")) for m in body.get("messages") or []
                if isinstance(m, dict) and m.get("role") == "system"]
    seen: list[str] = []
    for text in sources:
        for m in _ENV_RE.finditer(text or ""):
            line = m.group(0).strip().lstrip("- ").strip()
            if line not in seen:
                seen.append(line)
    return seen


def session_cwd(body: dict) -> str | None:
    for line in env_lines(body):
        key, _, value = line.partition(":")
        if key.strip() in ("Primary working directory", "Working directory") and value.strip():
            return value.strip()
    return None


def used_tools(body: dict) -> list[str]:
    names: list[str] = []
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") not in names:
                    names.append(b.get("name"))
    return names


def estimate_tokens(body: dict) -> int:
    """chars/4 of what the Ollama backend is actually sent (messages + tools)."""
    payload = json.dumps(to_ollama_messages(body)) + json.dumps(ollama_tools(body.get("tools") or []))
    return len(payload) // 4


# ── tool retrieval ───────────────────────────────────────────────────────────


def _client_tools(body: dict) -> list[dict]:
    return [t for t in body.get("tools") or []
            if isinstance(t, dict) and t.get("name") and "input_schema" in t]


# What is embedded per tool: the name and the start of its description, where
# Claude Code states what the tool is for. Chosen from 150 / 300 / 2000 chars
# on one captured fixture session (pagination task, 6 steps): at 2000 the long
# usage prose pulled ScheduleWakeup/Workflow into the top 6 on most steps; at
# 300 the top 6 were the file/search tools. One session, so a weak choice.
TOOL_DOC_CHARS = 300


def tool_doc(tool: dict) -> str:
    return f"{tool.get('name')}: {tool.get('description') or ''}"[:TOOL_DOC_CHARS]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def lexical_scores(query: str, docs: dict[str, str]) -> dict[str, float]:
    """Fallback ranking: shared identifier-ish words, length-normalised."""
    q = {w.lower() for w in _WORD_RE.findall(query)}
    out = {}
    for name, doc in docs.items():
        words = {w.lower() for w in _WORD_RE.findall(doc)}
        out[name] = len(q & words) / math.sqrt(len(words) or 1)
    return out


class ToolRetriever:
    """Embeds each tool doc once per (model, doc) and ranks tools for a step.

    The cache is keyed by sha256(model + doc), held in memory and mirrored to
    ``cache_path`` (JSON) so a proxy restart does not re-embed. Tool text is
    not secret (it is Claude Code's own tool list), but only the hash is used
    as the key."""

    def __init__(self, embed: EmbedFn | None, model: str, cache_path: Path | None = None,
                 timeout_s: float = 5.0) -> None:
        self.embed = embed
        self.model = model
        self.cache_path = cache_path
        self.timeout_s = timeout_s
        self._cache: dict[str, list[float]] | None = None
        self.embed_calls = 0

    def _key(self, doc: str) -> str:
        return hashlib.sha256(f"{self.model}\0{doc}".encode()).hexdigest()

    def _load(self) -> dict[str, list[float]]:
        if self._cache is None:
            self._cache = {}
            if self.cache_path is not None:
                try:
                    data = json.loads(self.cache_path.read_text())
                    if isinstance(data, dict):
                        self._cache = {k: v for k, v in data.items() if isinstance(v, list)}
                except (OSError, ValueError):
                    pass
        return self._cache

    def _save(self) -> None:
        if self.cache_path is None or self._cache is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache))
            tmp.replace(self.cache_path)
        except OSError as exc:  # a cache that cannot be written only costs re-embedding
            from llm_router import failopen

            failopen.record("LR-FO-LOCAL-AGENT-EMBED-CACHE-WRITE", exc)

    async def rank(self, tools: list[dict], query: str) -> tuple[list[tuple[str, float]], dict]:
        """``([(name, score)] best first, info)``; info["method"] is
        ``embed`` or ``lexical`` (with ``embed_error`` when the embed failed)."""
        docs = {t["name"]: tool_doc(t) for t in tools}
        info: dict = {"method": "embed", "embedded_now": 0}
        scores: dict[str, float] | None = None
        if self.embed is not None and docs:
            try:
                scores = await asyncio.wait_for(self._embed_scores(docs, query, info), self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - any embed failure falls back to lexical
                info["embed_error"] = f"{type(exc).__name__}: {exc}"[:160]
                scores = None
        if scores is None:
            info["method"] = "lexical"
            scores = lexical_scores(query, docs)
        ranked = sorted(docs, key=lambda n: (-scores.get(n, 0.0), n))
        return [(n, round(scores.get(n, 0.0), 4)) for n in ranked], info

    async def _embed_scores(self, docs: dict[str, str], query: str, info: dict) -> dict[str, float]:
        cache = self._load()
        missing = [d for d in dict.fromkeys(docs.values()) if self._key(d) not in cache]
        if missing:
            vecs = await self.embed(["search_document: " + d for d in missing])
            self.embed_calls += 1
            if len(vecs) != len(missing):
                raise ValueError("embedding count mismatch")
            for d, v in zip(missing, vecs):
                cache[self._key(d)] = v
            info["embedded_now"] = len(missing)
            self._save()
        (qvec,) = await self.embed(["search_query: " + query])
        self.embed_calls += 1
        return {n: _cosine(qvec, cache[self._key(d)]) for n, d in docs.items()}


def ollama_embedder(client, base_url: str, model: str, keep_alive: str | int = "30m") -> EmbedFn:
    """``/api/embed`` on the same Ollama the proxy serves from."""
    url = base_url.rstrip("/") + "/api/embed"

    async def embed(texts: list[str]) -> list[list[float]]:
        r = await client.post(url, json={"model": model, "input": texts, "keep_alive": keep_alive},
                              timeout=30.0)
        r.raise_for_status()
        vecs = r.json().get("embeddings")
        if not isinstance(vecs, list):
            raise ValueError("no embeddings in reply")
        return vecs

    return embed


# ── history ──────────────────────────────────────────────────────────────────


def _cut(text: str, cap: int) -> str:
    if cap <= 0 or len(text) <= cap:
        return text
    head = cap * 2 // 3
    tail = cap - head
    return text[:head] + _OMIT.format(n=len(text) - cap) + text[-tail:]


def _cap_input(value, cap: int = _TOOL_INPUT_STR_CAP):
    if isinstance(value, str):
        return _cut(value, cap)
    if isinstance(value, dict):
        return {k: _cap_input(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_input(v, cap) for v in value]
    return value


def _assistant_turn(m: dict) -> dict:
    content = m.get("content")
    if isinstance(content, str):
        return {"role": "assistant", "content": _cut(content, _ASSISTANT_TEXT_CAP)}
    blocks = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text", "").strip():
            blocks.append({"type": "text", "text": _cut(b["text"], _ASSISTANT_TEXT_CAP)})
        elif b.get("type") == "tool_use":
            blocks.append(dict(b, input=_cap_input(b.get("input") or {})))
    return {"role": "assistant", "content": blocks}


def _user_turn(m: dict, cap: int) -> dict:
    content = m.get("content")
    if isinstance(content, str):
        return {"role": "user", "content": _cut(strip_reminders(content), cap)}
    blocks = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            nb = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"),
                  "content": _cut(result_text(b), cap)}
            if b.get("is_error"):
                nb["is_error"] = True
            blocks.append(nb)
        elif b.get("type") == "text":
            text = strip_reminders(b.get("text", ""))
            if text:
                blocks.append({"type": "text", "text": _cut(text, 300)})
    return {"role": "user", "content": blocks}


def _exchanges(turns: list[dict]) -> list[tuple[dict, dict]]:
    """(assistant, user) pairs after the first user turn."""
    out = []
    i = 1
    while i + 1 < len(turns):
        if turns[i].get("role") == "assistant" and turns[i + 1].get("role") == "user":
            out.append((turns[i], turns[i + 1]))
            i += 2
        else:
            i += 1
    return out


def _read_paths(assistant: dict) -> list[str]:
    content = assistant.get("content")
    return [str((b.get("input") or {}).get("file_path", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read"
            and (b.get("input") or {}).get("file_path")] if isinstance(content, list) else []


def _mentioned(path: str, text: str) -> bool:
    name = Path(path).name
    return bool(name) and name in text


def _history(body: dict, keep: int, caps: tuple[int, int, int]) -> tuple[list[dict], dict]:
    newest_cap, older_cap, file_cap = caps
    turns = non_system(body.get("messages") or [])
    pairs = _exchanges(turns)
    recent = pairs[-keep:] if keep > 0 else []
    older = pairs[: len(pairs) - len(recent)]
    window_text = original_ask(body) + "\n" + json.dumps(
        [a.get("content") for a, _ in recent], default=str)[:20000] + newest_results_text(body, 20000)
    needed = []
    if file_cap > 0:
        needed = [(a, u) for a, u in older if any(_mentioned(p, window_text) for p in _read_paths(a))]
    omitted = len(older) - len(needed)
    ask = _cut(original_ask(body), _ASK_CAP)
    if omitted:
        ask += f"\n\n[{omitted} earlier tool step(s) omitted by llm-router compaction]"
    msgs: list[dict] = [{"role": "user", "content": ask}]
    for a, u in needed:
        msgs += [_assistant_turn(a), _user_turn(u, file_cap)]
    for idx, (a, u) in enumerate(recent):
        cap = newest_cap if idx == len(recent) - 1 else older_cap
        msgs += [_assistant_turn(a), _user_turn(u, cap)]
    return msgs, {"results_kept": len(recent), "file_reads_kept": len(needed), "steps_omitted": omitted}


def _system(body: dict) -> str:
    env = env_lines(body)
    return CONDENSED_SYSTEM + ("\n\nEnvironment:\n" + "\n".join(env) if env else "")


def _with_desc_cap(tools: list[dict], cap: int) -> list[dict]:
    out = []
    for t in tools:
        d = t.get("description")
        out.append(dict(t, description=d[:cap]) if isinstance(d, str) and len(d) > cap else t)
    return out


def select_tools(body: dict, ranked: list[tuple[str, float]], top_k: int) -> tuple[list[str], dict]:
    """Names to keep: top-K ranked (non-deferred), plus used, plus edit tools."""
    tools = {t["name"]: t for t in _client_tools(body)}
    used = [n for n in used_tools(body) if n in tools]
    deferred = {n for n, t in tools.items() if t.get("defer_loading") and n not in used}
    top = [n for n, _ in ranked if n not in deferred][:top_k]
    edit = [n for n in tools if n in EDIT_TOOLS]
    keep = list(dict.fromkeys(top + used + edit))
    return keep, {"top_k": top, "used": used, "edit_signal": edit}


def build(body: dict, ranked: list[tuple[str, float]], cfg: LocalAgentConfig) -> tuple[dict, dict]:
    """The compacted request (pure; no I/O) and its ``info``."""
    keep, sel = select_tools(body, ranked, cfg.top_k)
    rank_of = {n: i for i, (n, _) in enumerate(ranked)}
    protected = set(sel["edit_signal"]) | set(prev_tools(body))
    desc_cap = cfg.tool_desc_chars
    keep_results = cfg.keep_results
    rung = 0
    info: dict = {"tools_in": len(_client_tools(body)), "est_tokens_in": estimate_tokens(body)}
    while True:
        out = {k: v for k, v in body.items() if k not in ("system", "messages", "tools")}
        out[VALIDATE_TOOLS_KEY] = _client_tools(body)
        out["system"] = _system(body)
        out["messages"], hinfo = _history(body, keep_results, _CAPS[min(rung, len(_CAPS) - 1)])
        trimmed = _trim_unused_tools(dict(body, messages=[]), keep=set(keep))
        out["tools"] = _with_desc_cap(trimmed["tools"], desc_cap)
        est = estimate_tokens(out)
        if est <= cfg.max_prompt_tokens:
            break
        # The ladder: shorter older outputs, then fewer tools (lowest-ranked
        # first; the edit tools and the ones the newest output answers stay),
        # then fewer exchanges, then a shorter newest output, then shorter
        # descriptions.
        if rung < 2:
            rung += 1
        elif len(keep) > _MIN_TOOLS and any(n not in protected for n in keep):
            victim = max((n for n in keep if n not in protected), key=lambda n: rank_of.get(n, 1 << 30))
            keep.remove(victim)
        elif keep_results > 1:
            keep_results -= 1
        elif rung < len(_CAPS) - 1:
            rung += 1
        elif desc_cap > 200:
            desc_cap = 200
        else:
            break
    info.update(hinfo, tools_kept=[t["name"] for t in out["tools"]], selection=sel,
                est_tokens_out=est, over_budget=est > cfg.max_prompt_tokens,
                budget=cfg.max_prompt_tokens, ladder_rung=rung, desc_cap=desc_cap)
    return out, info


async def compact(body: dict, retriever: ToolRetriever, cfg: LocalAgentConfig) -> tuple[dict, dict]:
    """Rank the request's tools for this step, then ``build``."""
    query = original_ask(body)[:1500] + "\n" + newest_results_text(body, 2000)
    ranked, rinfo = await retriever.rank(_client_tools(body), query)
    out, info = build(body, ranked, cfg)
    info["retrieval"] = rinfo
    return out, info
