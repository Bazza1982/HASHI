from __future__ import annotations

import asyncio
import logging
import sys
import traceback

from orchestrator.agent_directory import AgentDirectory
from orchestrator.api_gateway import APIGatewayServer
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
            main_logger.info(
                "Workbench API listening on http://127.0.0.1:%s",
                global_cfg.workbench_port,
            )
        except Exception as e:
            self.kernel.workbench_api = None
            main_logger.warning(
                "Workbench API failed to start; continuing without workbench integration: %s",
                e,
            )
            main_logger.debug(traceback.format_exc())

    async def start_api_gateway(self, global_cfg, secrets):
        if not self.kernel.enable_api_gateway:
            main_logger.info("API Gateway disabled (use --api-gateway to enable).")
            return
        try:
            self.kernel.api_gateway = APIGatewayServer(
                global_cfg,
                secrets,
                workspace_root=self.kernel.paths.workspaces_root,
            )
            await self.kernel.api_gateway.start()
            main_logger.info(
                "API Gateway listening on http://127.0.0.1:%s",
                global_cfg.api_gateway_port,
            )
        except Exception as e:
            self.kernel.api_gateway = None
            main_logger.warning("API Gateway failed to start; continuing without it: %s", e)
            main_logger.debug(traceback.format_exc())

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
