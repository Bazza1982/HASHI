"""Compute-discovered translation provider. Browser media keeps the Phone v1 codec."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from orchestrator.compute_services import (
    ComputeReference, ComputeRequirements, ComputeServiceDirectory,
    ComputeServicesConfig, ComputeServiceError,
)
from .openai_live import OpenAILiveProvider, provider_http_session, _response_bytes
from .protocol import LiveVoiceError, identifier
from .provider import ProviderCapabilities

MODELS = frozenset({"hy-simulmt-en-zh", "hy-simulmt-zh-en"})
CREATE_PROTOCOL = "hashi.phone.webrtc"
ATTACH_PROTOCOL = "hashi.phone.sideband"


class SimulMTProvider(OpenAILiveProvider):
    provider_id = "hy-simulmt"
    capabilities = ProviderCapabilities(
        version="hy-simulmt-v1", max_input_messages=1, max_instruction_tokens=512,
        max_input_tokens=512, max_session_seconds=1800, proactive_opening=False,
        context_updates=False, task_results=False, mode="interpretation",
    )

    def __init__(self, *, directory=None, node_id=None, service_id=None):
        config = os.environ.get("HASHI_SIMULMT_COMPUTE_CONFIG", "")
        self.directory = directory or (ComputeServiceDirectory(ComputeServicesConfig(Path(config))) if config else None)
        self.node_id = node_id or os.environ.get("HASHI_SIMULMT_NODE", "")
        self.service_id = service_id or os.environ.get("HASHI_SIMULMT_SERVICE", "")
        self.bindings = {}

    def credential(self, secrets):
        return ""  # The configured Compute client owns credential-reference resolution.

    def is_available(self, secrets):
        return bool(self.directory is not None and self.node_id and self.service_id)

    def validate_selection(self, model, voice):
        if model not in MODELS or voice != "default":
            raise LiveVoiceError("live_model_unqualified", 503)

    def media_descriptor(self):
        # This is the actual wire codec, independent of provider identity/version.
        return {"transport": "webrtc", "protocol": "openai-live-v1",
                "data_channel": "oai-events", "version": 1}

    def billing_descriptor(self, model):
        return None

    def encode_history(self, messages):
        if messages:
            raise LiveVoiceError("live_interpretation_context_invalid")
        return []

    def validate_session(self, instructions, model, voice, messages):
        self.validate_selection(model, voice)
        self.encode_history(messages)

    async def fit_input(self, http, **kwargs):
        self.encode_history(kwargs["input_messages"])
        return [], {"input_tokens_exact": 0, "provider_tokens_limit": 512,
                    "provider_count_requests": 0, "history_included_units": 0,
                    "history_omitted_units": 0}

    async def create(self, http, *, key, sdp, model, voice, **kwargs):
        self.validate_selection(model, voice)
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode()) > 65536:
            raise LiveVoiceError("live_sdp_invalid")
        if not self.is_available({}):
            raise LiveVoiceError("live_translation_not_configured", 503)
        try:
            refreshed = await self.directory.refresh()
            if self.node_id in refreshed.errors:
                raise ComputeServiceError(refreshed.errors[self.node_id])
            resolved = refreshed.catalogue.resolve(
                ComputeReference(self.node_id, self.service_id, model),
                ComputeRequirements(input_modalities=frozenset({"audio"}),
                    output_modalities=frozenset({"audio", "text"}),
                    protocols=frozenset({CREATE_PROTOCOL, ATTACH_PROTOCOL}),
                    locations=frozenset({"local", "lan"})),
            )
        except ComputeServiceError as exc:
            raise LiveVoiceError(exc.code, 503) from exc
        headers = self.directory.client.request_headers(resolved.snapshot.node)
        try:
            async with http.post(resolved.interface_url(CREATE_PROTOCOL),
                json={"session": {"model": model, "audio": {"output": {"voice": voice}}},
                      "transport": {"type": "webrtc", "sdp": sdp}},
                headers=headers, allow_redirects=False) as response:
                payload = json.loads(await _response_bytes(response, limit=1048576))
                if response.status not in {200, 201}:
                    raise LiveVoiceError("live_provider_create_rejected", response.status if response.status in {429,503} else 502)
        except LiveVoiceError:
            raise
        except Exception as exc:
            raise LiveVoiceError("live_provider_create_unknown", 502) from exc
        session_id = identifier(payload.get("session", {}).get("id"))
        answer = payload.get("transport", {}).get("sdp")
        if not isinstance(answer, str) or not answer.startswith("v=0") or len(answer.encode()) > 65536:
            raise LiveVoiceError("live_provider_answer_invalid", 502)
        self.bindings = {sid: row for sid, row in self.bindings.items() if time.monotonic()-row[2] < 1800}
        if len(self.bindings) >= 256:
            raise LiveVoiceError("live_provider_capacity", 503)
        endpoint = resolved.interface_url(ATTACH_PROTOCOL, session_id=session_id)
        parsed = urlsplit(endpoint)
        websocket = urlunsplit(("wss" if parsed.scheme == "https" else "ws",
                               parsed.netloc, parsed.path, "", ""))
        self.bindings[session_id] = (websocket, headers, time.monotonic())
        return {"provider_session_id": session_id, "sdp_answer": answer}

    async def attach(self, http, *, key, provider_session_id):
        binding = self.bindings.get(provider_session_id)
        if not binding:
            raise LiveVoiceError("live_provider_binding_unavailable", 503)
        return await http.ws_connect(binding[0], headers=binding[1],
                                     heartbeat=20, max_msg_size=262144)

    def normalize_event(self, raw):
        event = super().normalize_event(raw)
        if event and event["type"] == "action.proposed":
            raise LiveVoiceError("live_interpretation_delegation_forbidden", 502)
        return event

    def update(self, *args, **kwargs):
        raise LiveVoiceError("live_interpretation_update_unsupported")

    def opening(self, *args, **kwargs):
        raise LiveVoiceError("live_interpretation_opening_unsupported")
