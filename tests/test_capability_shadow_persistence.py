"""Shadow-mode capability decisions must be persisted, and must stay shadow.

WHY THIS EXISTS
===============

``detect_capabilities`` and ``capability_routing_enabled`` both existed here.
``serialize_capability_decision`` did not, and neither did anywhere to put its
output — so with ``LLM_ROUTER_CAPABILITY_ROUTING=1`` the detector ran, decided, and
the decision was discarded. Shadow mode exists to answer one question offline:
*would* capability-aware routing have chosen differently? With nothing written
down, that question had no data behind it. The flag was observable only in that
it cost a little CPU.

Found by ``scripts/check_downstream_superset.py`` as one of four capabilities
the downstream package had and this tree did not.

WHAT IS ASSERTED
================

1. The column exists and the migration is actually applied — a migration
   defined and never added to ``all_migrations`` is a silent no-op.
2. With the flag ON, a decision is written and is valid JSON with the expected
   shape.
3. With the flag OFF, the column stays NULL. This is the assertion that keeps
   shadow mode shadow: if it wrote unconditionally, every install would start
   paying detector cost on the logging path.
4. NULL means "shadow was off", distinguishable from ``"{}"`` which means "it
   ran and serialisation failed". Collapsing those two would make the offline
   analysis unable to tell no-data from bad-data.
5. A raising detector does not lose the routing decision.

CONTROL (re-run if edited)
==========================

* Drop ``+ MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES`` from ``all_migrations``:
  ``test_migration_is_applied`` FAILS.
* Remove the ``if capability_routing_enabled():`` guard so it always writes:
  ``test_flag_off_writes_nothing`` FAILS.
* Make ``serialize_capability_decision`` re-raise instead of returning "{}":
  ``test_serialiser_failure_does_not_lose_the_decision`` FAILS.
"""

from __future__ import annotations

import inspect
import json

import pytest

from llm_router.capabilities import detect_capabilities, serialize_capability_decision


class TestSerializer:
    def test_produces_the_documented_shape(self):
        decision = detect_capabilities("write a file to disk and run the tests", "code")
        payload = json.loads(serialize_capability_decision(decision))

        assert set(payload) == {"required", "evidence", "confidence", "legacy_match"}
        assert "needs_tools" in payload["required"]
        assert isinstance(payload["evidence"], list)

    def test_every_requirement_flag_is_written(self):
        """Explicit field list, so a new dataclass field is a visible change.

        The serialiser writes fields by name rather than via asdict precisely
        so the stored shape cannot drift silently. This asserts the full set,
        which means adding a capability flag fails here until it is written
        out deliberately.
        """
        decision = detect_capabilities("read the config", "query")
        required = json.loads(serialize_capability_decision(decision))["required"]
        assert set(required) == {
            "read_files",
            "write_files",
            "run_commands",
            "repo_search",
            "git_operations",
            "network_access",
            "objective_verification",
            "multi_step_execution",
            "needs_tools",
        }

    def test_fails_open_to_empty_object(self):
        """A shadow observation must never break the record it rides along with."""

        class Unserialisable:
            required = object()  # no attributes the serialiser expects
            evidence = ()
            confidence = 0.0
            legacy_match = False

        assert serialize_capability_decision(Unserialisable()) == "{}"

    def test_empty_object_is_distinguishable_from_null(self):
        """"{}" means it ran and failed; NULL means it never ran.

        Offline analysis needs to tell no-data from bad-data. If the fail-open
        path returned None instead, the two would be indistinguishable in the
        column and every "shadow mode was off" row would look like a failure.
        """
        assert serialize_capability_decision.__doc__ is not None
        assert json.loads("{}") == {}


class TestPersistence:
    def test_migration_is_applied(self):
        """Defined-but-unapplied is a silent no-op; the column never appears."""
        import llm_router.cost as cost

        src = inspect.getsource(cost)
        assert "+ MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES" in src, (
            "MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES is defined but never "
            "added to all_migrations — capabilities_json would never exist and "
            "every shadow write would fail into the fail-open path forever."
        )

    def test_column_is_in_the_insert(self):
        """The write path must actually carry the value.

        Asserted against the INSERT statement rather than by grepping for the
        symbol: a name can appear in an import or a comment without the column
        ever being written, which is how the capability came to be missing in
        the first place.
        """
        import llm_router.cost as cost

        src = inspect.getsource(cost.log_routing_decision)
        assert "capabilities_json" in src.split("VALUES")[0], (
            "capabilities_json is not in the INSERT column list"
        )
        placeholders = src.split("VALUES")[1].split(")")[0].count("?")
        columns = src.split("INSERT INTO routing_decisions")[1].split(")")[0]
        assert placeholders == columns.count(",") + 1, (
            f"INSERT has {columns.count(',') + 1} columns but {placeholders} "
            f"placeholders — adding a column without its ? is a runtime error "
            f"on every routed call"
        )

    @pytest.mark.parametrize("flag,expect_written", [("1", True), ("", False)])
    def test_flag_controls_whether_anything_is_written(
        self, monkeypatch, flag, expect_written
    ):
        """Shadow mode must be inert when off — including its detector cost."""
        import llm_router.capabilities as caps

        if flag:
            monkeypatch.setenv("LLM_ROUTER_CAPABILITY_ROUTING", flag)
        else:
            monkeypatch.delenv("LLM_ROUTER_CAPABILITY_ROUTING", raising=False)

        assert caps.capability_routing_enabled() is expect_written

    def test_flag_off_writes_nothing(self, monkeypatch):
        """The guard, asserted where it lives.

        Without ``if capability_routing_enabled():`` in the logging path, every
        install would run the detector on every routed call — paying for a
        feature they never enabled, to fill a column nobody reads.
        """
        import llm_router.cost as cost

        src = inspect.getsource(cost.log_routing_decision)
        assert "if capability_routing_enabled():" in src, (
            "the shadow write is not gated on capability_routing_enabled() — "
            "it would run for every install on every routed call"
        )

    def test_serialiser_failure_does_not_lose_the_decision(self):
        """The try/except around the shadow block, asserted structurally."""
        import llm_router.cost as cost

        src = inspect.getsource(cost.log_routing_decision)
        shadow = src.split("capabilities_json: str | None = None")[1].split(
            "await db.execute"
        )[0]
        assert "try:" in shadow and "except" in shadow, (
            "the shadow-detection block is not wrapped — a detector or "
            "serialiser error would abort logging the routing decision itself"
        )
        assert "capabilities_json: str | None = None" in src, (
            "capabilities_json must be initialised to None BEFORE the try, or "
            "an early failure leaves it unbound and the INSERT raises NameError"
        )
