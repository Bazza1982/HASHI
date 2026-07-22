from __future__ import annotations

import asyncio
import http.client
import importlib
import json
import logging
import os
import socket
import sys
import traceback
from contextlib import suppress
from pathlib import Path

from orchestrator.agent_directory import AgentDirectory
from orchestrator.api_gateway import available_gateway_models, default_gateway_model
from orchestrator.background_jobs import BackgroundJobManager
from orchestrator.scheduler import TaskScheduler
from orchestrator.telegram_delivery_failover import delivery_health_watcher

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

    def _workbench_api_server_cls(self):
        module = sys.modules.get("orchestrator.workbench_api")
        if module is None:
            module = importlib.import_module("orchestrator.workbench_api")
        return module.WorkbenchApiServer

    def _api_gateway_server_cls(self):
        module = sys.modules.get("orchestrator.api_gateway")
        if module is None:
            module = importlib.import_module("orchestrator.api_gateway")
        return module.APIGatewayServer

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
            server_cls = self._workbench_api_server_cls()
            self.kernel.workbench_api = server_cls(
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
        if self.kernel.api_gateway is not None:
            return
        try:
            server_cls = self._api_gateway_server_cls()
            self.kernel.api_gateway = server_cls(
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

    def _resolve_enterprise_database_path(self, raw_url: str | None) -> Path:
        value = str(raw_url or "").strip()
        if not value:
            return self.kernel.paths.bridge_home / "state" / "enterprise.sqlite"
        if value.startswith("sqlite:///"):
            return Path(value[len("sqlite:///"):]).expanduser()
        if "://" in value:
            raise ValueError(f"unsupported enterprise scheduler lease database URL: {value}")
        return Path(value).expanduser()

    def _scheduler_enterprise_database_url(self, raw_url: str | None) -> str:
        value = str(raw_url or "").strip()
        if value:
            return value
        return str(self.kernel.paths.bridge_home / "state" / "enterprise.sqlite")

    def _scheduler_enterprise_lease_kwargs(self, global_cfg) -> dict:
        if not bool(getattr(global_cfg, "enterprise_scheduler_lease_enabled", False)):
            return {}
        holder = (
            getattr(global_cfg, "enterprise_scheduler_lease_holder", None)
            or os.environ.get("POD_NAME")
            or f"{getattr(global_cfg, 'instance_id', 'HASHI')}:{socket.gethostname()}:{os.getpid()}"
        )
        lease_name = str(getattr(global_cfg, "enterprise_scheduler_lease_name", None) or "superloop-scheduler")
        lease_ttl_seconds = max(1, int(getattr(global_cfg, "enterprise_scheduler_lease_ttl_seconds", 60) or 60))
        backend = str(getattr(global_cfg, "enterprise_scheduler_lease_backend", "db") or "db").strip().lower()

        if backend in {"k8s", "kubernetes"}:
            try:
                from orchestrator.enterprise import (
                    KubernetesApiLeaseClient,
                    KubernetesLeaseCoordinator,
                    KubernetesSchedulerLeaseStore,
                )

                namespace = (
                    getattr(global_cfg, "enterprise_scheduler_lease_kubernetes_namespace", None)
                    or os.environ.get("POD_NAMESPACE")
                    or "hashi-enterprise"
                )
                client = KubernetesApiLeaseClient.from_config(
                    in_cluster=bool(getattr(global_cfg, "enterprise_scheduler_lease_kubernetes_in_cluster", True)),
                    kubeconfig_path=getattr(global_cfg, "enterprise_scheduler_lease_kubeconfig_path", None),
                )
                lease_store = KubernetesSchedulerLeaseStore(
                    KubernetesLeaseCoordinator(client, namespace=str(namespace))
                )
            except Exception as exc:
                main_logger.warning("Enterprise scheduler Kubernetes lease disabled: %s", exc)
                bridge_logger.warning("Enterprise scheduler Kubernetes lease disabled: %s", exc)
                return {}
            return {
                "enterprise_lease_store": lease_store,
                "enterprise_lease_name": lease_name,
                "enterprise_lease_holder": str(holder),
                "enterprise_lease_ttl_seconds": lease_ttl_seconds,
            }

        if backend != "db":
            main_logger.warning("Enterprise scheduler lease disabled: unsupported backend %s", backend)
            bridge_logger.warning("Enterprise scheduler lease disabled: unsupported backend %s", backend)
            return {}

        try:
            from orchestrator.enterprise import EnterpriseLeaseStore

            database_url = self._scheduler_enterprise_database_url(
                getattr(global_cfg, "enterprise_database_url", None)
            )
            org_id = (
                getattr(global_cfg, "organization_id", None)
                or os.environ.get("HASHI_ORGANIZATION_ID")
                or os.environ.get("HASHI_ENTERPRISE_ORG_ID")
                or "ORG-001"
            )
            lease_store = EnterpriseLeaseStore.from_url(
                database_url,
                org_id=org_id,
                postgres_pool=bool(getattr(global_cfg, "enterprise_scheduler_lease_pool_enabled", False)),
                postgres_pool_min_size=max(
                    1,
                    int(getattr(global_cfg, "enterprise_scheduler_lease_pool_min_size", 1) or 1),
                ),
                postgres_pool_max_size=max(
                    1,
                    int(getattr(global_cfg, "enterprise_scheduler_lease_pool_max_size", 4) or 4),
                ),
            )
        except Exception as exc:
            main_logger.warning("Enterprise scheduler DB lease disabled: %s", exc)
            bridge_logger.warning("Enterprise scheduler DB lease disabled: %s", exc)
            return {}

        return {
            "enterprise_lease_store": lease_store,
            "enterprise_lease_name": lease_name,
            "enterprise_lease_holder": str(holder),
            "enterprise_lease_ttl_seconds": lease_ttl_seconds,
        }

    def start_scheduler(self, global_cfg):
        self.kernel.scheduler = TaskScheduler(
            self.kernel.paths.tasks_path,
            self.kernel.paths.state_path,
            self.kernel.runtimes,
            global_cfg.authorized_id,
            self.kernel.skill_manager,
            orchestrator=self.kernel,
            **self._scheduler_enterprise_lease_kwargs(global_cfg),
        )
        self.kernel.scheduler_task = asyncio.create_task(self.kernel.scheduler.run(), name="scheduler")

    def start_delivery_health_watcher(self):
        existing = getattr(self.kernel, "delivery_health_task", None)
        if existing is not None and not existing.done():
            return
        self.kernel.delivery_health_task = asyncio.create_task(
            delivery_health_watcher(self.kernel),
            name="telegram-delivery-health",
        )

    async def stop_delivery_health_watcher(self):
        task = getattr(self.kernel, "delivery_health_task", None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.kernel.delivery_health_task = None

    async def restart_delivery_health_watcher(self):
        await self.stop_delivery_health_watcher()
        self.start_delivery_health_watcher()
        main_logger.info("Hot restart: delivery health watcher recreated with reloaded code.")
        bridge_logger.info("Hot restart: delivery health watcher recreated with reloaded code")

    async def start_background_jobs(self):
        existing = getattr(self.kernel, "background_job_manager", None)
        if existing is not None:
            return existing
        manager = BackgroundJobManager(
            self.kernel.paths.bridge_home / "state" / "background_jobs",
            kernel=self.kernel,
        )
        await manager.start()
        self.kernel.background_job_manager = manager
        main_logger.info("Background job manager started.")
        bridge_logger.info("Background job manager started")
        return manager

    async def stop_background_jobs(self):
        manager = getattr(self.kernel, "background_job_manager", None)
        if manager is None:
            return
        await manager.stop()
        self.kernel.background_job_manager = None
        main_logger.info("Background job manager stopped.")
        bridge_logger.info("Background job manager stopped")

    async def restart_background_jobs(self):
        await self.stop_background_jobs()
        module = sys.modules.get("orchestrator.background_jobs")
        manager_cls = BackgroundJobManager if module is None else module.BackgroundJobManager
        manager = manager_cls(
            self.kernel.paths.bridge_home / "state" / "background_jobs",
            kernel=self.kernel,
        )
        await manager.start()
        self.kernel.background_job_manager = manager
        main_logger.info("Hot restart: background job manager recreated with reloaded code.")
        bridge_logger.info("Hot restart: background job manager recreated with reloaded code")
        return manager

    async def start_runtime_services(self, global_cfg, secrets):
        self.build_agent_directory()
        await self.start_workbench_api(global_cfg, secrets)
        await self.start_api_gateway(global_cfg, secrets)
        self.start_scheduler(global_cfg)
        self.start_delivery_health_watcher()
        await self.start_background_jobs()

    async def stop_scheduler(self, timeout: float = 5.0):
        if self.kernel.scheduler_task is None:
            return
        scheduler = self.kernel.scheduler
        bridge_logger.info("Stopping scheduler task")
        self.kernel.scheduler_task.cancel()
        try:
            await asyncio.wait_for(self.kernel.scheduler_task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            bridge_logger.warning("Scheduler task stop timed out or was cancelled")
        finally:
            lease_store = getattr(scheduler, "enterprise_lease_store", None)
            close = getattr(lease_store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:
                    bridge_logger.warning("Enterprise scheduler lease store close failed: %s", e)
            self.kernel.scheduler_task = None
            self.kernel.scheduler = None

    async def restart_scheduler(self):
        await self.restart_workbench_api()
        await self.restart_api_gateway()
        await self.stop_scheduler()
        reloaded_scheduler = sys.modules["orchestrator.scheduler"].TaskScheduler
        lease_kwargs = (
            self._scheduler_enterprise_lease_kwargs(self.kernel.global_cfg)
            if self.kernel.global_cfg
            else {}
        )
        self.kernel.scheduler = reloaded_scheduler(
            self.kernel.paths.tasks_path,
            self.kernel.paths.state_path,
            self.kernel.runtimes,
            self.kernel.global_cfg.authorized_id if self.kernel.global_cfg else 0,
            self.kernel.skill_manager,
            orchestrator=self.kernel,
            **lease_kwargs,
        )
        self.kernel.scheduler_task = asyncio.create_task(self.kernel.scheduler.run(), name="scheduler")
        main_logger.info("Hot restart: scheduler recreated with reloaded code.")
        bridge_logger.info("Hot restart: scheduler recreated with reloaded code")
        await self.restart_delivery_health_watcher()
        await self.restart_background_jobs()

    async def restart_workbench_api(self):
        if self.kernel.global_cfg is None:
            bridge_logger.warning("Hot restart: Workbench API restart skipped because global config is unavailable")
            return
        await self.stop_workbench_api(timeout=2.0)
        await self.start_workbench_api(self.kernel.global_cfg, self.kernel.secrets)
        if self.kernel.workbench_api is not None:
            bridge_logger.info("Hot restart: Workbench API recreated with reloaded code")

    async def restart_api_gateway(self):
        """Recreate an enabled Gateway so one /reboot adopts reloaded code."""
        if self.kernel.global_cfg is None:
            bridge_logger.warning("Hot restart: API Gateway restart skipped because global config is unavailable")
            return
        state = self._load_api_gateway_state()
        should_run = bool(
            self.kernel.api_gateway is not None
            or getattr(self.kernel, "enable_api_gateway", False)
            or state.get("enabled")
        )
        if not should_run:
            return

        self.kernel.enable_api_gateway = True
        await self.stop_api_gateway(timeout=2.0)
        await self.start_api_gateway(self.kernel.global_cfg, self.kernel.secrets)
        if self.kernel.api_gateway is not None:
            bridge_logger.info("Hot restart: API Gateway recreated with reloaded code")
        else:
            bridge_logger.warning("Hot restart: API Gateway failed to restart")

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
        await self.stop_delivery_health_watcher()
        await self.stop_background_jobs()
        await self.stop_workbench_api()
        await self.stop_api_gateway()
