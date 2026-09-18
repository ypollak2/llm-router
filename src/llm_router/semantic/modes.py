"""Three switches and a set of arms, so a result can say what produced it.

EACH SWITCH DEFAULTS TO WHAT ITS OWN EVIDENCE SUPPORTS

Source retrieval is ON: measured at n=60, paired, on the configuration that
actually ships (docs/measurements/2026-09-18-semantic-arms.md). History and
intervention are OFF: the M0/M1/M2 track has never been run. A feature that
defaults to on before its arm has been run has skipped the experiment it was
built for — which is why these are three switches and not one.

SHADOW IS A MEASUREMENT, NOT A SOFT LAUNCH

Shadow does the work, records what it cost and what it would have added, and
returns the prompt UNCHANGED — byte for byte. If shadow altered one character,
every comparison against `off` would be confounded and the mode would be a
change nobody agreed to, running under a name that says it is not a change.
There is a byte-equality test.

THREE SWITCHES, NOT ONE

Source retrieval, history retrieval and intervention fail differently and are
worth different amounts. A single flag means the first problem in any of them
disables the other two, and "what did this buy" stops being attributable.

ARMS

The research document's evaluation rests on B/C/D and M0/M1/M2. An arm sets all
three switches at once and stamps its name on the result, because a number with
no treatment attached cannot be compared with anything and offline replay of it
is not valid.

    B    corrected baseline — the five prerequisite fixes, no semantic layer
    C    budget-matched retrieval
    M0   structural baseline plus flat search over the same history
    M1   M0 plus typed, applicable experience records
    M2   M1 plus memory-guided intervention

WHAT AN ARM MAY NOT DO

Change model selection. C vs B measures retrieval; a routing arm would measure
routing, and an arm that moved both would make neither attributable. That is
the one thing this structure exists to prevent, and there is a test asserting
this module never mentions the routing symbols.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from llm_router.semantic import pack as spack


class Mode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


@dataclass(frozen=True)
class ModeConfig:
    source: Mode = Mode.OFF
    history: Mode = Mode.OFF
    intervention: Mode = Mode.OFF

    @property
    def any_enabled(self) -> bool:
        return any(m is not Mode.OFF for m in
                   (self.source, self.history, self.intervention))

    @property
    def any_active(self) -> bool:
        """Anything doing work, whether or not its output is used."""
        return self.any_enabled


# Arm → (source, history, intervention). The table IS the definition of each
# arm; anything that reads these values is reading the experiment's design.
ARMS: dict[str, ModeConfig] = {
    "B":  ModeConfig(Mode.OFF, Mode.OFF, Mode.OFF),
    "C":  ModeConfig(Mode.ON, Mode.OFF, Mode.OFF),
    "M0": ModeConfig(Mode.ON, Mode.OFF, Mode.OFF),
    "M1": ModeConfig(Mode.ON, Mode.ON, Mode.OFF),
    "M2": ModeConfig(Mode.ON, Mode.ON, Mode.ON),
}

# Arm D was "C plus two hops of graph traversal". It was run at n=60 and gave
# identical answers to C on every question, so the traversal was deleted and D
# with it — see semantic/retrieve.py and
# docs/measurements/2026-09-18-semantic-arms.md.


def _mode(raw: str) -> Mode:
    try:
        return Mode(raw.strip().lower())
    except ValueError:
        # A typo becomes off, never a guess. Guessing "on" from "onn" enables a
        # measured-nothing feature; guessing at all makes the config unreadable.
        return Mode.OFF


# Source retrieval defaults to ON. History and intervention do not.
#
# The split follows the evidence exactly. Source retrieval was measured at n=60,
# paired, against the corrected OKF baseline: 58/60 against 41/60, +28.3 points,
# 17 discordant pairs all one way, McNemar exact p=1.5e-05 — and the arm that
# was measured is the one that ships, OKF and the semantic pack TOGETHER, which
# performed identically to the pack alone on every question.
#
# History and intervention have no such number. The M0/M1/M2 track has not been
# run, so they stay off until it has. Turning on the measured half and leaving
# the unmeasured half alone is the whole reason these are three switches rather
# than one.
#
# The honest caveat, recorded here because this is where someone will look: the
# task measured is exact symbol lookup on uniquely-defined symbols, which is
# close to a best case for an ast index. See
# docs/measurements/2026-09-18-semantic-arms.md.
_DEFAULTS = {"SOURCE": Mode.ON, "HISTORY": Mode.OFF, "INTERVENTION": Mode.OFF}


def _mode_or(raw: str, fallback: Mode) -> Mode:
    """A set value wins; an unset one takes the measured default."""
    return _mode(raw) if raw.strip() else fallback


def current() -> ModeConfig:
    """The three switches, or the arm that overrides them.

    Each variable is read with its name spelled out rather than assembled from
    a prefix. `test_env_registry.py` scans the source for literals, and a name
    that only ever exists as an f-string is invisible to it — so three
    undocumented switches would have shipped, absent from the registry and from
    `llm-router doctor`, discoverable only by reading this file.
    """
    arm = os.environ.get("LLM_ROUTER_SEMANTIC_ARM", "").strip()
    if arm:
        if arm not in ARMS:
            raise ValueError(
                f"LLM_ROUTER_SEMANTIC_ARM={arm!r} is not an arm; "
                f"valid: {sorted(ARMS)}. A typo that fell back to a default "
                f"would produce a run labelled as something it was not."
            )
        return ARMS[arm]
    return ModeConfig(
        _mode_or(os.environ.get("LLM_ROUTER_SEMANTIC_SOURCE", ""),
                 _DEFAULTS["SOURCE"]),
        _mode_or(os.environ.get("LLM_ROUTER_SEMANTIC_HISTORY", ""),
                 _DEFAULTS["HISTORY"]),
        _mode_or(os.environ.get("LLM_ROUTER_SEMANTIC_INTERVENTION", ""),
                 _DEFAULTS["INTERVENTION"]),
    )


@dataclass
class Applied:
    """What the caller should use, and what it cost to decide that."""

    prompt: str
    arm: str = ""
    config: ModeConfig = ModeConfig()
    pack: spack.ContextPack | None = None
    shadow: dict[str, Any] | None = None
    # Set when an arm was selected and the run was recorded, so a later outcome
    # can be joined to the treatment that produced it. None when no arm is
    # running — ordinary use is not an experiment and should not accumulate
    # rows in an evaluation store.
    trace_id: int | None = None


def _trace_store(root: Path | str | None):
    """Traces live beside the index, under the same project namespace."""
    from llm_router import okf
    from llm_router.semantic.scope import resolve_scope
    from llm_router.semantic.traces import TraceStore

    scope = resolve_scope(root)
    return TraceStore(okf.project_knowledge_dir(root=scope) / "semantic" / "traces")


def _empty_pack(root: Path | str | None) -> spack.ContextPack:
    """A pack for an arm that deliberately retrieves nothing.

    Carries the scope and code snapshot so the baseline row says which state it
    ran on, and a `retrieval_status` of "off" that cannot be confused with
    "empty" — one means the layer was disabled, the other that it looked and
    found nothing, and conflating them makes arm B unreadable.
    """
    from llm_router.semantic.scope import resolve_scope, scope_key

    scope = resolve_scope(root)
    return spack.ContextPack(
        scope_id=scope_key(scope),
        snapshot_id=spack._snapshot_id(scope),
        retrieval_status="off",
    )


def _record_quietly(pack, prompt: str, arm: str, root) -> int | None:
    """Write a trace without letting a failed write change the experiment."""
    try:
        return _trace_store(root).record_retrieval(pack, query=prompt, arm=arm)
    except Exception as exc:                                 # noqa: BLE001
        import logging
        logging.getLogger("llm_router").debug(
            "semantic trace write failed: %s", exc)
        return None


def apply(
    prompt: str,
    root: Path | str | None = None,
    base: Path | None = None,
    experience: Any | None = None,
    budget_tokens: int = spack.DEFAULT_BUDGET_TOKENS,
) -> Applied:
    """Build a pack if this configuration says to, and attach it if allowed.

    Returns the prompt the caller should actually send. Under `off` that is the
    input unchanged and no work is done — off is off, not shadow with the
    output thrown away. Under `shadow` the work IS done, the cost is recorded,
    and the prompt is still returned unchanged.
    """
    arm = os.environ.get("LLM_ROUTER_SEMANTIC_ARM", "").strip()
    config = current()          # raises on an unknown arm, before any work
    result = Applied(prompt=prompt, arm=arm, config=config)

    if config.source is Mode.OFF and config.history is Mode.OFF:
        # Arm B lands here, and B is what every other arm is compared AGAINST.
        # Returning without a trace leaves the baseline as the one arm with no
        # rows — and an absent denominator looks exactly like a run nobody
        # performed. So a no-op arm still records that it ran, on which code,
        # and retrieved nothing on purpose.
        if arm:
            result.trace_id = _record_quietly(
                _empty_pack(root), prompt, arm, root)
        return result

    started = time.monotonic()
    built = spack.build(
        prompt,
        root=root,
        base=base,
        # History off means no experience store is consulted at all, rather
        # than consulted and filtered — otherwise M0 pays M1's retrieval cost
        # and the comparison charges the wrong arm.
        experience=experience if config.history is not Mode.OFF else None,
        budget_tokens=budget_tokens,
    )
    elapsed_ms = (time.monotonic() - started) * 1000

    # Recorded ONLY under an arm. Ordinary use is not an experiment, and a
    # trace store that fills up during normal work makes the evaluation rows
    # harder to find rather than easier — and costs a write on the hot path
    # for nothing. An arm, by contrast, exists to be compared later, which is
    # impossible without the row.
    if arm:
        result.trace_id = _record_quietly(built, prompt, arm, root)

    rendered = spack.render(built)
    # `source` governs attachment for both halves: history alone with no source
    # context is not a configuration anyone asked for, and `shadow` on either
    # switch means nothing is attached.
    attach = config.source is Mode.ON and config.history is not Mode.SHADOW

    if attach:
        result.pack = built
        result.prompt = f"{rendered}\n\n{prompt}" if rendered else prompt
        return result

    result.shadow = {
        "would_have_added_tokens": built.retrieved_tokens,
        "would_have_added_chars": len(rendered),
        "elapsed_ms": round(elapsed_ms, 2),
        "retrieval_status": built.retrieval_status,
        "evidence_count": len(built.evidence),
        "lesson_count": len(built.applicable_lessons),
        "omissions": len(built.omissions),
    }
    # The pack is exposed so a shadow run can be inspected — that is the whole
    # point of shadowing. The PROMPT is the thing that must not change, and it
    # has not been touched.
    result.pack = built
    return result
