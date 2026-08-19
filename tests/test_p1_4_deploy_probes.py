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

#: Skip unless BOTH the tool and the chart are present.
#:
#: The condition was `_HELM is None` alone, which covers a machine without
#: helm but not a checkout without the chart. `deploy/` is not carried into
#: redistributions, so there `helm template` ran against a path that does not
#: exist and raised CalledProcessError — reported as a probe-configuration
#: failure, which is not what happened.
#:
#: Stated as one reason so the skip message says which half is missing.
_SKIP_REASON = (
    "helm not installed" if _HELM is None
    else "deploy/helm chart not present in this checkout" if not _CHART.is_dir()
    else None
)


def _render(*extra: str) -> str:
    return subprocess.run(
        [_HELM, "template", str(_CHART), *extra],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
def test_chart_renders_tcp_probes_by_default():
    out = _render()
    assert "livenessProbe:" in out
    assert "readinessProbe:" in out
    assert "tcpSocket:" in out


@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
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
