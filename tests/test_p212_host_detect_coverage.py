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
    assert not host_detect.detect_hosts(home=tmp_path, which=lambda binary: None,
                                        environ={})[host].present

    config_dir = host_detect._config_dir(host_detect._HOSTS[host][1], tmp_path,
                                         host_detect.sys.platform, {})
    config_dir.mkdir(parents=True)
    (config_dir / "host-own-state.json").write_text("{}")  # a file the host wrote, not us

    info = host_detect.detect_hosts(home=tmp_path, which=lambda binary: None, environ={})[host]
    assert info.present
    assert info.binary is None
    assert info.config_dir == str(config_dir)


@pytest.mark.parametrize("host", sorted(host_detect._ROUTER_FILES))
def test_a_dir_holding_only_router_files_is_not_a_host(host, tmp_path):
    cfg = host_detect._config_dir(host_detect._HOSTS[host][1], tmp_path, "linux", {})
    cfg.mkdir(parents=True)
    for name in host_detect._ROUTER_FILES[host]:
        (cfg / name).write_text("{}")
    assert not host_detect.detect_hosts(home=tmp_path, which=lambda b: None,
                                        platform="linux", environ={})[host].present


def test_windows_config_dirs_follow_appdata(tmp_path):
    appdata = tmp_path / "D" / "Roaming"
    (appdata / "Code" / "User").mkdir(parents=True)
    info = host_detect.detect_hosts(home=tmp_path, which=lambda b: None, platform="win32",
                                    environ={"APPDATA": str(appdata)})["vscode"]
    assert info.config_dir == str(appdata / "Code" / "User")


def test_factory_is_detected_by_its_droid_binary(tmp_path):
    info = host_detect.detect_hosts(home=tmp_path, which=lambda b: "/bin/droid" if b == "droid" else None,
                                    environ={})["factory"]
    assert info.present and info.binary == "/bin/droid"
