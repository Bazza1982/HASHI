import json
import logging
from pathlib import Path
from typing import Optional, Any
from orchestrator.config import FlexibleAgentConfig, GlobalConfig, AgentConfig

class FlexibleBackendManager:
    def __init__(self, config: FlexibleAgentConfig, global_config: GlobalConfig, secrets: dict):
        self.config = config
        self.global_config = global_config
        self.secrets = secrets
        self.logger = logging.getLogger(f"BackendMgr.{config.name}")
        self.current_backend = None
        self.state_file = self.config.workspace_dir / "state.json"
        self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                if "active_backend" in state:
                    self.config.active_backend = state["active_backend"]
            except Exception as e:
                self.logger.error(f"Failed to load state.json: {e}")

    def _save_state(self):
        state = {"active_backend": self.config.active_backend}
        try:
            self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as e:
            self.logger.error(f"Failed to save state.json: {e}")

    async def initialize_active_backend(self) -> bool:
        engine = self.config.active_backend
        self.logger.info(f"Initializing active backend: {engine}")
        
        # Find backend config in allowed_backends
        backend_cfg_raw = next((b for b in self.config.allowed_backends if b["engine"] == engine), None)
        if not backend_cfg_raw:
            self.logger.error(f"Active backend {engine} not found in allowed_backends.")
            return False

        # Create a mock AgentConfig for the adapter
        # Start with agent-level extra (e.g. process_timeout, background_mode) as base
        agent_extra = dict(getattr(self.config, "extra", None) or {})
        # Overlay per-backend values (minus routing fields)
        backend_extra = dict(backend_cfg_raw)
        backend_extra.pop("engine", None)
        backend_extra.pop("model", None)
        backend_scope = backend_cfg_raw.get("access_scope", self.config.access_scope)
        backend_extra.pop("access_scope", None)
        extra = {**agent_extra, **backend_extra}
        adapter_cfg = AgentConfig(
            name=self.config.name,
            engine=engine,
            workspace_dir=self.config.workspace_dir,
            system_md=self.config.system_md,
            model=backend_cfg_raw.get("model", "default"),
            is_active=True,
            extra=extra,
            access_scope=backend_scope,
            project_root=self.config.project_root,
        )

        try:
            from adapters.registry import get_backend_class
            BackendClass = get_backend_class(engine)
            api_key = self.secrets.get(f"{engine}_key", None)
            
            self.current_backend = BackendClass(adapter_cfg, self.global_config, api_key)
            return await self.current_backend.initialize()
        except Exception as e:
            self.logger.error(f"Failed to initialize backend {engine}: {e}")
            return False

    async def switch_backend(self, target_engine: str) -> bool:
        self.logger.info(f"Switching backend to {target_engine}")
        backend_cfg_raw = next((b for b in self.config.allowed_backends if b["engine"] == target_engine), None)
        if not backend_cfg_raw:
            self.logger.error(f"Target backend {target_engine} not allowed.")
            return False

        previous_engine = self.config.active_backend

        # Cleanly shut down current backend
        if self.current_backend:
            await self.shutdown()

        # Update config and state
        self.config.active_backend = target_engine
        self._save_state()

        # Initialize target backend — rollback on failure
        if not await self.initialize_active_backend():
            self.logger.error(
                f"Failed to initialize {target_engine}; rolling back to {previous_engine}"
            )
            self.config.active_backend = previous_engine
            self._save_state()
            if not await self.initialize_active_backend():
                self.logger.critical(
                    f"Rollback to {previous_engine} also failed. Agent has no active backend."
                )
            return False
        return True

    async def shutdown(self):
        if self.current_backend:
            await self.current_backend.shutdown()
            self.current_backend = None

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event=None,
    ):
        if not self.current_backend:
            raise RuntimeError("No active backend initialized.")
        return await self.current_backend.generate_response(
            prompt,
            request_id,
            is_retry=is_retry,
            silent=silent,
            on_stream_event=on_stream_event,
        )
