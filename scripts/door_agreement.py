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
import hashlib
import importlib.util
import json
import math
import os
import socket
import subprocess
import sys
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
            "hook_policy",
            "reference, not a door: classify_signals(HOOK_POLICY), the 'hook' that "
            "scripts/measure_low_signal_rate.py compared with the gateway (783/1571 on 2026-09-23)",
            _load_hook_policy,
            no_llm_note="no LLM layer: heuristic only",
        ),
    )
}


# ── Network guard ────────────────────────────────────────────────────────────

class NetworkRefused(RuntimeError):
    pass


def _install_network_guard() -> None:
    def refuse(*_a, **_k):
        raise NetworkRefused("door_agreement: outbound network is refused during measurement")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]


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


def measure(prompts: list[str], classifiers: dict[str, Classify]) -> dict:
    """Counts only. ``classifiers`` maps site name -> classify(text)."""
    n = len(prompts)
    if n == 0:
        raise ValueError("empty corpus")
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
        tt = sum(1 for x, y in zip(labels[a], labels[b]) if x[0] != y[0])
        ti = sum(1 for x, y in zip(labels[a], labels[b]) if x[1] != y[1])
        pairs[f"{a}|{b}"] = {
            "task_type_disagree": tt,
            "tier_disagree": ti,
            "rate": round(tt / n, 6),
            "wilson95": wilson95(tt, n),
            "tier_rate": round(ti / n, 6),
            "tier_wilson95": wilson95(ti, n),
        }

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
    return {
        "n": n,
        "pairs": pairs,
        "all_sites_disagree": all_tt,
        "all_sites_rate": round(all_tt / n, 6),
        "all_sites_wilson95": wilson95(all_tt, n),
        "all_sites_tier_disagree": all_ti,
        "all_sites_tier_rate": round(all_ti / n, 6),
        "per_site": per_site,
    }


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


def freeze_corpus(path: Path) -> int:
    """Write measure_low_signal_rate.collect()'s kept prompts, 0600, never overwriting."""
    m = sys.modules.get("measure_low_signal_rate")
    if m is None:
        src = Path(__file__).resolve().parent / "measure_low_signal_rate.py"
        spec = importlib.util.spec_from_file_location("measure_low_signal_rate", src)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

    prompts, drops = m.collect()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"refusing to overwrite existing corpus: {path}", file=sys.stderr)
        return 3
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for i, text in enumerate(prompts):
            fh.write(json.dumps({"i": i, "text": text}, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)
    print(json.dumps({
        "corpus": str(path),
        "n": len(prompts),
        "dropped": sum(drops.values()),
        "sha256": file_sha256(path),
        "mode": oct(path.stat().st_mode & 0o777),
    }))
    return 0


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
    ap.add_argument("--corpus", type=Path)
    ap.add_argument("--sites", default="hook,gateway", help=f"comma list from: {','.join(SITES)}")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.freeze_corpus:
        return freeze_corpus(args.freeze_corpus)
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

    switches: dict[str, dict] = {}
    for s in names:
        for var, val in SITES[s].no_llm_env.items():
            os.environ[var] = val
        switches[s] = {"env": dict(SITES[s].no_llm_env), "why": SITES[s].no_llm_note}

    _install_network_guard()
    classifiers = {s: SITES[s].load() for s in names}
    result = measure(prompts, classifiers)

    import llm_router

    pkg_root = Path(llm_router.__file__).resolve().parent
    tool_dir = Path(__file__).resolve().parent
    out = {
        "n": result["n"],
        "corpus_sha256": file_sha256(args.corpus),
        "git_sha": _git_head(pkg_root),
        "git_dirty": _git_dirty(pkg_root),
        "tool_git_sha": _git_head(tool_dir),
        "sites": {s: SITES[s].description for s in names},
        "no_llm_switches": switches,
        "network_guard": "socket connect/connect_ex/create_connection refused for the whole run",
        **{k: v for k, v in result.items() if k != "n"},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    for pair, v in out["pairs"].items():
        lo, hi = v["wilson95"]
        print(f"{pair}: task_type {v['task_type_disagree']}/{out['n']} = {100 * v['rate']:.1f}% "
              f"[{100 * lo:.1f}, {100 * hi:.1f}]; tier {v['tier_disagree']}/{out['n']}")
    print(f"all sites task_type disagree: {out['all_sites_disagree']}/{out['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
