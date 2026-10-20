"""local-check judge (LC-4): rubric prompt, two backends, plants gate G3b. Ported from PREREG-Q37 (judge_q37.py, plants handling, common_q37.claude()).

The judge decides ONE thing: does the answer accurately describe the change in the diff. Format and length are checked by ``grader`` and the
judge is told never to fail for them (``rubric.LIMITS`` is the single source of those limits).

Backends
  * ``ApiBackend``  - the user's Anthropic key (ANTHROPIC_API_KEY or the configured Anthropic provider). Pinned model id comes from a
    parameter (the bank manifest supplies it; no model name is a default here, D-205), temperature 0, served model id and exact cost from usage.
  * ``ClaudeCliBackend`` - ``claude -p`` on the user's subscription: no tools (``--tools ""``), ``--safe-mode``, hooks disabled by a settings
    file, cwd in a fresh temp dir with no CLAUDE.md/AGENTS.md at or above it (guarded), env allow-list, 240 s timeout.

REAL HOME on purpose. The benchmark harness (common_q37.claude) ran children with a scratch HOME so that benchmark agents could not touch the
operator's config, and authenticated them with a separate OAuth token file. A user running ``local-check`` is the account owner running
their own CLI: the CLI can only authenticate from the user's real HOME (keychain / ~/.claude credentials); a scratch HOME gives "Not logged in".
Isolation here comes from --safe-mode + no tools + disabled hooks + an instruction-free cwd, not from a fake HOME. ANTHROPIC_API_KEY is
deliberately NOT passed to the CLI, so it cannot silently switch the subscription route to metered billing.

Neither backend logs or returns the key; ``redact`` removes any secret value from text before it is stored.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from llm_router.local_check.budget import Budget, BudgetExhausted

CLI_TIMEOUT_S = 240
API_MAX_TOKENS = 400
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
# Variables a `claude -p` child may see. HOME is the user's real HOME (auth). No *_API_KEY: see module docstring.
CLI_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR",
                 "CLAUDE_CODE_OAUTH_TOKEN", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "COMSPEC",
                 # proxy-only / private-CA networks (the CLI cannot reach Anthropic without these); none of them carries a key
                 "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Plants gate G3b thresholds (design section 4).
G3B_GOOD_MIN, G3B_SWAPPED_MIN, G3B_MUTATED_MIN = 11, 11, 5
G3B_EXPECTED = {"good": 12, "swapped": 12, "mutated": 6}
G3B_MAX_ERROR_RATE = 0.05

RUBRIC = (
    "Decide ONLY whether the candidate answer accurately describes the change the diff makes. Format, length, sentence count, tone, wording, imperative mood, section layout, "
    "whether named files or identifiers exist, and claims about tests are checked by a separate program: NEVER fail an answer for any of those, and never apply a length or sentence limit.\n"
    "FAIL only for one of these concrete defects, and say which one:\n"
    "  A  WRONG_CHANGE: the answer describes a different change than the diff makes, or leaves out the main change of the diff entirely. (Leaving out a minor secondary edit is NOT a defect.)\n"
    "  B  CONTRADICTED: a specific statement in the answer is clearly wrong according to the diff (opposite direction such as add vs remove or enable vs disable, wrong before/after values, wrong behaviour). A loosely worded or imprecise phrase about a minor detail (for example saying 'export' where the code includes or injects) is NOT a defect; a statement about the main change that the diff contradicts is.\n"
    "  C  GENERIC: the answer is so generic that it would fit almost any diff (for example 'Update code' or 'Improve things') and does not say what changed.\n"
    "  D  (pull request descriptions only) the Test plan steps are about something unrelated to this change, so none of them could verify it.\n"
    "Do NOT fail for: leaving out minor details, plausible statements about motivation, wording that differs from the code, a brief test plan, or anything you cannot point to in the diff.")


def judge_prompt(task_input: str, candidate: str) -> str:
    """``task_input`` is the verbatim task text the local model was given (the bank item's last user message)."""
    return ("You are a strict but fair reviewer of one answer to a developer task. Judge ONLY against the task input shown.\n\n" + RUBRIC +
            f"\n\n=== TASK INPUT (verbatim) ===\n{task_input}\n=== END TASK INPUT ===\n\n=== CANDIDATE ANSWER ===\n{candidate[:4000]}\n=== END CANDIDATE ===\n\n"
            'Reply with exactly one JSON object and nothing else: {"verdict": "pass" or "fail", "defect": "A" or "B" or "C" or "D" or null, '
            '"quote": "<=20 words from the answer or diff that shows the defect, or empty", "reason": "<=25 words"}')


def parse_verdict(text: str) -> tuple[str | None, str | None, str]:
    """-> (verdict or None, defect, reason). A fail without a defect letter is unparseable (None)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None, None, ""
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None, None, ""
    if not isinstance(d, dict):
        return None, None, ""
    v = str(d.get("verdict", "")).lower()
    df = d.get("defect")
    if v == "pass":
        return "pass", None, str(d.get("reason", ""))[:200]
    if v == "fail" and df in ("A", "B", "C", "D"):
        return "fail", df, (str(d.get("reason", "")) + " | " + str(d.get("quote", "")))[:300]
    return None, None, ""


@dataclass
class RawReply:
    text: str = ""
    error: str | None = None
    served_model: str = ""
    cost_usd: float | None = None      # METERED cost, exact from usage and the price table (API route only); None = unknown / not metered
    notional_cost_usd: float | None = None  # the CLI's reported figure: NOT money on a subscription (CLI route only)
    input_tokens: int | None = None
    output_tokens: int | None = None
    secs: float = 0.0


class Backend(Protocol):
    name: str
    metered: bool
    model: str

    def complete(self, prompt: str) -> RawReply: ...


class JudgeSetupError(Exception):
    """The backend cannot be used safely (missing binary, instruction file above the cwd, no key)."""


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        if s and len(s) >= 8 and s in text:
            text = text.replace(s, "[redacted]")
    return text


def resolve_api_key(environ: dict | None = None, config_key: Callable[[], str] | None = None) -> str:
    """The user's Anthropic key: ANTHROPIC_API_KEY first, else the configured Anthropic provider. '' when none."""
    env = os.environ if environ is None else environ
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    if config_key is None:
        def config_key() -> str:
            from llm_router.config import get_config
            return getattr(get_config(), "anthropic_api_key", "") or ""
    try:
        return (config_key() or "").strip()
    except Exception:  # config problems must not crash route selection; the caller falls back to the subscription route
        return ""


def is_anthropic_url(url: str) -> bool:
    from urllib.parse import urlsplit
    u = urlsplit((url or "").strip())
    return u.scheme == "https" and (u.hostname or "").lower() == "api.anthropic.com" and u.port in (None, 443)


def check_api_route(model: str, configured_base_url: str = "") -> None:
    """Raise ``JudgeSetupError`` (with a user-facing message) when the API route cannot be used honestly.

    * the consent text promises the key and prompts go to Anthropic: a configured Anthropic provider with a custom base URL is refused;
    * the USD cap must be enforceable: a model with no price in ``llm_router.pricing`` is refused (its spend would always read $0).
    """
    from llm_router import pricing
    if configured_base_url and not is_anthropic_url(configured_base_url):
        raise JudgeSetupError("your Anthropic provider uses a custom base URL; the judge sends your key and prompts only to " + ANTHROPIC_BASE_URL
                              + ", so the API route is not available. Use the Claude subscription route instead.")
    if pricing.resolve(model) is None or pricing.is_local(model) or pricing.cost_usd(model, 1, 1) is None:
        raise JudgeSetupError(f"judge model {model!r} has no price in the price table, so the dollar cap could not be enforced; "
                              "pick a priced model or use the Claude subscription route.")


class ApiBackend:
    name = "api"
    metered = True

    def __init__(self, model: str, api_key: str, client_factory: Callable[[str], object] | None = None, configured_base_url: str = ""):
        if not model:
            raise JudgeSetupError("a pinned judge model id is required")
        if not api_key:
            raise JudgeSetupError("no Anthropic API key")
        check_api_route(model, configured_base_url)
        self.model = model
        self._key = api_key
        self._factory = client_factory or self._default_client

    @staticmethod
    def _default_client(api_key: str):
        import anthropic
        # The judge is a measurement instrument with a pinned model and exact cost accounting, not a routed completion.
        # base_url is explicit: the SDK would otherwise honour ANTHROPIC_BASE_URL, which may point at llm-router's own proxy or another host.
        return anthropic.Anthropic(api_key=api_key, base_url=ANTHROPIC_BASE_URL, max_retries=0, timeout=float(CLI_TIMEOUT_S))  # llm_router: direct-ok

    def complete(self, prompt: str) -> RawReply:
        from llm_router import pricing
        t0 = time.time()
        try:
            client = self._factory(self._key)
            resp = client.messages.create(model=self.model, max_tokens=API_MAX_TOKENS, temperature=0,
                                          messages=[{"role": "user", "content": prompt}])
            text = "".join(getattr(b, "text", "") for b in (getattr(resp, "content", None) or []))
            served = str(getattr(resp, "model", "") or "")
            usage = getattr(resp, "usage", None)
            tin = getattr(usage, "input_tokens", None)
            tout = getattr(usage, "output_tokens", None)
            cost = None
            if isinstance(tin, int) and isinstance(tout, int):
                cr = getattr(usage, "cache_read_input_tokens", 0)
                cw = getattr(usage, "cache_creation_input_tokens", 0)
                cost = pricing.cost_usd(served or self.model, tin, tout, cr if isinstance(cr, int) else 0, cw if isinstance(cw, int) else 0)
            return RawReply(text=redact(text, (self._key,)), served_model=served, cost_usd=cost, input_tokens=tin, output_tokens=tout, secs=round(time.time() - t0, 1))
        except Exception as exc:  # any SDK/network error is a judge error for this attempt; the message is not kept (it can echo request details)
            return RawReply(error=type(exc).__name__, secs=round(time.time() - t0, 1))


def ancestor_instruction_files(cwd: Path) -> list[str]:
    cwd = Path(cwd).resolve()
    return [str(d / f) for d in [cwd, *cwd.parents] for f in INSTRUCTION_FILES if (d / f).exists()]


def cli_env(environ: dict | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {k: env[k] for k in CLI_ENV_ALLOW if k in env}


class ClaudeCliBackend:
    name = "claude-cli"
    metered = False

    def __init__(self, model: str, claude_bin: str | None = None, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 environ: dict | None = None, temp_root: str | None = None, timeout: int = CLI_TIMEOUT_S):
        if not model:
            raise JudgeSetupError("a pinned judge model id is required")
        self.model = model
        self.claude_bin = claude_bin or shutil.which("claude") or ""
        if not self.claude_bin:
            raise JudgeSetupError("the claude CLI is not on PATH")
        self._run = runner
        self._environ = environ
        self._temp_root = temp_root
        self._timeout = timeout
        self._work: Path | None = None

    def _workdir(self) -> tuple[Path, Path]:
        """-> (settings file, cwd): a fresh temp dir, checked to have no instruction file at or above it."""
        if self._work is None:
            self._work = Path(tempfile.mkdtemp(prefix="lc-judge-", dir=self._temp_root)).resolve()
            atexit.register(self.cleanup)           # backstop if the caller never uses the context manager / cleanup()
            (self._work / "cwd").mkdir(mode=0o700)
            (self._work / "settings.json").write_text('{"disableAllHooks": true}')
        cwd = self._work / "cwd"
        found = ancestor_instruction_files(cwd)
        if found:
            raise JudgeSetupError("an instruction file (CLAUDE.md/AGENTS.md) exists at or above the judge cwd: " + ", ".join(found))
        return self._work / "settings.json", cwd

    def argv(self, settings: Path) -> list[str]:
        return [self.claude_bin, "-p", "--safe-mode", "--no-session-persistence", "--settings", str(settings),
                "--model", self.model, "--output-format", "json", "--tools", ""]

    def complete(self, prompt: str) -> RawReply:
        settings, cwd = self._workdir()          # JudgeSetupError propagates: a guard failure is not a retryable judge error
        env = cli_env(self._environ)
        secrets = (env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),)
        t0 = time.time()
        try:
            r = self._run(self.argv(settings), input=prompt, text=True, capture_output=True, timeout=self._timeout, cwd=str(cwd), env=env, check=False)
            v = json.loads(r.stdout)
            usage = v.get("modelUsage", {})
            served = ",".join(usage) if isinstance(usage, dict) else ""
            cost = v.get("total_cost_usd")          # the CLI's own estimate: notional on a subscription, never added to the USD spend
            err = "claude_error" if (v.get("is_error") or r.returncode) else None
            return RawReply(text=redact(v.get("result", "") or "", secrets), error=err, served_model=served,
                            notional_cost_usd=float(cost) if isinstance(cost, (int, float)) else None, secs=round(time.time() - t0, 1))
        except Exception as exc:
            return RawReply(error=type(exc).__name__, secs=round(time.time() - t0, 1))

    def __enter__(self) -> "ClaudeCliBackend":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._work is not None:
            shutil.rmtree(self._work, ignore_errors=True)
            self._work = None


@dataclass
class JudgeOutcome:
    verdict: str                        # "pass" | "fail" | "judge_error"
    defect: str | None = None
    reason: str = ""
    served_model: str = ""
    cost_usd: float | None = None       # metered USD, sum over attempts; None if any attempt's cost is unknown (always None on the CLI route)
    notional_cost_usd: float | None = None  # CLI route only: the CLI's reported figure summed over attempts; NOT money on a subscription
    attempts: int = 0
    error: str | None = None


def judge_answer(backend: Backend, budget: Budget, prompt: str) -> JudgeOutcome:
    """One judgement: at most two attempts (one retry on a backend error or an unparseable reply). The budget is checked before EACH attempt.
    ``BudgetExhausted`` propagates."""
    out = JudgeOutcome("judge_error")
    costs: list[float | None] = []
    notional: list[float] = []
    for attempt in range(2):
        budget.check()
        rep = backend.complete(prompt)
        budget.record(rep.cost_usd)
        costs.append(rep.cost_usd)
        out.attempts = attempt + 1
        out.served_model = rep.served_model or out.served_model
        out.cost_usd = None if any(c is None for c in costs) else sum(c for c in costs if c is not None)
        if rep.notional_cost_usd is not None:
            notional.append(rep.notional_cost_usd)
            out.notional_cost_usd = sum(notional)
        if rep.error:
            out.error = rep.error
            continue
        v, df, why = parse_verdict(rep.text)
        if v is None:
            out.error = "unparseable"
            continue
        out.verdict, out.defect, out.reason, out.error = v, df, why, None
        return out
    return out


def judge_candidate(backend: Backend, budget: Budget, task_input: str, candidate: str) -> JudgeOutcome:
    """Judge one candidate answer. An empty/whitespace answer is a fail WITHOUT a call (as Q37 did): it spends nothing and counts no attempt."""
    if not (candidate or "").strip():
        return JudgeOutcome("fail", "C", "empty answer", cost_usd=0.0, attempts=0)
    return judge_answer(backend, budget, judge_prompt(task_input, candidate))


def served_model_matches(served: str, pinned: str) -> tuple[bool, str]:
    """G0 helper: does the model that actually answered match the pinned id? -> (ok, reason).

    Match = exact string, or both ids resolve through ``llm_router.pricing`` to the SAME canonical id (the price table's documented alias /
    undated-name mapping, e.g. an undated name and its dated snapshot). ``served`` may be a comma-separated list (the CLI reports every model it
    used): ALL entries must match. An empty ``served`` never matches.
    """
    from llm_router import pricing
    names = [n.strip() for n in (served or "").split(",") if n.strip()]
    if not pinned or not names:
        return False, "served model id missing" if pinned else "pinned model id missing"
    want = pricing.resolve(pinned)
    for n in names:
        if n == pinned:
            continue
        if want is not None and pricing.resolve(n) == want:
            continue
        return False, f"served model {n!r} does not match pinned {pinned!r}"
    return True, "ok"


@dataclass
class PlantRow:
    plant_id: str
    kind: str
    verdict: str
    defect: str | None = None
    served_model: str = ""


@dataclass
class G3bResult:
    status: str                         # "PASS" | "FAIL" | "INCONCLUSIVE" (budget stop)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)   # kind -> {"n", "correct", "errors"}
    errors: int = 0
    n_judged: int = 0
    error_rate: float | None = None
    reasons: list[str] = field(default_factory=list)
    served_models: list[str] = field(default_factory=list)
    rows: list[PlantRow] = field(default_factory=list)
    calls: int = 0
    cost_usd: float | None = None       # metered USD (API route); None when any call was unpriced or the route is the CLI
    notional_cost_usd: float | None = None  # CLI route: sum of the CLI's reported figures; NOT money on a subscription
    stop_reason: str = ""


def evaluate_g3b(rows: list[PlantRow], expected: dict[str, int] = G3B_EXPECTED) -> tuple[str, dict[str, dict[str, int]], list[str], float | None]:
    """Pure threshold check on judged plant rows -> (status, counts, reasons, error_rate). Good must be judged pass; swapped and mutated must be judged fail."""
    want = {"good": "pass", "swapped": "fail", "mutated": "fail"}
    mins = {"good": G3B_GOOD_MIN, "swapped": G3B_SWAPPED_MIN, "mutated": G3B_MUTATED_MIN}
    counts = {k: {"n": 0, "correct": 0, "errors": 0} for k in want}
    for r in rows:
        c = counts.get(r.kind)
        if c is None:
            continue
        c["n"] += 1
        if r.verdict == "judge_error":
            c["errors"] += 1
        elif r.verdict == want[r.kind]:
            c["correct"] += 1
    reasons: list[str] = []
    for k, n_exp in expected.items():
        if counts[k]["n"] != n_exp:
            reasons.append(f"{k}: {counts[k]['n']} plants judged, {n_exp} expected")
        if counts[k]["correct"] < mins[k]:
            reasons.append(f"{k}: {counts[k]['correct']}/{n_exp} correct, need >= {mins[k]}")
    total = sum(c["n"] for c in counts.values())
    err_total = sum(c["errors"] for c in counts.values())
    rate = None if total == 0 else err_total / total
    if rate is None:
        reasons.append("no plants judged")
    elif rate > G3B_MAX_ERROR_RATE:
        reasons.append(f"judge errors {err_total}/{total} exceed {G3B_MAX_ERROR_RATE:.0%}")
    return ("FAIL" if reasons else "PASS"), counts, reasons, rate


def run_g3b(backend: Backend, budget: Budget, plants: list[dict]) -> G3bResult:
    """Judge the shipped plants under the live judge. Each plant dict: {id, kind, text, messages}. A budget stop yields INCONCLUSIVE (resumable), never PASS."""
    rows: list[PlantRow] = []
    served: list[str] = []
    costs: list[float | None] = []
    notional: list[float] = []
    stop = ""
    for p in plants:
        task_input = p["messages"][-1]["content"]
        try:
            o = judge_candidate(backend, budget, task_input, p["text"])
        except BudgetExhausted as e:
            stop = e.reason
            break
        costs.append(o.cost_usd)
        if o.notional_cost_usd is not None:
            notional.append(o.notional_cost_usd)
        if o.served_model and o.served_model not in served:
            served.append(o.served_model)
        rows.append(PlantRow(str(p["id"]), p["kind"], o.verdict, o.defect, o.served_model))
    status, counts, reasons, rate = evaluate_g3b(rows)
    cost = None if (not costs or any(c is None for c in costs)) else sum(c for c in costs if c is not None)
    if stop:
        status, reasons = "INCONCLUSIVE", [f"budget stop: {stop}"] + reasons
    return G3bResult(status=status, counts=counts, errors=sum(c["errors"] for c in counts.values()), n_judged=len(rows), error_rate=rate,
                     reasons=reasons, served_models=served, rows=rows, calls=budget.calls, cost_usd=cost, notional_cost_usd=sum(notional) if notional else None, stop_reason=stop)
