"""Question-driven local image understanding for text-only HASHI backends."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from tools.media_read import (
    MediaReadError,
    _resolve_media_path,
    _validate_signature,
    normalize_image,
)

_ATTACHMENT_REF_RE = re.compile(
    r"^attachment:(?P<message>[A-Za-z0-9._-]+):(?P<attachment>[A-Za-z0-9._-]+)$"
)
_DETAIL_LEVELS = {"brief", "standard", "detailed"}
_MAX_QUESTION_CHARS = 2_000
_MAX_ITEM_CHARS = 1_500
_MAX_ITEMS = 20


class VisionInspectError(ValueError):
    """A bounded error safe to return to the calling model."""


@dataclass(frozen=True)
class VisionAnswer:
    answer: str
    observations: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()


class VisionProvider(Protocol):
    async def inspect(
        self,
        *,
        image_bytes: bytes,
        question: str,
        detail: str,
    ) -> VisionAnswer: ...


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_endpoint(options: dict[str, Any]) -> str:
    endpoint = str(options.get("endpoint") or "http://127.0.0.1:8081/v1").rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise VisionInspectError("vision endpoint must be an http(s) URL")
    allowed_hosts = {
        str(item).strip().casefold()
        for item in options.get("allowed_hosts", [])
        if str(item).strip()
    }
    if not _is_loopback_host(parsed.hostname) and parsed.hostname.casefold() not in allowed_hosts:
        raise VisionInspectError(
            "vision endpoint must be loopback or explicitly listed in allowed_hosts"
        )
    return endpoint


def _clip_text(value: Any, maximum: int = _MAX_ITEM_CHARS) -> str:
    return str(value or "").strip()[:maximum]


def _string_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        text
        for text in (_clip_text(item) for item in value[:_MAX_ITEMS])
        if text
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("output_text") or "")
    return ""


def _parse_answer(content: Any) -> VisionAnswer:
    text = _content_text(content).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        answer = _clip_text(payload.get("answer"), 6_000)
        observations = _string_items(payload.get("observations"))
        uncertainties = _string_items(payload.get("uncertainties"))
        if answer:
            return VisionAnswer(answer, observations, uncertainties)
    if not text:
        raise VisionInspectError("vision provider returned an empty answer")
    return VisionAnswer(_clip_text(text, 6_000))


class LlamaCppVisionProvider:
    def __init__(self, options: dict[str, Any], *, client: httpx.AsyncClient | None = None):
        self.options = dict(options or {})
        self.endpoint = _validated_endpoint(self.options)
        self.model = str(self.options.get("model") or "qwen3-vl-2b-instruct")
        # HTTPX applies this as per-network-operation inactivity, not as a
        # wall-clock cap on the whole vision task. The old key is read only as
        # a configuration migration alias.
        raw_idle_timeout = self.options.get(
            "idle_timeout_seconds",
            self.options.get("timeout_seconds", 90),
        )
        self.idle_timeout = float(raw_idle_timeout)
        if self.idle_timeout <= 0:
            raise VisionInspectError("idle_timeout_seconds must be positive")
        self._client = client

    async def inspect(
        self,
        *,
        image_bytes: bytes,
        question: str,
        detail: str,
    ) -> VisionAnswer:
        prompt = (
            "Analyze the real visual content of this image. Focus on objects, people, "
            "actions, scene context, and spatial relationships relevant to the question. "
            "Do not merely transcribe visible text and do not guess hidden facts. "
            "Return JSON with keys answer, observations, and uncertainties.\n"
            f"Detail level: {detail}.\nQuestion: {question}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(image_bytes).decode("ascii")
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.idle_timeout)
        try:
            response = await client.post(f"{self.endpoint}/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VisionInspectError(f"llama.cpp request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise VisionInspectError("llama.cpp response contained no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        return _parse_answer(message.get("content") if isinstance(message, dict) else None)


class OpenRouterVisionProvider(LlamaCppVisionProvider):
    """Use an explicitly authorized OpenRouter vision model as the VLM."""

    def __init__(
        self,
        options: dict[str, Any],
        *,
        secrets: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ):
        super().__init__(options, client=client)
        secret_name = str(options.get("api_key_secret") or "openrouter_key").strip()
        self.api_key = str((secrets or {}).get(secret_name) or "").strip()
        if not self.api_key:
            raise VisionInspectError(
                f"OpenRouter vision credential {secret_name!r} is unavailable"
            )

    async def inspect(
        self,
        *,
        image_bytes: bytes,
        question: str,
        detail: str,
    ) -> VisionAnswer:
        prompt = (
            "Analyze the real visual content of this image. Focus on objects, people, "
            "actions, scene context, and spatial relationships relevant to the question. "
            "Do not merely transcribe visible text and do not guess hidden facts. "
            "Return JSON with keys answer, observations, and uncertainties.\n"
            f"Detail level: {detail}.\nQuestion: {question}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(image_bytes).decode("ascii")
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.idle_timeout)
        try:
            response = await client.post(
                f"{self.endpoint}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VisionInspectError(f"OpenRouter vision request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise VisionInspectError("OpenRouter vision response contained no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        return _parse_answer(message.get("content") if isinstance(message, dict) else None)


def _attachment_path(image_ref: str, media_roots: list[Path]) -> Path:
    match = _ATTACHMENT_REF_RE.fullmatch(image_ref)
    if match is None:
        raise VisionInspectError(
            "attachment references must use attachment:<message_id>:<attachment_id>"
        )
    message_id = match.group("message")
    attachment_id = match.group("attachment")
    for raw_root in media_roots:
        root = Path(raw_root).expanduser().resolve()
        candidates = (
            root / "messages" / message_id / "manifest.json",
            root / message_id / "manifest.json",
        )
        for manifest_path in candidates:
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise VisionInspectError("attachment manifest is unreadable") from exc
            for item in manifest.get("attachments", []):
                if str(item.get("attachment_id") or "") != attachment_id:
                    continue
                stored = Path(str(item.get("stored_path") or ""))
                if not stored.is_absolute():
                    stored = manifest_path.parent / str(item.get("filename") or "")
                try:
                    resolved = stored.expanduser().resolve(strict=True)
                    resolved.relative_to(root)
                except (FileNotFoundError, ValueError) as exc:
                    raise VisionInspectError("attachment path is missing or outside its media root") from exc
                expected = str(item.get("sha256") or "").strip().casefold()
                if expected:
                    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
                    if actual != expected:
                        raise VisionInspectError("attachment checksum does not match its manifest")
                return resolved
    raise VisionInspectError("attachment was not found in the authorized media roots")


def _resolve_image(
    image_ref: str,
    *,
    access_root: Path,
    workspace_dir: Path,
    media_roots: list[Path],
) -> Path:
    if image_ref.startswith("attachment:"):
        return _attachment_path(image_ref, media_roots)
    return _resolve_media_path(
        image_ref,
        access_root=access_root,
        workspace_dir=workspace_dir,
        media_roots=media_roots,
    )


async def execute_vision_inspect(
    args: dict[str, Any],
    *,
    access_root: Path,
    workspace_dir: Path,
    media_roots: list[Path],
    options: dict[str, Any],
    secrets: dict[str, Any] | None = None,
    provider: VisionProvider | None = None,
) -> str:
    """Inspect one authorized image and return bounded semantic evidence as JSON."""
    started = time.monotonic()
    try:
        image_ref = str(args.get("image_ref") or "").strip()
        question = str(args.get("question") or "").strip()
        detail = str(args.get("detail") or "standard").strip().casefold()
        if not image_ref:
            raise VisionInspectError("image_ref is required")
        if not question:
            raise VisionInspectError("question is required")
        if len(question) > _MAX_QUESTION_CHARS:
            raise VisionInspectError("question exceeds 2000 characters")
        if detail not in _DETAIL_LEVELS:
            raise VisionInspectError("detail must be brief, standard, or detailed")
        path = _resolve_image(
            image_ref,
            access_root=access_root,
            workspace_dir=workspace_dir,
            media_roots=media_roots,
        )
        kind = _validate_signature(path)
        if kind != "image":
            raise VisionInspectError("vision_inspect accepts image files only")
        normalized = normalize_image(path)
        provider_name = str(options.get("provider") or "llama_cpp").strip().casefold()
        if provider_name not in {"llama_cpp", "llama.cpp", "openrouter"}:
            raise VisionInspectError(f"unsupported vision provider: {provider_name}")
        if provider is not None:
            selected_provider = provider
        elif provider_name == "openrouter":
            selected_provider = OpenRouterVisionProvider(
                options,
                secrets=dict(secrets or {}),
            )
        else:
            selected_provider = LlamaCppVisionProvider(options)
        answer = await selected_provider.inspect(
            image_bytes=normalized.data,
            question=question,
            detail=detail,
        )
        output = {
            "status": "ok",
            "answer": answer.answer,
            "observations": list(answer.observations),
            "uncertainties": list(answer.uncertainties),
            "model": str(options.get("model") or "qwen3-vl-2b-instruct"),
            "detail": detail,
            "normalized_size": list(normalized.normalized_size),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    except (MediaReadError, VisionInspectError, OSError, ValueError) as exc:
        return f"Error: vision_inspect failed: {exc}"
