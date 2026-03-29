"""Media generation providers — image, video, audio via direct API calls.

LiteLLM handles text LLMs. For media generation (images, video, audio),
we use direct httpx calls to each provider's REST API.
"""

from __future__ import annotations

import base64
import time

import httpx

from llm_router.config import get_config
from llm_router.types import LLMResponse

_client: httpx.AsyncClient | None = None
"""Module-level singleton for the shared httpx async client.

Lazily initialized on first use via ``_get_client()``.  A single client
is reused across all media requests to leverage HTTP/2 connection pooling
and avoid per-request TLS handshake overhead.
"""


def _get_client() -> httpx.AsyncClient:
    """Return the shared async HTTP client, creating it on first call.

    The client uses a 120-second timeout because media generation APIs
    (especially video) can take well over a minute to return even a
    queue-submission response.

    Returns:
        The module-level singleton ``httpx.AsyncClient``.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=120.0)
    return _client


# ── Image Generation ─────────────────────────────────────────────────────────


async def generate_image_openai(
    prompt: str, model: str = "dall-e-3", size: str = "1024x1024", quality: str = "standard",
) -> LLMResponse:
    """Generate an image via the OpenAI DALL-E REST API.

    Calls ``POST /v1/images/generations``.  DALL-E 3 may revise the prompt
    for safety/quality; the revised prompt is included in the response content.

    Pricing (per image):
        - dall-e-3 standard: $0.040
        - dall-e-3 hd:       $0.080
        - dall-e-2:          $0.020

    Args:
        prompt: Text description of the desired image.
        model: DALL-E model variant (``"dall-e-3"`` or ``"dall-e-2"``).
        size: Output dimensions (e.g. ``"1024x1024"``, ``"1792x1024"``).
        quality: ``"standard"`` or ``"hd"`` (DALL-E 3 only; hd doubles cost).

    Returns:
        An ``LLMResponse`` with ``media_url`` set to the generated image URL.
        ``input_tokens`` and ``output_tokens`` are 0 (image APIs are per-image).
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    resp = await client.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        json={"model": model, "prompt": prompt, "n": 1, "size": size, "quality": quality},
    )
    resp.raise_for_status()
    data = resp.json()
    elapsed = (time.monotonic() - start) * 1000

    image_url = data["data"][0].get("url", "")
    revised_prompt = data["data"][0].get("revised_prompt", prompt)

    # DALL-E 3 pricing
    cost = {"dall-e-3": 0.040, "dall-e-2": 0.020}.get(model, 0.040)
    if quality == "hd":
        cost = 0.080

    return LLMResponse(
        content=f"Image generated: {revised_prompt}",
        model=f"openai/{model}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="openai",
        media_url=image_url,
    )


async def generate_image_fal(
    prompt: str, model: str = "flux-pro", size: str = "landscape_16_9",
) -> LLMResponse:
    """Generate an image via the fal.ai queue API (Flux, Stable Diffusion, etc.).

    Submits a generation request to fal's queue endpoint.  If the response
    contains a ``request_id`` (async job), polls ``/status`` every 2 seconds
    for up to 60 iterations (2 minutes) until the job completes.

    Pricing (per image, approximate):
        - flux-pro:     $0.050
        - flux-dev:     $0.025
        - flux-schnell: $0.003

    Args:
        prompt: Text description of the desired image.
        model: fal model name — ``"flux-pro"``, ``"flux-dev"``, or
            ``"flux-schnell"``.  Other names are passed through as
            ``fal-ai/{model}``.
        size: fal image size preset (e.g. ``"landscape_16_9"``,
            ``"square_hd"``).

    Returns:
        An ``LLMResponse`` with ``media_url`` set to the hosted image URL.
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    model_endpoints = {
        "flux-pro": "fal-ai/flux-pro/v1.1",
        "flux-dev": "fal-ai/flux/dev",
        "flux-schnell": "fal-ai/flux/schnell",
    }
    endpoint = model_endpoints.get(model, f"fal-ai/{model}")

    resp = await client.post(
        f"https://queue.fal.run/{endpoint}",
        headers={"Authorization": f"Key {config.fal_key}"},
        json={"prompt": prompt, "image_size": size, "num_images": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    elapsed = (time.monotonic() - start) * 1000

    # fal uses an async queue model: the initial POST returns a request_id,
    # and we must poll /status until completion.  2-second intervals balance
    # responsiveness against rate limits on the status endpoint.
    if "request_id" in data:
        request_id = data["request_id"]
        for _ in range(60):  # max 60 polls (~2 min ceiling)
            import asyncio
            await asyncio.sleep(2)
            status_resp = await client.get(
                f"https://queue.fal.run/{endpoint}/requests/{request_id}/status",
                headers={"Authorization": f"Key {config.fal_key}"},
            )
            status = status_resp.json()
            if status.get("status") == "COMPLETED":
                result_resp = await client.get(
                    f"https://queue.fal.run/{endpoint}/requests/{request_id}",
                    headers={"Authorization": f"Key {config.fal_key}"},
                )
                data = result_resp.json()
                break
        elapsed = (time.monotonic() - start) * 1000

    image_url = ""
    if "images" in data and data["images"]:
        image_url = data["images"][0].get("url", "")

    cost = {"flux-pro": 0.05, "flux-dev": 0.025, "flux-schnell": 0.003}.get(model, 0.03)

    return LLMResponse(
        content=f"Image generated with {model}",
        model=f"fal/{model}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="fal",
        media_url=image_url,
    )


async def generate_image_stability(
    prompt: str, model: str = "stable-diffusion-3", size: str = "1024x1024",
) -> LLMResponse:
    """Generate an image via the Stability AI REST API (Stable Diffusion 3).

    Calls the ``/v2beta/stable-image/generate/sd3`` endpoint.  Unlike
    OpenAI/fal, Stability returns the image as a base64-encoded PNG in
    the response body (no hosted URL).  The ``media_url`` field contains
    a truncated data-URI for preview; the full base64 payload is available
    in the raw response.

    Pricing: ~$0.03 per image for SD3.

    Args:
        prompt: Text description of the desired image.
        model: Model name (currently only ``"stable-diffusion-3"``).
        size: Output dimensions as ``"WIDTHxHEIGHT"`` (e.g. ``"1024x1024"``).

    Returns:
        An ``LLMResponse`` with a truncated base64 data-URI in ``media_url``.
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    width, height = (int(x) for x in size.split("x"))

    resp = await client.post(
        "https://api.stability.ai/v2beta/stable-image/generate/sd3",
        headers={
            "Authorization": f"Bearer {config.stability_api_key}",
            "Accept": "application/json",
        },
        data={"prompt": prompt, "output_format": "png", "width": width, "height": height},
    )
    resp.raise_for_status()
    data = resp.json()
    elapsed = (time.monotonic() - start) * 1000

    # Stability returns base64 image
    image_b64 = data.get("image", "")
    cost = 0.03  # ~$0.03 per image for SD3

    return LLMResponse(
        content=f"Image generated with Stability {model}",
        model=f"stability/{model}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="stability",
        media_url=f"data:image/png;base64,{image_b64[:50]}..." if image_b64 else "",
    )


# ── Video Generation ─────────────────────────────────────────────────────────


async def generate_video_fal(
    prompt: str, model: str = "kling-video", duration: int = 5,
) -> LLMResponse:
    """Generate a video via the fal.ai queue API (Kling, minimax, etc.).

    Video generation is always asynchronous.  After submitting the job,
    polls ``/status`` every 2 seconds for up to 90 iterations (3 minutes)
    to accommodate the longer render times of video models.

    Pricing (per video, approximate):
        - kling-video:   $0.30
        - minimax-video: $0.20

    Args:
        prompt: Text description of the desired video.
        model: fal video model — ``"kling-video"`` or ``"minimax-video"``.
        duration: Desired video length in seconds (default 5).

    Returns:
        An ``LLMResponse`` with ``media_url`` set to the hosted video URL.
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    model_endpoints = {
        "kling-video": "fal-ai/kling-video/v2/master",
        "minimax-video": "fal-ai/minimax-video/video-01-live",
    }
    endpoint = model_endpoints.get(model, f"fal-ai/{model}")

    resp = await client.post(
        f"https://queue.fal.run/{endpoint}",
        headers={"Authorization": f"Key {config.fal_key}"},
        json={"prompt": prompt, "duration": str(duration)},
    )
    resp.raise_for_status()
    data = resp.json()

    # Video generation is always async — poll for result.
    # 2-second intervals keep us under fal's rate limits while still
    # detecting completion promptly.  90 iterations = ~3 min ceiling.
    request_id = data.get("request_id", "")
    video_url = ""
    if request_id:
        import asyncio
        for _ in range(90):
            await asyncio.sleep(2)
            status_resp = await client.get(
                f"https://queue.fal.run/{endpoint}/requests/{request_id}/status",
                headers={"Authorization": f"Key {config.fal_key}"},
            )
            status = status_resp.json()
            if status.get("status") == "COMPLETED":
                result_resp = await client.get(
                    f"https://queue.fal.run/{endpoint}/requests/{request_id}",
                    headers={"Authorization": f"Key {config.fal_key}"},
                )
                result = result_resp.json()
                video_url = result.get("video", {}).get("url", "")
                break
    elif "video" in data:
        video_url = data["video"].get("url", "")

    elapsed = (time.monotonic() - start) * 1000
    cost = {"kling-video": 0.30, "minimax-video": 0.20}.get(model, 0.25)

    return LLMResponse(
        content=f"Video generated with {model} ({duration}s)",
        model=f"fal/{model}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="fal",
        media_url=video_url,
    )


async def generate_video_runway(
    prompt: str, model: str = "gen3a_turbo", duration: int = 5,
) -> LLMResponse:
    """Generate a video via the Runway ML REST API (Gen-3 Alpha).

    Submits a generation task to ``/v1/image_to_video``, then polls
    ``/v1/tasks/{id}`` every 2 seconds for up to 90 iterations (3 min).

    Pricing (per video, approximate):
        - gen3a_turbo: $0.25
        - gen3a:       $0.50

    Args:
        prompt: Text description of the desired video.
        model: Runway model variant — ``"gen3a_turbo"`` (fast) or ``"gen3a"``
            (higher quality).
        duration: Desired video length in seconds (default 5).

    Returns:
        An ``LLMResponse`` with ``media_url`` set to the hosted video URL.
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    resp = await client.post(
        "https://api.dev.runwayml.com/v1/image_to_video",
        headers={
            "Authorization": f"Bearer {config.runway_api_key}",
            "X-Runway-Version": "2024-11-06",
        },
        json={"model": model, "promptText": prompt, "duration": duration},
    )
    resp.raise_for_status()
    data = resp.json()
    task_id = data.get("id", "")

    # Poll for completion — same 2s/90-iteration pattern as fal
    import asyncio
    video_url = ""
    for _ in range(90):
        await asyncio.sleep(2)
        status_resp = await client.get(
            f"https://api.dev.runwayml.com/v1/tasks/{task_id}",
            headers={
                "Authorization": f"Bearer {config.runway_api_key}",
                "X-Runway-Version": "2024-11-06",
            },
        )
        status = status_resp.json()
        if status.get("status") == "SUCCEEDED":
            video_url = status.get("output", [""])[0]
            break

    elapsed = (time.monotonic() - start) * 1000
    cost = {"gen3a_turbo": 0.25, "gen3a": 0.50}.get(model, 0.25)

    return LLMResponse(
        content=f"Video generated with Runway {model} ({duration}s)",
        model=f"runway/{model}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="runway",
        media_url=video_url,
    )


# ── Audio Generation ─────────────────────────────────────────────────────────


async def generate_audio_openai(
    text: str, model: str = "tts-1", voice: str = "alloy",
) -> LLMResponse:
    """Generate speech audio via the OpenAI TTS REST API.

    Calls ``POST /v1/audio/speech``.  The response body is raw MP3 audio
    bytes, which are base64-encoded and returned as a truncated data-URI
    in ``media_url``.

    Pricing (per-character, billed per 1M characters):
        - tts-1:    $15.00 / 1M chars
        - tts-1-hd: $30.00 / 1M chars

    Args:
        text: The text to synthesize into speech.
        model: TTS model — ``"tts-1"`` (fast) or ``"tts-1-hd"`` (higher
            quality).
        voice: Voice preset — ``"alloy"``, ``"echo"``, ``"fable"``,
            ``"onyx"``, ``"nova"``, or ``"shimmer"``.

    Returns:
        An ``LLMResponse`` with ``input_tokens`` set to the character count
        (used for cost calculation) and ``media_url`` containing a truncated
        base64 data-URI of the MP3 audio.
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    resp = await client.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        json={"model": model, "input": text, "voice": voice},
    )
    resp.raise_for_status()
    elapsed = (time.monotonic() - start) * 1000

    # Response is raw audio bytes
    audio_b64 = base64.b64encode(resp.content).decode()
    chars = len(text)
    cost = (chars / 1_000_000) * {"tts-1": 15.0, "tts-1-hd": 30.0}.get(model, 15.0)

    return LLMResponse(
        content=f"Audio generated: {len(resp.content)} bytes, {chars} characters",
        model=f"openai/{model}",
        input_tokens=chars,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="openai",
        media_url=f"data:audio/mp3;base64,{audio_b64[:50]}...",
    )


async def generate_audio_elevenlabs(
    text: str, voice_id: str = "21m00Tcm4TlvDq8ikWAM", model: str = "eleven_multilingual_v2",
) -> LLMResponse:
    """Generate speech audio via the ElevenLabs text-to-speech API.

    Calls ``POST /v1/text-to-speech/{voice_id}``.  Returns raw MP3 audio
    bytes, base64-encoded into a truncated data-URI.

    Pricing: ~$0.30 per 1,000 characters.

    Args:
        text: The text to synthesize into speech.
        voice_id: ElevenLabs voice identifier. The default
            (``"21m00Tcm4TlvDq8ikWAM"``) is the "Rachel" preset voice.
        model: ElevenLabs model — ``"eleven_multilingual_v2"`` supports
            29 languages with voice cloning.

    Returns:
        An ``LLMResponse`` with ``input_tokens`` set to the character count
        and ``media_url`` containing a truncated base64 data-URI.
    """
    config = get_config()
    client = _get_client()
    start = time.monotonic()

    resp = await client.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": config.elevenlabs_api_key,
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
    )
    resp.raise_for_status()
    elapsed = (time.monotonic() - start) * 1000

    audio_b64 = base64.b64encode(resp.content).decode()
    chars = len(text)
    cost = (chars / 1000) * 0.30  # ~$0.30 per 1K characters

    return LLMResponse(
        content=f"Audio generated with ElevenLabs: {len(resp.content)} bytes",
        model=f"elevenlabs/{model}",
        input_tokens=chars,
        output_tokens=0,
        cost_usd=cost,
        latency_ms=elapsed,
        provider="elevenlabs",
        media_url=f"data:audio/mp3;base64,{audio_b64[:50]}...",
    )


# ── Dispatcher ───────────────────────────────────────────────────────────────
#
# These dictionaries map a provider prefix (e.g. "openai", "fal") to the
# corresponding async generator function.  The router uses these to dispatch
# media generation requests: it splits a model string like "fal/flux-pro"
# on the "/" separator, looks up the prefix in the appropriate dict, and
# calls the matched function.  This keeps the routing logic decoupled from
# individual provider implementations.

IMAGE_GENERATORS = {
    "openai": generate_image_openai,
    "fal": generate_image_fal,
    "stability": generate_image_stability,
}
"""Provider-prefix to image generation function mapping."""

VIDEO_GENERATORS = {
    "fal": generate_video_fal,
    "runway": generate_video_runway,
}
"""Provider-prefix to video generation function mapping."""

AUDIO_GENERATORS = {
    "openai": generate_audio_openai,
    "elevenlabs": generate_audio_elevenlabs,
}
"""Provider-prefix to audio generation function mapping."""
