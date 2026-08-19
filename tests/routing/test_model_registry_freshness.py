"""WP-12 / RED8-08 — the capability ladder is curated, and the cadence is enforced.

NORTH_STAR described the model ranking as "live" and "continuously-updated". It
is a hand-edited YAML snapshot; nothing fetches a ranking at runtime. The locked
decision (Option B) was to keep the static ladder, stop calling it live, and
enforce a refresh cadence — because a manually-refreshed snapshot with no
enforcement is a stale snapshot nobody has noticed yet.

The proof that a convention was not enough: `config/models.yaml` told readers to
refresh via `scripts/refresh-model-registry.py`, a file that does not exist and
is not in the repository history. The documented cadence pointed at nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_model_registry_freshness.py"
REGISTRY = REPO / "config" / "models.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("_freshness", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_registry_declares_a_snapshot_date():
    """An undated snapshot cannot be checked, which is where this started."""
    mod = _load()
    assert mod.read_snapshot_date(REGISTRY) is not None, (
        "config/models.yaml has no parseable snapshot_date"
    )


def test_the_snapshot_is_currently_fresh():
    mod = _load()
    assert mod.main(["prog"]) == 0


def test_a_stale_snapshot_fails_the_build():
    """The check must be able to fail, or it is decoration.

    Evaluated against an explicit date rather than the wall clock, so the
    assertion does not change its own answer as time passes — the same mistake
    the pricing tests made with Sonnet's introductory window.
    """
    mod = _load()
    snapshot = mod.read_snapshot_date(REGISTRY)
    long_after = snapshot.replace(year=snapshot.year + 2).isoformat()
    assert mod.main(["prog", "--today", long_after]) == 1


def test_a_missing_snapshot_date_is_an_error_not_a_pass(tmp_path, monkeypatch):
    """Fail closed. A registry that lost its date must not read as fresh."""
    mod = _load()
    undated = tmp_path / "models.yaml"
    undated.write_text("models:\n  - id: x\n", encoding="utf-8")
    monkeypatch.setattr(mod, "REGISTRY", undated)
    assert mod.main(["prog"]) == 2


def test_the_dead_refresh_script_is_not_silently_referenced():
    """RED8-08's tell: two files pointed at a script that was never written.

    Any surviving reference must be accompanied by a correction, otherwise a
    reader follows it and concludes the refresh is automated.
    """
    for path in (REGISTRY, REPO / "src" / "llm_router" / "model_registry.py"):
        text = path.read_text(encoding="utf-8")
        if "refresh-model-registry.py" not in text:
            continue
        assert "does not exist" in text, (
            f"{path.name} references the nonexistent refresh script without "
            f"saying that it does not exist"
        )


def test_the_refresh_script_still_does_not_exist():
    """Guards the correction itself.

    If someone writes the script, these warnings become wrong in the other
    direction — the failure mode WP-00's stale warnings had. This fails loudly
    at that moment so the docs get updated with it.
    """
    if (REPO / "scripts" / "refresh-model-registry.py").exists():
        pytest.fail(
            "scripts/refresh-model-registry.py now exists — remove the 'does "
            "not exist' warnings from config/models.yaml and model_registry.py"
        )


def test_north_star_no_longer_asserts_a_live_leaderboard():
    """WP-12 acceptance: no text asserting live/continuously-updated."""
    text = (REPO / "Docs" / "planning" / "NORTH_STAR.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        low = line.lower()
        if "continuously-updated" in low or "live leaderboard" in low:
            # The correction note is allowed to quote what it is correcting.
            assert low.lstrip().startswith(">"), (
                f"NORTH_STAR still asserts a live ranking outside the "
                f"correction note: {line.strip()!r}"
            )
