"""Regression: CHZ-SEC-06 — SSRF via unvalidated LLM_ROUTER_OLLAMA_URL.

The Ollama base URL reached urlopen with no scheme/host validation, so file://
was accepted and cloud-metadata addresses were attempted. validate_ollama_url
now gates it (also applied as a pydantic field_validator).
"""
import pytest
from llm_router.config import validate_ollama_url, RouterConfig

BLOCKED = [
    "file:///etc/passwd",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/",
    "http://metadata/computeMetadata/v1/",
    "gopher://evil",
    "ftp://x/y",
    "http://0.0.0.0:11434",
    "http://169.254.10.20:80",
]
ALLOWED = [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "https://ollama.internal.corp:443",
]

@pytest.mark.parametrize("url", BLOCKED)
def test_unsafe_urls_rejected(url):
    assert validate_ollama_url(url) == "", f"unsafe URL not rejected: {url}"

@pytest.mark.parametrize("url", ALLOWED)
def test_safe_urls_pass(url):
    assert validate_ollama_url(url) == url

def test_field_validator_blocks_file_scheme():
    assert RouterConfig(ollama_base_url="file:///etc/passwd").ollama_base_url == ""
    assert RouterConfig(ollama_base_url="http://localhost:11434").ollama_base_url == "http://localhost:11434"
