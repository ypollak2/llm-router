"""DASH-5 (D9) — "saved" must mean one thing across the summary's panels.

Two panels report the pre-overhead figure and two report the post-overhead one,
all under the bare word "saved":

* Routing / Free — baseline-avoided, **gross** of routing overhead.
* Codex / Gemini — `gross − overhead = realized`, i.e. **net** of overhead.

Same word, two definitions. The fix qualifies the gross panels explicitly
("gross") so "realized" is reserved for the overhead-netted figure — a reader
can now tell which is which. (Fully *netting* Routing/Free for overhead is the
DASH-1b single-basis work; here we make the labels honest, not the maths.)
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
sys.path.insert(0, str(_HOOK_DIR))
_spec = importlib.util.spec_from_file_location("session_end", _HOOK_DIR / "session-end.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

_SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"


def _strip(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_routing_saved_is_labelled_gross():
    """Fail-before: routing header says bare '% saved'. Pass-after: it carries a
    'gross' qualifier — while keeping '% saved' contiguous (other tests rely on
    it)."""
    tools = {"query": {"count": 5, "in": 5000, "out": 2500, "cost": 0.01,
                       "models": {"gpt-4o-mini": 5}}}
    # subscription=False explicitly: this asserts the CASH rendering, which
    # only a pay-per-token user sees. Left to ambient config it passed or
    # failed by whose machine ran it.
    text = _strip("\n".join(se._format_routing_section(tools, subscription=False)))
    assert "% saved" in text, "the '% saved' token must remain (savings-clamp test relies on it)"
    assert "gross" in text, "routing 'saved' must be qualified as gross"


def test_free_saved_is_labelled_gross():
    """The free-panel savings must be qualified 'gross saved', not bare 'saved',
    to match the Routing panel and distinguish it from Codex/Gemini 'realized'."""
    free_rows = [{"provider": "ollama", "input_tokens": 1000, "output_tokens": 500}]
    text = _strip("\n".join(se._format_free_section(free_rows)))
    assert "gross saved" in text, "free 'saved' must be qualified as gross"


def test_realized_reserved_for_overhead_netted_panels():
    """Source guard: 'realized' remains the label for the Codex/Gemini panels
    (gross − overhead), so 'gross' vs 'realized' name two distinct definitions."""
    txt = (_SRC / "hooks" / "session-end.py").read_text()
    assert "realized ${realized:.4f}" in txt, "provider/codex panels keep the realized (net) figure"
    # The gross qualifier and the realized qualifier must both be present as
    # distinct terms — one definition per label.
    assert "gross" in txt and "realized" in txt
