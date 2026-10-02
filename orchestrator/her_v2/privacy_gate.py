"""Experimental local PII gate for HERV3 model-provider payloads.

The optional Presidio/spaCy stack runs in a separate Python interpreter. The
main HASHI interpreter never imports that stack. This text-only pilot fails
closed when it cannot inspect a complete outbound payload.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any


MAX_PAYLOAD_CHARACTERS = 500_000
MAX_SIDECAR_BYTES = 2_000_000
SIDECAR_TIMEOUT_SECONDS = 30
_LABEL = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
_UNSUPPORTED_MEDIA_KEYS = frozenset(
    {"image_url", "input_audio", "audio", "video", "file_data", "input_file"}
)
_UNSUPPORTED_MEDIA_TYPES = frozenset(
    {"image_url", "input_audio", "audio", "video", "file", "input_file"}
)


class PrivacyGateError(RuntimeError):
    """A Level 2 request cannot be proven safe for the local model boundary."""


Detector = Callable[[list[str]], Awaitable[list[list[dict[str, Any]]]]]


class OutboundPrivacyGate:
    def __init__(
        self,
        *,
        detector: Detector | None = None,
        python_executable: str | None = None,
    ) -> None:
        self._detector = detector or self._detect_with_sidecar
        self._python_executable = python_executable
        self._request_id: str | None = None
        self._aliases: dict[tuple[str, str], str] = {}
        self._counts: Counter[str] = Counter()

    async def _detect_with_sidecar(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        executable = self._python_executable or os.getenv(
            "HASHI_PRIVACY_FILTER_PYTHON", ""
        )
        python_path = Path(executable) if executable else None
        if (
            python_path is None
            or not python_path.is_absolute()
            or not python_path.is_file()
            or not os.access(python_path, os.X_OK)
        ):
            raise PrivacyGateError("local privacy model is not configured")
        sidecar = Path(__file__).resolve().parents[2] / "tools" / "privacy_filter_sidecar.py"
        request = json.dumps({"texts": texts}, ensure_ascii=False).encode("utf-8")
        if len(request) > MAX_SIDECAR_BYTES:
            raise PrivacyGateError("privacy payload exceeds the pilot limit")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(python_path), str(sidecar),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_SIDECAR_BYTES,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(request), timeout=SIDECAR_TIMEOUT_SECONDS
            )
            if process.returncode != 0 or len(stdout) > MAX_SIDECAR_BYTES:
                raise PrivacyGateError("local privacy model failed")
            response = json.loads(stdout)
            if response.get("ok") is not True or not isinstance(
                response.get("matches"), list
            ):
                raise PrivacyGateError("local privacy model returned an invalid result")
            return response["matches"]
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except Exception:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise PrivacyGateError("local privacy model unavailable") from None

    @staticmethod
    def _collect(value: Any, values: dict[str, None], keys: set[str]) -> None:
        if isinstance(value, str):
            if value.startswith("data:"):
                raise PrivacyGateError("encoded media cannot be inspected")
            values.setdefault(value, None)
            return
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise PrivacyGateError("non-finite payload value")
            return
        if isinstance(value, list):
            for item in value:
                OutboundPrivacyGate._collect(item, values, keys)
            return
        if isinstance(value, Mapping):
            content_type = value.get("type")
            if _UNSUPPORTED_MEDIA_KEYS.intersection(value) or (
                isinstance(content_type, str)
                and content_type in _UNSUPPORTED_MEDIA_TYPES
            ):
                raise PrivacyGateError("media needs a separate privacy inspection")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise PrivacyGateError("non-text payload key")
                keys.add(key)
                values.setdefault(key, None)
                OutboundPrivacyGate._collect(item, values, keys)
            return
        raise PrivacyGateError("unsupported privacy payload value")

    @staticmethod
    def _spans(text: str, matches: Any) -> list[tuple[int, int, str]]:
        if not isinstance(matches, list):
            raise PrivacyGateError("invalid local privacy model result")
        ranked = []
        for match in matches:
            if not isinstance(match, Mapping):
                raise PrivacyGateError("invalid local privacy model result")
            start, end, label = match.get("start"), match.get("end"), match.get("label")
            score = match.get("score", 0)
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(text)
                or not isinstance(label, str)
                or not _LABEL.fullmatch(label)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 0 <= score <= 1
            ):
                raise PrivacyGateError("invalid local privacy model span")
            ranked.append((float(score), end - start, start, end, label))
        selected: list[tuple[int, int, str]] = []
        for _, _, start, end, label in sorted(ranked, reverse=True):
            if not any(start < prior_end and end > prior_start
                       for prior_start, prior_end, _ in selected):
                selected.append((start, end, label))
        return sorted(selected)

    def _replace(self, text: str, spans: list[tuple[int, int, str]]) -> str:
        parts = []
        previous = 0
        for start, end, label in spans:
            parts.append(text[previous:start])
            raw = text[start:end]
            key = (label, raw)
            token = self._aliases.get(key)
            if token is None:
                self._counts[label] += 1
                token = f"[{label}_{self._counts[label]}]"
                self._aliases[key] = token
            parts.append(token)
            previous = end
        parts.append(text[previous:])
        return "".join(parts)

    async def sanitize(self, payload: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or not request_id:
            raise PrivacyGateError("invalid privacy request scope")
        if not isinstance(payload.get("model"), str) or not payload["model"].strip():
            raise PrivacyGateError("missing privacy model target")
        if request_id != self._request_id:
            self._request_id = request_id
            self._aliases.clear()
            self._counts.clear()
        values: dict[str, None] = {}
        keys: set[str] = set()
        self._collect(payload, values, keys)
        texts = list(values)
        if sum(len(text) for text in texts) > MAX_PAYLOAD_CHARACTERS:
            raise PrivacyGateError("privacy payload exceeds the pilot limit")
        try:
            detected = await self._detector(texts)
        except PrivacyGateError:
            raise
        except Exception:
            raise PrivacyGateError("local privacy model unavailable") from None
        if not isinstance(detected, list) or len(detected) != len(texts):
            raise PrivacyGateError("invalid local privacy model result")
        replacements = {}
        for text, matches in zip(texts, detected):
            spans = self._spans(text, matches)
            if text in keys and spans:
                raise PrivacyGateError("sensitive payload key cannot be changed")
            replacements[text] = self._replace(text, spans)

        def copied(value: Any) -> Any:
            if isinstance(value, str):
                return replacements[value]
            if isinstance(value, list):
                return [copied(item) for item in value]
            if isinstance(value, Mapping):
                return {key: copied(item) for key, item in value.items()}
            return value

        sanitized = copied(payload)
        if sanitized.get("model") != payload.get("model"):
            raise PrivacyGateError("model target changed during privacy inspection")
        return sanitized
