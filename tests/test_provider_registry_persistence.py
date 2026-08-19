"""G-006-F3 — SQLite-backed provider registry + cross-instance polling.

Two backends:

* ``db_path=None`` (default) → pure in-memory; pre-G-006-F3 behaviour.
* ``db_path=Path(...)`` → SQLite-backed; mutations write through to
  the table and bump a version counter. Other instances pointing at
  the same file see the change on their next read.

The polling model gives "eventual consistency within one read" — good
enough for emergency-disable where the staleness budget is seconds,
not milliseconds.
"""
from __future__ import annotations

from pathlib import Path


from llm_router.provider_registry import RuntimeProviderRegistry


# ── 1. Backward compat: in-memory mode unchanged ─────────────────────────────


def test_in_memory_mode_still_works() -> None:
    reg = RuntimeProviderRegistry()  # no db_path
    assert reg.is_disabled("openai") is False
    reg.disable("openai", reason="x")
    assert reg.is_disabled("openai") is True
    reg.enable("openai")
    assert reg.is_disabled("openai") is False


def test_in_memory_state_is_lost_on_recreate() -> None:
    """The point of G-006-F3: pure in-memory mode has no persistence.
    Pinning this so a future refactor doesn't silently change it."""
    reg1 = RuntimeProviderRegistry()
    reg1.disable("openai", reason="x")
    reg2 = RuntimeProviderRegistry()
    assert reg2.is_disabled("openai") is False


# ── 2. SQLite persistence ────────────────────────────────────────────────────


def test_sqlite_disable_survives_close_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    reg1 = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    reg1.disable("openai", reason="credential leak")
    reg1.close()

    reg2 = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    assert reg2.is_disabled("openai") is True
    snapshot = reg2.list_disabled()
    assert len(snapshot) == 1
    assert snapshot[0]["provider"] == "openai"
    assert snapshot[0]["reason"] == "credential leak"


def test_sqlite_enable_persists(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    reg1 = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    reg1.disable("openai", reason="x")
    reg1.enable("openai")
    reg1.close()

    reg2 = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    assert reg2.is_disabled("openai") is False
    assert reg2.list_disabled() == []


def test_sqlite_clear_truncates_and_propagates(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    reg = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    reg.disable("a", reason="x")
    reg.disable("b", reason="x")
    reg.clear()
    assert reg.list_disabled() == []
    # And a peer sees the clear.
    peer = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    assert peer.list_disabled() == []


# ── 3. Cross-instance propagation via polling ────────────────────────────────


def test_two_instances_see_each_others_disables(tmp_path: Path) -> None:
    """Two registries pointing at the same SQLite file simulate two
    admin-API instances. Mutations on A propagate to B's read path."""
    db = tmp_path / "registry.db"
    a = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    b = RuntimeProviderRegistry(db_path=db, check_same_thread=False)

    # B starts clean.
    assert b.is_disabled("openai") is False

    # A disables.
    a.disable("openai", reason="leak")

    # B's next read sees the change (the version counter advanced).
    assert b.is_disabled("openai") is True

    # A enables.
    a.enable("openai")

    # B sees the un-disable too.
    assert b.is_disabled("openai") is False


def test_b_observes_a_disable_via_list(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    a = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    b = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    a.disable("openai", reason="leak")
    a.disable("anthropic", reason="quota")

    snap = b.list_disabled()
    providers = {row["provider"] for row in snap}
    assert providers == {"openai", "anthropic"}


def test_repeated_reads_only_refresh_when_version_changes(
    tmp_path: Path, monkeypatch
) -> None:
    """Sanity: when nothing changes, the read path does not bust the
    cache. Detect via _reload_from_db call count."""
    db = tmp_path / "registry.db"
    reg = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    reg.disable("openai", reason="x")

    calls = {"n": 0}
    original = reg._reload_from_db

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    reg._reload_from_db = counting  # type: ignore[assignment]

    # Five reads, no mutations from anywhere → no reloads.
    for _ in range(5):
        reg.is_disabled("openai")
    assert calls["n"] == 0


# ── 4. Idempotent re-disable updates reason but keeps version monotone ──────


def test_idempotent_redisable_updates_reason_and_bumps_version(
    tmp_path: Path,
) -> None:
    db = tmp_path / "registry.db"
    reg = RuntimeProviderRegistry(db_path=db, check_same_thread=False)
    reg.disable("openai", reason="first reason")
    snap1 = reg.list_disabled()
    v1 = reg._cached_version
    reg.disable("openai", reason="updated reason")
    snap2 = reg.list_disabled()
    v2 = reg._cached_version
    assert snap1[0]["reason"] == "first reason"
    assert snap2[0]["reason"] == "updated reason"
    assert v2 > v1


# ── 5. Env-driven persistence path via global accessor ───────────────────────


def test_global_accessor_uses_env_path(
    tmp_path: Path, monkeypatch
) -> None:
    """``LLM_ROUTER_PROVIDER_REGISTRY_PATH`` env routes the singleton
    through the SQLite backend."""
    import llm_router.provider_registry as pr_mod

    monkeypatch.setenv(
        "LLM_ROUTER_PROVIDER_REGISTRY_PATH", str(tmp_path / "g.db")
    )
    monkeypatch.setattr(pr_mod, "_global_registry", None)
    reg = pr_mod.get_global_registry()
    assert reg.db_path == tmp_path / "g.db"
    reg.disable("openai", reason="x")
    reg.close()

    # A second resolve picks up the persisted state.
    monkeypatch.setattr(pr_mod, "_global_registry", None)
    reg2 = pr_mod.get_global_registry()
    assert reg2.is_disabled("openai") is True


def test_global_accessor_without_env_is_in_memory(monkeypatch) -> None:
    """No env → pure-memory singleton."""
    import llm_router.provider_registry as pr_mod

    monkeypatch.delenv("LLM_ROUTER_PROVIDER_REGISTRY_PATH", raising=False)
    monkeypatch.setattr(pr_mod, "_global_registry", None)
    reg = pr_mod.get_global_registry()
    assert reg.db_path is None
