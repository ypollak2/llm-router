"""Three switches and a set of arms, so a result can say what produced it.

OFF BY DEFAULT

Nothing here has been shown to help yet. The corrected baseline is measured
(Docs/measurements/2026-09-18-grounding-corrected-baseline.md) and the semantic
layer is not, so it ships dark and is turned on by whoever is running the
comparison. A feature that defaults to on before its arm has been run has
skipped the experiment it was built for.

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
    C    budget-matched retrieval, no graph traversal
    D    C plus bounded traversal
    M0   structural baseline plus flat search over the same history
    M1   M0 plus typed, applicable experience records
    M2   M1 plus memory-guided intervention

WHAT AN ARM MAY NOT DO

Change model selection. D vs C measures retrieval; E vs D measures routing. An
arm that moved both would make neither attributable, which is the one thing
this structure exists to prevent — and there is a test asserting this module
never mentions the routing symbols.
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
    "D":  ModeConfig(Mode.ON, Mode.OFF, Mode.OFF),
    "M0": ModeConfig(Mode.ON, Mode.OFF, Mode.OFF),
    "M1": ModeConfig(Mode.ON, Mode.ON, Mode.OFF),
    "M2": ModeConfig(Mode.ON, Mode.ON, Mode.ON),
}

# C and D share their switches and differ in traversal depth, which is the
# thing being compared. Kept separate from ARMS so the switch table stays
# readable as switches.
ARM_MAX_HOPS: dict[str, int] = {"C": 0, "D": 2, "M0": 0, "M1": 0, "M2": 0}


def _mode(raw: str) -> Mode:
    try:
        return Mode(raw.strip().lower())
    except ValueError:
        # A typo becomes off, never a guess. Guessing "on" from "onn" enables a
        # measured-nothing feature; guessing at all makes the config unreadable.
        return Mode.OFF


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
        _mode(os.environ.get("LLM_ROUTER_SEMANTIC_SOURCE", "")),
        _mode(os.environ.get("LLM_ROUTER_SEMANTIC_HISTORY", "")),
        _mode(os.environ.get("LLM_ROUTER_SEMANTIC_INTERVENTION", "")),
    )


@dataclass
class Applied:
    """What the caller should use, and what it cost to decide that."""

    prompt: str
    arm: str = ""
    config: ModeConfig = ModeConfig()
    pack: spack.ContextPack | None = None
    shadow: dict[str, Any] | None = None


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
        max_hops=ARM_MAX_HOPS.get(arm, 0),
    )
    elapsed_ms = (time.monotonic() - started) * 1000

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
