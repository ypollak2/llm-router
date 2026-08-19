"""Render collected Scenarios as a narrative markdown report.

The output is a single document at Docs/SCENARIO_REPORT.md with:
    1. Executive scoreboard — counts per CLI / framework, pass/fail
    2. Per-scenario sections — title, CLI, framework, narrative,
       trace as a numbered story, outcome, notes
    3. Coverage matrix — which CLI × framework combinations are
       exercised

Designed to read like a story, not a test log. Each TraceEvent renders
as one bullet that explains what the actor did in plain language.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from tests.scenarios.core import Actor, Scenario, TraceEvent


_ACTOR_GLYPHS = {
    Actor.USER: "🧑",
    Actor.HOST: "🖥️",
    Actor.HOOK: "🪝",
    Actor.CLASSIFIER: "🧭",
    Actor.SIGNAL: "📡",
    Actor.DECISION: "⚖️",
    Actor.SELECTOR: "🎯",
    Actor.MODEL: "🤖",
    Actor.PROVIDER: "🌐",
    Actor.LINEAGE: "📜",
    Actor.SESSION: "🪑",
    Actor.BUDGET: "💵",
    Actor.FRAMEWORK: "🧱",
    Actor.OUTCOME: "🏁",
}


def _format_payload(payload: dict) -> str:
    """Render payload dict as inline `key=value` pairs, eliding noise."""
    if not payload:
        return ""
    bits = []
    for k, v in payload.items():
        if v in ("", None, [], {}, ()):
            continue
        if isinstance(v, float):
            bits.append(f"{k}={v:.4f}".rstrip("0").rstrip("."))
        elif isinstance(v, (list, tuple)):
            bits.append(f"{k}=[{', '.join(map(str, v))}]")
        elif isinstance(v, str) and len(v) > 80:
            bits.append(f"{k}={v[:77]!r}…")
        else:
            bits.append(f"{k}={v!r}")
    return " · ".join(bits)


def _render_event(event: TraceEvent) -> str:
    glyph = _ACTOR_GLYPHS.get(event.actor, "•")
    payload = _format_payload(event.payload)
    parts = [f"{event.step_no}. **{glyph} [{event.actor.value}]** {event.action}"]
    if payload:
        parts.append(f"\n     · {payload}")
    if event.note:
        parts.append(f"\n     › _{event.note}_")
    return "".join(parts)


def render_scenario(scenario: Scenario) -> str:
    """Render one scenario as a markdown section."""
    out = []
    status = "✅ PASS" if scenario.passed else "❌ FAIL"
    out.append(f"## {scenario.id} · {scenario.title}\n")
    meta_bits = [f"**Status:** {status}", f"**Duration:** {scenario.duration_ms} ms"]
    if scenario.cli:
        meta_bits.append(f"**CLI:** `{scenario.cli}`")
    if scenario.framework:
        meta_bits.append(f"**Framework:** `{scenario.framework}`")
    out.append(" · ".join(meta_bits))
    out.append("")

    if scenario.narrative:
        out.append("### Narrative")
        out.append(scenario.narrative.strip())
        out.append("")

    if scenario.expected_outcome:
        out.append(f"**Expected:** {scenario.expected_outcome}")
        out.append("")

    out.append("### What really happened")
    if not scenario.trace:
        out.append("_(no trace events recorded)_")
    else:
        for event in scenario.trace:
            out.append(_render_event(event))
    out.append("")

    if scenario.actual_outcome:
        out.append(f"**Actual outcome:** {scenario.actual_outcome}")
        out.append("")

    if scenario.notes:
        out.append("### Notes")
        for n in scenario.notes:
            out.append(f"- {n}")
        out.append("")

    return "\n".join(out)


def render_executive_summary(scenarios: list[Scenario]) -> str:
    """Top-of-report scoreboard."""
    total = len(scenarios)
    passed = sum(1 for s in scenarios if s.passed)
    failed = total - passed
    by_cli = Counter(s.cli or "—" for s in scenarios)
    by_framework = Counter(s.framework or "—" for s in scenarios)

    out = []
    out.append("# LLM Router Scenario Report\n")
    out.append("Each scenario below is a *story*, not just a pass/fail.")
    out.append("The trace shows every actor that touched the request — the")
    out.append("host CLI, the classifier, each signal, the decision engine,")
    out.append("the selector, the model, the provider, the lineage and")
    out.append("session stores — so you can audit what actually happened.")
    out.append("")
    out.append("## Executive summary\n")
    out.append("| Metric | Value |")
    out.append("|---|---|")
    out.append(f"| Total scenarios | {total} |")
    out.append(f"| Passed | **{passed}** |")
    out.append(f"| Failed | **{failed}** |")
    out.append(
        f"| Total trace events | {sum(len(s.trace) for s in scenarios)} |"
    )
    out.append(
        f"| Cumulative duration | {sum(s.duration_ms for s in scenarios)} ms |"
    )
    out.append("")

    out.append("### Per-CLI coverage\n")
    out.append("| CLI | Scenarios |")
    out.append("|---|---|")
    for cli, n in sorted(by_cli.items()):
        out.append(f"| `{cli}` | {n} |")
    out.append("")

    out.append("### Per-framework coverage\n")
    out.append("| Framework | Scenarios |")
    out.append("|---|---|")
    for fw, n in sorted(by_framework.items()):
        out.append(f"| `{fw}` | {n} |")
    out.append("")

    return "\n".join(out)


def render_coverage_matrix(scenarios: list[Scenario]) -> str:
    """A CLI × framework matrix showing where coverage exists."""
    clis = sorted({s.cli for s in scenarios if s.cli})
    frameworks = sorted({s.framework for s in scenarios if s.framework})

    if not clis or not frameworks:
        return ""

    out = ["## Coverage matrix (CLI × framework)\n"]
    header = "| CLI ╲ Framework | " + " | ".join(f"`{f}`" for f in frameworks) + " |"
    sep = "|---|" + "---|" * len(frameworks)
    out.append(header)
    out.append(sep)

    pairs: set[tuple[str, str]] = set()
    for s in scenarios:
        if s.cli and s.framework:
            pairs.add((s.cli, s.framework))

    for cli in clis:
        row = [f"`{cli}`"]
        for fw in frameworks:
            row.append("✓" if (cli, fw) in pairs else "—")
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    return "\n".join(out)


def write_report(scenarios: list[Scenario], out_path: Path) -> Path:
    """Render the full report to out_path. Returns the written path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sections = [render_executive_summary(scenarios)]
    if any(s.cli and s.framework for s in scenarios):
        sections.append(render_coverage_matrix(scenarios))

    sections.append("---\n")
    sections.append("## Scenarios\n")
    # Sort scenarios by CLI then framework then id so the report reads top-to-bottom
    sorted_scenarios = sorted(
        scenarios,
        key=lambda s: (s.cli or "~", s.framework or "~", s.id),
    )
    for sc in sorted_scenarios:
        sections.append(render_scenario(sc))
        sections.append("---\n")

    out_path.write_text("\n".join(sections))
    return out_path
