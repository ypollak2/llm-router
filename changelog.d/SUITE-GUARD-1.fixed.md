The test suite no longer loads real models on the operator's live Ollama. A test-only
`sitecustomize.py` (injected via PYTHONPATH) blocks socket connections to ports 11434
and 11435 in every child Python process, and the in-process guard in `conftest.py` was
extended to also block 11435. Refused connections are logged to a refusal file so that
swallowed exceptions still leave evidence. Tests that genuinely need the daemon are
marked `requires_ollama` (already deselected by default).
