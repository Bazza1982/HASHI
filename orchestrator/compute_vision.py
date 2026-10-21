"""HERV3's narrow LAN HASHI Compute vision projection consumer.

The PAO-owned Compute Service Directory chooses an exact configured LAN
service.  This consumer sends raw image bytes only to that resolved origin and
returns bounded derived text; it never changes the Agent's selected model.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from orchestrator.compute_services import (
    ComputeRequirements,
    ComputeServiceClient,
    ComputeServiceDirectory,
    ComputeServiceError,
    ComputeServicesConfig,
)
from orchestrator.multimodal_contract import normalize_request_content
from tools.media_read import MediaReadError, normalize_image


MAX_VISION_QUESTION_CHARS = 4_000
MAX_VISION_ANSWER_CHARS = 6_000


class ComputeVisionError(RuntimeError):
    """Stable failure at the LAN vision projection boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "compute_vision_failed")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class LanVisionProjection:
    text: str
    node_id: str
    service_id: str
    model_id: str


def _response_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ComputeVisionError("compute_vision_response_invalid")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise ComputeVisionError("compute_vision_response_invalid")
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "\n".join(
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ).strip()
    else:
        text = ""
    if not text:
        raise ComputeVisionError("compute_vision_response_empty")
    return text[:MAX_VISION_ANSWER_CHARS]


class LanComputeVisionProjector:
    """Project canonical image parts to text through a discovered LAN VLM."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        client: ComputeServiceClient | Any | None = None,
        directory: ComputeServiceDirectory | Any | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.client = client or ComputeServiceClient()
        self.directory = directory or ComputeServiceDirectory(
            ComputeServicesConfig(self.config_path),
            client=self.client,
        )

    async def _resolve(self):
        try:
            refreshed = await self.directory.refresh()
            matches = refreshed.catalogue.match(
                ComputeRequirements(
                    input_modalities=frozenset({"text", "image"}),
                    output_modalities=frozenset({"text"}),
                    protocols=frozenset({"openai.chat.completions"}),
                    locations=frozenset({"lan"}),
                )
            )
        except asyncio.CancelledError:
            raise
        except ComputeServiceError as exc:
            raise ComputeVisionError(exc.code) from exc
        if not matches:
            first_error = next(iter(refreshed.errors.values()), "compute_vision_unavailable")
            raise ComputeVisionError(str(first_error))
        return matches[0]

    async def project(
        self,
        request_content: Mapping[str, Any] | None,
        *,
        question: str,
    ) -> LanVisionProjection:
        normalized = normalize_request_content(request_content)
        image_parts = tuple(
            part
            for part in (normalized or {}).get("parts", ())
            if part.get("type") == "media" and part.get("modality") == "image"
        )
        if not image_parts:
            raise ComputeVisionError("compute_vision_image_missing")
        resolved = await self._resolve()
        offering = resolved.offering
        answers: list[str] = []
        selected_question = str(question or "Describe the image.").strip()[
            :MAX_VISION_QUESTION_CHARS
        ]
        for index, part in enumerate(image_parts, start=1):
            try:
                normalized_image = normalize_image(Path(str(part.get("local_ref") or "")))
            except (OSError, MediaReadError) as exc:
                raise ComputeVisionError("compute_vision_image_invalid") from exc
            if (
                offering.image.max_bytes is not None
                and len(normalized_image.data) > offering.image.max_bytes
            ):
                raise ComputeVisionError("compute_vision_image_too_large")
            payload = {
                "model": offering.model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Analyze the real visual content relevant to the user's "
                                    "request. Describe objects, people, actions, scene context, "
                                    "spatial relationships, and important visible text. State "
                                    "uncertainty; do not guess hidden facts.\n"
                                    f"User request: {selected_question}"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/jpeg;base64,"
                                    + base64.b64encode(normalized_image.data).decode("ascii")
                                },
                            },
                        ],
                    }
                ],
                "temperature": 0.1,
                "max_tokens": 512,
                "stream": False,
            }
            try:
                response = await self.client.invoke_json(
                    resolved,
                    "openai.chat.completions",
                    payload,
                )
                answer = _response_text(response)
            except asyncio.CancelledError:
                raise
            except ComputeVisionError:
                raise
            except ComputeServiceError as exc:
                raise ComputeVisionError(exc.code) from exc
            answers.append(
                f"Image {index} ({part.get('filename') or part.get('attachment_id')}):\n{answer}"
            )
        return LanVisionProjection(
            text="\n\n".join(answers),
            node_id=resolved.snapshot.node.id,
            service_id=offering.service_id,
            model_id=str(offering.model_id or ""),
        )


__all__ = [
    "ComputeVisionError",
    "LanComputeVisionProjector",
    "LanVisionProjection",
]
