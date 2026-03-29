"""Tests for Gemini media generators (Imagen 3, Veo 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.media import generate_image_gemini, generate_video_gemini


def _mock_config():
    """Return a mock config with gemini_api_key set."""
    cfg = MagicMock()
    cfg.gemini_api_key = "test-api-key"
    return cfg


def _mock_response(json_data: dict) -> MagicMock:
    """Mock httpx response — .json() is sync, .raise_for_status() is sync."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_data
    return resp


# ── Imagen 3 ─────────────────────────────────────────────────────────────────


class TestGenerateImageGemini:
    @pytest.mark.asyncio
    async def test_returns_image_data_uri(self):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(
            {"predictions": [{"bytesBase64Encoded": "AAAA"}]}
        )

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
        ):
            result = await generate_image_gemini("a cat in space")

        assert result.provider == "gemini"
        assert result.media_url == "data:image/png;base64,AAAA"
        assert result.cost_usd == 0.04
        assert "imagen-3" in result.model

    @pytest.mark.asyncio
    async def test_fast_model_cheaper(self):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(
            {"predictions": [{"bytesBase64Encoded": "BB"}]}
        )

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
        ):
            result = await generate_image_gemini("sunset", model="imagen-3-fast")

        assert result.cost_usd == 0.02

    @pytest.mark.asyncio
    async def test_empty_predictions_returns_empty_url(self):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({"predictions": []})

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
        ):
            result = await generate_image_gemini("nothing")

        assert result.media_url == ""

    @pytest.mark.asyncio
    async def test_aspect_ratio_mapping(self):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(
            {"predictions": [{"bytesBase64Encoded": "X"}]}
        )

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
        ):
            await generate_image_gemini("test", size="1792x1024")

        call_json = mock_client.post.call_args.kwargs["json"]
        assert call_json["parameters"]["aspectRatio"] == "16:9"


# ── Veo 2 ────────────────────────────────────────────────────────────────────


class TestGenerateVideoGemini:
    @pytest.mark.asyncio
    async def test_returns_video_data_uri(self):
        submit_resp = _mock_response({"name": "operations/op-123"})
        poll_resp = _mock_response({
            "done": True,
            "response": {"predictions": [{"bytesBase64Encoded": "VIDEODATA"}]},
        })

        mock_client = AsyncMock()
        mock_client.post.return_value = submit_resp
        mock_client.get.return_value = poll_resp

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await generate_video_gemini("a dancing cat")

        assert result.provider == "gemini"
        assert result.media_url == "data:video/mp4;base64,VIDEODATA"
        assert result.cost_usd == pytest.approx(1.75)
        assert "veo-2" in result.model

    @pytest.mark.asyncio
    async def test_custom_duration_affects_cost(self):
        submit_resp = _mock_response({"name": "operations/op-456"})
        poll_resp = _mock_response({
            "done": True,
            "response": {"predictions": [{"bytesBase64Encoded": "V"}]},
        })

        mock_client = AsyncMock()
        mock_client.post.return_value = submit_resp
        mock_client.get.return_value = poll_resp

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await generate_video_gemini("test", duration=10)

        assert result.cost_usd == pytest.approx(3.50)

    @pytest.mark.asyncio
    async def test_no_operation_name_returns_empty_url(self):
        submit_resp = _mock_response({})  # no "name" key

        mock_client = AsyncMock()
        mock_client.post.return_value = submit_resp

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
        ):
            result = await generate_video_gemini("test")

        assert result.media_url == ""
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_polls_until_done(self):
        submit_resp = _mock_response({"name": "operations/op-789"})
        not_done = _mock_response({"done": False})
        done = _mock_response({
            "done": True,
            "response": {"predictions": [{"bytesBase64Encoded": "FINAL"}]},
        })

        mock_client = AsyncMock()
        mock_client.post.return_value = submit_resp
        mock_client.get.side_effect = [not_done, not_done, done]

        with (
            patch("llm_router.media.get_config", return_value=_mock_config()),
            patch("llm_router.media._get_client", return_value=mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await generate_video_gemini("test")

        assert result.media_url == "data:video/mp4;base64,FINAL"
        assert mock_client.get.call_count == 3
