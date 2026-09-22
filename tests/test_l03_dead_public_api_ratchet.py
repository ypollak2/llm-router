"""L-01…L-06 — public symbols nothing calls.

The 2026-09-21 audit found clusters of exported functions with **no call site
anywhere in `src/`**. Confirmed here: every apparent "use" turned out to be an
`__all__` entry, a docstring cross-reference or a comment.

They are not equivalent, and the difference matters more than the count:

    trace_id.derive_trace_id       the ACTUAL G-025 feature — a restart-surviving
                                   composite trace id. Its sibling `hash_prompt`
                                   did ship and is wired. This one is still in
                                   `__all__`, so a reader concludes G-025
                                   delivered a trace-ID scheme. It delivered a
                                   hash helper.
    judge_cascade.should_*         the module's own "pure decision function",
                                   exported and unit-tested, while
                                   `streaming_judge.py` reimplements the
                                   comparison inline — deliberate, but it orphans
                                   the module's centrepiece.
    cost.py reporters (8)          orphaned reporting scaffolding, or built ahead
                                   of a consumer that never landed.
    storage/service.migrate_config DELETED 2026-09-22. It was dead AND
                                   unfinished — it invented `"new_field_v3"` as a
                                   stand-in target schema behind a
                                   `# TODO: Define target schema (mocked here)`,
                                   then wrote the "migrated" config anyway. The
                                   only thing it reliably did was change the
                                   version number.

One entry has since been deleted outright — `migrate_config` was the only one
that was both uncalled AND unfinished, so removing it took nothing working away.
The remaining eleven are complete functions that simply have no consumer, which
is a different decision.

**Why the rest is a ratchet and not a deletion.** `llm_router` ships on PyPI, and
these are public names on a published package: removing them is a breaking change
for any downstream importer, and whether they are abandoned scaffolding or
intended library surface is a product decision this test cannot make. Guessing it
here would be the same mistake as `test_shipped_modules_import.py` records for
`control_plane.audit` — *"stubbing the import would silently disable audit
logging in a control plane"*.

So the list is frozen. **Lower it; never extend it without recording why.** When
an entry gains a real caller, or is deliberately removed, this test fails and
forces the list to shrink.

L-05 needed no change: `context_signal.py`'s own docstring already states plainly
that it is "NOT the one in production", names the module that replaced it, and
says changing it changes no routing behaviour. The audit called it a decoy; it is
in fact the most honest file in the cluster.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"

#: symbol -> (module path, why it is still here)
DEAD_PUBLIC_API = {
    "derive_trace_id": ("trace_id.py", "L-01 — the real G-025 feature; never adopted"),
    "should_judge_inline": ("judge_cascade.py", "L-02 — streaming_judge reimplements it inline"),
    "should_cascade": ("judge_cascade.py", "L-02 — same"),
    "format_spend_for_display": ("cost.py", "L-03 — orphaned reporter"),
    "get_usage_summary": ("cost.py", "L-03 — orphaned reporter"),
    "log_quota_snapshot": ("cost.py", "L-03 — orphaned reporter"),
    "get_router_efficiency": ("cost.py", "L-03 — orphaned reporter"),
    "get_classifier_overhead": ("cost.py", "L-03 — orphaned reporter"),
    "get_cache_hit_stats": ("cost.py", "L-03 — orphaned reporter"),
    "log_savings": ("cost.py", "L-03 — orphaned reporter"),
    "log_quality_trend": ("cost.py", "L-03 — orphaned reporter"),
    "refresh_baseline_pricing_from_api": ("cost.py", "L-03 — second pricing source, no consumer"),
}


def _call_sites(symbol: str) -> list[str]:
    """Places `symbol` is actually CALLED or imported in src/.

    An `__all__` entry, a docstring `:func:` reference and a comment all mention
    the name without using it — counting those is how the first pass of this
    analysis wrongly concluded four of these were alive.
    """
    sites = []
    for f in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == symbol:
                    sites.append(f"{f.relative_to(SRC)}:{node.lineno} call")
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == symbol for a in node.names):
                    sites.append(f"{f.relative_to(SRC)}:{node.lineno} import")
    return sites


@pytest.mark.parametrize("symbol", sorted(DEAD_PUBLIC_API))
def test_listed_symbols_are_still_dead(symbol):
    """Strict: when one gains a caller, this fails and the list must shrink.

    Without this the list becomes a place dead code goes to be forgotten — the
    failure mode `KNOWN_BROKEN` lists have everywhere else in this suite.
    """
    module, why = DEAD_PUBLIC_API[symbol]
    sites = _call_sites(symbol)
    assert not sites, (
        f"{symbol} ({module}) now has a caller: {sites}. It was listed as dead "
        f"({why}) — remove it from DEAD_PUBLIC_API."
    )


def test_the_list_does_not_grow():
    """A budget, so the next orphan is a decision rather than an accident."""
    assert len(DEAD_PUBLIC_API) <= 12, (
        f"{len(DEAD_PUBLIC_API)} dead public symbols. Lower this list; do not "
        f"raise the bound without recording why the code ships uncalled."
    )


def test_every_listed_symbol_still_exists():
    """The list must describe reality, or it is documentation of nothing."""
    missing = []
    for symbol, (module, _why) in DEAD_PUBLIC_API.items():
        text = (SRC / module).read_text(encoding="utf-8")
        if f"def {symbol}" not in text:
            missing.append(f"{symbol} is no longer defined in {module}")
    assert not missing, (
        "\n".join(missing) + "\n\nIt was deleted — remove it from DEAD_PUBLIC_API too."
    )


def test_the_detector_is_not_vacuous():
    """`_call_sites` must be able to FIND a call, or every assertion is empty."""
    live = _call_sites("scrub_text")
    assert live, (
        "the call-site detector found no uses of `scrub_text`, which is called "
        "from several stores — it is not looking where it thinks it is"
    )


def test_derive_trace_id_advertises_a_feature_that_did_not_ship():
    """L-01 specifically: it remains in `__all__` with no implementation behind it.

    Recorded rather than fixed — removing a name from `__all__` is itself a
    breaking change for `import *`, and which way to resolve it is the same
    product decision as the rest of this list.
    """
    text = (SRC / "trace_id.py").read_text(encoding="utf-8")
    assert '"derive_trace_id"' in text, "no longer exported — update this test"
    assert not _call_sites("derive_trace_id"), "it gained a caller; G-025 shipped after all"
