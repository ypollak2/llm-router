"""`llm-router local-check` -- measure YOUR local model on the bundled public benchmark (LC-6, D-259/D-260/D-263/D-264).

    llm-router local-check [run] [--dry-run] [--no-judge] [--yes] [--model M] [--endpoint URL [--allow-remote]]
                           [--profile quick|standard] [--fresh] [--force] [--judge-route auto|api|subscription]
    llm-router local-check {run,status,explain,enable,disable} --help
    llm-router local-check status [--model M]
    llm-router local-check explain [--model M]
    llm-router local-check enable <lane> [--model M]
    llm-router local-check disable <lane> [--model M]

``run`` discovers a local runtime, shows a plan screen (model + digest, hardware, items in scope, time, judge route,
cost, cap, consent), runs the local arm serially, has Claude judge the answers that pass the programmatic grader, and records
a verdict through ``local_check.session``. Only public benchmark text and the local model's answers to it are ever sent to
Anthropic; no user prompt or file is sent. After a recorded PASS the lanes are switched on for routing through
``local_check.routing`` (D-260; a lane the user disabled stays off); ``enable`` / ``disable`` flip one lane by hand.

All collaborators are injectable (``Deps``) so the tests run with fakes and no network.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

# ---- exit codes (documented in docs/local-check.md)
EXIT_OK = 0              # PASS, a dry run, status/explain
EXIT_NOT_PASSED = 1      # completed: FAIL / NOT_PASSED / INCONCLUSIVE / PROBE, or the user declined consent
EXIT_USAGE = 2
EXIT_BANK = 3            # the bundled bank failed its integrity check
EXIT_NO_RUNTIME = 4      # no runtime / model found, or the endpoint was refused
EXIT_QUOTA = 5           # subscription quota above 95% (override with --force)
EXIT_REFUSED = 6         # anti-retry, cooldown, scope too narrow (< 50 items fit), run dir mismatch
EXIT_NO_JUDGE_ROUTE = 7  # no usable judge route (see --no-judge), or the judge cannot be used safely
EXIT_PROBE_FAILED = 8    # the runtime did not answer a probe (digest / context window)
EXIT_BUDGET = 10         # judge budget cap reached: INCONCLUSIVE, resumable
EXIT_LOCK = 11           # another local-check run holds the Ollama lock
EXIT_RECORD = 12         # the eligibility store refused the record; the run stays reserved
EXIT_INVALID = 13        # a validity gate failed: nothing recorded
EXIT_INTERRUPTED = 130   # Ctrl-C: progress is saved, re-run to resume

QUICK_MAX_CHARS = 12_000   # --profile quick: diffs up to 12k characters only
ROUTE_AUTO, ROUTE_API, ROUTE_SUB = "auto", "api", "subscription"


@dataclass
class Deps:
    """Everything outside the process, injectable. ``None`` fields are filled with the real thing in ``_resolve``."""
    environ: Mapping[str, str] | None = None
    which: Callable[[str], str | None] = shutil.which
    config_key: Callable[[], str] | None = None
    interactive: bool | None = None                 # None: stdin and stdout are TTYs
    input_fn: Callable[[str], str] = input
    out: Callable[[str], None] = print
    err: Callable[[str], None] = lambda s: print(s, file=sys.stderr)
    discover: Callable[..., list] | None = None
    make_adapter: Callable[..., Any] | None = None
    load_bank: Callable[[], Any] | None = None
    hardware: Callable[[], dict] | None = None
    quota_reader: Callable[[], tuple] | None = None
    proxy_ports: Callable[[], set[int]] | None = None
    api_backend: Callable[..., Any] | None = None   # (model, key, configured_base_url) -> backend
    cli_backend: Callable[..., Any] | None = None   # (model, claude_bin) -> backend
    now: Callable[[], float] = time.time
    runs_root: Path | None = None
    eligibility_path: Path | None = None
    lock_path: Path | None = None
    routing: Any = None                             # the routing API module (on_pass / enable / disable / ...); default local_check.routing
    routing_path: Path | None = None                # local_routing.json override (tests)
    prune: Callable[..., Any] | None = None         # retention.prune(root) -> PruneReport
    recommend: Callable[..., list] | None = None    # recommend.recommend(hardware, runtime, model, result)
    inventory: Callable[[], list[str]] | None = None  # installed local model names with tag (for enable / disable)
    status_lines: Callable[[], list] | None = None  # display.lines(path=...) -> [LCLine]

#: The lanes a local-check run measures and routing can switch.
LANES = ("commit_message", "pr_description")
#: Judge calls between two subscription-quota re-checks (one "batch").
QUOTA_BATCH = 10


# ------------------------------------------------------------------------------------------ judge route (D-260, D-264)

@dataclass
class Route:
    kind: str = ""                  # "api" | "subscription" | "" (none)
    api_key: str = ""
    configured_base_url: str = ""   # passed to ApiBackend; "" = direct to api.anthropic.com
    claude_bin: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""


def own_proxy_ports(environ: Mapping[str, str]) -> set[int]:
    """Ports llm-router's own proxy (and its fail-open shim) listens on: the install sentinel, LLM_ROUTER_PROXY_PORT, then the defaults."""
    from llm_router import proxy_default as pd
    ports = {pd.DEFAULT_PORT, pd.DEFAULT_UPSTREAM_PORT}
    try:
        sentinel = pd.read_sentinel() or {}
    except Exception:  # noqa: BLE001 - a broken sentinel must not break route selection
        sentinel = {}
    for v in (sentinel.get("port"), sentinel.get("upstream_port"), environ.get("LLM_ROUTER_PROXY_PORT")):
        try:
            if v not in (None, ""):
                ports.add(int(v))
        except (TypeError, ValueError):
            pass
    return ports


def points_at_own_proxy(url: str, ports: set[int]) -> bool:
    try:
        u = urlsplit(url.strip())
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return False
    return u.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1") and port in ports


def select_route(environ: Mapping[str, str], which: Callable[[str], str | None], ports: set[int], *, want: str = ROUTE_AUTO,
                 config_key: Callable[[], str] | None = None) -> Route:
    """D-260: the user's Anthropic key -> API route; else the ``claude`` CLI -> subscription route; else nothing (--no-judge only).

    D-264: when ANTHROPIC_BASE_URL is llm-router's own local proxy the judge goes direct to api.anthropic.com
    (``configured_base_url=""``); any other custom base URL refuses the API route (the key and prompts would leave for another host).
    """
    from llm_router.local_check import judge as J
    r = Route()
    key = J.resolve_api_key(dict(environ), config_key) if want != ROUTE_SUB else ""
    base = J.resolve_anthropic_base_url(dict(environ))
    claude = which("claude") or ""
    if want == ROUTE_API and not key:
        r.error = "no Anthropic API key (set ANTHROPIC_API_KEY or configure the Anthropic provider), so --judge-route api is not available."
        return r
    if key and want in (ROUTE_AUTO, ROUTE_API):
        if base and not J.is_anthropic_url(base):
            if points_at_own_proxy(base, ports):
                r.notes.append("ANTHROPIC_BASE_URL points at llm-router's own local proxy; the judge calls go direct to api.anthropic.com, not through it.")
            else:
                r.error = (f"ANTHROPIC_BASE_URL points at a custom host ({urlsplit(base).hostname or 'unparseable'}); the judge sends your key and prompts "
                           "only to api.anthropic.com, so the API route is refused. Use the Claude subscription route instead: "
                           "re-run with --judge-route subscription (needs the claude CLI), or use --no-judge.")
                return r
        r.kind, r.api_key, r.configured_base_url = ROUTE_API, key, ""
        return r
    if claude:
        r.kind, r.claude_bin = ROUTE_SUB, claude
        return r
    r.error = ("no judge available: set ANTHROPIC_API_KEY (API route) or install and log in to the claude CLI (subscription route). "
               "Without a judge only `--no-judge` works: programmatic checks only; the result is shown in `status` as a probe and never records a verdict or enables routing.")
    return r


# ------------------------------------------------------------------------------------------ subscription quota between batches

def _quota_budget(*, force: bool, reader: Callable[[], tuple] | None, notify: Callable[[str], None]) -> Any:
    """A call-capped Budget that re-reads the subscription quota every QUOTA_BATCH judge calls (LC-4b: the start-of-run check is one-shot).

    Above 95% (unless --force) ``check`` raises BudgetExhausted, which the session turns into the resumable budget stop; above 80% the
    user is warned once. An unreadable quota never stops the run.
    """
    from llm_router.local_check import budget as B

    @dataclass
    class QuotaBudget(B.Budget):
        _last: int = field(default=0, repr=False)
        _warned: bool = field(default=False, repr=False)

        def check(self) -> None:
            super().check()
            if self.calls - self._last >= QUOTA_BATCH:
                self._last = self.calls
                dec = B.recheck_quota(force=force, reader=reader)
                if dec.action == "warn" and not self._warned:
                    self._warned = True
                    notify("Note: " + dec.message)

    return QuotaBudget(metered=False)


# ------------------------------------------------------------------------------------------ plan

class _ProfileAdapter:
    """``--profile quick``: the same adapter, but only diffs up to ``QUICK_MAX_CHARS`` are in scope."""

    def __init__(self, inner: Any, cap: int):
        self._inner, self._cap = inner, cap

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def max_prompt_chars(self, model: str) -> int:
        m = self._inner.max_prompt_chars(model)
        return self._cap if m is None else min(m, self._cap)


@dataclass
class PlanInfo:
    key: str = ""
    digest: str | None = None
    settings: str = ""
    max_chars: int | None = None
    draw: int | None = None
    n_drawn: int = 0
    n_fit: int = 0
    n_prior: int = 0
    per_lane: dict[str, int] = field(default_factory=dict)
    refused: str = ""
    resume: str = ""
    estimate_s: float | None = None
    prior_scored: int = 0


def _fmt_s(sec: float) -> str:
    if sec < 90:
        return f"{sec:.0f} s"
    if sec < 5400:
        return f"{sec / 60:.0f} min"
    return f"{sec / 3600:.1f} h"


def estimate_run_seconds(model: str, n_items: int, hardware: Mapping) -> tuple[float | None, str]:
    """Size-class estimate only; no runtime calls or measurements."""
    size = re.search(r"(?<![A-Za-z0-9.])(?:(\d+)x)?(\d+(?:\.\d+)?)b(?![A-Za-z0-9])", model, re.IGNORECASE)
    if size is None:
        return None, ""
    billions = float(size.group(1) or 1) * float(size.group(2))
    for limit, per_item, label in ((4, 8, "<=4B"), (9, 20, "4-9B"),
                                    (16, 40, "9-16B"), (40, 90, "16-40B"),
                                    (float("inf"), 180, ">40B")):
        if billions <= limit:
            gpu = hardware.get("gpu")
            factor = 5 if gpu is not None and str(gpu).lower() == "none" else 1
            if gpu is None or gpu == "":
                label += " (GPU unknown)"
            return float(n_items * per_item * factor), label
    return None, ""


def build_plan(d: Deps, adapter: Any, model: str, bank: Any, runs_root: Path, *, no_judge: bool, force: bool) -> PlanInfo:
    from llm_router.local_check import attempts as A, runner as R, score as S
    p = PlanInfo()
    p.digest = adapter.digest(model)
    p.settings = adapter.settings_hash(model)
    p.max_chars = adapter.max_prompt_chars(model)
    p.key = A.config_key(model, p.digest, bank.version, bank.sha256, p.settings)
    now = d.now()
    plan = A.Plan(1) if no_judge else A.AttemptLog(runs_root).plan(p.key, now, force=force)
    if plan.draw is None:
        p.refused = plan.refused or "refused"
        return p
    p.draw = plan.draw
    items = bank.draw(plan.draw)
    fit = [i for i in items if p.max_chars is None or len(i.task) <= p.max_chars]
    p.n_drawn, p.n_fit, p.prior_scored = len(items), len(fit), len(plan.prior)
    prior_rows = [S.Row.from_dict(r) for r in plan.prior_rows]
    p.n_prior = len(A.union_ids(plan.prior_assigned, [i.id for i in fit]))
    lanes = Counter(i.job for i in fit) + Counter(r.job for r in prior_rows if r.present)
    p.per_lane = dict(sorted(lanes.items()))
    thin = [j for j in sorted({i.job for i in bank.draw(1)}) if lanes[j] < S.MIN_LANE_N]
    if p.n_prior < S.MIN_N or thin:
        p.refused = (f"scope too narrow: only {p.n_prior} of this draw's {p.n_drawn} items fit the model's context window"
                     f"{f' (max prompt ~{p.max_chars} characters)' if p.max_chars else ''}. A verdict needs at least {S.MIN_N} items"
                     f" and {S.MIN_LANE_N} per lane{f' (thin: {chr(44).join(thin)})' if thin else ''}. "
                     "Raise num_ctx (for Ollama: `OLLAMA_CONTEXT_LENGTH` or a Modelfile `PARAMETER num_ctx`), use a longer-context model, or drop --profile quick.")
        return p
    resv = plan.reservation
    if resv:
        p.resume = resv["run_id"]
        try:
            recs = R.RunStore(runs_root / resv["run_id"]).load()
        except (OSError, R.RunMismatch):
            recs = []
        done = {r["item_id"] for r in recs}
        pend = [R.SimpleItem(i.id, i.task) for i in fit if i.id not in done]
        p.estimate_s = R.estimate_seconds(pend, R.measure_rate(recs)) if recs else None
    return p


def _hardware_line(hw: Mapping[str, Any]) -> str:
    """RAM with the memory a model can use: total minus the reserve (recommend.capacity_gb), never the momentary free memory."""
    from llm_router.local_check.recommend import capacity_gb
    bits = []
    if hw.get("total_gb") is not None:
        cap = capacity_gb(hw)
        use = f", {cap:g} GB usable for a model" if cap is not None else ""
        bits.append(f"RAM {hw['total_gb']} GB{use}")
    gpu = hw.get("gpu")
    if gpu:
        bits.append(f"GPU {gpu}" + (f" {hw['vram_gb']} GB" if hw.get("vram_gb") is not None else ""))
    if hw.get("arch"):
        bits.append(str(hw["arch"]))
    return ", ".join(bits) or "not detected"


# ------------------------------------------------------------------------------------------ run

def _resolve(d: Deps) -> Deps:
    import os
    from llm_router import paths
    from llm_router.local_check import judge as J
    from llm_router.local_check.bank import load_bank
    from llm_router.local_check import runtimes
    from llm_router.resolver import eligibility_store
    if d.environ is None:
        d.environ = os.environ
    if d.interactive is None:
        d.interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    if d.discover is None:
        d.discover = runtimes.discover
    if d.make_adapter is None:
        d.make_adapter = runtimes.make_adapter
    if d.load_bank is None:
        d.load_bank = load_bank
    if d.hardware is None:
        def _hw() -> dict:
            from llm_router import hardware
            return hardware.probe_cached()
        d.hardware = _hw
    if d.proxy_ports is None:
        d.proxy_ports = lambda: own_proxy_ports(d.environ)       # type: ignore[arg-type]
    if d.api_backend is None:
        d.api_backend = lambda model, key, base: J.ApiBackend(model, key, configured_base_url=base)
    if d.cli_backend is None:
        d.cli_backend = lambda model, claude_bin: J.ClaudeCliBackend(model, claude_bin=claude_bin)
    if d.runs_root is None:
        d.runs_root = paths.state_path("local_check", "runs")
    if d.eligibility_path is None:
        d.eligibility_path = eligibility_store.eligibility_path()
    if d.lock_path is None:
        lock = paths.state_path("ollama.lock")
        d.lock_path = lock if lock.exists() else None
    if d.routing is None:
        from llm_router.local_check import routing
        d.routing = routing
    if d.prune is None:
        from llm_router.local_check import retention
        d.prune = retention.prune
    if d.recommend is None:
        from llm_router.local_check import recommend
        d.recommend = recommend.recommend
    if d.inventory is None:
        def _inv() -> list[str]:
            from llm_router.local_check import display
            return display.inventory()[0]
        d.inventory = _inv
    if d.status_lines is None:
        def _lines() -> list:
            from llm_router.local_check import display
            return display.lines(path=Path(d.eligibility_path), now=d.now())
        d.status_lines = _lines
    return d


def _parser(prog: str, run: bool = True, lane: bool = False) -> argparse.ArgumentParser:
    epilog = """subcommands:
  run      measure your local model (default)
  status   show recorded lanes, routing switches and draws used
  explain  show which grader rules failed in the last run, by item id
  enable   switch routing on for a lane (enable <lane> [--model M])
  disable  switch routing off for a lane"""
    ap = argparse.ArgumentParser(
        prog=prog, add_help=True, description="Measure your local model on the bundled public benchmark.",
        epilog=epilog if run else None, formatter_class=argparse.RawDescriptionHelpFormatter)
    if lane:
        ap.add_argument("lane", choices=LANES, help="the text-job lane to switch")
    ap.add_argument("--model", help="local model name with its tag (default: the only one found; asked for when several)")
    if run:
        ap.add_argument("--endpoint", help="runtime URL (default: auto-discover Ollama, LM Studio, vLLM, llama.cpp on loopback)")
        ap.add_argument("--allow-remote", action="store_true", help="allow a non-loopback --endpoint (the public bank prompts leave this machine for it)")
        ap.add_argument("--profile", choices=("quick", "standard"), default="standard", help="quick: only diffs up to 12k characters (narrower scope)")
        ap.add_argument("--dry-run", action="store_true", help="show the plan screen only; zero model calls")
        ap.add_argument("--no-judge", action="store_true", help="programmatic checks only; the result is shown in `status` as a probe and never records a verdict or enables routing")
        ap.add_argument("--yes", "-y", action="store_true", help="consent without a prompt (required when not on a terminal)")
        ap.add_argument("--fresh", action="store_true", help="discard the open (resumable) run of this configuration and start a new one (counts as a strike)")
        ap.add_argument("--force", action="store_true", help="override the 24 h cooldown and the subscription-quota refusal (>95%%)")
        ap.add_argument("--judge-route", choices=(ROUTE_AUTO, ROUTE_API, ROUTE_SUB), default=ROUTE_AUTO,
                        help="auto: your Anthropic API key if set, else the claude CLI (default)")
    return ap


def _pick_model(d: Deps, endpoints: list, wanted: str | None) -> tuple[Any, str] | str:
    """(endpoint, model) or an error string."""
    pairs, seen = [], set()
    for e in endpoints:
        for m in e.models:
            if (m, e.runtime, e.base_url) not in seen:       # one model listed twice by a runtime is still one model
                seen.add((m, e.runtime, e.base_url))
                pairs.append((e, m))
    if wanted:
        hit = [(e, m) for e, m in pairs if m == wanted]
        if not hit:
            shown = ", ".join(sorted({m for _, m in pairs})[:12]) or "none"
            for e in endpoints:
                if wanted in getattr(e, "unmeasurable", []):
                    return (f"model {wanted!r} is installed on {e.runtime} {e.base_url} but is not a chat/completion model "
                            f"(embedding or decision-only), so local-check cannot measure it. Measurable models: {shown}.")
            return f"model {wanted!r} was not found on {', '.join(e.runtime + ' ' + e.base_url for e in endpoints)}. Models found: {shown}."
        return hit[0]
    if not pairs:
        return "a runtime answered but reports no chat models; pull one first (for Ollama: `ollama pull <model>`)."
    if len(pairs) == 1:
        return pairs[0]
    if not d.interactive:
        return "several models found (" + ", ".join(f"{m}@{e.runtime}" for e, m in pairs[:12]) + "); pass --model."
    d.out("Local models found:")
    for n, (e, m) in enumerate(pairs, 1):
        d.out(f"  {n}. {m}  ({e.runtime})")
    try:
        choice = int(d.input_fn("Model number: ").strip())
        return pairs[choice - 1]
    except (ValueError, IndexError, EOFError):
        return "no model chosen; pass --model."


def cmd_local_check(argv: list[str], deps: Deps | None = None) -> int:
    d = _resolve(deps or Deps())
    args = list(argv)
    sub = "run"
    if args and args[0] in ("run", "status", "explain", "enable", "disable"):
        sub = args.pop(0)
    elif args and not args[0].startswith("-"):
        d.err(f"llm-router local-check: unknown subcommand {args[0]!r} (run, status, explain, enable, disable)")
        return EXIT_USAGE
    prog = ("llm-router local-check [run|status|explain|enable|disable]" if sub == "run"
            else f"llm-router local-check {sub}")
    ap = _parser(prog, run=(sub == "run"), lane=sub in ("enable", "disable"))
    if sub == "run" and any(a in ("--help", "-h") for a in args):
        d.out(ap.format_help())
        return EXIT_OK
    try:
        ns = ap.parse_args(args)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else EXIT_USAGE
    if sub in ("enable", "disable"):
        return _switch(d, sub, ns.lane, ns.model)
    if sub == "status":
        return _status(d, ns.model)
    if sub == "explain":
        return _explain(d, ns.model)
    return _run(d, ns)


def _run(d: Deps, ns: argparse.Namespace) -> int:
    from llm_router.local_check import budget as B, consent as C, judge as J, runner as R, runtimes, session as SE
    from llm_router.local_check.bank import BankError
    out, err = d.out, d.err
    if ns.allow_remote and not ns.endpoint:
        err("--allow-remote only applies together with an explicit --endpoint.")
        return EXIT_USAGE
    try:
        bank = d.load_bank()
    except BankError as e:
        err(f"The bundled benchmark failed its integrity check ({e}). Reinstall llm-router.")
        return EXIT_BANK

    # 1. runtime + model
    try:
        endpoints = d.discover(ns.endpoint, allow_remote=bool(ns.allow_remote))
    except runtimes.RuntimeError_ as e:
        err(f"Endpoint refused: {e}. A non-loopback endpoint needs --allow-remote (the benchmark prompts would leave this machine).")
        return EXIT_NO_RUNTIME
    if not endpoints:
        err("No local runtime found (looked for Ollama, LM Studio, vLLM, llama.cpp on loopback)." if not ns.endpoint
            else f"Nothing answers as a model runtime at {ns.endpoint}.")
        err("Start your runtime, or point to it with --endpoint URL.")
        return EXIT_NO_RUNTIME
    picked = _pick_model(d, endpoints, ns.model)
    if isinstance(picked, str):
        err(picked)
        return EXIT_NO_RUNTIME
    endpoint, model = picked
    adapter = d.make_adapter(endpoint, allow_remote=bool(ns.allow_remote))
    if ns.profile == "quick":
        adapter = _ProfileAdapter(adapter, QUICK_MAX_CHARS)

    # 2. plan
    runs_root = Path(d.runs_root)
    try:
        plan = build_plan(d, adapter, model, bank, runs_root, no_judge=ns.no_judge, force=ns.force)
    except runtimes.ProbeFailed as e:
        err(f"The runtime did not answer a probe for {model!r} ({e}). Check that {endpoint.runtime} is running and the model loaded, then retry.")
        return EXIT_PROBE_FAILED

    # 3. judge route + cost
    route, quota, est_usd, judge_model = Route(), None, None, bank.manifest.get("judge_id") or ""
    if not ns.no_judge:
        route = select_route(d.environ, d.which, d.proxy_ports(), want=ns.judge_route, config_key=d.config_key)
        if route.kind == ROUTE_API:
            try:
                d.api_backend(judge_model, route.api_key, route.configured_base_url)       # validates pricing / URL now, before any consent
            except J.JudgeSetupError as e:
                route.error, route.kind = str(e), ""
            else:
                est_usd = B.estimate_run_usd(judge_model, plan.n_fit)
        elif route.kind == ROUTE_SUB:
            quota = B.check_subscription_quota(force=ns.force, reader=d.quota_reader)

    # 4. plan screen
    out("== llm-router local-check: plan ==")
    out(f"Model:      {model}  [{endpoint.runtime} at {endpoint.base_url}]")
    out(f"Digest:     {plan.digest or 'not reported by this runtime (the verdict expires after 30 days instead of on a model change)'}")
    hw = d.hardware()
    out(f"Hardware:   {_hardware_line(hw)}")
    if not runtimes.is_loopback(endpoint.base_url):
        out(f"Endpoint:   REMOTE ({endpoint.base_url}): the public benchmark prompts are sent there.")
    if plan.draw is None:
        out(f"Items:      none: {plan.refused}")
    else:
        lanes = ", ".join(f"{j} {c}" for j, c in plan.per_lane.items())
        out(f"Items:      {plan.n_fit} of {plan.n_drawn} in this draw fit the model's context (draw {plan.draw}; {lanes}); profile {ns.profile}")
        if plan.max_chars:
            out(f"            context fit: prompts up to ~{plan.max_chars} characters")
    if plan.resume:
        out(f"Resume:     run {plan.resume} is open; this run continues it (answers already collected are kept)")
    estimate, label = estimate_run_seconds(model, plan.n_fit, hw)
    if plan.estimate_s is not None:
        timing = f"~{_fmt_s(plan.estimate_s)} remaining at the measured rate"
    elif estimate is not None:
        timing = (f"~{_fmt_s(estimate)} (estimate from the {label} size class, not measured; "
                  "the real run measures its rate on items 2-3 and prints the total)")
    else:
        timing = "measured by a 3-item rate probe at the start of the run; the total is printed when it finishes (small GPUs: minutes; CPU-only: hours)"
    out("Time:       " + timing)
    if plan.refused:
        err("Refused: " + plan.refused)
        return EXIT_REFUSED
    if ns.no_judge:
        out("Judge:      none (--no-judge): nothing is sent anywhere; programmatic checks only; the result is shown in `status` as a probe and never records a verdict or enables routing.")
        text = "Proceed with a local-only run? [y/N]"
    elif route.error:
        err("Judge:      unavailable: " + route.error)
        return EXIT_NO_JUDGE_ROUTE
    else:
        for n in route.notes:
            out("Note:       " + n)
        note = ""
        if quota is not None:
            if quota.action == "refuse":
                err("Judge:      " + quota.message)
                return EXIT_QUOTA
            if quota.action in ("warn", "unknown"):
                note = quota.message
        text = C.build_consent_text(route.kind, judge_model, n_answers=plan.n_fit, estimate_usd=est_usd, quota_note=note)
    if ns.dry_run:
        if not ns.no_judge:
            out(text.rsplit("\n", 1)[0])        # the consent wording without its "Proceed? [y/N]" question
        out("Dry run: no model call was made and nothing was written.")
        return EXIT_OK

    # 5. consent
    if not C.ask_consent(text, yes=ns.yes, interactive=bool(d.interactive), input_fn=d.input_fn, print_fn=out):
        out("Not started.")
        return EXIT_NOT_PASSED

    # 6. run
    backend = budget = None
    if not ns.no_judge:
        try:
            backend = d.api_backend(judge_model, route.api_key, route.configured_base_url) if route.kind == ROUTE_API else d.cli_backend(judge_model, route.claude_bin)
        except J.JudgeSetupError as e:
            err(f"The judge cannot be used: {e}")
            return EXIT_NO_JUDGE_ROUTE
        if route.kind == ROUTE_API:
            budget = B.Budget(metered=True, est_call_usd=B.per_call_estimate_usd(judge_model))
        else:
            budget = _quota_budget(force=bool(ns.force), reader=d.quota_reader, notify=out)
    chk = SE.Check(model=model, runtime=endpoint.runtime, bank=bank, adapter=adapter, runs_root=runs_root, backend=backend, budget=budget,
                   no_judge=bool(ns.no_judge), force=bool(ns.force), fresh=bool(ns.fresh), now=d.now, eligibility_path=Path(d.eligibility_path),
                   lock_path=d.lock_path, bank_loader=d.load_bank)
    t0 = time.time()
    resume_cmd = "llm-router local-check" + (f" --model {model}" if model else "") + (f" --endpoint {ns.endpoint}" if ns.endpoint else "")
    try:
        try:
            outcome = SE.run_check(chk)
        finally:
            if backend is not None and hasattr(backend, "cleanup"):
                backend.cleanup()
    except KeyboardInterrupt:
        out(f"\nInterrupted. Progress is saved; re-run to resume: {resume_cmd}")
        return EXIT_INTERRUPTED
    except (runtimes.ProbeFailed, R.RunStartError) as e:
        err(f"The run could not start or continue: the runtime stopped answering ({e}). Nothing was recorded; re-run when it answers: {resume_cmd}")
        return EXIT_PROBE_FAILED
    except R.RunMismatch as e:
        err(f"This run directory belongs to different settings ({e}). Discard it with --fresh (counts as a strike).")
        return EXIT_REFUSED
    except B.BudgetExhausted as e:
        err(f"Judge budget reached ({e.reason}). Resumable: {resume_cmd}")
        return EXIT_BUDGET
    except J.JudgeSetupError as e:
        err(f"The judge cannot be used safely: {e}. The local answers are saved; fix this and re-run: {resume_cmd}")
        return EXIT_NO_JUDGE_ROUTE
    for n in outcome.notes:
        out("Note: " + n)
    return _report(d, outcome, budget, time.time() - t0, resume_cmd, model, endpoint.runtime)


def _report(d: Deps, o: Any, budget: Any, secs: float, resume_cmd: str, model: str, runtime: str) -> int:
    from llm_router.local_check import score as S
    out, err = d.out, d.err
    st = o.status
    if st == "refused":
        err("Refused: " + o.reason)
        return EXIT_REFUSED
    if st == "lock_held":
        err(f"Another local-check run is using this runtime ({o.reason}). Wait for it to finish.")
        return EXIT_LOCK
    if st == "interrupted":
        out(f"Interrupted. Progress is saved; re-run to resume: {resume_cmd}")
        return EXIT_INTERRUPTED
    if st == "budget_stop":
        out(f"Stopped: judge budget reached ({o.reason}). INCONCLUSIVE, nothing recorded. Your local answers are kept; resume with: {resume_cmd}")
        return EXIT_BUDGET
    res = o.result
    routing_lines = _apply_routing(d, o, model, runtime) if res is not None and st == "scored" else []
    for ln in render_verdict(res, o, budget, secs, routing_lines):
        out(ln)
    if res is not None:
        for ln in _recommend_lines(d, res, model, runtime):
            out(ln)
    for ln in _prune_errors(d):          # a completed run, whatever its verdict (D-259 retention: keep 3 runs, answers 30 days)
        err(ln)
    if st == "record_refused":
        err(f"The result could not be recorded: {o.reason}. The run stays reserved; re-run to retry: {resume_cmd}")
        return EXIT_RECORD
    if res is None:
        return EXIT_INVALID
    if res.verdict == S.PASS:
        return EXIT_OK
    return EXIT_INVALID if res.verdict == S.INVALID else EXIT_NOT_PASSED


def _passed_lanes(res: Any, o: Any) -> list[str]:
    """Lanes this run just recorded as a PASS (never a prereg-only lane: only lanes this run wrote)."""
    from llm_router.local_check import score as S
    if res is None or res.verdict != S.PASS:
        return []
    rep = o.record_report or {}
    return sorted(j for j, v in rep.items() if v["status"] == "written" and j not in res.guard_excluded)


#: D-274: the one line shown for a judged PASS on a runtime routing cannot use (Ollama only in v16).
NOT_ROUTABLE_LINE = "measured; routing for this runtime is not supported yet (Ollama only in this release)"


def _apply_routing(d: Deps, o: Any, model: str, runtime: str = "ollama") -> list[str]:
    """D-260: switch the passed lanes on (routing.on_pass) and say, per lane, what happened. A lane the user disabled stays off.

    D-274: a PASS on a non-Ollama runtime is recorded but never switched on: one line says routing is not supported for it."""
    passed = _passed_lanes(o.result, o)
    if not passed:
        return []
    if runtime != "ollama":
        return [NOT_ROUTABLE_LINE]
    try:
        states = d.routing.on_pass(model, passed, path=d.routing_path)
    except Exception as e:  # noqa: BLE001 - a routing-file problem must not turn a recorded PASS into a crash
        return [f"routing: could not update ({type(e).__name__}); the result is recorded. Switch lanes on with `llm-router local-check enable <lane>`."]
    L = []
    for job in passed:
        s = states.get(job)
        if s == "enabled":
            L.append(f"routing: on for {job} -- disable with `llm-router local-check disable {job}`")
        elif s == "already_on":
            L.append(f"routing: already on for {job}")
        elif s == "kept_off":
            L.append(f"routing: kept off for {job} -- it was disabled; run `llm-router local-check enable {job}`")
        else:
            L.append(f"routing: {job}: unexpected state {s!r}")
    return L


def _prune_errors(d: Deps) -> list[str]:
    """Run retention.prune() on the runs root; return error lines only (nothing when it pruned cleanly or had nothing to do)."""
    try:
        rep = d.prune(Path(d.runs_root))
        errors = list(getattr(rep, "errors", []) or [])
    except Exception as e:  # noqa: BLE001 - retention must never fail a finished run
        errors = [f"{type(e).__name__}: {e}"]
    return [f"retention: {e}" for e in errors]


def _recommend_lines(d: Deps, res: Any, model: str, runtime: str) -> list[str]:
    """On FAIL / NOT_PASSED / a small-scope INCONCLUSIVE: models that fit this machine (measured first, untested labelled, pull text only)."""
    from llm_router.local_check import score as S
    from llm_router.local_check.recommend import capacity_gb, empty_reason
    from llm_router.resolver.eligibility_store import FULL_DRAW_N
    if not (res.verdict in (S.FAIL, S.NOT_PASSED) or (res.verdict == S.INCONCLUSIVE and res.n_assigned < FULL_DRAW_N)):
        return []
    hw = d.hardware()
    try:
        bank_version = d.load_bank().version
    except Exception:  # noqa: BLE001 - unknown bank means no comparable measurement
        bank_version = None
    try:
        try:
            recs = d.recommend(hw, runtime, model, res, bank_version=bank_version)
        except TypeError:  # legacy injected four-argument collaborators
            recs = d.recommend(hw, runtime, model, res)
    except Exception as e:  # noqa: BLE001 - recommendations are advice, never a crash
        return ["", f"recommendations unavailable ({type(e).__name__})."]
    L = ["", f"== Models that may fit this machine (runtime {runtime}" + (f", ~{capacity_gb(hw):g} GB usable for a model" if capacity_gb(hw) is not None else "") + ") =="]
    if not recs:
        return L + [empty_reason(runtime)]
    for r in recs:
        if r.status == "measured" and r.pooled_rate is not None:
            b = (r.bench or {}).get("pooled") or {}
            version = str((r.bench or {}).get("bank_version")).lower().lstrip("v")
            tag = f"measured {b.get('k')}/{b.get('n')} on v{version}"
        elif getattr(r, "untested_on", None) is not None:
            tag = f"untested on bank {r.untested_on} (no number shown)" if r.untested_on else "untested (no number shown)"
        else:
            tag = "untested (no measurement; sizes and licence only)"
        fit = "" if r.fit == "ok" else ", memory unknown"
        L.append(f"  {r.model_id}  [{r.family}; {r.download_size_gb:g} GB download; {r.licence}; {tag}{fit}]")
        L.append("    to try it, run yourself: " + (r.pull_command or f"pull {r.model_id} with your runtime"))
    if recs[0].note:
        L.append(recs[0].note)
    L.append("Nothing is pulled or routed for you; then run `llm-router local-check --model <name>` to measure it.")
    return L


def render_verdict(res: Any, o: Any, budget: Any, secs: float, routing_lines: list[str] | None = None) -> list[str]:
    """The verdict screen: verdict + n first, lane table, gates, rule concentration, what was recorded, routing, spend."""
    from llm_router.local_check import score as S
    L: list[str] = []
    if res is None:
        return [f"No verdict: {o.reason}"]
    L.append("")
    if res.verdict == S.INVALID:
        L.append(f"== Verdict: INVALID (nothing recorded) == {res.why}")
    else:
        pooled = res.pooled or {}
        if res.verdict == S.NOT_PASSED:
            L.append(f"== Verdict: not passed (NOT_PASSED) -- stopped early after {res.n_scored} of {res.n_assigned} items ({res.k} passed); pass bar: k >= {res.kmin}, Wilson 95% lower bound >= 0.60 ==")
        else:
            L.append(f"== Verdict: {res.verdict}   {res.k}/{res.n_assigned}   (n = {res.n_assigned}; pass bar: k >= {res.kmin}, Wilson 95% lower bound >= 0.60) ==")
        L.append(res.why)
        if res.mode == S.JUDGE and res.verdict != S.PASS and res.kfail is not None:
            L.append(f"powered FAIL at k <= {res.kfail}")
        L.append("")
        L.append(f"{'lane':<16}{'k/n':>9}{'rate':>8}{'Wilson LB':>11}")
        rows = [(j, v) for j, v in res.lanes.items()] + [("pooled", pooled)]
        for j, v in rows:
            if v:
                L.append(f"{j:<16}{str(v['k']) + '/' + str(v['n']):>9}{v['rate']:>8.2f}{v['lb']:>11.2f}")
        if res.guard_excluded:
            L.append(f"lane guard (< {S.LANE_GUARD_MIN:.2f}): {', '.join(res.guard_excluded)} excluded from the pass")
        if res.excluded_unjudged:
            L.append(f"{res.excluded_unjudged} answer(s) the judge could not rate were excluded from n")
    L.append("")
    L.append("gates: " + "  ".join(f"{name.split('_')[0]} {v['status']}" for name, v in res.gates.items()))
    for name, v in res.gates.items():
        if v["status"] in ("FAIL", "NOT_MEASURED"):
            detail = {k: x for k, x in v.items() if k != "status" and x not in (None, [], {})}
            L.append(f"  {name}: {v['status']} {detail}")
    rc = res.rule_concentration
    if rc:
        L.append(f"rule concentration: grader rule '{rc['rule']}' is behind {rc['with_rule']} of {rc['failures']} failures ({rc['share']:.0%}) -- "
                 "run `llm-router local-check explain`")
    # what was recorded
    L.append("")
    rep = o.record_report or {}
    written = sorted(j for j, v in rep.items() if v["status"] == "written")
    if written:
        L.append(f"recorded: {', '.join(written)} (local_eligibility.json; provenance: bank, model digest, settings, judge, run id)")
    elif res.verdict == S.INVALID:
        L.append("recorded: nothing (a validity gate failed; the run directory is kept for a resume)")
    elif res.verdict == S.INCONCLUSIVE:
        L.append("recorded: nothing (INCONCLUSIVE; an earlier recorded PASS is kept). A rerun after 24 h draws the second 70 items.")
    else:
        L.append("recorded: nothing" + (" (programmatic probe)" if res.verdict == S.PROBE else ""))
    L.extend(routing_lines or [])
    if budget is not None:
        s = budget.summary()
        spend = f", ${s['spent_usd']:.2f} spent (cap ${s['cap_usd']:.2f})" if s.get("metered") else ", subscription (no dollar figure)"
        L.append(f"judge: {s['calls']} calls{spend}; elapsed {_fmt_s(secs)}")
    else:
        L.append(f"elapsed {_fmt_s(secs)}")
    if res.verdict in (S.FAIL, S.NOT_PASSED):
        L.append("next: `llm-router local-check explain` shows which grader rules failed, by item id.")
    return L


# ------------------------------------------------------------------------------------------ status / explain

def _routing_switches(d: Deps) -> list[tuple[str, str, dict]]:
    """(model, job, entry) for every switch in local_routing.json; [] when the file is absent or unreadable."""
    import json
    path = Path(d.routing_path) if d.routing_path else d.routing.routing_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for model, jobs in sorted(data.items()) if isinstance(data, dict) else []:
        for job, e in sorted(jobs.items()) if isinstance(jobs, dict) else []:
            e = {"enabled": e} if isinstance(e, bool) else e
            if isinstance(e, dict):
                rows.append((model, job, e))
    return rows


def _lane_verdict(lane: dict) -> str:
    if lane.get("grade") == "passed":
        return "PASS"
    return "NOT_PASSED" if lane.get("grade") == "failed" and lane.get("status") == "not_passed" else "FAIL"


def _recorded_verdict(d: Deps, cfg: dict) -> tuple[str, bool] | None:
    """(pooled recorded verdict, is_for_this_digest) for the config, or None when nothing is recorded.

    A pooled PASS stores the pooled counts as ``passed`` on every lane that cleared the 0.70 guard and a guard-excluded lane as ``failed``
    with ``status: "lane_guard"``; so the pooled verdict is PASS iff any matching lane passed. A pooled FAIL / NOT_PASSED is stored
    identically on every lane. The digest is part of the match when both sides have one; a record for a different digest is reported as such."""
    import json
    if d.eligibility_path is None:
        return None
    try:
        data = json.loads(Path(d.eligibility_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    model = data.get(cfg["model"])
    if not isinstance(model, dict):
        return None
    lanes = model.get("text_job_local")
    if not isinstance(lanes, dict):
        return None
    matching = [lane for _, lane in sorted(lanes.items())
                if isinstance(lane, dict) and lane.get("source") == "local_check"
                and lane.get("bank_sha256") == cfg.get("bank_sha256")
                and lane.get("settings_hash") == cfg.get("settings_hash")]
    if not matching:
        return None
    want = cfg.get("digest")
    same = [ln for ln in matching if not (want and ln.get("model_digest") and ln["model_digest"] != want)]
    pool, current = (same, True) if same else (matching, False)
    if any(_lane_verdict(ln) == "PASS" for ln in pool):
        return "PASS", current
    counted = [ln for ln in pool if ln.get("status") != "lane_guard"] or pool
    return _lane_verdict(counted[0]), current


def _status(d: Deps, model: str | None) -> int:
    from llm_router.local_check import attempts as A
    out = d.out
    out("== local-check status ==")
    lines = d.status_lines()
    shown_lines = [ln.text for ln in lines if model is None or f"[{model}]" in ln.text or ln.text.startswith(model)]
    if shown_lines:
        out("recorded lanes:")
        for t in shown_lines:
            out("  " + t)
    else:
        out("recorded lanes: none -- run `llm-router local-check` to measure your local model")
    switches = [(m, j, e) for m, j, e in _routing_switches(d) if model is None or d.routing.norm_model(m) == d.routing.norm_model(model)]
    if switches:
        out("routing switches (local_routing.json):")
        for m, j, e in switches:
            if e.get("enabled") is True:
                out(f"  {m} {j}: on (since {e.get('since', '?')}, source {e.get('source', '?')}) -- disable: `llm-router local-check disable {j} --model {m}`")
            else:
                out(f"  {m} {j}: off ({e.get('reason', 'disabled')}) -- enable: `llm-router local-check enable {j} --model {m}`")
    else:
        out("routing switches: none (an Ollama PASS switches its lanes on; or `llm-router local-check enable <lane>`)")
    for ln in d.routing.doctor_lines(path=d.routing_path):
        out(ln)
    log = A.AttemptLog(Path(d.runs_root))
    now = d.now()
    by_key: dict[str, list[dict]] = {}
    for e in log.entries():
        by_key.setdefault(e["config_key"], []).append(e)
    shown = False
    for key, es in sorted(by_key.items(), key=lambda kv: kv[1][-1].get("ts", 0)):
        cfg = es[-1].get("config") or {}
        if model and cfg.get("model") != model:
            continue
        shown = True
        scored = [e for e in es if e.get("consumed")]
        probes = [e for e in es if e.get("state") == "scored" and not e.get("consumed")]
        programmatic = len(probes)
        extra = f"; {programmatic} programmatic-only run(s) (--no-judge) not counted" if programmatic else ""
        if probes:
            probe = probes[-1]
            probe_counts = f" {probe['k']}/{probe['n']}" if probe.get("k") is not None and probe.get("n") else ""
            extra += f"; latest probe result: {probe.get('verdict')}{probe_counts} (probe, not a judged verdict)"
        head = f"attempts: {cfg.get('model')} (bank v{cfg.get('bank_version')}): {len(scored)} of {A.MAX_DRAWS} judged draws used"
        if scored:
            last = scored[-1]
            counts = f" {last['k']}/{last['n']}" if last.get("k") is not None and last.get("n") else ""
            kept = ""
            if last.get("verdict") == "INCONCLUSIVE":
                rec = _recorded_verdict(d, cfg)
                if rec is not None:
                    kept = (f" (not recorded; recorded verdict {rec[0]} kept)" if rec[1]
                            else f" (not recorded; recorded verdict {rec[0]} is for a different model digest)")
            out(f"{head}; last {last.get('verdict')}{counts} {last.get('date', '')}{kept}{extra}")
            if last.get("verdict") == "PASS" and cfg.get("runtime", "ollama") != "ollama":
                out("  " + NOT_ROUTABLE_LINE)
        else:
            out(f"{head}; no judged run yet{extra}")
        if scored and len(scored) < A.MAX_DRAWS:
            nxt = scored[-1]["ts"] + A.COOLDOWN_S
            out("  next draw: " + ("now" if now >= nxt else "after " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(nxt)) + " (or --force)"))
        elif len(scored) >= A.MAX_DRAWS:
            out("  bank exhausted for this model, bank version and settings; change the model, the settings or update the bank")
        for draw in (1, 2):
            r = log.reservation(key, draw, now)
            if r:
                left = max(0.0, r["ts"] + A.RESERVATION_TTL_S - now)
                out(f"  open run {r['run_id']} (draw {draw}): re-run to resume; lapses in {_fmt_s(left)}")
    if not shown:
        out("attempts: none")
    return EXIT_OK


def _switch(d: Deps, sub: str, lane: str, model: str | None) -> int:
    """`enable|disable <lane> [--model M]`: flip one routing switch through local_check.routing. Model names carry their tag (``qwen3.5:latest``)."""
    err = d.err
    if model is None:
        names = sorted(set(d.inventory()))
        if len(names) != 1:
            err(("several local models found (" + ", ".join(names[:12]) + "); pass --model.") if names
                else "no local model found in the inventory; pass --model NAME (with its tag, as the runtime lists it).")
            return EXIT_NO_RUNTIME
        model = names[0]
    try:
        if sub == "enable":
            d.routing.enable(model, lane, "manual", path=d.routing_path)
            d.out(f"routing: on for {lane} on {model} (set by you). It applies only while the lane is passed and fresh -- see `llm-router local-check status`.")
        else:
            d.routing.disable(model, lane, "manual", path=d.routing_path)
            d.out(f"routing: off for {lane} on {model}. A later PASS keeps it off; `llm-router local-check enable {lane}` switches it back on.")
    except (OSError, ValueError) as e:
        err(f"could not update the routing switch: {type(e).__name__}: {e}")
        return EXIT_RECORD
    return EXIT_OK


def _explain(d: Deps, model: str | None) -> int:
    from llm_router.local_check import attempts as A
    out = d.out
    es = [e for e in A.AttemptLog(Path(d.runs_root)).entries()
          if e.get("rows") and e.get("state") == "scored" and (model is None or (e.get("config") or {}).get("model") == model)]
    if not es:
        out("No finished run with graded items to explain" + (f" for {model}" if model else "") + ". An INVALID run shows its gates only.")
        return EXIT_OK
    e = max(es, key=lambda x: x.get("ts", 0))
    rows = e["rows"]
    cfg = e.get("config") or {}
    failed = [r for r in rows if not r.get("ok")]
    out(f"== last run: {cfg.get('model')}  {e.get('date')}  draw {e.get('draw')}  verdict {e.get('verdict')}  ({len(rows) - len(failed)}/{len(rows)} items ok) ==")
    out("Item ids are public benchmark ids; no item text, prompt or answer is stored or shown.")
    if not failed:
        out("No failed items.")
        return EXIT_OK
    hist: dict[str, list[str]] = {}
    for r in failed:
        reasons = list(r.get("reasons") or [])
        if not reasons:
            reasons = ["judge:fail"] if r.get("prog") and r.get("judge") == "fail" else ["judge:not-rated"] if r.get("prog") else ["unknown"]
        for rule in reasons:
            hist.setdefault(rule, []).append(str(r["item_id"]))
    out(f"{'rule':<28}{'items':>6}  share of {len(failed)} failures")
    for rule, ids in sorted(hist.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out(f"{rule:<28}{len(ids):>6}  {len(ids) / len(failed):.0%}")
        out("    " + ", ".join(sorted(ids)))
    top, ids = max(hist.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(ids) / len(failed) >= 0.40:
        out(f"Concentration: rule '{top}' is behind {len(ids)} of {len(failed)} failures; one rule dominating usually means one fixable habit of the model.")
    return EXIT_OK
