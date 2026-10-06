"""Read-only execution facade over the one Agent configuration owner."""
from __future__ import annotations

import copy
from orchestrator.flexible_backend_manager import FlexibleBackendManager


class _FrozenState:
    def __init__(self, values):
        self.values = copy.deepcopy(values)

    def read(self):
        return copy.deepcopy(self.values)

    def replace(self, *args, **kwargs):
        raise RuntimeError("Session execution cannot write Agent configuration")

    update = replace


class SessionBackendManager(FlexibleBackendManager):
    def __init__(self, owner, runtime, frozen):
        # Do not run FlexibleBackendManager.__init__ or its restore/migration.
        self.owner = owner
        self.config = frozen["config"]
        self.global_config = owner.global_config
        self.secrets = owner.secrets
        self.logger = owner.logger
        self.current_backend = None
        self.runtime = runtime
        self.state_file = owner.state_file
        self.state_store = _FrozenState(frozen["state"])
        for name, value in frozen["settings"].items():
            setattr(self, name, value)

    def _write_state_dict(self, state):
        raise RuntimeError("Session execution cannot write Agent configuration")

    def _attach_runtime_context(self, adapter_cfg):
        super()._attach_runtime_context(adapter_cfg)
        if adapter_cfg.engine == "her-v2":
            owner = self.owner.current_backend
            if owner is not None and getattr(owner, "_initialized", False):
                adapter_cfg._her_v3_session_services = owner
