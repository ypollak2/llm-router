"""G-031 — Prometheus-format metrics for the admin API.

The audit's G-031 row called out the absence of a scrape endpoint:
structlog + OTel were wired but Prometheus was not, so any deployment
that runs a standard SOC stack had no way to graph budget burn, RBAC
denials, or audit-chain growth.

This module produces the Prometheus exposition format by hand instead
of pulling in ``prometheus_client``. The format is a stable text
protocol (``EXPOSITION_FORMAT_VERSION = 0.0.4``) — three line shapes:

::

    # HELP <name> <description>
    # TYPE <name> <counter|gauge|histogram>
    <name>{<label>="<value>",…} <number>

Avoiding the third-party dep keeps the routing core lightweight and
the surface easy to audit. The trade-off is that we have only
counters and gauges — no histograms / summaries. Adequate for what
G-031 asked for; a future slice can add ``prometheus_client`` as an
optional install if histograms become useful.

Metrics surfaced (the inventory G-031 promised):

* ``llm_router_session_count{state}`` — agent session totals by state
* ``llm_router_session_consumed_usd_total{state}`` — aggregate budget
  burn rolled up per state
* ``llm_router_admin_actions_total{action}`` — per-action counts from
  ``admin_actions.db``
* ``llm_router_audit_chain_length`` — row count of the hash-chained
  routing audit
* ``llm_router_disabled_providers`` /
  ``llm_router_disabled_models`` — emergency-disable surface
* ``llm_router_policy_active_version`` — currently-active org policy
  version (-1 when none has been pushed)
* ``llm_router_subscription_pressure{provider}`` — live quota pressure
  from ``quota_balance.get_provider_pressures``
* ``llm_router_metrics_render_seconds`` — self-instrumentation so
  operators can graph the cost of scraping
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable


# Exposition-format MIME type. Prometheus scrapers send
# ``Accept: text/plain; version=0.0.4`` and expect this back.
EXPOSITION_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@dataclass(frozen=True)
class Metric:
    """One ``# HELP`` / ``# TYPE`` block + its samples.

    ``samples`` is an iterable of ``(labels_dict, value)`` pairs.
    Labels are merged into the ``{key="value",…}`` clause on the
    rendered sample line; the dict can be empty for unlabelled
    metrics.
    """

    name: str
    help_text: str
    kind: str  # "counter" | "gauge"
    samples: tuple[tuple[dict[str, str], float], ...]


def _escape_label_value(raw: str) -> str:
    """Per the exposition spec: backslash, double-quote, and newline
    must be escaped in label values. All other characters pass
    through unchanged."""
    return (
        raw.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _render_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [
        f'{k}="{_escape_label_value(v)}"'
        for k, v in sorted(labels.items())
    ]
    return "{" + ",".join(parts) + "}"


def render(metrics: Iterable[Metric]) -> str:
    """Render an iterable of ``Metric`` blocks to the Prometheus
    exposition format. Each metric prints its HELP + TYPE header
    once, then one line per sample."""
    out: list[str] = []
    for m in metrics:
        out.append(f"# HELP {m.name} {m.help_text}")
        out.append(f"# TYPE {m.name} {m.kind}")
        for labels, value in m.samples:
            label_str = _render_labels(labels)
            out.append(f"{m.name}{label_str} {value}")
    out.append("")  # trailing newline (spec-required)
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────────
# Source-of-truth collectors. Each is a pure function from a
# subsystem handle to a ``Metric`` block. Failures are swallowed so
# one broken collector cannot take down the whole scrape — the
# metric still appears but its samples list is empty, which a
# Prometheus alert can spot ("metric absent for 5m").
# ────────────────────────────────────────────────────────────────────────


def _safe(
    collector_fn,
    name: str,
    help_text: str,
    kind: str,
) -> Metric:
    """Call ``collector_fn`` and wrap a Metric. Any exception is
    swallowed; the metric exposes its HELP + TYPE header but no
    samples so scrapers and alerts can detect the gap."""
    try:
        samples = tuple(collector_fn())
    except Exception:
        samples = ()
    return Metric(
        name=name, help_text=help_text, kind=kind, samples=samples,
    )


def collect_session_metrics(sessions) -> Iterable[Metric]:
    """Per-state session counts + budget-burn totals. ``sessions``
    is a ``SessionStore`` instance — query its underlying connection
    directly so we don't pay for a Python decode-per-row."""

    def counts():
        rows = sessions._conn.execute(
            "SELECT state, COUNT(*) FROM sessions GROUP BY state"
        ).fetchall()
        for state, n in rows:
            yield ({"state": str(state)}, float(n))

    def consumed():
        rows = sessions._conn.execute(
            "SELECT state, COALESCE(SUM(consumed_usd), 0) "
            "FROM sessions GROUP BY state"
        ).fetchall()
        for state, total in rows:
            yield ({"state": str(state)}, float(total))

    yield _safe(
        counts, "llm_router_session_count",
        "Agent session totals grouped by lifecycle state.",
        "gauge",
    )
    yield _safe(
        consumed, "llm_router_session_consumed_usd_total",
        "Aggregate budget burn (USD) rolled up per session state.",
        "counter",
    )


def collect_admin_action_metrics(admin_log) -> Iterable[Metric]:
    def counts():
        rows = admin_log._conn.execute(
            "SELECT action, COUNT(*) FROM admin_actions GROUP BY action"
        ).fetchall()
        for action, n in rows:
            yield ({"action": str(action)}, float(n))

    yield _safe(
        counts, "llm_router_admin_actions_total",
        "Cumulative count of admin-API mutations grouped by action.",
        "counter",
    )


def collect_audit_chain_metrics(audit_log) -> Iterable[Metric]:
    def length():
        (n,) = audit_log._conn.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()
        yield ({}, float(n))

    yield _safe(
        length, "llm_router_audit_chain_length",
        "Length of the hash-chained routing audit log.",
        "gauge",
    )


def collect_registry_metrics(registry) -> Iterable[Metric]:
    yield _safe(
        lambda: [({}, float(len(registry.list_disabled())))],
        "llm_router_disabled_providers",
        "Number of providers currently marked disabled "
        "(G-006-F2 emergency-disable surface).",
        "gauge",
    )
    yield _safe(
        lambda: [({}, float(len(registry.list_disabled_models())))],
        "llm_router_disabled_models",
        "Number of individual models currently marked disabled "
        "(G-006-F2 finisher).",
        "gauge",
    )


def collect_policy_metrics(policy_store) -> Iterable[Metric]:
    yield _safe(
        lambda: [
            ({}, float(policy_store.active_version() or -1)),
        ],
        "llm_router_policy_active_version",
        "Currently-active org-policy version (-1 when none has been "
        "pushed yet).",
        "gauge",
    )


def collect_subscription_pressure_metrics() -> Iterable[Metric]:
    """Pull live quota pressure from ``quota_balance``. Async-only
    so we run the call through a fresh event loop; failures fall
    back to empty samples per the ``_safe`` contract."""
    import asyncio

    def pressures():
        from llm_router.quota_balance import get_provider_pressures

        try:
            data = asyncio.run(get_provider_pressures())
        except RuntimeError:
            # Already inside an event loop (admin-API runs under
            # uvicorn). Return empty samples so the scrape still
            # exposes the metric shape; a future slice can replace
            # this with a sync passthrough.
            return ()
        return tuple(
            ({"provider": str(name)}, float(value))
            for name, value in data.items()
        )

    yield _safe(
        pressures, "llm_router_subscription_pressure",
        "Live 5-hour quota pressure (0.0–1.0) per subscription "
        "provider, sourced from ``quota_balance.get_provider_pressures``.",
        "gauge",
    )


def collect_self_metrics(render_start_ts: float) -> Iterable[Metric]:
    """Render-cost self-instrumentation — emit at the END of the
    collection pass so the value reflects the work above it."""
    yield Metric(
        name="llm_router_metrics_render_seconds",
        help_text="Wall-clock seconds spent producing this scrape.",
        kind="gauge",
        samples=(({}, time.time() - render_start_ts),),
    )


def collect_all(
    *,
    sessions=None,
    admin_log=None,
    audit_log=None,
    registry=None,
    policy_store=None,
    include_subscription_pressure: bool = True,
) -> str:
    """Top-level entry. Renders the full exposition block.

    Every collector is optional — pass ``None`` for any subsystem
    you don't want sampled. Missing subsystems are simply absent
    from the output (no fake zero samples). This is so test rigs
    can exercise individual collectors without standing up the
    whole admin API.
    """
    started = time.time()
    blocks: list[Metric] = []
    if sessions is not None:
        blocks.extend(collect_session_metrics(sessions))
    if admin_log is not None:
        blocks.extend(collect_admin_action_metrics(admin_log))
    if audit_log is not None:
        blocks.extend(collect_audit_chain_metrics(audit_log))
    if registry is not None:
        blocks.extend(collect_registry_metrics(registry))
    if policy_store is not None:
        blocks.extend(collect_policy_metrics(policy_store))
    if include_subscription_pressure:
        blocks.extend(collect_subscription_pressure_metrics())
    blocks.extend(collect_self_metrics(started))
    return render(blocks)


__all__ = [
    "EXPOSITION_CONTENT_TYPE",
    "Metric",
    "collect_admin_action_metrics",
    "collect_all",
    "collect_audit_chain_metrics",
    "collect_policy_metrics",
    "collect_registry_metrics",
    "collect_self_metrics",
    "collect_session_metrics",
    "collect_subscription_pressure_metrics",
    "render",
]
