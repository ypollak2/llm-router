"""Auto-route tail latency (AR-latency): the OKF bundle load/score is on the hook's hot path.

Both changes must leave results identical: the C YAML loader parses the same safe subset,
and the substring pre-exit in ``_score`` may only skip docs whose score is already 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llm_router import okf


def _doc(**kw):
    base = dict(path=Path("x.md"), type="Generic", title="T", body="", description="",
                resource="", tags=[], timestamp="", extra={})
    base.update(kw)
    return okf.OKFConcept(**base)


def test_yaml_loader_is_c_loader_when_available():
    expected = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert okf._YAML_LOADER is expected


def test_parse_matches_pure_python_safe_loader():
    text = ("---\ntype: SourceFile\ntitle: src/a_b.py\ntags: [x, y]\n"
            "key_symbols: [alpha, beta_gamma]\ntimestamp: 2026-10-19T00:00:00Z\n---\nbody text\n")
    c = okf._parse_okf(text, Path("a.md"))
    ref = yaml.safe_load(text.split("---", 2)[1])
    assert c is not None and c.type == ref["type"] and c.tags == ref["tags"]
    assert c.extra["key_symbols"] == ref["key_symbols"]
    assert okf._parse_okf("---\n: : bad: [\n---\nx", Path("b.md")) is None


@pytest.mark.parametrize(("doc", "kws", "positive"), [
    (_doc(body="the webhooks handler retries"), ["webhook"], True),          # plural tolerance, doc side
    (_doc(body="the webhook handler retries"), ["webhooks"], True),          # plural tolerance, prompt side
    (_doc(extra={"key_symbols": ["reconcile_invoice"]}), ["reconcile_invoice"], True),
    (_doc(extra={"key_symbols": ["reconcile_invoice"]}), ["reconcile"], True),
    (_doc(title="src/llm_router/okf.py", type="SourceFile"), ["src/llm_router/okf.py"], True),
    (_doc(tags=["billing"]), ["billing"], True),
    (_doc(body="nothing relevant here"), ["webhook", "invoice"], False),
    (_doc(body="agentic loop"), ["agent"], False),                           # whole tokens only
])
def test_score_unchanged_by_prefilter(doc, kws, positive):
    assert (okf._score(doc, kws) > 0) is positive


def test_score_empty_keywords_is_zero():
    assert okf._score(_doc(body="anything"), []) == 0
