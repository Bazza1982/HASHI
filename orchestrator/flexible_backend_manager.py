import json
import logging
from pathlib import Path
from typing import Optional, Any
from orchestrator.config import FlexibleAgentConfig, GlobalConfig, AgentConfig
from orchestrator.flexible_backend_registry import get_secret_lookup_order
from orchestrator.workzone import access_root_for_workzone

class FlexibleBackendManager:
    def __init__(self, config: FlexibleAgentConfig, global_config: GlobalConfig, secrets: dict):
        self.config = config
        self.global_config = global_config
        self.secrets = secrets
        self.logger = logging.getLogger(f"BackendMgr.{config.name}")
        self.current_backend = None
        self.state_file = self.config.workspace_dir / "state.json"
        self._agents_json_global = self._load_agents_json_global()
        self._load_state()

    def _load_agents_json_global(self) -> dict:
        """Load the 'global' section from agents.json for default_tools etc."""
        try:
            cfg_path = getattr(self.global_config, 'config_path', None)
            if cfg_path and Path(cfg_path).exists():
                raw = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
                return raw.get("global", {})
        except Exception:
            pass
        return {}

    def _load_state(self):
        self._active_model_override = None
        self.agent_mode = "flex"  # default mode
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                if "active_backend" in state:
                    self.config.active_backend = state["active_backend"]
                if "active_model" in state:
                    self._active_model_override = state["active_model"]
                if "agent_mode" in state:
                    self.agent_mode = state["agent_mode"]
            except Exception as e:
                self.logger.error(f"Failed to load state.json: {e}")

    def _save_state(self, active_model: str | None = None):
        if active_model is not None:
            self._active_model_override = active_model
        state = {
            "active_backend": self.config.active_backend,
            "agent_mode": self.agent_mode,
        }
        if getattr(self, "_active_model_override", None):
            state["active_model"] = self._active_model_override
        try:
            self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as e:
            self.logger.error(f"Failed to save state.json: {e}")

    def persist_state(self, active_model: str | None = None):
        self._save_state(active_model=active_model)

    def _resolve_api_key(self, engine: str) -> Optional[Any]:
        for secret_key in get_secret_lookup_order(engine, self.config.name):
            api_key = self.secrets.get(secret_key)
            if api_key:
                self.logger.info(f"Resolved API key for {engine} via '{secret_key}'")
                return api_key
        return None

    async def initialize_active_backend(self, target_model: str | None = None) -> bool:
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
            model=target_model or getattr(self, "_active_model_override", None) or backend_cfg_raw.get("model", "default"),
            is_active=True,
            extra=extra,
            access_scope=backend_scope,
            project_root=self.config.project_root,
        )

        try:
            from adapters.registry import get_backend_class
            BackendClass = get_backend_class(engine)
            api_key = self._resolve_api_key(engine)

            self.current_backend = BackendClass(adapter_cfg, self.global_config, api_key)

            # V2.2+: inject ToolRegistry for API backends that support tool calls
            if engine in ("openrouter-api", "deepseek-api", "ollama-api"):
                tools_cfg = self._resolve_tools_config(backend_cfg_raw)
                if tools_cfg:
                    self._attach_tool_registry(tools_cfg, adapter_cfg)

            return await self.current_backend.initialize()
        except Exception as e:
            self.logger.error(f"Failed to initialize backend {engine}: {e}")
            return False

    def _resolve_tools_config(self, backend_cfg_raw: dict) -> dict | None:
        """Merge global default_tools with per-backend tools config.

        Priority: per-backend 'allowed' list extends (not replaces) global defaults.
        Per-backend max_loops and tool_options override global ones.
        """
        global_raw = getattr(self, '_agents_json_global', None) or {}
        global_tools = global_raw.get("default_tools", {})
        backend_tools = backend_cfg_raw.get("tools", {})

        if not global_tools and not backend_tools:
            return None

        # Merge allowed lists (union, global first)
        global_allowed = set(global_tools.get("allowed", []))
        backend_allowed = set(backend_tools.get("allowed", []))
        merged_allowed = list(global_allowed | backend_allowed)

        if not merged_allowed:
            return None

        # Backend-specific settings override global
        merged = dict(global_tools)
        merged.update(backend_tools)
        merged["allowed"] = merged_allowed
        return merged

    def _attach_tool_registry(self, tools_cfg: dict, adapter_cfg) -> None:
        """Create and attach a ToolRegistry to the current OpenRouter backend."""
        try:
            from tools.registry import ToolRegistry

            allowed = tools_cfg.get("allowed", [])
            if not allowed:
                return

            workzone_dir = (adapter_cfg.extra or {}).get("workzone_dir")
            workspace_dir = Path(workzone_dir).expanduser().resolve() if workzone_dir else adapter_cfg.workspace_dir
            access_root = access_root_for_workzone(adapter_cfg.resolve_access_root(), workspace_dir if workzone_dir else None)
            max_loops = int(tools_cfg.get("max_loops", 25))

            # Per-tool options (e.g. bash.timeout_max, file_write.max_file_size_kb)
            tool_options = {k: v for k, v in tools_cfg.items()
                            if k not in ("allowed", "max_loops")}

            # Inject agent token and authorized_id for telegram_send_file tool
            enriched_secrets = dict(self.secrets)
            agent_token = self.secrets.get(self.config.telegram_token_key)
            if agent_token:
                enriched_secrets["_agent_telegram_token"] = agent_token
            if self.global_config and self.global_config.authorized_id:
                enriched_secrets["_authorized_telegram_id"] = str(self.global_config.authorized_id)

            registry = ToolRegistry(
                allowed_tools=allowed,
                access_root=access_root,
                workspace_dir=workspace_dir,
                secrets=enriched_secrets,
                tool_options=tool_options,
                max_loops=max_loops,
                audit_context={
                    "agent_name": getattr(adapter_cfg, "name", workspace_dir.name),
                    "workspace_dir": str(workspace_dir),
                    "safety_mode": "read_write",
                },
            )
            self.current_backend.tool_registry = registry
            self.logger.info(
                f"ToolRegistry attached: allowed={allowed}, max_loops={max_loops}"
            )
        except Exception as e:
            self.logger.error(f"Failed to attach ToolRegistry: {e}")

    async def switch_backend(self, target_engine: str, target_model: str | None = None) -> bool:
        self.logger.info(f"Switching backend to {target_engine}" + (f" model={target_model}" if target_model else ""))
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
        self._save_state(active_model=target_model)

        # Initialize target backend — rollback on failure
        if not await self.initialize_active_backend(target_model=target_model):
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
