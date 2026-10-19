"""P2.13 store for ``llm-router prefer``: a managed block inside routing.yaml.

routing.yaml is edited by hand, so this module never rewrites the file: it
replaces only the bytes between two marker lines and preserves every other byte
(CRLF, comments, a missing trailing newline, even invalid UTF-8). The block is a
JSON flow sequence, which is valid YAML (``repo_config`` reads it with PyYAML)
and which a hook can read with ``json.loads`` and no PyYAML::

    # >>> llm-router prefer (managed; edit with `llm-router prefer`) >>>
    preferences: [
      {"id":"p1","kind":"order",...,"text":"codex before claude"}
    ]
    # <<< llm-router prefer <<<

Stdlib-only (plus ``paths``, ``file_lock`` and ``failopen``), no PyYAML. Round A
stores and validates rules; no routing path reads them yet.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from llm_router import failopen
from llm_router.file_lock import exclusive_lock
from llm_router.paths import state_path
from llm_router.prefer_grammar import KNOWN_PROVIDERS

MARK_BEGIN = "# >>> llm-router prefer (managed; edit with `llm-router prefer`) >>>"
MARK_END = "# <<< llm-router prefer <<<"
_BEGIN = MARK_BEGIN.encode()
_END = MARK_END.encode()
_OUTSIDE_KEY = re.compile(rb"(?m)^preferences[ \t]*:")


class StoreError(Exception):
    """The file cannot be safely written; nothing was changed."""


def routing_path() -> Path:
    return state_path("routing.yaml")


def token() -> int | None:
    """mtime_ns of routing.yaml (cache key for readers), or None when absent."""
    try:
        return routing_path().stat().st_mtime_ns
    except OSError:
        return None


# ── block location ───────────────────────────────────────────────────────────

def _find_block(data: bytes) -> tuple[int, int] | None:
    """(start, end) byte span of the marker lines, end excluding the line break.

    None when there is no block. Raises StoreError when the markers are damaged
    (one without the other, repeated, or out of order).
    """
    begins = [m.start() for m in re.finditer(rb"(?m)^" + re.escape(_BEGIN) + rb"[ \t]*\r?$", data)]
    ends = [m.end() for m in re.finditer(rb"(?m)^" + re.escape(_END) + rb"[ \t]*(?=\r?$)", data)]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] <= begins[0]:
        raise StoreError("the managed preferences block in routing.yaml is damaged "
                         "(markers missing, repeated or out of order); fix or remove it by hand")
    return begins[0], ends[0]


def _parse_block(block: bytes) -> list[dict]:
    lines = block.decode("utf-8").splitlines()
    body = "\n".join(line for line in lines[1:-1])
    key, sep, rest = body.partition("preferences:")
    if not sep or key.strip():
        raise ValueError("no preferences key in block")
    rules = json.loads(rest)
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise ValueError("preferences is not a list of rules")
    return rules


def _block_rules(block: bytes) -> list[dict]:
    try:
        return _parse_block(block)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise StoreError(f"the managed preferences block in routing.yaml is unreadable ({exc}); "
                         "fix it by hand or remove the block") from exc


def load_rules(path: Path | None = None) -> list[dict]:
    """Stored rules; [] when there is no file or block. Raises StoreError when the
    block is damaged or unreadable (writers and `--list` must not mistake that
    for "no preferences")."""
    target = path or routing_path()
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreError(f"routing.yaml cannot be read: {exc}") from exc
    span = _find_block(data)
    return [] if span is None else _block_rules(data[span[0]:span[1]])


def read_rules(path: Path | None = None) -> list[dict]:
    """Stored rules, or [] when absent or unreadable (fail-open, recorded)."""
    try:
        return load_rules(path)
    except StoreError as exc:
        failopen.record("CHZ-FO-USER-RULES-READ", exc)
        return []


# ── validation ───────────────────────────────────────────────────────────────

def _key(target: dict) -> str:
    return json.dumps(target, sort_keys=True)


def _covers(ban: dict, other: dict) -> bool:
    """True when every model *other* names is excluded by *ban*."""
    if not set(other["providers"]) <= set(ban["providers"]):
        return False
    return ban["model"] is None or ban["model"] == other["model"]


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    graph: dict[str, set[str]] = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set())
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for nxt in graph[node]:
            if state.get(nxt) == 1 or (state.get(nxt) is None and visit(nxt)):
                return True
        state[node] = 2
        return False

    return any(state.get(n) is None and visit(n) for n in graph)


def check_conflicts(existing: list[dict], new: list[dict]) -> str | None:
    """Reason the combined rule set is contradictory, or None."""
    rules = [*existing, *new]
    for rule in new:
        if rule["kind"] == "cap":
            n = rule["n"]
            if n < 1 or (rule["unit"] == "pct" and n > 100):
                return f"cap {n} is out of range (per-day n >= 1, percent 1-100)"
    orders = [r for r in rules if r["kind"] == "order"]
    if _has_cycle([(_key(r["first"]), _key(r["then"])) for r in orders]):
        return "these order rules form a cycle (A before B and, directly or through others, B before A)"
    bans = [r["target"] for r in rules if r["kind"] == "ban"]
    for order in orders:
        for ban in bans:
            if _covers(ban, order["first"]):
                return "an order puts a banned target first; remove the ban or the order"
    whole = {p for ban in bans if ban["model"] is None for p in ban["providers"]}
    if set(KNOWN_PROVIDERS) <= whole:
        return "the bans together exclude every known provider"
    ollama = {"providers": ["ollama"], "model": None}
    if any(r["kind"] == "local_only" for r in rules) and any(_covers(ban, ollama) for ban in bans):
        return "an only-local rule needs ollama, which is banned"
    return None


# ── writing ──────────────────────────────────────────────────────────────────

def _render(rules: list[dict], nl: str) -> bytes:
    if rules:
        items = ("," + nl).join("  " + json.dumps(r, ensure_ascii=True, separators=(",", ":"))
                                for r in rules)
        body = f"preferences: [{nl}{items}{nl}  ]"
    else:
        body = "preferences: []"
    return (MARK_BEGIN + nl + body + nl + MARK_END).encode("ascii")


def _newline(data: bytes) -> str:
    first = data.find(b"\n")
    return "\r\n" if first > 0 and data[first - 1:first] == b"\r" else "\n"


def _backup(target: Path, data: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = target.with_name(f"{target.name}.pre-prefer-{stamp}")
    n = 0
    while dest.exists():
        n += 1
        dest = target.with_name(f"{target.name}.pre-prefer-{stamp}-{n}")
    dest.write_bytes(data)
    return dest


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o7777)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            failopen.record("CHZ-FO-USER-RULES-TMP-CLEANUP", exc)
        raise


def mutate_rules(change: Callable[[list[dict]], list[dict]], path: Path | None = None) -> list[dict]:
    """Read-modify-write the block under the file lock; returns the stored rules.

    *change* gets the current rules and returns the new list (it may raise
    StoreError to abort). Bytes outside the block are preserved exactly; the
    first write to a file without a block backs it up first.
    """
    target = Path(path) if path else routing_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(target.with_name(target.name + ".lock")) as locked:
        if not locked:
            raise StoreError("routing.yaml is locked by another writer; try again")
        data = target.read_bytes() if target.exists() else b""
        span = _find_block(data)
        outside = data[:span[0]] + data[span[1]:] if span else data
        if _OUTSIDE_KEY.search(outside):
            raise StoreError("routing.yaml already has a top-level `preferences:` key outside "
                             "the managed block; rename or remove it first (nothing was written)")
        current = _block_rules(data[span[0]:span[1]]) if span else []
        new_rules = change(list(current))
        if span:
            nl = _newline(data[span[0]:span[1]])
            out = data[:span[0]] + _render(new_rules, nl) + data[span[1]:]
        else:
            nl = _newline(data)
            sep = b"" if not data else (b"" if data.endswith(b"\n") else nl.encode()) + nl.encode()
            if data:
                _backup(target, data)
            out = data + sep + _render(new_rules, nl) + nl.encode()
        _atomic_write(target, out)
    return new_rules


def write_rules(rules: list[dict], path: Path | None = None) -> list[dict]:
    """Replace the stored rules with *rules* (see mutate_rules)."""
    return mutate_rules(lambda _old: list(rules), path)


def next_id(existing: list[dict]) -> str:
    nums = [int(str(r.get("id", ""))[1:]) for r in existing
            if re.fullmatch(r"p\d+", str(r.get("id", "")))]
    return f"p{max(nums, default=0) + 1}"


def _bare(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k not in ("id", "text")}


def _same(a: dict, b: dict) -> bool:
    return _bare(a) == _bare(b)


def add_rules(parsed: list[dict], text: str, path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Validate and store parsed rules. Returns (stored_all, added).

    A rule identical to a stored one is not added twice. Conflicts raise
    StoreError with the reason; nothing is written.
    """
    added: list[dict] = []

    def change(existing: list[dict]) -> list[dict]:
        fresh = [r for r in parsed if not any(_same(r, e) for e in existing)]
        reason = check_conflicts(existing, fresh)
        if reason:
            raise StoreError(reason)
        out = list(existing)
        for rule in fresh:
            stored = {"id": next_id(out), **rule, "text": text}
            out.append(stored)
            added.append(stored)
        return out

    stored = mutate_rules(change, path)
    if any(r.get("kind") == "local_only" for r in added):
        from llm_router import examples

        examples.record(text, "rule", 0.0)  # P3.6: hash-only evidence; a rule sentence is not a prompt, never embedded
    return stored, added


def remove_rule(rule_id: str, path: Path | None = None) -> list[dict]:
    """Remove one rule by id; raises StoreError when the id is unknown."""
    def change(existing: list[dict]) -> list[dict]:
        kept = [r for r in existing if r.get("id") != rule_id]
        if len(kept) == len(existing):
            raise StoreError(f"no stored preference with id {rule_id!r}")
        return kept

    return mutate_rules(change, path)
