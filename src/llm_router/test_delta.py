"""Quota test-delta — snapshot + diff usage state across a controlled test.

Background
----------
Routing-quality fixes (gap closers committed in this session) need a
way to be **measured**, not just believed. Running benchmarks doesn't
test the live router; it tests the benchmark harness. The truthful
measurement is: take a snapshot of every routing-relevant table, ask
the user to run a controlled task in another Claude Code session, take
a second snapshot, diff them.

The diff surfaces four numbers that matter:

* ``routed_calls`` — count + cost of LLM calls through llm_router MCP tools
  (was 31 over the last 60min before today's fixes).
* ``claude_calls`` — count + tokens of Claude Code's native turns
  (the dominant quota consumer in coding sessions).
* ``simple_share`` — fraction of routed calls classified as simple
  (was 0/31 today; should rise after the boundary fix).
* ``opus_baseline_counterfactual`` — what the same routed work would
  have cost if every routed call had gone to Opus 4.6 instead. The
  delta between actual and counterfactual is the savings number.

Snapshot files live at ``~/.llm-router/test_delta/<id>.json``. IDs are
``YYYYMMDD-HHMMSS-<6hex>`` so they sort by time but can't collide
between concurrent shells. JSON shape is pinned (see ``SNAPSHOT_SCHEMA``)
so a future client can read older snapshots without coercion.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".llm-router" / "usage.db"
SNAPSHOT_DIR = Path.home() / ".llm-router" / "test_delta"

# Headline metric: what the same number of input/output tokens would have cost
# if every routed call had gone to Opus (the host model).
#
# WP-03: the comment here claimed this "keeps the comparison apples-to-apples
# even as model pricing drifts", directly above two literals frozen at the
# retired $15/$75 Opus 3 tier. The claim was true of the *method* and false of
# the implementation — and it is the headline metric, so it was wrong by 3x.
from llm_router import pricing as _pricing  # noqa: E402

_opus_price = _pricing.price_for("opus")
OPUS_INPUT_PER_M = _opus_price.input if _opus_price else 0.0
OPUS_OUTPUT_PER_M = _opus_price.output if _opus_price else 0.0

SNAPSHOT_SCHEMA = 1


# ── Data ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TableSnapshot:
    """Counts + sums per row of interest."""
    rows: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    by_tier: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """One point-in-time read of usage.db tables."""
    schema: int
    id: str
    captured_at: float
    db_path: str
    routing: TableSnapshot
    claude: TableSnapshot
    usage: TableSnapshot

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "id": self.id,
            "captured_at": self.captured_at,
            "db_path": self.db_path,
            "routing": asdict(self.routing),
            "claude": asdict(self.claude),
            "usage": asdict(self.usage),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(
            schema=int(data["schema"]),
            id=str(data["id"]),
            captured_at=float(data["captured_at"]),
            db_path=str(data["db_path"]),
            routing=TableSnapshot(**data["routing"]),
            claude=TableSnapshot(**data["claude"]),
            usage=TableSnapshot(**data["usage"]),
        )


# ── Snapshot ────────────────────────────────────────────────────────────


def _read_routing_table(conn: sqlite3.Connection) -> TableSnapshot:
    """Aggregate ``routing_decisions`` — the canonical 'what did llm_router
    route this turn' record. Excludes sidecar backfills since those
    don't reflect real traffic."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(routing_decisions)")}
    cost_col = "cost_usd" if "cost_usd" in cols else "0"
    in_col = "input_tokens" if "input_tokens" in cols else "0"
    out_col = "output_tokens" if "output_tokens" in cols else "0"
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({cost_col}),0), "
        f"COALESCE(SUM({in_col}),0), COALESCE(SUM({out_col}),0) "
        f"FROM routing_decisions "
        f"WHERE COALESCE(reason_code,'') != 'sidecar_backfill'"
    ).fetchone()
    rows, cost, in_tok, out_tok = row
    by_tier = dict(conn.execute(
        "SELECT COALESCE(complexity,'unknown'), COUNT(*) "
        "FROM routing_decisions "
        "WHERE COALESCE(reason_code,'') != 'sidecar_backfill' "
        "GROUP BY complexity"
    ).fetchall())
    by_model = dict(conn.execute(
        "SELECT COALESCE(final_model,'unknown'), COUNT(*) "
        "FROM routing_decisions "
        "WHERE COALESCE(reason_code,'') != 'sidecar_backfill' "
        "GROUP BY final_model"
    ).fetchall())
    return TableSnapshot(
        rows=int(rows),
        cost_usd=float(cost),
        input_tokens=int(in_tok),
        output_tokens=int(out_tok),
        by_tier={str(k): int(v) for k, v in by_tier.items()},
        by_model={str(k): int(v) for k, v in by_model.items()},
    )


def _read_claude_table(conn: sqlite3.Connection) -> TableSnapshot:
    """Aggregate ``claude_usage`` — Claude Code's native turns. This is
    the dominant quota consumer in a coding session; the gap closers
    don't reduce it directly but the test-delta proves whether
    something *outside* this module is moving the number."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(claude_usage)")}
    if "tokens_used" not in cols:
        return TableSnapshot(rows=0, cost_usd=0.0, input_tokens=0, output_tokens=0)
    cost_col = "cost_saved_usd" if "cost_saved_usd" in cols else "0"
    rows_count, total_tokens, total_cost = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(tokens_used),0), COALESCE(SUM({cost_col}),0) "
        f"FROM claude_usage"
    ).fetchone()
    by_model = dict(conn.execute(
        "SELECT COALESCE(model,'unknown'), COUNT(*) FROM claude_usage GROUP BY model"
    ).fetchall())
    return TableSnapshot(
        rows=int(rows_count),
        cost_usd=float(total_cost),
        input_tokens=int(total_tokens),  # claude_usage records combined; surface as input
        output_tokens=0,
        by_model={str(k): int(v) for k, v in by_model.items()},
    )


def _read_usage_table(conn: sqlite3.Connection) -> TableSnapshot:
    """Aggregate the catch-all ``usage`` table — every LLM call regardless
    of router path. Crosschecks routing_decisions; a divergence between
    the two would indicate a routing-bypass we're not measuring."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
    if not cols:
        return TableSnapshot(rows=0, cost_usd=0.0, input_tokens=0, output_tokens=0)
    cost_col = "cost_usd" if "cost_usd" in cols else "0"
    in_col = "input_tokens" if "input_tokens" in cols else "0"
    out_col = "output_tokens" if "output_tokens" in cols else "0"
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({cost_col}),0), "
        f"COALESCE(SUM({in_col}),0), COALESCE(SUM({out_col}),0) "
        f"FROM usage WHERE COALESCE(success,1)=1"
    ).fetchone()
    rows, cost, in_tok, out_tok = row
    by_model = dict(conn.execute(
        "SELECT COALESCE(model,'unknown'), COUNT(*) FROM usage "
        "WHERE COALESCE(success,1)=1 GROUP BY model ORDER BY 2 DESC LIMIT 20"
    ).fetchall())
    return TableSnapshot(
        rows=int(rows),
        cost_usd=float(cost),
        input_tokens=int(in_tok),
        output_tokens=int(out_tok),
        by_model={str(k): int(v) for k, v in by_model.items()},
    )


def snapshot(db_path: Path | None = None) -> Snapshot:
    """Capture a Snapshot of the routing-relevant tables."""
    db = db_path or DEFAULT_DB_PATH
    if not db.is_file():
        # Treat missing DB as a fresh state — every diff against this is
        # the full delta of whatever happens next.
        return Snapshot(
            schema=SNAPSHOT_SCHEMA,
            id=_new_id(),
            captured_at=time.time(),
            db_path=str(db),
            routing=TableSnapshot(0, 0.0, 0, 0),
            claude=TableSnapshot(0, 0.0, 0, 0),
            usage=TableSnapshot(0, 0.0, 0, 0),
        )

    conn = sqlite3.connect(str(db))
    try:
        return Snapshot(
            schema=SNAPSHOT_SCHEMA,
            id=_new_id(),
            captured_at=time.time(),
            db_path=str(db),
            routing=_read_routing_table(conn),
            claude=_read_claude_table(conn),
            usage=_read_usage_table(conn),
        )
    finally:
        conn.close()


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def save_snapshot(snap: Snapshot, root: Path | None = None) -> Path:
    """Persist a snapshot under ``~/.llm-router/test_delta/<id>.json``."""
    root = root or SNAPSHOT_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{snap.id}.json"
    path.write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
    return path


def load_snapshot(snap_id: str, root: Path | None = None) -> Snapshot:
    root = root or SNAPSHOT_DIR
    path = root / f"{snap_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"snapshot not found: {path}")
    return Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))


# ── Diff ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TableDelta:
    """Difference between two snapshots of the same table."""
    added_rows: int
    added_cost_usd: float
    added_input_tokens: int
    added_output_tokens: int
    by_tier_added: dict[str, int]
    by_model_added: dict[str, int]


@dataclass(frozen=True)
class DeltaReport:
    """Headline numbers across the routing + claude + usage tables."""
    before_id: str
    after_id: str
    elapsed_sec: float
    routing: TableDelta
    claude: TableDelta
    usage: TableDelta

    @property
    def opus_baseline_for_routed(self) -> float:
        """What the routed calls would have cost if they'd all gone to Opus 4.6."""
        return (
            self.routing.added_input_tokens * OPUS_INPUT_PER_M
            + self.routing.added_output_tokens * OPUS_OUTPUT_PER_M
        ) / 1_000_000.0

    @property
    def savings_usd_vs_opus(self) -> float:
        return max(0.0, self.opus_baseline_for_routed - self.routing.added_cost_usd)

    @property
    def simple_share(self) -> float:
        added = self.routing.by_tier_added
        total = sum(added.values()) or 1
        return added.get("simple", 0) / total


def _diff_table(before: TableSnapshot, after: TableSnapshot) -> TableDelta:
    return TableDelta(
        added_rows=after.rows - before.rows,
        added_cost_usd=after.cost_usd - before.cost_usd,
        added_input_tokens=after.input_tokens - before.input_tokens,
        added_output_tokens=after.output_tokens - before.output_tokens,
        by_tier_added=_dict_subtract(after.by_tier, before.by_tier),
        by_model_added=_dict_subtract(after.by_model, before.by_model),
    )


def _dict_subtract(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    keys = set(after) | set(before)
    return {
        k: max(0, after.get(k, 0) - before.get(k, 0))
        for k in keys
        if after.get(k, 0) > before.get(k, 0)
    }


def diff(before: Snapshot, after: Snapshot) -> DeltaReport:
    return DeltaReport(
        before_id=before.id,
        after_id=after.id,
        elapsed_sec=max(0.0, after.captured_at - before.captured_at),
        routing=_diff_table(before.routing, after.routing),
        claude=_diff_table(before.claude, after.claude),
        usage=_diff_table(before.usage, after.usage),
    )


# ── Reporting ───────────────────────────────────────────────────────────


def render_markdown(report: DeltaReport) -> str:
    """Human-readable summary suitable for pasting into a PR or chat."""
    lines: list[str] = []
    lines.append(f"# Quota delta · {report.before_id} → {report.after_id}")
    lines.append("")
    lines.append(f"* Elapsed: **{report.elapsed_sec:.0f}s**")
    lines.append("")
    lines.append("## Routed calls (llm_router MCP tools)")
    r = report.routing
    lines.append(f"* New routed calls: **{r.added_rows}**")
    lines.append(f"* Cost: **${r.added_cost_usd:.4f}**")
    lines.append(f"* Tokens: **{r.added_input_tokens}** in / **{r.added_output_tokens}** out")
    if r.by_tier_added:
        lines.append("* By tier:")
        for tier, n in sorted(r.by_tier_added.items(), key=lambda kv: -kv[1]):
            lines.append(f"  * `{tier}` — {n}")
    lines.append(f"* **Simple share:** {report.simple_share * 100:.1f}%  "
                 f"(target ≥ 50% on coding sessions after the boundary fix)")
    lines.append("")
    lines.append("## Counterfactual — Opus 4.6 baseline")
    lines.append(f"* Routed work would have cost **${report.opus_baseline_for_routed:.4f}** at Opus 4.6")
    lines.append(f"* Realised savings: **${report.savings_usd_vs_opus:.4f}** "
                 f"({report.savings_usd_vs_opus / max(report.opus_baseline_for_routed, 1e-9) * 100:.1f}%)")
    lines.append("")
    lines.append("## Claude Code native turns (claude_usage)")
    c = report.claude
    lines.append(f"* New rows: **{c.added_rows}**")
    lines.append(f"* Tokens recorded: **{c.added_input_tokens}** "
                 "(claude_usage stores combined input+output as tokens_used)")
    if c.by_model_added:
        lines.append("* By model:")
        for model, n in sorted(c.by_model_added.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  * `{model}` — {n}")
    lines.append("")
    lines.append("## All LLM calls (usage table — cross-check)")
    u = report.usage
    lines.append(f"* New rows: **{u.added_rows}** "
                 "(should be ≥ routed rows; divergence = bypass)")
    if u.by_model_added:
        lines.append("* By model:")
        for model, n in sorted(u.by_model_added.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  * `{model}` — {n}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────


def _cmd_snapshot(_args: argparse.Namespace) -> int:
    snap = snapshot()
    path = save_snapshot(snap)
    print(f"snapshot saved: {snap.id}  →  {path}")
    print(f"routing_decisions: {snap.routing.rows} rows so far")
    print(f"claude_usage:      {snap.claude.rows} rows so far")
    print(f"usage:             {snap.usage.rows} rows so far")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    before = load_snapshot(args.before)
    after = snapshot() if args.after == "now" else load_snapshot(args.after)
    if args.after == "now":
        save_snapshot(after)
    report = diff(before, after)
    if args.format == "json":
        print(json.dumps({
            "before_id": report.before_id,
            "after_id": report.after_id,
            "elapsed_sec": report.elapsed_sec,
            "routing": asdict(report.routing),
            "claude": asdict(report.claude),
            "usage": asdict(report.usage),
            "opus_baseline_for_routed": report.opus_baseline_for_routed,
            "savings_usd_vs_opus": report.savings_usd_vs_opus,
            "simple_share": report.simple_share,
        }, indent=2))
    else:
        print(render_markdown(report))
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    if not SNAPSHOT_DIR.is_dir():
        print("(no snapshots yet)")
        return 0
    files = sorted(SNAPSHOT_DIR.glob("*.json"))
    for path in files:
        try:
            snap = Snapshot.from_dict(json.loads(path.read_text()))
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.captured_at))
            print(f"  {snap.id}  ({ts})  routed={snap.routing.rows}  claude={snap.claude.rows}")
        except Exception as err:
            print(f"  {path.name}  (unreadable: {err})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm_router test-delta")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="Capture current state.")
    sp.set_defaults(func=_cmd_snapshot)

    sp = sub.add_parser("diff", help="Diff two snapshots (or against now).")
    sp.add_argument("--before", required=True)
    sp.add_argument("--after", default="now",
                    help="Snapshot id, or 'now' to capture + diff. Default 'now'.")
    sp.add_argument("--format", choices=("markdown", "json"), default="markdown")
    sp.set_defaults(func=_cmd_diff)

    sp = sub.add_parser("list", help="List saved snapshots.")
    sp.set_defaults(func=_cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
