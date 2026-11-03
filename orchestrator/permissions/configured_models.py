"""Selected model credentials remain in the installed trusted service."""
from __future__ import annotations

from types import SimpleNamespace

from adapters.timeout_policy import IDLE_TIMEOUT_KEY, apply_timeout_layers

from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.her_v2.config import HERv2Config
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.ipc_rpc import emit_private_event
from orchestrator.permissions.model_broker import SelectedModelBroker

# This is the public inference-options boundary, not a provider/model catalogue.
# URLs, headers, credentials, file paths and plugin configuration stay private.
_PUBLIC_OPTIONS = frozenset({
    "temperature", "top_p", "top_k", "reasoning", "reasoning_effort", "thinking",
    "provider", "parallel_tool_calls", "response_format", "modalities", "audio",
    "frequency_penalty", "presence_penalty", "repetition_penalty", "seed", "stop",
})


class _ServiceModelAuthority:
    def permits_native(self, engine, *, provider=False):
        return engine == "her-v2" or provider


class ConfiguredModelFactory:
    def __init__(self, config_manager, authority, *, private_workspace, receipt_store=None):
        self.config_manager, self.authority = config_manager, authority
        self.private_workspace, self.receipt_store = private_workspace, receipt_store

    async def create(self, binding, config, run_guard):
        if not isinstance(config, HERv2Config):
            raise PermissionDenied("a frozen HERV3 model configuration is required")
        global_config, agents, secrets = self.config_manager.load()
        matches = [agent for agent in agents if agent.name == binding.context.agent_id]
        if len(matches) != 1:
            raise PermissionDenied("the model capability Agent is unavailable")
        manager = FlexibleBackendManager(matches[0], global_config, secrets)
        manager.runtime = SimpleNamespace(permission_service_client=_ServiceModelAuthority())
        drivers, public = {}, []
        try:
            for profile in config.all_provider_profiles():
                target = (profile.engine, profile.model)
                if target in drivers:
                    continue
                options = dict(profile.options or {})
                if set(options) - _PUBLIC_OPTIONS:
                    raise PermissionDenied("a profile contains non-public inference options")
                driver = manager.create_herv3_provider_backend(*target)
                drivers[target] = driver
                driver.config.workspace_dir = self.private_workspace
                driver.config.system_md = None
                driver.config.extra = apply_timeout_layers(
                    driver.config.extra, engine=target[0],
                    backend_extra=driver.config.extra,
                    persisted_override={IDLE_TIMEOUT_KEY: config.user_idle_timeout_s},
                )
                if not await driver.initialize():
                    raise PermissionDenied("the selected model transport could not initialize")
                public.append({"engine": target[0], "model": target[1], "extra": options})
        except BaseException:
            for driver in drivers.values():
                await driver.shutdown()
            raise
        if not drivers:
            raise PermissionDenied("the Turn has no selected model capability")
        broker = SelectedModelBroker(
            self.authority, binding, drivers=drivers, run_guard=run_guard,
            event_sink=emit_private_event, receipt_store=self.receipt_store,
        )
        return broker, {"profiles": public, "privacy_level": int(manager.privacy_level)}
