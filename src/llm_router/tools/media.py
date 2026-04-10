"""Media generation tools — llm_image, llm_video, llm_audio."""

from __future__ import annotations

from mcp.server.fastmcp import Context

from llm_router.router import route_and_call
from llm_router.types import TaskType


async def llm_image(
    prompt: str,
    ctx: Context,
    model: str | None = None,
    size: str = "1024x1024",
    quality: str = "standard",
) -> str:
    """Generate an image — auto-routes to Gemini Imagen, DALL-E, Flux, or Stable Diffusion.

    Args:
        prompt: Description of the image to generate.
        model: Optional model override (e.g. "gemini/imagen-3", "openai/dall-e-3", "fal/flux-pro", "stability/stable-diffusion-3").
        size: Image size (e.g. "1024x1024", "1792x1024").
        quality: Image quality — "standard" or "hd" (DALL-E only).
    """
    resp = await route_and_call(
        TaskType.IMAGE, prompt,
        model_override=model,
        media_params={"size": size, "quality": quality}, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.media_url:
        result += f"\n\nImage URL: {resp.media_url}"
    return result


async def llm_video(
    prompt: str,
    ctx: Context,
    model: str | None = None,
    duration: int = 5,
) -> str:
    """Generate a video — routes to Gemini Veo, Runway, Kling, or other video models.

    Args:
        prompt: Description of the video to generate.
        model: Optional model override (e.g. "gemini/veo-2", "runway/gen3a_turbo", "fal/kling-video").
        duration: Video duration in seconds (default: 5).
    """
    resp = await route_and_call(
        TaskType.VIDEO, prompt,
        model_override=model,
        media_params={"duration": duration}, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.media_url:
        result += f"\n\nVideo URL: {resp.media_url}"
    return result


async def llm_audio(
    text: str,
    ctx: Context,
    model: str | None = None,
    voice: str = "alloy",
) -> str:
    """Generate speech/audio — routes to ElevenLabs or OpenAI TTS.

    Args:
        text: Text to convert to speech.
        model: Optional model override (e.g. "openai/tts-1-hd", "elevenlabs/eleven_multilingual_v2").
        voice: Voice selection (OpenAI: alloy/echo/fable/onyx/nova/shimmer. ElevenLabs: voice ID).
    """
    resp = await route_and_call(
        TaskType.AUDIO, text,
        model_override=model,
        media_params={"voice": voice}, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.media_url:
        result += f"\n\nAudio: {resp.media_url}"
    return result


def register(mcp, should_register=None) -> None:
    """Register media tools with the FastMCP instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_image"):
        mcp.tool()(llm_image)
    if gate("llm_video"):
        mcp.tool()(llm_video)
    if gate("llm_audio"):
        mcp.tool()(llm_audio)
