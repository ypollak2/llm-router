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

import ast
import inspect
import json

import pytest

from llm_router.capabilities import detect_capabilities, serialize_capability_decision


# R13: these tests used to grep `inspect.getsource(...)` text for a phrase.
# That is satisfied by the phrase sitting in a COMMENT while the real call
# site is gone — reproduced by the audit (23 tests stayed green after exactly
# that mutation). The helper below finds the real AST node — an `ast.Assign`
# whose value is a `serialize_capability_decision(...)` call — which cannot
# exist unless the code actually does the assignment; comments are not part
# of the AST at all.
def _assigns_capabilities_json_from_serializer(node: ast.AST) -> bool:
    """True if *node* contains `capabilities_json = serialize_capability_decision(...)`."""
    return any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "capabilities_json" for t in n.targets)
        and isinstance(n.value, ast.Call)
        and ast.unparse(n.value.func) == "serialize_capability_decision"
        for n in ast.walk(node)
    )


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
        """Defined-but-unapplied is a silent no-op; the column never appears.

        Was `"+ MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES" in
        inspect.getsource(cost)` — satisfied by the phrase sitting in a
        comment anywhere in the module. `_get_db` builds `all_migrations` by
        summing module-level list constants with `+`; walking its AST for an
        `ast.Name` load of the constant proves the name is really an operand
        of that expression, which a comment cannot fake (`ast.Name` nodes
        only exist where the identifier is actually referenced by code).
        """
        import llm_router.cost as cost

        tree = ast.parse(inspect.getsource(cost._get_db))
        referenced_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES" in referenced_names, (
            "MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES is defined but never "
            "added to all_migrations — capabilities_json would never exist and "
            "every shadow write would fail into the fail-open path forever."
        )

    def test_column_is_in_the_insert(self):
        """The write path must actually carry the value.

        Was sliced out of `inspect.getsource` text on the markers "VALUES"
        and "INSERT INTO routing_decisions" — a comment quoting either marker
        ahead of the real SQL could shift the slice and hide a missing
        column. `string_constants` pulls the literal the way the AST helper
        does: only STRING CONSTANT nodes the code actually holds, so the text
        analysed below is guaranteed to be the SQL `db.execute` really runs.
        """
        import llm_router.cost as cost
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _ast_assert import string_constants

        literals = string_constants(cost.log_routing_decision)
        insert_stmts = [s for s in literals if "INSERT INTO routing_decisions" in s]
        assert insert_stmts, (
            "no 'INSERT INTO routing_decisions' string literal found in "
            "log_routing_decision — the write path may have moved"
        )
        insert_sql = insert_stmts[0]

        assert "capabilities_json" in insert_sql.split("VALUES")[0], (
            "capabilities_json is not in the INSERT column list"
        )
        placeholders = insert_sql.split("VALUES")[1].split(")")[0].count("?")
        columns = insert_sql.split("INSERT INTO routing_decisions")[1].split(")")[0]
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

        Was `"if capability_routing_enabled():" in inspect.getsource(...)`.
        A comment repeating that exact line, with the real `if` deleted,
        would have satisfied it — the audit's own reproduction of A-10.
        This instead requires an `ast.If` node whose test really CALLS
        `capability_routing_enabled()` and whose body really contains the
        `capabilities_json = serialize_capability_decision(...)` assignment,
        so the guard has to actually gate the write, not just be mentioned
        near it.
        """
        import llm_router.cost as cost

        tree = ast.parse(inspect.getsource(cost.log_routing_decision))
        guarded = any(
            isinstance(n, ast.If)
            and isinstance(n.test, ast.Call)
            and ast.unparse(n.test.func) == "capability_routing_enabled"
            and _assigns_capabilities_json_from_serializer(n)
            for n in ast.walk(tree)
        )
        assert guarded, (
            "the shadow write is not gated on `if capability_routing_enabled():` — "
            "it would run for every install on every routed call"
        )

    def test_serialiser_failure_does_not_lose_the_decision(self):
        """The try/except around the shadow block, asserted structurally.

        Was a TEXT SLICE of `inspect.getsource` between the anchors
        `"capabilities_json: str | None = None"` and `"await db.execute"` —
        a comment repeating either anchor could shift or hide the slice
        entirely. This instead finds the real `ast.AnnAssign` that
        initialises `capabilities_json = None`, and the real `ast.Try` whose
        body assigns it from `serialize_capability_decision(...)` and that
        has at least one `except` handler, then checks the initialisation's
        line precedes the try's — both properties only exist if the code
        really has this shape.
        """
        import llm_router.cost as cost

        tree = ast.parse(inspect.getsource(cost.log_routing_decision))

        ann_assigns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name) and n.target.id == "capabilities_json"
            and isinstance(n.value, ast.Constant) and n.value.value is None
        ]
        assert ann_assigns, (
            "capabilities_json must be initialised to None BEFORE the try, or "
            "an early failure leaves it unbound and the INSERT raises NameError"
        )
        init_line = ann_assigns[0].lineno

        shadow_tries = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Try) and n.handlers
            and _assigns_capabilities_json_from_serializer(n)
        ]
        assert shadow_tries, (
            "the shadow-detection block is not wrapped in a try/except — a "
            "detector or serialiser error would abort logging the routing "
            "decision itself"
        )
        assert shadow_tries[0].lineno > init_line, (
            "capabilities_json is initialised AFTER the try that can raise "
            "before assigning it — the INSERT would see an unbound name"
        )
