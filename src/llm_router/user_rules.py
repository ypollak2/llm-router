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
stores and validates rules. Round B (UN-01) reads them: ``map_reader.apply_to_chain``
applies ``ban`` and ``order`` rules (:func:`apply_rules`) at read time at every door
that builds a chain (the resource map does not bake them in, so removing a rule takes
effect at once). Unvalidated local models (P0.15) are appended after the rules run:
a ban removes them, but an order rule does not move them. UN-02 adds the exclusions
``cap`` (a hit cap drops its target until the window rolls), ``no_paid_api``
(:data:`UNPAID_PROVIDERS`) and ``local_only`` (the task's chain keeps only entries that
stay on this machine); like a ban they also apply to a pinned chain.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from llm_router import failopen
from llm_router.file_lock import exclusive_lock
from llm_router.paths import state_path
from llm_router.prefer_grammar import KNOWN_PROVIDERS

T = TypeVar("T")

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


# ── reading for routing (round B, UN-01) ─────────────────────────────────────

_rules_cache: dict[str, tuple[tuple[int, int], list[dict]]] = {}


def effective_rules() -> list[dict]:
    """Stored rules for the routing paths: [] when absent, damaged or unreadable
    (fail-open, recorded as CHZ-FO-USER-RULES-READ). Re-read only when the file's
    (mtime_ns, size) changes, so a per-call chain build costs one ``stat``."""
    try:
        target = routing_path()
        st = target.stat()
    except OSError:
        return []
    sig = (st.st_mtime_ns, st.st_size)
    hit = _rules_cache.get(str(target))
    if hit is not None and hit[0] == sig:
        return hit[1]
    rules = read_rules(target)
    _rules_cache[str(target)] = (sig, rules)
    return rules


def _matches(target: dict, model_id: str) -> bool:
    provider, _, model = model_id.partition("/")
    if provider not in target.get("providers", ()):
        return False
    glob = target.get("model")
    return glob is None or fnmatch.fnmatchcase(model, glob)


#: ``no paid API`` (UN-02): the providers that never bill per call. Local runtimes
#: (``ledger_view.LOCAL_PROVIDERS``) and the two subscription CLIs, which are treated as
#: unpaid because they run on the user's seat (as ``cost.log_usage`` costs them 0; an API-key
#: login for either is not told apart here). Every other provider is a paid API, including
#: every provider in ``tools/setup._PROVIDER_REGISTRY`` (each is reached with an API key; a test
#: pins it) and a provider this list does not know. Two are conditional: ``openai_compat`` is
#: unpaid only when its configured base URL is loopback (it can name a remote, key-billed
#: endpoint, config SEC-002), and ``anthropic`` only in the Claude subscription mode, the one
#: mode in which the router never exports an Anthropic API key
#: (``router._keep_reachable_providers``).
UNPAID_PROVIDERS = frozenset({"ollama", "lm_studio", "vllm", "llamacpp", "codex", "gemini_cli"})


def _claude_subscription() -> bool:
    """The Claude subscription mode: the loaded config's setting in a process that has it (the
    MCP server, the proxy), else ``LLM_ROUTER_CLAUDE_SUBSCRIPTION`` as the hooks read it."""
    cfg = sys.modules.get("llm_router.config")
    if cfg is not None:
        try:
            return bool(cfg.get_config().llm_router_claude_subscription)
        except Exception as exc:  # noqa: BLE001 - fall back to the variable the hooks read
            failopen.record("CHZ-FO-USER-RULES-READ", exc)
    return os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").strip().lower() in ("1", "true", "yes")


def _compat_base_is_loopback() -> bool:
    """``openai_compat_base_url`` of the loaded config is a loopback URL. A process without the
    config loaded (a hook) cannot tell, so the endpoint counts as paid there."""
    cfg = sys.modules.get("llm_router.config")
    if cfg is None:
        return False
    from llm_router.local_privacy import is_loopback_base

    return is_loopback_base(cfg.get_config().openai_compat_base_url)


def is_paid_api(provider: str) -> bool:
    """True when a call to *provider* is billed per call (see :data:`UNPAID_PROVIDERS`)."""
    if provider in UNPAID_PROVIDERS:
        return False
    if provider == "openai_compat":
        return not _compat_base_is_loopback()
    return not (provider == "anthropic" and _claude_subscription())


def _stays_local(model_id: str) -> bool:
    """``local_privacy.stays_local``: an Ollama model on a loopback daemon, not a ``-cloud`` tag
    (the NFR-PRIV definition of "stays on this machine"). Imported only when a rule needs it."""
    from llm_router.local_privacy import stays_local

    return stays_local(model_id)


def _bare_model(provider: str, model: str) -> str:
    """A ledger row's model without its provider prefix (rows hold both spellings)."""
    return model[len(provider) + 1:] if model.startswith(provider + "/") else model


def _cap_hit(rule: dict, executed: list[tuple[str, str]] | None, now: float | None) -> bool:
    target, n = rule["target"], rule["n"]
    if rule["unit"] == "day":
        rows = executed or []
        used = sum(1 for p, m in rows if _matches(target, f"{p}/{_bare_model(p, m)}"))
        return used >= n
    from llm_router import map_reader, resource_map

    data = map_reader.load_data(now=now)
    if data is None:
        return False            # no fresh map: pressure unknown, the cap is not hit
    pressures = [resource_map.quota_pressure(r) for r in data["resources"]
                 if r.get("status") == "connected"
                 and str(r.get("chain_id", "")).split("/", 1)[0] in target["providers"]]
    known = [p for p in pressures if p is not None]
    return bool(known) and max(known) * 100.0 >= n


#: How long a cap reading is reused while neither the rules, the ledger files nor the map
#: change. Bounds how late a cap that rolls over (or a hit written by another process before
#: SQLite touches the files) is seen; a chain build otherwise reads the ledger up to four times.
CAP_CACHE_TTL_S = 5.0
_cap_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _file_sig(path) -> tuple | None:
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cap_cache_key(caps: list[dict]) -> tuple:
    from llm_router import ledger_view

    db = str(ledger_view._db_path(None))
    key: tuple = (json.dumps(caps, sort_keys=True), db, _file_sig(db), _file_sig(db + "-wal"))
    if any(r.get("unit") == "pct" for r in caps):
        from llm_router import map_reader, resource_map

        key += (str(resource_map.map_path()), map_reader.token())
    return key


def cap_hits(rules: list[dict], *, now: float | None = None) -> list[dict]:
    """The ``cap`` rules that are hit now. A ``day`` cap counts the router-executed calls of its
    target in the rolling 24 h (``ledger_view.router_executed_models``, D-125); a ``pct`` cap
    compares the provider's quota pressure on the fresh resource map (the higher of the session
    and weekly readings, measured or estimated only) with its percent. A hit cap drops its
    target until the window rolls. Fail-open: a cap whose reading fails is not hit, and the
    failure is recorded as ``CHZ-FO-USER-RULES-READ``; unknown pressure or no ledger is 0 use.
    With ``now`` omitted the result is reused for :data:`CAP_CACHE_TTL_S` while the rules, the
    ledger files (``usage.db`` and its WAL) and the map are unchanged."""
    caps = [r for r in rules if r.get("kind") == "cap"]
    if not caps:
        return []
    if now is not None:
        return _read_caps(caps, now)
    try:
        key = _cap_cache_key(caps)
    except Exception as exc:  # noqa: BLE001 - no cache key: read uncached
        failopen.record("CHZ-FO-USER-RULES-READ", exc)
        return _read_caps(caps, None)
    t = time.monotonic()
    cached = _cap_cache.get(key)
    if cached is not None and cached[0] > t:
        return cached[1]
    hit = _read_caps(caps, None)
    try:
        key = _cap_cache_key(caps)   # after the read: opening a WAL ledger can create its -wal file
    except Exception as exc:  # noqa: BLE001 - keep the pre-read key
        failopen.record("CHZ-FO-USER-RULES-READ", exc)
    _cap_cache.clear()
    _cap_cache[key] = (t + CAP_CACHE_TTL_S, hit)
    return hit


def _read_caps(caps: list[dict], now: float | None) -> list[dict]:
    executed: list[tuple[str, str]] | None = None
    if any(r.get("unit") == "day" for r in caps):
        try:
            from llm_router import ledger_view

            executed = ledger_view.router_executed_models(ledger_view.rolling(24.0, now=now))
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger never blocks routing
            failopen.record("CHZ-FO-USER-RULES-READ", exc)
            caps = [r for r in caps if r.get("unit") != "day"]
    hit = []
    for rule in caps:
        try:
            if _cap_hit(rule, executed, now):
                hit.append(rule)
        except Exception as exc:  # noqa: BLE001 - fail-open per cap
            failopen.record("CHZ-FO-USER-RULES-READ", exc)
    return hit


def _token_of(hit: list[dict]) -> tuple[str, ...]:
    return tuple(sorted(str(r.get("id", r.get("text", ""))) for r in hit))


def cap_token(rules: list[dict] | None = None) -> tuple[str, ...] | None:
    """Cache-key part for chain caches: the ids of the caps hit now (``None`` without caps). A
    cap that is hit or rolls over changes the chain with the map and the rules file unchanged."""
    rules = effective_rules() if rules is None else rules
    if not any(r.get("kind") == "cap" for r in rules):
        return None
    return _token_of(cap_hits(rules))


def allows(model_id: str, *, task: str | None = None) -> bool:
    """True when the stored rules leave *model_id* in a one-entry chain (bans, hit caps,
    ``no_paid_api`` and, for *task*, ``local_only``; order rules cannot drop it). For a
    configured pin that bypasses the chain builders. Fail-open: a rule read that raises allows."""
    try:
        rules = effective_rules()
        return not rules or bool(apply_rules([model_id], lambda m: m, rules, reorder=False, task=task))
    except Exception as exc:  # noqa: BLE001 - a rule read never blocks routing
        failopen.record("CHZ-FO-USER-RULES-READ", exc)
        return True


def apply_rules(chain: Sequence[T], key: Callable[[T], str], rules: list[dict], *,
                reorder: bool = True, task: str | None = None,
                now: float | None = None) -> list[T]:
    """*chain* without the entries a ``ban`` rule or a hit ``cap`` names, without paid APIs
    under ``no_paid_api``, and, when a ``local_only`` rule names *task*, without every entry
    that does not stay on this machine (an empty result is the caller's no-route path). Then
    reordered so each ``order`` rule's ``first`` entries precede its ``then`` entries (relative
    order inside a group is kept; entries the rules do not name stay where they were).
    ``reorder=False`` (a pinned model, a floor chain) applies the exclusions only."""
    drops = [r["target"] for r in rules if r.get("kind") == "ban"]
    drops += [r["target"] for r in cap_hits(rules, now=now)]
    out = [m for m in chain if not any(_matches(b, key(m)) for b in drops)]
    if any(r.get("kind") == "no_paid_api" for r in rules):
        out = [m for m in out if not is_paid_api(key(m).partition("/")[0])]
    if task is not None and any(r.get("kind") == "local_only" and r.get("task") == task for r in rules):
        out = [m for m in out if _stays_local(key(m))]
    orders = [r for r in rules if r.get("kind") == "order"] if reorder else []
    for _ in range(len(orders) + 1):          # chained rules (A before B before C) settle in n passes
        changed = False
        for r in orders:
            ids = [key(m) for m in out]
            then_at = next((i for i, x in enumerate(ids) if _matches(r["then"], x)), None)
            if then_at is None:
                continue
            late = [i for i in range(then_at + 1, len(out)) if _matches(r["first"], ids[i])]
            if not late:
                continue
            moved = [out[i] for i in late]
            rest = [m for i, m in enumerate(out) if i not in set(late)]
            out = rest[:then_at] + moved + rest[then_at:]
            changed = True
        if not changed:
            break
    return out
