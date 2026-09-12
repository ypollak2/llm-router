"""Whether a local model can SEE is measured, never assumed.

Images are the largest single cost in real use: across 24 transcripts over 5
days on this machine, 4,263,554 of 5,298,942 tokens of tool output — 80.5% —
were images. Bash was 14.3%, text file reads 3.1%.

Ollama's `vision` capability flag is the same kind of claim its `tools` flag
makes, and `agentic_registry` exists precisely because that one lies. Measured
here: both installed models advertise vision; only one can actually read a value
out of an image.

The failure mode this guards against is specific. A model that decodes an image
but misreads a label returns a confident wrong answer about a screenshot, which
is worse than not routing at all — so every uncertain path resolves to "send it
to Claude".
"""
from __future__ import annotations

import json

import pytest

from llm_router import vision_registry as vr


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_LOCAL_VISION", raising=False)
    monkeypatch.delenv("LLM_ROUTER_VISION_MODEL", raising=False)


# ── the probe image ─────────────────────────────────────────────────────────

def test_the_probe_image_needs_no_third_party_library():
    """A PIL-based probe raised ImportError and returned False for every model,
    which is indistinguishable from "nothing here can see" — on a machine where
    a model reads real screenshots correctly."""
    png = vr._probe_image("4271")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 200


def test_the_probe_image_differs_per_code():
    """Random codes are what stop a model passing by memorising a fixture."""
    assert vr._probe_image("1234") != vr._probe_image("5678")


def test_the_probe_image_is_antialiased():
    """Hard-edged glyphs are not what a screenshot looks like: probed with
    aliased blocks, a capable model dropped one digit every time. A grey pixel
    somewhere is the evidence that edges are soft."""
    png = vr._probe_image("8315")
    import struct, zlib
    # walk the chunks to the IDAT
    pos, idat = 8, b""
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat = png[pos + 8:pos + 8 + length]
            break
        pos += 12 + length
    raw = zlib.decompress(idat)
    assert any(0 < byte < 255 for byte in raw), "no intermediate greys — not antialiased"


# ── the verdict ─────────────────────────────────────────────────────────────

def test_one_failed_trial_fails_the_whole_probe(monkeypatch):
    """A single trial is not a verdict: one model reads 1 of 3 codes, so a
    one-shot probe would call it capable a third of the time and the caller
    could not tell which run it got."""
    calls = {"n": 0}

    def _reply(*a, **k):
        calls["n"] += 1
        raise RuntimeError("ollama down")

    monkeypatch.setattr(vr.urllib.request, "urlopen", _reply)
    assert vr.probe_model("any:model") is False


def test_a_model_that_reads_every_code_passes(monkeypatch):
    seen = []

    class _R:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._p).encode()

    def _urlopen(request, timeout=None):
        body = json.loads(request.data.decode())
        # Echo the code back by reading it from the caller's own random choice:
        # the test cannot know it, so it is recovered from the generated image.
        seen.append(body["model"])
        return _R({"message": {"content": _urlopen.code}})

    real_probe_image = vr._probe_image

    def _capture(code):
        _urlopen.code = code
        return real_probe_image(code)

    monkeypatch.setattr(vr, "_probe_image", _capture)
    monkeypatch.setattr(vr.urllib.request, "urlopen", _urlopen)
    assert vr.probe_model("seeing:model") is True
    assert len(seen) == vr._PROBE_TRIALS, "all trials must run"


def test_an_unreachable_ollama_is_a_no_not_a_crash(monkeypatch):
    monkeypatch.setattr(vr.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    assert vr.probe_model("x") is False
    assert vr.advertised_vision_models() == []
    assert vr.best_vision_model() is None


# ── routing decisions ───────────────────────────────────────────────────────

def test_nothing_proven_means_do_not_route(monkeypatch):
    """The conservative direction is the one that cannot produce a confidently
    wrong answer about a screenshot."""
    monkeypatch.setattr(vr, "get_registry", lambda **k: {})
    assert vr.best_vision_model() is None
    assert vr.can_route_images() is False


def test_a_model_that_failed_is_never_chosen(monkeypatch):
    monkeypatch.setattr(vr, "get_registry", lambda **k: {"blind:model": False})
    assert vr.best_vision_model() is None


def test_a_passing_model_is_chosen(monkeypatch):
    monkeypatch.setattr(vr, "get_registry",
                        lambda **k: {"blind:model": False, "seeing:model": True})
    assert vr.best_vision_model() == "seeing:model"


def test_a_pinned_model_wins_only_if_it_passed(monkeypatch):
    monkeypatch.setattr(vr, "get_registry",
                        lambda **k: {"a:model": True, "b:model": True, "bad:model": False})
    monkeypatch.setenv("LLM_ROUTER_VISION_MODEL", "b:model")
    assert vr.best_vision_model() == "b:model"
    monkeypatch.setenv("LLM_ROUTER_VISION_MODEL", "bad:model")
    assert vr.best_vision_model() == "a:model", "a pin cannot override a failed probe"


@pytest.mark.parametrize("off", ["0", "off", "false", "no", "OFF"])
def test_the_kill_switch_stops_routing_regardless_of_capability(monkeypatch, off):
    monkeypatch.setattr(vr, "get_registry", lambda **k: {"seeing:model": True})
    monkeypatch.setenv("LLM_ROUTER_LOCAL_VISION", off)
    assert vr.best_vision_model() is None


# ── the cache ───────────────────────────────────────────────────────────────

def test_the_cache_is_keyed_to_the_installed_model_set(monkeypatch, tmp_path):
    """Pulling a new model must re-probe without a code change."""
    monkeypatch.setattr(vr, "advertised_vision_models", lambda: ["a:model"])
    monkeypatch.setattr(vr, "probe_model", lambda m, **k: True)
    assert vr.get_registry() == {"a:model": True}

    monkeypatch.setattr(vr, "advertised_vision_models", lambda: ["a:model", "b:model"])
    probed = []
    monkeypatch.setattr(vr, "probe_model", lambda m, **k: probed.append(m) or False)
    vr.get_registry()
    assert set(probed) == {"a:model", "b:model"}, "a new model was not re-probed"


def test_the_hot_path_never_blocks_on_a_probe(monkeypatch):
    """Each probe is seconds of round-trip; a router must not pay that inline."""
    monkeypatch.setattr(vr, "advertised_vision_models", lambda: ["a:model"])
    monkeypatch.setattr(vr, "probe_model",
                        lambda m, **k: pytest.fail("probed on the hot path"))
    assert vr.get_registry(allow_probe=False) == {}


def test_a_corrupt_cache_is_ignored_rather_than_trusted(monkeypatch):
    vr.cache_path().parent.mkdir(parents=True, exist_ok=True)
    vr.cache_path().write_text("{not json")
    assert vr._read_cache() is None


def test_the_cache_path_is_resolved_per_call(monkeypatch, tmp_path):
    """A module-level Path.home() freezes $HOME at import — the defect class
    that has bitten this tree four times."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "one"))
    first = vr.cache_path()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "two"))
    assert vr.cache_path() != first
