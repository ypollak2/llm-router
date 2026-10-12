"""Behavioral coverage of snippet hosts and directory-only detection."""

from pathlib import Path

import pytest

from llm_router import host_detect
from llm_router.commands import install


def _missing_hosts(snippets):
    missing = [
        host for host in snippets
        if host not in host_detect._HOSTS and not host_detect.UNDETECTABLE.get(host)
    ]
    return missing


def test_every_snippet_host_has_detection_or_a_reason():
    assert _missing_hosts(install._HOST_SNIPPETS) == []
    assert all(host_detect.UNDETECTABLE.values())


def test_coverage_check_finds_an_injected_host(monkeypatch):
    monkeypatch.setattr(
        install, "_HOST_SNIPPETS",
        {**install._HOST_SNIPPETS, "injected-host": "fake snippet"},
    )
    assert _missing_hosts(install._HOST_SNIPPETS) == ["injected-host"]


@pytest.mark.parametrize(
    "host",
    [
        "opencode",
        "copilot-cli",
        "openclaw",
        "trae",
        "factory",
        "vscode",
        "cursor",
        "windsurf",
    ],
)
def test_new_host_detected_from_config_directory(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert not host_detect.detect_hosts(home=tmp_path, which=lambda binary: None)[host].present

    config_dir = tmp_path / host_detect._HOSTS[host][1]
    config_dir.mkdir(parents=True)

    info = host_detect.detect_hosts(home=tmp_path, which=lambda binary: None)[host]
    assert info.present
    assert info.binary is None
    assert info.config_dir == str(config_dir)
