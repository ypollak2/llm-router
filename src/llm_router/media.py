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


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=120.0)
    return _client


# ── Image Generation ─────────────────────────────────────────────────────────


async def generate_image_openai(
    prompt: str, model: str = "dall-e-3", size: str = "1024x1024", quality: str = "standard",
) -> LLMResponse:
    """Generate an image using OpenAI's DALL-E API."""
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
    """Generate an image using fal.ai (Flux, SD, etc.)."""
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

    # fal may return a request_id for async jobs
    if "request_id" in data:
        # Poll for result
        request_id = data["request_id"]
        for _ in range(60):  # max 60 polls
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
    """Generate an image using Stability AI."""
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
    """Generate a video using fal.ai (Kling, minimax, etc.)."""
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

    # Video generation is always async — poll for result
    request_id = data.get("request_id", "")
    video_url = ""
    if request_id:
        import asyncio
        for _ in range(90):  # up to 3 minutes
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
    """Generate a video using Runway API."""
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

    # Poll for completion
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
    """Generate speech using OpenAI TTS."""
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
    """Generate speech using ElevenLabs."""
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

# Maps model prefix → generator function
IMAGE_GENERATORS = {
    "openai": generate_image_openai,
    "fal": generate_image_fal,
    "stability": generate_image_stability,
}

VIDEO_GENERATORS = {
    "fal": generate_video_fal,
    "runway": generate_video_runway,
}

AUDIO_GENERATORS = {
    "openai": generate_audio_openai,
    "elevenlabs": generate_audio_elevenlabs,
}
