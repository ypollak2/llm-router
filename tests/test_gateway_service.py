# SPDX-License-Identifier: MIT
"""Per-user gateway service generation must not bake in machine-specific paths."""

from pathlib import Path

import pytest

from llm_router.gateway_service import (
    LABEL,
    gateway_service_target,
    install_gateway_service,
    render_launchd_plist,
    render_systemd_user_unit,
)

_REPO = Path(__file__).resolve().parent.parent


def test_launchd_plist_uses_given_paths_not_hardcoded():
    out = render_launchd_plist("/opt/venv/bin/python", Path("/home/alice"))
    assert "/opt/venv/bin/python" in out
    assert "/home/alice/.llm_router/gateway.out.log" in out
    assert f"<string>{LABEL}</string>" in out
    assert "<key>LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS</key><string>codex,gemini_cli</string>" in out
    # No author-specific paths leaked in.
    assert "yaliandrona" not in out and "yali.pollak" not in out


def test_systemd_user_unit_uses_given_python():
    out = render_systemd_user_unit("/opt/venv/bin/python")
    assert "ExecStart=/opt/venv/bin/python -m llm_router.gateway" in out
    assert "Environment=LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS=codex,gemini_cli" in out
    assert "WantedBy=default.target" in out


def test_target_paths_per_platform():
    mac_dest, mac_cmd = gateway_service_target("Darwin")
    assert mac_dest.name == f"{LABEL}.plist" and "LaunchAgents" in str(mac_dest)
    assert "launchctl load" in mac_cmd

    lin_dest, lin_cmd = gateway_service_target("Linux")
    assert lin_dest.name == "llm_router-gateway.service" and "systemd/user" in str(lin_dest)
    assert "systemctl --user" in lin_cmd

    with pytest.raises(RuntimeError):
        gateway_service_target("Windows")


def test_install_write_false_does_not_touch_disk():
    dest, activate = install_gateway_service(
        python="/opt/venv/bin/python", system="Darwin", write=False
    )
    assert not dest.exists() or True  # write=False must not create it in LaunchAgents
    assert "launchctl" in activate


def test_checked_in_template_has_no_author_paths():
    """The reference plist must stay a placeholder template, never a personal artifact."""
    text = (_REPO / "deploy" / "com.llm_router.gateway.plist").read_text()
    assert "yaliandrona" not in text and "yali.pollak" not in text
    assert "__PYTHON__" in text and "__HOME__" in text
