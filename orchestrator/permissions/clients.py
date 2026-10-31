"""Private Functions/FC clients. Construction never starts a service.

L0 callers do not import or construct these owners. Endpoints and server keys
come from protected instance configuration, never a model or request DTO.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.remote_registry import BrokeredToolRegistry

PAO_METHODS = frozenset({
    "health", "open_execution", "execution_descriptor", "close_execution",
    "settle_run", "context_status", "request_state", "fence_context", "run_her", "configure_inference",
    "auxiliary_provider_call", "project_model_usage", "worker_usage_receipts",
    "operation_status", "execution_controls",
})
CONTROL_METHODS = frozenset({
    "get_session_request", "list_session_requests", "withdraw_session_request",
    "issue_session_actions", "submit_session_action", "submit_session_decision",
    "login", "logout", "session_user", "manage_session", "session_summary", "native_login",
})


def service_clients(global_config):
    """Read a public pinned installation descriptor; never discover by port."""
    raw = getattr(global_config, "permission_service", None)
    required = {"version", "instance_id", "pao_endpoint", "control_endpoint", "public_key"}
    if (not isinstance(raw, dict) or not required.issubset(raw)
            or set(raw) - required - {"service_sid"} or raw["version"] != 1):
        raise PermissionDenied("a qualified permission service installation is required")
    if str(raw["instance_id"]).upper() != str(global_config.instance_id).upper():
        raise PermissionDenied("permission service belongs to another instance")
    from orchestrator.permissions.windows_ipc import _PIPE

    for field in ("pao_endpoint", "control_endpoint"):
        value = raw[field]
        if not isinstance(value, str) or _PIPE.fullmatch(value) is None:
            raise PermissionDenied("invalid private permission endpoint")
    if raw["pao_endpoint"] == raw["control_endpoint"]:
        raise PermissionDenied("permission roles must have distinct endpoints")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(raw["public_key"], validate=True))
    except (TypeError, ValueError) as exc:
        raise PermissionDenied("invalid pinned permission key") from exc
    return (
        PermissionPipeClient(raw["pao_endpoint"], key, methods=PAO_METHODS),
        PermissionPipeClient(raw["control_endpoint"], key, methods=CONTROL_METHODS),
    )


class PermissionControlClient:
    """Only the trusted original-request FC receives this client."""

    def __init__(self, client):
        self._client = client

    def get_session_request(self, subject, request_id):
        return self._client.call("get_session_request", {"subject": subject, "request_id": request_id})

    def list_session_requests(self, subject, *, before=None, limit=10):
        return self._client.call("list_session_requests", {"subject": subject, "before": before, "limit": limit})

    def withdraw_session_request(self, subject, request_id, expected_revision):
        return self._client.call("withdraw_session_request", {
            "subject": subject, "request_id": request_id, "expected_revision": expected_revision,
        })

    def __getattr__(self, name):
        if name not in CONTROL_METHODS:
            raise AttributeError(name)
        def invoke(*args, **arguments):
            # Preserve the existing narrow view interfaces without exposing
            # generic positional dispatch or arbitrary remote method names.
            positional = {
                "get_session_request": ("subject", "request_id"),
                "list_session_requests": ("subject",),
                "withdraw_session_request": ("subject", "request_id", "expected_revision"),
                "issue_session_actions": ("subject", "request_id", "revision"),
                "submit_session_action": ("subject", "action_id"),
                "submit_session_decision": ("subject", "arguments"),
            }.get(name, ())
            if len(args) > len(positional) or any(key in arguments for key in positional[:len(args)]):
                raise TypeError("invalid permission control arguments")
            arguments.update(zip(positional, args))
            return self._client.call(name, arguments)
        return invoke


class _AuthorityViews:
    def __init__(self, client):
        self._client = client

    def execution_status(self, context):
        return self._client.call("context_status", {"context": context})

    def operation_status_for_task(self, context, operation_id):
        return self._client.call("operation_status", {"context": context, "operation_id": operation_id})

    def get_request(self, context, request_id):
        return self._client.call("request_state", {"context": context, "request_id": request_id})

    def fence_execution(self, context):
        return self._client.call("fence_context", {"context": context})


class _AuxiliaryModelClient:
    """Only the trusted Worker uses this PAO surface for its original ports."""

    def __init__(self, client, binding, accounting):
        self.client, self.binding, self.accounting = client, binding, accounting

    async def call_async(self, method, arguments, *, on_event=None):
        if method != "provider_call":
            raise PermissionDenied("the Worker auxiliary model port is fixed")
        value = await self.client.call_async("auxiliary_provider_call", {
            "binding": self.binding, **arguments,
        }, on_event=on_event)
        self.accounting.received(value["receipt"])
        return value["result"]


class RemoteRunExecutor:
    def __init__(self, owner, binding, descriptor):
        if (not isinstance(descriptor, dict)
                or set(descriptor) != {"binding", "level", "endpoint", "public_key", "tool_definitions"}
                or descriptor["binding"] != binding or descriptor["level"] not in {"L1", "L2"}):
            raise PermissionDenied("execution descriptor changed")
        self._owner, self.binding, self._level = owner, binding, descriptor["level"]
        client = PermissionPipeClient(
            descriptor["endpoint"],
            Ed25519PublicKey.from_public_bytes(base64.b64decode(descriptor["public_key"], validate=True)),
            methods=("tool_execute", "provider_call", "provider_checkpoint", "audit_record", "runtime_port"),
        )
        self._execution_client = client
        self._stage_provider = None
        self._permission_prestart_failure = False
        self.registry = BrokeredToolRegistry(
            client, definitions=descriptor["tool_definitions"],
            scratch_root=Path(owner.workspace_dir),
        )

    def bind_provider(self, provider):
        provider.tool_registry = self.registry
        self._stage_provider = provider
        self.registry._event_sink = getattr(provider, "on_stream_event", None)

    async def run_her(self, runtime, prompt, request_id, *, binding, **arguments):
        if binding != self.binding or request_id != binding.request_id:
            raise PermissionDenied("HER execution binding changed")
        from orchestrator.permissions.host_protocol import pack
        from orchestrator.permissions.host_provider import BrokeredProviderFactory

        recovery = arguments.pop("permission_recovery", None)
        self._execution_client.idle_timeout_s = float(runtime.config.user_idle_timeout_s)
        self._owner._client.idle_timeout_s = float(runtime.config.user_idle_timeout_s)
        try:
            public = await self._owner._client.call_async("configure_inference", {
                "binding": binding, "config": pack(runtime.config),
            })
        except OSError as error:
            from orchestrator.permissions.continuation import PermissionPark
            from orchestrator.permissions.execution_gate import GateResult

            self._permission_prestart_failure = True
            raise PermissionPark(GateResult("unavailable", "")) from error
        if self._stage_provider is not None:
            from orchestrator.permissions.worker_usage import WorkerAccounting

            accounting = WorkerAccounting(self._stage_provider, self._owner._client, binding)
            client = _AuxiliaryModelClient(self._owner._client, binding, accounting)
            if self._level == "L2":
                # Only the host owns main-loop continuation. Presentation and
                # companion calls must not overwrite its ready/pending batch.
                self._stage_provider.bind_provider_context(observer=None)
            self._stage_provider.backend_manager = BrokeredProviderFactory(
                client, profile_config=public["profiles"],
                scratch_root=self._owner.workspace_dir, privacy_level=public["privacy_level"],
                global_config=getattr(getattr(self._stage_provider, "runtime_context", None), "global_config", None),
            )
        if self._level == "L1":
            # L1 preserves all existing HERV3 presentation, habits, companion
            # and accounting ports. Tools cross the central execution gate.
            # A resumed execution can render fresh PCM while preserving its
            # original Run, Turn and pending Tool IDs. Its runtime occurrences
            # need distinct audit IDs; immutable facts still apply within an epoch.
            scoped_audit = runtime.audit_log.for_execution_epoch(binding.context.execution_epoch)
            runtime.audit_log = scoped_audit
            if self._stage_provider is not None:
                self._stage_provider.audit_log = scoped_audit
            return await runtime.run_turn(prompt, request_id, turn_id=binding.context.turn_id, **arguments)
        from orchestrator.permissions.host_protocol import unpack
        from orchestrator.permissions.continuation import PermissionPark
        from orchestrator.permissions.wire import decode
        command = {
            "config": pack(runtime.config), "prompt": prompt, "request_id": request_id,
            "turn_id": binding.context.turn_id,
            "effort": getattr(arguments.get("effort"), "value", arguments.get("effort")),
            "request_content": arguments.get("request_content"),
            "request_mode": arguments.get("request_mode", "text"),
            "pcm_input": runtime.pcm_input, "skills_catalogue": list(runtime.skills_catalogue),
            "recovery": recovery,
        }
        try:
            response = await self._owner._client.call_async(
                "run_her", {"binding": binding, "command": command},
                on_event=getattr(runtime.delivery, "callback", None),
            )
        except OSError as error:
            from orchestrator.permissions.execution_gate import GateResult

            # Contract v2 forbids any Tool effect before its exact durable
            # pending checkpoint. With no private boundary the original Turn
            # is still business-unstarted, even if host creation was uncertain.
            self._permission_prestart_failure = True
            raise PermissionPark(GateResult("unavailable", "")) from error
        if not isinstance(response, dict) or response.get("state") not in {"completed", "parked"}:
            raise PermissionDenied("restricted HER result is unavailable")
        # The adapter owns usage projection; a host result cannot be treated as
        # a zero-cost invocation. Adoption refuses an absent usage consumer.
        from orchestrator.permissions.host_usage import accept_host_usage

        self._stage_provider._permission_projection_commit = lambda projections: self._owner._client.call(
            "project_model_usage", {"binding": binding, "projections": projections},
        )
        accept_host_usage(self._stage_provider, response)
        if response["state"] == "parked":
            raise PermissionPark(decode(response["control"]))
        return unpack(response["result"])

    async def park(self):
        # A park retains the approval epoch but must release host resources.
        return await self._owner._client.call_async(
            "close_execution", {"binding": self.binding, "park": True},
        )


class PermissionRunClient:
    def __init__(self, client, *, store, workspace_dir, settings):
        self._client, self.store = client, store
        self.workspace_dir, self.settings = Path(workspace_dir), settings
        self.authority = _AuthorityViews(client)
        self._executors = {}
        self._health = None

    def actual_controls(self):
        reports = []
        for executor in tuple(self._executors.values()):
            reports.append(self._client.call("execution_controls", {"binding": executor.binding}))
        return reports

    async def qualify(self):
        result = await self._client.call_async("health", {"instance_id": self.store.instance_id})
        if (not isinstance(result, dict) or result.get("instance_id") != self.store.instance_id
                or result.get("state") != "ready" or result.get("execution_contract") != 2
                or self.settings.requested_level.value not in result.get("levels", [])):
            raise PermissionDenied("requested permission controls are unavailable")
        self._health = result
        return True

    def permits_native(self, engine, *, provider=False):
        # Constructing the HER orchestration shell grants no native API/tool
        # capability. Every managed Run qualifies/open-binds before inference.
        return engine in {"her-v2", "her-v3"}

    async def open_execution(self, **arguments):
        await self.qualify()
        binding = await self._client.call_async("open_execution", arguments)
        if not isinstance(binding, RuntimeExecutionBinding):
            raise PermissionDenied("service returned no canonical execution binding")
        descriptor = await self._client.call_async("execution_descriptor", {"binding": binding})
        executor = RemoteRunExecutor(self, binding, descriptor)
        self._executors[binding.context.execution_epoch] = executor
        return binding

    def execution_registry(self, binding):
        result = self._executors.get(binding.context.execution_epoch)
        if result is None or result.binding != binding:
            raise PermissionDenied("execution channel is unavailable")
        return result

    async def fence_for_backend_switch(self, binding):
        # A fresh bounded control connection cannot inherit a model's long idle
        # timeout. PAO has already fenced locally before this best-effort cleanup.
        client = PermissionPipeClient(self._client.endpoint, self._client.public_key,
                                      methods={"fence_context", "close_execution"}, idle_timeout_s=5)
        await client.call_async("fence_context", {"context": binding.context})
        result = await client.call_async("close_execution", {"binding": binding})
        if result is not True:
            raise OSError("old backend cleanup is unverified")
        self._executors.pop(binding.context.execution_epoch, None)
        return True

    async def close_execution(self, *, binding):
        result = await self._client.call_async("close_execution", {"binding": binding})
        if result is not True:
            raise PermissionDenied("executor cleanup is unverified")
        self._executors.pop(binding.context.execution_epoch, None)

    async def settle_run(self, *, binding):
        result = await self._client.call_async("settle_run", {"binding": binding})
        self._executors.pop(binding.context.execution_epoch, None)
        return result


def settings_revision(settings):
    """Stable policy revision from immutable canonical settings, not a counter."""
    raw = asdict(settings)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).digest()
    return int.from_bytes(digest[:7], "big") + 1
