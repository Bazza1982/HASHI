from unittest.mock import AsyncMock, patch

import pytest

from adapters.xai_imagine import (
    XaiImageResult,
    XaiVideoResult,
    generate_xai_image,
    generate_xai_video,
    is_imagine_image_model,
    is_imagine_video_model,
)
from adapters.xai_oauth_credentials import XaiCredentials


def test_is_imagine_image_model():
    assert is_imagine_image_model("grok-imagine-image-quality") is True
    assert is_imagine_image_model("grok-4.3") is False


def test_is_imagine_video_model():
    assert is_imagine_video_model("grok-imagine-video") is True
    assert is_imagine_video_model("grok-4.3") is False


@pytest.mark.asyncio
async def test_generate_xai_image_parses_urls():
    fake_response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "model": "grok-imagine-image-quality",
                "data": [{"url": "https://example.com/a.png"}],
            },
        },
    )()

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            assert url.endswith("/images/generations")
            assert json["prompt"] == "a blue moon"
            return fake_response

    with patch(
        "adapters.xai_imagine.resolve_xai_credentials",
        return_value=XaiCredentials(
            provider="xai-oauth",
            api_key="token",
            base_url="https://api.x.ai/v1",
            source="test",
        ),
    ), patch("adapters.xai_imagine.httpx.AsyncClient", return_value=_FakeClient()):
        result = await generate_xai_image(prompt="a blue moon")

    assert isinstance(result, XaiImageResult)
    assert result.urls == ["https://example.com/a.png"]


@pytest.mark.asyncio
async def test_generate_xai_image_retries_403_with_force_refresh():
    class _FakeResponse:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

        def json(self):
            return self._body

    ok_body = {
        "model": "grok-imagine-image",
        "data": [{"url": "https://example.com/fresh.png"}],
    }

    class _FakeClient:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            self.calls += 1
            if self.calls == 1:
                assert headers["Authorization"] == "Bearer stale"
                return _FakeResponse(403)
            assert headers["Authorization"] == "Bearer fresh"
            return _FakeResponse(200, ok_body)

    with patch(
        "adapters.xai_imagine.resolve_xai_credentials",
        side_effect=[
            XaiCredentials(
                provider="xai-oauth",
                api_key="stale",
                base_url="https://api.x.ai/v1",
                source="test",
            ),
            XaiCredentials(
                provider="xai-oauth",
                api_key="fresh",
                base_url="https://api.x.ai/v1",
                source="test-refresh",
            ),
        ],
    ) as resolve, patch("adapters.xai_imagine.httpx.AsyncClient", return_value=_FakeClient()):
        result = await generate_xai_image(prompt="a blue moon")

    assert result.urls == ["https://example.com/fresh.png"]
    assert resolve.call_count == 2
    assert resolve.call_args_list[1].kwargs["force_refresh"] is True


@pytest.mark.asyncio
async def test_generate_xai_video_parses_request_id():
    fake_response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "model": "grok-imagine-video",
                "request_id": "vid-123",
            },
        },
    )()

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            assert url.endswith("/videos/generations")
            assert json["prompt"] == "a blue moon rising"
            return fake_response

    with patch(
        "adapters.xai_imagine.resolve_xai_credentials",
        return_value=XaiCredentials(
            provider="xai-oauth",
            api_key="token",
            base_url="https://api.x.ai/v1",
            source="test",
        ),
    ), patch("adapters.xai_imagine.httpx.AsyncClient", return_value=_FakeClient()):
        result = await generate_xai_video(prompt="a blue moon rising")

    assert isinstance(result, XaiVideoResult)
    assert result.request_id == "vid-123"
