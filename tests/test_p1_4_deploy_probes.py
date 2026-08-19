"""P1-4 — the Helm chart and Docker image declare health probes.

The deployment had no liveness/readiness probes and the image had no
HEALTHCHECK, so Kubernetes / Docker could not tell a wedged process from a
healthy one. The SSE entrypoint has no unauthenticated HTTP /health route, so
the probes are TCP on the listen port.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHART = _REPO / "deploy" / "helm" / "llm_router"
_HELM = shutil.which("helm")


def _render(*extra: str) -> str:
    return subprocess.run(
        [_HELM, "template", str(_CHART), *extra],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.skipif(_HELM is None, reason="helm not installed")
def test_chart_renders_tcp_probes_by_default():
    out = _render()
    assert "livenessProbe:" in out
    assert "readinessProbe:" in out
    assert "tcpSocket:" in out


@pytest.mark.skipif(_HELM is None, reason="helm not installed")
def test_probes_are_toggleable():
    out = _render("--set", "probes.enabled=false")
    assert "livenessProbe:" not in out
    assert "readinessProbe:" not in out


def test_dockerfile_declares_healthcheck():
    text = (_REPO / "Dockerfile").read_text()
    assert "HEALTHCHECK" in text
    assert "17891" in text


def test_values_expose_probe_knobs():
    text = (_CHART / "values.yaml").read_text()
    assert "probes:" in text
    assert "initialDelaySeconds" in text
