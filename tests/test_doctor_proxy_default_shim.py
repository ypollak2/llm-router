"""`llm-router doctor`'s proxy-default section with the fail-open shim.

docs/BUGS.md #11 review: with the shim installed, the sentinel's ``port`` is the
shim's, and the shim accepts even when the main proxy behind it is dead. A probe
of ``port`` alone printed "answering" while every call bypassed routing. The
section must also probe ``upstream_port`` and name the right service to restart.
Real loopback sockets; never the live 8787/8797.
"""

from __future__ import annotations

import socket

import pytest

from llm_router import proxy_default as pd
from llm_router.commands import doctor


def _listening() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


def _dead_port() -> int:
    s = _listening()
    port = s.getsockname()[1]
    s.close()
    return port


def _run(monkeypatch, capsys, sentinel: dict) -> tuple[str, list[str]]:
    monkeypatch.setattr(pd, "read_sentinel", lambda: sentinel)
    issues: list[str] = []
    doctor._proxy_default_section(issues)
    return capsys.readouterr().out, issues


@pytest.fixture
def shim():
    s = _listening()
    yield s.getsockname()[1]
    s.close()


def test_dead_main_proxy_behind_a_live_shim_is_an_issue(monkeypatch, capsys, shim):
    upstream = _dead_port()
    out, issues = _run(monkeypatch, capsys, {"port": shim, "upstream_port": upstream})
    assert f"main proxy NOT answering on 127.0.0.1:{upstream}" in out
    assert "routing is bypassed" in out
    assert f"gui/$(id -u)/{pd.LABEL} " in out
    assert len(issues) == 1 and str(upstream) in issues[0]


def test_shim_and_main_proxy_both_answering_is_clean(monkeypatch, capsys, shim):
    main = _listening()
    try:
        out, issues = _run(monkeypatch, capsys, {"port": shim, "upstream_port": main.getsockname()[1]})
    finally:
        main.close()
    assert "NOT answering" not in out
    assert "behind the fail-open shim" in out
    assert issues == []


def test_dead_shim_names_the_shim_service(monkeypatch, capsys):
    port = _dead_port()
    out, issues = _run(monkeypatch, capsys, {"port": port, "upstream_port": _dead_port()})
    assert f"fail-open shim NOT answering on 127.0.0.1:{port}" in out
    assert f"gui/$(id -u)/{pd.SHIM_LABEL}" in out
    assert len(issues) == 1


def test_no_shim_sentinel_keeps_the_single_probe(monkeypatch, capsys):
    port = _dead_port()
    out, issues = _run(monkeypatch, capsys, {"port": port, "upstream_port": None})
    assert f"NOT answering on 127.0.0.1:{port}" in out
    assert "fail-open shim" not in out
    assert f"gui/$(id -u)/{pd.LABEL} " in out
    assert len(issues) == 1
