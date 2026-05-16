from __future__ import annotations

import asyncio
import http.client
import json
import logging
import sys
import traceback
from pathlib import Path

from orchestrator.agent_directory import AgentDirectory
from orchestrator.api_gateway import APIGatewayServer, available_gateway_models, default_gateway_model
from orchestrator.scheduler import TaskScheduler
from orchestrator.workbench_api import WorkbenchApiServer

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")


class ServiceManager:
    """Control runtime services while live handles remain on the kernel."""

    def __init__(self, kernel):
        self.kernel = kernel

    def build_agent_directory(self):
        capabilities_path = self.kernel.paths.bridge_home / "agent_capabilities.json"
        self.kernel.agent_directory = AgentDirectory(
            self.kernel.paths.config_path,
            capabilities_path,
            self.kernel.runtimes,
        )
        return self.kernel.agent_directory

    def _api_gateway_state_path(self) -> Path:
        return self.kernel.paths.bridge_home / "api_gateway_state.json"

    def _load_api_gateway_state(self) -> dict:
        path = self._api_gateway_state_path()
        default_state = {
            "enabled": bool(getattr(self.kernel, "enable_api_gateway", False)),
            "default_model": default_gateway_model(),
        }
        if not path.exists():
            return default_state
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default_state
        if not isinstance(loaded, dict):
            return default_state
        state = dict(default_state)
        state.update(loaded)
        if state.get("default_model") not in available_gateway_models():
            state["default_model"] = default_gateway_model()
        state["enabled"] = bool(state.get("enabled"))
        return state

    def _save_api_gateway_state(self, *, enabled: bool | None = None, default_model: str | None = None) -> dict:
        state = self._load_api_gateway_state()
        if enabled is not None:
            state["enabled"] = bool(enabled)
        if default_model is not None:
            normalized = str(default_model or "").strip()
            if normalized not in available_gateway_models():
                raise ValueError(f"unknown API gateway model: {default_model}")
            state["default_model"] = normalized
        path = self._api_gateway_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return state

    def api_gateway_base_url(self) -> str | None:
        global_cfg = self.kernel.global_cfg
        if global_cfg is None:
            return None
        running = self.kernel.api_gateway
        host = getattr(running, "bind_host", None) or str(getattr(global_cfg, "api_host", "") or "127.0.0.1").strip()
        if host in {"", "0.0.0.0", "localhost"}:
            host = "127.0.0.1"
        return f"http://{host}:{int(global_cfg.api_gateway_port)}"

    def api_gateway_state_snapshot(self) -> dict:
        state = self._load_api_gateway_state()
        return {
            "enabled": state["enabled"],
            "running": self.kernel.api_gateway is not None,
            "default_model": state["default_model"],
            "available_models": available_gateway_models(),
            "base_url": self.api_gateway_base_url(),
            "port": getattr(self.kernel.global_cfg, "api_gateway_port", None) if self.kernel.global_cfg else None,
        }

    async def start_workbench_api(self, global_cfg, secrets):
        try:
            self.kernel.workbench_api = WorkbenchApiServer(
                self.kernel.paths.config_path,
                global_cfg,
                self.kernel.runtimes,
                secrets=secrets,
                orchestrator=self.kernel,
            )
            await self.kernel.workbench_api.start()
            bind_host = getattr(self.kernel.workbench_api, "bind_host", "127.0.0.1")
            main_logger.info(
                "Workbench API listening on http://%s:%s",
                bind_host,
                global_cfg.workbench_port,
            )
            bridge_logger.info(
                "Workbench API listening on http://%s:%s",
                bind_host,
                global_cfg.workbench_port,
            )
        except Exception as e:
            self.kernel.workbench_api = None
            main_logger.warning(
                "Workbench API failed to start; continuing without workbench integration: %s",
                e,
            )
            main_logger.debug(traceback.format_exc())
            bridge_logger.warning(
                "Workbench API failed to start; continuing without workbench integration: %s: %s",
                type(e).__name__,
                e,
            )

    async def start_api_gateway(self, global_cfg, secrets):
        state = self._load_api_gateway_state()
        if state["enabled"]:
            self.kernel.enable_api_gateway = True
        if not self.kernel.enable_api_gateway:
            main_logger.info("API Gateway disabled (use --api-gateway to enable).")
            return
        try:
            self.kernel.api_gateway = APIGatewayServer(
                global_cfg,
                secrets,
                workspace_root=self.kernel.paths.workspaces_root,
                default_model=state["default_model"],
            )
            await self.kernel.api_gateway.start()
            bind_host = getattr(self.kernel.api_gateway, "bind_host", None) or "127.0.0.1"
            main_logger.info(
                "API Gateway listening on http://%s:%s",
                bind_host,
                global_cfg.api_gateway_port,
            )
            bridge_logger.info(
                "API Gateway listening on http://%s:%s",
                bind_host,
                global_cfg.api_gateway_port,
            )
        except Exception as e:
            self.kernel.api_gateway = None
            main_logger.warning("API Gateway failed to start; continuing without it: %s", e)
            main_logger.debug(traceback.format_exc())

    async def start_api_gateway_runtime(self) -> tuple[bool, str]:
        if self.kernel.global_cfg is None:
            return False, "API Gateway cannot start before global config is loaded."
        if self.kernel.api_gateway is not None:
            self.kernel.enable_api_gateway = True
            self._save_api_gateway_state(enabled=True)
            return True, "API Gateway is already running."
        self.kernel.enable_api_gateway = True
        self._save_api_gateway_state(enabled=True)
        await self.start_api_gateway(self.kernel.global_cfg, self.kernel.secrets)
        if self.kernel.api_gateway is None:
            return False, "API Gateway failed to start."
        return True, f"API Gateway started on {self.api_gateway_base_url()}."

    async def stop_api_gateway_runtime(self, timeout: float = 5.0) -> tuple[bool, str]:
        self.kernel.enable_api_gateway = False
        self._save_api_gateway_state(enabled=False)
        if self.kernel.api_gateway is None:
            return True, "API Gateway is already stopped."
        await self.stop_api_gateway(timeout=timeout)
        return True, "API Gateway stopped."

    def set_api_gateway_default_model(self, model: str) -> tuple[bool, str]:
        normalized = str(model or "").strip()
        if normalized not in available_gateway_models():
            return False, f"Unknown API gateway model: {model}"
        self._save_api_gateway_state(default_model=normalized)
        if self.kernel.api_gateway is not None:
            self.kernel.api_gateway.set_default_model(normalized)
        return True, f"API Gateway default model set to {normalized}."

    def start_scheduler(self, global_cfg):
        self.kernel.scheduler = TaskScheduler(
            self.kernel.paths.tasks_path,
            self.kernel.paths.state_path,
            self.kernel.runtimes,
            global_cfg.authorized_id,
            self.kernel.skill_manager,
            orchestrator=self.kernel,
        )
        self.kernel.scheduler_task = asyncio.create_task(self.kernel.scheduler.run(), name="scheduler")

    async def start_runtime_services(self, global_cfg, secrets):
        self.build_agent_directory()
        await self.start_workbench_api(global_cfg, secrets)
        await self.start_api_gateway(global_cfg, secrets)
        self.start_scheduler(global_cfg)

    async def stop_scheduler(self, timeout: float = 5.0):
        if self.kernel.scheduler_task is None:
            return
        bridge_logger.info("Stopping scheduler task")
        self.kernel.scheduler_task.cancel()
        try:
            await asyncio.wait_for(self.kernel.scheduler_task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            bridge_logger.warning("Scheduler task stop timed out or was cancelled")
        finally:
            self.kernel.scheduler_task = None
            self.kernel.scheduler = None

    async def restart_scheduler(self):
        await self.repair_workbench_api_if_needed()
        await self.stop_scheduler()
        reloaded_scheduler = sys.modules["orchestrator.scheduler"].TaskScheduler
        self.kernel.scheduler = reloaded_scheduler(
            self.kernel.paths.tasks_path,
            self.kernel.paths.state_path,
            self.kernel.runtimes,
            self.kernel.global_cfg.authorized_id if self.kernel.global_cfg else 0,
            self.kernel.skill_manager,
            orchestrator=self.kernel,
        )
        self.kernel.scheduler_task = asyncio.create_task(self.kernel.scheduler.run(), name="scheduler")
        main_logger.info("Hot restart: scheduler recreated with reloaded code.")
        bridge_logger.info("Hot restart: scheduler recreated with reloaded code")

    async def repair_workbench_api_if_needed(self):
        global_cfg = self.kernel.global_cfg
        if global_cfg is None:
            bridge_logger.warning("Workbench API repair skipped: global config is unavailable")
            return
        workbench_api = self.kernel.workbench_api
        if workbench_api is not None:
            bind_host = getattr(workbench_api, "bind_host", None) or "127.0.0.1"
            if await self._workbench_api_healthy(bind_host, global_cfg.workbench_port):
                return
            bridge_logger.warning(
                "Workbench API exists but health check failed on %s:%s; rebuilding service",
                bind_host,
                global_cfg.workbench_port,
            )
            await self.stop_workbench_api(timeout=2.0)
        bridge_logger.warning(
            "Workbench API missing during hot restart; attempting repair on port %s",
            global_cfg.workbench_port,
        )
        await self.start_workbench_api(global_cfg, self.kernel.secrets)

    async def _workbench_api_healthy(self, host: str, port: int, timeout: float = 1.0) -> bool:
        def _probe():
            conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
            try:
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                response.read()
                return 200 <= response.status < 500
            except Exception:
                return False
            finally:
                conn.close()

        return await asyncio.to_thread(_probe)

    async def stop_workbench_api(self, timeout: float = 5.0):
        if self.kernel.workbench_api is None:
            return
        bridge_logger.info("Stopping Workbench API")
        try:
            await asyncio.wait_for(self.kernel.workbench_api.shutdown(), timeout=timeout)
        except (asyncio.TimeoutError, Exception) as e:
            main_logger.warning("Workbench API shutdown warning: %s", e)
            bridge_logger.warning("Workbench API shutdown warning: %s: %s", type(e).__name__, e)
        finally:
            self.kernel.workbench_api = None

    async def stop_api_gateway(self, timeout: float = 5.0):
        if self.kernel.api_gateway is None:
            return
        bridge_logger.info("Stopping API Gateway")
        try:
            await asyncio.wait_for(self.kernel.api_gateway.stop(), timeout=timeout)
        except (asyncio.TimeoutError, Exception) as e:
            main_logger.warning("API Gateway shutdown warning: %s", e)
            bridge_logger.warning("API Gateway shutdown warning: %s: %s", type(e).__name__, e)
        finally:
            self.kernel.api_gateway = None

    async def stop_runtime_services(self):
        await self.stop_scheduler()
        await self.stop_workbench_api()
        await self.stop_api_gateway()
