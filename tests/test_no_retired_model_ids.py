"""P0.2-b (plan v16): routing code names no retired model id.

The retired set is ``pricing.retired_models()``: entries kept only so historical
rows still price. Routing code is ``src/llm_router/hooks/*.py`` plus
``router.py``. Only string literals count (f-strings included); a comment that
mentions a family ("claude-sonnet-4+") is not a routing target.

On origin/main da31df7 this test found ``claude-opus-4-6`` in auto-route.py
(the SUBSCRIPTION OVERRIDE ``/model`` line), subagent-start.py and
chain_builder.py. A test that finds nothing on the baseline proves nothing.
"""

from __future__ import annotations

import io
import os
import re
import tokenize
from pathlib import Path

from llm_router import pricing

# RETIRED_IDS_SCAN_ROOT points the same test at another checkout's
# src/llm_router (test-only; the baseline proof runs it against da31df7, where
# it must fail with >= 1 hit). Unset: this checkout.
_SRC = Path(
    os.environ.get("RETIRED_IDS_SCAN_ROOT")
    or Path(__file__).resolve().parents[1] / "src" / "llm_router"
)

# Not routing code. cc-usage-track.py prices Claude Code usage that already
# happened; its model ids are lookup keys for observed rows, not targets.
_NOT_ROUTING = {"cc-usage-track.py"}

_STRING_TOKENS = {tokenize.STRING} | {
    getattr(tokenize, name)
    for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE")
    if hasattr(tokenize, name)
}


def _routing_files() -> list[Path]:
    files = sorted(p for p in (_SRC / "hooks").glob("*.py") if p.name not in _NOT_ROUTING)
    files.append(_SRC / "router.py")
    return files


def _id_pattern(model_id: str) -> re.Pattern[str]:
    # The id itself, or the id plus an 8-digit date suffix; never a longer id
    # that merely starts with it ("claude-sonnet-4" must not match "claude-sonnet-4-6").
    return re.compile(rf"(?<![\w.-]){re.escape(model_id)}(?:-\d{{8}})?(?![\w.-])")


def _string_literals(source: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in _STRING_TOKENS:
            out.append((tok.start[0], tok.string))
    return out


def _scan(path: Path, retired: frozenset[str]) -> tuple[list[str], int]:
    """Return ("<line>: <id>" for each retired id in a string literal, n literals)."""
    patterns = {mid: _id_pattern(mid) for mid in sorted(retired)}
    literals = _string_literals(path.read_text(encoding="utf-8"))
    hits = [
        f"{line}: {mid}"
        for line, text in literals
        for mid, pat in patterns.items()
        if pat.search(text)
    ]
    return hits, len(literals)


def test_routing_code_names_no_retired_model_id():
    retired = pricing.retired_models()
    files = _routing_files()
    hits: list[str] = []
    n_literals = 0
    for path in files:
        file_hits, n = _scan(path, retired)
        n_literals += n
        hits += [f"{path.relative_to(_SRC)}:{h}" for h in file_hits]
    print(
        f"checked files={len(files)} string_literals={n_literals} "
        f"retired_ids={len(retired)} ({', '.join(sorted(retired))}) hits={len(hits)}"
    )
    # An empty check passes everything: prove there was something to check.
    assert "claude-opus-4-6" in retired
    assert len(files) >= 30
    assert n_literals > 1000
    assert not hits, "retired model ids in routing code:\n" + "\n".join(hits)


def test_scanner_catches_the_baseline_shape(tmp_path):
    """Mutation guard: the da31df7 line, verbatim in shape, is a hit."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        'x = f"⚡ SUBSCRIPTION OVERRIDE: {t}/{c} → /model claude-opus-4-6 [CRITICAL]"\n'
        "# claude-sonnet-4+ in a comment is not a target\n"
        'y = "anthropic/claude-sonnet-4-6"\n'
        'z = "claude-sonnet-4-20250514"\n',
        encoding="utf-8",
    )
    hits, n = _scan(sample, pricing.retired_models())
    assert n >= 3  # f-string parts tokenize separately on 3.12+
    assert sorted(h.split(": ", 1)[1] for h in hits) == ["claude-opus-4-6", "claude-sonnet-4"]

