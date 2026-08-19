"""RED6-04 — no component may bind a public interface without the gate.

`server.py`'s SSE entry point refuses to bind 0.0.0.0 unless an env var is set
explicitly. Three other components that serve real, paid model calls do not:

    gateway.py         FastAPI; a whole-file grep for `Depends(` returns ZERO.
                       Its only protection is a browser CSRF/DNS-rebinding Host
                       check whose own docstring says non-browser clients are
                       unaffected — so by design it lets through exactly the
                       traffic shape any curl, SDK, or hostile local process
                       produces. binds via presets.bind(), ungated.
    route_server.py    console script `llm_router-route`; zero auth checks;
                       `--host 0.0.0.0` accepted with no refusal.
    commands/admin_api.py  documents `--host 0.0.0.0` as a feature.

That is one MISSING ABSTRACTION, not three bugs. The audit reached the same
conclusion: "the gate should be a shared utility, not something each new server
component has to remember to reimplement." Three components forgot; one
remembered. A per-component gate stays two-out-of-four the next time someone adds
a server.

These tests pin the shared utility and its application, so a NEW server component
that forgets it fails here rather than in someone's network.
"""

from __future__ import annotations

import pytest


# ── the shared utility ────────────────────────────────────────────────────────

def test_shared_gate_exists():
    from llm_router.net_bind import allow_public_bind, refuse_public_bind_or_exit

    assert callable(allow_public_bind)
    assert callable(refuse_public_bind_or_exit)


def test_gate_is_closed_by_default(monkeypatch):
    """Absent an explicit opt-in, a public bind must be refused. The default is
    the whole point: an operator who has not thought about it gets localhost."""
    from llm_router import net_bind

    monkeypatch.delenv(net_bind.ALLOW_PUBLIC_ENV, raising=False)
    monkeypatch.delenv("LLM_ROUTER_SSE_ALLOW_PUBLIC", raising=False)
    assert net_bind.allow_public_bind() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON", "True"])
def test_explicit_opt_in_opens_the_gate(monkeypatch, value):
    from llm_router import net_bind

    monkeypatch.setenv(net_bind.ALLOW_PUBLIC_ENV, value)
    assert net_bind.allow_public_bind() is True


@pytest.mark.parametrize("value", ["", "off", "0", "false", "no", "maybe"])
def test_anything_else_keeps_it_closed(monkeypatch, value):
    """A typo must not open a public bind. Fail closed on the unrecognised."""
    from llm_router import net_bind

    monkeypatch.delenv("LLM_ROUTER_SSE_ALLOW_PUBLIC", raising=False)
    monkeypatch.setenv(net_bind.ALLOW_PUBLIC_ENV, value)
    assert net_bind.allow_public_bind() is False


def test_legacy_sse_env_still_honoured(monkeypatch):
    """server.py shipped LLM_ROUTER_SSE_ALLOW_PUBLIC. Someone is relying on it;
    consolidating must not silently revoke their opt-in."""
    from llm_router import net_bind

    monkeypatch.delenv(net_bind.ALLOW_PUBLIC_ENV, raising=False)
    monkeypatch.setenv("LLM_ROUTER_SSE_ALLOW_PUBLIC", "on")
    assert net_bind.allow_public_bind() is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]"])
def test_public_hosts_are_refused(monkeypatch, host):
    from llm_router import net_bind

    monkeypatch.delenv(net_bind.ALLOW_PUBLIC_ENV, raising=False)
    monkeypatch.delenv("LLM_ROUTER_SSE_ALLOW_PUBLIC", raising=False)
    with pytest.raises(SystemExit) as exc:
        net_bind.refuse_public_bind_or_exit(host, component="test")
    assert exc.value.code != 0


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_always_allowed(monkeypatch, host):
    """The gate must not obstruct the safe default, or it gets removed."""
    from llm_router import net_bind

    monkeypatch.delenv(net_bind.ALLOW_PUBLIC_ENV, raising=False)
    net_bind.refuse_public_bind_or_exit(host, component="test")  # must not raise


def test_refusal_names_the_component_and_the_remedy(monkeypatch, capsys):
    """A refusal an operator cannot act on becomes an env var they set blindly."""
    from llm_router import net_bind

    monkeypatch.delenv(net_bind.ALLOW_PUBLIC_ENV, raising=False)
    monkeypatch.delenv("LLM_ROUTER_SSE_ALLOW_PUBLIC", raising=False)
    with pytest.raises(SystemExit):
        net_bind.refuse_public_bind_or_exit("0.0.0.0", component="gateway")
    err = capsys.readouterr().err
    assert "gateway" in err
    assert net_bind.ALLOW_PUBLIC_ENV in err


# ── every serving component must call it ─────────────────────────────────────

@pytest.mark.parametrize(
    "module_path",
    [
        "src/llm_router/gateway.py",
        "src/llm_router/route_server.py",
        "src/llm_router/commands/admin_api.py",
    ],
)
def test_every_serving_component_consults_the_gate(module_path):
    """Source-level, because the alternative is starting three real servers.

    A component that binds a host it did not check is the defect; this asserts
    the call exists at all, and the per-component behaviour tests above cover
    what it does.
    """
    from pathlib import Path

    # Anchored to THIS FILE, not to `llm_router.__file__`.
    #
    # This assertion is about the REPOSITORY's source text, so it must find the
    # repository -- and the installed package is not a reliable route to it. In
    # the ordinary run `llm_router.__file__` is `<repo>/src/llm_router/__init__.py`, so
    # three parents up landed on the repo root and this worked. Under G-D the
    # wheel is installed, `llm_router.__file__` is
    # `.wheelvenv/lib/python3.11/site-packages/llm_router/__init__.py`, and the same
    # three parents give `.wheelvenv/lib/python3.11/` -> FileNotFoundError on
    # `src/llm_router/gateway.py`.
    #
    # G-D caught this, which is precisely what it exists for: a test that reads
    # the source tree while deriving its location from the import path passes
    # in-tree and cannot work against a wheel. The test file itself is always in
    # the repo under either invocation, so it is the stable anchor.
    root = Path(__file__).resolve().parents[2]
    src = (root / module_path).read_text()
    assert "refuse_public_bind_or_exit" in src, (
        f"{module_path} binds a host without consulting the shared gate"
    )


def test_presets_docstring_does_not_demo_an_unauthenticated_public_bind():
    """presets.py's docstring showed a `team-server` example with host 0.0.0.0.

    It is NOT a shipped preset — measured: _DEFAULTS holds only 'local' at
    127.0.0.1 — but the docstring is the only place a user learns the file
    format, so it was teaching the footgun as normal practice.
    """
    from pathlib import Path

    from llm_router import presets

    doc_src = Path(presets.__file__).read_text().split('"""')[1]
    assert "0.0.0.0" not in doc_src, (
        "presets docstring still demonstrates a public bind as an ordinary "
        "configuration"
    )
