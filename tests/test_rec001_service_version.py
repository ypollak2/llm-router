"""REC-001: the service reports the package's real version, not a literal.

service.py hardcoded version="5.3.0" in the FastAPI app and the /health payload
while the package was at 15.x — a health check that lies about what is running.
"""
from __future__ import annotations

import asyncio

import llm_router


def _service():
    # service.py opens its log file under the state dir at import time.
    from llm_router import paths
    paths.state_path("x").parent.mkdir(parents=True, exist_ok=True)
    from llm_router import service
    return service


def test_health_reports_the_package_version():
    body = asyncio.run(_service().health_check())
    assert body["version"] == llm_router.__version__
    assert body["version"] != "5.3.0"


def test_openapi_title_version_matches():
    assert _service().app.version == llm_router.__version__
