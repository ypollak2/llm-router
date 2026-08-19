"""Scenario framework core — TraceEvent, Scenario, ScenarioResult.

Each Scenario is a builder that records the routing journey: who said
what, which signals fired with what scores, which decision was chosen
and why, which models were attempted in what order, what the outcome
was. Tests assert on the final outcome (passes/fails like normal
pytest), but the trace is what gives the report its narrative depth.

Usage:
    def test_my_scenario(scenario_collector):
        s = Scenario(
            id="cursor-01",
            title="Cursor user requests Python refactor",
            cli="cursor",
            framework=None,
            narrative="...",
        )
        s.user("submit prompt", prompt="refactor this...")
        s.signal_fires("code_keywords", score=0.8, evidence="matched 'refactor'")
        s.signal_no_fire("pii_secret", score=0.0, evidence="no secrets")
        s.decision_chose("route_code_tasks", action="code_chain")
        s.model_call("ollama/qwen3.5:latest", success=True, cost_usd=0.0, latency_ms=2400)
        s.lineage_recorded("up_inversion=none, tier=local")
        s.outcome("Local model produced clean refactor", success=True)
        scenario_collector.add(s)
        assert s.passed
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Actor(str, Enum):
    USER = "user"
    HOST = "host"
    HOOK = "hook"
    CLASSIFIER = "classifier"
    SIGNAL = "signal"
    DECISION = "decision"
    SELECTOR = "selector"
    MODEL = "model"
    PROVIDER = "provider"
    LINEAGE = "lineage"
    SESSION = "session"
    BUDGET = "budget"
    FRAMEWORK = "framework"
    OUTCOME = "outcome"


@dataclass
class TraceEvent:
    step_no: int
    actor: Actor
    action: str
    payload: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Scenario:
    id: str
    title: str
    cli: str | None = None
    framework: str | None = None
    narrative: str = ""
    expected_outcome: str = ""
    trace: list[TraceEvent] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    duration_ms: int = 0
    passed: bool = True
    actual_outcome: str = ""
    notes: list[str] = field(default_factory=list)

    # ── Builder API ────────────────────────────────────────────────────

    def _add(self, actor: Actor, action: str, payload: dict | None = None, note: str = ""):
        self.trace.append(TraceEvent(
            step_no=len(self.trace) + 1,
            actor=actor,
            action=action,
            payload=payload or {},
            note=note,
        ))

    def user(self, action: str, **payload):
        self._add(Actor.USER, action, payload=payload)
        return self

    def host(self, action: str, **payload):
        self._add(Actor.HOST, action, payload=payload)
        return self

    def hook(self, action: str, **payload):
        self._add(Actor.HOOK, action, payload=payload)
        return self

    def classifier(self, action: str, **payload):
        self._add(Actor.CLASSIFIER, action, payload=payload)
        return self

    def signal_fires(self, name: str, *, score: float, evidence: str = ""):
        self._add(
            Actor.SIGNAL, f"{name} FIRED",
            payload={"score": score, "evidence": evidence},
        )
        return self

    def signal_no_fire(self, name: str, *, score: float, evidence: str = ""):
        self._add(
            Actor.SIGNAL, f"{name} did not fire",
            payload={"score": score, "evidence": evidence},
        )
        return self

    def decision_chose(self, name: str, *, action: str, fired_signals: tuple[str, ...] = ()):
        self._add(
            Actor.DECISION, f"{name} chose action={action!r}",
            payload={"action": action, "fired_signals": list(fired_signals)},
        )
        return self

    def selector_picked_chain(self, chain: tuple[str, ...]):
        self._add(
            Actor.SELECTOR, "chain resolved",
            payload={"chain": list(chain)},
        )
        return self

    def model_call(self, model: str, *, success: bool, cost_usd: float = 0.0,
                   latency_ms: int = 0, error: str = ""):
        verb = "succeeded" if success else "FAILED"
        self._add(
            Actor.MODEL, f"{model} {verb}",
            payload={
                "model": model, "success": success,
                "cost_usd": cost_usd, "latency_ms": latency_ms,
                "error": error,
            },
        )
        return self

    def provider_event(self, provider: str, event: str, **payload):
        self._add(Actor.PROVIDER, f"{provider}: {event}", payload=payload)
        return self

    def lineage_recorded(self, summary: str = "", **payload):
        self._add(Actor.LINEAGE, "record persisted",
                  payload=payload, note=summary)
        return self

    def session_event(self, event: str, **payload):
        self._add(Actor.SESSION, event, payload=payload)
        return self

    def budget_event(self, event: str, **payload):
        self._add(Actor.BUDGET, event, payload=payload)
        return self

    def framework_event(self, event: str, **payload):
        self._add(Actor.FRAMEWORK, event, payload=payload)
        return self

    def note(self, text: str):
        self.notes.append(text)
        return self

    def outcome(self, description: str, *, success: bool):
        self._add(
            Actor.OUTCOME, "scenario complete",
            payload={"success": success},
            note=description,
        )
        self.actual_outcome = description
        self.passed = success
        self.duration_ms = int((time.time() - self.started_at) * 1000)
        return self


class ScenarioCollector:
    """Session-scoped collection for the reporter to consume at end of run."""

    def __init__(self):
        self.scenarios: list[Scenario] = []

    def add(self, scenario: Scenario):
        self.scenarios.append(scenario)

    def __len__(self) -> int:
        return len(self.scenarios)
