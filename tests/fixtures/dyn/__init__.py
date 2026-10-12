"""Fixtures for dynamic-discovery tests (PLAN v16 P1.12, lite scope shipped with P0.15).

Three fixtures, test code only: ``fake_ollama`` (a real loopback HTTP server that also
serves the OpenAI-compatible ``/v1/models``), ``homes`` (fixture state directories) and
``net_guard`` (fails a test that connects to a non-loopback host).
"""
