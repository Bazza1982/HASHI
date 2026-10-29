"""Actual HASHI API adapters in a restricted host, with brokered transport only."""
from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

from adapters.registry import get_backend_class
from orchestrator.flexible_backend_registry import canonical_backend_engine
from orchestrator.privacy_levels import require_herv3_provider_compatibility
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.execution_gate import GateResult
from orchestrator.permissions.model_broker import api_result_from_wire
from orchestrator.permissions.wire import PermissionRemoteError


class BrokeredProviderFactory:
    """Create existing adapters for exact qualified profiles, without real keys."""

    def __init__(self, client, *, profile_config, scratch_root, privacy_level):
        self._client = client
        self._profiles = {}
        self.privacy_level = privacy_level
        self._root = Path(scratch_root)
        if not self._root.is_absolute():
            raise ValueError("a sealed host scratch root is required")
        for item in profile_config:
            engine, model = canonical_backend_engine(item["engine"]), item["model"]
            if not isinstance(model, str) or not model:
                raise ValueError("an exact selected model is required")
            self._profiles[(engine, model)] = dict(item)
        # Derive this view from the qualified profiles received from PAO.
        self.config = SimpleNamespace(allowed_backends=[
            {"engine": engine, "model": model} for engine, model in self._profiles
        ])
        self.global_config = SimpleNamespace()

    def create_ephemeral_backend(self, engine, target_model=None):
        return self._create(engine, target_model)

    def create_herv3_provider_backend(self, engine, target_model=None):
        require_herv3_provider_compatibility(engine, self.privacy_level)
        backend = self._create(engine, target_model)
        backend._herv3_privacy_scope = True
        return backend

    def _create(self, engine, model):
        engine = canonical_backend_engine(engine)
        profile = self._profiles.get((engine, model))
        if profile is None:
            raise PermissionError("host provider is outside the qualified task profiles")
        cfg = SimpleNamespace(
            name="restricted-her-host", engine=engine, model=model,
            workspace_dir=self._root, system_md=None, extra=dict(profile.get("extra") or {}),
        )
        backend = get_backend_class(engine)(cfg, self.global_config, "broker-transport-no-credential")
        backend.privacy_level = self.privacy_level
        client = self._client

        async def invoke(adapter, payload, headers, callback, *, use_stream):
            del headers
            # Preserve the exact outbound model context before crossing a
            # fallible service boundary. This is a ready request, not a claim
            # that a Provider answered or that any Tool completed.
            from adapters.openrouter_api import _APIResult

            adapter._observe_provider_context(
                boundary="ready_provider_request", messages=payload["messages"],
                request_id="", result=_APIResult("", None, ""), provider_request_id="",
            )
            try:
                result = await client.call_async(
                    "provider_call", {"engine": engine, "model": model, "payload": payload, "use_stream": use_stream},
                    on_event=callback,
                )
            except PermissionRemoteError as exc:
                if exc.code in {"PermissionDenied", "StaleFencingToken", "permission_service_unavailable"}:
                    raise PermissionPark(GateResult("permission_changed", "")) from exc
                raise
            except (OSError, EOFError, TimeoutError) as exc:
                raise PermissionPark(GateResult("unavailable", "")) from exc
            return api_result_from_wire(result)

        async def once(adapter, payload, headers, callback):
            return await invoke(adapter, payload, headers, callback, use_stream=False)

        async def stream(adapter, payload, headers, callback):
            return await invoke(adapter, payload, headers, callback, use_stream=True)

        backend._call_api_once = MethodType(once, backend)
        backend._stream_api_once = MethodType(stream, backend)
        return backend
