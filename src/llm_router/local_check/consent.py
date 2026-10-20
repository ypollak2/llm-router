"""local-check consent + plan text for the judge step (LC-4, design section 2: consent text shown once per run, y/N, ``--yes``).

Only the judge/cost/consent part of the plan screen lives here; LC-6 composes it with model, hardware and timing lines.
"""
from __future__ import annotations

from typing import Callable

from llm_router.local_check import budget as _budget

ROUTE_API = "api"
ROUTE_SUBSCRIPTION = "subscription"


def judge_route_label(route: str) -> str:
    return "your ANTHROPIC_API_KEY (metered API)" if route == ROUTE_API else "your Claude subscription (claude -p)"


def build_consent_text(route: str, judge_model: str, n_plants: int = _budget.PLANT_CALLS, n_answers: int = _budget.MAX_ANSWER_CALLS,
                       cap_usd: float = _budget.DEFAULT_CAP_USD, cap_calls: int = _budget.DEFAULT_CAP_CALLS,
                       estimate_usd: float | None = None, quota_note: str = "") -> str:
    """The exact plan/consent wording. ``estimate_usd`` is only used for the API route; ``None`` means the model has no known price."""
    n_calls = _budget.planned_calls(n_answers, n_plants)
    lines = [
        f"Judge: {judge_route_label(route)}, model {judge_model}.",
        "What is sent to Anthropic: the public benchmark task text (commit and pull-request diffs from open-source repositories) and your local model's answers to them.",
        "What is not sent: your files, your prompts, your repositories, or anything else from this machine.",
    ]
    if route == ROUTE_API:
        if estimate_usd is None:
            lines.append(f"Cost: up to ~{n_calls} requests ({n_plants} validity checks + up to {n_answers} answers); the price of this model is not in the price table, so the dollar cost cannot be estimated.")
        else:
            lines.append(f"Cost: ~${estimate_usd:.2f} estimated for ~{n_calls} requests ({n_plants} validity checks + up to {n_answers} answers), billed to your Anthropic API account.")
        lines.append(f"Cap: the run stops at ${cap_usd:.2f} or {cap_calls} requests, whichever comes first, and can be resumed.")
    else:
        lines.append(f"Cost: ~{n_calls} requests ({n_plants} validity checks + up to {n_answers} answers); this counts toward your plan limit. No API charges.")
        lines.append(f"Cap: the run stops at {cap_calls} requests and can be resumed.")
    if quota_note:
        lines.append(quota_note)
    lines.append("Proceed? [y/N]")
    return "\n".join(lines)


def ask_consent(text: str, *, yes: bool = False, interactive: bool = True, input_fn: Callable[[str], str] = input,
                print_fn: Callable[[str], None] = print) -> bool:
    """True only on ``--yes`` or an explicit y/yes at a prompt. A non-interactive run without ``--yes`` never consents."""
    print_fn(text)
    if yes:
        return True
    if not interactive:
        print_fn("Not confirmed: re-run with --yes to consent without a prompt.")
        return False
    try:
        return input_fn("> ").strip().lower() in ("y", "yes")
    except EOFError:
        return False
