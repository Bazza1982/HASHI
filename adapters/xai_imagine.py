from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from adapters.xai_oauth_credentials import (
    DEFAULT_XAI_BASE_URL,
    XaiOAuthCredentialError,
    resolve_xai_credentials,
)

IMAGINE_IMAGE_MODELS = frozenset(
    {
        "grok-imagine-image",
        "grok-imagine-image-quality",
    }
)
IMAGINE_VIDEO_MODELS = frozenset(
    {
        "grok-imagine-video",
        "grok-imagine-video-1.5-preview",
    }
)

DEFAULT_IMAGINE_MODEL = "grok-imagine-image-quality"
DEFAULT_IMAGINE_VIDEO_MODEL = "grok-imagine-video"
_AUTH_RETRY_STATUSES = {401, 403}


@dataclass
class XaiImageResult:
    urls: list[str]
    model: str
    raw: dict[str, Any]


@dataclass
class XaiVideoResult:
    request_id: str
    model: str
    raw: dict[str, Any]


def is_imagine_image_model(model: str | None) -> bool:
    name = str(model or "").strip()
    return name in IMAGINE_IMAGE_MODELS or name.startswith("grok-imagine-image")


def is_imagine_video_model(model: str | None) -> bool:
    name = str(model or "").strip()
    return name in IMAGINE_VIDEO_MODELS or name.startswith("grok-imagine-video")


async def generate_xai_image(
    *,
    prompt: str,
    model: str = DEFAULT_IMAGINE_MODEL,
    bearer_token: str | None = None,
    oauth_refresh_token: str | None = None,
    hermes_home: str | None = None,
    base_url: str = DEFAULT_XAI_BASE_URL,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    n: int = 1,
    response_format: str = "url",
) -> XaiImageResult:
    creds = await asyncio.to_thread(
        resolve_xai_credentials,
        static_api_key=bearer_token,
        oauth_refresh_token=oauth_refresh_token,
        hermes_home=hermes_home,
        base_url=base_url,
    )

    payload: dict[str, Any] = {
        "model": model or DEFAULT_IMAGINE_MODEL,
        "prompt": prompt,
        "n": max(1, int(n or 1)),
        "response_format": response_format,
    }
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if resolution:
        payload["resolution"] = resolution

    url = f"{creds.base_url.rstrip('/')}/images/generations"
    headers = {
        "Authorization": f"Bearer {creds.api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code in _AUTH_RETRY_STATUSES:
            creds = await asyncio.to_thread(
                resolve_xai_credentials,
                static_api_key=bearer_token,
                oauth_refresh_token=oauth_refresh_token,
                hermes_home=hermes_home,
                base_url=base_url,
                force_refresh=True,
            )
            headers["Authorization"] = f"Bearer {creds.api_key}"
            response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    urls: list[str] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        image_url = str(item.get("url") or "").strip()
        if image_url:
            urls.append(image_url)

    if not urls:
        raise XaiOAuthCredentialError("xAI image generation returned no image URLs")

    return XaiImageResult(urls=urls, model=str(data.get("model") or model), raw=data)


async def generate_xai_video(
    *,
    prompt: str,
    model: str = DEFAULT_IMAGINE_VIDEO_MODEL,
    bearer_token: str | None = None,
    oauth_refresh_token: str | None = None,
    hermes_home: str | None = None,
    base_url: str = DEFAULT_XAI_BASE_URL,
    image_url: str | None = None,
) -> XaiVideoResult:
    creds = await asyncio.to_thread(
        resolve_xai_credentials,
        static_api_key=bearer_token,
        oauth_refresh_token=oauth_refresh_token,
        hermes_home=hermes_home,
        base_url=base_url,
    )

    payload: dict[str, Any] = {
        "model": model or DEFAULT_IMAGINE_VIDEO_MODEL,
        "prompt": prompt,
    }
    if image_url:
        payload["image_url"] = image_url

    url = f"{creds.base_url.rstrip('/')}/videos/generations"
    headers = {
        "Authorization": f"Bearer {creds.api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code in _AUTH_RETRY_STATUSES:
            creds = await asyncio.to_thread(
                resolve_xai_credentials,
                static_api_key=bearer_token,
                oauth_refresh_token=oauth_refresh_token,
                hermes_home=hermes_home,
                base_url=base_url,
                force_refresh=True,
            )
            headers["Authorization"] = f"Bearer {creds.api_key}"
            response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    request_id = str(data.get("request_id") or data.get("id") or "").strip()
    if not request_id:
        raise XaiOAuthCredentialError("xAI video generation returned no request id")

    return XaiVideoResult(request_id=request_id, model=str(data.get("model") or model), raw=data)
