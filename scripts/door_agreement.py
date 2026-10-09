#!/usr/bin/env python3
"""Do the router's classifier doors agree? (PLAN v16 P1.6 task 0, P1.6-i)

Seven call sites classify a prompt, each with its own code path. P1.6 replaces
them with one engine, one PR per site; after every PR this tool is re-run on the
same frozen corpus and the disagreement is recorded as baseline-and-delta. The
target is 0.

    # once: freeze the corpus (the 2026-09-23 n=1571 was never stored)
    $ python scripts/door_agreement.py --freeze-corpus PATH

    # every time: measure
    $ python scripts/door_agreement.py --corpus PATH --sites hook,gateway --out OUT.json

    # optional: a counts-only per-line date sidecar, then a split by date
    $ python scripts/door_agreement.py --corpus PATH --freeze-dates DATES
    $ python scripts/door_agreement.py --corpus PATH --dates DATES --date-cutoff 2026-09-23 ...

To measure an older commit, put that tree's ``src`` first on PYTHONPATH (a git
worktree of the SHA). Every site resolves its module from the imported
``llm_router`` package, and ``git_sha`` in the output is the HEAD of that tree.

Privacy. The corpus is real prompt text. This tool never prints or writes a
prompt: the output holds counts, the corpus sha256 and per-site label tallies.
The frozen corpus is created with mode 0600 and never overwritten.

No model calls, no network. Classification uses the heuristic/sync paths, as
``scripts/measure_low_signal_rate.py`` did. A site that has an LLM layer gets it
switched off with the environment switch its own code reads (listed per site in
``no_llm_switches`` in the output); on top of that every outbound socket connect
is refused for the whole measurement, so a missed switch fails loudly instead of
calling a model.

Adding a site (one per P1.6 wiring PR): write a loader that returns
``classify(text) -> (task_type, tier)`` and add a ``Site`` to ``SITES``.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Callable

Z95 = 1.959963984540054
NONE_LABEL = "<none>"  # the site declined to classify (the hook returns None)

Classify = Callable[[str], "tuple[str | None, str | None]"]


@dataclass(frozen=True)
class Site:
    name: str
    description: str
    load: Callable[[], Classify]
    # env var -> value set before the site's module is imported, each with why.
    no_llm_env: dict[str, str] = field(default_factory=dict)
    no_llm_note: str = ""


# ── Sites ────────────────────────────────────────────────────────────────────

def _hook_path() -> Path:
    import llm_router

    return Path(llm_router.__file__).resolve().parent / "hooks" / "auto-route.py"


def _load_hook() -> Classify:
    path = _hook_path()
    spec = importlib.util.spec_from_file_location("_door_agreement_hook", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    if getattr(mod, "DISABLE_LLM_CLASSIFIERS", None) is not True:
        raise RuntimeError("hook LLM classifier layers are not disabled; refusing to run")

    def _refuse(*_a, **_k):
        raise RuntimeError("hook tried an LLM classifier; the no-LLM switch did not hold")

    # Belt and braces: the layers are gated off, and any call raises.
    for name in ("classify_with_ollama", "classify_with_gemini", "classify_with_openai"):
        if hasattr(mod, name):
            setattr(mod, name, _refuse)

    def classify(text: str):
        r = mod.classify_prompt(text)
        if r is None:
            return None, None
        return r.get("task_type"), r.get("complexity")

    return classify


def _load_gateway() -> Classify:
    from llm_router import gateway

    def classify(text: str):
        return gateway._classify(text)

    return classify


def _load_proxy() -> Classify:
    import asyncio

    from llm_router import router
    from llm_router.proxy import steps, tiers

    # Trees before P0.9-e (da31df7 among them) classify through choose_model,
    # which also builds the provider chain (usage.db queries, the Ollama model
    # list). The tier decision reads only (task_type, complexity), computed before
    # the chain, so the build is replaced by an empty chain. Later trees never call it.
    async def _no_chain(*_a, **_k):
        return []

    router._build_and_filter_chain = _no_chain
    loop = asyncio.new_event_loop()

    def classify(text: str):
        # The proxy classifies tier_text(body): the newest human text of the request
        # (system-reminders and tool results stripped), tail-truncated to 3000 chars.
        body = {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
        r = loop.run_until_complete(tiers._default_classify(steps.tier_text(body)))
        return r.get("task_type"), r.get("complexity")

    return classify


def _load_hook_policy() -> Classify:
    from llm_router import classify as c

    def classify(text: str):
        s = c.classify_signals(text, c.HOOK_POLICY)
        return s.task_type.value, s.complexity.value

    return classify


_HOOK_NO_LLM_ENV = {
    # da31df7 and earlier: "true" disables layers 2+3 and skips the 0.5 s Ollama
    # /api/tags probe that otherwise runs at import when the flag is unset.
    "LLM_ROUTER_CLASSIFY_LOCAL_ONLY": "true",
    # Read by the same block on old trees when CLASSIFY_LOCAL_ONLY is unset.
    "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "true",
    # P0.7-c and later: the only switch for layers 2+3; "off" is the default.
    "LLM_ROUTER_HOOK_LLM_LAYER": "off",
}

SITES: dict[str, Site] = {
    s.name: s
    for s in (
        Site(
            "hook",
            "hooks/auto-route.py classify_prompt (UserPromptSubmit door); "
            "None (too short / skip pattern) is recorded as <none>",
            _load_hook,
            no_llm_env=_HOOK_NO_LLM_ENV,
            no_llm_note="layer 2 (Ollama) and layer 3 (cloud API) forced off via env; "
            "the module's DISABLE_LLM_CLASSIFIERS must read True after import, and "
            "classify_with_ollama/gemini/openai are replaced by a function that raises",
        ),
        Site(
            "gateway",
            "gateway._classify (HTTP gateway and SDK door): classify_signals(GATEWAY_POLICY)",
            _load_gateway,
            no_llm_note="no LLM layer: heuristic only",
        ),
        Site(
            "proxy",
            "proxy/tiers.py _default_classify (turn-first tier decision) on "
            "steps.tier_text of a one-user-message body holding the prompt",
            _load_proxy,
            # Importing the router imports LiteLLM, which fetches its model cost
            # map from GitHub at import; this makes it use the bundled copy.
            no_llm_env={"LITELLM_LOCAL_MODEL_COST_MAP": "True"},
            no_llm_note="no LLM layer in the class; router._build_and_filter_chain "
            "(usage.db + Ollama model list, used by choose_model on pre-P0.9-e trees) "
            "is replaced by an empty chain because the class is computed before it; "
            "LITELLM_LOCAL_MODEL_COST_MAP=True stops LiteLLM's import-time cost-map download",
        ),
        Site(
            "hook_policy",
            "reference, not a door: classify_signals(HOOK_POLICY), the 'hook' that "
            "scripts/measure_low_signal_rate.py compared with the gateway (783/1571 on 2026-09-23)",
            _load_hook_policy,
            no_llm_note="no LLM layer: heuristic only",
        ),
    )
}


# ── Network guard ────────────────────────────────────────────────────────────

class NetworkRefused(BaseException):
    """BaseException, not Exception: a door's ``except Exception`` fallback must
    not swallow a refused connection and carry on as if nothing happened."""


#: Every refusal during the run. main() fails the run if this is non-empty, so
#: even a bare ``except:`` in a door cannot hide an attempted connection.
REFUSALS: list[str] = []


def _install_network_guard() -> Callable[[], None]:
    """Refuse DNS and every outbound connect. Returns a function that undoes it."""
    saved = {
        (socket.socket, "connect"): socket.socket.connect,
        (socket.socket, "connect_ex"): socket.socket.connect_ex,
        (socket, "create_connection"): socket.create_connection,
        (socket, "getaddrinfo"): socket.getaddrinfo,
        (socket, "gethostbyname"): socket.gethostbyname,
        (socket, "gethostbyname_ex"): socket.gethostbyname_ex,
    }

    def guard(what: str):
        def refuse(*_a, **_k):
            REFUSALS.append(what)
            raise NetworkRefused(f"door_agreement: {what} is refused during measurement")

        return refuse

    for (owner, attr) in saved:
        setattr(owner, attr, guard(attr))

    def undo() -> None:
        for (owner, attr), fn in saved.items():
            setattr(owner, attr, fn)

    return undo


# ── Statistics ───────────────────────────────────────────────────────────────

def wilson95(k: int, n: int) -> list[float]:
    if n <= 0:
        raise ValueError("wilson95 needs n > 0")
    p = k / n
    denom = 1 + Z95 * Z95 / n
    centre = (p + Z95 * Z95 / (2 * n)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 * Z95 / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


def _norm(v) -> str:
    return NONE_LABEL if v is None else str(v)


def _rate(k: int, n: int) -> dict:
    return {"n": n, "disagree": k, "rate": round(k / n, 6) if n else None,
            "wilson95": wilson95(k, n) if n else None}


def _shared(la: list[str], lb: list[str]) -> dict:
    """Disagreement on the prompts where BOTH sites' labels lie in the vocabulary
    both sites emitted on this corpus. A label only one site can produce (the
    hook's ``coordinate``) is a guaranteed disagreement; this rate leaves those
    prompts out so the two causes can be told apart."""
    vocab = sorted(set(la) & set(lb))
    idx = [i for i in range(len(la)) if la[i] in vocab and lb[i] in vocab]
    k = sum(1 for i in idx if la[i] != lb[i])
    return {"vocab": vocab, **_rate(k, len(idx))}


def measure(prompts: list[str], classifiers: dict[str, Classify],
            buckets: list[str] | None = None) -> dict:
    """Counts only. ``classifiers`` maps site name -> classify(text). ``buckets``
    (one label per prompt, e.g. a date window) adds a per-bucket task_type split."""
    n = len(prompts)
    if n == 0:
        raise ValueError("empty corpus")
    if buckets is not None and len(buckets) != n:
        raise ValueError("buckets must have one entry per prompt")
    names = list(classifiers)
    labels: dict[str, list[tuple[str, str]]] = {}
    for name in names:
        fn = classifiers[name]
        out = []
        for p in prompts:
            t, tier = fn(p)
            out.append((_norm(t), _norm(tier)))
        labels[name] = out

    pairs = {}
    for a, b in combinations(names, 2):
        ta, tb = [x[0] for x in labels[a]], [x[0] for x in labels[b]]
        ra, rb = [x[1] for x in labels[a]], [x[1] for x in labels[b]]
        tt = sum(1 for x, y in zip(ta, tb) if x != y)
        ti = sum(1 for x, y in zip(ra, rb) if x != y)
        pair = {
            "task_type_disagree": tt,
            "tier_disagree": ti,
            "rate": round(tt / n, 6),
            "wilson95": wilson95(tt, n),
            "tier_rate": round(ti / n, 6),
            "tier_wilson95": wilson95(ti, n),
            "shared_labels_task_type": _shared(ta, tb),
            "shared_labels_tier": _shared(ra, rb),
        }
        if buckets is not None:
            split = {}
            for bucket in sorted(set(buckets)):
                idx = [i for i in range(n) if buckets[i] == bucket]
                split[bucket] = _rate(sum(1 for i in idx if ta[i] != tb[i]), len(idx))
            pair["task_type_by_bucket"] = split
        pairs[f"{a}|{b}"] = pair

    all_tt = sum(1 for i in range(n) if len({labels[s][i][0] for s in names}) > 1)
    all_ti = sum(1 for i in range(n) if len({labels[s][i][1] for s in names}) > 1)
    per_site = {}
    for s in names:
        tt_counts: dict[str, int] = {}
        ti_counts: dict[str, int] = {}
        for t, tier in labels[s]:
            tt_counts[t] = tt_counts.get(t, 0) + 1
            ti_counts[tier] = ti_counts.get(tier, 0) + 1
        per_site[s] = {
            "task_type_counts": dict(sorted(tt_counts.items())),
            "tier_counts": dict(sorted(ti_counts.items())),
        }
    result = {
        "n": n,
        "pairs": pairs,
        "all_sites_disagree": all_tt,
        "all_sites_rate": round(all_tt / n, 6),
        "all_sites_wilson95": wilson95(all_tt, n),
        "all_sites_tier_disagree": all_ti,
        "all_sites_tier_rate": round(all_ti / n, 6),
        "per_site": per_site,
    }
    if buckets is not None:
        result["bucket_counts"] = {b: buckets.count(b) for b in sorted(set(buckets))}
    return result


# ── Corpus ───────────────────────────────────────────────────────────────────

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_corpus(path: Path) -> list[str]:
    prompts = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            text = obj.get("text") if isinstance(obj, dict) else None
            if not isinstance(text, str):
                raise ValueError(f"corpus line {line_no}: no string 'text' field")
            prompts.append(text)
    return prompts


def _measure_module():
    m = sys.modules.get("measure_low_signal_rate")
    if m is None:
        src = Path(__file__).resolve().parent / "measure_low_signal_rate.py"
        spec = importlib.util.spec_from_file_location("measure_low_signal_rate", src)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    return m


def write_exclusive(path: Path, lines) -> None:
    """Write ``lines`` to ``path`` mode 0600 without ever overwriting it and
    without ever leaving a partial file at ``path``: write a temp file in the
    same directory, fsync, then hard-link it into place (``link`` fails if
    ``path`` exists, and is atomic). Raises FileExistsError if it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        os.unlink(tmp)


def freeze_corpus(path: Path) -> int:
    """Write measure_low_signal_rate.collect()'s kept prompts, 0600, never overwriting."""
    if path.exists():
        print(f"refusing to overwrite existing corpus: {path}", file=sys.stderr)
        return 3
    prompts, drops = _measure_module().collect()
    try:
        write_exclusive(path, (json.dumps({"i": i, "text": t}, ensure_ascii=False) + "\n"
                               for i, t in enumerate(prompts)))
    except FileExistsError:
        print(f"refusing to overwrite existing corpus: {path}", file=sys.stderr)
        return 3
    print(json.dumps({
        "corpus": str(path),
        "n": len(prompts),
        "dropped": sum(drops.values()),
        "sha256": file_sha256(path),
        "mode": oct(path.stat().st_mode & 0o777),
    }))
    return 0


def _utc_date(ts) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()


def freeze_dates(corpus: Path, path: Path) -> int:
    """A counts-only date sidecar for a frozen corpus: one ``{"i", "date"}`` per
    corpus line, no text and no text hash, aligned by line. Dates come from a
    fresh ``collect_records()`` run, matched to corpus lines by the sha256 of the
    text in memory only; a text seen more than once gets its earliest UTC date,
    and a corpus line no current record matches gets ``null``. The first line is
    ``{"meta": {...}}`` naming the corpus sha256 it belongs to."""
    if path.exists():
        print(f"refusing to overwrite existing sidecar: {path}", file=sys.stderr)
        return 3
    records, _ = _measure_module().collect_records()
    earliest: dict[str, str] = {}
    for rec in records:
        d = _utc_date(rec.ts)
        if d is None:
            continue
        h = hashlib.sha256(rec.text.encode("utf-8")).hexdigest()
        if h not in earliest or d < earliest[h]:
            earliest[h] = d
    texts = read_corpus(corpus)
    dates = [earliest.get(hashlib.sha256(t.encode("utf-8")).hexdigest()) for t in texts]
    meta = {"meta": {
        "corpus_sha256": file_sha256(corpus),
        "n": len(texts),
        "dated": sum(1 for d in dates if d),
        "date_basis": "UTC date of the collected record's ts; earliest if a text repeats; "
                      "null if no record collected now matches the line",
    }}
    try:
        write_exclusive(path, [json.dumps(meta) + "\n"] + [
            json.dumps({"i": i, "date": d}) + "\n" for i, d in enumerate(dates)])
    except FileExistsError:
        print(f"refusing to overwrite existing sidecar: {path}", file=sys.stderr)
        return 3
    print(json.dumps({**meta["meta"], "sidecar": str(path), "sha256": file_sha256(path)}))
    return 0


def read_dates(path: Path, corpus_sha256: str, n: int) -> list[str | None]:
    with path.open(encoding="utf-8") as fh:
        meta = json.loads(fh.readline())["meta"]
        if meta["corpus_sha256"] != corpus_sha256:
            raise ValueError("date sidecar belongs to a different corpus")
        dates = [json.loads(line)["date"] for line in fh if line.strip()]
    if len(dates) != n:
        raise ValueError(f"date sidecar has {len(dates)} rows, corpus has {n}")
    return dates


def date_buckets(dates: list[str | None], cutoff: str) -> list[str]:
    return ["undated" if d is None else (f"<={cutoff}" if d <= cutoff else f">{cutoff}")
            for d in dates]


# ── Provenance ───────────────────────────────────────────────────────────────

def _git_head(where: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(where), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — provenance is best effort, never fatal
        return None


def _git_dirty(where: Path) -> bool | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(where), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--freeze-corpus", type=Path, help="collect and write the corpus (0600), then exit")
    ap.add_argument("--freeze-dates", type=Path,
                    help="with --corpus: write a counts-only per-line date sidecar (0600), then exit")
    ap.add_argument("--corpus", type=Path)
    ap.add_argument("--sites", default="hook,gateway", help=f"comma list from: {','.join(SITES)}")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dates", type=Path, help="date sidecar from --freeze-dates")
    ap.add_argument("--date-cutoff", default="2026-09-23", help="YYYY-MM-DD, with --dates")
    args = ap.parse_args(argv)

    if args.freeze_corpus:
        return freeze_corpus(args.freeze_corpus)
    if args.freeze_dates:
        if not args.corpus:
            ap.error("--freeze-dates needs --corpus")
        return freeze_dates(args.corpus, args.freeze_dates)
    if not args.corpus or not args.out:
        ap.error("--corpus and --out are required (or --freeze-corpus)")

    names = [s.strip() for s in args.sites.split(",") if s.strip()]
    unknown = [s for s in names if s not in SITES]
    if unknown or len(names) < 2:
        print(f"need >= 2 known sites; unknown: {unknown}; known: {list(SITES)}", file=sys.stderr)
        return 2

    prompts = read_corpus(args.corpus)
    if not prompts:
        print(f"empty corpus: {args.corpus}", file=sys.stderr)
        return 2
    corpus_sha = file_sha256(args.corpus)
    buckets = None
    if args.dates:
        buckets = date_buckets(read_dates(args.dates, corpus_sha, len(prompts)), args.date_cutoff)

    switches: dict[str, dict] = {}
    for s in names:
        for var, val in SITES[s].no_llm_env.items():
            os.environ[var] = val
        switches[s] = {"env": dict(SITES[s].no_llm_env), "why": SITES[s].no_llm_note}

    _install_network_guard()
    classifiers = {s: SITES[s].load() for s in names}
    result = measure(prompts, classifiers, buckets)
    if REFUSALS:
        print(f"{len(REFUSALS)} network attempt(s) refused ({sorted(set(REFUSALS))}); "
              "a no-network switch did not hold, no output written", file=sys.stderr)
        return 4

    import llm_router

    pkg_root = Path(llm_router.__file__).resolve().parent
    tool_dir = Path(__file__).resolve().parent
    out = {
        "n": result["n"],
        "corpus_sha256": corpus_sha,
        "git_sha": _git_head(pkg_root),
        "git_dirty": _git_dirty(pkg_root),
        "tool_git_sha": _git_head(tool_dir),
        "sites": {s: SITES[s].description for s in names},
        "labels": {
            "none_label": NONE_LABEL,
            "none_meaning": "the site declined to classify (hook classify_prompt returns None for "
                            "text under 8 chars after strip or matching SKIP_PATTERNS); counted as "
                            "its own label, so it disagrees with every site that did classify",
            "shared_labels": "shared_labels_* rates count only prompts where both sites' labels lie in "
                             "the vocabulary both sites emitted on this corpus",
        },
        "no_llm_switches": switches,
        "network_guard": "DNS (getaddrinfo, gethostbyname[_ex]) and socket connect/connect_ex/"
                         "create_connection refused for the whole run; any attempt fails the run",
        "network_refusals": len(REFUSALS),
        **({"date_cutoff": args.date_cutoff, "dates_sha256": file_sha256(args.dates)} if args.dates else {}),
        **{k: v for k, v in result.items() if k != "n"},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    for pair, v in out["pairs"].items():
        lo, hi = v["wilson95"]
        sh = v["shared_labels_task_type"]
        print(f"{pair}: task_type {v['task_type_disagree']}/{out['n']} = {100 * v['rate']:.1f}% "
              f"[{100 * lo:.1f}, {100 * hi:.1f}]; shared-labels {sh['disagree']}/{sh['n']}; "
              f"tier {v['tier_disagree']}/{out['n']}")
    print(f"all sites task_type disagree: {out['all_sites_disagree']}/{out['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
